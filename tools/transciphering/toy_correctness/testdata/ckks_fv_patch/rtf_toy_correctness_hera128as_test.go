// This file is NOT built as part of the tools/transciphering Go module (it
// lives under a `testdata/` directory, which `go build ./...`/`go vet ./...`
// ignore). It is staged by third_party/fetch_rtf.sh, which copies everything
// in this directory verbatim into third_party/RtF-Transciphering/ckks_fv/
// after the pinned checkout, so it compiles as an additive file of the
// vendored `package ckks_fv`.
//
// Purpose: exact-round-count parity check for HERA, alongside
// rtf_toy_correctness_test.go (which tests "80as": numRound=4,
// HeraModDownParams80, radix=0 -- BenchmarkRtFHera80as's exact config). This
// file tests "128as": numRound=5, HeraModDownParams128, radix=2 --
// BenchmarkRtFHera128as's exact config (RtF_bench_test.go:50-51:
// benchmarkRtFHera(b, "128as", 5, 3, 2, false)) -- which is what
// tools/transciphering/cipher/hera.go's HeraRounds=5 actually targets. The
// existing r=4 harness proves the pipeline mechanics; this one proves them
// at the same round count our Go client ships.
package ckks_fv

import (
	"crypto/rand"
	"math"
	"testing"

	"github.com/ldsec/lattigo/v2/utils"
)

// rtfToyLogNHera128as: same toy-ring rationale as rtfToyLogN in
// rtf_toy_correctness_test.go (N > H=192, smallest convenient power of two).
const rtfToyLogNHera128as = 10

// toyRtFHeraParams128as returns a deep copy of RtFHeraParams[3] ("128as")
// with only LogN changed -- same technique as toyRtFHeraParams in
// rtf_toy_correctness_test.go.
func toyRtFHeraParams128as() *HalfBootParameters {
	hb := RtFHeraParams[3].Copy()
	hb.LogN = rtfToyLogNHera128as
	return hb
}

// TestRtFHera128asToyCorrectness mirrors TestRtFHeraToyCorrectness but at
// numRound=5, HeraModDownParams128, radix=2 -- BenchmarkRtFHera128as's exact
// config, matching HeraRounds=5 in tools/transciphering/cipher/hera.go.
func TestRtFHera128asToyCorrectness(t *testing.T) {
	const numRound = 5   // HERA-128 config, matches BenchmarkRtFHera128as
	const paramIndex = 3 // "as" family: arcsine eval, 4 sparse slots
	const radix = 2       // matches BenchmarkRtFHera128as's SlotToCoeffMatFV radix
	const tolerance = 5e-2

	hbtpParams := toyRtFHeraParams128as()
	params, err := hbtpParams.Params()
	if err != nil {
		t.Fatalf("toy params rejected by NewParametersFromModuli: %v", err)
	}
	messageScaling := float64(params.PlainModulus()) / hbtpParams.MessageRatio

	heraModDown := HeraModDownParams128[paramIndex].CipherModDown
	stcModDown := HeraModDownParams128[paramIndex].StCModDown

	params.SetLogFVSlots(params.LogSlots()) // fullCoeffs=false, matches "128as"

	kgen := NewKeyGenerator(params)
	sk, pk := kgen.GenKeyPairSparse(hbtpParams.H)

	fvEncoder := NewMFVEncoder(params)
	ckksEncoder := NewCKKSEncoder(params)
	fvEncryptor := NewMFVEncryptorFromPk(params, pk)
	ckksDecryptor := NewCKKSDecryptor(params, sk)

	rotationsHalfBoot := kgen.GenRotationIndexesForHalfBoot(params.LogSlots(), hbtpParams)
	pDcds := fvEncoder.GenSlotToCoeffMatFV(radix)
	rotationsStC := kgen.GenRotationIndexesForSlotsToCoeffsMat(pDcds)
	rotations := append(rotationsHalfBoot, rotationsStC...)
	rotations = append(rotations, params.Slots()/2)
	rotkeys := kgen.GenRotationKeysForRotations(rotations, true, sk)
	rlk := kgen.GenRelinearizationKey(sk)
	hbtpKey := BootstrappingKey{Rlk: rlk, Rtks: rotkeys}

	hbtp, err := NewHalfBootstrapper(params, hbtpParams, hbtpKey)
	if err != nil {
		t.Fatalf("NewHalfBootstrapper: %v", err)
	}

	fvEvaluator := NewMFVEvaluator(params, EvaluationKey{Rlk: rlk, Rtks: rotkeys}, pDcds)

	coeffs := make([][]float64, 16)
	for s := 0; s < 16; s++ {
		coeffs[s] = make([]float64, params.N())
	}

	key := make([]uint64, 16)
	for i := 0; i < 16; i++ {
		key[i] = uint64(i + 1)
	}

	data := make([][]float64, 16)
	for s := 0; s < 16; s++ {
		data[s] = make([]float64, params.Slots())
		for i := 0; i < params.Slots(); i++ {
			data[s][i] = utils.RandFloat64(-1, 1)
		}
	}

	nonces := make([][]byte, params.Slots())
	for i := 0; i < params.Slots(); i++ {
		nonces[i] = make([]byte, 64)
		if _, err := rand.Read(nonces[i]); err != nil {
			t.Fatalf("rand.Read: %v", err)
		}
	}

	keystream := make([][]uint64, params.Slots())
	for i := 0; i < params.Slots(); i++ {
		keystream[i] = plainHera(numRound, nonces[i], key, params.PlainModulus())
	}

	for s := 0; s < 16; s++ {
		for i := 0; i < params.Slots()/2; i++ {
			j := utils.BitReverse64(uint64(i), uint64(params.LogN()-1))
			coeffs[s][j] = data[s][i]
			coeffs[s][j+uint64(params.N()/2)] = data[s][i+params.Slots()/2]
		}
	}

	plainCKKSRingTs := make([]*PlaintextRingT, 16)
	for s := 0; s < 16; s++ {
		plainCKKSRingTs[s] = ckksEncoder.EncodeCoeffsRingTNew(coeffs[s], messageScaling)
		poly := plainCKKSRingTs[s].Value()[0]
		for i := 0; i < params.Slots(); i++ {
			j := utils.BitReverse64(uint64(i), uint64(params.LogN()))
			poly.Coeffs[0][j] = (poly.Coeffs[0][j] + keystream[i][s]) % params.PlainModulus()
		}
	}

	plaintexts := make([]*Plaintext, 16)
	for s := 0; s < 16; s++ {
		plaintexts[s] = NewPlaintextFVLvl(params, 0)
		fvEncoder.FVScaleUp(plainCKKSRingTs[s], plaintexts[s])
	}

	hera := NewMFVHera(numRound, params, fvEncoder, fvEncryptor, fvEvaluator, heraModDown[0])
	kCt := hera.EncKey(key)

	fvKeystreams := hera.Crypt(nonces, kCt, heraModDown)
	fvKeystreams[0] = fvEvaluator.SlotsToCoeffs(fvKeystreams[0], stcModDown)
	fvEvaluator.ModSwitchMany(fvKeystreams[0], fvKeystreams[0], fvKeystreams[0].Level())

	ciphertext := NewCiphertextFVLvl(params, 1, 0)
	ciphertext.Value()[0] = plaintexts[0].Value()[0].CopyNew()
	fvEvaluator.Sub(ciphertext, fvKeystreams[0], ciphertext)
	fvEvaluator.TransformToNTT(ciphertext, ciphertext)
	ciphertext.SetScale(math.Exp2(math.Round(math.Log2(float64(params.Qi()[0]) / float64(params.PlainModulus()) * messageScaling))))

	ctBoot, _ := hbtp.HalfBoot(ciphertext, true)

	ckksEvaluator := NewCKKSEvaluator(params, EvaluationKey{Rlk: rlk, Rtks: rotkeys})
	evalResult := ckksEvaluator.MultByConstNew(ctBoot, 2.0)
	if err := ckksEvaluator.Rescale(evalResult, params.Scale(), evalResult); err != nil {
		t.Fatalf("Rescale: %v", err)
	}
	ckksEvaluator.AddConst(evalResult, 1.0, evalResult)

	got := ckksEncoder.DecodeComplex(ckksDecryptor.DecryptNew(evalResult), params.LogSlots())

	maxAbsErr := 0.0
	for i := 0; i < params.Slots(); i++ {
		want := 2*data[0][i] + 1
		errAbs := math.Abs(real(got[i]) - want)
		if errAbs > maxAbsErr {
			maxAbsErr = errAbs
		}
	}

	t.Logf("toy RtF+HalfBoot+CKKS-eval correctness (HERA-128as, r=5): LogN=%d, slots=%d, max|got-want|=%e (tolerance=%e)",
		rtfToyLogNHera128as, params.Slots(), maxAbsErr, tolerance)

	if maxAbsErr > tolerance {
		t.Fatalf("correctness check FAILED: max abs error %e exceeds tolerance %e", maxAbsErr, tolerance)
	}
}
