# Paper Handoff — HERA-16 / Rubato-128L Transciphering Arm

Written 2026-08-19 for the collaborator drafting the transciphering section
of the paper. This is not persuasion and not framing — it is a list of what
this repo can currently back up, what it cannot, and where to look for each.

## On the branch itself, first

The context you were working from was stale, and you were right not to use
numbers you couldn't verify. One correction to how that was described to
you: the `rubato-swap` branch is **not** unpushed — `git log main..origin/
rubato-swap` shows all 5 of its commits already sitting on `origin`. What's
stale is `main`, which this branch has not been merged into. Practically,
this changes nothing about what you should do: **diff `rubato-swap` against
whatever you were working from, or pull it directly, rather than taking any
figure in this handoff on trust.** Every number below carries a path you can
open yourself.

## What is measured

| Claim | Artifact / source |
|---|---|
| HERA-16 and Rubato-128L both pass a known-answer test against `ckks_fv`'s own reference keystream | `cipher/hera_test.go`, `cipher/rubato_test.go`; re-run 2026-08-19, both PASS |
| Both pass a toy-scale (LogN 16→10) full-pipeline HE correctness harness | `toy_correctness/testdata/ckks_fv_patch/*.go`; re-run 2026-08-19: HERA r=5 max abs err 2.49e-5, Rubato-128L 3.40e-5, both vs. 5e-2 tolerance |
| Rubato-128L's Gaussian noise sampler statistically matches its target σ | `cipher/rubato_test.go: TestRubatoNoiseStatistics`; re-run 2026-08-19: stddev 1.1–1.3% off target across two runs |
| Per-record client-encrypt cost, HERA vs. Rubato, 1/4/8/16-lane sweep | `artifacts/hera_vs_rubato_transciphering.json`; Rubato is 0.48–0.82x HERA's cost (cheaper) |
| Per-keystream-element cost, both ciphers, with and without noise | same artifact, `per_element_normalization`; ns-scale, reconciles arithmetically with the ms-scale per-record figures — see `docs/MEASUREMENT_PROVENANCE.md` |
| Upload size identical between ciphers at every lane count; ~16–249x smaller than plain CKKS | same artifact; `artifacts/bandwidth_ladder.json` |
| Toy-scale (LogN=10) peak RSS: HERA 293,332 KB, Rubato 610,808 KB (~2.08x), stable across 4 re-runs | same artifact, `gate_b_resource_usage` |
| Full-scale (LogN=16) HERA peak VmHWM 9.54 GB, 73.7s wall — **at `RtFHeraParams[3]` "128as", LogSlots=4 (16 of 32,768 slots), not full occupancy** | `artifacts/hera_crypt_rss_full_run.jsonl`, `artifacts/hera_crypt_rss_checkpoints.jsonl` |
| Rubato-128L's first full-scale (LogN=16) attempt did NOT complete — SIGKILLed during setup, measured lower bound 13.28 GB VmHWM, at `RtFRubatoParams[0]` "128af", LogSlots=15 (full 32,768-slot occupancy) | `artifacts/rubato_crypt_rss_full_run.jsonl` (partial trace, committed), `logs/rubato_full_run.log` |
| Multiplicative depth: HERA-16 = 10 sequential levels, Rubato-128L = 2 | read directly from `ckks_fv/fv_hera.go` and `fv_rubato.go`; CONFIRMED-SOURCE, not measured — see `artifacts/hera_vs_rubato_transciphering.json`'s `multiplicative_depth_argument` |
| Exact Rubato-128L parameters as implemented | `cipher/rubato.go` lines 78-86: n=64, r=2, q=0x1fc0001, σ≈1.6357 |

Every one of these is reproducible: `third_party/fetch_rtf.sh` then
`go test ./cipher/...` and the toy-correctness harnesses under
`third_party/RtF-Transciphering/ckks_fv/` (see `tools/transciphering/
toy_correctness/README.md` for the exact commands).

## What is explicitly NOT measured, and why

- **No end-to-end HHE-vs-plain-CKKS latency verdict, for either cipher.**
  `vendor_server`'s BFV evaluation is stub-only — the homomorphic (server-
  side) HERA/Rubato evaluation and FV→CKKS repacking have never been
  integrated into the deployed service. `artifacts/hhe_breakeven.json`'s 36
  cells remain `"PENDING"` for exactly this reason and were **not** touched
  by this pass — do not fill them in without an actual `vendor_server` run.
- **Rubato-128L's full-scale (LogN=16) memory/time figure does not exist as
  a completed number, but it is now a measured non-completion with a hard
  lower bound, not silence** (see the table above and the section below).
  If you need a completed full-scale Rubato figure for the paper, it does
  not exist yet and would need to be run on a host with more headroom, not
  estimated by scaling the toy number — see `docs/RUBATO_FULLSCALE_PLAN.md`.
- **The multiplicative-depth argument (10 vs. 2 levels) is sourced from
  reading the reference implementation, not measured by running it
  homomorphically.** It's a real, citable structural fact, but it is not
  the same kind of evidence as a wall-clock number, and the client-side
  timing numbers above measure a *different* cost model (plaintext CPU),
  so their agreement in direction with the depth argument should not be
  cited as confirming it.
- **The plain-CKKS baseline used for any historical HHE-vs-CKKS comparison
  (e.g. an "N.Nx" ratio) was measured against a since-rewritten HERA
  implementation and a different session** — do not combine it with any
  number in this handoff. See `docs/MEASUREMENT_PROVENANCE.md` row 20/21
  for exactly which historical figures are and are not still traceable to a
  source file.
- **Batched-reduction correctness beyond a single block/256-slot record is
  untested** for both ciphers.

## Metrics table

The full provenance table — every timing/memory number that appears
anywhere in this repo, the progress decks, or `README.md`, with unit, exact
source file, date, and machine state, plus the arithmetic reconciling the
nanosecond and millisecond figures — is `docs/MEASUREMENT_PROVENANCE.md`.
Read it before citing any HERA-vs-Rubato number; it also documents one
specific figure (`README.md`'s previous "1.10 ms / 17.5 ms" HERA claim)
that turned out not to trace to any file in this repo and has been removed.

## The full-scale memory comparison as run is not a HERA-vs-Rubato result — read this before citing either number

This is a 2026-08-19 finding, and it changes what the two full-scale memory
figures above can be used to say.

**The HERA and Rubato full-scale runs used different CKKS/RtF slot
occupancies, not just different ciphers.** HERA's 9.54 GB figure was
measured at `RtFHeraParams[3]` ("128as"), `LogSlots=4` — only 16 of the
ring's 32,768 available slots hold data. Rubato's run used
`RtFRubatoParams[0]` ("128af"), `LogSlots=15` — full occupancy, all 32,768
slots. That is the only Rubato RtF parameter set this checkout has; there is
no Rubato "128as" analog to run instead. **A 2,048x difference in slot
occupancy, traced to source** (`GenSlotToCoeffMatFV`'s memory cost is driven
by `LogSlots` and the RNS level count, not by cipher block size or round
count — a block-size hypothesis was checked and rejected first; see
`docs/MEASUREMENT_PROVENANCE.md`'s "PRIMARY FINDING" section for the full
trace).

**Consequence: do not present the 9.54 GB / 13.28 GB pair as "HERA vs
Rubato" for memory, and do not present the earlier toy-scale 2.08x RSS
ratio that way either — it inherits the same mismatch (LogSlots 4 vs 9 at
toy LogN=10) at smaller magnitude.** As currently measured, this repo has:
one HERA figure at LogSlots=4, and one Rubato non-completion at LogSlots=15.
There is no LogSlots-matched full-scale figure for either cipher yet. The
LogSlots-matched next step — HERA at its own existing `RtFHeraParams[2]`
("128af", LogSlots=15) — has not been run either; `docs/
RUBATO_FULLSCALE_PLAN.md` expects it to also not fit on a ~16 GB host, which
would itself be the more interesting finding (a HalfBoot/StC memory wall at
full slot occupancy, largely cipher-independent, rather than a
Rubato-specific cost).

**What this does NOT affect:** the multiplicative-depth argument (10 vs. 2
levels, read from source, not measured) and the client-side per-record/
per-element cost comparisons above are unaffected — neither involves
`GenSlotToCoeffMatFV` or CKKS/RtF slot occupancy at all. Nor does the
1,052-byte/249x upload-size figure (pure AEAD byte-counting, no CKKS
parameters involved). Only the full-scale and toy-scale *memory* comparisons
are affected.

**For the paper: if this arm's memory story is cited at all, cite it as
"HERA at 16-slot occupancy completed at 9.54 GB; Rubato's only available
parameter set forces full 32,768-slot occupancy and did not complete on a
~16 GB host, with a measured 13.28 GB lower bound before OOM" — not as "X
uses N times more memory than Y."** The performance-comparison-point framing
below (Rubato-128L as a comparison point, not a proposed production cipher)
still holds and is unaffected by this finding.

## Rubato-128L security status — the open blocker for this section

This is the one item in this handoff that is not a measurement gap but an
**analysis** gap, and it is the single thing standing between the current
repo state and a citable security paragraph for Rubato-128L:

- The parameter set actually implemented and benchmarked is Rubato-128L:
  **n=64, r=2, q=0x1fc0001, σ≈1.6357** (`cipher/rubato.go` lines 78-86).
- Grassi et al. (CRYPTO 2023, eprint 2023/822, §6.1/7.1, p.22) give a
  key-recovery attack breaking five of the six Rubato family members for
  ≥25% of modulus choices. For Rubato-128L specifically, the paper states
  the attack's bound **"cannot be established."**
- The only claim this repo currently supports is: **Rubato-128L as
  implemented above is NOT COVERED BY the Grassi et al. published bound.**
  That is a strictly weaker claim than "Rubato-128L is secure." No
  independent security re-derivation for this exact parameter set has been
  performed anywhere in this repo.
- Full statement and citation: `docs/spec.md` §8.8.1;
  `PPFDaaS_REMEDIATION_PLAN.md` §7.2 addendum.

**For the paper: write "not covered by the published attack bound," not
"secure."** If the paper's security section needs the stronger claim, that
requires either an independent cryptanalytic argument (out of scope for
this repo as it stands) or reframing Rubato-128L strictly as a performance/
feasibility comparison point against HERA-16, not as a proposed production
cipher choice — which is exactly how `PPFDaaS_REMEDIATION_PLAN.md` §7.2 and
`cipher/rubato.go`'s own package doc already frame it.
