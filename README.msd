# PPFDaaS (Privacy-Preserving Fraud Detection as a Service)

PPFDaaS is a privacy-preserving payment fraud detection system built with CKKS homomorphic encryption. The bank encrypts transaction features locally, the vendor performs inference on ciphertext, and only the bank decrypts the score. Plaintext transaction data never leaves the bank boundary.

This README is based on the implementation handoff in PROJECT_STATE.md and the normative engineering specification in docs/spec.md (v1.1).

## Demo Day Quickstart (3 Commands)

Run these from repository root when you need the fastest path to a live demo.

1. Build binaries:

```bash
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

2. Ensure reproducible benchmark environment (recommended before benchmark):

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

3. Run demo:

```bash
source .venv/bin/activate
python scripts/demo_e2e.py
```

Optional benchmark evidence:

```bash
python3 tests/benchmark_comparison.py
```

## What This Repository Contains

- A production-oriented Depth-1 inference path using CKKS (n=8192).
- A reduced coeff-modulus variant (160-bit) that improves latency while preserving 128-bit security at n=8192.
- A Degree-2 fallback toolchain for cases where accuracy gates require deeper polynomial inference.
- End-to-end plumbing across C++, Python, gRPC/protobuf, and benchmarking scripts.

## Core Stack

- C++: Microsoft SEAL 4.1.2, gRPC, protobuf, OpenMP, CMake
- Python: scikit-learn, XGBoost, numpy, pybind11 bindings
- App shell: FastAPI backend and frontend scaffold under bank_client/frontend
- Tooling: Catch2 tests, Python verification and benchmark scripts

## High-Level Architecture

1. Data preprocessing and model artifact generation happen in Python (compiler and backend pipeline).
2. Bank client serializes and encrypts features using SEAL wrapper bindings.
3. Vendor gRPC service deserializes ciphertext, runs CKKS inference, and returns encrypted scores.
4. Bank client decrypts and post-processes predictions.

Important contract points from spec:
- CKKS scale stays at 2^40.
- Depth-1 path uses the 8-step rotation key set {1,2,4,8,16,32,64,128}.
- Proto and timing fields are aligned end-to-end, including deserialization timing.

## Repository Layout (Key Paths)

- CMake and root orchestration:
  - CMakeLists.txt
- Bank side:
  - bank_client/bank_client.py
  - bank_client/backend/feature_pipeline_degree2.py
  - bank_client/he_wrapper/seal_wrapper.cpp
  - bank_client/he_wrapper/seal_wrapper_160.cpp
- Vendor side:
  - vendor_server/src/
  - vendor_server/include/
  - vendor_server/tests/
- Compiler/data pipeline:
  - compiler/train_xgboost.py
  - compiler/linearize.py
  - compiler/degree2_linearizer.py
  - compiler/serialize_weights.py
  - compiler/serialize_degree2_weights.py
  - compiler/auc_dispatch.py
- Interface definitions:
  - proto/inference.proto
  - generated/
- Validation and benchmarking:
  - tests/verify_all.py
  - tests/benchmark_comparison.py
  - scripts/demo_e2e.py

## Build

From the repository root:

```bash
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

Notes captured from repository state:
- If protobuf is installed from Debian packages, CMake module mode for protobuf is expected.
- gRPC plugin path is typically /usr/bin/grpc_cpp_plugin on Debian-based systems.

## Dataset Setup

The ULB credit-card dataset is required locally as `data/creditcard.csv`, but it is intentionally not tracked in git because of repository size limits.

Use the helper script:

```bash
bash scripts/fetch_creditcard_dataset.sh
```

The script supports two modes:
- Kaggle CLI mode (automatic): requires `kaggle` CLI plus credentials configured in your environment.
- Manual mode: prints exact expected path and naming so you can place the CSV directly at `data/creditcard.csv`.

## Typical Workflow

1. Train and linearize model artifacts.
2. Generate or refresh keys.
3. Build C++ binaries.
4. Start vendor server.
5. Run bank client inference flow.
6. Validate with tests and benchmark scripts.

Representative commands (adjust to your environment):

```bash
bash scripts/fetch_creditcard_dataset.sh

python3 compiler/train_xgboost.py
python3 compiler/linearize.py
python3 compiler/gen_keys_160.py

cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel

python3 tests/verify_all.py
python3 tests/benchmark_comparison.py
```

## Full Demo Sequence (Copy/Paste)

Use this exact sequence from a clean terminal at repository root.

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
python3 compiler/linearize.py
python3 compiler/gen_keys_160.py
```

### 3) Run Contract/Structure Verification

```bash
python3 tests/verify_all.py
ctest --test-dir build --output-on-failure
```

### 4) Run Performance Comparison (Auto-starts both servers)

For reproducible latency-gate comparisons, set CPU governor to performance first:

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

Then run:

```bash
python3 tests/benchmark_comparison.py
```

Notes:
- The benchmark now prints a CPU governor warning when not in performance mode.
- Gate thresholds are calibrated to reference hardware; cross-machine variance is expected.

Professor trace mode (full per-request pipeline trace to stderr):

```bash
TRACE=1 python3 tests/benchmark_comparison.py
```

What this adds:
- PRE-ENCRYPT (batch shape + request UUID)
- POST-ENCRYPT (ciphertext byte size)
- GRPC-SEND / GRPC-RECV timestamps and request-id echo
- timing table with 5 TimingBreakdown fields
- POST-DECRYPT raw score + fraud probability (slot 0)
- end-of-run aggregate trace summary (means/std, rotation share, >3000us count, min/max round-trip)

Output channels:
- Trace logs: stderr
- Existing JSON summary and gates: stdout

### 5) Run the End-to-End Encrypted Inference Demo

```bash
source .venv/bin/activate
python scripts/demo_e2e.py
```

If the script appears stuck with no output, pull latest changes and rerun; startup readiness now uses TCP port checks instead of buffered server stdout lines.

### 6) Generate Research Figures/Tables

```bash
python3 scripts/generate_research_artifacts.py
python3 scripts/generate_ablation.py
python3 scripts/generate_roc.py
```

### Accuracy Check (Professor-Ready)

This prints dataset imbalance and model quality (ROC-AUC and PR-AUC) on the current artifacts:

```bash
python3 scripts/show_accuracy_check.py
```

### 7) Where Outputs Land

- Result plots and CSV/JSON summaries: `results/`
- Benchmark JSON: `artifacts/comparison_results.json`
- Additional logs: `logs/`

### Optional: Manual Server Run (Separate Terminal)

Only needed if you want to run client calls manually against a persistent server.

Terminal A:

```bash
./build/vendor_server/vendor_server_160 artifacts/model_weights.bin 50052
```

Terminal B:

```bash
python3 scripts/generate_ablation.py
python3 scripts/demo_e2e.py
```

## Variant Strategy (200-bit vs 160-bit)

Both variants keep n=8192 and maintain 128-bit security. The reduced 160-bit variant removes one middle prime, reducing compute cost for the implemented depth while preserving required contracts.

### Fair Benchmark Results (May 28, 2026 - Corrected)

**Previous measurements were not apples-to-apples** (160-bit measured only `multiply_plain` without rotations). Fair benchmarks now measure identical full inference pipelines:

- **160-bit mean latency**: 4.80 ms (5-run average with full pipeline: encrypt + multiply + rescale + 8 Galois rotations)
- **200-bit mean latency**: 7.23 ms (5-run average with full pipeline: encrypt + multiply + rescale + 8 Galois rotations)  
- **True speedup**: **1.51x** (160-bit faster, not the previously-claimed 3.92x)
- **Variance**: 160-bit ±4.7%, 200-bit ±2.1% (both highly reproducible)

**Cost breakdown** (why only 1.51x despite 25% smaller modulus):
- Galois rotations (8 parallel ops): ~62% of total latency
- multiply_plain: ~14%
- Encryption + rescale + I/O: ~24%

Rotations don't scale purely with modulus size due to hardware acceleration (AVX-2 permutation). Full analysis in [BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md).

**Framing (Phase 5, docs/spec.md §5.7):** the 1.51x figure above, and the
~37-40% total_inference_us reduction in docs/spec.md §5.4 / artifacts/comparison_results.json,
are both **Type 1 self-ablations** -- same codebase/circuit/hardware, only the
160-bit vs 200-bit modulus chain differs. Neither is a comparison against an
external baseline library or a different reduction strategy; see
docs/spec.md §5.7 for the full Type 1/2/3 definitions, and
artifacts/rotation_strategy_comparison.json for the Type 2 (fold vs BSGS vs
naive) strategy comparison.

## Current Status Snapshot

From PROJECT_STATE.md and spec update sections:
- Functional correctness: passing in current handoff state.
- Warmed steady-state latency targets: passing in measured runs.
- Remaining operational concern: cold-start behavior may require separate acceptance handling depending on deployment SLA.

## Verification and Testing

Use Python and C++ validation together:

```bash
python3 tests/verify_all.py
ctest --test-dir build --output-on-failure
```

If you need focused performance evidence generation:

```bash
python3 tests/benchmark_comparison.py
python3 scripts/generate_research_artifacts.py
python3 scripts/generate_ablation.py
python3 scripts/generate_roc.py
```

For stable benchmark comparisons across runs, prefer performance CPU governor before executing `tests/benchmark_comparison.py`.

## Artifacts and Outputs

Common generated outputs include:
- Model and feature artifacts in artifacts/
- Logs in logs/
- Comparison and benchmark JSON/CSV outputs under artifacts/ and script-defined result paths

## Source of Truth

- Implementation handoff and sprint notes: PROJECT_STATE.md
- Normative engineering spec and contracts: docs/spec.md

For any interface or contract-sensitive change (proto fields, timing breakdown semantics, CKKS parameterization), follow docs/spec.md first and treat PROJECT_STATE.md as execution history and measured status context.
