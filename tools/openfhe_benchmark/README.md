# OpenFHE cross-library benchmark (Phase 4, §4.3)

This directory re-implements the PPFDaaS depth-1 linear (logistic-regression)
circuit — `ct_out.slot[k*256] = sum_j weights[k*256+j] * features[k*256+j]`
for 16 lanes — under **OpenFHE**, using `EvalFastRotationPrecompute` /
`EvalFastRotation` to perform **genuine Halevi-Shoup rotation hoisting**: the
key-switching digit decomposition is computed once per ciphertext and shared
across every rotation that follows.

This is "Strategy 3: Hoisted flat" in `docs/spec.md` §7.4, compared against
the two SEAL strategies in `vendor_server/src/rotation_hoisting.cpp`:

- **Sequential fold** (`hoisted_tree_sum`, §7.2): 8 dependent rotations, 8
  critical-path steps, rotation set `{1,2,4,8,16,32,64,128}`.
- **BSGS two-layer** (`bsgs_reduction`, §7.3): 32 independent rotations in 2
  critical-path layers, rotation set `BSGS_ROTATION_STEPS` (30 elements:
  `{1..15} ∪ {16,32,...,240}`).
- **Hoisted flat** (this directory, §7.4): the *same* 32-rotation BSGS
  rotation set and two-layer structure as `bsgs_reduction`, but each layer's
  15 rotations share one `EvalFastRotationPrecompute` — the amortization SEAL's
  public API does not expose (§7.1).

## Status in this checkout

**OpenFHE is not installed in this environment** (no `OpenFHEConfig.cmake`,
no pkg-config file, nothing under `/usr/local` or `/usr`). This directory is
a complete, compile-ready scaffold — `tools/openfhe_benchmark/CMakeLists.txt`
fails closed with a `FATAL_ERROR` and these same build instructions if
`find_package(OpenFHE)` does not succeed. `results/openfhe_results.json`
contains `"status": "PENDING"` placeholders because the benchmark has never
been built or run here.

This directory is **standalone**: it is never added as a subdirectory by the
root `CMakeLists.txt` or `vendor_server/CMakeLists.txt`, and is not part of
the deployed vendor server or its TCB (`docs/spec.md` §6.9).

## Building

1. Install OpenFHE >= v1.1.x from source:

   ```bash
   git clone https://github.com/openfheorg/openfhe-development.git
   cd openfhe-development && mkdir build && cd build
   cmake .. -DCMAKE_BUILD_TYPE=Release
   make -j$(nproc)
   sudo make install
   ```

2. Configure and build this directory directly (NOT via the repo root):

   ```bash
   cmake -S tools/openfhe_benchmark -B tools/openfhe_benchmark/build -DCMAKE_BUILD_TYPE=Release
   cmake --build tools/openfhe_benchmark/build -j$(nproc)
   ```

3. Run the benchmark from this directory (it writes a path relative to its
   working directory):

   ```bash
   cd tools/openfhe_benchmark
   ./build/openfhe_benchmark
   ```

   This performs 20 warmup + 100 measured end-to-end runs of the circuit,
   checks each run's decrypted result against a plaintext oracle (fixed seed
   42, same construction as `vendor_server/src/benchmark_160.cpp
   --strategy=bsgs`), and writes `results/openfhe_results.json` with
   `"status": "MEASURED"` and per-stage `mean/std/p50/p95/p99/min/max`
   latencies (encrypt, EvalMult, both `EvalFastRotationPrecompute` calls, both
   rotation layers' total `EvalFastRotation` time, decrypt, and end-to-end
   total). If the correctness gate fails, the binary exits non-zero and
   `"correctness_passed"` is `false` — such a run's timings must not be cited
   (Phase 5.5 pattern).

## Parameter equivalence: SEAL 160-bit vs OpenFHE

| Parameter | SEAL 160-bit (`eval_context_160.{h,cpp}`) | OpenFHE (`openfhe_linear_eval.cpp`, `build_context()`) | Notes |
|---|---|---|---|
| Scheme | CKKS | CKKS (`CryptoContextCKKSRNS`) | |
| Ring dimension N | 8192 (`set_poly_modulus_degree(8192)`) | requested via `SetRingDim(8192)` | OpenFHE's parameter generator may **override** the requested ring dimension based on `(SetMultiplicativeDepth, SetSecurityLevel, SetScalingModSize)` — if the built binary reports a different `GetRingDimension()`, that override is itself part of the cross-library finding (§7.5): it would mean OpenFHE's default parameter selection for an equivalent depth/security/scale combination does not fit in N=8192. |
| Coeff modulus chain | `{60, 40, 60}` bits, `CoeffModulus::Create(8192, {60,40,60})`, total 160 bits | not set explicitly; OpenFHE derives its RNS chain from `SetMultiplicativeDepth(1)` + `SetScalingModSize(40)` (one scaling prime per multiplicative level + auxiliary primes) | OpenFHE does not expose an explicit per-prime bit-size list at the `CCParams` level the way SEAL's `CoeffModulus::Create` does; depth + scaling-mod-size is the closest equivalent specification. |
| Scale | 2^40 (`scale = std::pow(2.0, 40)`) | `SetScalingModSize(40)` | Both target ~40-bit scaling factors. |
| Security level | `sec_level_type::tc128` (HomomorphicEncryption.org v1.1, 128-bit classical, ternary secret) | `HEStd_128_classic` | Same standard, same security target. |
| Multiplicative depth | 1 data level consumed (drop one 40-bit prime via `rescale_to_next_inplace`) | `SetMultiplicativeDepth(1)` | Equivalent: one `EvalMult` (ciphertext x plaintext) + automatic rescale. |
| Batch / slot count | 4096 slots (`poly_modulus_degree / 2`) | `SetBatchSize(4096)` | 16 lanes x 256 features = 4096. |
| Packing layout | 16 transactions x 256 features, lane-aligned at `slot[k*256]` | identical (`kLanes=16`, `kFeatures=256`) in `openfhe_linear_eval.h` | Same plaintext oracle construction as `benchmark_160.cpp --strategy=bsgs` (fixed seed 42). |
| Rotation set | `BSGS_ROTATION_STEPS` (30 elements, for the BSGS comparison) | `kBsgsRotationSteps` (identical 30 elements) | `EvalRotateKeyGen` is called with this full set in `build_context()`. |
| Rotation mechanism | `Evaluator::rotate_vector` (no shared precompute — each call is a full, independent rotation) | `EvalFastRotationPrecompute` (once per layer) + `EvalFastRotation` (per rotation, reuses the precompute) | This is the genuine-hoisting vs no-hoisting API gap that is the subject of this comparison (§7.1, §7.5). |
| Scaling technique | N/A (SEAL CKKS rescales explicitly via `rescale_to_next_inplace`) | `FLEXIBLEAUTO` (default) — `EvalMult` rescales automatically | Functionally equivalent for a single multiplicative level. |

## Files

- `CMakeLists.txt` — standalone CMake project; `FATAL_ERROR` with these build
  instructions if `find_package(OpenFHE)` fails.
- `openfhe_linear_eval.h` / `openfhe_linear_eval.cpp` — `build_context()` and
  `run_circuit_hoisted()`: the CKKS context, key generation, and the
  hoisted-BSGS circuit itself (encrypt -> EvalMult -> two hoisted rotation
  layers -> decrypt), plus the in-band parity gate against the plaintext
  oracle.
- `openfhe_benchmark.cpp` — 20 warmup + 100 measured timing harness, writes
  `results/openfhe_results.json`.
- `results/openfhe_results.json` — benchmark output. Currently `"status":
  "PENDING"` (never built/run in this environment); see
  `scripts/rotation_strategy_comparison.py` (Phase 4 §4.4) for how this feeds
  the unified strategy comparison table.
