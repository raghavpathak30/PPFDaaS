package ckks_fv

// Documents why the CNP-B pmul uses a SIGNED lift of w_q: lifting negative weights to [0,t) (what
// RingTToMul does with a PlaintextRingT) gives the same decrypted result mod t but far more noise.
//
//	E2E_RUN=1 E2E_LOGN=10 go test -run '^TestCNPSignedLift$' -v

import (
	"os"
	"testing"
)

func TestCNPSignedLift(t *testing.T) {
	if os.Getenv("E2E_RUN") != "1" {
		t.Skip("set E2E_RUN=1 to run")
	}
	x := cnpSetup(t, cnpEnvStr("E2E_CIPHER", "hera"), cnpEnvInt("E2E_LOGN", 10))
	N, lanes, tmod := x.params.N(), x.params.N()/cnpFeatures, int64(x.params.PlainModulus())
	d := cnpLoadData(t, "", lanes, uint64(tmod))
	nonces, counter, ks := cnpClientKS(t, x)
	crypt, _, _ := cnpCrypt(t, x, nonces, counter)
	ks0 := cnpStC(x, crypt()[0].CopyNew().Ciphertext())
	w := cnpWRev(d.wq, N)

	// (1) Production level 0 (one 60-bit prime): the [0,t) lift eats ~19 bits of headroom (it would fall
	// past the decryption bound at LogN 16, where the noise is larger); the signed lift keeps >20.
	level := ks0.Level()
	ct := cnpSubStage(x, cnpClientPolyExact(x, cnpDataVec(d, N, lanes, 0), ks, 0), ks0)
	sg := cnpPmulStage(x, ct, cnpPtMulSigned(x, level, w))
	us := cnpPmulStage(x, ct, cnpPtMulUnsignedLift(x, level, w))
	cs, rawS := cnpDecryptRingT(x, sg)
	cu, rawU := cnpDecryptRingT(x, us)
	diff0 := 0
	for i := range cs {
		if cs[i] != cu[i] {
			diff0++
		}
	}
	nbS, hbS := cnpNoise(x, rawS)
	_, hbU := cnpNoise(x, rawU)
	t.Logf("level 0: signed lift headroom %.2f bits (noise 2^%.1f); [0,t) lift headroom %.2f bits; %d of %d coefficients differ mod t", hbS, nbS, hbU, diff0, N)
	if hbS-hbU < 8 || hbU >= 5 {
		t.Errorf("expected the [0,t) lift to cost >= 8 bits of headroom and fall under the 5-bit gate at level 0 (signed %.2f, [0,t) %.2f)", hbS, hbU)
	}

	// (2) A fresh encryption at level 3 (60+3*45 bits) has room for both: same plaintext mod t, very different noise.
	lv := 3
	ptRt := NewPlaintextRingT(x.params)
	for i := range ptRt.Value()[0].Coeffs[0] {
		ptRt.Value()[0].Coeffs[0][i] = uint64((i*7919 + 13) % int(tmod))
	}
	pt := NewPlaintextFVLvl(x.params, lv)
	x.fvEncoder.FVScaleUp(ptRt, pt)
	fresh := x.fvEnc.EncryptNew(pt)
	sg = cnpPmulStage(x, fresh, cnpPtMulSigned(x, lv, w))
	us = cnpPmulStage(x, fresh, cnpPtMulUnsignedLift(x, lv, w))
	cs, rawS = cnpDecryptRingT(x, sg)
	cu, rawU = cnpDecryptRingT(x, us)
	diff := 0
	for i := range cs {
		if cs[i] != cu[i] {
			diff++
		}
	}
	nbS, hbS = cnpNoise(x, rawS)
	nbU, hbU := cnpNoise(x, rawU)
	t.Logf("level %d: signed lift   log2 noise %.2f, headroom %.2f bits", lv, nbS, hbS)
	t.Logf("level %d: [0,t) lift    log2 noise %.2f, headroom %.2f bits", lv, nbU, hbU)
	t.Logf("level %d: coefficients differing mod t: %d of %d; extra noise from the [0,t) lift: 2^%.1f", lv, diff, N, nbU-nbS)
	if diff != 0 {
		t.Errorf("signed and [0,t) lifts must decrypt to the same polynomial mod t, %d differ", diff)
	}
	if nbU-nbS < 8 {
		t.Errorf("expected the [0,t) lift to cost >= 8 more bits of noise, got %.1f", nbU-nbS)
	}
}
