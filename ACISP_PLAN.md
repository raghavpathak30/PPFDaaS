# ACISP 2027 (Round 1) submission plan: Coefficient-Native Packing (CNP)

Written 2026-11-22 (Sunday). Audited `main` at `b34b4d2` (same as `origin/main`), with a clean tree. Read-only: no code or doc was changed.

**This file names you, your GitHub handle, the DGX host and your institution, so it can identify you. Keep it untracked. Never put it in an anonymised snapshot.**

Deadline: Mon 30 Nov 23:59 AoE, which is Tue 1 Dec 17:29 IST. The paper may be at most **20 pages including the bibliography** in unmodified llncs, and it is double-blind.

Tags: **IN-REPO** means a committed file backs the number. **OUT-OF-REPO** means it was measured, but the file sits only in `~/dgx_results/`. **UNMEASURED** means it was never run. **ARITHMETIC** means I derived it from the fields cited; it is not a measurement.

---

## 0. The five findings that matter

1. **Pipeline B has never been compared with a fair baseline at secure parameters.** At LogN 16, CNP-B has been compared only with the HalfBoot lower bound, and that run is not committed. The fair FV-slot baseline has run only at toy scale (LogN 10 and 12, laptop). There, CNP-B wins **online** by about 2.3x, but **loses offline and total** by 1.11x (HERA) and 2.18x (Rubato) (`reports/bfv_slot_baseline_report.md:384-389`). One DGX session fixes this: about 3.5 h unattended plus about 1 h attended.
2. **Both pipelines' secure-parameter headline numbers exist only outside the repo.** They are in `~/dgx_results/cnp-s{0,1}` (CNP-A) and `~/dgx_results/cnpb-*` (CNP-B), all from 2026-10-28, image `d7ea62b`. Your professor's rule that every number traces to the repo is broken until these are committed. That takes about 1 h. The CNP code has not changed since `d7ea62b`: `git diff --stat d7ea62b main` over the CNP paths touches only `scripts/dgx/run_transcipher.sh`, which gained the `slot` target. So the runs still describe the current code.
3. **The GitHub repo cannot be anonymised by linking to it.** Your name or handle is in `LICENSE`, `go.mod`, the Docker labels, every commit author and both slide decks. Submit with no repo link (recommended), or with a separately built anonymised snapshot (section 3).
4. **Page budget.** Nothing in this repo assumes PQCrypto's 30-pages-plus-references rule; I grepped for `pqcrypto`, `30 pages`, `page limit` and `llncs`, and found nothing. The venue framing in the repo is **WAHC 2026** (`PROJECT_STATE.md:292`, `docs/spec.md:4296`, `docs/spec.md:4621`, `PPFDaaS_REMEDIATION_PLAN.md:3,487`). So the 30-page assumption must live in the Overleaf draft. Moving from 30 pages plus references to 20 pages including references is a cut of roughly 40%. Appendices don't count, but the body must stand alone, so the slot-baseline table, the threat model and the parameter table have to stay in the body.
5. **ACISP reviewers are cryptographers, and model privacy is not argued.** Neither arm floods noise (`reports/bfv_slot_baseline_report.md:316`, UNVERIFIED). The mask hides the non-output coefficients, but the output coefficient's decryption noise still depends on the weights. CNP-B's final noise headroom at LogN 16 is 21.77 bits (OUT-OF-REPO), which is too little for 40+ bits of flooding without changing parameters. This has to be scoped in writing; it cannot be fixed in 9 days.

---

## 1. Measurement gaps

### 1a. Pipeline B: is the CNP ratio against a fair baseline?

**At LogN 16, no.** The only LogN 16 comparison is CNP-B against the HalfBoot arm. That arm is a **lower bound** (Sub + HalfBoot + `2x+1`, with no LR, no slot rotations and no bias; `baseline_note` in each DGX JSON). From the uncommitted DGX JSONs (`per_transaction_ms.total` and `baseline_lower_bound_per_transaction_ms.total`):

| Run (2026-10-28, lnmdgx1, `d7ea62b`) | CNP-B online, ms/txn | HalfBoot lower bound, ms/txn | Online ratio (ARITHMETIC) |
|---|---|---|---|
| `cnpb-hera-s0` | 0.0643 | 119.63 | 1,861x |
| `cnpb-hera-s1` | 0.0693 | 116.23 | 1,677x |
| `cnpb-rubato-s0` | 0.0610 | 135.99 | 2,228x |
| `cnpb-rubato-s1` | 0.0681 | 138.03 | 2,027x |

**Online is a small share of the cost.** Offline per transaction (ARITHMETIC, from `per_output_ct_ms`, `outputs_per_crypt` and 256 lanes per output ciphertext):

| Cipher | Keystream Crypt, ms/txn | StC, ms/txn | Offline total, ms/txn |
|---|---|---|---|
| HERA (s0) | 216,322 / 4,096 = 52.8 | 3,337 / 256 = 13.0 | about 65.9 |
| Rubato (s0) | 149,804 / 15,360 = 9.75 | 3,941 / 256 = 15.4 | about 25.1 |

The HalfBoot arm shares that offline path (`offline_note`). So against HalfBoot, CNP-B's **total** advantage is about **2.8x for HERA and about 6.4x for Rubato**, not about 2,000x (ARITHMETIC). This matches the toy-scale 2.85x to 6.75x at `reports/bfv_slot_baseline_report.md:251-254`.

**Against the fair slot baseline: toy scale only.** That is LogN 10 and 12, laptop on AC, `powersave` (`artifacts/bfv_slot_baseline/laptop_20261103T205043Z/replicate_ranges.json`, IN-REPO):

| Metric | HERA, LogN 12 | Rubato, LogN 12 |
|---|---|---|
| Online | CNP-B 2.27x faster (2.05 to 2.42) | CNP-B 2.29x faster (2.11 to 2.29) |
| Total | **Slot 1.11x faster** (1.10 to 1.18) | **Slot 2.18x faster** (2.15 to 2.18) |

**Expect the same direction at LogN 16. This is my ESTIMATE, not a claim.** StC is 13.0 to 15.4 ms/txn of CNP-B's offline cost, and the slot path skips StC entirely. Write the B claim now so that it survives "CNP-B wins online, slot wins total". Don't wait for the run to decide the framing.

### 1b. The `bfv-slot-baseline` branch: how complete it is, and what remains

- `git diff --stat main...bfv-slot-baseline` is **empty**: the branch is fully merged (merge `b48fe24`), with 0 commits ahead of `main`, and `origin/bfv-slot-baseline` exists. What the branch added, measured from its fork point (`git diff --stat d7ea62b bfv-slot-baseline`): 184 files and +25,517 lines in total. Leaving out `artifacts/`, that is 11 files and +2,689 lines over 11 commits (`db156d7` to `2849d7b`).
- **What it implements** (CONFIRMED-SOURCE):
  - `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/rtf_slot_test.go` (1,094 lines) is a slot-domain LR on the RtF path. It uses the same `128af` parameters, t, (8,6) quantisation and mask scheme as CNP-B, with no StC, no HalfBoot and no CKKS. Sub and pmul run at level 1 because noise forces it (`report:94-96`). Then it modswitches to level 0 and does rotate-and-sum (4 rotations for HERA, 18 for Rubato).
  - `rtf_slot_unit_test.go` (272 lines) covers layout, key-count and scale-up unit tests.
  - `scripts/dgx/run_transcipher.sh` gains the `slot` target (`:107`). `tools/transciphering/docker-entrypoint.sh:41` handles it.
  - `scripts/cnp_b/score.py --arm slot`, `scripts/bfv_slot_baseline/{run_laptop_compare,replicate_ranges,stc_rotation_keys}.py` and `tests/test_bfv_slot_compare.py`.
- **Done:**
  - Correctness at LogN 10 on all 56,962 rows: HERA is bit-exact, and Rubato's ΔAUC is −2.5e-5 (`report:66-79`, IN-REPO).
  - Same-session laptop timing at LogN 10 and 12.
  - Peak RSS at LogN 12 and 14.
  - The runbook, `docs/DGX_RUNBOOK_slot.md`, is merged.
- **Never done:**
  - Any run at LogN 16.
  - Any build of the image from a SHA that contains `slot`. The DGX image built on 28 Oct was `d7ea62b`, which predates `slot`.
  - The wrapper's `slot` target has only been exercised against a stub docker (`report:332`).
- **Your professor's run:** I found no `slot_cnp_results_*.tar.gz` and no `results/slot_*` anywhere under `~`, and I can't see your professor's home directory on the DGX. **Ask them before you launch, so the shared machine doesn't do the same work twice.**
- **What it takes to get a number:**
  1. Follow runbook sections 0 to 3: preflight, clone, unpack `cnpb_data.tar.gz` (29 MB, `~/cnpb_data` on your laptop), and build. About 0.5 h attended. The DGX needs github.com and the Go module proxy; the 28 Oct build proved they are reachable from campus. Your 50 GB home quota is fine, because the image lives in Docker's root directory. Check the `df` line in the preflight.
  2. Round 1, `slot`, HERA on socket 0 and Rubato on socket 1: **ESTIMATE 1.5 h** (`runbook:94`).
  3. Round 2, `cnp`, the same pairing, **in the same session** so the ratio is defensible (`docs/MEASUREMENT_PROVENANCE.md:13-18`): **MEASURED 1 h 26 min and 47 min, so allow 2 h** (`runbook:104`).
  4. Copy the results to the laptop. Score there, because the host python3 has no numpy: `python3 scripts/cnp_b/score.py --arm slot|cnp ...` (`report:364-367`). Commit the runs and compute the online, offline and total ratios. About 1.5 h attended.
  5. Optional rounds 3 and 4, the socket-swapped repeat: commands at `report:350-362`, about 3.5 h more, unattended, overnight.
- **Risks:**
  - The slot path has never run at LogN 16.
  - The memory ESTIMATE is about 11 GB for HERA and about 20 GB for Rubato (`report:373-374`). That fits easily.
  - Headroom at LogN 16 is UNVERIFIED, but it trends at 24 to 27 bits against a 5-bit gate (`report:120-126`).
  - Earlier jobs started with load average 12 to 25, so use the socket check (`runbook:43`).

### 1c. Every claim the paper is likely to make

The hours are attended work plus unattended DGX time.

| # | Claim | Status | What is missing | DGX? | Hours | Reviewer if it stays as is |
|---|---|---|---|---|---|---|
| B1 | CNP-B against a fair slot-packed LR at LogN 16 (online, offline, total) | UNMEASURED | The slot run and a same-session CNP-B rerun (1b) | Yes | 1.5 attended + 3.5 unattended | "The ~2,000x is against a strawman lower bound. The authors' own toy data show the slot baseline winning on total time." Likely reject. |
| B2 | CNP-B at LogN 16: online 0.061 to 0.069 ms/txn; HERA bit-exact on 56,962 rows; Rubato ΔAUC +4.8e-5 and −6.3e-5; headroom 21.77 bits; 47 StC keys; peak RSS 61.8 to 65.1 GB | OUT-OF-REPO (`~/dgx_results/cnpb-*`, `score-*.txt`) | A commit with a provenance README | No | 1 | Nothing, as long as the paper cites it. **It fails your professor's rule.** |
| B3 | CNP-B against HalfBoot (lower bound) | OUT-OF-REPO at LogN 16; IN-REPO at toy scale | Commit (B2). The paper must label it a lower bound and report total as well as online. | No | 0.5 writing | "A misleading headline ratio." |
| B4 | CNP-B costs: more Galois keys (47 vs 4 to 6), 15 to 16x more download per transaction (4,096 B vs 256 to 273 B), 2.8 to 4.0x the peak RSS of slot | Keys: OUT-OF-REPO (`rotation_key_counts.stc_only`). Download: CONFIRMED-SOURCE. RSS: toy, IN-REPO (`report:404`). | The slot side at LogN 16 (B1) | Yes (with B1) | Included in B1 | "The authors hide the costs." The report itself says to state these (`report:397-406`). |
| A1 | CNP-A against SEAL slot fold, server compute: 7.2 to 7.4x per ciphertext (fold 10,873 to 10,934 µs at 16 lanes, CNP 1,471 to 1,504 µs at 32 lanes), 14.5 to 14.9x per transaction | OUT-OF-REPO (`~/dgx_results/cnp-s{0,1}/cnp_bench.json`; same container and session, performance governor, `OMP_NUM_THREADS=1`) | A commit | No | 0.5 | Not traceable. Fails your professor's rule. |
| A2 | CNP-A end to end over gRPC, p50: 19.9 to 20.0 ms (fold, 16 lanes) against 9.65 to 9.83 ms (CNP), about 2.0x; upload 262,257 B per request for both, so CNP's 32 lanes halve upload per transaction | OUT-OF-REPO (same files) | A commit | No | Included in A1 | As A1. |
| A3 | CNP-A is exact enough: max abs error 3.7e-10 on 56,962 rows; AUC 0.979398 equals plaintext; 0 Galois keys and 0 rotations; masked chi-square p 0.31 pooled; masked weight recovery fails | IN-REPO: `artifacts/errors_cnp.json`, `artifacts/cnp_a_test_numbers.json` (`t4`, `t5b`, `t5c`, `t6_cnp`) | Nothing | No | 0 | Fine. |
| A4 | The slot fold is the right baseline for A | No analysis. Measured alternatives: BSGS 39.3 ms and naive 315.7 ms are both slower (OUT-OF-REPO, same JSON). | A cost-model paragraph on a feature-major slot layout, which also needs no rotations but needs 256 ciphertexts per batch. Analysis, not a run. | No | 1 | "Did you compare against the best slot packing, not just the deployed one?" |
| S1 | Model privacy against the bank comes from masking | Mask tests are IN-REPO (A: `cnp_a_test_numbers.json` `t5`; B: toy JSONs, plus `mask_tests` OUT-OF-REPO at LogN 16). **There is no noise-flooding or circuit-privacy argument** (`report:316`). The fully packed 256x256 Toeplitz leak was never tested (`docs/cnp_gate.md:72`). | Writing: state that the scope is semi-honest, with input privacy plus coefficient hiding, and that noise-based leakage is out of scope. Say why flooding doesn't fit at 21.77 bits of headroom. Cite Li–Micciancio for A (`docs/spec.md` §6.6). | No | 1.5 | At a security venue: "Without noise flooding the decryption noise leaks w; the privacy claim is unsupported." |
| S2 | The parameters are secure: CKKS N=8192 with `{60,40,60}` at tc128; RtF `128af` at LogN 16 | CONFIRMED-SOURCE (SEAL tc128; `ckks_fv/rtf_params.go`) | Cite the HE standard and Cho et al. (RtF) for `128af`. Call LogN 10 to 14 functional checks only. Write "not covered by the Grassi et al. bound" for Rubato-128L, never "secure" (`docs/PAPER_HANDOFF.md:157`). | No | 0.5 | "Are LogN 16 / 128af parameters actually 128-bit?" |
| C1 | Client cost and upload are the same for CNP-B and slot | CONFIRMED-SOURCE, not timed (`report:58`) | Nothing, if it is worded as a structural fact | No | 0 | Fine. |
| C2 | Absolute HERA or Rubato client-encrypt cost, or HHE beating CKKS on client CPU or bandwidth | **Stale**: HERA's client timings were measured at t=2^26 (`docs/MEASUREMENT_PROVENANCE.md:50,60-68`) | Re-measure with the `bench` target, or **cut** | Optional | 1 | Only matters if the paper claims it. **Recommend cutting.** |
| R1 | Run-to-run variability at LogN 16 | Each cipher ran on both sockets once (s0 and s1); see the spread in A1 and B2 | The optional socket-swapped slot repeat (1b, step 5) | Yes | 0.3 + 3.5 | "n = 1 per configuration." Medium risk. |

---

## 2. Correctness and consistency risks

| # | Number | Disagreeing sources | Cite this one |
|---|---|---|---|
| K1 | Whether CNP-B has run at LogN 16 | `docs/cnp_b.md:6` says "Nothing here has been run at LogN 16 yet", and `reports/bfv_slot_baseline_report.md:354` repeats it. `~/dgx_results/cnpb-*` is four LogN 16 runs from 2026-10-28. | The DGX JSONs, once committed. Then fix `cnp_b.md:6`. |
| K2 | **Upload per transaction** | `docs/cnp_b.md:29`: "3.2 KB/txn bit-packed, 4 KB as uint32". That is wrong by 4x, a per-ciphertext figure written as per-transaction (`report:180-190`). `report:153` and the DGX `wire_bytes`: 800 B bit-packed, 1,024 B as uint32. `README.md:324` and `docs/spec.md:4543`: 1,052 B. | `wire_bytes.upload_bytes_per_transaction_uint32_wire = 1024` (DGX JSON). **1,052 = 1,024 + 28 B of AEAD framing** (`OnlineCiphertextBytes(n)=4n+28`, `docs/MEASUREMENT_PROVENANCE.md:79`). State the AEAD bytes separately. Never use 3.2 KB. |
| K3 | Where the bias is added | `docs/cnp_gate.md:61`: "The bank adds the bias in plaintext after decrypt." `docs/code_and_data_flow.md:9,63,158`: the client adds it. The code adds it **on the server, inside the mask**: `rtf_cnp_test.go:241` (B), `rtf_slot_test.go:232`, `vendor_server/include/cnp_codec.h:183` `build_output_mask` (A). | The code lines, and `reports/paper_fact_checks.md:61-63`. |
| K4 | **Value margin against t/2** at (8,6) | `docs/cnp_gate.md:51-52`: 5.62 bits, worst-case ratio 0.045 (HERA) and 0.047 (Rubato), both **without the bias**. `reports/paper_fact_checks.md:52-57`: 6.02 bits empirical with the bias; worst case with the bias 889,976, which gives **4.23 bits** (HERA). | `paper_fact_checks.md` §2, because the bias is added on the server, so the bound must include it. Rubato's worst case with the bias is not in any file. ARITHMETIC: 779,365 + 137,080 = 916,445, which gives about 4.18 bits. Commit that sum before citing it. |
| K5 | "Headroom" names two different things | Noise headroom: 21.77 bits (DGX, LogN 16) and 22.05 to 22.28 bits (toy, `report:124-125`). Value margin: 4.2 to 6.0 bits (K4). | Use two names in the paper, for example "noise budget" and "plaintext margin". |
| K6 | StC Galois keys at LogN 16 | `report:172`: 47 keys, 8,279,556,096 B, **ESTIMATE**. DGX JSON: `rotation_key_counts.stc_only = 47`, measured. `setup_retained_heap_kb.stc_rotation_keys = 8,085,916` KB, which is about 8.28e9 B and consistent. | The DGX JSON. The ESTIMATE label in the report is now stale. |
| K7 | CNP-B against HalfBoot | `report:251-254`: 2.85 to 6.75x total (toy). DGX: 1,677 to 2,228x online and about 2.8x / 6.4x total (ARITHMETIC, 1a). | Report both, each with its scope. Never "~2000x" alone. |
| K8 | Slot report status | `reports/bfv_slot_baseline_report.md:3`: "Not pushed. `main` not touched." `:330`: "the DGX is not reachable from here." Both are stale (merged as `b48fe24`, origin branch exists). | Not a number. Fix later. |
| K9 | HERA client timings | `README.md:278` gives HERA t = 0x1fc0001, but the table at `README.md:299-313`, plus `docs/spec.md:4599-4602` and `README.md:411-413`, report timings measured at t = 2^26 (`docs/MEASUREMENT_PROVENANCE.md:50,60-68`). | Nothing yet: don't cite them (C2). |
| K10 | RtF memory | `docs/spec.md:4386-4387`: "Sparse LogSlots=4 does NOT reduce RAM." That contradicts `docs/MEASUREMENT_PROVENANCE.md:128-138` (LogSlots drives StC memory). `docs/spec.md:4369` and `artifacts/hhe_breakeven.json:24,51` give a "~60 GB" literature anchor. | Measured: 61.8 GB (HERA) and 64.8 to 65.1 GB (Rubato), `vm_hwm_kb_end` at `128af`, LogN 16 (DGX JSON). Not the 60 GB literature figure. |
| K11 | 160-bit vs 200-bit reduction (only if the paper mentions it) | `PROJECT_STATE.md:54`: "~48%". `README.md:254`: "~49%". | `artifacts/comparison_results.json` `summary`: means 14,877.6 and 7,538.1 µs, which is 49.3%. |
| K12 | Toy HE error, HERA r=5 (not needed for CNP) | `PROJECT_STATE.md:112`: 1.4e-5. `docs/PAPER_HANDOFF.md:34`: 2.49e-5. `README.md:382`: 1.390409e-05. | `artifacts/hera_vs_rubato_transciphering.json` `correctness_gates.gate_b_toy_he_harness`: 1.390409e-05. |
| K13 | Quantisation margin | `scripts/quantisation_sweep.py:244` and `artifacts/quantisation_sweep.json` assume a 26-bit t (`MEASUREMENT_PROVENANCE.md:71`). | Don't cite. Use `docs/cnp_gate.md` and K4 instead. |
| K14 | Normative-source gaps | `docs/spec.md` contains **no CNP section**: grep finds 0 hits for "CNP" or "coefficient-native". `PROJECT_STATE.md` predates all CNP work (`:1` is dated 2026-08-19; `:292` says WAHC). CNP-A's only design note is the agent file `.agent/cnp_a_state.md`. | For CNP-A: `vendor_server/include/cnp_codec.h` and `.agent/cnp_a_state.md:3-16`. For CNP-B: `docs/cnp_b.md` (after K1 and K2) and `docs/cnp_gate.md` (after K3). Never `spec.md` or `PROJECT_STATE.md`. |
| K15 | RSS units | The report uses KB and decimal GB. 61,802,744 kB is 61.8 GB, or 58.9 GiB. | Choose one unit for the paper and say which. |
| K16 | CNP gate reproducibility | `docs/cnp_gate.md:76`: never re-run to confirm bit-identical output. | Low risk. Every RNG is seeded. |

**Recommended single deliverable:** a `paper_numbers.md`, in the repo, that maps each number in the paper to `file` + JSON field (or line) + tag. It is about 2 h of work, and it is the direct answer to your professor's rule. Build it from the final Overleaf draft.

---

## 3. Anonymity blockers

Grep patterns used: `raghav`, `pathak`, `raghavpathak30`, `lnmdgx`, `LNMIIT`, `debranjan`, `/home/raghavp`, `-home-raghavp`. I excluded `.venv*`, `third_party/`, `build/` and `__pycache__`. I found no roll numbers and no IPs other than the cloud metadata address. The TLS certificates are not tracked.

### Tracked text files

| Path:line | Content |
|---|---|
| `LICENSE:3` | "Copyright (c) 2026 Raghav Pathak" |
| `tools/transciphering/go.mod:1` | `module github.com/raghavpathak30/ppfdaas/transciphering` |
| `tools/transciphering/bench/main.go:35` | The same import path |
| `reports/paper_fact_checks.md:31` | The same path, in test output |
| `Dockerfile.client:53`, `Dockerfile.server:68` | `image.source=https://github.com/raghavpathak30/PPFDaaS` |
| `compose.prod.yaml:3,33` | `ghcr.io/raghavpathak30/...` |
| `docs/DGX_RUNBOOK_slot.md:3,38` | `lnmdgx1` |
| `docs/DGX_RUNBOOK_slot.md:5,58,136,149,159` | "Raghav" |
| `docs/DGX_RUNBOOK_slot.md:50` | The GitHub clone URL with your handle |
| `scripts/cloud_transcipher_bench/run_benchmark.sh:4`, `scripts/cloud_transcipher_bench/README.md:3` | "Raghav's go-ahead" |
| `PPFDaaS_REMEDIATION_PLAN.md:3` | `raghavpathak30/PPFDaaS` |
| `RESEARCH_FINDINGS_v3.md:112` | "Raghav" |
| `docs/SESSION_LOG.md:271` | "Prof. Debranjan" |
| `docs/SESSION_LOG.md:1453,1808` | "Raghav", "Raghav Pathak" |
| `artifacts/hera_vs_rubato_transciphering.json:5`, `artifacts/hera_vs_rubato_transciphering_PRIOR_wrong_noise_sampler.json:4` | "Prof. Debranjan" in `task` |
| `.agent/cnp_b_state.md:1` | `/home/raghavp/BTP-cnpb` |
| `reports/bfv_slot_baseline_report.md:199` | `/home/raghavp/BTP` |
| `PPFDaaS_REMEDIATION_PLAN.md` | Also contains a `/home/raghavp` path |
| `artifacts/bfv_slot_baseline/**/*.json` | Home paths in `data_source`. **29 files**; list them with `git ls-files artifacts \| xargs grep -Il '/home/raghavp\|-home-raghavp'`. Examples: `laptop_20261103T205043Z/session.json` has `/home/raghavp/cnpb_data`, and `correctness_logn10/slot_hera.json` has `/tmp/claude-1000/-home-raghavp-BTP-cnpb/...`. |
| `ctest_output.txt` (17 hits), `verify_output.txt` (8), `logs/train_output.txt:3`, `logs/dispatch_output.txt:3,5`, `logs/linearize_output.txt:1,3` | `/home/raghavp/...` |
| `README.md:559,583` | "Professor trace mode", "Professor-Ready". Not a name, but it marks a student project. |

### Binary files and metadata

| Path | Content |
|---|---|
| `docs/RtF_Transciphering_Progress_v2.pptx`, `docs/RtF_Transciphering_Progress_v3.pptx` | `dc:creator` and `cp:lastModifiedBy` "Raghav Pathak"; the slide text contains "Raghav Pathak" (3x) and "LNMIIT" (1x) |
| `docs/*.docx` (3 files) | Creator "Un-named": clean. `PPFDaaS_Master_Implementation_Guide.docx` mentions filing a provisional patent with an "institute IPR cell". That is not your name, but don't ship it. |
| `artifacts/quantisation_sweep.png` | Only the Matplotlib `Software` tag: clean |

### Git

- All **89 commits on all branches** have author and committer `raghavpathak30` or `Raghav Pathak`, with your Gmail address.
- The remote is `github.com/raghavpathak30/PPFDaaS`.
- A history rewrite is not worth it. **Don't link this repo.**

### Files that will become blockers when committed

- Every `~/dgx_results/*/machine_state_{start,end}.json` has `"hostname": "lnmdgx1"`.
- `scripts/dgx/run_ckks.sh:124`, `scripts/dgx/run_transcipher.sh:153` and `tools/ckks/ckks_bench.cpp:367` write the hostname into new runs. The new slot runs will carry it too.
- `lnmdgx1` points to the institution. Commit the raw files to the private repo for traceability. Put a sanitised copy only in an anonymised snapshot, if you make one.

### Overleaf (I cannot see it; check it yourself)

- Author block, acknowledgements, funding, "our previous work [X]".
- Any GitHub URL.
- `lnmdgx1`, LNMIIT or "DGX at ..." in the experimental setup. "A 2-socket Xeon E5-2698 v4 server, 503 GB" is fine.
- Figures exported from the two `.pptx` decks.
- PDF metadata: run `pdfinfo paper.pdf` and check `Author` and `Creator`. Check whether `hyperref` sets `pdfauthor`.

### If you want a repo link

Build a new orphan snapshot with no history. Include only the CNP code, the inputs, the scrubbed artifacts and a short README. Leave out `.pptx`/`.docx`, `docs/SESSION_LOG.md`, the `.agent/` notes, `*_output.txt`, `logs/` and this file. Rename the Go module path. Host it on anonymous.4open.science.

That takes **2 to 3 hours, and one miss means desk rejection.** ACISP has no artifact track and the link is optional, so **I recommend no link**, with the line "code available on acceptance".

---

## 4. Repo presentation (LOWER PRIORITY than sections 1 to 3: none of this changes what a reviewer sees if the repo isn't linked)

- **Root clutter:** `ctest_output.txt`, `verify_output.txt`, `cmake_output.txt`, `server_stub.py`, `RESEARCH_FINDINGS{,_v2,_v3}.md`, `BENCHMARK_RESULTS.md`, `AUDIT.md`, `PPFDaaS_REMEDIATION_PLAN.md`, `.cursorrules`. `inference_pb2*.py` must stay at the root: the Docker image needs them (`f40dbcd`).
- **Duplicated variants:**
  - `vendor_server/src/inference_service.cpp` (legacy 200-bit, holds the secret key) next to `inference_service_160.cpp`.
  - `ckks_context.cpp`, `eval_context_160.cpp` and `tools/local_benchmark/ckks_context_160.cpp`.
  - `benchmark.cpp` and `benchmark_160.cpp`.
  - `bank_client/he_wrapper/seal_wrapper.cpp` and `seal_wrapper_160.cpp`.
  - The broken degree-2 path: `*_depth2*`, `*_degree2*`, `compiler/degree2_linearizer.py`.
- **Committed binary:** `tools/lattigo_benchmark/lattigo_benchmark`, an 11 MB ELF. `vendor_server/artifacts/*.bin` are 0-byte files.
- **Superseded artifacts:** `*_PRIOR*`, `*_PLUMBING_powersave`, `*_JUN28_*`, `artifacts/performance_revalidation/`, `artifacts/legacy/`.
- **Misleading location:** CNP-B, the paper's main contribution, lives under `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/`, and `third_party/fetch_rtf.sh` copies it from there. Moving it means changing `fetch_rtf.sh` and the Docker build. **Don't move it before the deadline.**
- **Archive-branch candidates after submission:** the HERA-vs-Rubato client axis, the bandwidth ladder and HHE regime map (`docs/spec.md` §8.9 and §8.10), the rotation taxonomy, the Lattigo and OpenFHE tools, the degree-2 path, the cloud bench scaffold, the Docker/DevSecOps files, and the `.pptx`/`.docx` files.
- **Tests:** bare `pytest -q` (in `.venv`) on 2026-11-22 gave **2 failed, 42 passed** in 12.66 s. Both failures are pre-existing source-grep tests on the legacy 200-bit `inference_service.cpp`: `tests/test_inference.py::test_service_uses_spec_timing_boundaries_and_debug_invariant` and `::test_service_uses_direct_pointer_serialization_path_only`. The Go tests (KATs, CNP and slot unit tests) are not run by pytest.

---

## 5. Ranked plan to 1 Dec

Ranked by reviewer risk removed per hour.

| Rank | Item | Section | Attended h | Unattended DGX h |
|---|---|---|---|---|
| 1 | Ask your professor whether they already ran the slot runbook | 1b | 0.1 | |
| 2 | Overleaf anonymity pass (author block, acknowledgements, links, hostname, self-citations, `pdfinfo`) | 3 | 1 | |
| 3 | Overleaf page count: get to 20 pages **including the bibliography**; move proofs and extra tables to the appendix; keep the slot table, threat model and parameters in the body | 0.4 | 1.5 (audit) + 3 (cuts) | |
| 4 | DGX rounds 1 and 2: slot plus a same-session CNP-B rerun at LogN 16 | 1b, B1 | 1 | 3.5 |
| 5 | Commit the existing DGX runs (CNP-A `cnp-s{0,1}`, CNP-B `cnpb-*`, `score-*.txt`) to `artifacts/dgx_logn16_20261028/` with a README giving SHAs, image IDs and sha256 sums | A1, A2, B2 | 1 | |
| 6 | Rewrite the B claim: online, offline and total against slot; HalfBoot only as a labelled lower bound; state the costs (keys, download, RSS) | B1, B3, B4, K7 | 2.5 | |
| 7 | Threat-model scoping: no noise flooding, why it doesn't fit, the Toeplitz case untested, IND-CPA-D for A | S1 | 1.5 | |
| 8 | Score, commit and compute ratios from rounds 1 and 2 | 1b step 4 | 1.5 | |
| 9 | Consistency fixes in the paper text (K2 to K7, K10) | 2 | 1 | |
| 10 | `paper_numbers.md`: every paper number mapped to file and field | 2 | 2 | |
| 11 | CNP-A baseline-fairness paragraph (feature-major cost model; BSGS and naive measured) | A4 | 1 | |
| 12 | Parameter-security citations | S2 | 0.5 | |
| 13 | Socket-swapped repeat, rounds 3 and 4 | R1 | 0.5 | 3.5 |
| 14 | Fix the stale repo docs (K1, K3, K6, K8): `docs/cnp_b.md:6,29`, `docs/cnp_gate.md:61`, `report:3,172` | 2 | 0.5 | |

Attended total: about 23 to 24 h, against 36 to 54 h available. That leaves about 12 h of slack for a failed DGX run and for your professor's review.

### Day by day (4 to 6 h a day; DGX jobs run unattended)

| Day | Work |
|---|---|
| **Sun 22 Nov** (today) | Rank 1: message your professor. Rank 5: commit the existing DGX results (1 h). If you are on campus and your professor confirms it hasn't been run: preflight, build, and launch round 1 (`runbook:96-100`). |
| **Mon 23** | Check round 1 (`runbook:124-136`) and the socket load, then launch round 2. Ranks 2 and 3: the Overleaf anonymity pass and page audit. Start the cuts. |
| **Tue 24** | Rank 8: copy round 1 and 2 results to the laptop, score them, commit, and compute the ratios. Rank 6: rewrite the B section around the real LogN 16 result. If both rounds are clean, launch rounds 3 and 4 overnight. |
| **Wed 25** | Rank 7: threat model. Rank 11: the A-fairness paragraph. Rank 12: parameter citations. **Fallback slot:** if round 1 failed, diagnose and rerun once today. |
| **Thu 26** | Score and commit rounds 3 and 4. Rank 10: `paper_numbers.md`. Rank 9: consistency fixes. |
| **Fri 27** | Finish trimming to 20 pages. **Send the full draft and `paper_numbers.md` to your professor.** |
| **Sat 28** | Address your professor's comments. Second anonymity pass. Rank 14 if time allows. |
| **Sun 29** | Freeze. `pdfinfo`; page count including the bibliography; check that llncs is unmodified (no `\vspace` or margin hacks); compile the references. |
| **Mon 30** | **Submit by 20:00 IST.** The hard deadline is Tue 1 Dec 17:29 IST, so Tuesday morning is buffer only. |

**If the slot run fails at LogN 16 and the rerun also fails:** present the fair comparison at LogN 12 only (IN-REPO) and the LogN 16 CNP-B results against the lower bound, explicitly labelled. Restrict claims to online latency. Don't extrapolate to LogN 16.

### Recommended cuts (there isn't time for these)

- **All of section 4's restructuring**, and any anonymised repo snapshot. Submit without a link.
- **HERA and Rubato client-cost re-measurement** (C2). Drop every absolute client-encrypt claim; keep only "client work is identical across CNP-B and slot" (CONFIRMED-SOURCE).
- **Everything that isn't CNP**: the HHE bandwidth ladder and regime map (`spec.md` §8.9 and §8.10; stale at t=2^26), the rotation taxonomy, Lattigo and OpenFHE, the 160-vs-200-bit ablation, and the HERA-vs-Rubato memory comparisons (invalid, `MEASUREMENT_PROVENANCE.md:89-191`). Those were WAHC-era material.
- **Noise-flooding implementation**: scope it in the text (S1) instead.
- **Implementing an alternative slot layout for A**: write the cost-model paragraph only (A4).
- **Degree-2 fix, the 2 failing legacy pytest tests, Docker audit**: none are seen by reviewers.
- **Socket-swapped repeat** (rank 13), if rounds 1 and 2 aren't clean by Tuesday evening.
