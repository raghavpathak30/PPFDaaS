# RtF/HERA RAM-reduction research (Phase 7 optimization directions)

Date: 2026-07-28. Investigation only — no HE parameters were changed in the
committed pipeline (`ckks_fv/rtf_params.go` untouched); no benchmarks were
re-attempted at the full ~60 GB scale. This session did, however, run new
**direct instrumented measurements** against the pinned checkout
(`third_party/RtF-Transciphering`, commit
`105fc73115b56f1d6ff357029c7682b19a6d8510`) that materially correct the prior
RAM-anchor breakdown in `RESEARCH_FINDINGS_v3.md` §B1. Read that correction
first — it changes the priority order of every direction below.

Epistemics: **[CONFIRMED-RAN]** = executed this session, exact command
given. **[CONFIRMED-SOURCE]** = verified against the library's actual source
(file+line) or a cited paper/eprint. **[UNVERIFIED]** = plausible, not
checked — flagged, not asserted. Figures attributed to a background research
pass on external literature are marked accordingly; they were not
independently re-verified against the primary PDFs by the author of this
document and should be spot-checked before being treated as load-bearing.

---

## §0. Premise correction — Galois keys are NOT the dominant RAM cost

`RESEARCH_FINDINGS_v3.md` §B1 estimated **~364 BSGS CoeffsToSlots Galois
keys ≈ 10.2 GB**, tagged `[UNVERIFIED — key count derivation not confirmed
by code read]`, using a naive flat-matrix split (Baby=Giant=⌈√(N/2)⌉=182).
This session closed that gap by instrumenting the real code path directly,
and the naive estimate is wrong on both factors.

**[CONFIRMED-RAN]** Command: a scratch `_test.go` added temporarily to
`ckks_fv/` (not committed — deleted after the measurement), calling
`kgen.GenRotationIndexesForHalfBoot(...)` and
`kgen.GenRotationIndexesForSlotsToCoeffsMat(...)` at the REAL, secure
`LogN=16` for `RtFHeraParams[1]` (128s) and `RtFHeraParams[3]` (128as) — the
exact HE parameter objects `BenchmarkRtFHera80s`/`BenchmarkRtFHera80as` use.
This is safe to run on a 15 GB host because these two functions only touch
`map[int]bool` index sets (`computeBootstrappingDFTIndexMap`,
`findbestbabygiantstepsplit` in `keygen.go`/`ckks_encoder.go`) — no key
material is generated.

Result: **20 distinct Galois keys** (18 from HalfBoot's CoeffsToSlots +
4 from SlotsToCoeffs, deduplicated, +1 conjugate), for *both* param sets.
Not 364.

**Why the naive estimate was wrong:** it assumed the flat single-matrix
optimal split `N1=N2=√(dslots)`. The real code (a) biases the split via
`MaxN1N2Ratio=16.0` (set uniformly across every `RtFHeraParams` entry,
`rtf_params.go` lines 336/405/473/541/610) to favor reusing "baby-step"
rotations across the whole multi-level DFT factorization (`findbestbaby
giantstepsplit`, `ckks_encoder.go:557-590`), and (b) — critically — the
relevant BSGS dimension is `dslots`, derived from `LogSlots=4` (16 slots),
**not** the full ring `N/2=32,768`, for the sparse "s"/"as" configs
(`keygen.go:502-506`). This directly contradicts the prior claim in
`docs/spec.md` §8.3 that "sparse LogSlots=4 does NOT reduce RAM: BSGS
CoeffsToSlots Galois keys scale with N, not with LogSlots" — that claim is
now shown to be **wrong for the rotation-index count** (though see below:
it does not follow that overall RAM is small).

**[CONFIRMED-SOURCE + derived]** Per-key size, from `rlwe/keys.go:20-22`
(`SwitchingKey.Value [][2]*ring.Poly`, one `[2]*ring.Poly` pair per RNS
decomposition digit) and `params.go:712-718` (`Beta() = ⌈QiCount/Alpha⌉`):

| Param set | QiCount | PiCount(Alpha) | Beta | Per-key size | 20 keys total |
|---|---|---|---|---|---|
| 128s (idx=1) | 25 | 5 | 5 | 5×2×65536×30×8 ≈ **157.3 MB** | **3.15 GB** |
| 128as (idx=3) | 24 | 4 | 6 | 6×2×65536×28×8 ≈ **176.2 MB** | **3.52 GB** |

This is a *derived* figure (real measured key count × a size formula read
from source), not itself a live-process RSS measurement — but it is
grounded in two confirmed facts, not a guess. It replaces the prior
~10.2 GB estimate with **~3.2–3.5 GB**. The earlier ~28 MB/key figure in
`RESEARCH_FINDINGS_v3.md` also under-counted: it used "2 polynomials" per
key without the `Beta` decomposition-digit multiplier, i.e. it was too small
per-key by the same factor (~5-6x) that the key *count* was too large by
(~18x) — two errors of opposite sign that happened to land the total in a
plausible-looking ~10 GB range by coincidence.

**[CONFIRMED-RAN] The real, previously-unmeasured, unexpectedly large cost
is somewhere else entirely: `GenSlotToCoeffMatFV` (the StC plaintext
decoding-matrix precomputation).** Same instrumented run, using
`runtime.ReadMemStats` before/after each setup phase (`runtime.GC()` between
iterations to isolate live heap):

| Phase | 128s HeapAlloc Δ | 128as HeapAlloc Δ |
|---|---|---|
| `NewKeyGenerator` (ring/NTT context, N=65536) | +78.7 MB | +73.5 MB |
| `GenRotationIndexesForHalfBoot` (index-only) | +0.0 MB | +0.0 MB |
| **`fvEncoder.GenSlotToCoeffMatFV(radix=0)`** | **+4,318.4 MB** | **+3,875.9 MB** |

Peak RSS for the whole measurement (both param sets run sequentially,
`runtime.GC()` between them): **4.08 GB** [CONFIRMED-RAN, `/proc/<pid>/status
VmHWM` polling — `/usr/bin/time` is not installed on this host, same
documented substitution as the toy-correctness harness].

**[CONFIRMED-SOURCE: `mfv_encoder.go:603-628`]** `GenSlotToCoeffMatFV`
iterates `for level := 0; level < modCount; level++` where
`modCount = len(params.qi)` (the **entire residual+sine+CtS chain**, 24-25
for these param sets) and, at *every* level, calls the radix-specific
decoding-matrix generator and immediately `EncodeDiagMatrixT`s it into real
`ring.Poly` plaintext data via `encoder.encodeDiagonal`. For radix=0 (what
`BenchmarkRtFHera80s`/`80as` actually use — confirmed by the benchmark's own
call signature, `radix=0`), `genDcdMatsInOne` (`mfv_encoder.go:668-681`,
hardcoded to `logSlots==4`) returns exactly 2 combined diagonal matrices per
level. So the true replication factor is `modCount(24-25) × 2` — a
structural cost that is **independent of Galois-key tuning** and **retained
for the pipeline's entire lifetime** (`pDcds` is reused by every subsequent
`SlotsToCoeffs` call inside `hera.Crypt`'s post-processing, so it cannot be
freed early). This session did not fully decompose *why* each of those ~48
per-level diagonal-matrix objects costs as much as it does (that would need
reading `encodeDiagonal`/`PtDiagMatrixT` in more depth) — the ~4 GB figure
is measured, the mechanism location is source-confirmed, the byte-level
"why" is **[UNVERIFIED — needs a follow-up heap profile, e.g. `pprof`, to
attribute bytes within `EncodeDiagMatrixT`]**.

**Net effect of this correction:** key material (~3.2–3.5 GB, derived) +
StC precompute (~4 GB, measured) + ring setup (~0.08 GB, measured) ≈
**~7.3–7.6 GB already committed before any HERA-in-BFV evaluation or
HalfBoot/SineEval work begins** — for *one* param set held resident. That is
about half of this host's 15 GB budget from setup alone, which is
consistent with (though does not fully explain) the documented OOM: the
`README.md`-recorded crash happened specifically during the "RtF HERA
Offline Latency" sub-benchmark (the homomorphic HERA round evaluation
itself, `hera.Crypt`), i.e. *after* this ~7.5 GB of setup had already
succeeded. **The dominant contributor to the full ~60 GB anchor remains
unidentified** — it is not primarily Galois keys, and the ~4 GB StC
precompute, while real and larger than expected, still leaves most of the
~60 GB unaccounted for. This is the single most important open question this
pass surfaces, and it reframes Directions 1 and 2 below (both target a
resource that is now shown to be a comparatively small slice of the total).

---

## §1. Galois-key memory (reframed by §0)

- **[CONFIRMED-RAN/derived, §0]** Only ~20 distinct keys, ~3.2–3.5 GB total,
  for the 4-slot ("s"/"as") configs actually used by the paper's benchmarks.
  This is **not** the dominant cost.
- **`MaxN1N2Ratio` is already exposed and tunable** on `HalfBootParameters`
  (`rtf_params.go`), currently `16.0` everywhere
  [CONFIRMED-SOURCE: `mfv_encoder.go:516`, "Optimal maxN1N2Ratio value is
  between 4 and 16 depending on the sparsity of the matrix"]. Lowering it
  would shift the split toward fewer baby-step (shared) rotations and more
  giant-step (per-level, non-shared) ones — i.e. it trades in the *wrong*
  direction for key count, since baby-step rotations are already the
  reused/hoisted ones. **Expected RAM saved: minimal to none, and possibly
  negative** [UNVERIFIED — not empirically swept this session]. Given §0,
  this knob is not worth spending further effort on for RAM specifically.
- **Hoisted rotations** already are the mechanism reducing distinct-key
  count (that is what `findbestbabygiantstepsplit`'s "hoisted" vs "normal"
  split computes) — this is already implemented, not a new lever.
- **On-demand/streamed key generation** (generate a rotation key, use it,
  free it, rather than holding all ~20 resident): plausible engineering
  change, but with only ~3.2–3.5 GB total for all 20 keys, the maximum
  possible saving is bounded by that same ~3.5 GB — a real but now-modest
  upside given the ~60 GB anchor. **Effort: moderate** (would require
  restructuring `BootstrappingKey`/`RotationKeySet` consumption in
  `halfbootstrapper.go` to generate-then-discard per rotation rather than
  the current all-at-once `GenRotationKeysForRotations` call)
  [UNVERIFIED — not attempted].

**Verdict: Direction 1 has limited upside.** The task's framing ("the
dominant cost... ~10 GB from ~364 keys") does not hold up under direct
measurement. Do not prioritize BSGS retuning as the primary RAM lever.

---

## §2. Sparse-secret bootstrapping (reframed by §0)

- **[CONFIRMED-SOURCE: `keygen.go:112-123`, `rtf_params.go` `H: 192` on
  every `RtFHeraParams` entry]** The fork *already* uses a sparse secret
  (`GenKeyPairSparse(H=192)`, `ring.NewTernarySamplerSparse`). This is not
  something to newly enable — it's the baseline.
- **Per-key/per-ciphertext size does not depend on H at all** — it depends
  on `Beta` (`= ⌈QiCount/Alpha⌉`), `N`, and `QPiCount`, none of which are a
  function of the secret's Hamming weight. **Reducing H below 192 would not
  reduce Galois-key memory or StC-precompute memory** (§0's two measured/
  derived costs) **by even one byte** [CONFIRMED-SOURCE: `params.go` —
  `Beta()`/`QiCount()`/`QPiCount()` reference only moduli, never `H`].
  H only affects secret-key *generation* cost (trivial, one ternary poly)
  and the *security level*.
- **Security literature on how sparse is safe** (from a dedicated research
  pass this session — not independently re-verified against the primary
  PDFs, treat as a lead to confirm, not a settled fact):
  - Bossuat, Troncoso-Pastoriza, Hubaux, "Bootstrapping for Approximate
    Homomorphic Encryption with Negligible Failure-Probability by Using
    Sparse-Secret Encapsulation," eprint 2022/024 (ACNS 2022) — the origin
    of the dense+sparse "encapsulation" pattern (a sparse secret only for
    ModRaise) that current Lattigo v6 parameter sets (e.g.
    `N16QP1553H192H32`) also use.
  - Son, Cheon, "Revisiting the Hybrid Attack on Sparse and Ternary Secret
    LWE," eprint 2019/1019 (WAHC'19) — shows a LogN=16, H=64, claimed-128-bit
    parameter set is actually only ~113-bit under a refined hybrid
    dual/MITM attack; a companion paper, eprint 2019/1114, covers similar
    ground.
  - `rtf_params.go`'s `H=192` has **no inline citation**. The best
    inference available is that H was raised specifically to restore the
    margin the Son-Cheon-style attack erodes at lower H — matching current
    Lattigo's own default H=192 at LogN=16 — but this derivation chain is
    **[UNVERIFIED]**, not confirmed against either source directly.
- **Conclusion: going sparser than H=192 is not a RAM lever at all** (it
  doesn't touch either measured cost) **and would require a fresh security
  re-derivation against hybrid-attack estimators** to justify even
  attempting, which the "for the RAM problem" framing does not warrant.
  **Do not pursue.**

---

## §3. HalfBoot depth / SineEval variant

**[CONFIRMED-SOURCE: `rtf_params.go`]** A cheaper SineEval variant already
exists in the same file — it's the difference between the "s"/"f" (non-
arcsine) and "as"/"af" (arcsine) families:

| | 128s/128f (non-arcsine) | 128as/128af (arcsine) |
|---|---|---|
| SineEvalModuli | **8 primes** (`ArcSineDeg: 0`) | **11 primes** (`ArcSineDeg: 7`) |
| ResidualModuli | **12 primes** | **8 primes** |
| HalfBoot depth | 4 CtS + 8 Sine = **12 levels** | 4 CtS + 11 Sine = **15 levels** |
| Total QiCount | **25** (128s) | **24** (128as) |
| KeySwitchModuli(Alpha) | 5 | 4 |
| Beta | 5 | 6 |

The non-arcsine variant does shorten HalfBoot depth by 3 levels, but the
*same file's own parameter choices* correspondingly **lengthen the residual
chain by 4 primes** (12 vs 8) — likely to preserve output precision/level
budget after a shorter bootstrap. Net effect: total `QiCount` is
**25 vs 24 — essentially a wash**, and per-key size is actually slightly
*larger* for 128s (Beta=5 vs 6, but 25 vs 24 moduli — 157.3 MB vs 176.2 MB,
128s slightly smaller) while total 20-key memory is nearly identical
(3.15 GB vs 3.52 GB, §0 table). **Simply switching to the existing
non-arcsine variant does not meaningfully reduce RAM in this codebase's
actual parameter choices** [CONFIRMED-SOURCE, direct comparison of the two
`RtFHeraParams` entries].

A **custom** shortened SineEval (fewer than 8 primes, e.g. a lower-degree
`SinDeg` or fewer double-angle rounds) *without* correspondingly lengthening
`ResidualModuli` could still reduce total `QiCount`, hence `Beta` and
per-key/per-ciphertext size — but this needs: (a) a new parameter set (not
in `rtf_params.go` today), (b) re-deriving what residual budget a downstream
degree-2 eval actually needs (see §5), and (c) re-running the toy
correctness harness (`tools/transciphering/toy_correctness/`) against it to
bound the precision cost. **Effort: moderate — this project already has the
toy-harness infrastructure to validate such a change cheaply (LogN=10, this
session, 1.37s, PASS).** **[UNVERIFIED — not attempted this session; the
correctness-harness path to validate it already exists and is low-risk.]**

---

## §4. Newer/leaner libraries (assessed, not ported)

Findings from a dedicated background research pass this session (not
independently re-verified against primary PDFs by the author of this
document):

- **XBOOT** (Niu, Huang, Yang, Chen, Kong, Hong, Wei, "XBOOT: Free-XOR Gates
  for CKKS with Applications to Transciphering," TCHES 2025, eprint
  2025/074): targets the same transciphering problem (AES/Rasta→CKKS) via a
  "lazy reduction" trick that simulates XOR as CKKS addition, cutting
  multiplicative depth per round (9→3). Its own repository README states
  benchmarks assume **~64 GB RAM and 64 physical cores** — i.e. a similar or
  *heavier* hardware bar than RtF+HERA, not a cheaper one — and flags itself
  as an insecure toy (client-side symmetric encryption not included). Built
  on Lattigo v6.0 (a different library generation from this fork's v2-era
  base) with only 2 commits. **Verdict: does not relieve the RAM problem,
  and would be a full re-port (different construction, different library),
  not an incremental change. Not recommended.**
- **Mainline Lattigo v6** (`tuneinsight/lattigo`): confirmed no `ckks_fv`
  package and no HERA/Rubato transciphering support exists in mainline —
  hybrid BFV/CKKS transciphering remains exclusive to the KAIST fork. v6's
  bootstrapping parameter sets use the same dense+sparse "encapsulation"
  naming pattern (e.g. `H192H32`) as the security literature in §2, not a
  new memory-reduction scheme. No documented Galois-key memory improvement
  specific to a HERA/RtF-style pipeline was found.
- **Reference forks**: `dasec/MT-PRO` (derived from RtF, for biometric
  template protection) is tiny (6 commits, 0 stars) with no stated Go/Lattigo
  version and no RAM discussion — a niche reuse, not a maintained
  alternative. No other actively-maintained "ckks_fv on modern Lattigo" port
  was found.

**Verdict: no external library or fork currently offers a lower-RAM path for
this specific pipeline.** The most promising external lead is *literature*,
not a port — see §5.

---

## §5. Algorithmic — is full HalfBoot needed? (highest-upside, highest-risk)

The production circuit downstream of transciphering is a degree-2/depth-1
logistic layer [docs/spec.md §8, "Run existing CKKS inference circuit
(unchanged from Phase 1–5)"]. Two distinct questions:

**(a) Is the 4-slot ("as") sparse-packing choice itself already a "thin
bootstrap"?** Yes, partially — `genDcdMatsInOne` (§0, `mfv_encoder.go:668`)
is a special-cased, maximally-combined 2-matrix StC specifically for
`LogSlots=4`, and the whole "s"/"as" vs "f"/"af" split in `rtf_params.go` is
precisely a sparse-packing-for-few-active-slots strategy. This is not a new
idea to introduce; it's already what distinguishes the params this pipeline
uses. From the background research pass: "Faster CoeffToSlot and
SlotToCoeff for Sparsely Packed Ciphertexts with Application to CKKS
Bootstrapping" (eprint 2026/1023) reports 1.71×–5.28× speedup on full
bootstrap for sparse packing via a slot-repetition trick, keeping CtS/StC
depth at 1 — directly on-point for this exact scenario, but **not yet
compared against what this codebase's `genDcdMatsInOne` already does**, and
states no RAM figure. [UNVERIFIED — needs a read of the current
`genDcdMatsInOne` against that paper's construction to see if there's
headroom left, or if this fork already captures the same saving by a
different route.]

**(b) Can the downstream degree-2 eval skip part of HalfBoot entirely (e.g.
`repack=false`, or a genuinely "thin" bootstrap that produces fewer than a
full CKKS-packed ciphertext)?** **This is where the correctness assumption
must be flagged explicitly, per this task's instructions:** `HalfBoot(ct,
repack bool)` already exposes a `repack=false` path
[CONFIRMED-SOURCE: `halfboot.go:13`], but the benchmarks this project's own
`RtF_bench_test.go` runs for the 4-slot config call `HalfBoot(ciphertext,
true)` (repack=**true**) [CONFIRMED-SOURCE: `RtF_bench_test.go:296-300`],
specifically because the downstream circuit needs a standard, slot-packed
CKKS ciphertext to feed "the existing CKKS circuit unchanged" — the entire
point of Phase 7 per `docs/spec.md` §8 is to not modify the Phase 1–5
inference path. **Skipping repack would produce a ciphertext in a
non-standard layout that the existing degree-2 evaluator cannot consume
without modification** — so this specific saving is very likely
architecturally foreclosed by the "don't touch the existing CKKS circuit"
design constraint, not a free lunch. **[UNVERIFIED reasoning, but the
constraint is explicit in this project's own spec, not a guess]** — treat
any attempt to skip repack as requiring a change to the downstream circuit
too, which is out of Phase 7's stated scope.

A **genuinely reduced-depth partial bootstrap** (skip some SineEval rounds
because a degree-2 eval doesn't need full [-1,1]-range precision recovery)
is a real research question but was not investigated further this session
— it would require deriving the actual noise/precision budget the degree-2
eval needs and checking whether `SinDeg`/`SinRescal` could be cut
accordingly, which is a natural extension of the toy-correctness-harness
methodology (§3) but was out of scope for a literature/code-reading pass.
**Flagged as the single highest-upside, highest-risk direction — it is also
the least concretely scoped of the six.**

---

## §6. Stopgaps (bridges, not solutions)

Listed honestly as ways to get *a* number sooner, not as RAM reductions:

- **Spot-priced r7i.4xlarge for a single run.** Already the plan in
  `scripts/cloud_transcipher_bench/`. Typical spot discount 60-70% off
  on-demand per that runbook — but exact current pricing was **not pulled**
  this session (needs a live quote before committing spend); interruption
  risk exists for a run that "may take 30–90 minutes" per the script's own
  comment. This bridges to a number faster/cheaper; it does not reduce the
  ~60 GB anchor.
- **Swap/mmap-tolerant execution** (run on a smaller instance with a large
  swapfile, accept degraded latency, just to get *a* correctness/precision
  number rather than a trustworthy latency number). This is explicitly a
  bridge to unblock *measurement*, not performance — any timing captured
  under heavy swap must be labeled as such and excluded from any real
  latency claim; only a pass/fail or a rough peak-RSS figure would be
  trustworthy from such a run. **Not recommended as a source of
  `online_transcipher_ms`/`repacking_ms` numbers** — those must come from
  the real (non-swapping) ~60GB-class run per the existing PENDING
  discipline in `hhe_breakeven.json`.

---

## Ranked recommendation

**The single most promising path is not any of the six directions as
originally framed — it's finishing the diagnostic §0 started: locate where
the *rest* of the ~60 GB anchor actually goes**, since Galois keys (~3.2–3.5
GB, derived) and the StC precompute (~4 GB, measured) together account for
only ~7.5 GB of it, leaving the majority — and the actual OOM trigger,
which happened during `hera.Crypt`, i.e. the homomorphic HERA-round
evaluation itself, not key setup — completely unlocalized.

**Expected working set if this is confirmed to be the HERA-in-BFV evaluation
itself:** unknown — **[UNVERIFIED, needs empirical test]**. This is exactly
the honest answer the "no fabricated numbers" rule requires: nobody has
measured it yet.

**The one empirical step that would confirm/deny this:** instrument
`benchmarkRtFHera` (or a copy of it) with `runtime.ReadMemStats` /
`/proc/<pid>/status VmHWM` checkpoints **between** (a) key generation
completing, (b) `hera.EncKey` completing, (c) each of the 4 HERA rounds
inside `hera.Crypt` completing, and (d) `SlotsToCoeffs`/`ModSwitchMany`
completing — all *before* `HalfBoot` is even called. This can very likely
still be run on this 15 GB host for the 4-slot config (only 16 ciphertexts
in flight, not the full-coefficient path), since the documented OOM
happened specifically inside this phase and a checkpointed version would at
worst reproduce the same OOM but *tell us which round or which sub-step*
triggers it, rather than dying silently mid-benchmark as the undecorated
`go test -bench` run did. That single instrumented run — not a cloud
rental, not a parameter redesign — is what should happen next, because
every other direction in this document is optimizing a cost that (§0) has
now been shown to be smaller than assumed, while the actual dominant cost
remains a complete unknown.

**What to try first, and why:** run the checkpointed `hera.Crypt`
instrumentation above, on this host, before spending any cloud budget or
attempting any parameter redesign (§3) or key-generation refactor (§1).
It's free, low-risk (same OOM-and-restart worst case as already happened
once), and it is the only step that converts the current
"~60 GB, mostly unattributed" anchor into an actionable, source-grounded
optimization target instead of a guess.
