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
2. **Exact scale-up, done on the SERVER** (`CNP_SCALEUP=exact`, level 0, `cnpScaleUp` in rtf_cnp_test.go): limb = round(m*q0/t) via
   128-bit arithmetic instead of the fork's m*floor(q0/t). It is public and keyless, timed inside the "scale-up + Sub" stage.
   The upload stays RtF's symmetric ciphertext: N elements mod t (25 bits each), no client scale-up (`cnpClientSym`).
   Why: with the keystream every coefficient is uniform mod t, so FVScaleUp's floor costs ~2^20 of noise; after pmul that left
   4.75 bits of headroom at LogN 10 (gate is 5). Exact: ~22 bits after pmul. Fallback (not used in the DGX commands):
   `CNP_STC_MODE=keep CNP_SCALEUP=fvscaleup` (no modswitch to level 0; >160 bits at toy scale, 5-6 limbs per ciphertext).
   Wire bytes are in the JSON (`wire_bytes`): upload 25 bits x N per output ct (3.2 KB/txn at 256 lanes if bit-packed, 4 KB as uint32),
   download 2 x N x 8 B per output ct at level 0.
3. Mask: uniform mod t (crypto/rand, rejection sampling on 32-bit draws), `round(bias*2^14) mod t` at 256k+255, scaled up with the
   same exact round(m*q0/t) as the upload (`cnpMaskFV` -> `cnpScaleUp`) at the ciphertext's level. With FVScaleUp's floor the mask
   cost ~2^20 of noise (22 -> 13.9 bits at toy scale); with the exact scale-up post-mask headroom equals post-pmul headroom.
4. Headroom gate: evaluated on the FINAL ciphertext (after the mask add), threshold `CNP_HEADROOM_GATE_BITS` (5); all four checkpoints
   (before Sub, after Sub, after pmul, after mask add) are logged. The harness stops after output 0 if it fails. HERA bit-exactness is a hard stop.
