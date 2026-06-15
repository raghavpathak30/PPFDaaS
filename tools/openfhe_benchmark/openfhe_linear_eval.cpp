// tools/openfhe_benchmark/openfhe_linear_eval.cpp — Phase 4, §4.3.
//
// See openfhe_linear_eval.h for the circuit description and the mapping back
// to vendor_server/src/rotation_hoisting.cpp's bsgs_reduction().

#include "openfhe_linear_eval.h"

#include <algorithm>
#include <chrono>
#include <cmath>

namespace ppfdaas_openfhe {

using namespace lbcrypto;
using clock_type = std::chrono::high_resolution_clock;

namespace {
double us(const clock_type::time_point& t0, const clock_type::time_point& t1) {
    return std::chrono::duration<double, std::micro>(t1 - t0).count();
}
} // namespace

LinearEvalContext build_context() {
    CCParams<CryptoContextCKKSRNS> parameters;
    parameters.SetMultiplicativeDepth(1);
    parameters.SetScalingModSize(40);
    parameters.SetBatchSize(kSlotCount);
    parameters.SetSecurityLevel(HEStd_128_classic);
    // OpenFHE picks a ring dimension automatically from
    // (multiplicative depth, security level, scaling mod size); 8192 is the
    // SEAL 160-bit context's ring dimension and is requested explicitly here
    // for parameter-equivalence, but OpenFHE may override it if its own
    // parameter selection determines a larger ring is required at this
    // security level / depth (see README.md, "Ring dimension" row).
    parameters.SetRingDim(8192);

    CryptoContext<DCRTPoly> cc = GenCryptoContext(parameters);
    cc->Enable(PKE);
    cc->Enable(KEYSWITCH);
    cc->Enable(LEVELEDSHE);
    cc->Enable(ADVANCEDSHE); // EvalFastRotationPrecompute / EvalFastRotation

    LinearEvalContext ctx;
    ctx.cc = cc;
    ctx.key_pair = cc->KeyGen();
    cc->EvalMultKeyGen(ctx.key_pair.secretKey);

    std::vector<int32_t> rotation_indices(kBsgsRotationSteps.begin(), kBsgsRotationSteps.end());
    cc->EvalRotateKeyGen(ctx.key_pair.secretKey, rotation_indices);

    ctx.cyclotomic_order = cc->GetCyclotomicOrder();
    return ctx;
}

CircuitTiming run_circuit_hoisted(
        LinearEvalContext& ctx,
        const std::vector<double>& features,
        const std::vector<double>& weights,
        std::vector<double>& decoded_out) {
    CircuitTiming timing;
    auto& cc = ctx.cc;

    Plaintext pt_features = cc->MakeCKKSPackedPlaintext(features);
    Plaintext pt_weights = cc->MakeCKKSPackedPlaintext(weights);

    const auto t0 = clock_type::now();
    auto ct = cc->Encrypt(ctx.key_pair.publicKey, pt_features);
    const auto t1 = clock_type::now();
    timing.encrypt_us = us(t0, t1);

    // multiply_plain + rescale (FLEXIBLEAUTO rescales automatically inside
    // EvalMult), matching vendor_server's
    // multiply_plain_inplace + rescale_to_next_inplace pair.
    auto ct_mul = cc->EvalMult(ct, pt_weights);
    const auto t2 = clock_type::now();
    timing.eval_mult_us = us(t1, t2);

    // ── Layer 1 (baby steps, j=1..15): genuine hoisting ─────────────────────
    // EvalFastRotationPrecompute computes the shared key-switching digit
    // decomposition for ct_mul ONCE; every EvalFastRotation below reuses it.
    // This is the amortization that hoisted_tree_sum / bsgs_reduction CANNOT
    // express through SEAL's public API (docs/spec.md §7.1).
    const auto t3 = clock_type::now();
    auto precomp_baby = cc->EvalFastRotationPrecompute(ct_mul);
    const auto t4 = clock_type::now();
    timing.precompute_baby_us = us(t3, t4);

    auto baby_acc = ct_mul;
    double rotations_baby_us = 0.0;
    for (int32_t j = 1; j < kBabyStep; ++j) {
        const auto ta = clock_type::now();
        auto rotated = cc->EvalFastRotation(ct_mul, j, ctx.cyclotomic_order, precomp_baby);
        const auto tb = clock_type::now();
        rotations_baby_us += us(ta, tb);
        baby_acc = cc->EvalAdd(baby_acc, rotated);
    }
    timing.rotations_baby_us = rotations_baby_us;

    // ── Layer 2 (giant steps, i=1..15): genuine hoisting on baby_acc ────────
    const auto t5 = clock_type::now();
    auto precomp_giant = cc->EvalFastRotationPrecompute(baby_acc);
    const auto t6 = clock_type::now();
    timing.precompute_giant_us = us(t5, t6);

    auto acc = baby_acc;
    double rotations_giant_us = 0.0;
    for (int32_t i = 1; i < kGiantStep; ++i) {
        const auto ta = clock_type::now();
        auto rotated = cc->EvalFastRotation(baby_acc, i * kBabyStep, ctx.cyclotomic_order, precomp_giant);
        const auto tb = clock_type::now();
        rotations_giant_us += us(ta, tb);
        acc = cc->EvalAdd(acc, rotated);
    }
    timing.rotations_giant_us = rotations_giant_us;

    const auto t7 = clock_type::now();
    Plaintext result;
    cc->Decrypt(ctx.key_pair.secretKey, acc, &result);
    const auto t8 = clock_type::now();
    timing.decrypt_us = us(t7, t8);

    result->SetLength(kSlotCount);
    decoded_out = result->GetRealPackedValue();
    timing.total_us = us(t0, t8);

    // ── In-band parity gate against the plaintext oracle ────────────────────
    double max_abs_error = 0.0;
    for (int k = 0; k < kLanes; ++k) {
        double expected = 0.0;
        for (int j = 0; j < kFeatures; ++j) {
            expected += weights[k * kFeatures + j] * features[k * kFeatures + j];
        }
        const double got = decoded_out[k * kFeatures];
        max_abs_error = std::max(max_abs_error, std::fabs(got - expected));
    }
    timing.max_abs_error = max_abs_error;

    return timing;
}

} // namespace ppfdaas_openfhe
