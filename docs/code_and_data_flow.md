# PPFDaaS Code And Data Flow Guide

## 1) What This System Does

PPFDaaS is a privacy-preserving fraud inference pipeline built around CKKS homomorphic encryption.

- Bank-side code encrypts feature vectors and sends ciphertext only.
- Vendor-side code computes inference over ciphertext and returns ciphertext scores.
- Bank-side code decrypts and applies sigmoid + bias for probabilities.

Core contract definitions are in [proto/inference.proto](../proto/inference.proto).

## 2) End-To-End Pipeline At A Glance

1. Raw dataset is loaded from [data/creditcard.csv](../data/creditcard.csv).
2. Training pipeline builds processed 256-feature arrays and an XGBoost model in [compiler/train_xgboost.py](../compiler/train_xgboost.py).
3. Linearization pipeline trains a depth-1 logistic model and serializes 2060-byte weight binary in [compiler/linearize.py](../compiler/linearize.py).
4. Key generation writes 160-bit HE keys in [compiler/gen_keys_160.py](../compiler/gen_keys_160.py).
5. Vendor gRPC server starts on port 50052 via [vendor_server/src/vendor_server_160.cpp](../vendor_server/src/vendor_server_160.cpp).
6. Bank client encrypts + sends requests with [bank_client/bank_client.py](../bank_client/bank_client.py).
7. Vendor service computes HE inference and returns timing breakdown in [vendor_server/src/inference_service_160.cpp](../vendor_server/src/inference_service_160.cpp).
8. Scripts and tests collect metrics and artifacts in [tests/benchmark_comparison.py](../tests/benchmark_comparison.py), [scripts/generate_research_artifacts.py](../scripts/generate_research_artifacts.py), [scripts/generate_ablation.py](../scripts/generate_ablation.py), and [scripts/generate_roc.py](../scripts/generate_roc.py).

## 3) Main Components And Responsibilities

### Compiler And Artifacts

- [compiler/train_xgboost.py](../compiler/train_xgboost.py)
  - Reads CSV.
  - Splits train/test.
  - Applies scaler, winsorize, clip/normalize.
  - Expands with polynomial interactions and truncates to 256 features.
  - Trains XGBoost and saves arrays/models.

- [compiler/linearize.py](../compiler/linearize.py)
  - Loads 256-feature arrays.
  - Trains logistic regression.
  - Exports weights + bias.
  - Writes [artifacts/model_weights.bin](../artifacts/model_weights.bin) through [compiler/serialize_weights.py](../compiler/serialize_weights.py).

- [compiler/serialize_weights.py](../compiler/serialize_weights.py)
  - Binary layout (little-endian):
    - 4 bytes: uint32 n_features (=256)
    - 8 bytes: float64 bias
    - 256 * 8 bytes: float64 weights
  - Total = 2060 bytes.

- [compiler/gen_keys_160.py](../compiler/gen_keys_160.py)
  - Uses `seal_wrapper_160` to generate and persist HE keys:
    - [artifacts/public_key_160.bin](../artifacts/public_key_160.bin)
    - [artifacts/secret_key_160.bin](../artifacts/secret_key_160.bin)
    - [artifacts/galois_keys_160.bin](../artifacts/galois_keys_160.bin)

### Bank Side

- [bank_client/bank_client.py](../bank_client/bank_client.py)
  - Initializes SEAL wrapper with bank keys.
  - Loads bias from model binary header.
  - Pads request batches to 16x256 layout.
  - Encrypts plaintext vector to ciphertext.
  - Sends gRPC request to `RunInference`.
  - Decrypts response ciphertext.
  - Applies sigmoid: `expit(raw + bias)`.
  - Returns probabilities and timing dictionary.

### Vendor Side

- [vendor_server/src/ckks_context_160.cpp](../vendor_server/src/ckks_context_160.cpp)
  - Configures CKKS parameters for 160-bit chain with n=8192.
  - Generates keys and evaluator context.
  - Enforces parameter sanity checks.

- [vendor_server/src/inference_service_160.cpp](../vendor_server/src/inference_service_160.cpp)
  - gRPC handler for `RunInference`.
  - Validates ciphertext size and parameter compatibility.
  - Performs:
    - deserialize ciphertext
    - multiply by plaintext model weights
    - rescale
    - hoisted rotation tree sum
    - serialize output ciphertext
  - Populates timing fields in response.

- [vendor_server/src/vendor_server_160.cpp](../vendor_server/src/vendor_server_160.cpp)
  - Entry point binary.
  - Reads weight path and port (default 50052).
  - Starts server loop.

### Interface Contract

- [proto/inference.proto](../proto/inference.proto)
  - Request: ciphertext, request_id, institution_id, n_transactions.
  - Response: status, result_ciphertext, request_id, error_message, timing.
  - Timing fields:
    - `deserialization_us`
    - `multiply_plain_us`
    - `rotation_hoisting_us`
    - `serialization_us`
    - `total_inference_us`

## 4) Detailed Data Flow

### Stage A: Raw Data -> Processed 256 Features

Implemented in [compiler/train_xgboost.py](../compiler/train_xgboost.py).

1. Load [data/creditcard.csv](../data/creditcard.csv).
2. Remove target (`Class`) and optional `Time`.
3. Train/test split with stratification.
4. Scale features using `StandardScaler`.
5. Winsorize tails (`limits=[0.01, 0.01]`).
6. Clip to [-3, 3] and divide by 3.0.
7. Polynomial interactions (degree 2, interaction-only, no bias).
8. Truncate to first 256 columns for fixed HE input contract.
9. Save:
   - [artifacts/X_train.npy](../artifacts/X_train.npy)
   - [artifacts/X_test.npy](../artifacts/X_test.npy)
   - [artifacts/X_train_raw.npy](../artifacts/X_train_raw.npy)
   - [artifacts/X_test_raw.npy](../artifacts/X_test_raw.npy)
   - [artifacts/y_train.npy](../artifacts/y_train.npy)
   - [artifacts/y_test.npy](../artifacts/y_test.npy)
   - [artifacts/xgb_model.pkl](../artifacts/xgb_model.pkl)
   - [artifacts/scaler.pkl](../artifacts/scaler.pkl)
   - [artifacts/poly.pkl](../artifacts/poly.pkl)

### Stage B: 256 Features -> LR Weights Binary

Implemented in [compiler/linearize.py](../compiler/linearize.py).

1. Load [artifacts/X_train.npy](../artifacts/X_train.npy) and labels.
2. Train logistic regression on all 256 features.
3. Extract `weights` and `bias`.
4. Validate AUC from model predictions and direct logits path.
5. Serialize binary weights via [compiler/serialize_weights.py](../compiler/serialize_weights.py).
6. Save:
   - [artifacts/model_weights.bin](../artifacts/model_weights.bin)
   - [artifacts/weights.npy](../artifacts/weights.npy)

### Stage C: Key Material

Implemented in [compiler/gen_keys_160.py](../compiler/gen_keys_160.py).

- Generates and stores 160-bit context keys in [artifacts](../artifacts).

### Stage D: Online Inference Request Lifecycle

Client path in [bank_client/bank_client.py](../bank_client/bank_client.py), server path in [vendor_server/src/inference_service_160.cpp](../vendor_server/src/inference_service_160.cpp).

1. Bank client receives plaintext `X` (shape: n_txns x 256).
2. Client pads to 16 x 256 slots if needed.
3. Client encrypts packed vector.
4. Client sends `InferenceRequest` to vendor.
5. Vendor deserializes ciphertext.
6. Vendor multiplies by plaintext model weights.
7. Vendor performs hoisted rotation reduction.
8. Vendor serializes ciphertext result and timing.
9. Client decrypts response ciphertext.
10. Client applies sigmoid + bias and returns fraud probabilities.

## 5) Runtime Scripts And Their Data Dependencies

### Demo Script

- [scripts/demo_e2e.py](../scripts/demo_e2e.py)
  - Starts vendor server binary.
  - Waits for TCP readiness on port 50052.
  - Loads [artifacts/X_test.npy](../artifacts/X_test.npy).
  - Runs 5 encrypted inferences and prints score table.

### Benchmark Script

- [tests/benchmark_comparison.py](../tests/benchmark_comparison.py)
  - Launches baseline and reduced servers.
  - Warmups each client (`WARMUP_ROUNDS=20`).
  - Measures `MEASURE_ROUNDS=100`.
  - Computes gates and writes:
    - [artifacts/comparison_results.json](../artifacts/comparison_results.json)

### Research Artifact Script

- [scripts/generate_research_artifacts.py](../scripts/generate_research_artifacts.py)
  - Runs 1000 requests on reduced server.
  - Writes:
    - [results/latency_breakdown.csv](../results/latency_breakdown.csv)
    - [results/latency_summary.json](../results/latency_summary.json)

### Ablation Script

- [scripts/generate_ablation.py](../scripts/generate_ablation.py)
  - Measures rotation hoisting cost.
  - Can auto-start vendor server if missing.
  - Writes:
    - [results/ablation_hoisting.csv](../results/ablation_hoisting.csv)
    - [results/ablation_methodology.json](../results/ablation_methodology.json)

### ROC Script

- [scripts/generate_roc.py](../scripts/generate_roc.py)
  - Loads XGBoost model and LR weights.
  - Computes ROC curves and AUC.
  - Writes:
    - [results/roc_comparison.png](../results/roc_comparison.png)
    - [results/roc_comparison.pdf](../results/roc_comparison.pdf)

## 6) Validation And Contract Safety Nets

- [tests/verify_all.py](../tests/verify_all.py)
  - Checks proto contract shape and field indices.
  - Checks CKKS parameter invariants.
  - Checks model binary sizes (2060/4108).
  - Checks CMake wiring.

## 7) Practical Run Order

1. Build binaries.
2. Fetch dataset.
3. Run [compiler/train_xgboost.py](../compiler/train_xgboost.py).
4. Run [compiler/linearize.py](../compiler/linearize.py).
5. Run [compiler/gen_keys_160.py](../compiler/gen_keys_160.py).
6. Run [tests/verify_all.py](../tests/verify_all.py).
7. Run [scripts/demo_e2e.py](../scripts/demo_e2e.py).
8. Run benchmark and reporting scripts as needed.

## 8) Notes And Current Caveats

- Benchmark gates are hardware-calibrated; CPU governor should be `performance` for fair comparisons.
- [scripts/demo_e2e.py](../scripts/demo_e2e.py) now uses TCP readiness checks to avoid stdout-buffer startup stalls.
- [scripts/generate_roc.py](../scripts/generate_roc.py) currently reads [artifacts/feature_idx.npy](../artifacts/feature_idx.npy); ensure that artifact is present if running ROC generation in a fresh workspace.

---

If you want, this file can be split further into two docs:
- architecture-only
- operations/runbook-only
