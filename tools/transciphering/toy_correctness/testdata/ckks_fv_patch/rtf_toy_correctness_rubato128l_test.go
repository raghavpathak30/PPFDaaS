// This file is NOT built as part of the tools/transciphering Go module (it
// lives under a `testdata/` directory, which `go build ./...`/`go vet ./...`
// ignore). It is staged by third_party/fetch_rtf.sh, which copies everything
// in this directory verbatim into third_party/RtF-Transciphering/ckks_fv/
// after the pinned checkout, so it compiles as an additive file of the
// vendored `package ckks_fv`.
//
// Purpose: PPFDaaS Phase 7 correctness harness for Rubato-128L, the Rubato
// counterpart to rtf_toy_correctness_test.go (HERA). Proves the full
// server-side path -- Rubato-in-BFV transcipher -> HalfBoot -> FV->CKKS
// repack -> a CKKS eval -- runs correctly end to end within a small RAM
// budget, at a reduced ring so it fits comfortably.
//
// Structurally this follows benchmarkRtFRubato (RtF_bench_test.go:311-475)
// exactly -- same params (RtFRubatoParams[0], "128af", the only CKKS/RtF
// param set Rubato has in this checkout), same RUBATO128L variant constants
// (fv_rubato.go:48-53), same full-coefficient packing (SetLogFVSlots(LogN),
// HalfBoot(ct, repack=false)) -- reduced only to a toy LogN and terminated
// with a trivial CKKS eval + tolerance check (mirroring
// rtf_toy_correctness_test.go's TestRtFHeraToyCorrectness) instead of
// timing sub-benchmarks. This is a functional-correctness check only, not a
// security or performance claim.
package ckks_fv

import (
	"crypto/rand"
	"math"
	"testing"

	"github.com/ldsec/lattigo/v2/utils"
)

// rtfToyLogNRubato128L: same toy-ring rationale as rtfToyLogN in
// rtf_toy_correctness_test.go (N > H=192, smallest convenient power of two;
// preserves NTT-validity of every reused modulus since 1024 | 65536).
const rtfToyLogNRubato128L = 10

// toyRtFRubatoParams returns a deep copy of RtFRubatoParams[0] ("128af",
// Rubato's only CKKS/RtF param set in this checkout). Every modulus is
// reused byte-for-byte from the upstream entry. Unlike HERA's "as" config
// (LogSlots=4, fixed independent of LogN -- a sparse-slot packing), "128af"
// uses full-coefficient packing with LogSlots = LogN-1 (rtf_params.go:
// 550-551: LogN=16, LogSlots=15); LogSlots must shrink along with LogN here
// or Parameters.Params() panics ("slots cannot be greater than LogN-1").
func toyRtFRubatoParams() *HalfBootParameters {
	hb := RtFRubatoParams[0].Copy()
	hb.LogN = rtfToyLogNRubato128L
	hb.LogSlots = rtfToyLogNRubato128L - 1
	return hb
}

// TestRtFRubato128LToyCorrectness mirrors TestRtFHeraToyCorrectness but for
// Rubato-128L (RUBATO128L: blocksize=64, numRound=2, q=0x1fc0001,
// sigma=1.6356633496458739795537788457309656607510203877762320964302959),
// following benchmarkRtFRubato's exact params/config (RtF_bench_test.go:
// 311-475): full-coefficient packing (not sparse slots, unlike HERA's "as"
// config), radix=2, HalfBoot(repack=false).
func TestRtFRubato128LToyCorrectness(t *testing.T) {
	const rubatoParam = RUBATO128L
	const radix = 2
	const tolerance = 5e-2

	blocksize := RubatoParams[rubatoParam].Blocksize
	outputsize := blocksize - 4
	numRound := RubatoParams[rubatoParam].NumRound
	plainModulus := RubatoParams[rubatoParam].PlainModulus
	sigma := RubatoParams[rubatoParam].Sigma

	hbtpParams := toyRtFRubatoParams()
	params, err := hbtpParams.Params()
	if err != nil {
		t.Fatalf("toy params rejected by NewParametersFromModuli: %v", err)
	}
	params.SetPlainModulus(plainModulus)
	params.SetLogFVSlots(params.LogN())
	messageScaling := float64(params.PlainModulus()) / hbtpParams.MessageRatio

	rubatoModDown := RubatoModDownParams[rubatoParam].CipherModDown
	stcModDown := RubatoModDownParams[rubatoParam].StCModDown

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
	rotkeys := kgen.GenRotationKeysForRotations(rotations, true, sk)
	rlk := kgen.GenRelinearizationKey(sk)
	hbtpKey := BootstrappingKey{Rlk: rlk, Rtks: rotkeys}

	hbtp, err := NewHalfBootstrapper(params, hbtpParams, hbtpKey)
	if err != nil {
		t.Fatalf("NewHalfBootstrapper: %v", err)
	}

	fvEvaluator := NewMFVEvaluator(params, EvaluationKey{Rlk: rlk, Rtks: rotkeys}, pDcds)

	coeffs := make([][]float64, outputsize)
	for s := 0; s < outputsize; s++ {
		coeffs[s] = make([]float64, params.N())
	}

	key := make([]uint64, blocksize)
	for i := 0; i < blocksize; i++ {
		key[i] = uint64(i + 1)
	}

	data := make([][]float64, outputsize)
	for s := 0; s < outputsize; s++ {
		data[s] = make([]float64, params.N())
		for i := 0; i < params.N(); i++ {
			data[s][i] = utils.RandFloat64(-1, 1)
		}
	}

	nonces := make([][]byte, params.N())
	for i := 0; i < params.N(); i++ {
		nonces[i] = make([]byte, 8)
		if _, err := rand.Read(nonces[i]); err != nil {
			t.Fatalf("rand.Read (nonce): %v", err)
		}
	}
	counter := make([]byte, 8)
	if _, err := rand.Read(counter); err != nil {
		t.Fatalf("rand.Read (counter): %v", err)
	}

	keystream := make([][]uint64, params.N())
	for i := 0; i < params.N(); i++ {
		keystream[i] = plainRubato(blocksize, numRound, nonces[i], counter, key, plainModulus, sigma)
	}

	for s := 0; s < outputsize; s++ {
		for i := 0; i < params.N()/2; i++ {
			j := utils.BitReverse64(uint64(i), uint64(params.LogN()-1))
			coeffs[s][j] = data[s][i]
			coeffs[s][j+uint64(params.N()/2)] = data[s][i+params.N()/2]
		}
	}

	plainCKKSRingTs := make([]*PlaintextRingT, outputsize)
	for s := 0; s < outputsize; s++ {
		plainCKKSRingTs[s] = ckksEncoder.EncodeCoeffsRingTNew(coeffs[s], messageScaling)
		poly := plainCKKSRingTs[s].Value()[0]
		for i := 0; i < params.N(); i++ {
			j := utils.BitReverse64(uint64(i), uint64(params.LogN()))
			poly.Coeffs[0][j] = (poly.Coeffs[0][j] + keystream[i][s]) % params.PlainModulus()
		}
	}

	plaintexts := make([]*Plaintext, outputsize)
	for s := 0; s < outputsize; s++ {
		plaintexts[s] = NewPlaintextFVLvl(params, 0)
		fvEncoder.FVScaleUp(plainCKKSRingTs[s], plaintexts[s])
	}

	rubato := NewMFVRubato(rubatoParam, params, fvEncoder, fvEncryptor, fvEvaluator, rubatoModDown[0])
	kCt := rubato.EncKey(key)

	// Server-side: homomorphic Rubato decryption of the bank's upload
	// (output-element 0 only -- this harness proves correctness of one
	// lane, not throughput).
	fvKeystreams := rubato.Crypt(nonces, counter, kCt, rubatoModDown)
	fvKeystreams[0] = fvEvaluator.SlotsToCoeffs(fvKeystreams[0], stcModDown)
	fvEvaluator.ModSwitchMany(fvKeystreams[0], fvKeystreams[0], fvKeystreams[0].Level())

	ciphertext := NewCiphertextFVLvl(params, 1, 0)
	ciphertext.Value()[0] = plaintexts[0].Value()[0].CopyNew()
	fvEvaluator.Sub(ciphertext, fvKeystreams[0], ciphertext)
	fvEvaluator.TransformToNTT(ciphertext, ciphertext)
	ciphertext.SetScale(math.Exp2(math.Round(math.Log2(float64(params.Qi()[0]) / float64(params.PlainModulus()) * messageScaling))))

	// HalfBoot with repack=false: matches benchmarkRtFRubato's full-coefficient
	// (non-sparse) packing -- unlike HERA's "as" config, no repacking needed.
	ctBoot, _ := hbtp.HalfBoot(ciphertext, false)

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

	t.Logf("toy RtF+HalfBoot+CKKS-eval correctness (Rubato-128L, r=%d, sigma=%v): LogN=%d, slots=%d, max|got-want|=%e (tolerance=%e)",
		numRound, sigma, rtfToyLogNRubato128L, params.Slots(), maxAbsErr, tolerance)

	if maxAbsErr > tolerance {
		t.Fatalf("correctness check FAILED: max abs error %e exceeds tolerance %e", maxAbsErr, tolerance)
	}
}
