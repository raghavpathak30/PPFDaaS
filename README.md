# PPFDaaS (Privacy-Preserving Fraud Detection as a Service)

PPFDaaS is a privacy-preserving payment-fraud inference system built on CKKS homomorphic
encryption. The bank encrypts transaction features locally, the vendor evaluates a linear
model on ciphertext only, and only the bank ever decrypts a score. Plaintext transaction
data never leaves the bank; the deployed vendor server never holds a secret key.

This README is a map into three primary sources, in priority order for anything
contract-sensitive:

1. `docs/spec.md` (v1.1) — normative CKKS parameters, proto contract, threat model (§6),
   rotation-strategy taxonomy (§7), transciphering threat model (§8).
2. `PROJECT_STATE.md` — session-by-session execution history.
3. `AUDIT.md` — measurement-integrity findings (CPU governor, cross-architecture
   confounds, what is/isn't apples-to-apples). Read this before citing any latency number.

## Trust Boundary & Security Architecture

The deployed vendor server (`vendor_server_160`, built from
`vendor_server/src/inference_service_160.cpp` on `EvalContext160`,
`vendor_server/include/eval_context_160.h`) has **no `seal::SecretKey`, no `seal::Decryptor`,
no `seal::KeyGenerator`, and no `seal::PublicKey`/`seal::Encryptor`** anywhere in the linked
process — not even transiently. It can only encode plaintext model weights and evaluate
(`multiply_plain`, `rescale`, `rotate_vector`). Galois keys are never read from a local file;
they arrive only through an explicit provisioning protocol (below) driven entirely by the
bank, over a channel that can be mutually authenticated TLS
(`scripts/generate_dev_certs.sh`, §6.7). Full semi-honest threat model, adversary model, and
the honest limits of what this system protects (input privacy, **not** model privacy — see
§6.5): `docs/spec.md` §6.

A second, legacy 200-bit server (`vendor_server_main`, `inference_service.cpp`,
`CKKSContext`) also exists in this repo, used only for the §5.7 self-ablation measurements.
It **does** hold a secret key and is a structurally different codebase from
`vendor_server_160` — not part of any deployment story. `CKKSContext160`
(`ckks_context_160.{h,cpp}`) and the `benchmark`/`benchmark_160` local-circuit binaries are
similarly secret-key-holding, self-timing tools, explicitly out of TCB and never linked into
`Dockerfile.server`'s target (`vendor_server_160`).

### Provisioning / Canary State Machine

Replacing an earlier design where Galois keys were bind-mounted from a shared host
directory, `vendor_server_160` now boots holding **no** key material, in state
`PROV_AWAITING_KEYS`, and refuses `RunInference` (`ERR_NOT_PROVISIONED`) until the bank
drives it through (`proto/inference.proto`, `vendor_server/include/provisioning_state.h`):

```
PROV_INIT -> PROV_AWAITING_KEYS -> PROV_VALIDATING -> PROV_READY
                     \                   |                |
                      \------------> PROV_FAULT <---------/
```

1. **`ProvisionGaloisKeys`** — the bank pushes an *evaluation-only bundle*: serialized
   Galois keys plus the encryption parameters they were generated under. No bytes that
   grant decryption are ever sent. Relin keys are deliberately never provisioned — the
   depth-1 circuit has no ciphertext × ciphertext multiply, so there is nothing for them to
   do. The server checks the byte stream deserializes correctly, that its `parms_id`
   matches the server's own hardcoded parameters, and that every element of
   `ROTATION_STEPS = {1,2,4,8,16,32,64,128}` is present. Failure → `PROV_FAULT`.
2. **`CanaryCheck` / `CanaryConfirm`** — this rung alone can catch "structurally valid
   Galois keys generated under the wrong secret key," and it does so **without the secret
   key ever leaving the bank**: the bank encrypts a known constant, the server applies the
   real production rotation schedule and returns the (still-opaque-to-it) result, and the
   bank decrypts locally and reports back a pass/fail verdict via `CanaryConfirm`. `passed`
   → `PROV_READY`; anything else → terminal `PROV_FAULT`.
3. **Continuous validation** — every subsequent `RunInference` call re-checks
   `ciphertext.parms_id()`; 3 consecutive mismatches while `PROV_READY` also trips
   `PROV_FAULT`.

`PROV_FAULT` is terminal: no in-process recovery, no degraded mode, no substitute-key
fallback — a process restart and full re-provisioning is the only way out. Full state-machine
and adversary-model detail: `docs/spec.md` §6.2–§6.3.

## What This Repository Contains

- A production-oriented Depth-1 CKKS inference path (n=8192), eval-only on the server by
  construction, with a fail-closed provisioning protocol (above).
- Two CKKS parameter variants at n=8192/tc128 (128-bit security): a 200-bit baseline
  (`{60,40,40,60}`) and a 160-bit reduced/deployed variant (`{60,40,60}`) — see "CKKS
  Parameters" below.
- A Degree-2 fallback path (n=16384), specified and scaffolded but currently **broken** — the
  bank-side feature expansion crashes before producing weights (see "Degree-2 Fallback"
  below) — automatically selected by an AUC gate when the Depth-1 model doesn't clear an
  accuracy bar.
- Three named, measured rotation/reduction strategies (SEAL sequential fold, SEAL BSGS
  two-layer, and cross-library hoisting comparisons via OpenFHE and Lattigo) —
  `docs/spec.md` §7.
- A research arm for HHE/transciphering (HERA-16 and Rubato-128L symmetric ciphers as CKKS
  upload-bandwidth reducers, selectable via `CipherBackend`) — client side measured for both,
  server side validated end-to-end only at toy scale, not yet integrated. See
  "Transciphering / Hybrid HE (HHE) Arm" below and `docs/spec.md` §8.
- End-to-end plumbing across C++, Python, Go, gRPC/protobuf, with an honest-measurement
  discipline: every timing run is parity-gated against a plaintext oracle
  (`scripts/parity_gate.py`) before being trusted, and every artifact is reproducible via
  `scripts/reproduce_all.py` / `make reproduce`.

## Model: Independent LR Surrogate, Not a Distillation of XGBoost

The HE-evaluated model is an **independently trained** `LogisticRegression` fit directly on
the 256-feature dataset (`compiler/train_logistic_regression.py`). XGBoost
(`compiler/train_xgboost.py`) is trained on the same features and used **only** to validate
the dataset/feature pipeline and to establish an accuracy ceiling (target AUC ≥ 0.98) — it is
**not** linearized, distilled, or otherwise compressed into the LR model; there is no
SHAP-based or least-squares surrogate step. The gap between the two is reported directly as

```
linearization_cost_auc = xgb_test_auc - lr_test_auc
```

in `artifacts/linearization_cost.json`, framed honestly as "the accuracy cost of using a
linear model the HE circuit can evaluate at depth 1, relative to a non-linear ceiling," not
as an approximation-error bound on a distillation step.

## Dataset & Feature Contract

- Source: the ULB `creditcard.csv` dataset (`data/creditcard.csv`, fetched via
  `scripts/fetch_creditcard_dataset.sh`, not tracked in git).
- `compiler/train_xgboost.py` scales, winsorizes, clips, expands with degree-2 polynomial
  interactions, and truncates to a fixed **256-feature** contract.
- Batches of up to 16 transactions are packed into a single ciphertext as a **16×256 slot
  layout** (4096 of the 4096 available slots at n=8192, one transaction per 256-slot lane).

## CKKS Parameters

Both variants use `poly_modulus_degree = 8192`, `scale = 2^40`, SEAL 4.1.2, and
`sec_level_type::tc128` (128-bit security), and provision the same restricted Galois key set
`{1,2,4,8,16,32,64,128}`:

| Variant | `coeff_modulus` | Total bits | Spare mult. levels after depth-1 circuit | Status |
|---|---|---|---|---|
| 200-bit baseline | `{60,40,40,60}` | 200 | 1 | Self-ablation reference only (`vendor_server_main`) |
| 160-bit reduced | `{60,40,60}` | 160 | 0 | **Deployed default** (`vendor_server_160`) |

n=8192 permits up to 218 total `coeff_modulus` bits at 128-bit security per the HE standard
parameter tables, so both variants sit at the identical security level — the trade-off is
purely operational (the 160-bit variant has no headroom for future circuit-depth increases
without a full key regeneration). Recommended production default is 160-bit; see
`docs/spec.md` §5.5 for the exact condition under which 200-bit should be preferred instead.

### Depth-1 Circuit

One `multiply_plain` (ciphertext × plaintext model weights) → one `rescale_to_next` → an
**8-step sequential-fold tree-sum** over the Galois key set above
(`vendor_server/src/rotation_hoisting.cpp::hoisted_tree_sum`, doubling steps 1→128,
`log2(256) = 8`). Rotations consume no multiplicative level; only the one `multiply_plain`
does. After the fold, `acc.slot[k*256]` holds transaction `k`'s dot product for `k = 0..15`;
the bias is added server-side as a `add_plain_inplace` afterward (§6.5) and the client applies
`sigmoid`.

### Degree-2 Fallback (Specified, Currently Broken)

Selected automatically by `compiler/auc_dispatch.py` when the Depth-1 LR's AUC falls below
0.92 (borderline retry zone: [0.92, 0.94); primary path: ≥ 0.94). It is a **separate CKKS
context and binary**, not a config flag:

- `n = 16384`, `coeff_modulus = {60,40,40,40,60}` — all keys must be regenerated (n=8192 keys
  are incompatible).
- Bank-side plaintext polynomial expansion (`compiler/degree2_linearizer.py`,
  `bank_client/backend/feature_pipeline_degree2.py`): top-256 linear terms + top-32-feature
  pairwise interactions (`C(32,2) = 496` terms, zero-padded) = 512 features per transaction,
  packed 16×512 into the 8192-slot ciphertext.
- The HE circuit itself stays depth-1 (`multiply_plain` + rescale + a 9-step tree-sum for 512
  features) — the added expressivity comes entirely from the plaintext feature engineering,
  not from a deeper HE circuit, so no relinearization keys are needed here either.
- AUC gate: ≥ 0.96 required to accept the fallback weights
  (`compiler/serialize_degree2_weights.py`, `artifacts/degree2_weights.bin`, 4108 bytes).
- **Broken:** `compiler/degree2_linearizer.py`'s `build_degree2_features` computes
  `N_FEATURES_D2 - N_TOP_LINEAR - N_INTERACT = 512 - 256 - 496 = -240`, an invalid negative
  `np.zeros()` dimension. Calling `linearize_degree2(...)` raises `ValueError` immediately,
  before any weights are fit. Only the CKKS-context construction and the
  weight-serialization round-trip (`serialize_degree2_weights.py`) have actually been run;
  the feature-expansion step itself has never successfully executed.
- On the current dataset/pipeline, Depth-1 AUC is 0.979 (`artifacts/dispatch_result.json`) —
  the primary path is active, so this crash has never been hit in production.

## Proto Contract (`proto/inference.proto`)

`TimingBreakdown` carries **5 fields**, with `deserialization_us` explicitly as field 1 (not
an afterthought bolted onto the end) so that `ct.load()` deserialization time is captured
rather than silently folded into a residual:

```protobuf
message TimingBreakdown {
    int64 deserialization_us   = 1;  // ct.load() time
    int64 multiply_plain_us    = 2;  // multiply_plain + rescale
    int64 rotation_hoisting_us = 3;  // hoisted_tree_sum
    int64 serialization_us     = 4;  // ct.save() into response
    int64 total_inference_us   = 5;  // full RunInference wall time
}
```

Invariant: `deserialization_us + multiply_plain_us + rotation_hoisting_us + serialization_us`
≈ `total_inference_us` (residual ≤ ~0.3 ms). The service also exposes the provisioning RPCs
(`ProvisionGaloisKeys`, `CanaryCheck`, `CanaryConfirm`, `GetProvisioningStatus`) and a
Phase-7 transciphering canary RPC (`CanaryCheckTranscipher`, currently stub-only — see below).

## Rotation/Reduction Strategy Taxonomy (`docs/spec.md` §7)

**Terminology note first:** SEAL's public `Evaluator::rotate_vector` does not expose true
Halevi-Shoup hoisting (shared digit-decomposition across rotations of the same source
ciphertext) — so despite the name, neither strategy below implemented against SEAL is
"hoisted" in that technical sense. Three strategies are measured:

1. **Sequential fold** (`hoisted_tree_sum`, SEAL) — 8 rotations of the accumulator, critical
   path 8, not parallelizable. The only strategy the deployed server actually provisions and
   runs.
2. **BSGS two-layer** (`bsgs_reduction`, SEAL, Phase 4.1) — 30 rotations (15 baby + 15 giant)
   across 2 independent, OpenMP-parallelizable layers. More total work, shorter critical
   path. Requires a superset Galois key set (30 elements) not provisioned in production.
3. **Hoisted flat** (OpenFHE / Lattigo, cross-library) — same 30-rotation BSGS set,
   reimplemented against a library that *does* expose genuine hoisting
   (`EvalFastRotation`/`RotateHoistedNew`).

Key finding (§7.5.2, matched-ring N=8192, governor-validated, `tools/lattigo_benchmark/`):
Lattigo's genuinely-hoisted BSGS was measured **~5x slower (mean), ~4x slower (p99)** than
SEAL's 6-core-OMP BSGS at the same ring/rotation-set — decomposed into a 3.72x OMP-parallelism
factor and a 1.34x net Go-vs-C++/language-and-hoisting factor that are not separately
isolated. **The "genuine hoisting removes SEAL's public-API ceiling" hypothesis is not
demonstrated by any measurement available in this repo** — the one library with a hoisting
API is net-slower at matched thread count, for reasons this repo's data cannot fully
decompose. An earlier attempt at this comparison using OpenFHE (§7.5.1) was invalidated by a
ring-dimension confound (OpenFHE's parameter generator rejects N=8192 at this depth/security
level and silently uses N=16384); that result (SEAL ~25x faster than OpenFHE) is kept for
transparency but is not a clean library-only delta. Full derivation, consistency checks, and
the two remaining un-run comparisons: `docs/spec.md` §7.5–§7.5.2.

## Benchmark Framing: Type 1 / 2 / 3 (`docs/spec.md` §5.7)

Every latency number in this repo is exactly one of three comparison types, and conflating
them is the single most common way to overclaim from this codebase:

- **Type 1 — Self-ablation.** Same circuit, same hardware, only the CKKS modulus chain
  differs (200-bit vs 160-bit). This is what the 160-bit-vs-200-bit numbers below are, and
  **only** what they are — never cite them as beating an external library or a different
  reduction strategy.
- **Type 2 — Reduction-strategy comparison.** Same codebase, same modulus chain (160-bit),
  strategy differs (fold vs BSGS vs naive). `artifacts/rotation_strategy_comparison.json`,
  `artifacts/execution_matrix.json`.
- **Type 3 — Cross-library comparison.** Same circuit, different HE library entirely
  (OpenFHE, Lattigo), for external validation that the numbers aren't a SEAL-build artifact.
  OpenFHE's cell is **PENDING** in this environment (not installed); Lattigo's ran (§7.5.2
  above).

### 160-bit vs 200-bit (Type 1), governor-validated 2026-06-29

`cpu_governor=performance`, turbo disabled, taskset-pinned to distinct physical cores, 13th
Gen Intel Core i7-13650HX, n=1000/arm, in-band parity gate passed both arms
(`artifacts/comparison_results.json`):

| | mean (µs) | median (µs) |
|---|---|---|
| 200-bit baseline | 14,877.6 | 14,972.0 |
| 160-bit reduced | 7,538.1 | 7,563.5 |

Reduction: ~49% (mean), ~49% (median), Mann-Whitney U=1,000,000, p≈0. **This is a Type 1
self-ablation only** — it does not, and has never claimed to, represent a comparison against
any external baseline (the previously-circulated 3.92x figure was retired; see
`docs/spec.md` §5.7 for the correction history and why an earlier "same binary family"
framing was itself inaccurate — the current clean architecture-matched comparison is
`vendor_server/build/benchmark` vs `benchmark_160`, both local-circuit-only,
`scripts/privacy_cost_matched_pair.py` → `artifacts/privacy_cost_matched_pair.json`).

**Fold beats BSGS (Type 2)**, and cross-library hoisting does not currently beat SEAL's
unhoisted BSGS at matched thread count (Type 3) — see "Rotation/Reduction Strategy Taxonomy"
above. Type 3 vs OpenFHE remains PENDING (not installed in this environment).

**Hardware/CPU-governor caveat:** the current dev host has no root access to switch the CPU
governor by default; numbers above are explicitly governor-validated (`performance`, turbo
disabled) where stated, and every artifact JSON that isn't carries a `"status"` field noting
its governor state — do not average across governor states, and see `AUDIT.md` for why some
percentages (not absolute deltas) moved materially between `powersave` and `performance`
re-runs.

## Transciphering / Hybrid HE (HHE) Arm — Partial (`docs/spec.md` §8)

The HHE arm replaces the bank's CKKS ciphertext upload with a symmetric-cipher online phase,
keeping the server-side HE computation identical; the vendor's TCB is unchanged (§8.1). Two
ciphers are implemented and benchmarked, selectable via `bench --cipher=hera|rubato`:
**HERA-16** (m=16, r=5, t=2^26 — the selected cipher, §8.8) and **Rubato-128L** (n=64, r=2,
q=0x1fc0001, per `tools/transciphering/cipher/rubato.go:78-80`), implemented afterward as a
benchmarking comparison point, not as a reversal of the HERA-16 selection.

**Rubato-128L status.** It passes its known-answer test against the pinned `ckks_fv`
reference keystream (`tools/transciphering/cipher/rubato_test.go: TestRubatoKnownAnswer`) and
the toy-scale end-to-end HE correctness harness (below). Security framing: Rubato-128L is
**not covered by the Grassi et al. (CRYPTO 2023) attack bound**. That is a statement about
which Rubato variants the published attack covers; no independent security analysis of this
parameter set exists in this repo. See `docs/spec.md` §8.8 and §8.8.1.

**Client side: complete and measured for both ciphers** (`tools/transciphering/`, standalone
Go module, explicitly not part of the deployed TCB — same status as
`tools/openfhe_benchmark/`). All figures below are same-session (2026-08-18, powersave
governor, AC power), from `artifacts/hera_vs_rubato_transciphering.json`; see
`docs/MEASUREMENT_PROVENANCE.md` for the full source trace and unit reconciliation.

**Per-keystream-element client encrypt** (isolates single-block algebra from
AEAD/quantization overhead, `tools/transciphering/cipher/block_bench_test.go`, median of 7
repetitions; artifact field `per_element_normalization`):

| | ns / keystream element | Ratio vs HERA-16 |
|---|---|---|
| HERA-16 | 251.75 | — |
| Rubato-128L, with Gaussian noise (what `Encrypt()` runs) | 193.73 | 0.77x |
| Rubato-128L, without noise (algebra only) | 143.82 | 0.571x |

**Per-record client encrypt** (full `Encrypt()` incl. AEAD, 256 features/transaction, n=200;
artifact field `axis_b_hera_vs_rubato_client_cipher.by_lane_count`):

| Lanes | HERA-16 (mean/median) | Rubato-128L (mean/median) | Ratio (Rubato/HERA, mean) |
|---|---|---|---|
| 1  | 0.16364 / 0.15 ms  | 0.07917 / 0.058 ms | 0.484x |
| 4  | 0.38755 / 0.359 ms | 0.28006 / 0.247 ms | 0.723x |
| 8  | 0.95596 / 0.971 ms | 0.78315 / 0.66 ms  | 0.819x |
| 16 | 1.64116 / 1.534 ms | 1.15038 / 0.975 ms | 0.701x |

The ns/element and ms/record figures use different units and scopes by design;
`docs/MEASUREMENT_PROVENANCE.md` shows the arithmetic reconciling them (within a 1.2-2.5x
AEAD/quantization/per-call overhead band). Rubato-128L measuring **cheaper** reverses an
earlier measurement (Rubato 1.748-2.246x HERA across the lane sweep) that used an unverified
Box-Muller noise approximation instead of the reference's discrete Gaussian sampler; the old
sampler's own overhead, not Rubato's algebra, was the dominant cost. Preserved, not deleted:
`artifacts/hera_vs_rubato_transciphering_PRIOR_wrong_noise_sampler.json`.

**Upload size is identical between the two ciphers at every lane count** (both use the same
`OnlineCiphertextBytes(n) = 4n+28` wire format): **1,052 bytes** for a single transaction vs
262,257 bytes for standard CKKS (**~249x smaller**); 16,412 bytes vs 262,257 bytes at full
16-lane occupancy (**~16x smaller**). The cipher choice changes client CPU cost only, not
this reduction. Full ladder: `artifacts/bandwidth_ladder.json`.

**Multiplicative depth — read from source, not measured.** From the pinned `ckks_fv` source
(artifact field `multiplicative_depth_argument`, CONFIRMED-SOURCE): **HERA-16 needs 10
sequential ciphertext-multiplication levels** (5 rounds x degree-3 cube S-box, 2 sequential
mults each); **Rubato-128L needs 2** (2 rounds x degree-2 Feistel-square, where all 63
per-round squarings are mutually independent). This depth gap is Rubato's design rationale
(Ha et al., Eurocrypt 2022), but it is a structural count: the homomorphic cost it predicts
has not been measured, because `vendor_server`'s BFV eval is stub-only. The client-side
timings above measure a different cost model (plaintext CPU) and do not confirm it.

### Memory comparisons between HERA and Rubato: INVALID as run

**No HERA-vs-Rubato memory comparison in this repo is a valid result.** Every one was
measured at mismatched `LogSlots`, so each run evaluated a different-sized workload, not just
a different cipher (full trace: `docs/MEASUREMENT_PROVENANCE.md`, "PRIMARY FINDING"):

- **Toy scale (LogN=10), ~2.08x peak RSS (Rubato/HERA): INVALID.** HERA's toy harness uses
  `LogSlots=4`; Rubato's only available param set uses `LogSlots = LogN-1` = **9**.
- **Full scale (LogN=16), peak/lower-bound memory and the ~4.07x StC-precompute delta:
  INVALID.** HERA ran at `LogSlots=4`; Rubato at `LogSlots=15`. The StC precompute
  (`GenSlotToCoeffMatFV`) depends on `LogSlots`, not on cipher block size or round count.

The individual full-scale measurements are real, but they cannot be compared with each other:

- **HERA, `RtFHeraParams[3]` "128as", LogN=16, LogSlots=4 (16 slots): full ring, near-empty
  payload.** `hera.Crypt` completed in 73.7 s with peak VmHWM 9.54 GB under
  `GOMEMLIMIT=11GiB GOGC=50` on this 15 GB dev host (`artifacts/hera_crypt_rss_full_run.jsonl`,
  `vm_hwm_kb: 9543896`; `logs/hera_full_run.log`). **This is not a full-occupancy result** —
  only 16 of the 32,768 available slots carry data. CPU governor and power source were not
  recorded for this run. HERA at `LogSlots=15` has not been run.
- **Rubato-128L, "128af", LogN=16, LogSlots=15 (32,768 slots): did not complete.**
  OOM-killed (SIGKILL) on the 15 GB host during setup. Measured lower bound: **13.28 GB
  VmHWM** (`vm_hwm_kb: 13279632` at `slot_to_coeff_mat`, the last checkpoint before the
  kill, right after the StC precompute), reached before `rubato.Crypt` ran. The true peak is unknown
  (`artifacts/rubato_crypt_rss_full_run.jsonl`, partial trace).

**Planned next step:** a LogSlots-matched comparison — HERA at `RtFHeraParams[2]` "128af"
(LogSlots=15) vs Rubato "128af" (LogSlots=15); see `docs/RUBATO_FULLSCALE_PLAN.md`.

### Server side: reference path validated end-to-end at toy scale, not integrated into `vendor_server`

The remaining step — homomorphic cipher evaluation inside BFV followed by an FV→CKKS repacking
(StC + modular reduction) so the existing CKKS inference circuit runs unmodified — uses
KAIST-CryptLab's `ckks_fv` (`RtF-Transciphering`) scheme bridge. The bridge itself is vendored
and pinned at commit `105fc73115b56f1d6ff357029c7682b19a6d8510` under `third_party/`
(gitignored checkout, reproducible via `third_party/fetch_rtf.sh`, toolchain documented in
`third_party/BUILD_NOTES.md`), and builds clean under Go 1.25 with zero source changes
(`go vet ./...`, `go test -c` both exit 0 in `ckks_fv/`).

- **Toy correctness: PASS for both ciphers.** `LogN 16 → 10` toy copies of the reference
  params, all moduli reused, ran the full server-side path end-to-end (cipher-in-BFV
  transcipher → HalfBoot → FV→CKKS repack → CKKS eval → decrypt). Max abs error
  (`artifacts/hera_vs_rubato_transciphering.json`, `gate_b_toy_he_harness`): HERA
  r=5/"128as" 1.390409e-05; Rubato-128L 3.419122e-05 (includes real Gaussian noise) — both
  against a 5e-2 tolerance. Caveats: the CKKS stage evaluated a trivial `2x+1` circuit, not
  the fraud model; and the two toy runs use different `LogSlots` (4 vs 9), which does not
  affect pass/fail but does invalidate their memory comparison (above).

### Blockers: RAM at real slot occupancy, and integration

- **RAM is again a blocker at real slot occupancy.** The only completed full-scale run (HERA,
  9.54 GB) ran at LogSlots=4 (16 slots). At full occupancy, Rubato-128L "128af" was
  OOM-killed on the 15 GB host, and HERA at `LogSlots=15` is untested. A cloud runbook is
  scaffolded at `scripts/cloud_transcipher_bench/` but has not been run; its ~48 GB free-RAM preflight gate
  is an **estimate**, not a measurement (derivation in `run_benchmark.sh` and
  `docs/RUBATO_FULLSCALE_PLAN.md`).
- **Integration is also still missing.** `vendor_server`'s BFV evaluation is stub-only
  (`CanaryCheckTranscipher`, `vendor_server/src/inference_service_160.cpp:263-287`), and all
  36 cells of `artifacts/hhe_breakeven.json` have `online_transcipher_ms` and `repacking_ms`
  = `"PENDING"`. **There is no end-to-end HHE-vs-CKKS latency verdict.**
- Other open items: batched-reduction correctness at 256-slot blocks (everything above is
  single-block); the toy harness runs `2x+1`, not the real fraud circuit. Two literature
  fallbacks were checked for an external server-side number and both dead-ended (RtF Table 5
  is unreachable — HTTP 403 on every accessible mirror; Presto measures client-side
  stream-key generation on bank hardware, not server-side HE evaluation) — see
  `docs/spec.md` §8.3 for the full trail.

**The honest framing of this arm's current contribution is bandwidth reduction, not
latency.** §8.10 additionally shows the CPU-time motivation for HHE is regime-dependent, not
a blanket win: at single-transaction granularity the server dominates total latency and
client-encrypt choice barely matters; at full 16-lane batch occupancy, client-encrypt does
become the bottleneck (5.021x the server's amortized per-transaction cost,
`artifacts/c3_comparison.json`) — but at that same occupancy, HERA-16's encrypt cost (4821.0
µs median) exceeds plain CKKS's (4181.8 µs), so switching ciphers would not reduce the
now-dominant client-side cost at that operating point. Caveat: §8.10's HERA figures come from
a 2026-06-28 run of the HERA implementation that predates the 2026-08-18 round-key rewrite
(governor-validated snapshot under `artifacts/performance_revalidation/performance/`), in a
different session from the HERA/Rubato figures above, and have no Rubato counterpart; do not combine
them. The surviving, unconditional HHE advantage at every occupancy is upload bandwidth
(~16-249x smaller), not CPU time. **The open empirical question this repo has not yet
answered is the bandwidth-savings-vs-server-transcipher-compute break-even** — do not read
any existing artifact as implying HHE currently beats CKKS end-to-end; it hasn't been
measured.

## Quickstart

```bash
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel

echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor  # optional, recommended before benchmarking

source .venv/bin/activate
python scripts/demo_e2e.py
```

Optional benchmark evidence:

```bash
python3 tests/benchmark_comparison.py
```

## Core Stack

- C++: Microsoft SEAL 4.1.2, gRPC, protobuf, OpenMP, CMake
- Python: scikit-learn, XGBoost, numpy, pybind11 bindings
- Go: `tools/transciphering/`, `tools/lattigo_benchmark/` (research/comparison tools, not
  in the deployed TCB)
- Tooling: Catch2 tests, Python verification and benchmark scripts

## Repository Layout (Key Paths)

- Bank side:
  - `bank_client/bank_client.py`
  - `bank_client/backend/feature_pipeline_degree2.py`
  - `bank_client/he_wrapper/seal_wrapper.cpp`, `seal_wrapper_160.cpp`
- Vendor side:
  - `vendor_server/src/`, `vendor_server/include/`
  - `vendor_server/include/eval_context_160.h` — deployed, eval-only capability surface
  - `vendor_server/include/provisioning_state.h` — fail-closed state machine
  - `vendor_server/tests/`
- Compiler / data pipeline:
  - `compiler/train_xgboost.py`, `train_logistic_regression.py`
  - `compiler/degree2_linearizer.py`, `serialize_weights.py`, `serialize_degree2_weights.py`
  - `compiler/auc_dispatch.py`, `gen_keys_160.py`
- Interface definitions:
  - `proto/inference.proto`, `vendor_server/generated/`
- Validation and benchmarking:
  - `tests/verify_all.py`, `tests/benchmark_comparison.py`, `tests/benchmark_throughput.py`
  - `scripts/parity_gate.py` — in-band correctness gate, required before any timing is trusted
  - `scripts/rotation_strategy_comparison.py`, `build_execution_matrix.py`
  - `scripts/privacy_cost_analysis.py`, `privacy_cost_matched_pair.py`
  - `scripts/e2e_latency_breakdown.py`, `c3_client_server_comparison.py`, `bandwidth_ladder.py`,
    `hhe_breakeven.py`
  - `scripts/reproduce_all.py` (`make reproduce` / `make dry-run`), `demo_e2e.py`
- Cross-library / cross-arm research tools (not part of the deployed TCB):
  - `tools/openfhe_benchmark/` — OpenFHE hoisted-flat comparison, §7.4/§7.5
  - `tools/lattigo_benchmark/` — Lattigo hoisted-BSGS comparison, §7.5.2
  - `tools/transciphering/` — HHE client-side arm (HERA-16 and Rubato-128L), §8
  - `scripts/cloud_transcipher_bench/` — scaffolded, not yet run, cloud runbook for the
    server-side transcipher benchmark at real slot occupancy (see "Blockers" under the HHE
    arm above and `docs/RUBATO_FULLSCALE_PLAN.md`)
  - `tools/local_benchmark/` — secret-key-holding 160-bit benchmark context
- Vendored reference implementations (gitignored checkouts, not part of the deployed TCB):
  - `third_party/openfhe-development/` — vendored OpenFHE checkout, §7.5.1
  - `third_party/RtF-Transciphering/` — vendored KAIST `ckks_fv` bridge, pinned via
    `third_party/fetch_rtf.sh` (toolchain notes: `third_party/BUILD_NOTES.md`), §8
- Docs / state tracking:
  - `docs/spec.md` — full technical spec
  - `docs/SESSION_LOG.md` — dated session-by-session history
  - `PROJECT_STATE.md` — current state summary (start here)
  - `docs/MEASUREMENT_PROVENANCE.md` — source trace for every HERA/Rubato timing and memory
    figure, including which comparisons are invalid
  - `docs/PAPER_HANDOFF.md` — what the transciphering arm can and cannot back up, for paper
    writing

## Build

```bash
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

Notes:
- If protobuf is installed from Debian packages, CMake module mode for protobuf is expected.
- gRPC plugin path is typically `/usr/bin/grpc_cpp_plugin` on Debian-based systems.

## Dataset Setup

The ULB credit-card dataset is required locally as `data/creditcard.csv`; it is intentionally
not tracked in git (repository size limits).

```bash
bash scripts/fetch_creditcard_dataset.sh
```

Supports Kaggle CLI mode (automatic, requires `kaggle` credentials) or manual mode (prints
the exact expected path so you can place the CSV yourself).

## Full Demo Sequence (Copy/Paste)

### 0) Build Once

```bash
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

### 1) Prepare Dataset

```bash
bash scripts/fetch_creditcard_dataset.sh
```

### 2) Generate Model + HE Artifacts

```bash
python3 compiler/train_xgboost.py
python3 compiler/train_logistic_regression.py
python3 compiler/gen_keys_160.py
```

### 3) Run Contract/Structure Verification

```bash
python3 tests/verify_all.py
ctest --test-dir vendor_server/build --output-on-failure
```

### 4) Run Performance Comparison (Auto-starts both servers)

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
python3 tests/benchmark_comparison.py
```

Notes:
- The benchmark prints a CPU governor warning when not in `performance` mode.
- Gate thresholds are calibrated to reference hardware; cross-machine variance is expected.

Professor trace mode (full per-request pipeline trace to stderr):

```bash
TRACE=1 python3 tests/benchmark_comparison.py
```

### 5) Run the End-to-End Encrypted Inference Demo

```bash
source .venv/bin/activate
python scripts/demo_e2e.py
```

If the script appears stuck with no output, pull latest changes and rerun — startup
readiness uses TCP port checks, not buffered server stdout.

### 6) Generate Research Figures/Tables

```bash
python3 scripts/generate_research_artifacts.py
python3 scripts/generate_ablation.py
python3 scripts/generate_roc.py
```

### Accuracy Check (Professor-Ready)

```bash
python3 scripts/show_accuracy_check.py
```

### 7) Where Outputs Land

- Result plots and CSV/JSON summaries: `results/`
- Benchmark JSON: `artifacts/comparison_results.json`
- Additional logs: `logs/`

### Optional: Manual Server Run (Separate Terminal)

Terminal A:

```bash
./build/vendor_server/vendor_server_160 artifacts/model_weights.bin 50052
```

Terminal B:

```bash
python3 scripts/generate_ablation.py
python3 scripts/demo_e2e.py
```

## Verification and Testing

```bash
python3 tests/verify_all.py
ctest --test-dir vendor_server/build --output-on-failure
```

For focused performance evidence generation:

```bash
python3 tests/benchmark_comparison.py
python3 scripts/generate_research_artifacts.py
python3 scripts/generate_ablation.py
python3 scripts/generate_roc.py
```

Prefer the `performance` CPU governor before running `tests/benchmark_comparison.py` for
stable cross-run comparisons.

## Measurement Integrity

This repository's governing rule: a number appears in an artifact or the paper only if it
was produced by executing the thing it describes. No estimated, synthesized, or interpolated
point numbers. Where a measurement could not be made (missing hardware access, missing
library, insufficient RAM), the artifact says `"status": "PENDING"` with a concrete reason —
never a fabricated value. `AUDIT.md` is the canonical record of what was checked; key points:

- Every latency number in `artifacts/` states the CPU governor it was measured under
  (`hardware_manifest.cpu_governor` where present) — governor-validated (`performance`,
  turbo disabled) figures and superseded `powersave` figures are both kept, clearly labeled;
  do not mix them.
- The §5.8 privacy-cost number was de-confounded: the original measurement compared two
  structurally different server architectures (200-bit decrypt-capable legacy service vs
  160-bit eval-only service), not just two modulus chains. The corrected,
  architecture-matched number is in `artifacts/privacy_cost_matched_pair.json`; the old,
  confounded number is kept under `deployed_cross_architecture_e2e_delta_DEPRECATED` for
  transparency, not citation.
- OpenFHE vs SEAL BSGS (§7.5.1) is confounded by ring dimension (OpenFHE's own parameter
  generator rejects N=8192 at this depth/security level and silently uses N=16384); the
  matched-ring comparison (§7.5.2) uses Lattigo instead and is the one that should be cited
  for the hoisting question.
- Server-side HHE transciphering (FV→CKKS) is PENDING for two reasons: RAM at real slot
  occupancy, and the missing `vendor_server` integration (BFV eval is stub-only) — see
  "Transciphering / Hybrid HE (HHE) Arm" above and `tools/transciphering/README.md`.

## Source of Truth

- Normative engineering spec and contracts: `docs/spec.md` (CKKS parameters, proto fields,
  threat model §6, rotation taxonomy §7, transciphering §8)
- Implementation handoff and sprint notes: `PROJECT_STATE.md`
- Measurement-integrity audit (governor, confounds, transciphering blocker): `AUDIT.md`
- Remediation history and current one-line status: `PPFDaaS_REMEDIATION_PLAN.md`
- HERA-vs-Rubato timing/memory number provenance and unit reconciliation:
  `docs/MEASUREMENT_PROVENANCE.md`
- Paper-writing handoff (what's measured, what isn't, and why): `docs/PAPER_HANDOFF.md`

For any interface- or contract-sensitive change (proto fields, timing-breakdown semantics,
CKKS parameterization, threat-model claims), follow `docs/spec.md` first and treat
`PROJECT_STATE.md` as execution history. Before citing any latency/throughput number in a
paper or presentation, check `AUDIT.md` and the artifact's own `status` /
`*_DEPRECATED` fields first.

## Unverified / TODO

- The Degree-2 fallback path is broken, not just unexercised: `compiler/degree2_linearizer.py`
  raises a negative-dimension `ValueError` before producing any features (see "Degree-2
  Fallback" above). Fixing that crash is a precondition for ever running it through a live
  end-to-end gRPC demo — this is not merely a "hasn't been tried yet" item.
- `tools/openfhe_benchmark/`'s `fold`/`naive` cells and the full
  `reduction_strategy x modulus_chain x library` execution matrix beyond what's listed above
  remain PENDING — OpenFHE is not installed in this environment.
- Cloud pricing for the `r7i.4xlarge`/`r7i.8xlarge` transciphering benchmark instances has not
  been pulled/confirmed as of this writing.
