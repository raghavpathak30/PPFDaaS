# PPFDaaS — Remediation & Research Roadmap

**From the current repo (`raghavpathak30/PPFDaaS`, HEAD `edaa83f`) to a submittable WAHC artifact.**

This document is the complete, ordered list of changes surfaced across the review. It is organized into **phases** that must be executed in order — each phase is a gate for the next. Skipping forward produces an unverified subsystem feeding another unverified subsystem, which is exactly the failure that produced the current state.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done · **BLOCKER** = paper-invalidating if unfixed · **MISSING** = does not exist in repo, must be built.

---

## Phase 0 — Correctness (BLOCKER gate for everything)

> Nothing below Phase 0 is meaningful until the core computation is correct and *proven* correct. Every number the current system has ever produced is wrong because of item 0.1.

### 0.1 — [x] Fix the slot-reduction fold — **BLOCKER**
- **STATUS:** Fixed. `rotation_hoisting.cpp` now implements `acc = acc + rotate(acc, step)` (loop-carried over the accumulator, step doubling 1→128); the OpenMP `parallel for` over rotations was removed. Post-condition `acc.slot[k*256] == sum_{j=0..255} w[j]*x[j]` is documented in-file and verified by 0.2/0.4.
- **File:** `vendor_server/src/rotation_hoisting.cpp` (and `rotation_hoisting_degree2.cpp`)
- **Defect:** implements `acc += rot(ct, step)` over rotations of the *original* ciphertext. This computes a 9-term partial sum (slots {0,1,2,4,8,16,32,64,128}), **not** the 256-term inner product the post-condition comment claims.
- **Fix:** implement the true log-time fold with the loop-carried dependency — each rotation acts on the **accumulated** ciphertext: `acc += rot(acc, step)`, step doubling each iteration. Confirm post-condition holds: `acc.slot[k*256] == sum_{j=0..255} (w[j]*x[j])` per lane.
- **Consequence to accept:** the OpenMP `parallel for` over rotations **must be removed** — a correct fold is a sequential dependency chain and is not parallelizable in this form. (Parallelism returns only via BSGS / flat layers in Phase 4, as a *measured* alternative, not a free lunch.)
- **Verify:** re-run the numeric oracle below; decrypted slot 0 must match the plaintext dot product to CKKS precision.

### 0.2 — [x] Replace the one-hot test trap — **BLOCKER**
- **STATUS:** Fixed. `test_he_core.cpp` now runs a randomized dense-vector oracle parity test (N=100 random (w, x) pairs, 1600 lane checks), asserting `max_abs_error < TOL` against the plaintext `w·x` oracle, plus structured basis-probe vectors. The `>= 2.0x speedup` assertion was removed from this correctness test.
- **File:** `vendor_server/tests/test_he_core.cpp` (`test_rotation_hoisting`)
- **Defect:** test uses one-hot weights at slot 0 only — the unique input layout where broken and correct implementations agree. The test was built around the bug, including a comment admitting the accumulator "degenerates (~9×)".
- **Fix:** rewrite as **oracle parity on randomized dense inputs**:
  - Draw N≥100 random weight/feature vectors per run.
  - Compute ground truth in plaintext NumPy/C++: `expected = w · x + b`.
  - Run the encrypted path, decrypt, assert `max_abs_error < tol`.
  - Add structured basis-probe vectors at non-degenerate slot positions.
- **Remove** the `>= 2.0x speedup` assertion from this test entirely — correctness tests must be pure; performance assertions move to the benchmark harness (Phase 5).

### 0.3 — [x] Add an end-to-end HE-vs-plaintext parity harness — **MISSING / BLOCKER**
- **STATUS:** Fixed. `tests/test_inference.py::run_runtime_validation` now compares **logit vs logit** (decrypted raw score + bias vs `X @ w + b`) over the full 56,962-sample held-out test set, reporting max/mean/median absolute error and the error distribution, plus ROC-AUC/PR-AUC computed from the encrypted-path logits. Results land in `artifacts/errors.json`.
- **File (current, broken):** `tests/test_inference.py::run_runtime_validation` compares a **logit** (`X @ w + b`) against a **sigmoid probability** (`fraud_probabilities[0]`) — dimensionally incoherent, asserts nothing.
- **Fix:** new harness that compares **logit vs logit** (decrypted raw score vs `X @ w + b`) over the full test set, reporting:
  - max absolute error, mean absolute error, error distribution (not a single anecdote).
  - ROC-AUC / PR-AUC computed from the **encrypted-path** scores vs labels.
- This is the first time the system will have a real accuracy number for the encrypted path. The current `0.979 AUC` is plaintext-only (`scripts/show_accuracy_check.py`, `artifacts/dispatch_result.json`) and says nothing about ciphertext.

### 0.4 — [x] Numeric oracle (drop-in verification snippet)
- **STATUS:** Confirmed. The corrected fold (`acc2` below) matches `true 256-sum` at slot 0; this is the same invariant now implemented by 0.1 and exercised by 0.2's randomized oracle. Snippet kept below as a standing reference check.

Keep this as a standing check while editing the fold:
```python
import numpy as np
rng = np.random.default_rng(0)
ct = rng.normal(size=4096)
def rot(v, s): return np.roll(v, -s)
# BROKEN (current repo): rotations of the ORIGINAL ct
acc = ct.copy()
for s in [1,2,4,8,16,32,64,128]:
    acc = acc + rot(ct, s)
# CORRECT fold: rotations of the ACCUMULATOR
acc2 = ct.copy()
for s in [1,2,4,8,16,32,64,128]:
    acc2 = acc2 + rot(acc2, s)
print("broken slot0:", acc[0], " correct slot0:", acc2[0],
      " true 256-sum:", ct[0:256].sum())
# correct slot0 must equal true 256-sum
```

### 0.5 — [x] Resolve the `raw_slot0` out-of-range-on-decrypt bug
- **STATUS:** Resolved as a direct consequence of 0.1's fold fix. Encrypted-path parity (0.3) now reports `max_abs_error=4.34e-7`, `mean_abs_error=7.71e-8` over 56,962 samples (`artifacts/errors.json`), with `roc_auc_encrypted=0.9794`, `pr_auc_encrypted=0.8238` (`artifacts/dispatch_result.json`: `depth1_auc=0.9791`) — within CKKS precision of the plaintext oracle, no out-of-range slot-0 values observed.
- Likely a symptom of 0.1 and/or a scale/slot-offset mismatch on the Python decryption side.
- Re-test after 0.1 lands. If it persists, instrument scale and `parms_id` at each pipeline stage and confirm slot-offset arithmetic (`k*256`) against the corrected fold's output layout.

**Phase 0 exit criteria:** randomized oracle parity green; end-to-end logit-vs-logit error characterized; encrypted-path AUC computed; `raw_slot0` resolved.

---

## Phase 1 — Trust boundary & threat model (BLOCKER gate for any security claim)

> The implemented threat model is currently "one trusted admin owns everything via a shared `artifacts/` volume." The README claims a two-party vendor/bank boundary. The code must be made to enforce what the paper will claim.

### 1.1 — [x] Remove secret-key capability from the server — **BLOCKER**
- **STATUS:** Fixed. `EvalContext160` (`vendor_server/include/eval_context_160.h`, `vendor_server/src/eval_context_160.cpp`) is the eval-only context type used by `vendor_server_160`: no `SecretKey`, `Decryptor`, or `KeyGenerator` anywhere in its construction. The SecretKey-holding `ckks_context_160.{h,cpp}` is excluded from `vendor_server_160`'s sources entirely, and (Phase 2 pre-gate, below) has been physically relocated out of `vendor_server/` into `tools/local_benchmark/`, marked "OUT OF TCB".
- **Files:** `vendor_server/src/ckks_context_160.cpp` (lines ~30–32 generate a secret key), `ckks_context_160.h` (members), and the 200-bit equivalents.
- **Defect:** the server constructor generates its own secret key and holds a live `Decryptor`. Even if never applied to client data, its existence makes the privacy claim unprovable (requires proving a universal negative).
- **Fix:** introduce an **evaluation-only context type** — no secret key field, no `Decryptor`, no `KeyGenerator` anywhere in the linked server process. The server's TCB is then defined by *capabilities present*, not *intentions*.
- **Knock-on:** server warm-ups/self-tests that currently self-encrypt must be redesigned to use plaintext-side checks or the canary handshake (1.4).

### 1.2 — [x] Make Galois-key loading fail-CLOSED — **BLOCKER**
- **STATUS:** Fixed. `EvalContext160::ProvisionGaloisKeys` (`vendor_server/src/eval_context_160.cpp`) throws / transitions the provisioning state machine to `PROV_FAULT` on missing, malformed, or `parms_id`-mismatched Galois keys — there is no longer any code path that fabricates local Galois keys under a server-held secret key.
- **File:** `vendor_server/src/ckks_context_160.cpp` (the else-branch that fabricates local Galois keys when `galois_keys_160.bin` is missing).
- **Defect:** fail-OPEN — on missing keys the server fabricates Galois keys under its own secret key and serves semantically garbage at status OK. (The fail-closed fix you wrote during the Docker modules was **never pushed**; the repo regressed to silent-failure mode.)
- **Fix:** absence of client keys is a **hard halt**, not a degraded mode. Throw / transition to FAULT (see 1.5). Key material is a correctness precondition, not a config default.

### 1.3 — [x] Stop shipping the model to the client (or drop the model-privacy claim) — **BLOCKER for any model-privacy narrative**
- **STATUS:** Resolved via Option A. The bias term is now applied server-side (`pt_bias_` in `inference_service_160.cpp`, added homomorphically before the result is serialized); `bank_client.py` no longer reads `model_weights.bin` at all. System is documented as providing **input privacy only**; model-privacy language has been removed in favor of this framing (see `bank_client.py` §1.3 comment).
- **File:** `bank_client/bank_client.py` (`BankClient.__init__` reads full `artifacts/model_weights.bin` — 256 weights + bias).
- **Defect:** the "vendor protects its proprietary model" framing is false; the client holds the full model. System delivers **input privacy only**.
- **Choose one and commit in writing:**
  - **(A) Claim input privacy only.** Move the bias term server-side so deployment stops *requiring* weight disclosure; delete all model-privacy language from README/spec/paper.
  - **(B) Actually provide model privacy.** Requires output masking (see 1.6) — and note this costs a multiplicative level, i.e. roughly the 38–48% optimization the project is proudest of. This trade-off, *measured*, is a publishable result (Phase 5).

### 1.4 — [x] Abolish the shared `artifacts/` trust domain — **BLOCKER**
- **STATUS:** Fixed. `compose.yaml` no longer mounts `galois_keys_160.bin`, `secret_key_160.bin`, or any decryption-capable material into `vendor_server`; only the vendor's own `model_weights.bin` (bias) is volume-mounted. Provisioning is now a wire protocol — `ProvisionGaloisKeys` + canary handshake (`provisioning_state.h`/`.cpp`, `bank_client.py::provision_and_validate`) — with no shared filesystem between bank and vendor domains. Relin keys are not provisioned (not needed at depth-1, per the note below).
- **File:** `compose.yaml` (mounts `artifacts/` into both containers; it contains secret key + model + eval keys).
- **Fix:** provisioning becomes a **protocol**, not a shared filesystem:
  - Bank domain holds the secret key (HSM/sealed store), generates pk/Galois/relin, performs all encrypt/decrypt.
  - Vendor domain receives, over authenticated TLS, only an **evaluation bundle** (parms + Galois keys, public key only if the server must encrypt).
  - No bytes that grant decryption ever cross into the vendor domain.
- **Note:** relin keys are **not needed** at depth-1 linear (no ct×ct multiply) — do not provision them "just in case"; it needlessly enlarges the vendor capability set.

### 1.5 — [x] Implement the fail-closed provisioning state machine — **MISSING / BLOCKER**
- **STATUS:** Implemented. `ProvisioningStateMachine` (`vendor_server/include/provisioning_state.h`) implements `PROV_AWAITING_KEYS -> PROV_VALIDATING -> PROV_READY` with terminal `PROV_FAULT` reachable from any state. `RunInference` rejects all calls outside `PROV_READY` with an explicit "not provisioned" error. All three validation rungs are implemented: structural (`parms_id`/Galois-element-completeness check on `ProvisionGaloisKeys`), behavioral (the `CanaryCheck`/`CanaryConfirm` handshake), and continuous (`record_request_and_maybe_trip` checks `parms_id`/ciphertext size every request and trips `FAULT` on mismatch-rate).
- States: `INIT → AWAITING_KEYS → VALIDATING → READY`, with terminal `FAULT` reachable from any state and no auto-exit from FAULT.
- Inference RPCs permitted **only** in `READY`; rejected with explicit "not provisioned" otherwise — never queued, never served with substitute keys.
- Three validation rungs:
  - **Structural:** supplied Galois keys parse under supplied parms; `parms_id` matches; key set contains *every* Galois element the rotation schedule needs (check completeness eagerly).
  - **Behavioral:** the canary handshake (1.4 / below).
  - **Continuous:** every request re-checks `parms_id`, expected ciphertext size/level, session key fingerprint; mismatch rate trips FAULT.

### 1.6 — [x] Output sanitization (required only under choice 1.3-B, but design now)
- **STATUS:** Deferred by design. Choice 1.3 adopted **Option A** (input privacy only), under which output masking is not a correctness/privacy requirement — the bank already holds the model and is the only party that decrypts. This item remains unimplemented intentionally; the design notes below are retained for a future Phase 7 / model-privacy (Option B) revisit, should that be pursued.
- **Defect:** returned ciphertext is unmasked — all 4096 slots (including partial sums) go back to the client. A client who knows its own inputs can recover weights by solving a small linear system.
- **Fix:** multiplicative slot mask (plaintext indicator zeroing non-result slots) + additive noise flooding in surviving slots.
- **Couple with 2.x:** flooding variance must dominate the circuit error bound (Li–Micciancio); this spends precision bits and must be co-designed with scale/chain.

### 1.7 — [x] Transport & integrity
- **STATUS:** Fixed. `BuildServerCredentials()` (`vendor_server/src/inference_service_160.cpp`) builds mutually authenticated TLS credentials from `PPFD_TLS_CERT`/`PPFD_TLS_KEY`/`PPFD_TLS_CA` when set, falling back to `InsecureServerCredentials()` under the honest-but-curious assumption documented in `docs/spec.md` §6. `bank_client.py` accepts matching `tls_ca_path`/`tls_cert_path`/`tls_key_path` for client-side mTLS. `compose.yaml` documents the opt-in mTLS path and dev-cert generation (`scripts/generate_dev_certs.sh`).
- **File:** `vendor_server/src/inference_service_160.cpp` uses `grpc::InsecureServerCredentials()`.
- **Fix:** mutually authenticated TLS; client authentication; replay/integrity protection. CKKS ciphertexts are malleable — only honest-but-curious is defensible, and only with authenticated transport. State this assumption explicitly in the threat model.

### 1.8 — [x] Write the threat model down — **MISSING**
- **STATUS:** Done. `docs/spec.md` §6 "Threat Model & Trust Boundaries [PHASE 1 ADDITION]" (~line 3528) states the semi-honest/honest-but-curious adversary, key custody (bank holds secret/Galois/public keys; vendor holds only the evaluation bundle), what the vendor sees, key lifecycle via the provisioning protocol, and the IND-CPA-D (Li–Micciancio) caveat for decrypted scores (§6.6).
- **File:** `docs/spec.md` (3,527 lines, currently **zero** threat-model section; one stray comment at line ~1811).
- **Add a section** stating: semi-honest (honest-but-curious) adversary; who holds which keys; what the server sees; what leaks; the **IND-CPA-D** caveat (Li–Micciancio) for any decrypted score that is shared/logged/billed; key lifecycle. Do not claim IND-CPA when the deployment shares decrypted values.

**Phase 1 exit criteria:** server has no decryption capability by construction; fail-closed on missing/invalid keys; provisioning is a protocol with a verified state machine; threat model written and matches enforced behavior; transport authenticated.

---

## Phase 2 — Concurrency correctness (gate for any throughput number)

### Pre-gate — [x] Relocate `ckks_context_160.{h,cpp}` out of `vendor_server/`
- **STATUS:** Done. `ckks_context_160.{h,cpp}` (holds a live `SecretKey`, `Decryptor`, and `KeyGenerator`) moved via `git mv` from `vendor_server/include`/`vendor_server/src` to `tools/local_benchmark/`. `vendor_server/CMakeLists.txt` updated: `benchmark_160`'s source path and include directories point at the new location; `he_core` gained a `PUBLIC` include of `tools/local_benchmark` (needed by `he_inference.h`'s `CKKSContext160` declaration, also used by the 200-bit `benchmark.cpp`). The relocated header carries an explicit "OUT OF TCB ... must NEVER be linked into vendor_server_160 ... See docs/spec.md §6" banner. `Dockerfile.server` only builds `vendor_server_160`, which never references this file — unaffected. Full `cmake --build .` succeeds for all targets.

### 2.1 — [x] Fix shared mutable buffers under multithreaded gRPC — **BLOCKER for throughput claims**
- **STATUS:** Fixed. `acc_buf_`, `canary_acc_buf_`, `ct_out_buf_`, `canary_out_buf_` removed as instance members of `FraudInferenceServiceImpl160`. `RunInference` now uses `static thread_local seal::Ciphertext tl_acc_buf` and `static thread_local std::vector<char> tl_ct_buf` (matching the 200-bit normative pattern in `inference_service.cpp`); `CanaryCheck` uses function-local `canary_acc_buf`/`canary_out_buf` (one-shot provisioning, no concurrency concern, but correct by construction). `ProvisioningStateMachine` (`sm_`) was audited and confirmed already thread-safe (`std::atomic` state/counters, `std::mutex`-guarded `last_message_`) — documented in a new "Thread-safety contract (Phase 2, §2.1)" comment block above the class's member declarations; no code change needed there. `RunVendorServer160` now sizes gRPC's sync thread pool from `PPFD_GRPC_THREADS` via `SetSyncServerOption(NUM_CQS/MIN_POLLERS/MAX_POLLERS)` (previously defaulted to MIN/MAX_POLLERS=1/2, capping real concurrency). New `tests/test_concurrent_inference.py` (32 requests / 8 threads) passes: all results are valid probabilities, and repeated runs of an identical input agree within 1e-5 of a sequential reference.
- **File:** `vendor_server/src/inference_service_160.cpp` — `acc_buf_` / `ct_out_buf_` are instance members shared across concurrent RPCs on a single service object (the `thread_local` fix exists only in the 200-bit `inference_service.cpp`).
- **Defect:** torn ciphertext buffers under concurrency deserialize/decrypt into garbage silently (no integrity layer to catch it).
- **Fix:** per-request ownership — request-scoped arenas or genuinely thread-confined storage. Service object holds only **immutable** shared state (context, eval keys, encoded weights).
- **Document** SEAL object thread-safety contracts and memory-pool policy as architectural constraints, not folklore.

**Phase 2 exit criteria:** correctness-under-concurrency established (required before any closed-loop throughput benchmark in Phase 5).

---

## Phase 3 — Parameter justification (reviewer-facing rigor)

- [x] 3.1 — sec_level_type::tc128 asserted in SEALContext construction in
      eval_context_160.cpp and ckks_context.cpp; throws on violation; parameter
      justification comment added citing HE-standard v1.1 Table 2
- [x] 3.2 — precision_analysis.py written; artifacts/precision_analysis.json
      produced; scale headroom characterized; comment added in eval_context_160.cpp
- [x] 3.3 — invariant_noise_budget check removed from tests/verify_all.py;
      replaced with CKKS-appropriate structural verification (context validity,
      chain depth, slot count)

### 3.1 — Compute and assert the security level
- **Files:** `eval_context_160.cpp` ({60,40,60}, N=8192), `ckks_context.cpp` ({60,40,40,60}, N=8192).
- **Done:** `sec_level_type::tc128` is now passed explicitly to the `SEALContext` constructor in both files (it was previously only the implicit SEAL default). A parameter-justification comment above each construction cites HomomorphicEncryption.org Security Standard v1.1, Table 2 (218-bit bound for N=8192; 160<=218 and 200<=218), SEAL's `seal_he_std_parms_128_tc()` (hestdparms.h) and `SEALContext::Validate` (context.cpp), explains the fail-closed `parameters_set()==false -> throw` mechanism, and documents the depth-1 data-chain headroom (2 data levels for {60,40,60}, 3 for {60,40,40,60}).

### 3.2 — Add precision analysis for the scale choice
- **Done:** `scripts/precision_analysis.py` (new) + `tools/local_benchmark/precision_probe.cpp` (new, OUT OF TCB) measure per-stage decrypted error against a plaintext oracle for a representative 4096-slot batch, and report full-dataset noise floor/ceiling/headroom from `artifacts/errors.json`. Output written to `artifacts/precision_analysis.json`. `eval_context_160.cpp` now has a precision-justification comment with the real measured numbers: MaxAE=4.344e-07 (~21.1 bits below 1.0), scale=2^40 leaves ~21.1 bits of remaining headroom, and the 40-bit-vs-30-bit scale tradeoff is documented.

### 3.3 — Fix the BFV-concept footgun
- **Done:** the `invariant_noise_budget` check (a BFV-only concept, meaningless for CKKS) has been removed from `tests/verify_all.py` step 4 entirely — not replaced with a placeholder. It is replaced with CKKS-appropriate structural verification: `sec_level_type::tc128` assertion, modulus-chain total bits and data-level count for both the 160-bit and 200-bit contexts, and a comment explaining that CKKS correctness is instead verified by the Phase 0 parity harness (`artifacts/errors.json`).

---

## Phase 4 — The research core: rotation/reduction trade-space

> This is where the project stops being "ran a known model under CKKS" and becomes a measurement contribution. None of this is novel until Phase 0 makes the kernel correct.

- [x] Pre-gate (a) — root `CMakeLists.txt` rewritten as a thin
      `add_subdirectory(vendor_server)` / `add_subdirectory(tests)` wrapper
      (was a stale full duplicate of `vendor_server/CMakeLists.txt` with
      source paths that never existed at the repo root, including a
      reference to `src/ckks_context_160.cpp` relocated to
      `tools/local_benchmark/` in the Phase 2 pre-gate); `python3
      tests/verify_all.py` STEP 10/10 now **PASS**.
- [x] Pre-gate (b) — `tests/test_inference.py`'s
      `test_service_uses_spec_timing_boundaries_and_debug_invariant` member
      assertion updated to accept `std::optional<seal::CKKSEncoder> encoder`
      / `std::optional<seal::Encryptor> encryptor` (the actual
      `ckks_context.h` declarations; `std::optional<T>` is still a value
      member, not `unique_ptr`), in addition to the old plain-value forms.
      Assertion now passes against the real header.
- [x] 4.1 — `bsgs_reduction()` (BSGS two-layer reduction: 30 independent
      rotations — 15 baby + 15 giant — across 2 critical-path layers, vs
      `hoisted_tree_sum`'s 8 sequential rotations / 8 critical-path steps)
      added to `vendor_server/include/rotation_hoisting.h` and
      `vendor_server/src/rotation_hoisting.cpp`, alongside (not replacing)
      `hoisted_tree_sum`. `BSGS_ROTATION_STEPS` (30-element Galois step set,
      `{1..15} ∪ {16,32,...,240}`) added as a separate constant;
      `bsgs_reduction` throws `std::runtime_error` if `galois_keys` is
      missing any required element. Verified correct against a plaintext
      dot-product oracle via `benchmark_160 --strategy=bsgs`
      (max_abs_error=1.7e-6, tolerance=1e-3, n=100, `correctness_passed=true`).
- [x] 4.2 — Added "TERMINOLOGY NOTE (Phase 4, §4.2)" comment blocks above
      `hoisted_tree_sum` in both `rotation_hoisting.h` and
      `rotation_hoisting.cpp` (no rename — `hoisted_tree_sum` keeps its name
      and signature) explaining that true Halevi-Shoup hoisting (shared
      ModDown across automorphisms) is not exposed by SEAL's public API, and
      that `hoisted_tree_sum` is a sequential dependency-chain fold.
      `docs/spec.md` §7 "Rotation/Reduction Strategy Taxonomy" added, with
      §7.1 defining true hoisting precisely and the SEAL API limitation, and
      §7.2-§7.4 naming the three measured strategies (sequential fold, BSGS
      two-layer, OpenFHE hoisted flat), citing Halevi & Shoup (2014) and the
      OpenFHE `EvalFastRotation` docs.
- [x] 4.3 — `tools/openfhe_benchmark/` written end-to-end: standalone CMake
      project (`find_package(OpenFHE)` with `FATAL_ERROR` + build
      instructions if absent — never added as a subdirectory of the root or
      `vendor_server` configs, not part of the TCB), `openfhe_linear_eval.h`
      / `.cpp` (the identical depth-1 16-lane/256-feature circuit via
      `EvalFastRotationPrecompute`/`EvalFastRotation` over the same
      `BSGS_ROTATION_STEPS` set, HEStd_128_classic, multiplicative depth 1,
      scale 2^40), `openfhe_benchmark.cpp` (20 warmup + 100 timed,
      mean/std/p50/p95/p99/min/max per stage + in-band parity gate),
      `README.md` (build instructions + SEAL-160-bit/OpenFHE parameter
      equivalence table). **OpenFHE is not installed in this environment**
      (no `OpenFHEConfig.cmake`/pkg-config found anywhere on the system);
      `results/openfhe_results.json` holds `"status": "PENDING"` with an
      explicit reason and is overwritten with real measurements once built.
- [x] 4.4 — `scripts/rotation_strategy_comparison.py` written and run: reads
      `artifacts/comparison_results.json` (Phase 3 e2e-gRPC sequential fold,
      mean=2.026ms/p99=2.985ms), invokes `benchmark_160 --strategy=fold` and
      `--strategy=bsgs` (local-circuit-only, same methodology for both;
      in-band parity gate enforced — both pass, max_abs_error 7.7e-7 and
      1.7e-6 respectively), reads `tools/openfhe_benchmark/results/openfhe_results.json`
      (PENDING), and writes `artifacts/rotation_strategy_comparison.json`
      plus a paper-ready Strategy/Rotations/Critical-Path/Latency/p99/Galois-Keys
      table on stdout, with an explicit methodology note distinguishing the
      e2e-gRPC row from the local-circuit rows.

### Pre-gate (a) — Fix root `CMakeLists.txt`
- **File:** `CMakeLists.txt` (repo root).
- **Done:** replaced the stale full duplicate (broken `src/ckks_context.cpp` /
  `src/ckks_context_160.cpp` paths that never resolved at the repo root) with
  a minimal wrapper: `enable_testing()`, `add_subdirectory(vendor_server)`,
  `add_subdirectory(tests)`. `vendor_server/CMakeLists.txt` remains the single
  source of truth for all C++ targets and already references
  `tools/local_benchmark/ckks_context_160.cpp` correctly (Phase 2 pre-gate).
  Docker builds are unaffected (`Dockerfile.server` configures
  `vendor_server/` directly, not the root). Verified:
  `python3 tests/verify_all.py` STEP 10/10 **PASS**.

### Pre-gate (b) — Fix `tests/test_inference.py` member assertion
- **File:** `tests/test_inference.py`,
  `test_service_uses_spec_timing_boundaries_and_debug_invariant`.
- **Done:** the assertion `"seal::CKKSEncoder encoder" in src_h or "CKKSEncoder
  encoder" in src_h` could never match `std::optional<seal::CKKSEncoder>
  encoder;` (the actual declaration in `vendor_server/include/ckks_context.h`)
  because of the `<seal::CKKSEncoder>` template syntax. Updated to also accept
  `std::optional<seal::CKKSEncoder> encoder` / `std::optional<seal::Encryptor>
  encryptor`, with a comment explaining `std::optional<T>` is still a value
  member (no heap indirection), satisfying the "value member, not unique_ptr"
  invariant the test enforces.

### 4.1 — Implement multiple reduction strategies as measurable variants
- **Sequential fold** (the corrected 0.1): log n rotations, depth-log critical path, unhoistable/unparallelizable.
- **BSGS two-layer:** ~2√n rotations restructured into two layers of *independent* rotations (hoistable, parallel); inverts dependency from chain(depth log n, width 1) to two layers(depth 1, width √n).
- **Flat/hoisted layer** (where the library permits): genuine Halevi-Shoup hoisting — share the digit decomposition across automorphisms.
- **Done:** `bsgs_reduction(ct_in, galois_keys, evaluator, ct_out, n_features=256,
  baby_step=16, giant_step=16)` added to `vendor_server/src/rotation_hoisting.cpp`
  / declared in `vendor_server/include/rotation_hoisting.h`, additive to
  `hoisted_tree_sum` (not a replacement). Baby-step layer rotates the ORIGINAL
  ciphertext by j=1..15 (OpenMP `parallel for`, independent rotations of the
  same source); giant-step layer rotates the baby-step accumulator by
  i*16 for i=1..15 (OpenMP `parallel for`, again independent rotations of the
  same source). `BSGS_ROTATION_STEPS` (30 elements) declared separately from
  `EvalContext160::ROTATION_STEPS` (the deployed server's 8-element set,
  unchanged). `bsgs_reduction` validates `galois_keys.has_key(...)` for every
  required element via `seal::util::GaloisTool`, throwing `std::runtime_error`
  with a message pointing to `docs/spec.md` §4 if the deployed 8-element key
  set is passed in. Production BSGS deployment requires reprovisioning with
  `BSGS_ROTATION_STEPS` — documented in `rotation_hoisting.h` and `docs/spec.md` §7.3.

### 4.2 — Stop calling thread-parallel rotation "hoisting"
- Halevi-Shoup hoisting = sharing the key-switch decomposition across rotations. SEAL's public API does **not** expose this. Calling an OpenMP loop "hoisting" is a desk-reject signal at WAHC.
- **Done:** Part A — added "TERMINOLOGY NOTE (Phase 4, §4.2)" comment blocks
  above `hoisted_tree_sum`'s declaration (`rotation_hoisting.h`) and definition
  (`rotation_hoisting.cpp`), explaining what true Halevi-Shoup hoisting is,
  that SEAL's public API doesn't expose the shared ModDown step, and that
  `hoisted_tree_sum` is a sequential 8-step dependency-chain fold — NOT a
  rename, signature and call sites in `inference_service_160.cpp` unchanged.
  Part B — `docs/spec.md` §7 "Rotation/Reduction Strategy Taxonomy" added:
  §7.1 defines true hoisting and the SEAL API gap; §7.2 (sequential fold, 8
  rotations/8 critical-path steps, `{1,2,4,8,16,32,64,128}`); §7.3 (BSGS
  two-layer, 30 rotations/2 critical-path steps, `BSGS_ROTATION_STEPS`); §7.4
  (OpenFHE hoisted flat, same 30-rotation set, genuine hoisting via
  `EvalFastRotationPrecompute`/`EvalFastRotation`); §7.5 frames the systems
  contribution (SEAL's public-API ceiling). Cites Halevi & Shoup (2014, CRYPTO,
  "Algorithms in HElib" §3) and OpenFHE's `EvalFastRotation`
  docs/tutorials. The existing §4.5 terminology note now points readers to §7.

### 4.3 — Cross-library study (converts short paper → full paper)
- SEAL cannot do true hoisting via public API. OpenFHE exposes fast-rotation-with-precompute; Lattigo exposes hoisted rotations natively.
- **Done:** `tools/openfhe_benchmark/` (new, standalone CMake project,
  never built by the root or `vendor_server` configs, not in the TCB) contains:
  `CMakeLists.txt` (`find_package(OpenFHE)` → `FATAL_ERROR` with full build
  instructions if not found), `openfhe_linear_eval.h`/`.cpp` (`build_context()`:
  CKKS, ring dim 8192 requested, multiplicative depth 1, scale 2^40, batch
  size 4096, `HEStd_128_classic`, `EvalRotateKeyGen` over `kBsgsRotationSteps`
  (= `BSGS_ROTATION_STEPS`); `run_circuit_hoisted()`: encrypt → `EvalMult` →
  two BSGS layers each via one `EvalFastRotationPrecompute` + 15
  `EvalFastRotation` calls → decrypt, with an in-band parity gate against a
  plaintext oracle), `openfhe_benchmark.cpp` (20 warmup + 100 timed, per-stage
  mean/std/p50/p95/p99/min/max, writes `results/openfhe_results.json`),
  `README.md` (build/run instructions + SEAL-160-bit ↔ OpenFHE parameter
  equivalence table, including the caveat that OpenFHE may select a ring
  dimension other than 8192 for this depth/security/scale combination).
  **OpenFHE is not installed in this environment** — confirmed no
  `OpenFHEConfig.cmake` or pkg-config file anywhere on the system; the
  scaffold is code-complete and compile-ready but unbuilt.
  `results/openfhe_results.json` ships with `"status": "PENDING"` and a
  `"reason"` field; running `./openfhe_benchmark` overwrites it with
  `"status": "MEASURED"` and real statistics.

### 4.4 — Measurement comparison script
- **Done:** `scripts/rotation_strategy_comparison.py` (new) produces
  `artifacts/rotation_strategy_comparison.json` and a paper-ready stdout table
  with columns Strategy | Rotations | Critical Path | Latency (ms) | p99 (ms) |
  Galois Keys, covering: (1) SEAL sequential fold, e2e gRPC (Phase 3,
  `artifacts/comparison_results.json:summary.reduced_160bit`, 8/8/2.026/2.985/8);
  (2) SEAL sequential fold, local circuit (Phase 4, `benchmark_160
  --strategy=fold`, 8/8/3.948/4.228/8); (3) SEAL BSGS two-layer, local circuit
  (Phase 4, `benchmark_160 --strategy=bsgs`, 30/2/8.545/12.535/30); (4) OpenFHE
  hoisted flat (PENDING, 30/1/-/-/30). Both `benchmark_160` invocations enforce
  the in-band parity gate (Phase 5.5 pattern) before reporting timings — both
  pass (max_abs_error 7.7e-7 and 1.7e-6, tolerance 1e-3). An explicit
  `methodology_note` in the JSON flags that row (1) is a full gRPC round trip
  while rows (2)-(4) are local-circuit-only and not directly comparable to row
  (1); rows (2) and (3) ARE same-methodology and are the primary fold-vs-BSGS
  comparison. The BSGS row carries a `note` explaining that, on this 20-core
  host with `OMP_NUM_THREADS` unset, BSGS's mean/p99 EXCEED the fold's despite
  a shorter critical path (2 vs 8) — more total rotation work (30 vs 8) with no
  hoisting to amortize it, plus OpenMP thread-spawn overhead, dominates the
  shorter dependency chain. This is itself part of the §7.5 finding.

---

## Phase 5 — Honest measurement methodology

> Governing rule: **a number may appear in the paper only if it was produced by executing the thing it describes.**

### 5.1 — [x] Kill the estimated/fabricated baseline — **BLOCKER**
- **File:** `scripts/generate_ablation.py` defaults to `methodology = "estimated-linear-rotation-model"` — naive timings synthesized as measured × 255/8.
- **Fix:** either implement the naive path behind a server flag and **measure** it, or cut it. Publishing a speedup vs an *estimated* baseline is grounds for rejection. (And the 255-rotation naive sum is a strawman regardless — the log-fold IS the real baseline.)
- **STATUS:** Fixed. `benchmark_160` now implements `--strategy={fold,bsgs,naive}`: `naive` runs `naive_tree_sum` (255 real rotations, `NAIVE_ROTATION_STEPS` constant), provisions the full Galois key set `{1..255}`, runs the in-band parity gate before any timing, and reports Galois-keygen time separately from inference latency. `scripts/generate_ablation.py` now always sets `"methodology": "measured"` (no more `estimated-linear-rotation-model` fallback), reads real fold/naive numbers from `artifacts/ablation_methodology.json`, and supports `--fast-ablation` (n=20) without changing the default (n=100).

### 5.2 — [x] Reframe the 38–48% number honestly
- It is a **self-ablation** (your 160-bit vs your 200-bit config), legitimate as a tuning table — **not** a baseline comparison. There is currently **no** comparison to any external system (Lattigo/OpenFHE/HELR/published HE-LR latency). Label it as self-ablation; add at least one external comparison via 4.3.
- **STATUS:** Fixed. `docs/spec.md` §5.7 "Benchmark Framing [PHASE 5 ADDITION]" defines Type 1 (self-ablation), Type 2 (reduction-strategy comparison), and Type 3 (cross-library comparison), and states the headline latency-reduction figure is Type 1 only. §5.4/§5.5 rewritten with the real `artifacts/comparison_results.json` numbers (mean reduction 36.97%, median reduction 39.59%, replacing the old fabricated 48.31%/49.55%), explicitly framed as self-ablation with no claim against an external baseline, and pointing to `artifacts/rotation_strategy_comparison.json` (Type 2) and `tools/openfhe_benchmark/results/openfhe_results.json` (Type 3, PENDING). `README.msd` and `artifacts/comparison_results.json#framing` carry the same Type 1/§5.7 framing; `scripts/generate_ablation.py` already cites §5.7. External comparison via 4.3 remains out of scope for this phase (4.3 is a Phase 4 item, not opened here).

### 5.3 — [x] Fix benchmark hygiene
- **File:** `tests/benchmark_comparison.py` (the real harness: 20 warmup + 100 measured, mean/std/p50/p95/p99 — respectable but flawed).
- Fixes:
  - **Randomized real inputs** from the held-out test set — not the constant `0.01` vector currently used for all 100 runs.
  - **Capture hardware manifest programmatically** into the results JSON: CPU model, core topology, governor, compiler, flags, SEAL/library versions. (Currently free-text prose.)
  - ≥1000 measured iterations; report **median + IQR + bootstrap CIs + p99** (means/stds hide the bimodality already visible in `artifacts/comparison_results.json`).
  - Nonparametric tests for cell-to-cell comparisons.
- **STATUS:** Fixed. `tests/benchmark_comparison.py` rewritten: 1 reserved parity-gate sample + 20 warmup + 1000 measured per variant, drawn from a fixed-seed permutation of the 56,962-row held-out set; `_hardware_manifest()` captures CPU model/cores/governor/RAM/SEAL version/compiler flags programmatically; `_summarize()` reports median/IQR/bootstrap CI (n=10000)/p95/p99; `_mann_whitney()` runs the nonparametric U test. Re-run end-to-end (real, n=1000 each, `artifacts/comparison_results.json`): median_us baseline_200bit=17670.5, reduced_160bit=10675.5, U=887378.0, p=1.02e-197. SLA gates (calibrated for `cpu_governor=performance`) are non-fatal under this sandbox's `powersave` governor but still recorded. While building this, discovered and fixed a real deployment defect: `vendor_server/artifacts/galois_keys.bin` (the 200-bit mirror) was missing, causing the 200-bit server to silently generate mismatched local Galois keys (max_abs_error=0.384); added the missing symlink (mirrors the existing `galois_keys_160.bin`/`model_weights.bin` pattern), fixing max_abs_error to 2.2e-07.

### 5.4 — [x] Separate latency and throughput experiments — **throughput MISSING**
- Latency: exactly one request in flight.
- Throughput: closed-loop concurrency sweep — **only valid after Phase 2**. The 16-lane batching is currently never benchmarked; add occupancy curves (1/4/8/16 lanes) and amortized per-transaction cost.
- **STATUS:** Fixed. NEW `tests/benchmark_throughput.py`: closed-loop `n_clients` sweep `{1,4,8,16}` x 30s against `vendor_server_160` (req/s, mean/p50/p99 latency-under-load, amortized per-tx cost), plus a single-client batch-occupancy sweep (`lanes in {1,4,8,16}`, 100 rounds + 10 warmup, no concurrent load). Asserts `PPFD_GRPC_THREADS>=4`. Real run -> `artifacts/throughput_results.json`: n_clients=16 -> 121.26 req/s, mean_latency_ms=130.05, p99_latency_ms=361.70; occupancy lanes=16 -> per_tx_us=1070.05.

### 5.5 — [x] In-band parity gate on every timing run — **MISSING**
- Before timing a configuration, decrypt that config's encrypted output and check against the plaintext oracle within tolerance. Discard timings from any run that fails parity. This makes performance numbers claims about a *correct* system by construction.
- **STATUS:** Fixed. NEW `scripts/parity_gate.py`: `load_model_weights()` + `verify_encrypted_output(...) -> (passed, max_abs_error)` against the plaintext logistic-regression oracle. Integrated into `tests/benchmark_comparison.py` (`_run_parity_gate`, run once per variant before warmup/measurement) and `tests/benchmark_throughput.py` (`_run_parity_gate`, run before the concurrency/occupancy sweeps). Both raise on failure; the verification run's timing is discarded.

### 5.6 — [x] The execution matrix
| Axis | Levels |
|---|---|
| Reduction strategy | sequential fold · BSGS two-layer · hoisted flat (where lib permits) |
| Modulus chain | {60,40,60} · {60,40,40,60} · (+1 level "masked" variant for the privacy-cost result) |
| Parallelism | 1 / 2 / 4 / 8 threads, pinned |
| Batch occupancy | 1, 4, 8, 16 lanes |
| Library | SEAL · OpenFHE · Lattigo (high-value) |
- **STATUS:** Fixed. NEW `scripts/build_execution_matrix.py` -> `artifacts/execution_matrix.json`. `reduction_strategy_x_modulus_chain_x_library`: SEAL/160-bit fold/bsgs/naive all MEASURED (mean latency_us 4017.65 / 8544.72 / 121271.00); SEAL/200-bit fold MEASURED (9651.11); SEAL/200-bit bsgs/naive and all OpenFHE cells are PENDING with a documented reason (200-bit local-circuit binary has no `--strategy` dispatch beyond fold, scoped out per §5.1; OpenFHE not installed) — never estimated. `parallelism_axis` (threads 1/2/4/8, n_clients=8, lanes=16) and `occupancy_axis` (lanes 1/4/8/16, from §5.4) are both fully MEASURED.

### 5.7 — [x] Two cheap, high-value measurements
- **Ciphertext wire size per chain** (the 160-bit chain also shrinks bandwidth — a deployment-relevant number nobody reports).
- **Amortized per-transaction cost vs lane occupancy** (the batching story, currently claimed and never measured).
- **STATUS:** Fixed. Part A: NEW `vendor_server/src/wire_size_probe.cpp` (standalone, OUT-OF-TCB) + `scripts/measure_wire_size.py` -> `artifacts/wire_sizes.json`: standard (public-key) ciphertext sizes 160-bit=262257 bytes, 200-bit=393329 bytes, plus seeded (`Serializable<Ciphertext>`/`encrypt_symmetric`, ~2x smaller) and zlib/zstd compressed sizes (CKKS ciphertexts are high-entropy: compression ratio ~1.0, i.e. no benefit). Part B: NEW `scripts/generate_amortization_table.py` -> `artifacts/amortization_table.json`, derived from §5.4's occupancy sweep: per_tx_us drops from 16104.70 (lanes=1) to 1070.05 (lanes=16), amortization_factor up to 15.05x.

### 5.8 — [x] The privacy-cost-in-modulus-bits result
- Quantify: input-privacy-only at {60,40,60} vs model-privacy-with-masking requiring the restored level — in latency, bandwidth, and precision. The finding that the mask level costs ~exactly the optimization headroom is the paper's most quotable sentence.
- **STATUS:** Fixed. NEW `scripts/privacy_cost_analysis.py` -> `artifacts/privacy_cost_analysis.json`, using the 200-bit vs 160-bit pair (one additional 40-bit RNS prime = one additional multiplicative level, e.g. for model-weight masking) as the proxy. Real measured deltas: latency +6995.0us (+65.5%) median, bandwidth +131072 bytes (+50.0%) per ciphertext, precision both chains within the existing ~1e-7 noise floor (160-bit max_abs_error=4.19e-11, 200-bit=2.08e-07). `key_finding` is a one-sentence summary stating this explicitly.

---

## Phase 6 — Reproducibility / artifact (WAHC increasingly expects this)

### 6.1 — [x] Strip build artifacts from git — **MISSING hygiene**
- **STATUS:** Done. `.gitignore` updated to cover `build/`, `vendor_server/build/`, `CMakeFiles/`, `*.o`, `*.a`, `*.so`, `*.bin.d`, `*.o.d`, and compiled binaries. All 739 previously-tracked build files untracked via `git rm -r --cached build/ vendor_server/build/`. Clean build verified: `cmake -B vendor_server/build -S vendor_server` + `cmake --build vendor_server/build --parallel` succeeds; `ctest --test-dir vendor_server/build` 1/1 PASS. Absolute `/home/raghavp/BTP` paths baked into `.o.d` dependency files are no longer in the git object store.

### 6.2 — Push the supply-chain work that exists but isn't here — **MISSING**
- `compose.prod.yaml`, SHA-pinned images, GHCR/digest pinning, cosign keyless signing, SBOM, GitHub Actions provenance — all done during your Docker modules, **none pushed** to this repo (last commit adds only Dockerfiles/compose/.dockerignore).
- **Note (Phase 6, 2026-06-17):** out of scope for this artifact-evaluation pass; supply-chain provenance is a separate track.

### 6.3 — [x] Add what's missing for artifact evaluation
- **STATUS (README):** `README.msd` renamed to `README.md` via `git mv`; dangling `bank_client/frontend` FastAPI reference removed from "Core Stack"; all `compiler/linearize.py` references updated to `compiler/train_logistic_regression.py`; `ctest --test-dir build` corrected to `ctest --test-dir vendor_server/build`.
- **STATUS (LICENSE):** MIT `LICENSE` file added at repo root.
- **STATUS (reproduce script):** `scripts/reproduce_all.py` (new, 200 lines) implements all 13 measurement steps in order, with `--dry-run` (prints plan, exits 0), `--from N` (resume from step N), server auto-start/stop for steps requiring gRPC, output-existence + JSON-validity checks per step, and expected wall-time annotations. `Makefile` added at repo root with `make reproduce` / `make dry-run` / `make build` / `make test` / `make clean` targets. `python3 scripts/reproduce_all.py --dry-run` verified: exits 0, prints full 13-step plan.
- **STATUS (key-gen pipeline):** `compiler/gen_keys_160.py` is step 2 of `scripts/reproduce_all.py`; ordered sequencing and error exits are enforced by the script's `_run_step` function (non-zero exit → `sys.exit(returncode)`).
- Fixed seeds (`random_state=42`) kept and documented in `compiler/train_logistic_regression.py` module docstring.

### 6.4 — [x] Fix the dataset/accuracy methodology defects
- **STATUS (winsorization leakage):** `compiler/train_xgboost.py` — the `scipy.stats.mstats.winsorize` calls on `X_train_scaled` and `X_test_scaled` used each split's own quantiles (leakage). Fixed: replaced with `np.percentile(X_train_scaled, 1.0/99.0, axis=0)` + `np.clip(...)` applied to both splits using train-set bounds only. `scipy.stats.mstats` import removed. AUC will change on next `train_xgboost.py` run; the honest number is now reported by `train_logistic_regression.py`'s `linearization_cost_auc` field.
- **STATUS (linearize.py naming):** `compiler/linearize.py` renamed to `compiler/train_logistic_regression.py` via `git mv`. Module-level docstring added explicitly stating: XGBoost is used for dataset validation only; the HE inference model is an independent surrogate LR fitted on the same train split; `linearization_cost_auc = xgb_test_auc - lr_test_auc` is written to `artifacts/linearization_cost.json` after each run. `compiler/auc_dispatch.py` updated to import from `train_logistic_regression`. References in `README.md`, `docs/code_and_data_flow.md`, `docs/spec.md` updated.

---

## Phase 7 — Transciphering arm (optional Phase 2 of the paper; hard gate = Phases 0–6 done)

> Strengthens the paper meaningfully but is a second cryptographic subsystem with its own attack literature. Do **not** bolt it onto an unverified core.

### 7.1 — Library reality
- RtF transciphering (FV cipher eval + StC/half-boot + FV→CKKS) is **not implementable in SEAL** (no bootstrapping, no scheme bridge). Reference impl is **Lattigo `ckks_fv`** (KAIST RtF-Transciphering); the CHES 2025 SoK code extends it.
- **Decision:** Path A — adopt a Lattigo transciphering front-end as a separate ingestion service feeding your evaluator. Or Path B — quantize to integers and use BFV/BGV + Pasta/Masta (more mature, better-audited; abandons CKKS + your modulus ablation). Document the road not taken either way.

### 7.2 — Cipher choice must respect the attack record
- **Rubato:** Grassi et al. (CRYPTO 2023) — key recovery on full Rubato for ≥25% of modulus choices, 5 of 6 family members below claimed security. Choose parameters post-attack.
- **HERA:** has third-party algebraic cryptanalysis (round-key collisions). Usable as fallback with current parameters.
- **Elisabeth-4:** broken — do not use.
- **Design requirement:** cipher agility — treat the cipher as a replaceable module, parameters pinned to post-attack recommendations. State plainly that HHE trades bandwidth for a younger, less-audited symmetric assumption.

### 7.3 — Architecture deltas (extend Phase 1)
- Bank now also holds a symmetric key `k`; provisions vendor with `Enc_FV(k)` — a standing capability to transcipher any traffic under `k`. Add symmetric key rotation, nonce-uniqueness, replay rejection as first-class obligations.
- AEAD on the symmetric channel becomes load-bearing (stream ciphertexts are trivially additively malleable).
- Extend the canary handshake (1.5) **through** the FV→CKKS conversion — a new silent-wrong-answer surface.
- Precision: RtF requires `t > precision bits`; transciphered inputs arrive at framework-determined precision/level — co-design with scale, chain, and the flooding budget (1.6).

### 7.4 — The research delta (what's actually new)
- "Transciphering + ML" exists (SoK runs ResNet-20 through RtF). **Not** novel to transcipher an ML model.
- **Novel and yours:** the **small-circuit break-even map** — at what circuit depth / batch occupancy / request rate / uplink bandwidth does HHE beat plain CKKS upload *end-to-end* for latency-sensitive scoring? ResNet-20 is the easy pole; your depth-1 2 ms circuit is the opposite pole nobody has mapped. Use the **offline/online split** as the moving variable.

### 7.5 — The bandwidth ladder (measure all rungs for a realistic ~8 KB payload)
1. SEAL seeded fresh ciphertexts (`Serializable<Ciphertext>`) — halves upload at zero cost; your client doesn't even use this yet (320 KB → ~160 KB). **Do this in Phase 6 regardless — it costs days and inoculates the bandwidth discussion.**
2. Minimal-level / trimmed-chain encryption — your 160-bit ablation as a *bandwidth* result.
3. LWE-symmetric + LWEs-to-RLWE repacking (PEGASUS-style middle option).
4. Full RtF transciphering.

Report per rung: expansion factor, client CPU/energy, server cost, online latency.

---

## Sequencing & decision points

```
Phase 0 (correctness) ──┐
Phase 1 (trust model) ──┤ all three are non-negotiable for an honest system
Phase 2 (concurrency) ──┘   → completes the BTP regardless of publication
        │
        ▼
Phase 3 (params) + Phase 4 (rotation core) + Phase 5 (measurement)
        │   ← around here, make the publication call with real data in hand
        ▼
Phase 6 (artifact)  → preprint-ready, portfolio-ready
        │
        ▼
Phase 7 (transciphering)  → only if timeline allows; short→full paper upgrade
```

**Decision point (≈ end of Phase 5):** if the corrected-core trade-space curves and the privacy-cost result land cleanly → commit to a **WAHC full paper**, do Phases 4.3 + 7. If results are more incremental → commit to a **WAHC 6-page demo/short paper**, skip 4.3 + 7, ship the preprint.

**Target:** WAHC 2026 (CCS pre-conference workshop, ~July 2026 deadline based on the stable 2022–2025 cadence). PETS is **not** the venue — the delta is empirical/systems, not a novel privacy mechanism.

**Hard rule:** do not put a single number from the *current* artifacts in front of a reviewer. Every headline number today is either wrong (fold), incoherent (logit-vs-prob parity), or self/estimated-baseline (speedup).

---

## One-line status of the current repo

Phases 0–4 are complete: the core kernel computes the correct 256-term dot product (verified by a randomized oracle and an end-to-end logit-vs-logit parity harness, max_abs_error=4.3e-7, ROC-AUC=0.979 over the full held-out set); the vendor server has no decryption capability by construction, fail-closed Galois-key provisioning, a verified `PROV_AWAITING_KEYS -> PROV_VALIDATING -> PROV_READY`/`PROV_FAULT` state machine with a canary handshake, mTLS, and a written semi-honest threat model (`docs/spec.md` §6); `vendor_server_160`'s per-request HE buffers are now thread-confined (`static thread_local`), verified correct and deterministic under a 32-request/8-thread concurrent stress test; both the 160-bit and 200-bit CKKS contexts explicitly assert `sec_level_type::tc128` (HE-standard v1.1 Table 2, 218-bit bound at N=8192) with measured precision headroom (`artifacts/precision_analysis.json`, ~21.1 bits remaining), with the BFV-only `invariant_noise_budget` check removed from `tests/verify_all.py` in favor of CKKS-appropriate structural checks; and the rotation/reduction trade-space now has three named, precisely-defined strategies (`docs/spec.md` §7) — SEAL sequential fold (`hoisted_tree_sum`, 8 rotations/8 critical-path steps, unhoisted), SEAL BSGS two-layer (new `bsgs_reduction`, 30 rotations/2 critical-path steps, unhoisted, verified against a plaintext oracle), and OpenFHE hoisted flat (`tools/openfhe_benchmark/`, genuine Halevi-Shoup hoisting via `EvalFastRotation`, code-complete but unbuilt — OpenFHE not installed in this environment, results PENDING) — with `scripts/rotation_strategy_comparison.py` producing a unified, methodology-annotated comparison table (`artifacts/rotation_strategy_comparison.json`). Phase 5 (honest measurement methodology) is now complete: the fabricated naive baseline is gone (`benchmark_160 --strategy=naive`, real 255-rotation measurement), the 38-48% figure is reframed as a Type 1 self-ablation (`docs/spec.md` §5.7) with real re-measured numbers (36.97% mean / 39.59% median reduction, `artifacts/comparison_results.json`), `tests/benchmark_comparison.py` meets the ≥1000-iteration/median/IQR/bootstrap-CI/Mann-Whitney bar, latency and throughput are measured separately (`tests/benchmark_throughput.py` -> `artifacts/throughput_results.json`), every timing run is gated by `scripts/parity_gate.py`, the full execution matrix is built (`artifacts/execution_matrix.json`, PENDING cells documented not estimated), ciphertext wire sizes and the per-transaction amortization table are measured (`artifacts/wire_sizes.json`, `artifacts/amortization_table.json`), and the privacy-cost-in-modulus-bits result is measured (`artifacts/privacy_cost_analysis.json`). Phase 6 (reproducibility/artifact hygiene) is now complete: build artifacts untracked from git (739 files, `git rm -r --cached`; clean `cmake --build` + `ctest` verified); `README.msd` renamed to `README.md` with dangling `bank_client/frontend` reference removed; `LICENSE` (MIT) added; `scripts/reproduce_all.py` implements the full 13-step pipeline with `--dry-run`, `--from N`, server auto-management, output validation, and `Makefile` targets (`make reproduce` / `make dry-run`); `compiler/linearize.py` renamed to `compiler/train_logistic_regression.py` with an honest methodology docstring (independent LR surrogate, not XGBoost linearization) and `artifacts/linearization_cost.json` output; winsorization leakage in `compiler/train_xgboost.py` fixed (train-set `np.percentile` bounds applied to both splits). Phase 7 (transciphering) is optional and requires a separate timeline decision.
