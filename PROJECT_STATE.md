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
HERA-16 is the currently-implemented symmetric cipher
(`tools/transciphering/cipher/hera.go`). `CanaryCheckTranscipher` RPC and
provisioning scaffolding exist in `proto/inference.proto`; vendor-side BFV
evaluation is stub-only in `vendor_server`. It is **not** pending the KAIST
`ckks_fv` scheme bridge: the bridge is vendored and pinned at commit
`105fc73115b56f1d6ff357029c7682b19a6d8510` under `third_party/`
(gitignored checkout, reproducible via `third_party/fetch_rtf.sh`,
toolchain documented in `third_party/BUILD_NOTES.md`). It builds clean
under Go 1.25 with zero source changes — `go vet ./...` and
`go test -c -run '^$' .` both exit 0 in `ckks_fv/`.

Two results now exist for the reference path (neither yet integrated into
`vendor_server`):
- **Toy correctness: PASS.** `RtFHeraParams[3]` ("128as") deep-copied with
  only `LogN: 16 -> 10` changed, all moduli reused; full server-side path
  (HERA-in-BFV transcipher -> HalfBoot -> FV->CKKS repack -> CKKS eval ->
  decrypt) run end-to-end. Max abs error 2.1e-5 against a 5e-2 tolerance.
  Caveat: the CKKS stage evaluated a trivial `2x+1` circuit, not the fraud
  model.
- **Full-scale: CONFIRMED-RAN.** `hera.Crypt` completed at the real secure
  LogN 16 / "128as" params on this 15 GB host: 73.7 s total, peak VmHWM
  9.54 GB, zero swap, under `GOMEMLIMIT=11GiB GOGC=50`. The prior ~60 GB
  RAM anchor was never itself measured end-to-end; this run supersedes it.
  Memory is ~97% setup (StC precompute + key material) vs. ~3% `hera.Crypt`;
  runtime is ~70% `hera.Crypt`, of which ~92% is the cube/S-box step.

**Real blocker:** integrating the validated reference path into
`vendor_server` — not hardware procurement. Open items: (1)
`artifacts/hhe_breakeven.json` — all cells still PENDING; (2)
batched-reduction correctness at 256-slot blocks (everything verified so
far is single-block); (3) the toy harness still runs a trivial `2x+1`
circuit, not the real fraud model; (4)
`scripts/cloud_transcipher_bench/run_benchmark.sh`'s ~90 GB preflight gate
is ~9.4x the measured peak and needs revising regardless of whether cloud
is ever used. Last measured same-session client-CPU ratio, HERA r=5 vs.
plain-CKKS 160-bit encode+encrypt: **~9.0-9.4x** (varies with desktop
contention; see `docs/SESSION_LOG.md` 2026-08-06c/d — same-session ratio
is the trustworthy number, not either absolute value).

### Docker / DevSecOps
No session-log entry covers this arm. `Dockerfile.client`, `compose.prod.yaml`,
and `.dockerignore` currently show as modified in the working tree
(uncommitted) with no session narrative explaining why. **Status:
unaudited** — flagging rather than guessing.

### Paper (WAHC 2026 cycle)
Last touched 2026-07-02 (§8.3 parameter table, known-unknown framing,
consistency sweep). No entry since.

## Active blockers and next actions, in priority order

1. **Active workstream**: swap the transciphering cipher HERA -> Rubato,
   re-measure, and produce a cross-arm comparison (SIMD circuit, HERA vs.
   Rubato, RtF transciphering vs. plain CKKS path). Alongside this,
   research SIMD batching efficiency — transactions per batch vs. latency
   vs. bandwidth/data movement.
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
