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
| Full-scale (LogN=16, real params) HERA peak VmHWM 9.54 GB, 73.7s wall | `artifacts/hera_crypt_rss_full_run.jsonl`, `artifacts/hera_crypt_rss_checkpoints.jsonl` |
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
- **No full-scale (LogN=16) measurement for Rubato-128L at all** — only the
  toy-scale (LogN=10) numbers above exist. If you need a full-scale
  Rubato memory/time figure for the paper, it does not exist yet and would
  need to be run, not estimated by scaling the toy number.
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
