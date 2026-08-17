# Research Findings v3 — Block B (Transciphering) Intelligence Update

Date: 2026-07-01. Investigation only — no code changes, no benchmarks run as part of producing this document. **Supersedes nothing in v1 (2026-06-22) or v2 (2026-06-30); it resolves/corrects specific v2 items and closes one source-readable gap.** Inherits and does not contradict `AUDIT.md`, `PROJECT_STATE.md`, `docs/spec.md` §5.4/§5.7/§5.8/§7/§8, `RESEARCH_FINDINGS.md` (v1), and `RESEARCH_FINDINGS_v2.md` (v2).

Epistemics legend (unchanged from v1/v2): **[CONFIRMED-RAN]** verified by executing something this session. **[CONFIRMED-SOURCE]** verified against the library's actual source / official docs / a paper, cited. **[UNVERIFIED]** plausible, not checked — flagged, not asserted.

**What this pass closed that v2 did not:** (1) `ckks_fv/rtf_params.go` direct file read [CONFIRMED-SOURCE], closing the CoeffsToSlotsModuli UNVERIFIED gap; (2) full Presto §V read (HTML version) confirming the paper's scope [CONFIRMED-SOURCE]; (3) exhaustive mirror check for RtF Table 5, upgrading "not yet fetched" to "dead end." Did NOT run any benchmarks.

---

## RESOLUTION LEDGER — v2 open items, status after this pass

| # | v2 open item | Status now | Where |
|---|---|---|---|
| 1 | B1: CoeffsToSlotsModuli for 128as — exact count/bit-widths | **CLOSED → [CONFIRMED-SOURCE]** (rtf_params.go, line 520–527: 4 × 58-bit) | §B1 |
| 2 | B1: KeySwitchModuli count for 128as | **CORRECTED** — v2 (session summary) said 5 primes; actual file shows **4 primes** × 61-bit | §B1 |
| 3 | B5: RtF Table 5 server-side timing rows | **DEAD END** — HTTP 403 on eprint, Springer, ASIACRYPT slides, eprint 2025/669, eprint 2025/071; upgraded from "not yet fetched" to "no accessible mirror found" | §B5 |
| 4 | B5: Presto §V software-baseline recoverable as server-side timing | **WRONG — CORRECTED** — Presto §V measures CLIENT-SIDE HERA stream-key generation, not server-side HE evaluation; v2 §B5's "×3–5" claim is retracted | §B5 |
| 5 | B3: r7i cloud SKU starting tier | **UPDATED** — r7i.2xlarge (64 GiB) is bare-minimum-with-no-margin given ~60 GB RAM anchor; real starting tier is r7i.4xlarge (128 GiB) | §B3 |

---

## BLOCK B — Transciphering break-even (v3 addenda only; all v2 text still stands except corrections below)

### B1. CoeffsToSlotsModuli for 128as — CLOSED [CONFIRMED-SOURCE]

**[CONFIRMED-SOURCE]** Read `ckks_fv/rtf_params.go` directly from `github.com/KAIST-CryptLab/RtF-Transciphering` (branch `master`, commit HEAD, cloned `--depth=1 --sparse` and deleted after read; no build, no run). `RtFHeraParams[3]` ("128as", line 479–542):

```
CoeffsToSlotsModuli: CoeffsToSlotsModuli{
    Qi: []uint64{
        0x400000000360001, // 58 CtS
        0x3ffffffffbe0001, // 58 CtS
        0x400000000660001, // 58 CtS
        0x4000000008a0001, // 58 CtS
    },
    ...
}
```

**4 primes × 58-bit = 232 bits total for CoeffsToSlotsModuli.** File: `ckks_fv/rtf_params.go`, lines 520–527.

This closes the single remaining UNVERIFIED gap from v2. HalfBoot depth for 128as is now fully [CONFIRMED-SOURCE]:
- CoeffsToSlots: **4 levels** (4 CoeffsToSlotsModuli primes) [CONFIRMED-SOURCE]
- EvalMod (SineEval, arcsine variant): **11 levels** (3 ArcSine + 2 DoubleAngle + 6 Sine, per SineEvalModuli count [CONFIRMED-SOURCE])
- Total HalfBoot depth: **15 levels** [CONFIRMED-SOURCE]

**Correction to v2/session-summary KeySwitchModuli claim:** The session summary stated "KeySwitchModuli P: 5 primes × 61-bit = 305 bits" for 128as. The actual file shows **4 primes × 61-bit = 244 bits** for 128as (entries 128f and 128s, which share a different parameter set, do have 5 KeySwitchModuli; 128af and 128as both have 4). The 5-prime figure applies to `RtFHeraParams[0]` (128f) and `RtFHeraParams[1]` (128s) only.

**NAMING TRAP [CONFIRMED-SOURCE]:** ALL four `RtFHeraParams` entries (128f, 128s, 128af, 128as) have `LogN: 16` (N = 65,536). The "80" in `BenchmarkRtFHera80as` and the "128" in the HE parameter name refer to different things. "80"/"128" in the benchmark name = symmetric cipher security level (HERA-80 vs HERA-128). "128" in the HE parameter name (128f, 128s, 128af, 128as) refers to the HE parameter family label, not the ring security level. LogN=16 is the same ring for every HERA configuration. Sparse LogSlots=4 (16 active slots) does NOT change the ring dimension and therefore does not rescue RAM: the BSGS CoeffsToSlots butterfly operates on the full N=65,536 ring.

**BSGS Galois key count and RAM [UNVERIFIED — key count derivation not confirmed by code read, only by first-principles estimate]:**
- Baby step = ceil(√(N/2)) = ceil(√32,768) = 182; Giant = 182 → ~364 rotation keys
- Extended basis Q+P for 128as: 8 (ResidualModuli) + 11 (SineEvalModuli) + 4 (CoeffsToSlotsModuli) + 4 (KeySwitchModuli) = 27 primes
- Per Galois key: 2 polynomials × N coefficients × 27 moduli × 8 bytes = 2 × 65,536 × 27 × 8 ≈ 28 MB
- CoeffsToSlots keys alone: ~364 × 28 MB ≈ **10.2 GB** [UNVERIFIED — depends on exact key set generated by the implementation]
- **RAM anchor: ~60 GB [WELL-TRIANGULATED; internal key-count breakdown is UNVERIFIED]** from two convergent sources:
  1. First-principles: ≥10 GB CoeffsToSlots keys + EvalKey + SineEval scratch + live ciphertext buffers = 20–60 GB range
  2. **[CONFIRMED-SOURCE]** arXiv:2409.06422v1 §II: independent empirical report — "HERA requires ~60 GB RAM for 80-bit security"; the authors chose PASTA instead for this exact RAM reason

### B3. Cloud SKU recommendation — UPDATED

**Correction from v2:** v2 recommended `r7i.2xlarge` (64 GiB) as "starting point" for the 80as retry. With a ~60 GB RAM anchor from two convergent sources, 64 GiB is **bare minimum with no margin** — any fragmentation, OS overhead, or memory allocator headroom will exceed it.

**Updated recommendation: r7i.4xlarge (128 GiB) as real starting tier.** On-demand pricing for r7i.4xlarge and r7i.8xlarge NOT yet pulled (v2 only priced 64 GiB). Do not invent a price — pull from Vantage Instances before quoting. Cloud harness scaffolding at `scripts/cloud_transcipher_bench/` covers r7i.4xlarge provisioning — prepped-not-executed.

### B5. RtF Table 5 — DEAD END; Presto §V — CORRECTED

**[CONFIRMED-SOURCE] RtF Table 5 (eprint 2020/1335): UNREACHABLE via all accessible mirrors.**
All paths tried this session returned HTTP 403 or equivalent access denial:
- `eprint.iacr.org/2020/1335` (abstract page and `.pdf` variant)
- Springer/ASIACRYPT 2021 proceedings link
- eprint 2025/669 (SoK: FHE-Friendly Symmetric Ciphers, CHES 2025) — cites Table 5 but does not reproduce its numeric rows
- eprint 2025/071

This is a **dead end**, not a future fetch task. The only paths to Table 5 are: (a) institutional library access (university proxy, ACM/Springer subscription), (b) physical proceedings, (c) author request. This changes the §8.3/§8.5 PENDING reason from "not yet fetched" to "Table 5 inaccessible via all accessible mirrors; need institutional access or cloud execution."

**[CONFIRMED-SOURCE] Presto (arXiv 2507.00367) §V: CLIENT-SIDE only — wrong claim in v2 §B5 retracted.**

v2 §B5 stated: "software-baseline server latency is recoverable as hardware×3–5" from Presto §V's hardware-vs-software speedup ratio. This is **wrong**.

Presto §V (accessed via HTML version, arXiv:2507.00367) measures **HERA stream key generation on the bank's edge device** — a client-side operation on embedded hardware (FPGA, i7-9700 AVX2) to generate the keystream before encryption. The paper's stated focus is "privacy-preserving outsourcing… the overhead of the encryption at the client side."

- Table I, HERA SW (AVX2, i7-9700, 3 GHz): 4,575 cycles = 1.52 µs/block [CONFIRMED-SOURCE]
- Table I, HERA D3 FPGA: 90 cycles = 0.54 µs/block [CONFIRMED-SOURCE]
- 3–5× speedup is FPGA vs AVX2 client-side cipher throughput — not server-side HE evaluation, not HalfBoot, not RtF transcipher latency.

**There is no server-side latency number in Presto.** The only path to `online_transcipher_ms` and `repacking_ms` remains RtF Table 5 (inaccessible via web) or direct cloud execution. Do not parameterize the server-side break-even cells from Presto.

The v2 §B5 sentence containing the "×3–5" claim has been corrected in-place in `RESEARCH_FINDINGS_v2.md`.

---

## BLOCK A — No changes from v2

v2's Block A findings (A3 as recommended primary path, A4 methodology correction, etc.) stand unchanged. This pass was Block B-only.

---

## BLOCKS C, D — No changes from v2

v2's C and D findings stand unchanged.

---

## SYNTHESIS

### Open items after this pass

1. **[DEAD END via web]** RtF Table 5 server-side transcipher/HalfBoot timing — need institutional access or cloud execution.
2. **[UNVERIFIED]** Internal key-count breakdown driving the ~60 GB RAM figure — first-principles estimate of ~364 BSGS keys is plausible but not confirmed by code read (implementation may generate a different key set for sparse LogSlots=4 CoeffsToSlots).
3. **[NOT YET EXECUTED]** Cloud benchmark: `scripts/cloud_transcipher_bench/run_benchmark.sh` on r7i.4xlarge (128 GiB), `BenchmarkRtFHera80as` with peak-RSS monitoring. Scaffolding complete — pending Raghav go-ahead.
4. **[NOT YET PULLED]** On-demand pricing for r7i.4xlarge and r7i.8xlarge — v2 only has 64 GiB pricing.
5. All v2 open items not on this list remain open (A2 N=16384 margin grep, A3 smoke test, B3 spot pricing, C1 sealbench, C2 encrypt_symmetric CPU).
