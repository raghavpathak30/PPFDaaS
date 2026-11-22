# Slot vs CNP-B, Rubato, LogN 16 (unpaired)

> **NOT A PAIRED COMPARISON.** The slot run (22 Nov 2026) and the CNP-B runs (28 Oct 2026) used
> different images, different repo SHAs, different dates and different host load. The paired
> comparison is round 2: CNP-B rerun in the same session as the slot run
> (`docs/DGX_RUNBOOK_slot.md:102-104`, `run_slot_logn16.sh:7`). Ratios below are indicative only.
> Section 3 shows that the session effect is about 20% on identical code.

## Sources

| Label | Path | Run |
|---|---|---|
| **S** | `~/dgx_results/slot_rubato_s1/slot_rubato.json` | slot, socket 1 (cpus 20-39, NUMA node 1) |
| **S-score** | `~/dgx_results/slot_rubato_s1/slot_rubato_score.json` | written 22 Nov by `scripts/cnp_b/score.py` (see 1) |
| **C0** | `~/dgx_results/cnpb-rubato-s0/cnp_rubato.json` | CNP-B, socket 0 (cpus 0-19, NUMA node 0) |
| **C1** | `~/dgx_results/cnpb-rubato-s1/cnp_rubato.json` | CNP-B, socket 1 (cpus 20-39, NUMA node 1), the same socket as S |
| **C0-score**, **C1-score** | `~/dgx_results/cnpb-rubato-s{0,1}/cnp_rubato_score.json` | October scores |
| **M-S**, **M-C0**, **M-C1** | `machine_state_start.json` in each of those directories | host state |

| | S | C0 | C1 |
|---|---|---|---|
| Date (`date_utc`) | 2026-11-22T13:18:53Z | 2026-10-28T21:14:02Z | 2026-10-28T19:37:22Z |
| Repo SHA (`provenance.repo_sha`) | `b34b4d2` | `d7ea62b` | `d7ea62b` |
| Image (`provenance.image_ref`) | `ppfdaas-transcipher-bench:b34b4d2` | `ppfdaas-transcipher-bench:cnpb` | `ppfdaas-transcipher-bench:cnpb` |
| RtF patch (`provenance.rtf_patch_sha256`) | `e7aa4d97…` | `e7aa4d97…` | `e7aa4d97…` |
| ckks_fv (`provenance.rtf_sha`) | `105fc731…` | `105fc731…` | `105fc731…` |
| Param set (`param_state.param_set` / `param_set`) | 128af | 128af | 128af |
| Host load at start (`host_loadavg_1_5_15`, M-*) | 3.37 / 5.79 / 6.98 | 12.10 / 11.30 / 11.66 | 19.16 / 17.61 / 16.05 |
| Rows (`correctness.rows`) | 56,962 | 56,962 | 56,962 |

## 1. Accuracy

Plaintext baseline ROC AUC is 0.9793983927 in all three score files (`roc_auc_plaintext_float`).

| | ΔROC AUC (`delta_roc_auc`) | Mean abs logit error (`mean_abs_logit_error`) | Max abs logit error (`max_abs_logit_error`) | Rows ≠ integer sim (`rows_differing_from_integer_simulation`) |
|---|---|---|---|---|
| Slot (S-score) | **−1.857e-5** | 0.02389 | 0.1295 | 56,916 |
| CNP-B s0 (C0-score) | +4.836e-5 | 0.02380 | 0.1461 | 56,903 |
| CNP-B s1 (C1-score) | −6.272e-5 | 0.02378 | 0.1360 | 56,921 |

The slot ΔAUC lies between the two CNP-B values. All three are below 1e-4.

The slot run was scored with
`python3 scripts/cnp_b/score.py --data ~/cnpb_data --results ~/dgx_results/slot_rubato_s1 --cipher rubato --arm slot`.
`~/cnpb_data` was regenerated on 2026-11-04, so it is not the copy at `/data` on the host. The score
reproduces the Go-side checks in S exactly, which shows that `expected.i64` and `plain_logit.f64` match what the host used:

| | Go side (S `correctness.*`) | score.py (S-score) |
|---|---|---|
| Rows ≠ integer sim | `rows_not_equal_to_integer_simulation` = 56,916 | 56,916 |
| Max abs integer diff | `max_abs_integer_diff` = 2,025 | 2,025 |
| Mean abs logit error | `mean_abs_logit_error_vs_float_plaintext` = 0.023893259482012503 | 0.023893259482012635 |
| Max abs logit error | `max_abs_logit_error_vs_float_plaintext` = 0.12953016762014968 | 0.12953016762014968 |

## 2. Why 99.9% of rows differ from the integer simulation

**This is the expected effect of Rubato's added noise. It is not a defect.**

**Mechanism.** Rubato adds Gaussian noise to the keystream on the client (`RtF_bench_test.go:658-660`).
It is sampled by `rubatoAddGaussianNoise` with `bound := int(6 * sigma)` = 9 (`:766-769`) and σ = 1.6357 (`tools/transciphering/cipher/rubato.go:82`).
The server evaluates the keystream without that noise and subtracts it, so each decrypted feature is
xq_i + e_i with |e_i| ≤ 9. The dot product then gives the bank Σ wq_i·(xq_i + e_i), and the gap from
the integer simulation is d = Σ wq_i·e_i. The integer simulation (`expected.i64`, `scripts/cnp_b/prepare_inputs.py`) has no
noise term, so it can match only when d happens to be exactly 0. The slot harness builds the client
keystream with the same `plainRubato(..., RubatoParams[..].Sigma)` call as CNP-B (`rtf_cnp_probe_test.go:42`, through `cnpCtx`).

**Predicted vs observed.** The sampler is `ring.GaussianSampler.AGN`
(`third_party/RtF-Transciphering/ring/ring_sampler_gaussian.go:105-119`). It rounds |N(0,1)|·σ to the
nearest integer and rejects values above 9, which gives Var(e) = 2.7587. With ‖wq‖₂ = 277.30
(`~/cnpb_data/wq.i16`; 246 of 256 weights nonzero, gcd 1), the prediction is SD(d) = √(2.7587)·277.30 = 460.6.
The predicted count of rows with d = 0 is n/(√(2π)·SD) = 49.3 of 56,962.

| | Mean d | SD d | Mean \|d\| | Rows with d = 0 | Max \|d\| / SD |
|---|---|---|---|---|---|
| **Predicted** | 0 (SE 1.93) | 460.6 (SE 1.4) | 367.5 | 49.3 | ~4.5 to 4.7 expected for n = 56,962 |
| Slot (S, `slot_rubato_dec.i64`) | 0.68 | 460.7 | 368.4 | 46 | 4.46 |
| CNP-B s0 (C0, `cnp_rubato_dec.i64`) | −2.24 | 459.0 | 366.4 | 59 | 4.92 |
| CNP-B s1 (C1, `cnp_rubato_dec.i64`) | 3.96 | 458.4 | 366.1 | 41 | 4.50 |

d = decrypted integer − `expected.i64` on the same rows. I computed it from the `.i64`/`.i32` files in
each run directory; these columns are not JSON fields. The row counts agree with the JSON fields
(`correctness.rows_not_equal_to_integer_simulation`: 56,916 / 56,903 / 56,921).

The slot SD matches the prediction to within 0.1. Its mean is 0.35 SE from zero, and its zero count
is within one Poisson SD of 49.3. A slot-path defect would show up as a nonzero mean, an SD above 460.6, or
errors near t/2 from wrap-around. None of these appear, and both arms have the same error distribution.
The control is HERA, which adds no noise: both CNP-B HERA runs are bit-exact
(`~/dgx_results/score-hera-s{0,1}.txt`, `rows_differing_from_integer_simulation` = 0).

Noise headroom also passed (S `headroom_gate`: min 25.28 bits ≥ 5; C0 21.77 bits, per C0 `headroom_gate`).

## 3. Cost per transaction (ms, unpaired)

**Derivation.** S records `per_transaction_ms.{offline,online,total}` directly
(= `per_crypt_batch_ms.*` / `lanes_per_crypt` 15,360).

C0 and C1 record a per-transaction figure **only for online**, and `per_transaction_ms.total` there is
the online total. Offline and total are derived here with the repo's own formula
(`scripts/bfv_slot_baseline/run_laptop_compare.py:194,211-212`):
offline per output ct = `cnp_stages."keystream_crypt (offline)".median_ms` / `outputs_per_crypt` (60) +
`cnp_stages."stc_modswitch (offline)".median_ms`, then ÷ `lanes_per_output_ct` (256).
Online = `per_output_ct_ms.total` / 256.

| ms per transaction | Slot (S) | CNP-B s0 (C0) | CNP-B s1 (C1) |
|---|---|---|---|
| Offline | **7.9919** (`per_transaction_ms.offline`) | 25.1473 (derived: (149,804.095 / 60 + 3,940.973) / 256) | 25.6276 (derived: (154,379.386 / 60 + 3,987.670) / 256) |
| Online | 0.10750 (`per_transaction_ms.online`) | **0.06104** (`per_transaction_ms.total`) | **0.06809** (`per_transaction_ms.total`) |
| Total | **8.0994** (`per_transaction_ms.total`) | 25.2083 (derived: offline + online) | 25.6957 (derived) |

**Offline components (ms per transaction):**

| | Slot (S) | CNP-B s0 (C0) | CNP-B s1 (C1) |
|---|---|---|---|
| Crypt | 7.9105 (`slot_stages_per_crypt_batch."keystream_crypt (offline)".median_ms` 121,505.679 / 15,360) | 9.7529 (149,804.095 / 15,360) | 10.0507 (154,379.386 / 15,360) |
| StC (CNP-B) / modswitch (slot) | 0.0814 (`slot_stages_per_crypt_batch."keystream_modswitch (offline, all usable cts)".median_ms` 1,250.597 / 15,360) | 15.3944 (`cnp_stages."stc_modswitch (offline)".median_ms` 3,940.973 / 256) | 15.5768 (3,987.670 / 256) |

**Session effect.** Crypt is the same code in both arms (`offline_note` in S and C0/C1). It ran
19% (C0) and 21% (C1) faster tonight, when the 1-minute host load was 3.6x (C0) and 5.7x (C1) lower. Every cross-run ratio carries an
error of about that size. The conclusions that survive it:

- **Offline and total favour slot.** CNP-B's StC alone, 15.4–15.6 ms, is about twice slot's entire
  offline cost of 7.99 ms. A 20% session correction cannot close that gap.
- **Online favours CNP-B** by 1.58x (C1, same socket) to 1.76x (C0) here. That margin is within reach of the
  session effect, so it needs round 2 to confirm.
- Online is 1.3% of slot's total and 0.24–0.26% of CNP-B's total.

## 4. Setup and key material

Retained heap is `setup_retained_heap_kb.<stage>` in kB, and setup time is `setup_ms.<stage>`.

| Stage | Slot (S) ms | Slot (S) kB | CNP-B s0 (C0) ms | CNP-B s0 (C0) kB | CNP-B s1 (C1) ms | CNP-B s1 (C1) kB |
|---|---|---|---|---|---|---|
| `keygen` | 1,897.542 | 1,400,763 | 2,191.094 | 1,400,762 | 2,169.135 | 1,400,747 |
| `relinearization_key` | 945.003 | 344,086 | 1,126.911 | 344,081 | 1,113.410 | 344,081 |
| `fv_evaluator` | 806.388 | 1,770,127 | 1,013.574 | 1,791,116 | 965.987 | 1,791,121 |
| `encode_weights_ptmul_signed_lift` | 569.416 | 61,449 | 3.830 | 2,051 | 5.309 | 2,048 |
| Rotation keys: `slot_rotation_keys` (S) / `stc_rotation_keys` (C) | **5,373.760** | **1,032,249** | **51,583.614** | **8,085,916** | **50,954.737** | **8,085,927** |
| `stc_matrices` (CNP-B only) | none | none | 91,072.908 | 18,857,055 | 92,974.706 | 18,857,055 |

**Rotation keys:**

| | Slot (S) | CNP-B s0 / s1 (C0 / C1) |
|---|---|---|
| Count | 6 (`rotation_key_counts.slot_rotations`; `rotation_key_indexes` = 1, 2, 4, 8, 16, 32) | 47 (`rotation_key_counts.stc_only`) |
| Bytes | 1,056,964,608 allocated (`rotation_key_bytes_allocated`); 31,457,280 touched at level 0 (`rotation_key_bytes_touched_at_level0_computed`) | Not recorded as bytes. Retained heap 8,085,916 / 8,085,927 kB (above) |
| Rotations per batch | 18 (`rotations_per_crypt`) | 0 online. StC rotations are inside `stc_modswitch` (offline) |

C0 and C1 also record `rotation_key_counts.baseline_halfboot_extra` = 17 and `setup_*.baseline_halfboot_*`.
Those belong to the HalfBoot lower-bound arm in the same run, not to CNP-B, so they are left out above.
Peak RSS (`vm_hwm_kb_end`: S 19,166,544 kB; C0 64,782,040; C1 65,060,944) covers that HalfBoot arm too.
It is therefore not a CNP-B-only figure.

**Wire bytes per transaction:**

| | Slot (S) | CNP-B (C0, C1) |
|---|---|---|
| Upload, bit-packed (`wire_bytes.upload_bytes_per_transaction_bitpacked`) | 800 | 800 |
| Download, 60-bit packed (`wire_bytes.download_bytes_per_transaction_60bit_packed`) | 256 | 3,840 |

## Not settled here

- Any paired ratio. Round 2 gives that.
- Whether the remaining 1.58–1.76x online gap survives same-session timing.
- Partial-batch (unamortized) cost at LogN 16. Neither arm's per-output-ct figure is compared here.
