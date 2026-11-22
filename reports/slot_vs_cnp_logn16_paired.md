# Slot vs CNP-B, HERA and Rubato, LogN 16 (paired)

All four jobs ran on 22 Nov 2026 on `lnmdgx1` from one image, at LogN 16, param set 128af, over
the full 56,962-row test set. Within each cipher, the slot and CNP-B jobs ran on the same socket:
HERA on socket 0 and Rubato on socket 1. This is the round-2 paired run that
`reports/slot_vs_cnp_rubato_logn16.md` deferred to.

## Sources

Paths are under `~/dgx_results/results/` (also in `~/dgx_results/slot_cnp_results_20261122.tar.gz`).

| Label | JSON | Score JSON (written by `scripts/cnp_b/score.py`, see 3) |
|---|---|---|
| **SH** slot, HERA, s0 | `slot_hera_s0/slot_hera.json` | `slot_hera_s0/slot_hera_score.json` |
| **CH** CNP-B, HERA, s0 | `cnp_hera_s0/cnp_hera.json` | `cnp_hera_s0/cnp_hera_score.json` |
| **SR** slot, Rubato, s1 | `slot_rubato_s1/slot_rubato.json` | `slot_rubato_s1/slot_rubato_score.json` |
| **CR** CNP-B, Rubato, s1 | `cnp_rubato_s1/cnp_rubato.json` | `cnp_rubato_s1/cnp_rubato_score.json` |

`~/dgx_results/slot_rubato_s1/` (top level) is a byte-identical copy of `results/slot_rubato_s1/`.
All six of its run files compare equal (`cmp`), and its existing score file is identical to the one
regenerated here.

| | SH | CH | SR | CR |
|---|---|---|---|---|
| Start, UTC (`date_utc`) | 13:18:50Z | 14:25:01Z | 13:18:53Z | 14:25:04Z |
| End, UTC (`machine_state_end.json` `timestamp_utc`) | 14:18:41Z | 15:49:42Z | 13:41:33Z | 15:04:54Z |
| Repo SHA (`provenance.repo_sha`) | `b34b4d2` | `b34b4d2` | `b34b4d2` | `b34b4d2` |
| Image (`provenance.image_ref`) | `ppfdaas-transcipher-bench:b34b4d2` | same | same | same |
| ckks_fv / RtF patch (`provenance.rtf_sha` / `rtf_patch_sha256`) | `105fc731…` / `e7aa4d97…` | same | same | same |
| Pinned CPUs (`machine_state_start.json` `cpu_mhz_pinned_cpus` keys) | 0-19 | 0-19 | 20-39 | 20-39 |
| Host load 1/5/15 at start (`host_loadavg_start`) | 3.37 5.79 6.98 | 6.72 11.28 7.62 | 3.37 5.79 6.98 | 6.72 11.28 7.62 |
| Host load 1/5/15 at end (`host_loadavg_end`) | 4.70 3.56 3.36 | 13.66 13.66 16.35 | 3.83 4.07 4.76 | 4.33 4.28 4.74 |
| `exit_code` file / test result (`container.log`) | 0 / PASS (3,590 s) | 0 / PASS (5,075 s) | 0 / PASS (1,359 s) | 0 / PASS (2,386 s) |
| Headroom gate (`headroom_gate`, threshold `headroom_gate_bits` = 5) | passed, 26.97 bits | passed, 21.77 bits | passed, 25.28 bits | passed, 21.81 bits |
| Rows (`correctness.rows`) | 56,962 | 56,962 | 56,962 | 56,962 |

**The pairing is same night, same image and same socket, but not concurrent.** The two slot jobs
ran first, at 13:18–14:18 UTC. The two CNP-B jobs ran after them, at 14:25–15:49 UTC, under higher
host load. That makes the drift check in 1 the measure of the session effect.

## 1. Cost per transaction (ms)

**Slot.** The figures are measured. SH and SR record `per_transaction_ms.{offline,online,total}`,
which is `per_crypt_batch_ms.*` / `lanes_per_crypt` (SH 4,096, SR 15,360). Offline is
`keystream_crypt` + `keystream_modswitch`, and the slot path has no SlotToCoeff (`offline_note`).

**CNP-B.** Offline and total are derived. CH and CR record per-transaction figures only for the
online stages (`per_transaction_ms.{mask,pmul,scaleup_sub,total}`). Their `total` is not a
whole-pipeline figure. It is also not literally the sum of the stage medians: it is the median of a
separately timed `online_total` (`rtf_cnp_test.go:671`). That differs from the sum of stage medians
by 0.7% (CH: 14.356 vs 14.453 ms per output ct) and 0.3% (CR: 17.543 vs 17.589).

Offline and total use the repo's formula (`scripts/bfv_slot_baseline/run_laptop_compare.py:194,211-212`):

```
offline_per_ct  = cnp_stages."keystream_crypt (offline)".median_ms / outputs_per_crypt
                + cnp_stages."stc_modswitch (offline)".median_ms
offline_per_txn = offline_per_ct / lanes_per_output_ct                       (256)
online_per_txn  = per_output_ct_ms.total / lanes_per_output_ct
total_per_txn   = (offline_per_ct + per_output_ct_ms.total) / lanes_per_output_ct
```

| ms per transaction | SH slot HERA | CH CNP-B HERA | SR slot Rubato | CR CNP-B Rubato |
|---|---|---|---|---|
| Offline | **46.7127** (`per_transaction_ms.offline`) | 59.1879, derived: (194,462.646 / 16 + 2,998.183) / 256 = 15,152.098 / 256 | **7.9919** (`per_transaction_ms.offline`) | 19.9319, derived: (124,636.631 / 60 + 3,025.282) / 256 = 5,102.559 / 256 |
| Online | 0.09637 (`per_transaction_ms.online`) | **0.05608** (`per_transaction_ms.total`; = 14.356 / 256) | 0.10750 (`per_transaction_ms.online`) | **0.06853** (`per_transaction_ms.total`; = 17.543 / 256) |
| Total | **46.8091** (`per_transaction_ms.total`) | 59.2440, derived: (15,152.098 + 14.356) / 256 | **8.0994** (`per_transaction_ms.total`) | 20.0004, derived: (5,102.559 + 17.543) / 256 |

The CNP-B inputs are `cnp_stages."keystream_crypt (offline)".median_ms` (CH 194,462.646, CR
124,636.631), `outputs_per_crypt` (CH 16, CR 60), `cnp_stages."stc_modswitch (offline)".median_ms`
(CH 2,998.183, CR 3,025.282), `lanes_per_output_ct` (256) and `per_output_ct_ms.total` (CH 14.356,
CR 17.543).

**Ratios, CNP-B / slot (derived from the table; > 1 means slot is cheaper):**

| | HERA (CH / SH) | Rubato (CR / SR) |
|---|---|---|
| Online | 0.582 (CNP-B 1.72x faster) | 0.637 (CNP-B 1.57x faster) |
| Offline | 1.267 | 2.494 |
| Total | 1.266 | 2.469 |

**Offline components (ms per transaction, derived):**

| | SH | CH | SR | CR |
|---|---|---|---|---|
| Crypt | 46.6414 (`slot_stages_per_crypt_batch."keystream_crypt (offline)".median_ms` 191,043.273 / 4,096) | 47.4762 (194,462.646 / (16 × 256)) | 7.9105 (121,505.679 / 15,360) | 8.1144 (124,636.631 / (60 × 256)) |
| StC (CNP-B) / modswitch (slot) | 0.0713 (`slot_stages_per_crypt_batch."keystream_modswitch (offline, all usable cts)".median_ms` 291.876 / 4,096) | 11.7117 (2,998.183 / 256) | 0.0814 (1,250.597 / 15,360) | 11.8175 (3,025.282 / 256) |

**Drift control.** Crypt is the same call in both arms. Both harnesses call `cnpCrypt`
(`rtf_cnp_probe_test.go:50`; slot at `rtf_slot_test.go:560`, CNP-B at `rtf_cnp_test.go:553`) with
the same `cnpConfig` and `modDown` (`rtf_slot_test.go:111`, `rtf_cnp_setup_test.go:83,91`). Its
median ran 1.8% slower in CH than in SH (194,462.646 / 191,043.273 = 1.0179). It ran 2.6% slower in
CR than in SR (124,636.631 / 121,505.679 = 1.0258), consistent with the CNP-B jobs' higher host
load. That is about a tenth of the ~20% session effect in the unpaired report.

**Rep spread.** Each stage has 5 reps (`reps`). The ranges below are derived by combining per-stage
min and max across reps (`*.runs_ms`), so they are wider than any single rep.

| ms per transaction | SH | CH | SR | CR |
|---|---|---|---|---|
| Offline, min to max | 46.24–47.19 | 57.29–63.00 | 7.88–8.06 | 19.47–24.08 |
| Online, min to max | 0.0941–0.1053 | 0.0551–0.0593 | 0.0962–0.1205 | 0.0634–0.0704 |

For each cipher the two arms' ranges do not overlap, either offline or online.

**What drives each gap.** For both ciphers, the offline gap is StC: about 11.7–11.8 ms per
transaction, against 0.07–0.08 ms for slot's modswitch. StC costs about the same per output
ciphertext for both ciphers (3.0 s), because it does not depend on the cipher. It matters much more
for Rubato, where Crypt is only about 8 ms per transaction, than for HERA, where Crypt is about
47 ms and dominates both arms. Online is 0.2% of slot's total for HERA and 1.3% for Rubato, and 0.1%
and 0.3% of CNP-B's.

## 2. Setup and key material

Time is `setup_ms.<stage>` and retained heap is `setup_retained_heap_kb.<stage>` (kB). CH and CR also
contain `setup_*.baseline_halfboot_*` stages and `rotation_key_counts.baseline_halfboot_extra` = 17.
Those belong to the HalfBoot lower-bound arm in the same binary (`arms` = "cnp baseline"), so they
are left out of everything below.

| Stage | SH ms / kB | CH ms / kB | SR ms / kB | CR ms / kB |
|---|---|---|---|---|
| `keygen` | 1,905.509 / 1,400,762 | 2,121.255 / 1,400,762 | 1,897.542 / 1,400,763 | 2,039.321 / 1,400,762 |
| `relinearization_key` | 911.574 / 344,081 | 1,035.362 / 344,081 | 945.003 / 344,086 | 1,038.023 / 344,081 |
| `fv_evaluator` | 862.538 / 1,769,103 | 919.621 / 1,791,116 | 806.388 / 1,770,127 | 940.803 / 1,791,116 |
| `encode_weights_ptmul_signed_lift` | 132.284 / 16,383 | 4.449 / 2,048 | 569.416 / 61,449 | 3.780 / 2,048 |
| Rotation keys: `slot_rotation_keys` (slot) / `stc_rotation_keys` (CNP-B) | 3,782.001 / 688,173 | **46,461.685 / 8,085,911** | 5,373.760 / 1,032,249 | **46,013.548 / 8,085,911** |
| `stc_matrices` (CNP-B only) | none | **86,284.141 / 18,857,080** | none | **82,074.241 / 18,857,080** |
| Setup total, ms (derived: sum of the rows above) | 7,593.906 | 136,826.513 | 9,592.109 | 132,109.716 |

For CNP-B, StC matrices plus StC rotation keys are 97.0% of setup time in both CH and CR (derived).

**Rotation keys:**

| | SH | CH | SR | CR |
|---|---|---|---|---|
| Count | 4 (`rotation_key_counts.slot_rotations`; `rotation_key_indexes` 1, 2, 4, 8) | 47 (`rotation_key_counts.stc_only`) | 6 (`rotation_key_counts.slot_rotations`; indexes 1–32) | 47 (`rotation_key_counts.stc_only`) |
| Bytes | 704,643,072 allocated (`rotation_key_bytes_allocated`) | not recorded as bytes; ≈ 8,279,556,096 derived | 1,056,964,608 allocated (`rotation_key_bytes_allocated`) | ≈ 8,279,556,096 derived |
| Used | online, in `rotate_and_sum` (`rtf_slot_test.go:371`), 4 rotations per batch (`rotations_per_crypt`) | offline, inside `stc_modswitch` (`SlotsToCoeffs`, `rtf_cnp_probe_test.go:71`) | online, 18 rotations per batch (`rotations_per_crypt`) | offline, as CH |

The CNP-B byte figure is derived. Both slot runs give 176,160,768 bytes per key
(704,643,072 / 4 = 1,056,964,608 / 6). Then 47 × 176,160,768 = 8,279,556,096 bytes = 8,085,504 kB,
which is within 0.005% of the measured retained heap of 8,085,911 kB. The slot retained heap matches
its allocated bytes the same way (SH 688,173 kB = 704,689,152 bytes).

**Serving time vs one-off.**

- **One-off:** the setup times above. That includes generating the StC matrices (82–86 s) and the
  47 StC rotation keys (46 s), and for slot the 4 or 6 rotation keys (3.8 s / 5.4 s).
- **Resident at serving time:** what the server must hold to keep producing outputs. The offline
  phase repeats for every new batch of nonces, so anything it uses counts as serving-time material,
  even though it is not on the online path.

| Server-resident at serving time | Slot | CNP-B |
|---|---|---|
| Relinearization key (offline Crypt; passed to the evaluator at `rtf_slot_test.go:132`, `rtf_cnp_setup_test.go:131`) | yes | yes |
| Rotation keys | 4 or 6 slot keys, used **online** | 47 StC keys, used **offline**, for every output ct |
| StC matrices (`pDcds`, built into the evaluator at `rtf_cnp_setup_test.go:131`) | none (`nil`, `rtf_slot_test.go:132`) | 18,857,080 kB, used **offline**, for every output ct |
| Keys or matrices used online | slot rotation keys | none: the online stages are scale-up+Sub, a plaintext multiply and a mask add (`rtf_cnp_test.go:662-665`) |
| Sum of retained heap for relin + rotation keys + StC matrices + encoded weights (derived) | SH 1,048,637 kB; SR 1,437,784 kB | CH and CR 27,289,120 kB each |

The FV evaluator (`fv_evaluator`, about 1.77–1.79 GB) is resident in every arm and is excluded from
that sum. Rotation keys need the secret key, so in a deployment the bank would generate them and
upload them once. That one-off upload is about 0.70 GB (SH) or 1.06 GB (SR) for slot, against about
8.28 GB for CNP-B (derived from the byte figures above). The StC matrices need no secret key, and the
harness builds them from the encoder (`GenSlotToCoeffMatFV`, `rtf_cnp_setup_test.go:125`).

**Peak RSS** (`vm_hwm_kb_end`): SH 10,198,528 kB; CH 61,712,452; SR 19,166,544; CR 66,543,752.
That is 6.05x (HERA) and 3.47x (Rubato) for CNP-B over slot (derived).

In these runs the CNP-B peak was reached during CNP-B's own full-test-set loop, before the HalfBoot
arm was set up. `memory_checkpoints` already shows the final value at `after_crypt_batch_2` (CH) and
`after_correctness` (CR), and both come before `after_halfboot_setup` (`rtf_cnp_test.go:920,928`).
VmHWM for a Go process includes heap that has not been returned to the OS. For comparison, the
largest `heap_alloc_kb` checkpoint is 56,474,877 kB in CH and 45,723,219 kB in CR.

## 3. Correctness and AUC

All four runs were scored tonight with
`python3 scripts/cnp_b/score.py --data ~/cnpb_data --results ~/dgx_results/results/<job> --cipher <c> --arm <slot|cnp>`.
`~/cnpb_data` is the 2026-11-04 regeneration, not the host's `/data`. The score reproduces every
Go-side `correctness.*` field in all four runs: rows, rows differing, max integer diff and
bit-exactness exactly, and logit errors to within 1.4e-16. That confirms the scoring used the same
`expected.i64` and `plain_logit.f64` as the host.

The plaintext baseline ROC AUC (`roc_auc_plaintext_float`) is 0.9793983927 in all four files, over
98 frauds (`frauds`).

| | SH | CH | SR | CR |
|---|---|---|---|---|
| Bit-exact vs integer sim (`correctness.bit_exact`) | **true** | **true** | false | false |
| Rows ≠ integer sim (`correctness.rows_not_equal_to_integer_simulation`) | 0 | 0 | 56,916 | 56,912 |
| Max abs integer diff (`correctness.max_abs_integer_diff`) | 0 | 0 | 2,025 | 1,951 |
| Mean abs logit error (`correctness.mean_abs_logit_error_vs_float_plaintext`) | 0.007887 | 0.007887 | 0.023893 | 0.023866 |
| Max abs logit error (`correctness.max_abs_logit_error_vs_float_plaintext`) | 0.09531 | 0.09531 | 0.12953 | 0.13721 |
| Quantisation-only max logit error, no HE (`correctness."max_abs_quantisation_error_integer_sim_vs_float (no HE)"`) | 0.09531 | 0.09531 | 0.09531 | 0.09531 |
| ROC AUC, encrypted (`roc_auc_encrypted`, score JSON) | 0.9793903 | 0.9793903 | 0.9793798 | 0.9798535 |
| ΔROC AUC (`delta_roc_auc`, score JSON) | −8.08e-6 | −8.08e-6 | −1.86e-5 | +4.55e-4 |
| PR AUC, encrypted / plaintext (`pr_auc_encrypted` / `pr_auc_plaintext_float`) | 0.82345 / 0.82383 | 0.82345 / 0.82383 | 0.82350 / 0.82383 | 0.82330 / 0.82383 |

**HERA.** Both arms are bit-exact, and their decrypted outputs are identical row for row
(`slot_hera_dec.i64` vs `cnp_hera_dec.i64`, after reordering by `*_rows.i32`; computed, not a JSON
field). So HERA's whole logit error and ΔAUC come from quantisation: its max logit error equals the
no-HE quantisation error.

**Rubato.** The rows differ because Rubato's client-side keystream noise is not in the integer
simulation. The mechanism and prediction are in `reports/slot_vs_cnp_rubato_logn16.md` §2: SD of
d = 460.6, about 49 zero rows expected. I computed the same statistics from the `.i64`/`.i32` files,
where d = decrypted − `expected.i64` on the same rows; these are not JSON fields.

| | Mean d (SE) | SD d | Rows with d = 0 | Max \|d\| |
|---|---|---|---|---|
| SR | 0.68 (1.93) | 460.7 | 46 | 2,025 |
| CR | 0.10 (1.93) | 460.4 | 50 | 1,951 |

Both arms match the predicted noise distribution. There is no sign of a bias or wrap-around in
either.

**Rubato ΔAUC.** The noise is unseeded, so each run is one draw (`seeding_note`). CR's +4.55e-4
against SR's −1.86e-5 is therefore not evidence of a difference between arms. The October CNP-B
Rubato runs gave +4.84e-5 and −6.27e-5 (`reports/slot_vs_cnp_rubato_logn16.md` §1). All
four ΔAUC values are below 5e-4 in magnitude.

## 4. Wire bytes per transaction

Both harnesses compute these from the object layout, not from serialised messages ("the fork has no
ciphertext marshaller", `wire_bytes.note`).

| bytes per transaction | SH | CH | SR | CR |
|---|---|---|---|---|
| Up, bit-packed 25-bit (`wire_bytes.upload_bytes_per_transaction_bitpacked`) | 800 | 800 | 800 | 800 |
| Up, uint32 wire (`wire_bytes.upload_bytes_per_transaction_uint32_wire`) | 1,024 | 1,024 | 1,024 | 1,024 |
| Down, 60-bit packed (`wire_bytes.download_bytes_per_transaction_60bit_packed`) | **240** | **3,840** | **256** | **3,840** |
| Down, uint64 limbs (`wire_bytes.download_bytes_per_transaction_uint64_limbs`) | 256 | 4,096 | 273.07 | 4,096 |

Every arm downloads one 983,040-byte level-0 FV ciphertext per output ct
(`wire_bytes.download_bytes_per_output_ct_60bit_packed`). The difference comes from how many
transactions share one ciphertext:
- CNP-B: 256 per ciphertext (`lanes_per_output_ct`).
- Slot: 4,096 (SH, `lanes_per_output_ct_mean`) or 3,840 on average (SR).

CNP-B's download is therefore 16x (HERA) and 15x (Rubato) larger per transaction (derived:
3,840 / 240 and 3,840 / 256). Upload is the same in every arm.

## 5. What this supports and what it does not

**Supported by these four runs:**

- **Offline and total cost favour slot, for both ciphers.** CNP-B / slot total is 1.27x for HERA and
  2.47x for Rubato. The gap is StC, about 11.7–11.8 ms per transaction, which slot does not have.
  The gap is far larger than the 1.8–2.6% Crypt drift between the paired jobs, and the rep ranges do
  not overlap.
- **Online cost favours CNP-B, for both ciphers.** It is 1.72x faster for HERA and 1.57x for Rubato.
  Its slowest rep beats slot's fastest for both ciphers, and it ran under the higher host load. For
  this pairing, that answers the open question in the unpaired report: whether the online gap
  survives same-session timing.
- **Online is a small share of total in every arm**, from 0.1% to 1.3%, so for whole-pipeline
  throughput the offline result is the one that decides.
- **Server-side key material favours slot by about 19–26x** at serving time: about 1.0–1.4 GB for
  slot against about 27.3 GB for CNP-B (derived from retained heap). The one-off rotation-key upload
  is about 0.7–1.1 GB for slot against about 8.3 GB for CNP-B (derived). Setup time is 7.6–9.6 s for
  slot against 132–137 s for CNP-B.
- **Download favours slot 15–16x per transaction.** Upload is identical.
- **Correctness is equivalent between the arms.** HERA is bit-exact in both, with identical outputs.
  Rubato's deviation matches the predicted noise distribution in both. All headroom gates passed
  with at least 21.77 bits.

**Not supported:**

- **Confidence intervals on these ratios.** There is one job per arm per cipher, with 5 reps per
  stage, and the ranges in 1 are min/max envelopes, not intervals.
- **A same-moment comparison.** The slot pair and the CNP-B pair ran one after the other. The Crypt
  control bounds the session effect at 1.8–2.6%, but only for Crypt.
- **HERA vs Rubato.** The two ciphers ran on different sockets (0 and 1), so cross-cipher ratios
  confound cipher with socket. Also, the preflight shows NUMA node 0 with 1.77 GB `MemFree` against
  72.5 GB on node 1 (`~/dgx_results/slot_cnp_meta/preflight.txt`, mostly page cache), plus other
  users' processes on both sockets. Whether the socket-0 jobs allocated memory on the remote node
  was not measured.
- **Server memory requirements.** Peak RSS is Go VmHWM, and the serving-resident sums and
  rotation-key byte counts for CNP-B are derived from retained heap, not measured allocations or
  wire transfers.
- **Measured network cost.** The wire bytes are computed from the object layout.
- **Partial-batch (unamortised) latency, end-to-end latency with a network, or deployment
  throughput.** None was measured.
- **The HalfBoot lower-bound arm.** It is in the CNP-B JSONs (`baseline_*`) but outside this
  comparison.
- **Any arm difference in Rubato AUC.** Each ΔAUC is one unseeded noise draw over 98 frauds.
