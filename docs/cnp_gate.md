# CNP Day-1 gate (plaintext only)

Reproduce: `python scripts/cnp_gate.py --artifacts <dir with X_test.npy, y_test.npy, weights.npy, model_weights.bin, X_train.npy>` (about 8 min). Writes `artifacts/cnp_gate.json`.
Plaintext numpy only. Nothing here measures real HE timing or noise.

Baseline AUC (X@w+b, 56,962 test rows) = 0.979398 (expected ≈0.9794; matches).

## Verdicts (rule: layout asserts pass AND a worst-case-safe pair exists with |ΔAUC| ≤ 0.001)

| Pipeline | Layout asserts | Safe pair with \|ΔAUC\| ≤ 0.001 | Verdict |
|---|---|---|---|
| CNP-A (CKKS, Δx=Δw=2^40) | pass (N=8192/L=32 and N=65536/L=256, 20 trials each) | max\|err\| 6.4e-12, ΔAUC = 0 | **GO** (float sim only; see caveats) |
| CNP-B HERA (t=33292289) | pass | yes, 55 of 121 cells | **GO** |
| CNP-B Rubato-128L (t=33292289, σ=1.6357) | pass | yes, 55 of 121 cells | **GO** |

Fallbacks (two 128-feature blocks, mean-centring) were **not needed** and not run. The code path exists in the script (`--force-fallbacks`) but is untested.

## Sources of parameters (grep-verified)
- t_HERA = 0x1FC0001 = 33292289: `tools/transciphering/cipher/hera.go:63`; cross-check `third_party/RtF-Transciphering/ckks_fv/rtf_params.go:414,482`.
- t_Rubato = 0x1FC0001: `tools/transciphering/cipher/rubato.go:80`. **Same t as HERA**, so the mod-t sweep is identical for both. Rubato differs only by noise.
- σ = 1.6356633496…: `rubato.go:82`. `TestRubatoNoiseStatistics` (`rubato_test.go:138`) shows the noise is integer-valued with |d| ≤ int(6σ) = 9 (`rubato.go:345`). I model it as `round(N(0,σ))` clipped to ±9. This approximates, but is not, ckks_fv's discrete Gaussian sampler.

## Top 5 safe pairs (ranked by |ΔAUC|, then headroom). Worst-case ratio = bound/(t/2); headroom is empirical bits.

HERA
| log2Δx | log2Δw | worst-case/(t/2) | headroom bits | overflows | ΔAUC |
|---|---|---|---|---|---|
| 8 | 6 | 0.045 | 5.62 | 0 | −8.1e-6 |
| 10 | 4 | 0.045 | 5.61 | 0 | +9.2e-6 |
| 14 | 4 | 0.721 | 1.61 | 0 | +1.2e-5 |
| 12 | 4 | 0.180 | 3.61 | 0 | +1.3e-5 |
| 13 | 4 | 0.361 | 2.61 | 0 | +1.4e-5 |

Rubato (5 seeds; the noise bound ±9 is included in the worst-case bound)
| log2Δx | log2Δw | worst-case/(t/2) | headroom bits | overflows | ΔAUC mean | AUC std |
|---|---|---|---|---|---|---|
| 8 | 9 | 0.374 | 2.62 | 0 | −1.4e-6 | 3.0e-4 |
| 8 | 10 | 0.747 | 1.62 | 0 | +3.2e-6 | 3.1e-4 |
| 10 | 8 | 0.727 | 1.62 | 0 | −1.4e-5 | 7.7e-5 |
| 14 | 4 | 0.722 | 1.61 | 0 | +1.4e-5 | 4.5e-6 |
| 13 | 4 | 0.361 | 2.61 | 0 | +1.8e-5 | 8.9e-6 |

The top-5 ordering is within noise. All differences are around 1e-5, far below the Rubato seed std of 3e-4. Treat any pair with worst-case ratio < 1 and ΔAUC ≤ 1e-3 as equivalent. For the Rubato best pair, the worst single seed deviates 4.4e-4 from baseline.

## Chosen pair for both ciphers: (log2Δx, log2Δw) = (8, 6)

Read from `artifacts/cnp_gate.json` (cells with log2_dx=8, log2_dw=6); both are worst-case safe with |ΔAUC| ≤ 0.001.

| Cipher | worst-case bound / (t/2) | empirical max\|s\| | headroom bits | overflows | ΔAUC |
|---|---|---|---|---|---|
| HERA | 752,896 / 16,646,144 = 0.045 | 338,999 | 5.62 | 0 | −8.1e-6 |
| Rubato (5 seeds, includes ±9 noise bound) | 779,365 / 16,646,144 = 0.047 | 339,401 | 5.62 | 0 | +6.2e-5 (std 3.0e-4) |

Use this single pair for both ciphers. Rubato's (8,6) is absent from its top 5 only because that ranking is noise-level (see below).

## Precision bound
With |x_j| ≤ 1 (guaranteed by `compiler/train_xgboost.py:60-61`), xq = round(Δx·x), wq = round(Δw·w):

    safe  ⇔  Σ_j |wq_j| · (⌈Δx⌉ + B) ≤ t/2        B = 0 (HERA), 9 (Rubato, 6σ noise bound)

Approximately Δx·Δw·‖w‖₁ ≲ t/2 with ‖w‖₁ = 45.80, i.e. Δx·Δw ≲ 3.6e5 ≈ 2^18.5 (before Rubato's +9). Output is `center(Σ xq·wq mod t)/(Δx·Δw) + b`. The bank adds the bias in plaintext after decrypt. Sums are exact in float64 BLAS (asserted < 2^53).
The accuracy floor is about log2Δx, log2Δw ≥ 4. At (4,x) every cell has |ΔAUC| ≈ 1e-3 or more. Rubato at Δx=2^4 is −1.5e-3 or worse, because σ=1.6 against a 16-step grid is large.

## Mask check (B, N=8192, L=32, real quantised data)
- Adding a mod-t mask that is zero on 256k+255 leaves all 32 outputs unchanged (asserted).
- Non-output coefficients after masking: χ² = 76.1, 63 dof, p = 0.125 (uniform not rejected, 163,200 samples). Unmasked: χ² = 5.1e6, p ≈ 0.
- Unmasked leak: with a single-lane query (other lanes zero), the triangular solve recovers all 256 quantised weights exactly (asserted).

## Surprises / caveats
1. **No zero padding.** The audit refutes it: 0 constant and 0 all-zero columns. The 256 features are `[:256]` of the 435-feature degree-2 expansion (`train_xgboost.py:65-72`), so they are all live. |x| ≤ 1 holds (min −1, max 1, train and test).
2. **A bug in my own assertion, not in the layout.** The first wrap-around assert tripped. The cause was an off-by-one in my check, which treated the always-zero Kronecker padding slot N+255 as a wrap target. I replaced it with an exact check (slot N+255 is 0, nonzero wrap terms fold only onto indices 0..254, never 256k+255). The layout assertions themselves were not loosened.
3. **The leak needs adversarial or known lanes.** With a fully packed batch, lane-k output coefficients mix neighbouring lanes and (for lane 0) lane L−1 via the negacyclic wrap. The triangular form is exact only for an attacker-chosen single-lane query. The general case is a 256×256 Toeplitz solve, which I did not test. The mask is needed regardless.
4. **CNP-A is a weak test.** It is float64, with no CKKS noise, rescaling or modulus-chain budget. Δx·Δw = 2^80 is a depth/modulus question that this gate does not answer.
5. **Headroom is thin for Rubato.** The best pair has 2.6 bits of empirical headroom. Several of the top-5 pairs have worst-case ratios of 0.72 to 0.75.
6. 10 cells per cipher are worst-case-unsafe but showed zero overflow and passing AUC on this test set. Do not use them. They are not guaranteed for unseen inputs.
7. I read the inputs from the main checkout's `artifacts/` because they are gitignored and absent from this worktree. Nothing there was modified. I have not re-run the script a second time to confirm bit-identical output, though every RNG is seeded (`SEED=20260617`).
