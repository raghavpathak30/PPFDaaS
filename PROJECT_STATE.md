# PPFDaaS Project State — 2026-08-17

Full dated session-by-session history lives in `docs/SESSION_LOG.md`. This
file holds only what's true right now.

## Current status per arm

### CKKS core
Three CKKS variants coexist:
- **200-bit baseline** (`ckks_context.cpp`): n=8192, coeff_modulus
  `{60,40,40,60}`.
- **160-bit deployed** (`eval_context_160.cpp` / `inference_service_160.cpp`):
  n=8192, coeff_modulus `{60,40,60}`. Benchmark winner (~48% latency
  reduction over 200-bit, see `docs/SESSION_LOG.md` 2026-04-13); this is
  the variant behind the currently-deployed vendor server.
- **Degree-2 fallback** (`ckks_context_depth2.cpp`): n=16384, coeff_modulus
  `{60,40,40,40,60}`. **Implemented but broken**: `compiler/degree2_linearizer.py`
  computes `N_FEATURES_D2 - N_TOP_LINEAR - N_INTERACT = 512 - 256 - 496 =
  -240`, an invalid negative `np.zeros()` dimension — confirmed still
  present in current source (2026-08-17). Calling `linearize_degree2(...)`
  raises `ValueError` immediately. Never hit in production because the
  primary depth-1 path's AUC (0.979) never triggers the fallback
  (`artifacts/dispatch_result.json`: `active_path: "depth1"`). Unfixed
  since it was found in the 2026-07-27 session.

### Transciphering / HHE (Phase 7)
Two symmetric ciphers are now implemented and benchmarked:
`tools/transciphering/cipher/hera.go` (HERA-16, m=16, r=5, t=2^26) and
`cipher/rubato.go` (Rubato-128L, n=64, r=2, q=0x1fc0001), selectable via
`bench --cipher=hera|rubato`. Both derive round keys via a shared
SHAKE256-XOF scheme (`cipher/shakeprf.go`) ported verbatim from the pinned
`ckks_fv` reference — this replaced HERA's prior fixed round-constant table
(whose 5th row was an admitted unsourced placeholder) and AES-ECB-PRF key
schedule, neither of which matched the reference. `CanaryCheckTranscipher`
RPC and provisioning scaffolding exist in `proto/inference.proto`;
vendor-side BFV evaluation is stub-only in `vendor_server`, for either
cipher. It is **not** pending the KAIST `ckks_fv` scheme bridge: the bridge
is vendored and pinned at commit `105fc73115b56f1d6ff357029c7682b19a6d8510`
under `third_party/` (gitignored checkout, reproducible via
`third_party/fetch_rtf.sh`, toolchain documented in
`third_party/BUILD_NOTES.md`). It builds clean under Go 1.25 with zero
source changes — `go vet ./...` and `go test -c -run '^$' .` both exit 0 in
`ckks_fv/`.

**`tools/transciphering` itself now depends on `third_party/` being
vendored, not just `toy_correctness/`'s tests.** `cipher/rubato.go`'s
Gaussian noise sampler calls `github.com/ldsec/lattigo/v2`'s
`ring.GaussianSampler.AGN` (a KAIST-CryptLab addition absent from the
public `lattigo/v2` module — confirmed by diffing against it), satisfied
via a `go.mod replace` to the relative path `../../third_party/RtF-Transciphering`.
Run `third_party/fetch_rtf.sh` before `go build ./...` in that module; see
`tools/transciphering/README.md`'s "Build / run" section. Checked: this
doesn't break any existing automated pipeline — neither `Dockerfile.client`
nor `Dockerfile.server` invokes the Go toolchain, and no CI workflow exists
in this repo at all.

**Correctness gates, both required before any timing number is trusted, and
both PASS for both ciphers:**
- **Gate A — known-answer test** (`cipher/hera_test.go`, `rubato_test.go`):
  this module's keystream vs. `ckks_fv`'s own `plainHera`/`plainRubato`
  reference for fixed inputs. Caught a real bug during implementation
  (Rubato's Feistel layer reducing mod the wrong modulus via an
  accidentally-shared helper) before any measurement. **Cannot and does not
  validate the noise sampler's distribution** — it runs Rubato at `sigma=0`
  by design. That gap let a second, more serious bug through review-invisible
  to both gates: see below.
- **Gate B — toy-scale HE harness** (`toy_correctness/`): full server-side
  path (cipher-in-BFV → HalfBoot → FV→CKKS repack → CKKS eval → decrypt) at
  `LogN 16→10`, all moduli reused. **PASS, all three configs**: HERA
  r=4/"80as" (pre-existing, max abs error ~2e-5), HERA r=5/"128as" (new,
  exact parity with `HeraRounds=5`, max abs error 1.4e-5), Rubato-128L (new,
  includes real Gaussian noise, max abs error 3.4e-5) — all against a 5e-2
  tolerance. Caveat unchanged: the CKKS stage evaluates a trivial `2x+1`
  circuit, not the fraud model. Also cannot validate the noise
  *distribution* (only that CKKS's tolerance absorbs its magnitude) — but
  this gate uses `ckks_fv`'s own `plainRubato`/`ring.GaussianSampler`
  directly, so it was never exposed to the client-module bug below.

**Correctness bug found and fixed after initial measurement (found by
review, not by either gate):** `cipher/rubato.go`'s Gaussian noise sampler
initially approximated the reference's discrete Gaussian via a
Box-Muller transform over `crypto/rand`-drawn uniforms, never verified
statistically equivalent to `ckks_fv`'s actual `ring.GaussianSampler.AGN`.
Since Rubato's security rests on an LWE-style assumption stated over
Gaussian error, this was a correctness bug, not a performance detail.
Fixed: `noiseAGN` now calls the reference sampler directly (`go.mod`
`replace`s `github.com/ldsec/lattigo/v2` with the pinned
`third_party/RtF-Transciphering` checkout, since `AGN` is a KAIST-CryptLab
addition absent from the public module). Added
`TestRubatoNoiseStatistics` (empirical mean/std-dev vs. `sigma`, PASS) to
catch this class of bug going forward. **This changed the measured
numbers substantially, including their direction** — see below.

**HERA-vs-Rubato cross-arm comparison (re-measured after the fix, same
session, AC power, powersave governor):** Rubato-128L client encrypt now
costs **~0.48-0.82x HERA-16 per record** across a 1/4/8/16-lane sweep (256
features/txn, n=200) — i.e. Rubato is CHEAPER, reversing the first
(wrong-sampler) measurement of ~1.75-2.25x, which is preserved (not
deleted) at `artifacts/hera_vs_rubato_transciphering_PRIOR_wrong_noise_sampler.json`
for exactly this kind of before/after diff. Per keystream element (HERA
yields 16/block, Rubato 60, confirmed via a same-invocation 7-repetition
micro-benchmark under low/clean load): **~0.77x with Gaussian noise
included, ~0.57x without** — also reversed from the prior ~3.05x/~1.19x.
The wrong sampler's own overhead, not Rubato's algebra, was the dominant
cost in the original measurement. (An intermediate re-measurement pass
reported ~0.70x/~0.53x from a less rigorous 5-repetition run; a review pass
flagged the without-noise figure as suspicious since that code path never
invokes the sampler at all — investigated and confirmed the discrepancy
traces to the FIRST report's single-shot, low-repetition benchmark
methodology, not the sampler fix; both rigorous re-measurements converge
to ~144-146 ns/element independently. See
`tools/transciphering/README.md`'s per-element section for the full
account.) Upload size is identical between the two ciphers at every lane
count — the swap changes client CPU cost only, not the wire-format
reduction vs. plain CKKS (16x-249x smaller). Gate B toy-scale (LogN=10)
wall time/RSS (re-verified after the fix, since the Rubato figure is the
basis for a full-scale memory-risk estimate: calls `ckks_fv`'s own
`plainRubato`/`ring.GaussianSampler` directly, confirmed never exposed to
the client-side bug, and RSS confirmed stable across 4 total runs — ~7%
band for Rubato, ~3.5% for HERA, under both clean and elevated-load
conditions): HERA r=5 ~287MB (283-293MB range), Rubato-128L ~611-654MB —
the only server-side evidence available while `vendor_server` is
stub-only, and not the deciding
number: by multiplicative depth (read directly from `ckks_fv`'s source —
HERA's cube S-box is 10 sequential-multiplication levels across 5 rounds,
Rubato's Feistel-square is 2, since its per-round squarings are mutually
independent and parallel), Rubato should need ~5x fewer sequential
ciphertext multiplications homomorphically — the corrected client numbers
now happen to point the same direction, but that agreement isn't
confirmation (the two are different cost models); confirming the depth
argument is blocked on the same `vendor_server` integration as everything
else in this section. Full
numbers, machine state, and correctness-gate detail:
`artifacts/hera_vs_rubato_transciphering.json`.

Full-scale (non-toy) RSS instrumentation exists for HERA only (not yet
re-run for Rubato): **CONFIRMED-RAN.** `hera.Crypt` completed at the real
secure LogN 16 / "128as" params on this 15 GB host: 73.7 s total, peak
VmHWM 9.54 GB, zero swap, under `GOMEMLIMIT=11GiB GOGC=50`. Memory is ~97%
setup (StC precompute + key material) vs. ~3% `hera.Crypt`; runtime is ~70%
`hera.Crypt`, of which ~92% is the cube/S-box step.

**Real blocker:** integrating the validated reference path into
`vendor_server` — not hardware procurement, and not affected by which
cipher is used. Open items: (1) `artifacts/hhe_breakeven.json` — all cells
still PENDING, independent of this task; (2) batched-reduction correctness
at 256-slot blocks (everything verified so far is single-block, for both
ciphers); (3) the toy harnesses still run a trivial `2x+1` circuit, not the
real fraud model; (4) `scripts/cloud_transcipher_bench/run_benchmark.sh`'s
~90 GB preflight gate is ~9.4x the measured HERA peak and needs revising
regardless of whether cloud is ever used; (5) full-scale RSS instrumentation
for Rubato-128L has not been run. The prior "~9.0-9.4x HERA vs. plain CKKS"
client-CPU ratio (`docs/SESSION_LOG.md` 2026-08-06c/d) was measured against
the pre-fix HERA implementation and should not be combined with the numbers
above — a fresh plain-CKKS baseline was not re-run this session (requires
`vendor_server`/gRPC infrastructure, out of scope for this cipher-swap
measurement pass).

### Docker / DevSecOps
No session-log entry covers this arm. `Dockerfile.client`, `compose.prod.yaml`,
and `.dockerignore` currently show as modified in the working tree
(uncommitted) with no session narrative explaining why. **Status:
unaudited** — flagging rather than guessing.

### Paper (WAHC 2026 cycle)
Last touched 2026-07-02 (§8.3 parameter table, known-unknown framing,
consistency sweep). No entry since.

## Active blockers and next actions, in priority order

1. **HERA -> Rubato swap: DONE for the client-cipher and toy-HE-harness
   arms.** Rubato-128L implemented (`cipher/rubato.go`), both correctness
   gates pass for both ciphers, cross-arm client-CPU comparison measured
   same-session (`artifacts/hera_vs_rubato_transciphering.json`; see the
   Transciphering/HHE section above). NOT done: the RtF-transciphering-vs-
   plain-CKKS axis (blocked on `vendor_server` integration, unrelated to
   the cipher choice — pre-existing PENDING status, unaffected by this
   task) and the SIMD-circuit axis at the homomorphic-evaluation level
   (only client-cipher-level lane sweep and single-lane toy-harness
   correctness were measured, not many-lane HE throughput). SIMD batching
   efficiency research (transactions per batch vs. latency vs.
   bandwidth/data movement) not started.
2. Degree-2 `degree2_linearizer.py` negative-dimension bug — unfixed, but
   low urgency since it's not on the production dispatch path.
3. Docker/DevSecOps arm needs an audit pass: reconcile the uncommitted
   changes to `Dockerfile.client` / `compose.prod.yaml` / `.dockerignore`
   with an actual session record.

## Repo structure

Regenerated 2026-08-17 via:
```
find . -maxdepth 2 -not -path './.git*' -not -path '*/build/*' \
  -not -path './data/*' -not -path '*/__pycache__/*' -not -name '*.pyc' \
  -not -path './third_party/*' -not -path './.venv*' \
  -not -path './.pytest_cache/*' -not -path './logs/*' -not -path './.cursor/*'
```
(the old command in this file predated `tools/transciphering/`,
`third_party/`, `scripts/cloud_transcipher_bench/`, and the Docker files,
and didn't exclude `third_party/`/`.venv*`/`.venv-kaggle*`, which would
otherwise dump ~18k vendored-dependency lines here.)

```
.
├── artifacts/                  104 files — canonical result JSONs, keys, model bins (never hand-edit)
│   └── performance_revalidation/   paired baseline_powersave/ vs performance/ governor runs
├── AUDIT.md                    Phase 0 measurement-integrity audit (2026-06-19)
├── bank_client/
│   ├── backend/                 feature_pipeline_degree2.py
│   ├── frontend/
│   ├── he_wrapper/               seal_wrapper.cpp, seal_wrapper_160.cpp (pybind)
│   ├── bank_client.py
│   └── CMakeLists.txt
├── certs/                      dev TLS certs (ca/client/server)
├── compiler/                   train_xgboost.py, train_logistic_regression.py,
│                                degree2_linearizer.py, serialize*.py, auc_dispatch.py, gen_keys_160.py
├── docs/
│   ├── spec.md, PPFDaaS_Eng_Spec_v1.1.docx
│   ├── PPFDaaS_Audit_and_Defense_Guide.docx  (v1.1)
│   ├── RtF_Transciphering_Progress_v2/v3.pptx
│   └── SESSION_LOG.md          <- full session history
├── generated/, vendor_server/generated/   protoc/grpc codegen output
├── lab/                        mock_server.py (untracked, exploratory)
├── proto/inference.proto
├── PPFDaaS_REMEDIATION_PLAN.md  phased remediation roadmap (WAHC artifact target)
├── RESEARCH_FINDINGS*.md        v1-v3 (untracked)
├── results/                    ROC/ablation plots, ablation_hoisting.csv
├── scripts/                    benchmark/analysis scripts (bandwidth_ladder, e2e_latency_breakdown,
│                                privacy_cost_analysis, quantisation_sweep, rotation_strategy_comparison, ...)
│   ├── cloud_transcipher_bench/
│   └── governor_harness/       revalidate_under_performance.py, setup_performance_governor.sh
├── tests/                      benchmark_comparison.py, verify_all.py, numeric_oracle.py, ...
├── third_party/                vendored: openfhe-development, RtF-Transciphering
├── tools/
│   ├── lattigo_benchmark/
│   ├── local_benchmark/
│   ├── openfhe_benchmark/
│   └── transciphering/         cipher/{hera,backend}.go, bench/, toy_correctness/, results/
├── vendor_server/
│   ├── include/, src/           ckks_context{,_depth2}.cpp, eval_context_160.cpp,
│   │                            inference_service{,_160}.cpp, rotation_hoisting{,_degree2}.cpp,
│   │                            weight_loader{,_degree2}.cpp
│   └── tests/test_he_core.cpp
├── CMakeLists.txt
├── compose.yaml, compose.prod.yaml, Dockerfile.client, Dockerfile.server
└── PROJECT_STATE.md            <- this file
```

## Measurement rules

- Absolute benchmark latencies on this host are NOT comparable across
  sessions. Governor, battery/AC state, turbo availability and load all
  move them by 3-4x with unchanged code. Only same-session paired ratios
  are defensible. Concrete example: the 3.7x gap between the 2,933 µs and
  ~10,840 µs client encode+encrypt runs (`docs/SESSION_LOG.md`, 2026-08-06c)
  turned out to trace to the laptop running on battery rather than AC power
  (clock clamping to ~650-960 MHz against a 4.9 GHz ceiling) — see the
  2026-08-06 addendum in the session log.
- Never compare a number from one run against a number from a different
  date.
- `artifacts/*.json` are canonical. Never edit, regenerate, or overwrite
  one in place.
- Every claim carries CONFIRMED-RAN / CONFIRMED-SOURCE / UNVERIFIED.

## Key Spec Contracts (must not change)

Verified against current source on 2026-08-17:

- `model_weights.bin`: exactly 2060 bytes — offset 0: `uint32_t n_features
  = 256` (LE); offset 4: `float64 bias` (LE); offset 12: `float64[256]
  weights` (LE). **Verified** against `compiler/serialize_weights.py`.
- `degree2_weights.bin`: exactly 4108 bytes, same layout, N=512.
  **Verified** against `compiler/serialize_degree2_weights.py`.
- `TimingBreakdown` proto field order (breaking change if reordered):
  1 `deserialization_us`, 2 `multiply_plain_us`, 3 `rotation_hoisting_us`,
  4 `serialization_us`, 5 `total_inference_us`. **Verified** against
  `proto/inference.proto`.
- CKKS Depth-1: n=8192, coeff_modulus `{60,40,40,60}`, galois
  `{1,2,4,8,16,32,64,128}`. **Verified** against `ckks_context.cpp`.
- CKKS Depth-2 (Degree-2 fallback): n=16384, coeff_modulus
  `{60,40,40,40,60}`, galois `{1,2,4,8,16,32,64,128,256}`. **Verified**
  against `ckks_context_depth2.cpp`.
- **160-bit deployed variant** (was missing from this contract list
  entirely): n=8192, coeff_modulus `{60,40,60}`. **Verified** against
  `eval_context_160.cpp`.
- gRPC max message size — **corrected**, the old "512KB (Depth-1), 3MB
  (Degree-2)" figures match nothing in current source.
  `inference_service_160.cpp` (the deployed 160-bit service) explicitly
  sets 8MB send/receive. The original `inference_service.cpp` sets no
  explicit limit (gRPC default applies). `bank_client.py`'s
  `_grpc_options` helper defaults to a 512KB constructor argument but is
  caller-overridable, so it isn't a fixed contract either.
- Depth-1 latency budget: < 10,000 µs `total_inference_us`.
- `TimingBreakdown` invariant: deser + mul + rot + ser ≈ total, residual
  ≤ 300 µs.
