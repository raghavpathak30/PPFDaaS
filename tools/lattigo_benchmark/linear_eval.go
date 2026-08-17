// tools/lattigo_benchmark/linear_eval.go
//
// Lattigo v6 implementation of the same depth-1 linear-eval circuit as
// tools/openfhe_benchmark/openfhe_linear_eval.cpp, for a true matched-ring,
// matched-rotation, library-only delta (docs/spec.md §7.5).
//
// Circuit: ct_mul = ct_features (*) pt_weights (+ rescale), then BSGS two-layer
// hoisted reduction over 256 slots/lane × 16 lanes = 4096 total slots.
//
// Security parameters:
//   LogN=13 (N=8192) — matched ring with SEAL's deployed 160-bit config.
//   LogQ={60,60,40}, LogP={58} → total LogQP=218 bits, exactly matching SEAL's
//   N=8192/tc128 ceiling (seal/util/hestdparms.h:31-32, CONFIRMED-SOURCE prior session).
//   Prime ordering: Q[0]=60-bit (output/base prime at level 0), Q[1]=60-bit (spare),
//   Q[2]=40-bit (scaling prime at MaxLevel=2, consumed first by Rescale).
//   This makes scale after Mul+Rescale = 2^80/2^40 = 2^40 = DefaultScale, matching
//   the standard CKKS convention seen in Lattigo's ExampleParameters128BitLogN14LogQP438.
//   (Wrong ordering {60,40,60} gives scale=2^20 after Rescale → parity gate failure.)
//   Lattigo's NewParametersFromLiteral imposes NO standards-table gate — security
//   is caller-asserted (RESEARCH_FINDINGS_v2.md §A3, CONFIRMED-RAN prior session).
//
// Rotation set: kBabySteps ∪ kGiantSteps = {1..15} ∪ {16,32,...,240}, 30 elements,
// mirroring vendor_server/include/rotation_hoisting.h's BSGS_ROTATION_STEPS.
//
// Genuine hoisting: RotateHoistedNew(ct, rotations) computes the key-switching
// digit decomposition (ModDown) ONCE for ct, then reuses it across all rotations in
// that call — the same Halevi-Shoup amortization as OpenFHE's EvalFastRotationPrecompute
// (docs/spec.md §7.4). SEAL's public rotate_vector() cannot express this (§7.1-§7.3):
// each call performs its own full key-switch (decomposition + automorphism + ModUp).
// This is the library-only delta that §7.5 isolates; now achievable at matched
// ring dimension via Lattigo, resolving the ring-dimension confound in §7.5.1.

package main

import (
	"math"
	"time"

	"github.com/tuneinsight/lattigo/v6/core/rlwe"
	"github.com/tuneinsight/lattigo/v6/schemes/ckks"
)

// Slot / lane / feature constants mirror vendor_server/include/eval_context_160.h:
// 16 lanes × 256 features/lane = 4096 total CKKS slots = N/2 at N=8192.
const (
	kSlotCount = 4096
	kLanes     = 16
	kFeatures  = 256
	kBabyStep  = 16 // sqrt(kFeatures)
	kGiantStep = 16 // sqrt(kFeatures)
)

// kBabySteps: j = 1..15 (rotations of ct_mul by j; ct_mul itself is the j=0 term).
// Mirrors the first 15 elements of BSGS_ROTATION_STEPS in rotation_hoisting.h.
var kBabySteps = []int{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}

// kGiantSteps: i*16 for i = 1..15 (rotations of baby_acc; baby_acc is the i=0 term).
// Mirrors the last 15 elements of BSGS_ROTATION_STEPS in rotation_hoisting.h.
var kGiantSteps = []int{16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240}

// LinearEvalContext holds CKKS parameters + key material.
// Allocated once before warmup; reused across all warmup and measured runs
// (key generation is not part of the timed circuit, matching openfhe_linear_eval.cpp).
type LinearEvalContext struct {
	params    ckks.Parameters
	sk        *rlwe.SecretKey
	encoder   *ckks.Encoder
	encryptor *rlwe.Encryptor
	decryptor *rlwe.Decryptor
	evaluator *ckks.Evaluator
}

// CircuitTiming holds per-stage latencies for one end-to-end run.
// Layout mirrors openfhe_benchmark.cpp's CircuitTiming, adapted for Lattigo's
// combined precompute+rotation API (RotateHoistedNew, unlike OpenFHE's
// separate EvalFastRotationPrecompute + EvalFastRotation calls).
type CircuitTiming struct {
	EncryptUs        float64 // encode features (plaintext) + encrypt → ciphertext
	EvalMultUs       float64 // Mul(ct_feat, pt_weights) + Rescale (= multiply_plain + rescale_to_next)
	HoistedBabyUs    float64 // RotateHoistedNew(ct_mul, kBabySteps): precompute+15 rotations
	AccumulateBabyUs float64 // 15 Add ops: baby_acc = ct_mul + sum(baby rotations)
	HoistedGiantUs   float64 // RotateHoistedNew(baby_acc, kGiantSteps): precompute+15 rotations
	AccumulateGiantUs float64 // 15 Add ops: acc = baby_acc + sum(giant rotations)
	DecryptUs        float64 // decrypt + decode
	TotalUs          float64 // end-to-end (encrypt .. decrypt+decode)
	MaxAbsError      float64 // parity gate: max over k=0..15 of |decoded[k*256] - expected_k|
}

// BuildContext allocates CKKS parameters and generates all key material.
// Parameters: LogN=13, LogQ={60,60,40}, LogP={58} → total LogQP=218 bits
// = SEAL's N=8192/tc128 ceiling from hestdparms.h [CONFIRMED-SOURCE].
// Galois keys for all 30 BSGS rotation indices are generated here.
func BuildContext() (LinearEvalContext, error) {
	params, err := ckks.NewParametersFromLiteral(ckks.ParametersLiteral{
		LogN:            13,
		LogQ:            []int{60, 60, 40},
		LogP:            []int{58},
		LogDefaultScale: 40,
	})
	if err != nil {
		return LinearEvalContext{}, err
	}

	kgen := ckks.NewKeyGenerator(params)
	sk, pk := kgen.GenKeyPairNew()

	allRotations := append(append([]int{}, kBabySteps...), kGiantSteps...)
	galEls := params.GaloisElements(allRotations)
	gks := kgen.GenGaloisKeysNew(galEls, sk)
	evk := rlwe.NewMemEvaluationKeySet(nil, gks...)

	return LinearEvalContext{
		params:    params,
		sk:        sk,
		encoder:   ckks.NewEncoder(params),
		encryptor: ckks.NewEncryptor(params, pk),
		decryptor: ckks.NewDecryptor(params, sk),
		evaluator: ckks.NewEvaluator(params, evk),
	}, nil
}

func durUs(d time.Duration) float64 { return float64(d.Nanoseconds()) / 1e3 }

// RunCircuitHoisted runs one end-to-end circuit on features/weights (each
// length kSlotCount=4096: 16 lanes × 256 features per lane). Returns per-stage
// timings and the in-band parity-gate result (MaxAbsError).
//
// Matches openfhe_linear_eval.cpp's run_circuit_hoisted() in circuit structure
// and timing discipline; adapted for Lattigo v6's API.
func RunCircuitHoisted(ctx *LinearEvalContext, features, weights []float64) CircuitTiming {
	var t CircuitTiming
	params := ctx.params
	enc := ctx.encoder
	eval := ctx.evaluator

	// Encode plaintext inputs (outside timed region — same as OpenFHE benchmark
	// which calls MakeCKKSPackedPlaintext before the encrypt timer starts).
	ptFeatures := ckks.NewPlaintext(params, params.MaxLevel())
	ptWeights := ckks.NewPlaintext(params, params.MaxLevel())
	if err := enc.Encode(features, ptFeatures); err != nil {
		panic(err)
	}
	if err := enc.Encode(weights, ptWeights); err != nil {
		panic(err)
	}

	// ── Encrypt ───────────────────────────────────────────────────────────────
	t0 := time.Now()
	ctFeat, err := ctx.encryptor.EncryptNew(ptFeatures)
	if err != nil {
		panic(err)
	}
	t1 := time.Now()
	t.EncryptUs = durUs(t1.Sub(t0))

	// ── multiply_plain + rescale_to_next ──────────────────────────────────────
	// Mul(ct, pt): ct_mul.Scale = 2^40 * 2^40 = 2^80 at level 2.
	// Rescale: divides by Q[2] ≈ 2^40 (the scaling prime at MaxLevel) →
	// ct_mul.Scale ≈ 2^40 at level 1. Equivalent to SEAL's
	// multiply_plain_inplace + rescale_to_next_inplace.
	ctMul := ckks.NewCiphertext(params, 1, params.MaxLevel())
	if err := eval.Mul(ctFeat, ptWeights, ctMul); err != nil {
		panic(err)
	}
	if err := eval.Rescale(ctMul, ctMul); err != nil {
		panic(err)
	}
	t2 := time.Now()
	t.EvalMultUs = durUs(t2.Sub(t1))

	// ── Layer 1 (baby steps): genuine Halevi-Shoup hoisting ──────────────────
	// RotateHoistedNew computes the key-switching digit decomposition (ModDown)
	// for ctMul ONCE, then applies each of the 15 Galois automorphisms in
	// kBabySteps, reusing that precomputation. This is the amortization that
	// SEAL's public API cannot express (docs/spec.md §7.1-§7.3).
	// Compare directly against OpenFHE's precompute_baby_us + rotations_baby_total_us.
	babyRotated, err := eval.RotateHoistedNew(ctMul, kBabySteps)
	if err != nil {
		panic(err)
	}
	t3 := time.Now()
	t.HoistedBabyUs = durUs(t3.Sub(t2))

	// Accumulate baby-step partial sums: baby_acc = ctMul (j=0) + sum_{j=1..15}
	babyAcc := ctMul.CopyNew()
	for _, j := range kBabySteps {
		if err := eval.Add(babyAcc, babyRotated[j], babyAcc); err != nil {
			panic(err)
		}
	}
	t4 := time.Now()
	t.AccumulateBabyUs = durUs(t4.Sub(t3))

	// ── Layer 2 (giant steps): genuine hoisting on baby_acc ──────────────────
	giantRotated, err := eval.RotateHoistedNew(babyAcc, kGiantSteps)
	if err != nil {
		panic(err)
	}
	t5 := time.Now()
	t.HoistedGiantUs = durUs(t5.Sub(t4))

	// Accumulate giant-step partial sums: acc = baby_acc (i=0) + sum_{i=1..15}
	acc := babyAcc.CopyNew()
	for _, step := range kGiantSteps {
		if err := eval.Add(acc, giantRotated[step], acc); err != nil {
			panic(err)
		}
	}
	t6 := time.Now()
	t.AccumulateGiantUs = durUs(t6.Sub(t5))

	// ── Decrypt + decode ──────────────────────────────────────────────────────
	ptResult := ctx.decryptor.DecryptNew(acc)
	decoded := make([]float64, kSlotCount)
	if err := enc.Decode(ptResult, decoded); err != nil {
		panic(err)
	}
	t7 := time.Now()
	t.DecryptUs = durUs(t7.Sub(t6))
	t.TotalUs = durUs(t7.Sub(t0))

	// ── In-band parity gate ───────────────────────────────────────────────────
	// For each lane k=0..15: acc.slot[k*256] should equal the plaintext dot product
	// sum_{j=0}^{255} weights[k*256+j] * features[k*256+j]. Threshold 1e-3
	// matches openfhe_benchmark.cpp's kTolerance. Fails the benchmark if exceeded.
	var maxAbsErr float64
	for k := 0; k < kLanes; k++ {
		var expected float64
		for j := 0; j < kFeatures; j++ {
			expected += weights[k*kFeatures+j] * features[k*kFeatures+j]
		}
		got := decoded[k*kFeatures]
		maxAbsErr = math.Max(maxAbsErr, math.Abs(got-expected))
	}
	t.MaxAbsError = maxAbsErr

	return t
}
