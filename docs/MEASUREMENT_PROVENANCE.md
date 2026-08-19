# Measurement Provenance — HERA-16 vs Rubato-128L Transciphering Arm

Written 2026-08-19 in response to a specific, correct concern: a collaborator
drafting a paper noticed that per-keystream-element figures in this repo are
reported in **nanoseconds** while every other client-side timing figure is
reported in **milliseconds**, and read that as an unexplained three-orders-
of-magnitude inconsistency. He was right to refuse to use either number until
this was traced. This document traces every transciphering timing/memory
figure that currently appears anywhere in this repo, the paper decks, or
`README.md` back to a concrete source, and states plainly which ones agree
with each other and which ones do not.

**House rule (PROJECT_STATE.md, established 2026-08-06):** absolute
latencies on this development host are NOT reproducible across sessions.
Governor state, AC/battery power, and background load move the same code's
wall-clock time by 3-4x. **Only same-session paired ratios are defensible.**
Every reconciliation below is judged against that rule, not against whether
two absolute numbers from different days happen to match.

## The provenance table

| # | Metric | Unit | What one unit covers | Value | Source | Date | Governor | Power | Load avg (1/5/15) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | HERA-16 per-record encrypt, 1 lane | ms | 1 transaction, 256 features, full `Encrypt()` incl. AEAD | mean 0.1636 / median 0.150 | `artifacts/hera_vs_rubato_transciphering.json` → `axis_b...by_lane_count[0]` | 2026-08-18 | powersave | AC | 2.16/2.45/2.00 |
| 2 | HERA-16 per-record encrypt, 16 lanes | ms | 16 transactions batched, 4096 features total | mean 1.6412 / median 1.534 | same artifact, `by_lane_count[3]`; identical to `results/hera_bench_lane16_r_fixed2.json` | 2026-08-18 | powersave | AC | 2.16/2.45/2.00 |
| 3 | Rubato-128L per-record encrypt, 1 lane | ms | 1 transaction, 256 features, full `Encrypt()` incl. AEAD + noise | mean 0.0792 / median 0.058 | same artifact, `by_lane_count[0]` | 2026-08-18 | powersave | AC | 2.16/2.45/2.00 |
| 4 | Rubato-128L per-record encrypt, 16 lanes | ms | 16 transactions batched | mean 1.1504 / median 0.975 | same artifact, `by_lane_count[3]` | 2026-08-18 | powersave | AC | 2.16/2.45/2.00 |
| 5 | HERA-16 per-keystream-element | ns | 1 of 16 elements yielded by one HERA block, algebra only (dedicated micro-benchmark, no AEAD) | 251.75 | same artifact → `per_element_normalization`; `cipher/block_bench_test.go: BenchmarkHERABlock` | 2026-08-18T11:28:27Z | powersave | AC | 1.19–1.53 |
| 6 | Rubato-128L per-keystream-element, with noise | ns | 1 of 60 elements yielded by one Rubato block, incl. the Gaussian noise draw (what `Encrypt()` actually runs) | 193.73 | same artifact; `BenchmarkRubatoBlock` | 2026-08-18T11:28:27Z | powersave | AC | 1.19–1.53 |
| 7 | Rubato-128L per-keystream-element, no noise | ns | same, noise step skipped (algebra only) | 143.82 | same artifact; `BenchmarkRubatoBlockNoNoise` | 2026-08-18T11:28:27Z | powersave | AC | 1.19–1.53 |
| 8 | HERA-16 per-record, 1 lane (PRIOR, wrong sampler) | ms | same as #1, but Rubato's noise was Box-Muller/`crypto.rand`, not the reference AGN sampler — kept for the record, not citable | mean 0.0821 / median 0.075 | `artifacts/hera_vs_rubato_transciphering_PRIOR_wrong_noise_sampler.json` | 2026-08-17 | powersave | AC | 2.53/2.40/2.13 |
| 9 | Rubato-128L per-record, 1 lane (PRIOR, wrong sampler) | ms | same caveat as #8 | mean 0.1724 / median 0.150 | same PRIOR artifact | 2026-08-17 | powersave | AC | 2.53/2.40/2.13 |
| 10 | HERA-16 per-element (PRIOR, wrong sampler) | ns | same as #5, single `-benchtime=2000x` run, no repetition — later found methodologically weak, see below | 209.4 | same PRIOR artifact → `per_element_normalization` | 2026-08-17 | powersave (inherited from artifact top-level `machine_state`; no separate sub-timestamp recorded) | AC | 2.53/2.40/2.13 |
| 11 | Rubato-128L per-element with/without noise (PRIOR) | ns | same caveat | 639.6 / 248.1 | same PRIOR artifact | 2026-08-17 | powersave | AC | 2.53/2.40/2.13 |
| 12 | HERA r=5/"128as" toy-scale (LogN=10) wall time / peak RSS, original | s / KB | one full HalfBoot pipeline run through `ckks_fv`'s own reference code, uncontended host | 1.262 s / 293,332 KB | PRIOR artifact `gate_b_resource_usage`; also `toy_correctness/README.md` | 2026-08-17 | powersave | AC | not recorded separately for this sub-run |
| 13 | HERA r=5/"128as" toy-scale, re-verified | s / KB | same, re-run under contention to confirm RSS wasn't affected by the client-side sampler bug | 2.24–2.67 s / 283,028–293,332 KB | current artifact `gate_b_resource_usage` | 2026-08-18 | powersave | AC | 6.9–8.6 (elevated — unrelated `ollama` process at ~560% CPU) |
| 14 | Rubato-128L toy-scale, original | s / KB | same pipeline, Rubato-128L | 1.791 s / 610,808 KB | PRIOR artifact `gate_b_resource_usage`; `toy_correctness/README.md` | 2026-08-17 | powersave | AC | not recorded separately |
| 15 | Rubato-128L toy-scale, re-verified (×3) | s / KB | same, 3 repetitions | 3.86–3.94 s / 610,808–654,328 KB | current artifact `gate_b_resource_usage` | 2026-08-18 | powersave | AC | 6.9–8.6 (elevated) |
| 16 | Toy-scale Rubato/HERA ratio | ratio | peak RSS and wall time, original uncontended run only | wall 1.42×, RSS 2.08× | current + PRIOR artifacts, `toy_vs_hera_ratio*` | 2026-08-17/18 | powersave | AC | n/a |
| 17 | Full-scale (LogN=16, real "128as" params) HERA `hera.Crypt` wall time / peak VmHWM | s / GB | one full run of the real, secure-parameter homomorphic HERA evaluation — the only full-scale figure that exists for either cipher | 73.7 s / 9.54 GB | `artifacts/hera_crypt_rss_full_run.jsonl` (`vm_hwm_kb: 9543896`); `artifacts/hera_crypt_rss_checkpoints.jsonl` for the per-round breakdown | 2026-08-04T15:29 | **not recorded in the artifact itself** — flagged gap, see below | not recorded | not recorded |
| 18 | Upload size, 1 lane / 16 lanes | bytes | wire bytes of one online ciphertext, identical for both ciphers | 1,052 / 16,412 | current + PRIOR artifacts; `cipher.OnlineCiphertextBytes(n) = 4n+28` | 2026-08-18 | n/a (size, not timing) | n/a | n/a |
| 19 | Plain CKKS standard upload | bytes | one standard-parameter CKKS ciphertext | 262,257 | `artifacts/bandwidth_ladder.json` | see that artifact | n/a | n/a | n/a |
| 20 | HERA-16 r=4 per-record, 1 lane / 16 lanes (historical, pre-r=5, pre-SHAKE256) | ms | different cipher version: `HeraRounds=4`, fixed-table round keys — superseded code, kept as historical record | mean 0.508 / 7.435 (or 4.85, see note) | `results/hera_bench_lane1.json`, `results/hera_bench_lane16.json` (git-tracked, commit `32e6516`) | 2026-07-21 | not recorded in file | not recorded | not recorded |
| 21a | HERA-16 r=5 per-record, 1/16 lanes, Chrome-open run — **HERA figure UNSOURCEABLE, plain-CKKS companion IS sourced** | µs | pre-SHAKE256-fix HERA code; same-session paired plain-CKKS 160-bit `encode_encrypt` baseline | HERA: mean 1,102.3 (1 lane) / 15,842.0 (16 lanes). Plain-CKKS 160-bit: mean 10,414.8 / median 10,840.9. Ratio 9.45× (mean) / 9.40× (median) | HERA side: `docs/SESSION_LOG.md` 2026-08-06c prose table only — cites `tools/transciphering/results/hera_bench_lane{1,16}_r5_PRIOR_contended.json`, **never committed, absent from git history and the working tree.** Plain-CKKS side: `artifacts/e2e_latency_breakdown_PRIOR2_contended.json` (committed, confirmed: `160bit.client_stage_us.encode_encrypt_us.mean_us=10414.79`, `median_us=10840.95`) | artifact timestamp 2026-08-05T19:29:50Z UTC (session log labels this "2026-08-06", consistent with IST = UTC+5:30 crossing midnight) | powersave | **battery — UNVERIFIED**, not battery/AC-confirmed in the 2026-08-06c entry itself; a 2026-08-17 addendum later attributes this run's 650–960 MHz clock clamping to battery power, but says so as "reported, not re-confirmed by rerunning" | 5.51 (1-min figure only, per session-log prose — 5/15-min values not recorded) |
| 21b | HERA-16 r=5 per-record, 1/16 lanes, Chrome-closed re-run — same caveat as 21a | µs | same measurement, re-run same day with Chrome closed | HERA: mean 1,096.3 (1 lane) / 17,517.5 (16 lanes). Plain-CKKS 160-bit: mean 9,842.7 / median 10,465.9. Ratio 8.98× (mean) / 9.11× (median) | HERA side: `docs/SESSION_LOG.md` 2026-08-06d prose table only — cites `hera_bench_lane1_r5.json` / `hera_bench_lane16_r5.json` (the same filenames, overwritten in place), **also never committed**, absent from git history and the working tree. Plain-CKKS side: `artifacts/e2e_latency_breakdown.json` (committed, confirmed: `mean_us=9842.67`, `median_us=10465.85`) | artifact timestamp 2026-08-05T19:41:27Z UTC | powersave | same battery-UNVERIFIED caveat as 21a | 0.97 (1-min only) |
| 22 | Rubato-128L full LogN=16 attempt — **DID NOT COMPLETE** | KB (VmHWM) | measured lower bound only, reached during setup, before `rubato.Crypt` was ever called | SIGKILL between checkpoints `slot_to_coeff_mat` and the next (unlabelled) one; last complete checkpoint `vm_hwm_kb=13,279,632` (13.28 GB / 12.66 GiB) | `artifacts/rubato_crypt_rss_full_run.jsonl` (6 complete lines + 1 truncated), `logs/rubato_full_run.log` (2 lines, no PASS/FAIL) | 2026-08-19T19:58:33+05:30 | powersave (inherited from session default, not re-confirmed for this specific run) | not recorded | not recorded |
| 23 | HERA vs Rubato setup-phase RSS, side by side | KB (VmHWM) | same checkpoint labels, both from a real full-LogN=16 run, **different param sets — see the LogSlots finding below before comparing these** | see table under "Per-phase setup comparison" below | `artifacts/hera_crypt_rss_full_run.jsonl`, `artifacts/rubato_crypt_rss_full_run.jsonl` | 2026-08-04 / 2026-08-19 | powersave | AC (HERA), not recorded (Rubato) | not recorded |
| 24 | `heap_alloc_kb` exceeding `vm_hwm_kb` at the same checkpoint | KB | open, unconfirmed anomaly, reproduced independently in two different runs of two different ciphers | HERA: 14.4 GB heap_alloc vs 9.0 GB VmHWM at `round_0` (2026-08-04, unconstrained pre-fix trace). Rubato: 20,257,175 KB heap_alloc vs 13,279,632 KB VmHWM at `slot_to_coeff_mat` (2026-08-19) | `artifacts/hera_crypt_rss_checkpoints.jsonl`; `artifacts/rubato_crypt_rss_full_run.jsonl`; source-checked against `cipher/rss_checkpoint_test.go` (both fields correctly read/labelled, not a code bug) | 2026-08-04 / 2026-08-19 | n/a | n/a | n/a |
| 25 | Row 17's HERA harness — was unreproducible, now fixed | n/a | the `*_test.go` that produced row 17 and row 22 lived in `third_party/RtF-Transciphering/ckks_fv/`, gitignored, deleted after each run | n/a | superseded by `tools/transciphering/cipher/rss_checkpoint_test.go`, committed 2026-08-19 | 2026-08-19 | n/a | n/a | n/a |

## PRIMARY FINDING (2026-08-19): the HERA-vs-Rubato full-scale comparison ran at mismatched LogSlots — 4 vs 15, not a cipher difference

This supersedes the block-size framing an earlier pass of this session used
before reading source. It is the primary result of this recovery pass, not
a footnote to the Rubato-128L OOM below — read this section first.

### Block-size hypothesis: considered and rejected

`RubatoBlockSize = 64` (`cipher/rubato.go:78`) vs `HeraStateSize = 16`
(`cipher/hera.go:56`) is a real 4x difference, and the observed StC-precompute
memory ratio between the two full-scale runs (12.4 GB Rubato / 3.05 GB HERA
≈ 4.07x — see the per-phase table below) is numerically close enough that it
looked like the explanation. It is not. Read directly from source
(`third_party/RtF-Transciphering/ckks_fv/mfv_encoder.go:603`):

```go
func (encoder *mfvEncoder) GenSlotToCoeffMatFV(radix int) (pDcds [][]*PtDiagMatrixT)
```

This function — the one whose call is bracketed by the `slot_to_coeff_mat`
checkpoint in both traces — takes only `radix`. Internally it depends on
`params.logFVSlots` and `modCount := len(params.qi)`. Cipher block size and
round count never enter this call, directly or indirectly. **The 4.07x
numeric match to the 4x block-size ratio is coincidental**, not causal.

### The actual driver: LogSlots, from a parameter-set mismatch that predates this session

Traced through `rtf_params.go` and `halfboot_params.go`:

- HERA's full-scale run used `RtFHeraParams[3]`, **"128as": `LogSlots: 4`**
  (comment in source: "Use only 4 slots for data encoding").
- Rubato's run used `RtFRubatoParams[0]`, **"128af": `LogSlots: 15`**
  (full-coefficient encoding) — **the only Rubato RtF parameter set that
  exists in this checkout.** There is no Rubato "128as" analog.
- `modCount` is held identical: `halfboot_params.go:32`,
  `Qi := append(hb.ResidualModuli, hb.DiffScaleModulus...)`. HERA-128as's and
  Rubato-128af's `ResidualModuli` (8 entries) and `DiffScaleModulus` (1 entry,
  `0x2a0001`) are byte-for-byte identical values in `rtf_params.go`. So
  `modCount = 9` for both — not a confound.
- **LogSlots (4 vs 15) is the one input to `GenSlotToCoeffMatFV` that differs
  between the two runs**, and it drives both dimensions of the function's
  memory: matrix count returned by `genDcdMatsRad2`/`genDcdDiabDecomp` scales
  as `~LogSlots/2` (3 matrices at LogSlots=4, 8 at LogSlots=15 — a traced,
  confirmed 2.67x), and the number of BSGS-encoded ring-polynomial keys per
  matrix (`EncodeDiagMatrixT`, `multDiabMats`) grows combinatorially with
  `2^LogSlots` (capped at N/2). The combined effect could not be reduced to a
  clean closed-form multiplier by hand in this pass — the qualitative driver
  and its direction are source-confirmed; the exact 4.07x is not independently
  re-derived from first principles, and should not be read as "explained to
  the last digit," only as "correctly attributed to the right variable."
- **This mismatch is not new and was already documented, just not connected
  to the memory comparison.** `third_party/RtF-Transciphering/ckks_fv/
  rtf_toy_correctness_rubato128l_test.go:41-44` (committed via
  `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/`, staged into
  the vendored tree by `fetch_rtf.sh`) already states: *"Unlike HERA's 'as'
  config (LogSlots=4, fixed independent of LogN — a sparse-slot packing),
  '128af' uses full-coefficient packing with LogSlots = LogN-1... LogSlots
  must shrink along with LogN here or Parameters.Params() panics."* Whoever
  wrote that comment already knew the two ciphers' only available param
  families pack data at structurally different slot occupancies. It was never
  linked to what that implies for a full-scale memory or StC-cost comparison
  until this pass.

### Per-phase setup comparison (row 23)

Same checkpoint labels, both real full-LogN=16 runs:

| checkpoint | HERA VmHWM (KB) | HERA delta | Rubato VmHWM (KB) | Rubato delta |
|---|---|---|---|---|
| entry | (not separately checkpointed pre-setup in the 2026-08-04 trace; setup starts effectively at process entry) | — | 6,692 | — |
| params_ready | — | — | 6,968 | +276 |
| keypair_sparse | ~85,680 (from the 2026-08-04 full trace's early checkpoints) | — | 85,680 | +78,712 |
| encoders_ready | — | — | 860,144 | +774,464 |
| rotation_indexes_halfboot | ~860,200 (setup, pre-StC) | — | 860,200 | +56 |
| **slot_to_coeff_mat** | **~3,914,000** (setup total 3,653,896 KB key material + 3,053 MB StC per `RtF_Transciphering_Progress_v3.pptx` slide 14 → `~3,113,896`–`3,914,000` band depending on exact split point measured) | — | **13,279,632** | **+12,419,432** |
| entry → hera.Crypt start | 9,265,684 (full setup complete) | — | *(run died here — no further checkpoints)* | — |
| peak | **9,543,896** (round_5_cube, +278 MB over hera.Crypt's own entry) | — | **≥13,279,632, true peak unknown (higher)** | — |

The HERA setup-phase numbers above for individual sub-checkpoints before
`hera.Crypt`'s own entry are reconstructed from `RSS_INSTRUMENTATION.md`'s
prose breakdown (StC precompute ~3,053 MB, key material ~3,654 MB, encKey
~1,259 MB — same document, "FINDING 3" slide) rather than re-read line-by-line
from `hera_crypt_rss_checkpoints.jsonl` in this pass; treat the HERA
per-checkpoint column as **approximate**, not to the same precision as the
Rubato column (which is read directly from the surviving 2026-08-19 trace).
**Both columns are real measurements of different workloads (LogSlots 4 vs
15) — do not read this table as "Rubato's StC step costs 4x more because it
is Rubato's StC step."** It costs more because it ran a ~2,048x larger
slot-occupancy configuration through the same function.

### The comparison is invalid as run — no cross-cipher claim is supportable from these two runs

HERA-128as (LogSlots 4, 16 usable slots) vs Rubato-128af (LogSlots 15, 32,768
usable slots) is a **2,048x difference in encoded slot occupancy.** Neither
run's full-scale memory or timing figure can be attributed to the *cipher* —
each ran a materially different homomorphic-evaluation workload. Concretely:
**the 13.28 GB Rubato lower bound and the 9.54 GB HERA peak are both real
measurements, of different workloads, and no memory, runtime, or StC-cost
comparison between them is supportable.** This statement is repeated in
`PROJECT_STATE.md` and `docs/PAPER_HANDOFF.md` because it changes what either
document can claim about "HERA vs Rubato at full scale" — as run, there is no
such comparison; there is one LogSlots=4 HERA measurement and one
LogSlots=15 Rubato non-completion.

### Where the 9.54 GB / 73.7s / "full-scale" figures are cited, and what needs correcting

Grepped `README.md`, `docs/`, and `docs/RtF_Transciphering_Progress_v3.pptx`
for `9,543,896` / `9.54 GB` / `73.7` / "full-scale". Full list:

- `README.md:329,336,337,358,361,370,372,445` — six uses of "full-scale" and
  one of the 73.7s/9.54GB pair (line 358). **Corrected in this pass** to say
  "LogN 16, LogSlots 4 (16 slots)" at the point of first use (line 358) and
  cross-referenced from the others rather than repeating the caveat six times.
- `docs/MEASUREMENT_PROVENANCE.md:40` (row 17) and its surrounding prose
  (lines 191-200) — **corrected in this pass**, see row 17's note below.
- `docs/PAPER_HANDOFF.md:30,47-48` — **corrected in this pass**, see that
  file.
- `PROJECT_STATE.md:117,138-139,151` — **corrected in this pass**, see the
  2026-08-19 dated block.
- `docs/SESSION_LOG.md` (multiple lines, e.g. 299, 745, 843, 889, 912, 917,
  1236, 1299) — **not edited**; `SESSION_LOG.md` is a dated historical record,
  not a living reference doc. A dated correction block is appended instead
  (same pattern already used for the row-21 correction), pointing here.
- `docs/RtF_Transciphering_Progress_v3.pptx` slides 14, 15, 17, 18 —
  **NOT edited**, per instruction. All four already say `"128as"` explicitly
  in the slide text (e.g. slide 14: *"Direct instrumentation of hera.Crypt at
  full scale, LogN 16 '128as'"*), so the param-set name is present; what they
  omit is that "128as" means LogSlots=4 (16 of 32,768 slots). Anyone citing
  these slides for the paper needs this document's context alongside them.

Row 17 itself (the table entry) is left as a historical record of what was
measured — the number `9,543,896` and `73.7s` are real — but its own
description column and the surrounding prose (originally "Full-scale
(LogN=16, real '128as' params)... the only full-scale figure that exists for
either cipher") is corrected: **"full-scale" describes the ring (LogN=16)
correctly but implied full slot occupancy, which is false — it was measured
at LogSlots=4, 16 of 32,768 usable slots.**

### Do other headline numbers inherit this? (reported, not fixed)

- **1,052-byte / ~249x upload-size figure (row 18, `README.md:313-316`,
  `docs/spec.md:4537`)** — **NOT affected.** `OnlineCiphertextBytes(n) =
  4n+28` is a pure AEAD-ciphertext byte count on the client-side symmetric
  path; it never touches CKKS/RtF parameters, LogSlots, or `ckks_fv` at all.
  The 262,257-byte "standard CKKS" comparator is a fixed ciphertext wire size
  (proportional to ring degree × modulus count, not to how many of its slots
  hold meaningful data), so it is not affected either.
- **Toy-scale (LogN 16→10) correctness harnesses, both ciphers** — **AFFECTED,
  same mismatch, present since these harnesses were written.**
  `rtf_toy_correctness_hera128as_test.go:34-38` copies `RtFHeraParams[3]`
  ("128as") with LogSlots fixed at 4 regardless of LogN.
  `rtf_toy_correctness_rubato128l_test.go:38-50` copies `RtFRubatoParams[0]`
  ("128af") with `LogSlots = LogN-1`, i.e. **9** at the toy LogN=10. So the
  toy-scale correctness runs for the two ciphers differ in slot occupancy by
  `2^9 / 2^4 = 32x`, not the LogN scaling factor alone. This means **row 16's
  toy-scale RSS ratio (2.08x, rows 12-15) is not a clean per-cipher
  comparison either** — it inherits the same LogSlots mismatch that the
  full-scale attempt did, at a smaller but still real magnitude. It was
  never a valid basis for extrapolating "Rubato costs ~2x HERA at full
  scale," independent of the fact that toy-to-full-scale extrapolation is
  already flagged elsewhere as non-linear/unconfirmed.
- **`artifacts/hhe_breakeven.json`** — **NOT currently affected in practice**,
  because its `online_transcipher_ms`/`repacking_ms` cells (the only cells
  that would touch CKKS/RtF LogSlots) remain `"PENDING"` — the framing's
  `batch_occupancy_lanes` axis (1/4/8/16) is client-side AEAD record
  batching from `bench/main.go`, decoupled from CKKS slot occupancy, and its
  `online_encrypt_ms` values don't involve `ckks_fv` at all. **Landmine for
  later**: `pending_reason` still cites the stale "~60 GB" HERA RAM anchor
  (already superseded elsewhere in this repo by the 9.54 GB 128as figure);
  whoever eventually fills in the PENDING cells will need this document's
  LogSlots caveat at that point, since the server-side transcipher timing
  that fills those cells will run at whatever LogSlots the chosen param set
  uses.

## Rows 21a/21b are the actual "three orders of magnitude" complaint — and it is two separate issues, one of which is NOT a contradiction

The collaborator's instinct that something doesn't add up is correct, but
not for the reason it first looks like. There are two independent things
here, and an earlier version of this document incorrectly conflated them
into a single "15.8 ms vs. 17.5 ms disagreement." **Correction: they are not
in disagreement. They are two distinct, legitimate runs, both kept
deliberately, exactly as `docs/SESSION_LOG.md`'s 2026-08-06c/d entries
describe:**

**Not a problem — two different runs, correctly both real.** Row 21a
(15,842.0 µs, 16 lanes) is the Chrome-open/contended run (load avg 5.51).
Row 21b (17,517.5 µs) is the same benchmark re-run the same day with Chrome
closed (load avg 0.97) — and the 16-lane figure went *up*, not down, while
the single-record figure stayed flat (1,102.3 → 1,096.3 µs) and the paired
plain-CKKS baseline improved (10,414.8 → 9,842.7 µs). `docs/SESSION_LOG.md`
2026-08-06d already documents this directly, attributing the non-uniform
shift to `powersave`'s clock floor rather than Chrome contention specifically
— ordinary run-to-run noise on this host, not a code change and not an
error. Both entries were kept intentionally (not overwritten) for exactly
this reason. The earlier framing of this as "the session log's own prose
table disagrees with the deck slide it says it updated" was wrong: block
06c produced 21a, block 06d is a *later, separate* re-run that produced 21b
and explicitly narrates the difference — there was never a single contested
figure, only two sequential measurements this document previously read as
one.

**The real, standing problem — the HERA half of both rows is unsourced.**
Independent of the above, and not resolved by correcting it: **neither
`hera_bench_lane1_r5_PRIOR_contended.json` / `hera_bench_lane16_r5_PRIOR_contended.json`
(row 21a) nor `hera_bench_lane1_r5.json` / `hera_bench_lane16_r5.json` (row
21b) were ever committed to git.** `git log --all --diff-filter=A` finds
zero additions of any `_r5.json` or `_r5_PRIOR_contended.json` file, ever,
in this repository's history. This is true for **both** runs, not just one
— correcting the "disagreement" framing does not make either figure
reproducible. The plain-CKKS companion figure in each row **is** sourced
(`artifacts/e2e_latency_breakdown_PRIOR2_contended.json` and
`artifacts/e2e_latency_breakdown.json` respectively, both committed), so the
~9.4×/~9.0× ratios are only half-reproducible: the denominator can be
recomputed from a committed artifact, the numerator cannot. **Per the hard
rule governing this pass, rows 21a/21b's HERA-side numbers are flagged, not
deleted, and remain out of `README.md`'s prose** in favor of the current,
fully sourced row 1–4 numbers (measured 2026-08-18, both sides of every
ratio in a single committed artifact).

**Units, separately (this part of the original analysis was correct and is
unchanged):** rows 5–7 (ns/element) and rows 1–4 (ms/record) are not the
same kind of measurement — one is an isolated micro-benchmark of the
cipher's inner loop with no AEAD/quantization overhead, the other is the
full client-facing `Encrypt()` call for an entire 256-feature transaction.
Comparing "251.75" against a millisecond figure without noticing the unit
label produces a gap that reads like "three orders of magnitude" at a
glance; see the reconciliation below showing these are consistent once
units and scope are accounted for.

## Do the per-element and per-record figures reconcile? Yes — arithmetic below

Take the **current, same-session, correctly-sampled** numbers only (rows
1–7, all from 2026-08-18). A per-record `Encrypt()` call for `n` features
computes `ceil(n / yield_per_block)` blocks, where HERA yields 16 usable
elements/block and Rubato-128L yields 60 (`RubatoOutputSize`). Multiplying
the per-block-element cost from the micro-benchmark (rows 5–7) by the number
of elements a record actually needs gives a predicted algebra-only cost,
which should sit at or below the measured full-`Encrypt()` cost (rows 1–4);
the gap is AEAD sealing (AES-128-GCM), quantization, and per-call setup.

**HERA-16, 1 lane (256 features → 16 blocks × 16 elements = 256 elements):**
`256 × 251.75 ns = 64,448 ns = 0.0645 ms` predicted (algebra only) vs.
**0.164 ms measured** (row 1) → measured is 2.5× the algebra-only floor —
plausible AEAD/quantization overhead, not a contradiction.

**HERA-16, 16 lanes (4096 features → 256 blocks × 16 = 4096 elements):**
`4096 × 251.75 ns = 1,031,168 ns = 1.031 ms` predicted vs. **1.641 ms
measured** (row 2) → 1.59×, consistent with per-call overhead amortizing
better at higher lane counts (the ratio to the algebra floor shrinks from
2.5× to 1.6× as batch size grows, exactly what fixed per-call overhead
predicts).

**Rubato-128L, 1 lane (256 features → 5 blocks of 60, last one truncated to
16 used → 5 block computations, with-noise cost applies):** `5 × 11,624 ns =
58,120 ns = 0.0581 ms` predicted vs. **0.058 ms measured (median)** (row 3)
— this one matches almost exactly, because the median (not mean, which is
pulled up by GC/scheduler tail latency) is the cleaner comparator against a
median-of-7-repetitions micro-benchmark.

**Rubato-128L, 16 lanes (4096 features → 69 blocks of 60 = 4140 ≥ 4096):**
`69 × 11,624 ns = 802,056 ns = 0.802 ms` predicted vs. **1.150 ms measured
(mean) / 0.975 ms (median)** (row 4) → 1.22–1.43×, same shape as HERA's
overhead band.

**Conclusion: the ns and ms figures reconcile to within a 1.2–2.5× band
attributable to AEAD/quantization/per-call overhead, consistently across
both ciphers and all measured lane counts, when both sides come from the
same session.** There is no unexplained gap between rows 1–7. The apparent
three-orders-of-magnitude problem was (a) a units-legibility issue and (b)
rows 21a/21b sitting in the prose next to the correctly-sourced ones,
inviting exactly the comparison that triggered this concern — not because
21a and 21b disagree with each other (they don't; see above), but because
neither one's HERA-side value can be reproduced from anything in this repo.

**Rows 8–11 (PRIOR artifact) and rows 1–7 do NOT reconcile with each
other, and are not supposed to** — different code (wrong noise sampler),
different session, correction documented explicitly in the PRIOR artifact's
own `correction_note` and in `PROJECT_STATE.md`.

**Row 20 and rows 21a/21b do NOT reconcile with rows 1–4**, and the
candidate causes, in order of plausibility, are:

1. **Different code**, not just different session: rows 20/21a/21b predate
   the 2026-08-18 rewrite of `cipher/hera.go`'s round-key derivation (fixed
   `heraRC` table + AES-128-ECB PRF → SHAKE256-XOF), which is a structurally
   different keystream computation, not a parameter tweak.
2. **Different measurement session** entirely (2026-07-21 / 2026-08-05 vs.
   2026-08-18) — per the house rule, this alone is sufficient to make direct
   comparison invalid regardless of cause (1).
3. **Possible battery clock clamping, UNVERIFIED** — `docs/SESSION_LOG.md`'s
   2026-08-17 addendum reports (without re-confirming by rerunning) that
   the 650–960 MHz clamping observed during *both* the 06c and 06d runs
   (i.e. both 21a and 21b) traces to the laptop running on battery power,
   not to Chrome contention or the `powersave` governor alone. An earlier
   version of this document incorrectly stated this had been resolved to
   AC power for these runs — corrected here: it was not confirmed either
   way at measurement time, and the later addendum's best guess is battery
   for both. This is a real, previously-documented cause of a comparable
   3.7× swing elsewhere on this exact host, so it's a plausible contributor
   to the row 20/21a/21b gap, not just a formality.
4. **Rows 21a and 21b are unsourced regardless of cause 1–3** — per the
   provenance note above, no amount of explaining candidate causes changes
   that neither figure can currently be reproduced from any file in this
   repo.

## What this means for the paper

- Cite rows 1–7 (2026-08-18, current) for any HERA-vs-Rubato client-cipher
  comparison. Cite the ratio, not the absolute ms/ns value, if citing across
  more than this one session.
- Do not cite rows 21a/21b's "1.10 ms / 17.5 ms"-family HERA figures for
  anything; they remain out of `README.md`'s prose. Their paired plain-CKKS
  companions (10,414.8 / 9,842.7 µs) ARE individually reproducible
  (`artifacts/e2e_latency_breakdown_PRIOR2_contended.json` /
  `e2e_latency_breakdown.json`), but the ratio as a whole is not, since its
  other half isn't. If an r=5, pre-SHAKE256 historical HERA figure is ever
  needed, it would have to be re-measured, not recovered — the source files
  don't exist.
- Row 17 (the only full-scale figure) has no recorded governor/power/load
  state in its own artifact. Flagged as a provenance gap: `PROJECT_STATE.md`
  and the paper decks assert `GOMEMLIMIT=11GiB GOGC=50` was used but not the
  CPU governor or power source at the time. Treat the 73.7s wall-time figure
  as directional only; the 9.54 GB peak RSS figure is far less sensitive to
  these confounds (memory, not wall-clock) and is the more citable of the
  two.
- No full-scale (LogN=16) measurement exists for Rubato-128L at all — only
  the toy-scale (LogN=10) rows 14–16 exist, explicitly flagged in every
  source as "directional, not predictive" of full-scale behavior. Rubato's
  first full-scale attempt (row 22, 2026-08-19) did not complete: SIGKILL
  during setup, 13.28 GB VmHWM measured lower bound, true peak unknown.
- **The "PRIMARY FINDING" section above supersedes any prior framing of the
  HERA-vs-Rubato full-scale comparison as a cipher-vs-cipher result.** As run,
  HERA's 9.54 GB figure (LogSlots=4) and Rubato's 13.28 GB lower bound
  (LogSlots=15) measure different workloads, 2,048x apart in slot occupancy.
  Do not cite one against the other for any claim about which cipher costs
  more at full scale. See `docs/RUBATO_FULLSCALE_PLAN.md` for the
  LogSlots-matched re-run this implies (HERA at `RtFHeraParams[2]`, "128af",
  LogSlots=15) as the actual next step, in place of any Rubato-alone re-run.
- The toy-scale (LogN=10) correctness harnesses for HERA and Rubato also run
  at different LogSlots (4 vs 9) — see "Do other headline numbers inherit
  this?" above. The 249x upload-size figure and `hhe_breakeven.json`'s
  current PENDING cells are not affected.
