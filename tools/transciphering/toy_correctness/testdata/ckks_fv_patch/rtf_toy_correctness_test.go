// This file is NOT built as part of the tools/transciphering Go module (it
// lives under a `testdata/` directory, which `go build ./...`/`go vet ./...`
// ignore). It is staged by third_party/fetch_rtf.sh, which copies it verbatim
// into third_party/RtF-Transciphering/ckks_fv/ after the pinned checkout, so
// it compiles as an additive file of the vendored `package ckks_fv` (same
// mechanism Go test files normally use to reach package-internal symbols
// such as plainHera).
//
// Purpose: PPFDaaS Phase 7 correctness harness (see
// tools/transciphering/toy_correctness/README.md). Proves the full
// server-side path -- HERA-in-BFV transcipher -> HalfBoot -> FV->CKKS repack
// -> a CKKS eval -- runs correctly end to end within a ~15 GB RAM budget.
// It is a REDUCED, DELIBERATELY-INSECURE toy parameter set (LogN shrunk from
// 16 to rtfToyLogN; every modulus is reused verbatim from RtFHeraParams[3]
// ("128as"), which stays NTT-valid because rtfToyLogN's ring size divides
// 65536). This is NOT a security or performance claim -- see
// RESEARCH_FINDINGS_v3.md §B and PPFDaaS's Phase 7 instructions for why the
// real (~60 GB) run is deferred to cloud hardware.
package ckks_fv

import (
	"crypto/rand"
	"math"
	"testing"

	"github.com/ldsec/lattigo/v2/utils"
)

// rtfToyLogN: toy-only ring degree. NOT secure -- do not reuse for any real
// deployment. Chosen as the smallest power-of-two ring comfortably above the
// H=192 sparse-secret Hamming weight inherited from RtFHeraParams[3] (that
// GenKeyPairSparse requires N > H to sample) while leaving headroom for the
// BSGS rotation-key machinery; 2^10=1024 was the first size tried and it
// worked, so no further reduction was pursued once it stayed well inside 15 GB.
const rtfToyLogN = 10

// toyRtFHeraParams returns an additive HalfBootParameters entry: a deep copy
// of RtFHeraParams[3] ("128as", arcsine + 4 sparse slots) with only LogN
// changed. It does not modify RtFHeraParams[3] itself (rtf_params.go is
// untouched). Every modulus (ResidualModuli, KeySwitchModuli, SineEvalModuli,
// CoeffsToSlotsModuli, DiffScaleModulus) is reused byte-for-byte from the
// upstream 128as entry, preserving the full 15-level HalfBoot depth
// (4 CtS + 11 SineEval) documented in RESEARCH_FINDINGS_v3.md §B1.
func toyRtFHeraParams() *HalfBootParameters {
	hb := RtFHeraParams[3].Copy()
	hb.LogN = rtfToyLogN
	return hb
}

// TestRtFHeraToyCorrectness runs the same pipeline as
// BenchmarkRtFHera80as(paramIndex=3, numRound=4, radix=0, fullCoeffs=false)
// but against the toy LogN, and asserts the decrypted output of a trivial
// CKKS eval (2*x + 1) applied to the repacked HalfBoot output matches the
// known plaintext computation within tolerance. This is a functional
// correctness check, not a timing measurement -- online_transcipher_ms and
// repacking_ms remain PENDING per tools/transciphering/README.md.
func TestRtFHeraToyCorrectness(t *testing.T) {
	const numRound = 4   // HERA-80 config, matches BenchmarkRtFHera80as
	const paramIndex = 3 // "as" family: arcsine eval, 4 sparse slots
	const radix = 0      // matches BenchmarkRtFHera80as's SlotToCoeffMatFV radix
	const tolerance = 5e-2

	hbtpParams := toyRtFHeraParams()
	params, err := hbtpParams.Params()
	if err != nil {
		t.Fatalf("toy params rejected by NewParametersFromModuli: %v", err)
	}
	messageScaling := float64(params.PlainModulus()) / hbtpParams.MessageRatio

	heraModDown := HeraModDownParams80[paramIndex].CipherModDown
	stcModDown := HeraModDownParams80[paramIndex].StCModDown

	params.SetLogFVSlots(params.LogSlots()) // fullCoeffs=false, matches "80as"

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
		key[i] = uint64(i + 1) // known key, matches RtF_bench_test.go convention
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

	// Server-side: homomorphic HERA decryption of the bank's upload (slot 0
	// only -- this harness proves correctness of one lane, not throughput).
	fvKeystreams := hera.Crypt(nonces, kCt, heraModDown)
	fvKeystreams[0] = fvEvaluator.SlotsToCoeffs(fvKeystreams[0], stcModDown)
	fvEvaluator.ModSwitchMany(fvKeystreams[0], fvKeystreams[0], fvKeystreams[0].Level())

	ciphertext := NewCiphertextFVLvl(params, 1, 0)
	ciphertext.Value()[0] = plaintexts[0].Value()[0].CopyNew()
	fvEvaluator.Sub(ciphertext, fvKeystreams[0], ciphertext)
	fvEvaluator.TransformToNTT(ciphertext, ciphertext)
	ciphertext.SetScale(math.Exp2(math.Round(math.Log2(float64(params.Qi()[0]) / float64(params.PlainModulus()) * messageScaling))))

	// HalfBoot: ModRaise -> SubSum -> CoeffsToSlots -> EvalMod, repacked to CKKS.
	ctBoot, _ := hbtp.HalfBoot(ciphertext, true)

	// Trivial CKKS eval on the repacked ciphertext: 2*x + 1. Exercises "the
	// existing CKKS circuit unchanged" step from docs/spec.md §8, not just a
	// decode of the repack.
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

	t.Logf("toy RtF+HalfBoot+CKKS-eval correctness: LogN=%d, slots=%d, max|got-want|=%e (tolerance=%e)",
		rtfToyLogN, params.Slots(), maxAbsErr, tolerance)

	if maxAbsErr > tolerance {
		t.Fatalf("correctness check FAILED: max abs error %e exceeds tolerance %e", maxAbsErr, tolerance)
	}
}
