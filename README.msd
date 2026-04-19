# PPFDaaS (Privacy-Preserving Fraud Detection as a Service)

PPFDaaS is a privacy-preserving payment fraud detection system built with CKKS homomorphic encryption. The bank encrypts transaction features locally, the vendor performs inference on ciphertext, and only the bank decrypts the score. Plaintext transaction data never leaves the bank boundary.

This README is based on the implementation handoff in PROJECT_STATE.md and the normative engineering specification in docs/spec.md (v1.1).

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

## Variant Strategy (200-bit vs 160-bit)

Both variants keep n=8192 and maintain 128-bit security. The reduced 160-bit variant removes one middle prime, reducing compute cost for the implemented depth while preserving required contracts.

Reported benchmark summary from artifacts/comparison_results.json (project state snapshot):
- 200-bit mean total inference: about 4872.5 us
- 160-bit mean total inference: about 2518.6 us
- Total latency reduction: about 48.3%

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

## Artifacts and Outputs

Common generated outputs include:
- Model and feature artifacts in artifacts/
- Logs in logs/
- Comparison and benchmark JSON/CSV outputs under artifacts/ and script-defined result paths

## Source of Truth

- Implementation handoff and sprint notes: PROJECT_STATE.md
- Normative engineering spec and contracts: docs/spec.md

For any interface or contract-sensitive change (proto fields, timing breakdown semantics, CKKS parameterization), follow docs/spec.md first and treat PROJECT_STATE.md as execution history and measured status context.
