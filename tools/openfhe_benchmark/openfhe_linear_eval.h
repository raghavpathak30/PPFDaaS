// tools/openfhe_benchmark/openfhe_linear_eval.h — Phase 4, §4.3.
//
// OpenFHE re-implementation of the SAME depth-1 linear (logistic-regression)
// circuit evaluated by vendor_server/src/he_inference.cpp +
// rotation_hoisting.cpp under SEAL:
//
//   1. ct_mul = ct (*) pt_weights                 (multiply_plain + rescale)
//   2. ct_out.slot[k*256] = sum_{j=0}^{255} ct_mul.slot[k*256 + j]   for k=0..15
//
// using 16-lane / 256-feature packing (4096 slots total, see
// vendor_server/include/eval_context_160.h).
//
// Unlike SEAL's public API (vendor_server/src/rotation_hoisting.cpp,
// hoisted_tree_sum / bsgs_reduction — both NOT genuine hoisting, see
// docs/spec.md §7.1-§7.3), OpenFHE exposes the Halevi-Shoup hoisting
// primitive directly: EvalFastRotationPrecompute() computes the shared
// key-switching digit decomposition ONCE for a ciphertext, and
// EvalFastRotation() reuses that precomputation for each rotation index.
// step.2 above is therefore implemented as the SAME two-layer BSGS
// restructuring as bsgs_reduction() (vendor_server/include/rotation_hoisting.h,
// kBsgsRotationSteps below mirrors BSGS_ROTATION_STEPS there), but with the
// per-layer rotations sharing one precomputation instead of each being an
// independent full rotate_vector() call. This is "Strategy 3: Hoisted flat"
// in docs/spec.md §7.4.
#pragma once

#include "openfhe.h"

#include <array>
#include <vector>

namespace ppfdaas_openfhe {

// Mirrors vendor_server's BSGS_ROTATION_STEPS
// (vendor_server/include/rotation_hoisting.h, Phase 4 §4.1): 15 baby steps
// {1..15} + 15 giant steps {16,32,...,240} = 30 distinct rotation indices.
inline constexpr std::array<int32_t, 30> kBsgsRotationSteps = {
    // Baby steps: rotations of the post-multiply ciphertext, j = 1..15
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    // Giant steps: rotations of the baby-step partial sum, i*16 for i = 1..15
    16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240,
};

constexpr int kSlotCount = 4096;  // batch size: 16 lanes x 256 features
constexpr int kLanes = 16;
constexpr int kFeatures = 256;
constexpr int kBabyStep = 16;
constexpr int kGiantStep = 16;

// Holds the CryptoContext + key material for one benchmark run. Re-used
// across all warmup/measured iterations (KeyGen is not part of the timed
// circuit, matching vendor_server's benchmark_160.cpp).
struct LinearEvalContext {
    lbcrypto::CryptoContext<lbcrypto::DCRTPoly> cc;
    lbcrypto::KeyPair<lbcrypto::DCRTPoly> key_pair;
    lbcrypto::usint cyclotomic_order = 0;
};

// Builds a CKKS CryptoContext intended to be the closest OpenFHE equivalent
// of the SEAL 160-bit context (vendor_server/include/eval_context_160.h):
// ring dimension 8192, multiplicative depth 1, scale ~2^40, 16-lane/256-feature
// packing (batch size 4096), HEStd_128_classic. KeyGen includes EvalMult keys
// and EvalRotate keys for all of kBsgsRotationSteps. See README.md for the
// full parameter equivalence table and the caveats around OpenFHE's automatic
// ring-dimension selection (OpenFHE may choose a ring dimension other than
// 8192 for a given depth + security level; this is itself part of the
// cross-library finding, §7.5).
LinearEvalContext build_context();

// Per-stage timing for one end-to-end run of the circuit described above.
// "precompute" fields are EvalFastRotationPrecompute() — the shared digit
// decomposition that genuine hoisting amortizes across the rotations in the
// corresponding layer. "rotations_*_total" is the SUM of the per-rotation
// EvalFastRotation() times within that layer (15 calls each for baby/giant).
struct CircuitTiming {
    double encrypt_us = 0.0;
    double eval_mult_us = 0.0;          // includes automatic rescale (FLEXIBLEAUTO)
    double precompute_baby_us = 0.0;    // EvalFastRotationPrecompute(ct_mul), once
    double rotations_baby_us = 0.0;     // sum of 15 x EvalFastRotation
    double precompute_giant_us = 0.0;   // EvalFastRotationPrecompute(baby_acc), once
    double rotations_giant_us = 0.0;    // sum of 15 x EvalFastRotation
    double decrypt_us = 0.0;
    double total_us = 0.0;              // encrypt .. decrypt, end-to-end

    // In-band parity gate (Phase 4 / Phase 5.5 pattern): max over k=0..15 of
    // |decoded.slot[k*256] - expected[k]|, where expected[k] is the plaintext
    // dot product for lane k.
    double max_abs_error = 0.0;
};

// Runs the full circuit once on `features`/`weights` (each kSlotCount=4096
// long: 16 lanes x 256 features), returning per-stage timings (including the
// correctness gate against the plaintext oracle computed internally) and the
// decoded packed result in `decoded_out` (length kSlotCount; lane k's
// dot product is decoded_out[k*kFeatures]).
CircuitTiming run_circuit_hoisted(
        LinearEvalContext& ctx,
        const std::vector<double>& features,
        const std::vector<double>& weights,
        std::vector<double>& decoded_out);

} // namespace ppfdaas_openfhe
