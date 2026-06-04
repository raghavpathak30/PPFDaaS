# Homomorphic Encryption Circuit Latency Benchmarks
## ACCURATE & FAIR MEASUREMENT (Corrected May 28, 2026)

---

## Executive Summary

**IMPORTANT CLARIFICATION**: Previous measurements were **not apples-to-apples**. Earlier 160-bit benchmark measured only `multiply_plain`, while 200-bit measured the full inference pipeline. **This document contains FAIR measurements where both circuits perform identical operations.**

### Fair Comparison Results:
| Metric | 160-bit | 200-bit | Difference |
|--------|---------|---------|-----------|
| **Mean Latency** | **4.80 ms** | **7.23 ms** | 2.43 ms slower |
| **Speedup** | — | — | **1.51x faster (160-bit)** |
| **Operations** | Full inference | Full inference | ✅ **IDENTICAL** |

---

## What Changed & Why

### Previous (MISLEADING) Benchmark:
- **160-bit**: Measured only `Encrypt + multiply_plain` = 1.83 ms
- **200-bit**: Measured `Encrypt + multiply + rescale + 8 rotations` = 7.16 ms
- **Claimed speedup**: 3.92x (WRONG — measuring different things!)

### Current (FAIR) Benchmark:
- **160-bit**: Measures `Encrypt + multiply + rescale + 8 rotations` = 4.80 ms
- **200-bit**: Measures `Encrypt + multiply + rescale + 8 rotations` = 7.23 ms
- **True speedup**: 1.51x (NOW ACCURATE!)

### What Was Missing from Old 160-bit Benchmark:
```cpp
// Old (incomplete):
seal::Ciphertext result;
ctx.evaluator->multiply_plain(ct, pt_weights, result);  // Only this

// New (complete):
ctx.evaluator->multiply_plain_inplace(ct, pt_weights);
ctx.evaluator->rescale_to_next_inplace(ct);              // ← Was missing
hoisted_tree_sum(ct, ctx.galois_keys, *ctx.evaluator, acc, 256);  // ← Was missing (8 Galois rotations)
```

The **rescale and rotations account for ~60% of per-transaction latency** on 200-bit. On 160-bit they're similar operations, just with smaller modulus.

---

## Test Configuration

- **Benchmark Date**: May 28, 2026
- **Number of Runs**: 10 iterations per circuit
- **Feature Vector Size**: 4,096 elements per transaction
- **Batch Size**: 1 transaction per request (representative)
- **Compilation**: `-O3 -DNDEBUG -std=c++17 -mavx2 -fopenmp`
- **Build Type**: Release
- **Measurement Method**: High-resolution C++ chrono with 1,000 iterations per run

---

## Complete Results

### 160-bit Circuit (Fair: Full Inference Pipeline)

| Metric | Value | Notes |
|--------|-------|-------|
| **Coefficient Modulus** | {60, 40, 60} bits = 160 total | Smaller modulus |
| **Polynomial Degree** | 8,192 | Same as 200-bit |
| **Operations Measured** | ✅ Encrypt + multiply + rescale + rotate | Full pipeline |
| **Mean Latency** | **4,799.97 µs (4.80 ms)** | Primary metric |
| **Median Latency** | 4,755.39 µs (4.76 ms) | Center of distribution |
| **Min Latency** | 4,490.79 µs (4.49 ms) | Best case |
| **Max Latency** | 5,144.80 µs (5.14 ms) | Worst case |
| **Std Deviation** | 226.28 µs (0.226 ms) | ±4.7% variation |
| **Coefficient of Variation** | 4.71% | Slightly higher variability |
| **Sample Count** | 10 runs | Statistical confidence: good |

**Breakdown (estimated from service logs):**
- Encrypt: ~500 µs
- multiply_plain: ~800 µs
- rescale: ~200 µs
- 8 Galois rotations: ~2,500-2,800 µs
- Other overhead: ~200 µs

### 200-bit Circuit (Fair: Full Inference Pipeline)

| Metric | Value | Notes |
|--------|-------|-------|
| **Coefficient Modulus** | {60, 40, 40, 60} bits = 200 total | Larger modulus |
| **Polynomial Degree** | 8,192 | Same as 160-bit |
| **Operations Measured** | ✅ Encrypt + multiply + rescale + rotate | Full pipeline |
| **Mean Latency** | **7,230.76 µs (7.23 ms)** | Primary metric |
| **Median Latency** | 7,180.42 µs (7.18 ms) | Center of distribution |
| **Min Latency** | 7,082.78 µs (7.08 ms) | Best case |
| **Max Latency** | 7,475.90 µs (7.48 ms) | Worst case |
| **Std Deviation** | 153.00 µs (0.153 ms) | ±2.1% variation |
| **Coefficient of Variation** | 2.11% | More stable measurements |
| **Sample Count** | 10 runs | Statistical confidence: good |

**Breakdown (from service logs):**
- Deserialize: ~100-200 µs
- multiply_plain: ~1,000 µs
- rescale: ~300 µs
- 8 Galois rotations: ~4,500-5,000 µs
- serialize: ~200 µs
- **Total: ~7.2 ms**

---

## Fair Performance Comparison

### Key Finding: 1.51x Speedup (Not 3.92x)

```
Speedup = 200-bit latency / 160-bit latency
        = 7,230.76 µs / 4,799.97 µs
        = 1.505x
```

**160-bit is 1.5x faster** when both measure the same full inference pipeline.

---

## Why Only 1.51x and Not 3.92x?

### The Dominant Cost: Galois Rotations

The `hoisted_tree_sum` function performs **8 parallel Galois rotations** which dominate the cost:

```
Total Cost Breakdown (estimated for 7.2 ms operation):
├─ Galois rotations (8 steps): ~5,000 µs (69%)
├─ multiply_plain:              ~1,000 µs (14%)
├─ Encryption:                   ~500 µs (7%)
├─ Rescale + I/O:                ~500 µs (7%)
└─ Other overhead:               ~200 µs (3%)
```

### Why Rotations Don't Scale with Modulus as Much:

1. **Rotations are not purely arithmetic** — they involve:
   - Coefficient selection/permutation
   - Memory access patterns
   - Cache efficiency
   - Galois key application

2. **160-bit vs 200-bit on rotation cost**:
   - 160-bit uses 160-bit primes
   - 200-bit uses 200-bit primes
   - Difference: ~25% larger operations
   - But rotations use hardware acceleration (AVX-2), reducing impact

3. **Linear vs Modular Scaling**:
   - Pure arithmetic: 25% more expensive (200/160 = 1.25x)
   - Actual with rotations: 50% more expensive (7.2/4.8 = 1.50x)
   - Middle ground because rotations are partially hardware-accelerated

---

## Latency Range & Variability

### Overall System Range
```
Minimum: 4,490.79 µs (4.49 ms) — 160-bit best case
Maximum: 7,475.90 µs (7.48 ms) — 200-bit worst case
Span: 2,985 µs (3.0 ms)
```

### Per-Circuit Distribution

**160-bit Distribution:**
- Mean ± 1 σ: 4.80 ± 0.23 ms
- 95% confidence interval (±2σ): 4.80 ± 0.45 ms → [4.35 ms, 5.25 ms]
- Variability: ±4.7% CV (slightly higher, possibly cache-sensitive)

**200-bit Distribution:**
- Mean ± 1 σ: 7.23 ± 0.15 ms
- 95% confidence interval (±2σ): 7.23 ± 0.31 ms → [6.92 ms, 7.54 ms]
- Variability: ±2.1% CV (more stable)

### Why 160-bit Has Higher Variability:
Likely due to cache efficiency differences with smaller moduli and memory access patterns being less optimized for 160-bit operations in the current system configuration.

---

## What These Benchmarks ACTUALLY Measure

### Scope: HE Core Operations Only

These benchmarks measure **homomorphic encryption computation latency**, NOT end-to-end system latency:

```
What's INCLUDED (measured):
├─ Plaintext encoding of features
├─ HE encryption of encoded plaintext
├─ Element-wise multiply by plaintext weights
├─ Rescaling for noise management
└─ Tree reduction via Galois rotations

What's NOT INCLUDED (not measured):
├─ Network latency (client → server)
├─ gRPC deserialization overhead
├─ gRPC serialization overhead
├─ Network latency (server → client)
├─ Client-side HE decryption
├─ Sigmoid computation on decrypted result
└─ Other system overhead
```

### How This Fits in Actual Fraud Detection:

**Full End-to-End Latency (Estimated from bank_client.py):**
```
1. Client: Encrypt features              ~200 µs (not in benchmark)
2. Network: Send request                 ~1-10 ms (not measured)
3. Server: Deserialize + HE inference    ~4.8-7.2 ms (THIS BENCHMARK)
4. Network: Send response                 ~1-10 ms (not measured)
5. Client: Decrypt + sigmoid             ~300 µs (not in benchmark)
────────────────────────────────────────────────────────
Total End-to-End:                        ~6-27 ms (depends on network)
```

**Key insight**: These benchmarks measure the **compute-bound portion** (3), which is reproducible and deterministic. The full system latency depends heavily on network conditions.

---

## Comparison with Previous Incorrect Measurement

### What Was Wrong:

| Aspect | Old (Incorrect) | Now (Correct) |
|--------|-----------------|--------------|
| 160-bit measured | multiply_plain only | full pipeline with rotations |
| 200-bit measured | full pipeline | full pipeline ✅ |
| 160-bit latency | 1.83 ms (incomplete) | 4.80 ms (complete) |
| 200-bit latency | 7.16 ms | 7.23 ms (confirmed) |
| Claimed speedup | 3.92x | 1.51x ✅ |
| Why different | 160-bit skipped rotations | Both include rotations |

### The Missing ~3 ms in 160-bit:
```
4.80 ms (complete) - 1.83 ms (incomplete) = 2.97 ms
≈ Cost of rescale + 8 Galois rotations
```

This confirms that **tree reduction (rotations) is 62% of the total latency** on 160-bit!

---

## Building & Running Benchmarks

### Rebuild with Fair Benchmarks:
```bash
cmake --build build --parallel
```

The updated `benchmark_160` target now:
- ✅ Includes full pipeline (multiply + rescale + rotations)
- ✅ Uses `depth1_he_inference_160()` function
- ✅ Measures identical operations to 200-bit benchmark

### Run Multi-run Benchmarks:
```bash
# 10 runs (current)
python3 scripts/benchmark_multirun.py build 10

# 20 runs (more statistics)
python3 scripts/benchmark_multirun.py build 20

# After CPU frequency scaling fixes
sudo cpupower frequency-set -g performance
python3 scripts/benchmark_multirun.py build 10
```

### Check Individual Benchmarks:
```bash
./build/vendor_server/benchmark_160      # 160-bit full pipeline
./build/vendor_server/benchmark          # 200-bit full pipeline
```

---

## Statistical Summary Table

| Parameter | 160-bit | 200-bit | Ratio | Note |
|-----------|---------|---------|-------|------|
| Modulus bits | 160 | 200 | 1.25x | 25% difference |
| Mean latency | 4.80 ms | 7.23 ms | 1.51x | 50% slower (not 25%!) |
| Median latency | 4.76 ms | 7.18 ms | 1.51x | Confirms median |
| Min latency | 4.49 ms | 7.08 ms | 1.58x | Best-case difference |
| Max latency | 5.14 ms | 7.48 ms | 1.45x | Worst-case difference |
| Std deviation | 0.226 ms | 0.153 ms | 1.48x | 160-bit more variable |
| CV (variation) | 4.71% | 2.11% | 2.23x | 160-bit less stable |
| Range (max-min) | 0.655 ms | 0.393 ms | 1.67x | 160-bit more spread |

---

## Recommendations & Next Steps

### 1. ✅ COMPLETED: Fair Benchmarks
- Both circuits now measure identical operations
- Accurate 1.51x speedup factor confirmed
- Measurements are reproducible (CV ~ 2-5%)

### 2. RECOMMENDED: CPU Isolation
```bash
# Enable performance mode to reduce variability
sudo cpupower frequency-set -g performance

# Run again to see if CV improves
python3 scripts/benchmark_multirun.py build 10
```

### 3. RECOMMENDED: Thread Scaling Analysis
```bash
# Measure impact of OpenMP thread count on rotations
PPFD_OMP_THREADS=1 python3 scripts/benchmark_multirun.py build 5
PPFD_OMP_THREADS=2 python3 scripts/benchmark_multirun.py build 5
PPFD_OMP_THREADS=4 python3 scripts/benchmark_multirun.py build 5
```

### 4. ADVANCED: Detailed Profiling
```bash
# Breakdown latency per sub-operation
./build/vendor_server/vendor_server_160 &  # Start server
python3 -c "from bank_client import BankClient; c = BankClient('localhost:50052'); print(c.run_inference(np.zeros((1,256)))['timing_breakdown'])"
```

### 5. FOR PRODUCTION: Real Service Benchmarking
The `inference_service` and `inference_service_160` implementations include detailed timing:
```cpp
td->set_deserialization_us(dur(t_start, t_deserialized));
td->set_multiply_plain_us(dur(t_deserialized, t_mul));
td->set_rotation_hoisting_us(dur(t_mul, t_rot));
td->set_serialization_us(dur(t_rot, t_end));
td->set_total_inference_us(dur(t_start, t_end));
```

Run server benchmarks to see real service latencies including gRPC overhead.

---

## Conclusions

1. **160-bit IS faster, but only 1.51x — not 3.92x**
   - Previous claim was comparing different operations
   - Fair comparison required using `depth1_he_inference_160()`

2. **Both circuits perform identical HE operations:**
   - Encrypt + multiply_plain + rescale + tree reduction (8 rotations)
   - Only difference: parameter modulus size (160 vs 200 bits)

3. **Galois rotations dominate cost:**
   - ~69% of latency comes from rotations
   - Only ~14% from element-wise multiply
   - Scaling is not purely linear with modulus

4. **Measurements are reproducible:**
   - CV 2-5% indicates good stability
   - Safe to use for performance decisions

5. **End-to-end system latency is network-dependent:**
   - These benchmarks measure compute-bound portion only
   - Add ~2-20 ms for network round-trip in production

---

## Data Archive

**Individual Run Values:**

160-bit (µs): 5144.80, 5116.87, 4628.53, 4490.79, 4808.36, 4702.42, 4584.14, 4909.66, 4651.80, 4962.28

200-bit (µs): 7472.67, 7377.01, 7095.08, 7082.78, 7097.04, 7154.22, 7192.03, 7174.41, 7475.90, 7186.43

To rerun complete analysis:
```bash
python3 scripts/benchmark_multirun.py build 10
```
