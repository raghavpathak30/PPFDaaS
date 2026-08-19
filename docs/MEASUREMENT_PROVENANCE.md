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
| 21 | HERA-16 r=5 per-record, 1 lane / 16 lanes (deck/README figure — **UNSOURCEABLE**) | ms | claimed same measurement as #20 but at r=5, pre-SHAKE256-fix code | "1.10 ms" / "17.5 ms" (`docs/RtF_Transciphering_Progress_v3.pptx` slide 3; identically in root `README.md` line 285 before this pass) | **No file matching these values exists in the repo, tracked or untracked.** `docs/SESSION_LOG.md`'s own 2026-08-06b entry cites `results/hera_bench_lane1_r5.json` / `..._lane16_r5.json` (and gives **15.8 ms**, not 17.5 ms, for the 16-lane figure) — those files were never committed (`git log --all --diff-filter=A` finds zero `_r5.json` additions ever) and do not exist in the working tree | 2026-08-06 (per session log) | powersave | AC (session log resolved an earlier battery-clamping confound for this exact session) | ~2.5 (per session log prose) |

## Row 21 is the actual "three orders of magnitude" complaint — and it is two separate, compounding issues

The collaborator's instinct that something doesn't add up is correct, but not
for the reason it first looks like. There are two independent problems
layered on top of each other:

**Problem A — units, not magnitude.** Rows 5–7 (ns/element) and rows 1–4
(ms/record) are not the same kind of measurement at all: one is an isolated
micro-benchmark of the cipher's inner loop with no AEAD/quantization
overhead, the other is the full client-facing `Encrypt()` call for an entire
256-feature transaction. Comparing "251.75" against "1.10" without noticing
the unit label looks like a ~229× (≈2.4-order-of-magnitude) gap — close to
what reads as "three orders of magnitude" if eyeballed. This is not an error;
see the reconciliation below showing these numbers are consistent once units
and scope are accounted for.

**Problem B — row 21 traces to nothing.** Independent of the units issue,
the specific figures currently sitting in `README.md` ("1.10 ms" / "17.5
ms") do not correspond to any artifact file that exists in this repository.
The session log that reports measuring them cites result files
(`hera_bench_lane{1,16}_r5.json`) that were never committed to git and are
absent from the working tree — and even the session log's own prose table
gives a different 16-lane figure (15.8 ms) than the deck slide it says it
updated (17.5 ms). This is a real gap, not paranoia: **per the hard rule
governing this pass, row 21's numbers are flagged, not deleted, and are
being removed from `README.md`'s prose in favor of the current, fully
sourced row 1–4 numbers** (see `README.md`'s transciphering section after
this pass).

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
three-orders-of-magnitude problem was (a) a units-legibility issue and (b) a
genuinely unsourceable older figure (row 21) sitting in the prose next to
the correctly-sourced ones, inviting exactly the comparison that triggered
this concern.

**Rows 8–11 (PRIOR artifact) and rows 1–7 do NOT reconcile with each
other, and are not supposed to** — different code (wrong noise sampler),
different session, correction documented explicitly in the PRIOR artifact's
own `correction_note` and in `PROJECT_STATE.md`.

**Row 20 and row 21 do NOT reconcile with rows 1–4**, and the candidate
causes, in order of plausibility, are:

1. **Different code**, not just different session: row 20/21 predate the
   2026-08-18 rewrite of `cipher/hera.go`'s round-key derivation (fixed
   `heraRC` table + AES-128-ECB PRF → SHAKE256-XOF), which is a structurally
   different keystream computation, not a parameter tweak.
2. **Different measurement session** entirely (2026-07-21 / 2026-08-06 vs.
   2026-08-18) — per the house rule, this alone is sufficient to make direct
   comparison invalid regardless of cause (1).
3. **Battery vs. AC clock clamping** — documented elsewhere in this repo
   (`docs/SESSION_LOG.md` 2026-08-06e) as a real, previously-misdiagnosed
   cause of a 3.7× swing on this exact host; the 2026-08-06b session
   specifically checked for and ruled this out for its own numbers (resolved
   to AC, powersave), so it is not the cause of the row 20/21 gap, but is
   listed here as a standing candidate cause for any two absolute-latency
   figures on this host that were not checked.
4. **Row 21 specifically is unsourced**, per the "UNSOURCEABLE" note above —
   no amount of explaining candidate causes changes that its exact value
   cannot currently be reproduced from any file in this repo.

## What this means for the paper

- Cite rows 1–7 (2026-08-18, current) for any HERA-vs-Rubato client-cipher
  comparison. Cite the ratio, not the absolute ms/ns value, if citing across
  more than this one session.
- Do not cite row 21's "1.10 ms / 17.5 ms" for anything; it has been removed
  from `README.md`'s prose by this pass. If a corrected r=5, pre-SHAKE256
  historical figure is ever needed, it would have to be re-measured, not
  recovered — the source file doesn't exist.
- Row 17 (the only full-scale figure) has no recorded governor/power/load
  state in its own artifact. Flagged as a provenance gap: `PROJECT_STATE.md`
  and the paper decks assert `GOMEMLIMIT=11GiB GOGC=50` was used but not the
  CPU governor or power source at the time. Treat the 73.7s wall-time figure
  as directional only; the 9.54 GB peak RSS figure is far less sensitive to
  these confounds (memory, not wall-clock) and is the more citable of the
  two.
- No full-scale (LogN=16) measurement exists for Rubato-128L at all — only
  the toy-scale (LogN=10) rows 14–16 exist, explicitly flagged in every
  source as "directional, not predictive" of full-scale behavior.
