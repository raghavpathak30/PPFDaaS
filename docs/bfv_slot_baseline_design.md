# BFV-slot baseline for Pipeline B: design (Step 0)

Written before any slot-path code. File:line references are to the vendored RtF fork as patched and staged by `third_party/fetch_rtf.sh` (pinned SHA 105fc73), and to the tracked harness under `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/`. Paths below abbreviate `third_party/RtF-Transciphering/ckks_fv/` as `ckks_fv/` and the harness directory as `patch/`.

Tags: CONFIRMED-SOURCE = read in the code cited. CONFIRMED-RAN = observed in a run (added after Step 2; see the report). PREDICTED = derived here, to be checked by a run.

## 0. Summary of the design

| Decision | Choice |
|---|---|
| Domain | FV slots; no SlotToCoeff, no HalfBoot, no CKKS |
| HERA layout | 16 slots x 16 state elements = 256 features per lane; N/16 lanes in ONE output ciphertext per Crypt |
| Rubato layout | 60 usable elements split into groups of 32, 16, 8, 4 cts (8, 16, 32, 64 slots per lane); 15N/64 lanes in FOUR output ciphertexts per Crypt |
| Evaluation | pmul each element ciphertext by its weight vector, ADD within the group, then rotate-and-sum over S slots by doubling |
| Levels | Sub + pmul + add at level 1, modswitch to level 0, rotate at level 0, mask at level 0 (download identical per ciphertext to CNP-B) |
| Why not level 0 throughout | A slot-encoded weight vector has coefficients of size ~t, so pmul noise growth is ~2^27.7, not CNP-B's ~2^9 (section 4.4) |
| Scale-up | round(c * Q_l / t), which is exactly CNP-B's round(c * q0 / t) at level 0 (section 3) |
| Mask | Reuses `cnpUniformModT` and `cnpScaleUp` from CNP-B, drawn per SLOT not per coefficient (section 5) |

**One deviation from the brief, and it is forced by noise:** the brief says "the same exact scale-up as CNP-B: round(c*q0/t)". Taken literally this means level 0 for Sub and pmul, and the predicted headroom there is below the 5-bit gate. The design uses the same formula at level 1 and keeps level 0 for the output. `SLOT_EVAL_LEVEL=0` runs the literal version so the failure is measured, with the gate still enforced.

## 1. Keystream layout after cipher evaluation, before SlotToCoeff^FV

**One ciphertext per state element.** CONFIRMED-SOURCE.
- HERA keeps 16 state ciphertexts `stCt` (`ckks_fv/fv_hera.go:48`, encrypted at `:72`), and `Crypt` returns them (`:273`).
- Rubato keeps `blocksize` ciphertexts and `Crypt` returns them (`ckks_fv/fv_rubato.go:339`). For RUBATO128L blocksize is 64 (`:63-68`). Only the first blocksize-4 = 60 receive the final round key (`:337`, `:366-377`); the harness already uses 60 outputs (`patch/rtf_cnp_probe_test.go:59-62`).

**Slot i holds the keystream of nonce i; ciphertext k holds state element k.** CONFIRMED-SOURCE.
- Per-slot round constants come from the XOF seeded by `nonce[slot]`: `rc[r][st][slot]` (`ckks_fv/fv_hera.go:102-113`; Rubato `ckks_fv/fv_rubato.go:167-179`).
- They enter the state as a slot-encoded plaintext multiplication, one per state element (`ckks_fv/fv_hera.go:281`, `:285`, `:290`; Rubato `ckks_fv/fv_rubato.go:347`, `:351`, `:356`).
- The linear layer only adds across state ciphertexts (`ckks_fv/fv_hera.go:297-353`), and the cube is element-wise (`:355-363`). No operation rotates slots, so nonces do not mix.
- With `SetLogFVSlots(LogN)` (`patch/rtf_cnp_setup_test.go:115`) and N nonces (`:133`) the "small" encoder has gap 1 (`ckks_fv/mfv_encoder.go:247`), so `EncodeUintRingTSmall` is the full batching encoder.

So after `Crypt`: **decode(ct[k])[i] = keystream[nonce i][element k]**. To be checked by `TestSlotProbeLayout`.

Slot geometry (CONFIRMED-SOURCE, `ckks_fv/mfv_encoder.go:150-160`): the slot index runs through two rows of N/2 (`indexMatrix[i]` and `indexMatrix[i|rowSize]`), the powers of 5 and their negatives. `RotateColumns(ct, k)` is a cyclic left rotation by k inside each row (`ckks_fv/mfv_evaluator.go:820-846`, `ckks_fv/params.go:723-728`).

For contrast, CNP-B's probe found that after SlotToCoeff, element i sits at coefficient bitrev_LogN(i) (`patch/rtf_cnp_test.go:12-14`). The slot path applies no StC, so the client uses slot index = nonce index directly and no bit reversal.

## 2. Does t support full batching?

**Yes. No STOP.** CONFIRMED-SOURCE.
- CNP-B's parameter set is `128af` for both ciphers: `RtFHeraParams[2]` and `RtFRubatoParams[0]` (`patch/rtf_cnp_setup_test.go:80-81`, `:86-88`), with `PlainModulus: 33292289` (`ckks_fv/rtf_params.go:414`). Rubato re-sets `RubatoParams[RUBATO128L].PlainModulus` = 0x1fc0001, the same value (`ckks_fv/fv_rubato.go:65`, `patch/rtf_cnp_setup_test.go:113`).
- t - 1 = 33292288 = 127 * 2^18, so t = 1 mod 2^18, and t = 1 mod 2N for every N up to 2^17. LogN 16 needs 2N = 2^17. OK.
- The fork enforces this: `ring.NewRing(params.N(), []uint64{params.plainModulus})` (`ckks_fv/mfv_encoder.go:130`) fails unless the modulus is prime and = 1 mod 2N (`ring/ring.go:133-141`). RtF itself depends on it: the cipher's round constants are slot-encoded mod t.

## 3. Encoding the client's symmetric ciphertext into slots

Upload unchanged from CNP-B: the mod-t symmetric ciphertext, 25 bits per element, no client-side scale-up (`patch/rtf_cnp_test.go:264-278`). Only the assignment of elements to nonces changes (section 4.3).

Server, online, per keystream ciphertext k:
1. `EncodeUintRingT(sym_k)`: writes the elements in slot order and applies the inverse NTT mod t (`ckks_fv/mfv_encoder.go:220-228`). This gives a polynomial P in R_t with P's slots = sym_k. **This inverse NTT is an online cost CNP-B does not pay.** Keyless, public.
2. Exact scale-up into R_Q at the evaluation level l: limb = round(P_c * Q_l / t).
3. Ciphertext (Delta*P, 0) minus keystream ciphertext, exactly CNP-B's `cnpSubStage` (`patch/rtf_cnp_test.go:310-316`).

Result in slots: (m_i + ks_i) - ks_i = m_i (plus the cipher noise d_i for Rubato, `patch/rtf_cnp_test.go` Rubato note in `docs/cnp_b.md:16`).

**Scale-up at level l >= 1.** `cnpScaleUp` is level-0-only by construction (128-bit quotient, `patch/rtf_cnp_test.go:288-300`), and it must not be modified. The slot path adds `slotScaleUp`:

    Q_l = t*D + r  (D = floor(Q_l/t), r = Q_l mod t)
    round(c*Q_l/t) = c*D + round(c*r/t)

c and r are both < 2^25, so round(c*r/t) fits in 64 bits. Each limb is (c*(D mod q_i) + round(c*r/t)) mod q_i, so it is exact at every level with no big integers per coefficient. At level 0 it equals `cnpScaleUp` by construction; `TestSlotScaleUpExact` must check this bit-for-bit, and check level >= 1 against big.Int.

The fork's `FVScaleUp` (c * floor(Q/t), `ckks_fv/mfv_encoder.go:341-372`) is not used, for the same reason CNP-B rejected it.

## 4. Cheapest slot-domain LR

### 4.1 Operation counts (one Crypt batch, CONFIRMED-SOURCE for structure, arithmetic in the table)

HERA, 16 usable ciphertexts, N/16 transactions per Crypt:

| Variant | pmul | ct add | Rotations (key switches) | Galois keys | Output cts | Lanes per output ct | Verdict |
|---|---|---|---|---|---|---|---|
| Per-ct rotate-and-sum, lane = 256 slots of one ct (CNP-B's layout, in slots) | 16 | 16 x 8 | 16 x 8 = 128 | 8 | 16 | N/256 | Strawman |
| Per-ct BSGS (16+16) | 16 | 16 x 15 | 16 x 30 = 480 | 30 | 16 | N/256 | Worse |
| Per-ct hoisted, 255 rotations of one decomposition | 16 | 16 x 255 | 16 x 255 inner products | 255 | 16 | N/256 | Worse: more keys, and at level 0 the decomposition hoisting saves is cheap |
| **Add across ciphertexts, then doubling over 16 slots** | **16** | **15 + 4** | **4** | **4** | **1** | **N/16** | **Chosen** |
| Same, hoisted over 16 slots | 16 | 15 + 15 | 15 inner products, 1 decomposition | 15 | 1 | N/16 | Worse: 15 key inner products against 4 full switches, and 15 keys against 4 |

Depth in every row: one plaintext multiplication, no ciphertext-ciphertext multiplication, same as CNP-B.

Rubato-128L, 60 usable ciphertexts, 15N/64 transactions per Crypt when no element is wasted:

| Variant | Elements wasted | Transactions per Crypt | pmul | Rotations | Galois keys | Output cts | Verdict |
|---|---|---|---|---|---|---|---|
| Per-ct rotate-and-sum (G=1, S=256) | 0 | 15N/64 | 60 | 60 x 8 = 480 | 8 | 60 | Strawman |
| Uniform G=4, S=64 | 0 | 15N/64 | 60 | 15 x 6 = 90 | 6 | 15 | Worse |
| **Groups 32+16+8+4 (S = 8, 16, 32, 64)** | **0** | **15N/64** | **60** | **3+4+5+6 = 18** | **6** | **4** | **Chosen** |
| G=60, S=5 (300 elements for 256 features) | 14.7% | N/5 | 60 | 3 | 3 | 1 | Rejected: raises client cost 17% (section 4.3) |
| G=20, S=13 (260 elements) | 1.5% | 3N/13 | 60 | 15 | 4 | 3 | Rejected: raises client cost 1.6% |

Check: N/8 + N/16 + N/32 + N/64 = 15N/64 = 60N/256, the same as CNP-B's 60 x N/256. Group sizes must be powers of two so that G x S = 256 exactly, and 32+16+8+4 = 60 is the binary expansion of 60, the fewest such groups.

### 4.2 Layout of a group (G ciphertexts starting at `base`, S = 256/G slots per lane)

Feature j of a lane maps to r = j div G (slot offset) and c = j mod G (ciphertext `base + c`).

    Lane l in row 0: slots S*l + r.     Lane l in row 1: slots N/2 + S*l + r.     Lanes per row: (N/2)/S.

Weight vector for ciphertext base+c: W_c[slot] = wq[r*G + c], periodic in r.

    acc     = sum_c  W_c * ct[base+c]         // slot S*l+r holds the lane's partial sum over offset r
    out     = acc
    for step in 1, 2, 4, ..., S/2:  out += RotateColumns(out, step)

out[p] = sum_{i=0}^{S-1} acc[p+i], so at p = S*l the output is the full inner product for lane l. Every other slot holds a window sum spanning neighbouring lanes, which is what the mask removes. `InnerSum` (`ckks_fv/mfv_evaluator.go:886-908`) is not usable because it sums the whole row.

Rotation keys: HERA {1, 2, 4, 8}. Rubato {1, 2, 4, 8, 16, 32}. No conjugation key and no StC keys, so the slot-only setup skips `GenSlotToCoeffMatFV` (`patch/rtf_cnp_setup_test.go:124-128`). That is why the slot path needs its own setup function; `cnpSetup` stays untouched.

### 4.3 Client element-to-nonce assignment: changed, cost and upload unchanged

The brief allows this only if client cost and upload bytes stay the same. They do:

| Per full batch | CNP-B | Slot (this design) |
|---|---|---|
| Keystream blocks computed by client | N (`patch/rtf_cnp_probe_test.go:32-44`) | N, same function |
| Transactions, HERA | 16 x N/256 = N/16 | N/16 |
| Transactions, Rubato | 60 x N/256 = 15N/64 | 15N/64 |
| Blocks per transaction, HERA / Rubato | 16 / 64/15 | 16 / 64/15 |
| Elements uploaded per transaction | 256 at 25 bits | 256 at 25 bits |
| Client operation per element | one add mod t | one add mod t |

What changes is only which feature goes with which (nonce, element). CNP-B: all 256 features of a lane use element k across 256 nonces. Slot: they use 16 elements across 16 nonces (HERA). Neither needs new client primitives.

Partial batches: a lone transaction uses 16 blocks under the slot layout rather than touching 256 nonces, so partial batches do not raise client cost either. This design does not claim that as a win, and the benchmark measures full batches only.

### 4.4 Why level 1, not level 0

CNP-B's pmul plaintext is w_rev(X) with coefficients |w| <= 2^6, signed lifted, so noise grows by about ||w||_1 ~ 2^11.5 at worst (`docs/cnp_b.md:21`).

A slot weight vector is the inverse NTT mod t of the weights, so its coefficients are ~uniform mod t, and no lift can make them small. The signed lift (`cnpPtMulSigned`, reused as the brief requires) recovers only about 1 bit. Periodicity helps: W_c is invariant under rotation by S, so the polynomial lives in X^(N/(2S)) and has only 2S nonzero coefficients. Then:

    growth per ciphertext ~ sqrt(2S) * t/sqrt(12)
    group sum of G ciphertexts ~ sqrt(2S*G) * t/sqrt(12) = sqrt(512) * 2^23.2 ~ 2^27.7

This is the same for every group since S*G = 256. PREDICTED: with input noise 2^3 to 2^7, output noise is 2^31 to 2^35, against a level-0 budget of q0/(2t) ~ 2^34. That is at or past the gate at level 0, with rotations still to come. Note this figure does not improve with larger N, because the sparsity comes from S and not N, but the input noise can change with N.

At level 1 (Q ~ 2^105) the budget is 2^79, and the same pmul leaves ~45 bits. Modswitching to level 0 then scales the noise by 2^-45, leaving essentially the rounding term ~ ||s||_1-driven, and H=192 keeps this around 2^4 to 2^7. Doubling over S slots multiplies by S and adds key-switch noise. PREDICTED final headroom at level 0: around 20 to 26 bits. Level is a modulus-dropping choice, not a crypto parameter: Crypt leaves HERA at level 8 and Rubato at level 9 (24 moduli, `ckks_fv/halfboot_params.go:32-34` + `ckks_fv/rtf_params.go:411-470`; CipherModDown sums 15 and 14, `ckks_fv/rtf_params.go:51`, `:90`), and the fork already switches down.

Rotating at level 0 is the cheapest place: `switchKeysInPlace` uses beta = ceil((level+1)/alpha) = 1 decomposition block (`ckks_fv/mfv_evaluator.go:948`).

### 4.5 Galois key bytes (arithmetic from source, CONFIRMED-SOURCE)

One rotation key holds beta x 2 polynomials x (Q+P limbs) x N x 8 bytes (`ckks_fv/keys.go:54`, `rlwe` `NewSwitchingKey`). For 128af: 24 Q limbs + 4 P limbs, alpha = 4, beta = ceil(24/4) = 6 (`ckks_fv/params.go:707-714`). So one key = 6 x 2 x 28 x N x 8 = **2688 N bytes**: 2.75 MB at LogN 10, 11.0 MB at LogN 12, 176 MB at LogN 16.

| LogN 16, as generated by the fork | Key count | Bytes |
|---|---|---|
| Slot, HERA | 4 | 705 MB |
| Slot, Rubato | 6 | 1.06 GB |
| CNP-B (StC only) | measured at toy scale by `rotation_key_counts`, projected for LogN 16 in the report | see report |

Only 1 block x 2 polys x (1 Q limb + 4 P limbs) of each slot key is touched at level 0 (`ckks_fv/mfv_evaluator.go:953-956`), so a deployment could store 80N bytes per key (5.2 MB at LogN 16). That is arithmetic, not implemented or measured, and the comparison uses the full-chain bytes the fork actually allocates.

## 5. Masking

Same treatment as CNP-B: every non-output position is fresh uniform mod t from `crypto/rand`, the bias sits at the output positions, the mask is FV-encoded by exact scale-up at the final ciphertext's level, and the bank divides by 2^14.

Position means SLOT here, since the partial sums are in slots. Masking coefficients as CNP-B does would give a uniform polynomial, whose slots are uniform too, but pinning the output slots to the bias needs the slot encoding anyway.

    v = uniform mod t in all N slots            // cnpUniformModT, reused (patch/rtf_cnp_test.go:210-229)
    v[output slots] = biasQ mod t
    P = EncodeUintRingT(v)                      // inverse NTT mod t
    pt = cnpScaleUp(x, 0, P)                    // reused, level 0 (patch/rtf_cnp_test.go:285-300)
    ct += pt

One mask per OUTPUT ciphertext. CNP-B draws 16 (HERA) or 60 (Rubato) per batch, and the slot path 1 or 4. This is a real saving, not a weaker mask: each output ciphertext is fully masked.

The CNP-B mask tests are ported to this layout: masked = unmasked + bias at outputs; negative control with a raw unscaled mask; chi-square on non-output slots; weight recovery (unmasked succeeds, masked at chance). Noise flooding is not applied in either path, so like-for-like holds and neither claims circuit privacy.

## 6. Offline and online are different here, and must be measured separately

| Stage | CNP-B | Slot |
|---|---|---|
| Offline, once per batch | Crypt | Crypt |
| Offline, per keystream ct | SlotToCoeff^FV + ModSwitchMany (`patch/rtf_cnp_test.go:70-76`) | ModSwitchMany only |
| Online, per keystream ct | scale-up + Sub + pmul at level 0 | slot encode (inverse NTT mod t) + scale-up + Sub + pmul at level 1 + add |
| Online, per output ct | mask (1 per keystream ct) | modswitch + rotations + mask |

The slot path skips SlotToCoeff^FV offline. Its online stage is heavier per keystream ciphertext but aggregates many ciphertexts into one output. So the answer can differ for online, offline and total, and the three are reported separately.

**Toy-scale caution:** StC work grows with LogN: radix-2 factorisation has (logN+1)/2-ish stages (`ckks_fv/mfv_encoder.go:646-665`), and its diagonals grow too. So a toy-scale gap on offline is expected to UNDERSTATE the LogN 16 gap. That is the direction of the bias, not a number; LogN 16 must be measured on the DGX.

## 7. Risks to check by running

1. Input noise at level 1 is higher than assumed: measured at every checkpoint.
2. Rotation semantics across rows: `TestSlotProbeLayout` and the bit-exact HERA check.
3. Rubato's noise d_i passes through slots with the same weights, so the output error distribution should match CNP-B's. Checked by AUC.
4. Group order: lanes fill group 0 first; correctness compares by row id, so a mix-up cannot pass silently.

## 8. Addendum after running (CONFIRMED-RAN; the design above is unchanged)

Sources: `artifacts/bfv_slot_baseline/correctness_logn10/`, `artifacts/bfv_slot_baseline/level0_gate_stop_logn10/`, `artifacts/bfv_slot_baseline/probe_logn10/`, `artifacts/bfv_slot_baseline/memory_scaling/`. Numbers and paths are in `reports/bfv_slot_baseline_report.md`.

- Section 1 layout, one-hot and rotation semantics: `TestSlotProbeLayout` passed for HERA and Rubato at LogN 10 with 0 wrong slots.
- Section 3 scale-up: `TestSlotScaleUpExact` passed. It found one bug in the first version: the chain contains a 22-bit prime (`DiffScaleModulus` 0x2a0001) smaller than t, so the rounding term must be reduced per limb. This only affects level 8 and above, not the level-1 evaluation.
- Section 4.4 noise: the prediction held. LogN 10, HERA: input 2^3.71, after the first pmul 2^29.48 (growth 2^25.8, predicted 2^25.7), after the group sum 2^31.44 (growth 2^27.7, predicted 2^27.7). Literal level 0 stops at the gate: 0.73 bits HERA, 0.01 bits Rubato. Level 1 passes: 27.08 and 25.80 bits, slightly above the predicted 20 to 26.
- Section 4.5 key bytes: 2688 N per key confirmed by walking the allocated key objects (4 keys = 11,010,048 bytes at LogN 10).
