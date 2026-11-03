# BFV-slot baseline for Pipeline B: report

Branch `bfv-slot-baseline`, created from `cnp-b` at d7ea62b. Not pushed. `main` not touched.

Tags: CONFIRMED-RAN = observed in a run, with the file given. CONFIRMED-SOURCE = read in the code or computed from it. UNVERIFIED = not checked. ESTIMATE = a projection, labelled as such.

Path shorthand: `A/` = `artifacts/bfv_slot_baseline/`. `patch/` = `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/`.

## 1. Outcome

**Step 3 stopped: the laptop was on battery for the whole session, so there is no same-session timing comparison, and CNP-B vs slot on time is not answered.** The runner refused and wrote its record: `A/STOPPED_20261103T172456Z.json` (ADP0 online=0, USB-C online=0, BAT0 Discharging, 23%). CONFIRMED-RAN.

| Step | Status |
|---|---|
| 0 Design | Done: `docs/bfv_slot_baseline_design.md`, committed before any code |
| 1 Implement `slot` | Done: `patch/rtf_slot_test.go`, new files only |
| 2 Correctness | **Passed**: HERA bit-exact on all 56,962 rows, Rubato delta AUC -2.5e-5, headroom 27.08 / 25.80 bits |
| 3 Comparison | **STOPPED: on battery. No timing results** |
| 4 DGX prep | Done, not run: section 7 |

To finish Step 3, plug in and run this (about 30 minutes, ESTIMATE). The runner re-checks power before every arm and stops if AC is lost.

    python3 scripts/cnp_b/prepare_inputs.py --artifacts /home/raghavp/BTP/artifacts --out $HOME/cnpb_data
    python3 scripts/bfv_slot_baseline/run_laptop_compare.py --data $HOME/cnpb_data --logn 10 12 --reps 5

It writes `A/laptop_<UTC>/comparison.md` and `comparison.json`, with machine state for each arm and source paths.

**Decisions you should check:**

1. **Eval level (deviation from the brief).** The brief says round(c*q0/t), which means level 0. At level 0 the final headroom is 0.73 bits (HERA) and 0.01 bits (Rubato), so it fails the gate. I run Sub and pmul at level 1 using round(c*Q_1/t), which is the same formula and bit-identical to CNP-B's at level 0, then switch to level 0 for rotations, mask and download. No crypto parameter changed. The reason and evidence are in section 3.
2. **Reading of the battery stop.** I stopped only the timing comparison and carried on with Steps 0, 1, 2 and 4, because correctness, noise, key counts and RSS do not depend on the power source. If you meant "stop everything", the commits are small and can be discarded.
3. **LogN 14 runs (beyond the brief).** I ran the slot path at LogN 14 (peak 4.14 GB) to support the LogN 16 estimate. Nothing ran at LogN 16.

## 2. Layout chosen (design questions 1 to 6)

| Question | Answer | Tag | Source |
|---|---|---|---|
| Keystream layout before StC | One ciphertext per state element. Slot i of ct k = keystream[nonce i][element k] | CONFIRMED-RAN (0 wrong slots) | `A/probe_logn10/slot_probe_hera.json`, `slot_probe_rubato.json`; `ckks_fv/fv_hera.go:102-113,281-290` |
| t supports batching | Yes. t-1 = 127 * 2^18, so t = 1 mod 2N up to LogN 17. No STOP | CONFIRMED-RAN (`t_minus_1_mod_2N`=0 at LogN 10); CONFIRMED-SOURCE for LogN 16 | `ckks_fv/rtf_params.go:414`, `ring/ring.go:133-141`, `ckks_fv/mfv_encoder.go:130` |
| Encoding the upload | Inverse NTT mod t, then exact scale-up, then Sub. One-hot lands only at its slot | CONFIRMED-RAN (0 wrong slots) | `A/probe_logn10/`; `patch/rtf_slot_test.go` `slotScaler` |
| Cheapest LR | Add element ciphertexts within a group, then rotate-and-sum by doubling (table below) | CONFIRMED-SOURCE | `docs/bfv_slot_baseline_design.md` section 4 |
| Masking | Same treatment as CNP-B, per slot: `cnpUniformModT` and `cnpScaleUp` reused | CONFIRMED-RAN (section 3) | `patch/rtf_slot_test.go` `slotMaskSlots`, `slotMaskFV` |
| Offline vs online | Slot skips SlotToCoeff offline. Reported separately | CONFIRMED-SOURCE | `patch/rtf_slot_test.go` `slotKeystreamAtLevel`, `patch/rtf_cnp_test.go:70-76` |

Cost per Crypt batch, any N. Counts are CONFIRMED-SOURCE; `TestSlotLayoutCapacityMatchesCNP` and `TestSlotLayoutRotationsAndKeys` in `patch/rtf_slot_unit_test.go` pass.

| Arm | Lanes per Crypt | pmul | Rotations online | Galois keys | Output cts | Lanes per output ct | Depth |
|---|---|---|---|---|---|---|---|
| Slot, HERA | N/16 | 16 | 4 | 4 | 1 | N/16 | 1 pmul |
| CNP-B, HERA | N/16 | 16 | 0 (StC offline has many) | 26 at LogN 10 | 16 | N/256 | 1 pmul |
| Slot, Rubato | 15N/64 | 60 | 18 | 6 | 4 | mean 15N/256 | 1 pmul |
| CNP-B, Rubato | 15N/64 | 60 | 0 (StC offline has many) | 26 at LogN 10 | 60 | N/256 | 1 pmul |

**Client cost and upload are unchanged (no STOP).** The element-to-nonce assignment did change, which the brief allows on that condition. Same keystream blocks per transaction (HERA 16, Rubato 64/15), same one-add-mod-t per element, same 800 bytes bit-packed per transaction. CONFIRMED-SOURCE for client cost, not timed; `TestSlotLayoutCapacityMatchesCNP`. Upload bytes: CONFIRMED-SOURCE, `wire_bytes` in `A/memory_scaling/logn12/slot_hera.json`, `slot_rubato.json` and `A/memory_scaling/logn12_cnp/cnp_hera.json`.

Rubato's 32+16+8+4 grouping uses all 60 usable elements. Rejected alternatives with fewer rotations waste elements and raise client cost (G=60: 17%; G=20: 1.6%). Design section 4.1.

## 3. Correctness (Step 2)

LogN 10, `128af`, t = 33,292,289, (log2 dx, log2 dw) = (8, 6), signed lift, inputs from `scripts/cnp_b/prepare_inputs.py`, full test set. All CONFIRMED-RAN. Correctness does not depend on the power source.

| Check | HERA | Rubato | Required | Source |
|---|---|---|---|---|
| Rows | 56,962 (98 frauds) | 56,962 (98 frauds) | | `A/correctness_logn10/slot_{hera,rubato}_score.json` |
| Bit-exact vs `expected.i64` | **Yes, 0 rows differ** | n/a (cipher noise; 56,899 differ, max 2,030) | HERA exact | same |
| AUC encrypted | 0.979390 | 0.979374 | | same |
| AUC plaintext float | 0.979398 | 0.979398 | | same |
| Delta AUC | -8.1e-6 | **-2.5e-5** | \|d\| <= 0.001 (`docs/cnp_gate.md:8`) | same |
| Max logit error | 0.0953 (all quantisation) | 0.1305 | | same |
| Final headroom | **27.08 bits** | **25.80 bits** (worst group) | >= 5 | `A/correctness_logn10/slot_{hera,rubato}.json` |
| Mask: masked = unmasked + bias | 0 lanes differ | 0 lanes differ | 0 | same |
| Negative control, raw mask | 64 of 64 lanes caught | 240 of 240 lanes caught | > 0 | same |
| Chi-square, masked non-output slots (p) | 0.70 | 0.447 | | same |
| Chi-square, unmasked (p) | 0 | 0 | | same |
| Weight recovery, unmasked / masked | 32/32, 0/32 | 64/64, 0/64 | all, 0 | same |

Same 4096 rows against CNP-B, from different runs. Delta AUC HERA: slot -7.7e-6, CNP -7.7e-6. Rubato: slot +5.1e-6, CNP -2.4e-4. Both inside the bound, and the Rubato gap is below the seed std of 3.0e-4 recorded at `docs/cnp_gate.md`. Source: `A/correctness_logn10/paired_strat4096_auc.json`. **No accuracy difference between the arms.**

**Noise explains the level-1 decision.** LogN 10, HERA, `A/correctness_logn10/slot_hera.json`. CONFIRMED-RAN.

| Point | Level | Noise (log2) | Headroom (bits) |
|---|---|---|---|
| After Sub | 1 | 3.71 | 75.30 |
| After first pmul | 1 | 29.48 | 49.54 |
| After group sum | 1 | 31.44 | 47.57 |
| After modswitch | 0 | 3.58 | 30.43 |
| After rotate-and-sum | 0 | 6.93 | 27.08 |
| After mask (final) | 0 | 6.93 | 27.08 |

A slot-encoded weight vector has coefficients of size ~t, so pmul adds about 2^26 of noise, against about 2^9 for CNP-B's small w_rev(X). No lift removes this. Literal level 0 stops at the gate with 0.73 bits (HERA) and 0.01 bits (Rubato): `A/level0_gate_stop_logn10/slot_{hera,rubato}.json`.

Headroom by LogN, final ciphertext. CONFIRMED-RAN at 10 to 14. LogN 16 is UNVERIFIED.

| LogN | Slot HERA | Slot Rubato | CNP HERA | CNP Rubato | Source |
|---|---|---|---|---|---|
| 10 | 27.08 | 25.80 | 22.12 | 22.25 | `A/correctness_logn10/`, `A/cnp_regression_logn10/` |
| 12 | 27.04 | 24.66 | 22.05 | 21.97 | `A/memory_scaling/logn12/`, `A/memory_scaling/logn12_cnp/` |
| 14 | 26.29 | 24.46 | not run | not run | `A/memory_scaling/logn14/` |

**Existing tests.** No test broke. CONFIRMED-RAN.

| Suite | Before my changes | After |
|---|---|---|
| Bare `pytest` | 2 failed, 3 passed, 26 skipped | **2 failed**, 16 passed, 26 skipped (+13 new, all pass) |
| `go test ./...` in `tools/transciphering` | ok | ok |
| `go test .` in `ckks_fv`, ungated | ok | ok (+6 new) |
| `TestCNPRun`, `TestCNPProbeIndexMapping`, `TestCNPSignedLift`, E2E_RUN=1, LogN 10, both ciphers | not run | pass |

**The 2 pytest failures are not from this work and I did not fix them.** They fail the same way on the clean tree: `tests/test_inference.py::test_service_uses_spec_timing_boundaries_and_debug_invariant` and `::test_service_uses_direct_pointer_serialization_path_only`. They check `vendor_server/src/inference_service.cpp`, which this branch does not touch.

`TestRtFE2E` is gated and was not run with E2E_RUN=1. `rtf_e2e_test.go` is byte-identical to the branch point.

**Hard rules, checked:** `git diff d7ea62b HEAD` is empty for `rtf_cnp_test.go`, `rtf_cnp_setup_test.go`, `rtf_cnp_probe_test.go`, `rtf_cnp_lift_test.go`, `rtf_e2e_test.go` and `third_party/patches/`. The wrapper's docker arguments for the `cnp` target are identical before and after (checked with a stub docker). `score.py` defaults to the same `cnp_*` files. No tolerance, gate, crypto parameter, t or quantisation changed. `SLOT_EVAL_LEVEL` can only select a level; the gate still applies. Nothing ran at LogN 16.

## 4. What is known without timing

These do not depend on the power source.

Download per transaction. CONFIRMED-SOURCE, computed from the object layout. Source: `wire_bytes` in `A/memory_scaling/logn12/slot_{hera,rubato}.json`, `A/memory_scaling/logn12_cnp/cnp_hera.json`. Independent of N.

| | Slot HERA | Slot Rubato | CNP-B |
|---|---|---|---|
| Upload, bit-packed | 800 B | 800 B | 800 B |
| Download, uint64 limbs | **256 B** | **273 B** | **4,096 B** |
| CNP-B / slot, download | 16x | 15x | |

Galois keys. Bytes per key = 2688 N, confirmed by walking the allocated key objects (`rotation_key_bytes_allocated`).

| LogN | Slot HERA | Slot Rubato | CNP-B (StC only) | Tag | Source |
|---|---|---|---|---|---|
| 10 | 4 keys, 11.0 MB | 6 keys, 16.5 MB | 26 keys, 71.6 MB | CONFIRMED-RAN | `A/correctness_logn10/slot_*.json`, `A/cnp_regression_logn10/cnp_*.json` |
| 12 | 4 keys, 44.0 MB | 6 keys, 66.1 MB | 33 keys, 363 MB | CONFIRMED-RAN | `A/memory_scaling/logn12/`, `A/memory_scaling/logn12_cnp/` |
| 16 | 4 keys, 705 MB | 6 keys, 1.06 GB | 47 keys, 8.28 GB | **ESTIMATE** | `scripts/bfv_slot_baseline/stc_rotation_keys.py`, which predicted 26 and 33 correctly |

CNP bytes are the key count times the per-key bytes above; the CNP harness does not record bytes itself. The ~12x and ~8x LogN 16 gaps come from that estimate. The slot keys are only touched at level 0, so a deployment could store about 80 N bytes each (5.2 MB at LogN 16). That is arithmetic, not implemented or measured.

Peak RSS, 16 rows, `CNP_ARMS=cnp`. CONFIRMED-RAN.

| LogN | Slot HERA | CNP HERA | Slot Rubato | CNP Rubato | Source |
|---|---|---|---|---|---|
| 12 | 723,416 KB | 2,340,684 KB | 1,057,868 KB | 2,450,112 KB | `A/memory_scaling/logn12/`, `A/memory_scaling/logn12_cnp/` |
| 14 | 2,599,184 KB | not run | 4,139,312 KB | not run | `A/memory_scaling/logn14/` |

## 5. Step 3 comparison

**Not measured.** The requested table (offline and online per ciphertext, server time per transaction, AUC and max error under one session) does not exist. The Galois key, lane, upload, download and accuracy columns are in sections 2 to 4.

## 6. Caveats

| Item | Tag |
|---|---|
| LogN 10 to 14 are not secure parameters. They check function, not security | CONFIRMED-SOURCE |
| Neither arm applies noise flooding, so neither claims circuit privacy. Whether the arms' final noise leaks differently was not analysed | UNVERIFIED |
| Slot headroom at LogN 16. The trend 27.08, 27.04, 26.29 (HERA) and 25.80, 24.66, 24.46 (Rubato) is well above 5 bits | UNVERIFIED |
| The HalfBoot arm is still the existing 2x+1 lower bound, with no LR. It has no AUC, only a CKKS max error | CONFIRMED-SOURCE (`patch/rtf_cnp_test.go:1008`) |
| `baseline_halfboot_extra` is 0 at LogN 10: HalfBoot's rotations appear to be a subset of StC's at toy scale. I did not check whether this holds at larger LogN | UNVERIFIED |
| Governor is `powersave` and needs root to change. It will be recorded, as in earlier sessions | CONFIRMED-RAN |

Pre-existing issues I noticed and did not change:
- `docs/cnp_b.md:29` says upload is 3.2 KB/txn bit-packed and 4 KB as uint32 at 256 lanes. The formula gives 25 * 65536 / 8 / 256 = 800 B and 1,024 B, matching the harness `wire_bytes`. The doc appears to overstate by 4x; check any figure copied from it.
- `.agent/cnp_b_state.md:5` still uses `CNP_CLIENT_ENCODE=exact`, which commit ad978e4 replaced with `CNP_SCALEUP`.
- `rtf_cnp_test.go` is at `patch/`, not `tools/transciphering/` as the brief says.
- The `project-conventions` skill loaded in this environment describes a different repo (reachability-triage), so I ignored it.

## 7. DGX prep (Step 4). Not run; the DGX is not reachable from here.

`scripts/dgx/run_transcipher.sh` accepts `slot`. CONFIRMED-RAN with a stub docker only: arguments, read-only data mount and the DATA_DIR check. The real image was not built. A clean checkout from the pinned SHA staged the new files, and `go vet` and `go test -c` passed, on go1.25.0 (the image pins go1.25.14).

The branch is not pushed, so get this commit onto the build host first. Build from the repo root, with a clean tree:

    docker build -f tools/transciphering/Dockerfile.bench \
      --build-arg GIT_SHA="$(git rev-parse HEAD)" \
      --build-arg PATCH_SHA256="$(sha256sum third_party/patches/0001-rss-checkpoints.patch | cut -d' ' -f1)" \
      --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      -t ppfdaas-transcipher-bench .

Slot runs at LogN 16 (the harness default), then the socket-swapped repeat:

    wait_jobs() { for j in "$@"; do until [ -e "$HOME/results/$j/exit_code" ]; do sleep 60; done; done; }

    CIPHER=hera   NODE=0 CPUS=0-19  REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh slot_hera_s0 slot
    CIPHER=rubato NODE=1 CPUS=20-39 REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh slot_rubato_s1 slot
    wait_jobs slot_hera_s0 slot_rubato_s1

    CIPHER=hera   NODE=1 CPUS=20-39 REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh slot_hera_s1 slot
    CIPHER=rubato NODE=0 CPUS=0-19  REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh slot_rubato_s0 slot
    wait_jobs slot_hera_s1 slot_rubato_s0

**Run CNP-B and HalfBoot in the same session.** Under the house rule only same-session ratios are defensible (`docs/MEASUREMENT_PROVENANCE.md:13-18`), and `docs/cnp_b.md:6` says CNP-B has no LogN 16 run yet. Same image:

    CIPHER=hera   NODE=0 CPUS=0-19  REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh cnp_hera_s0 cnp
    CIPHER=rubato NODE=1 CPUS=20-39 REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh cnp_rubato_s1 cnp
    wait_jobs cnp_hera_s0 cnp_rubato_s1

    CIPHER=hera   NODE=1 CPUS=20-39 REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh cnp_hera_s1 cnp
    CIPHER=rubato NODE=0 CPUS=0-19  REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh cnp_rubato_s0 cnp
    wait_jobs cnp_hera_s1 cnp_rubato_s0

Score, wherever Python with numpy and scikit-learn is available (a copy of `$HOME/cnpb_data` is needed there):

    python3 scripts/cnp_b/score.py --arm slot --data $HOME/cnpb_data --results $HOME/results/slot_hera_s0 --cipher hera
    python3 scripts/cnp_b/score.py --arm slot --data $HOME/cnpb_data --results $HOME/results/slot_rubato_s1 --cipher rubato

**Peak memory at LogN 16, slot path. ESTIMATE, not measured.**

| Cipher | LogN 12 | LogN 14 | Growth 12 to 14 | **ESTIMATE, one batch at LogN 16 (LogN 14 x 4)** |
|---|---|---|---|---|
| HERA | 0.72 GB | 2.60 GB | 3.59x | **about 10.4 GB** |
| Rubato | 1.06 GB | 4.14 GB | 3.91x | **about 16.6 GB** |

Sources: `A/memory_scaling/logn12/`, `A/memory_scaling/logn14/`. **Treat these as lower bounds for a full run:** the LogN 14 runs used 16 rows, one batch, so memory growth over many batches is not included. The stratified 4096-row arms in Step 3 can supply a factor at LogN 12. The DGX runs leave GOGC and GOMEMLIMIT unset. Both figures are near or above this laptop's 11 GB limit, so LogN 16 stays on the DGX.

## 8. Verdict

**At toy scale, does CNP-B still beat the slot baseline?**

| Metric | Answer |
|---|---|
| Online | **Not measured** |
| Offline | **Not measured** |
| Total server time per transaction | **Not measured** |

I cannot give you a measured verdict because the laptop was on battery. The run in section 1 settles it at toy scale, and only the DGX settles it at LogN 16.

**What must change in the paper's B claim, whatever the timings show:**

1. **Do not present the CNP-vs-HalfBoot gap as CNP-B's speedup over the alternatives.** That baseline is a lower bound on a toy 2x+1 circuit. A correct, bit-exact slot-domain LR exists at the same parameters with no HalfBoot and no CKKS. CONFIRMED-RAN at LogN 10.
2. **Report offline, online and total separately.** Slot skips StC offline and pays more online, so one combined figure would hide the trade.
3. **Do not claim CNP-B's rotation or key advantage in general.** CNP-B needs no online rotations, but it needs far more Galois keys for StC than slot needs: 26 vs 4 to 6 at LogN 10 (CONFIRMED-RAN), and an estimated 47 vs 4 to 6 at LogN 16.
4. **State the download cost.** CNP-B downloads 15x to 16x more per transaction (4,096 B vs 256 to 273 B). CONFIRMED-SOURCE. Upload is equal.
5. **Do not claim an accuracy advantage.** Delta AUC is within the bound for both arms, and HERA is bit-exact in both.
