# CNP-B: Coefficient-Native Packing on the RtF path (HERA / Rubato, LogN 16, 128af)

Code: `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/rtf_cnp_{setup,probe,lift,}_test.go` (staged into the
patched RtF `ckks_fv` package by `third_party/fetch_rtf.sh`, run via `scripts/dgx/run_transcipher.sh <job> cnp`).
Inputs: `scripts/cnp_b/prepare_inputs.py`. Scoring (AUC): `scripts/cnp_b/score.py`.
Smoke numbers at LogN 10-12 are plumbing checks, not results. Nothing here has been run at LogN 16 yet.

## Online path (server)
scale-up + Sub (client ct - keystream ct) -> `Mul` by w_rev(X) -> `Add` fresh mask -> return the FV ciphertext (level 0).
No HalfBoot, no CKKS. Lane k, feature j = coefficient 256k+j; lanes = N/256 (256 at LogN 16); output at 256k+255;
(log2 dx, log2 dw) = (8, 6), bank divides by 2^14; t = 33292289 (docs/cnp_gate.md).

## Step-0 facts (probe, `TestCNPProbeIndexMapping`)
- After StC^FV, symmetric element i sits at FV coefficient bitrev_LogN(i), for HERA and Rubato (all i, outputs 0 and last;
  one-hot e_i lands exactly at bitrev(i)). The CLIENT orders elements (feature coefficient c <- symmetric index bitrev(c)).
- Rubato: the client keystream carries the cipher's added noise (|d|<=9) that the homomorphic keystream lacks, so Sub leaves it in the message.
- FV `DecodeRingT` destroys its input plaintext (`ring/ring_scaling.go:76`); copy first if you need the noise.
- Fork calls: `RingTToMul` + `Mul(ct, *PlaintextMul)`; `FVScaleUp` + `Add(ct, *Plaintext)`; `DecryptNew` + `DecodeRingT`.

## Design decisions beyond the brief (all reported)
1. **Signed lift of w_q** for the pmul plaintext (limb i holds w mod q_i). The fork's `RingTToMul` lifts to [0,t): same result mod t,
   ~2^23 more noise at level 3 and ~19 bits less headroom at level 0 (`TestCNPSignedLift`).
2. **Exact client scale-up** (`CNP_CLIENT_ENCODE=exact`, level 0): client limb = round(m*q0/t) instead of m*floor(q0/t).
   With the keystream every client coefficient is uniform mod t, so FVScaleUp's floor costs ~2^20 of noise; after pmul that left
   4.75 bits of headroom at LogN 10 (gate is 5). Exact encoding: ~22 bits after pmul. Fallback: `CNP_STC_MODE=keep` (skip the
   modswitch to level 0; headroom >160 bits at toy scale but 5-6 limbs per ciphertext).
3. Mask: uniform mod t (crypto/rand, rejection sampling on 32-bit draws), `round(bias*2^14) mod t` at 256k+255, FV-encoded with
   `FVScaleUp` at the ciphertext's level. Its own rounding adds ~2^20 noise after pmul (headroom 13.9 bits at toy).
4. Headroom gate: harness stops after output 0 if headroom after pmul < `CNP_HEADROOM_GATE_BITS` (5). HERA bit-exactness is a hard stop.
