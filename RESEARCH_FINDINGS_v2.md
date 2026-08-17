# Research Findings v2 — Verification Pass on the v1 Open Items

Date: 2026-06-30. Investigation only — no code changes, no benchmarks run as part of producing this document. **Supersedes nothing in v1 (2026-06-22); it resolves v1's enumerated open `[UNVERIFIED]` items where reachable and explicitly leaves the rest unresolved with reasons.** Inherits and does not contradict `AUDIT.md`, `PROJECT_STATE.md`, `docs/spec.md` §5.4/§5.7/§5.8/§7/§8, and `RESEARCH_FINDINGS.md` (v1).

Epistemics legend (unchanged from v1): **[CONFIRMED-RAN]** verified by executing something. **[CONFIRMED-SOURCE]** verified against the library's actual source / official docs / a paper, cited. **[UNVERIFIED]** plausible, not checked — flagged, not asserted.

**What this pass had access to that v1 did not:** web search + fetch that successfully reached the RtF paper (eprint 2020/1335, both abstract and PDF body), the RtF framework structure via the ASIACRYPT slides, the Presto HHE hardware-accel paper (arXiv 2507.00367, 2025), the cross-platform SEAL/OpenFHE benchmark paper (eprint 2025/473, the one v1 got a 403 on), Lattigo CKKS package docs, and an independent SEAL/HElib/OpenFHE/Lattigo CKKS benchmark (CSAIDE '25). It did **not** reach: a live AWS spot quote, the exact RtF Table-5 server-side per-config millisecond rows (they sit deeper in the PDF than the fetch surfaced), or `rtf_params.go`'s raw text (GitHub raw path still not directly fetchable from this environment).

---

## RESOLUTION LEDGER — v1 open items, status after this pass

| # | v1 open item | Status now | Where |
|---|---|---|---|
| 1 | A2: exact N=16384/tc128 security-margin bits | **STILL [UNVERIFIED]** — not pulled this pass | §A2 |
| 2 | A3: does Lattigo actually accept N=8192 for this config | **UPGRADED → [CONFIRMED-SOURCE]** (two independent sources) | §A3 |
| 3 | A4: OpenFHE-vs-SEAL standard-table discrepancy genuine or accounting | **PARTIALLY RESOLVED** — reframed; see note | §A4 |
| 4 | A4: eprint 2025/473 methodology | **RESOLVED → [CONFIRMED-SOURCE]**, and it *corrects* a v1 overclaim | §A4 |
| 5 | B1: what drives RtF offline-phase RAM | **STRENGTHENED** — HalfBoot/bootstrap confirmed as the heavy step [CONFIRMED-SOURCE], exact bytes still [UNVERIFIED] | §B1 |
| 6 | B1: smaller toy param set below 80as | **STILL [UNVERIFIED]** — `rtf_params.go` raw text not reached | §B1 |
| 7 | B3: r7i spot pricing | **STILL [UNVERIFIED]** — no live quote reached | §B3 |
| 8 | B4: genuinely lighter HHE alternative than RtF | **RESOLVED** — a real survey now exists to consult (SoK CHES2025) [CONFIRMED-SOURCE] | §B4 |
| 9 | C1: published SEAL encrypt-only baseline | **STILL [UNVERIFIED]** — recommendation unchanged: run `sealbench` | §C1 |
| 10 | C2: does encrypt_symmetric cut CPU not just bandwidth | **STILL [UNVERIFIED]** — needs the microbenchmark; reasoning sharpened | §C2 |
| 11 | C3: lane-for-lane CKKS-vs-HERA crossover | **RESOLVED IN SPEC** — §8.10 now does this explicitly (governor-validated); see note | §C3 |

---

## BLOCK A — Cross-library hoisting comparison (§7.5 thesis)

### A1. Why OpenFHE rejects `SetRingDim(8192)` — inherited [CONFIRMED-SOURCE] from v1, unchanged
v1 already confirmed against `ckksrns-parametergeneration.cpp` that the rejection is a **security-standard table lookup** (`StdLatticeParm::FindRingDim`), not a noise-budget estimate or a hardcoded modulus floor. Nothing this pass contradicts that. Treat v1 §A1 as current.

### A2. SEAL-up-to-N=16384 path — STILL the secondary route, one item still open
v1's finding that `rotation_hoisting.cpp` / `bsgs_reduction()` is **ring-agnostic by construction** (rotation-by-step is a property of the cyclotomic automorphism group, independent of slot count) stands and was a local source read — treat as current. The one item v1 flagged and this pass did **not** close:
- **[UNVERIFIED]** exact N=16384/tc128 coeff-modulus ceiling in bits. **Action unchanged:** pull the N=16384 row from `seal/util/hestdparms.h` before quoting a specific margin. This is a 2-minute local grep, not a research task — it was simply out of scope for a web-only pass.

### A3. Lattigo at N=8192 with native hoisting — **UPGRADED to [CONFIRMED-SOURCE]; this is now the recommended primary path with materially higher confidence than in v1**

Two independent confirmations this pass:

1. **[CONFIRMED-SOURCE]** Lattigo constructs `LogN: 13` (N=8192) and claims 128-bit security at that ring dimension — shown concretely in an independent published artifact (encrypted dynamic-control simulation, arXiv 2504.13403): `rlwe.NewParametersFromLiteral` with `LogN: 13, LogQ: [56], LogP: [51]`, with the text stating the chosen parameters "ensure the 128-bit security." This is a real, working N=8192 Lattigo instantiation in the wild, not a docs example.

2. **[CONFIRMED-SOURCE]** The *mechanism* that makes Lattigo behave differently from OpenFHE: per the Lattigo CKKS package docs (`pkg.go.dev/github.com/tuneinsight/lattigo/v5/schemes/ckks` and the v6 bootstrapping docs), `NewParametersFromLiteral` takes a **user-specified** `LogN` + modulus chain and the user is explicitly made responsible for security ("it is up to the user to ensure that the produced bootstrapping parameters are secure"). Lattigo does **not** run an OpenFHE-style `FindRingDim` standards-table gate that overrides the user's ring choice. **This is the architectural reason Lattigo can run the matched-ring (N=8192) comparison OpenFHE's `ParamsGenCKKSRNSInternal` refuses** — and it is itself a citable, precise contrast between the two libraries' parameter-generation philosophies (OpenFHE = standards-enforcing-by-default; Lattigo = user-asserts-security).

3. v1's `RotateHoistedNew` / `RotateHoisted` / `RotateHoistedLazyNew` native-hoisting API finding stands [CONFIRMED-SOURCE]. RtF itself is built on Lattigo (confirmed: the KAIST repo README states "using the lattigo library"), so the hoisting API is exercised in production HHE code.

**Net:** the v1 caveat ("did not confirm Lattigo's auto-generator wouldn't reject N=8192 like OpenFHE") is now substantially resolved — Lattigo's literal-construction path *has no such auto-generator gate*; security is the caller's assertion. The single residual check is trivial and local, not a research blocker:
- **[UNVERIFIED, but now low-risk]** a ~10-line Go smoke test building `ckks.NewParametersFromLiteral` at `LogN=13` with your exact `{60,40,60}`-equivalent chain + scale 2^40, then calling `RotateHoistedNew` once, confirming no error. Given confirmation (1) (an N=8192/128-bit Lattigo param set already exists in published code), this is verification, not discovery.

### A4. Cross-library fairness methodology — **v1 OVERCLAIMED; corrected here**

v1 stated "the accepted methodology in HE benchmarking is explicitly **not** to force identical low-level parameters across libraries" (citing HEBench). This pass shows that is **only one of two live conventions**, and the spec's §7.5.1 framing is defensible under *either*, so the correction strengthens rather than weakens the paper:

- **[CONFIRMED-SOURCE]** eprint 2025/473 (`Cross-Platform Benchmarking of the FHE Libraries`, the paper v1 could not load) explicitly did the **opposite** of HEBench: "To ensure uniformity and reliability of results, **identical parameter settings were applied across all platforms**." So forcing identical params *is* an accepted, published convention — directly contradicting v1's "explicitly not." Both conventions exist in the literature: (a) per-library standards-compliant tuning with parameters disclosed (HEBench), and (b) identical-parameter lockstep (2025/473).
- **Implication for §7.5.1:** the spec's choice to report the ring-dimension mismatch openly rather than hide it is methodologically sound under convention (a). But the existence of convention (b) is *also* why the matched-ring re-test (A2/A3) is worth doing: a reviewer in the (b) camp will regard the N=8192-vs-16384 comparison as simply invalid rather than "disclosed and therefore acceptable." A matched-ring Lattigo result satisfies *both* camps. This is an argument for A3 being worth the effort, not just a nice-to-have.
- **[CONFIRMED-SOURCE]** A directly relevant prior-art comparator now exists for citation: CSAIDE '25 (`Performance Analysis of Leading HE Libraries: SEAL, HElib, OpenFHE, and Lattigo`, dl.acm.org/10.1145/3729706.3729711) benchmarks all four including Lattigo on CKKS, reporting SEAL fastest in CKKS (~0.031 ms/op) and Lattigo slower but competitive. It also references the **T2 Universal Compiler** as a framework for fair cross-library comparison (HElib/Lattigo/PALISADE/SEAL/TFHE). If you want an external anchor for "is my SEAL number library-typical," this is a better citation than the 2025/473 SEAL-vs-OpenFHE-only paper.

### A4-addendum (OpenFHE/SEAL table discrepancy) — reframed
v1 flagged this as a possible second citable finding but [UNVERIFIED] whether genuine divergence or `qBound`/`extraModSize` accounting. This pass does not pull both raw tables side by side, so it remains **[UNVERIFIED]** at the byte level — BUT A3's finding reframes it: the more defensible and certainly-true framing is not "the two libraries' *tables* disagree" (unproven) but "the two libraries' *parameter-generation policies* differ — OpenFHE enforces a standards-table ring-dimension floor by default; Lattigo delegates security to the caller." That policy difference is [CONFIRMED-SOURCE] and is the safer sentence to put in the paper. Reserve the stronger "table values diverge" claim until both raw tables are read.

### Recommended primary path + backup (Block A) — UPDATED
- **Primary: A3 (Lattigo @ N=8192, `RotateHoistedNew`).** Confidence raised from v1. It is the only route that produces a true matched-ring, matched-rotation, library-only delta — satisfying both fairness conventions in A4 — without touching the deployed N=8192 SEAL config and without the scientifically weaker N=16384 detour. The one residual risk (Lattigo literal rejects this exact chain) is now low given a published N=8192/128-bit Lattigo param set exists; de-risk with the ~10-line smoke test first.
- **Backup: A2 (SEAL@16384 vs OpenFHE@16384).** Unchanged from v1: lower code-risk, scientifically weaker (moves SEAL's ring too, leaving a residual N-scaling confound), and only satisfies fairness-convention (a), not (b). Still has the open N=16384/tc128 margin grep.

---

## BLOCK B — Transciphering break-even

### B1/B2. RtF RAM footprint — STRENGTHENED; the heavy step is confirmed, the exact bytes are not
- **[CONFIRMED-SOURCE]** The RtF server-side pipeline is: scale the symmetric ciphertext into FV space → homomorphically evaluate the HERA decryption circuit under FV → `SlotToCoeffFV` → subtract keystream → **CKKS-bootstrap (HalfBoot)** to land back in CKKS slots (RtF paper §RtF-framework intro, eprint 2020/1335; corroborated by the ASIACRYPT 2021 slides' offline/online split and the Presto paper §II). HalfBoot is "a CKKS bootstrapping procedure less computationally taxing than full CKKS bootstrapping" (Presto §II, [CONFIRMED-SOURCE]) — but it is *still a CKKS bootstrap*, and CKKS bootstrapping is the canonical memory-heavy HE operation (large modulus chain, many live ciphertexts during CoeffToSlot/EvalMod/SlotToCoeff).
- This **confirms v1's circumstantial inference** that the offline/bootstrap circuit setup, not slot count, is what drove the OOM in the lightest 4-slot/80-bit config: the bootstrap's cost is dominated by the modulus-chain depth needed for the EvalMod step, which is largely independent of how few slots you populate. So trimming slots will *not* rescue the 15GB host — consistent with what the [CONFIRMED-RAN] OOM already showed.
- **[UNVERIFIED, still]** the exact per-config RAM number and the exact modulus-chain sizes for the 80as parameter set — `rtf_params.go` raw text was not fetchable this pass, and the RtF PDF's Table-5 parameter rows sit deeper than the fetch surfaced. **Action unchanged from v1:** read `rtf_params.go` locally (clone, `cat`, no run) to get LogN + modulus chain, then estimate RAM as (ring dim × chain length × live-ciphertext count during HalfBoot). The Lattigo v6 bootstrapping docs give a concrete anchor for the chain depth: default bootstrap is depth 15 (CoeffsToSlots 4 + EvalMod 8 + SlotsToCoeffs 3), ~821-bit consumption [CONFIRMED-SOURCE] — HalfBoot omits SlotsToCoeffs so ~depth 12, but that is still a very deep chain vs your depth-1 inference circuit, which is exactly why it dwarfs your deployed system's footprint.

### B3. Cloud SKUs — v1's on-demand prices stand; spot still [UNVERIFIED]
v1's [CONFIRMED-SOURCE] on-demand r7i prices (`r7i.2xlarge` 64 GiB ≈ $0.53/hr, `r7i.large` 16 GiB ≈ $0.13/hr) are not re-fetched here; treat as current pending drift. Spot pricing and whether 64 GiB clears the offline-phase OOM both remain **[UNVERIFIED]** and depend on B1's exact-bytes question. Recommendation unchanged: start at `r7i.2xlarge` (64 GiB, 4× this host) for the 80as retry; escalate to 128 GiB only if the 128-bit full-slot config is attempted. **Sharper guidance from B1:** because the binding cost is the HalfBoot chain depth (≈depth-12, ~hundreds of bits of modulus) and not slot count, the jump from 80-bit to 128-bit security raises the modulus chain and therefore RAM **super-linearly, not linearly** — so do not assume 128 GiB is enough for the 128-bit config just because 64 GiB cleared 80-bit. Budget for 128–256 GiB for the 128-bit run and confirm against `rtf_params.go` first.

### B4. Lighter-weight FV→CKKS alternatives — RESOLVED: a real survey now exists, and it confirms RtF is the relevant bridge
- **[CONFIRMED-SOURCE]** v1 could not do "a real survey" of HHE alternatives. One now exists and should be cited: **SoK: FHE-Friendly Symmetric Ciphers and Transciphering (CHES 2025, eprint 2025/669)**, with companion repo `AntCPLab/awesome-transciphering`. It categorizes FHE-friendly ciphers into four groups and evaluates each feasible cipher×transciphering combination. This is the authoritative map of the space and resolves v1's single-most-important open Block-B item ("is there a genuinely lighter alternative than RtF").
- **[CONFIRMED-SOURCE]** The SoK confirms the landscape v1 sketched: for **CKKS** targets specifically, RtF (Cho et al. CHK+21, real→BFV→CKKS) and the Aharoni et al. ADE+23 "AES-direct-to-CKKS" approach are the two CKKS-compatible families; CGGI/TFHE-targeting transciphering (Trivium/Kreyvium/Elisabeth-style) is a *different* HE target, not a drop-in for a CKKS inference pipeline. So RtF remains the appropriate bridge for *this* project's CKKS circuit — v1's conclusion holds, now backed by a survey rather than asserted.
- **[CONFIRMED-SOURCE]** v1's PEGASUS conclusion stands and is corroborated by the RtF paper itself, which compares against PEGASUS as a *different* construction (LWE↔RLWE repacking for non-polynomial eval), not a lower-RAM RtF substitute. PEGASUS does not change the §8 trust model.
- **One new candidate worth a look but [UNVERIFIED] for RAM:** Rubato (eprint 2022/537), the KAIST follow-up to HERA, is implemented in the *same* `ckks_fv` codebase and is reported (Presto paper) as lower multiplicative depth than HERA via its Feistel nonlinearity. Lower decryption-circuit depth → potentially smaller HalfBoot chain → **possibly lower RAM than HERA-80as**. This is the one concrete lead for fitting a real run into less memory that v1 did not surface. Whether it actually reduces the offline-phase footprint enough to matter on a 15GB host is **[UNVERIFIED]** — but it is in the repo you already cloned, so it costs nothing to check the `rtf_params.go` Rubato rows alongside the HERA ones.

### B5. Fallback literature numbers for a break-even model — PARTIALLY RESOLVED
- **[CONFIRMED-SOURCE] client-side number (this is the strong one):** RtF+HERA at 128-bit security achieves **1.6 µs latency and 21.7 MB/s throughput on the client side, 9085× faster and 17.8× higher-throughput than CKKS-only** (eprint 2020/1335 abstract + §1.1, verified against the PDF). Ciphertext expansion ratio 1.23–1.54, ≥23× smaller than symmetric-CKKS-only. These are directly usable, properly citable client-side anchors for the break-even model — and they *quantify* the client-side advantage your §8.10 is reasoning about, from the originating paper.
- **[CONFIRMED-SOURCE, DEAD END] server-side number (the one the break-even actually pivots on):** RtF Table 5 (eprint 2020/1335) is **unreachable via all accessible mirrors** — HTTP 403 on eprint, Springer/ASIACRYPT 2021, eprint 2025/669, eprint 2025/071. This is not "not yet fetched" but a confirmed dead end via web access; institutional library or physical proceedings access is required, or direct cloud execution of the benchmark. Presto (arXiv 2507.00367) §V **does NOT provide server-side HE latency** — it measures HERA stream-key generation on the bank's client-side edge device (AVX2 i7-9700 and D3 FPGA). **Retraction of earlier v2 claim:** the v2 statement that "software-baseline server latency is recoverable as hardware×3–5" from Presto is wrong. The 3–5× speedup Presto reports is FPGA vs AVX2 client-side cipher throughput, not server-side HE evaluation or HalfBoot latency. Do not parameterize `online_transcipher_ms`/`repacking_ms` from Presto. [Correction date: 2026-07-01; full evidence in RESEARCH_FINDINGS_v3.md §B5.] Until cloud execution, the server-side break-even cells stay **PENDING-MODELED-INPUT-MISSING** with no accessible literature source.

---

## BLOCK C — Client-encrypt-dominates finding

### C1. Is ~3ms client CKKS encrypt plausible? — unchanged, recommendation stands
v1's [CONFIRMED-RAN, inherited] e2e numbers stand. No published isolated-encrypt baseline at matched params was pulled this pass either; the **1.6µs RtF client figure above is not a comparator** (it's amortized symmetric-encrypt throughput, not a single CKKS public-key encrypt). Recommendation unchanged and cheap: run SEAL's own `sealbench` encrypt micro at N=8192 and cite that directly rather than hunting literature. Remains the right call.

### C2. `encrypt_symmetric` — bandwidth confirmed, CPU effect still open
v1's [CONFIRMED-SOURCE] reading of `7_serialization.cpp` stands: seeded ciphertexts store a PRNG seed instead of the second polynomial → ~2× serialized-size reduction (matches the repo's measured 1.998–1.999×), and SEAL's own docs claim only a *size* benefit, never a CPU benefit. This pass adds nothing that changes it. The plausible-but-unproven CPU saving (symmetric encrypt may skip the `pk1·u` polynomial multiply) remains **[UNVERIFIED]** and still needs the direct `encrypt` vs `encrypt_symmetric` microbenchmark before any "also cuts client CPU" claim. Note the interaction with §8.10: even if it *does* cut client CPU, §8.10(c) shows client-encrypt only dominates at high occupancy where HERA's own cost has overtaken CKKS's — so a client-CPU win from seeded ciphertexts would help the *CKKS* side of the comparison, not the transciphering motivation. Worth being precise about which side it helps.

### C3. Does this strengthen the transciphering motivation? — RESOLVED in the spec since v1
v1 flagged that the raw numbers did not obviously favor HERA at every lane count and that the comparison needed to be worked out explicitly. **This has since been done:** `docs/spec.md` §8.10 (governor-validated, `c3_client_server_comparison.py` → `artifacts/c3_comparison.json`) now lays out the lane-by-lane CKKS-encrypt-vs-HERA-encrypt crossover explicitly and reaches the honest conclusion v1 anticipated: the motivation is **bandwidth-first**, with a client-CPU advantage that holds at low occupancy and *inverts* at the system's steady-state batching target (lanes=16, where HERA-16 encrypt at 4821µs exceeds CKKS encrypt at 4181.8µs). So v1's C3 worry is now closed by the spec itself — the "client-encrypt dominates, transciphering wins" framing is correctly **not** claimed as a clean win at every operating point. No further research needed here; §8.10 is the resolved version of this item.

---

## BLOCK D — Reproducible timing manifest
No change from v1. v1's [CONFIRMED-SOURCE] minimum bar (performance governor, turbo disabled, core pinning via taskset/isolcpus, SMT-off recommended) stands; ASLR/NUMA effect on this workload remains [UNVERIFIED] and plausibly small. v1's D2 recommendation — prefer the local ROG box with performance governor + turbo off + taskset over a generic noisy cloud vCPU for *citable* numbers, reserving cloud purely for the Block B RAM problem — stands and is reinforced by B1: the cloud instance is for *memory capacity to complete a run*, not for clock-stable latency, so co-tenant jitter there does not contaminate any cited latency number.

---

## SYNTHESIS (updated)

### 1. What changed the decision since v1
- **Block A got more attractive and lower-risk.** A3 (Lattigo matched-ring) went from "plausible, one scary untested caveat" to [CONFIRMED-SOURCE] feasible with only a 10-line smoke test outstanding — because Lattigo's parameter construction provably has no OpenFHE-style ring-dimension gate (security is caller-asserted), and a published N=8192/128-bit Lattigo param set already exists. Additionally, A4 shows a matched-ring result satisfies *both* fairness conventions, whereas the current N-mismatched result only satisfies one — so A3 closes a reviewer-objection class, not just a curiosity.
- **Block B got better-understood but not easier.** The heavy step is confirmed to be the HalfBoot CKKS-bootstrap (deep modulus chain, ~depth-12), which means (a) trimming slots won't fit it on 15GB — a bigger box is genuinely mandatory, and (b) the 80→128-bit jump scales RAM super-linearly, so budget 128–256 GiB for the 128-bit config, not 128. The client-side break-even anchor (1.6µs, 9085×) is now citable from the source paper; the server-side anchor is still missing but now has named tables to pull rather than a paywall. **[V3 CORRECTION 2026-07-01: "named tables" clarification — RtF Table 5 is confirmed 403-unreachable on all accessible mirrors (dead end, not future fetch); Presto §V is CLIENT-SIDE only (no server-side latency). Both routes are exhausted. See RESEARCH_FINDINGS_v3.md §B.]**
- **One free lead for Block B:** Rubato (same `ckks_fv` repo, lower decryption depth than HERA) may have a smaller HalfBoot footprint — worth checking its `rtf_params.go` rows when you read the file for HERA anyway.

### 2. Which lever first — unchanged from v1, now with more conviction
**Block A (Lattigo matched-ring) first.** It is lower-effort, has no cloud spend, no super-linear-RAM uncertainty, produces a result that satisfies both benchmark-fairness conventions, and its single residual risk is now a 10-line smoke test rather than an open research question. Block B remains the higher-ceiling but higher-variance lever; pursue it second, and before spending any cloud money, do the two zero-cost local reads first: (i) `rtf_params.go` HERA *and* Rubato rows for exact chains/RAM estimation, (ii) RtF Table 5 + Presto §V for the server-side latency anchor. **[V3 CORRECTION 2026-07-01: (i) done — rtf_params.go read, 128as fully characterized, see RESEARCH_FINDINGS_v3.md §B1; (ii) exhausted — Table 5 is 403-dead-end on all mirrors, Presto §V is client-side only. No server-side latency anchor exists in accessible literature. WAHC 2026 cycle decision: server-side cost deliberately not measured this cycle — see docs/spec.md §8.3.]**

### 3. [UNVERIFIED] items still to resolve before committing effort (the short list that actually remains)
1. (A2) N=16384/tc128 modulus ceiling in bits — local grep of `seal/util/hestdparms.h`. Trivial.
2. (A3) Lattigo `NewParametersFromLiteral` at LogN=13 / your exact chain / scale 2^40 accepts without error — 10-line Go smoke test. Low-risk given a published N=8192 Lattigo param set exists.
3. (B1) Exact RtF 80as (and Rubato) modulus chain from `rtf_params.go` → first-principles RAM estimate. Local clone + cat, no run.
4. ~~(B5) RtF Table 5 + Presto §V server-side transcipher/HalfBoot latency rows~~ **[CLOSED-DEAD-END 2026-07-01: Table 5 is 403-unreachable on all accessible mirrors; Presto §V is CLIENT-SIDE only, no server-side rows exist. Break-even server cells stay PENDING. See RESEARCH_FINDINGS_v3.md §B5.]**
5. (B3) Live r7i spot quote + whether 64 GiB clears the 80as offline OOM — depends on (B1).
6. (C1) SEAL `sealbench` isolated-encrypt number at N=8192 — one command, removes the C1 [UNVERIFIED] entirely.
7. (C2) `encrypt` vs `encrypt_symmetric` CPU microbenchmark — only needed if the paper wants to claim a client-CPU (not just bandwidth) benefit; note §8.10 shows that benefit would help the CKKS side, not the transciphering motivation.

Items fully resolved since v1 and needing no further action: A1 (inherited), A3-mechanism, A4-methodology (and the v1 overclaim corrected), B4-survey, C3 (resolved in spec §8.10).
