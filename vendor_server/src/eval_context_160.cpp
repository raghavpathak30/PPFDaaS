#include "eval_context_160.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <vector>

EvalContext160::EvalContext160() : params(seal::scheme_type::ckks) {
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60, 40, 60}));

    const auto coeffs = params.coeff_modulus();
    int total_coeff_bits = 0;
    for (const auto &mod : coeffs) {
        total_coeff_bits += static_cast<int>(mod.bit_count());
    }
    if (total_coeff_bits != 160) {
        throw std::runtime_error("EvalContext160: total coeff modulus bits must be 160");
    }

    // ─── §3.1 Security-level justification (Phase 3, Item 3.1) ─────────────
    //
    // Parameters: poly_modulus_degree (N) = 8192, coeff_modulus = {60, 40, 60}
    // bits, total_coeff_modulus_bit_count = 160 bits (this is the KEY-level
    // chain, i.e. all 3 primes -- see below for the DATA chain after dropping
    // the special key-switching modulus).
    //
    // HomomorphicEncryption.org Security Standard v1.1, Table 2 (ternary
    // secret, 128-bit classical security, "tc128") gives a maximum total
    // coeff_modulus bit count of 218 bits for N=8192. 160 <= 218: this chain
    // satisfies tc128 with 58 bits to spare.
    //
    // SEAL 4.x enforces this bound directly and exactly: the table above is
    // hard-coded as seal::util::seal_he_std_parms_128_tc() in
    // seal/util/hestdparms.h, and SEALContext::Validate (context.cpp) compares
    // total_coeff_modulus_bit_count_ (the sum over ALL coeff_modulus primes,
    // computed for the key-level context data) against
    // CoeffModulus::MaxBitCount(poly_modulus_degree, sec_level). If that bound
    // is exceeded, SEALContext does NOT throw directly -- it sets
    // qualifiers().parameter_error = error_type::invalid_parameters_insecure
    // and parameters_set() == false. The `if (!context->parameters_set())
    // throw ...` immediately below is what converts that into a fail-closed
    // std::runtime_error, so the 128-bit claim is enforced at construction,
    // not asserted in a comment.
    //
    // NOTE: seal::SEALContext's `sec_level` constructor argument already
    // DEFAULTS to sec_level_type::tc128 (seal/context.h), so this bound was
    // implicitly enforced even before this change. Passing it explicitly
    // below removes the dependency on that easy-to-miss library default and
    // makes the security claim self-documenting and grep-able.
    //
    // Depth budget: this is a depth-1 linear circuit (one multiply_plain_inplace
    // + one rescale_to_next_inplace; rotations consume no level). Dropping the
    // 60-bit special key-switching modulus from the 3-prime {60,40,60} KEY
    // chain leaves a 2-prime {60,40} = 100-bit DATA chain at
    // context->first_parms_id(). rescale_to_next_inplace drops the trailing
    // 40-bit prime, leaving a 1-prime {60} = 60-bit DATA chain at
    // `second_parms_id` (set below). That is exactly 1 data level consumed by
    // exactly 1 rescale -- sufficient for this depth-1 circuit, and no more,
    // by design.
    //
    // Citations:
    //   - HomomorphicEncryption.org Security Standard, v1.1, Table 2.
    //   - SEAL 4.x source: seal/util/hestdparms.h (seal_he_std_parms_128_tc),
    //     seal/context.cpp (SEALContext::Validate, tc128 enforcement).
    //
    // ─── §3.2 Precision justification (Phase 3, Item 3.2) ──────────────────
    //
    // scale = 2^40 ~= 1.0995e12 (see `scale` member below). The middle prime
    // of the DATA chain is also 40 bits, so a single rescale_to_next_inplace
    // (which divides the ciphertext scale by that 40-bit prime, ~2^40) brings
    // the post-rescale scale back to ~2^40 -- i.e. the scale and the dropped
    // prime are chosen to match, so rescale is a clean no-op on the
    // represented scale (this is why pt_bias_'s bias_scale in
    // inference_service_160.cpp is computed as scale*scale/q_last and lands
    // back at ~2^40, matching the rescaled ciphertext's scale exactly).
    //
    // Expected CKKS noise after this depth-1 circuit (1 multiply_plain + 1
    // rescale + rotations/adds, which do not add rescaling noise) is
    // O(2^-40) relative error from encoding/rescaling alone.
    //
    // Observed (measured, not estimated -- see scripts/precision_analysis.py
    // and artifacts/precision_analysis.json):
    //   - Full held-out test set (n=56,962, Phase 0 artifacts/errors.json):
    //     max_abs_error (MaxAE) = 4.344e-07, i.e. log2(4.344e-07) ~= -21.13,
    //     so the achieved absolute error sits ~21.1 bits below 1.0. Of the
    //     40-bit scale, ~18.9 bits are "spent" reaching that error floor,
    //     leaving ~21.1 bits of headroom below the noise floor that remain
    //     usable for downstream computation (e.g. a Phase 4 multi-layer
    //     circuit).
    //   - scale headroom = log2(scale / MaxAE) ~= 61.1 bits (see
    //     artifacts/precision_analysis.json for the exact figure).
    //   - Per-stage breakdown for a representative 4096-slot batch (16
    //     transactions), also in artifacts/precision_analysis.json: the
    //     error grows from ~1e-10 (mean) after multiply_plain, to ~9e-10
    //     after rescale, to ~1.3e-7 (mean; max 1.3e-6) after
    //     hoisted_tree_sum's 8 rotations/additions accumulate per-rotation
    //     noise, essentially unchanged by the final add_plain bias term.
    //
    // Trade-off: a 30-bit scale would also be more than sufficient for
    // correctness at depth-1 (an extra ~10 bits of error is still <<1 in
    // absolute terms), but would reduce the distinguishability of sigmoid
    // scores in the tails (where small logit differences matter most for
    // ranking borderline-fraud transactions). The 40-bit scale is therefore a
    // conservative, deliberate choice for this depth-1 circuit, not an
    // unexamined default.
    context = std::make_shared<seal::SEALContext>(params, true, seal::sec_level_type::tc128);
    if (!context->parameters_set()) {
        throw std::runtime_error("EvalContext160: SEAL rejected parameters (insecure or invalid)");
    }

    // NOTE: deliberately NO seal::KeyGenerator, NO seal::SecretKey, NO
    // seal::PublicKey, NO seal::Encryptor, NO seal::Decryptor anywhere in
    // this constructor or this type. galois_keys is populated later, only
    // via load_and_validate_galois_keys() during provisioning (§1.5).
    encoder.emplace(*context);
    evaluator.emplace(*context);

    second_parms_id = context->first_context_data()->next_context_data()->parms_id();

    // Plaintext-side sanity check (§1.1): the previous constructor self-encrypted
    // a dummy vector to prove the depth-1 circuit stayed decodable after a
    // rescale. That required a live Encryptor + Decryptor (i.e. a secret key)
    // on the server, which is exactly the capability this type must not have.
    // The only thing this constructor can verify without keys is that the
    // CKKS encoder round-trips correctly for this parameter set.
    // CKKS slot count == poly_modulus_degree / 2 == 4096 for this parameter set.
    std::vector<double> probe(encoder->slot_count(), 0.5);
    seal::Plaintext pt;
    encoder->encode(probe, scale, pt);
    std::vector<double> decoded;
    encoder->decode(pt, decoded);

    const bool ok = decoded.size() == probe.size() &&
        std::all_of(decoded.begin(), decoded.end(), [](double v) {
            return std::isfinite(v) && std::abs(v - 0.5) < 1e-6;
        });
    if (!ok) {
        throw std::runtime_error("EvalContext160: CKKS encoder round-trip sanity check failed");
    }
}

void EvalContext160::load_and_validate_galois_keys(std::istream &in) {
    seal::GaloisKeys candidate;
    try {
        candidate.load(*context, in);
    } catch (const std::exception &e) {
        throw std::runtime_error(std::string("EvalContext160: failed to deserialize Galois keys: ") + e.what());
    }

    // Rung (a)-1: keys must have been generated under THIS server's encryption
    // parameters (the key-level parms_id covers the full coeff_modulus chain).
    const auto expected_parms_id = context->key_context_data()->parms_id();
    if (candidate.parms_id() != expected_parms_id) {
        throw std::runtime_error(
            "EvalContext160: Galois key parms_id does not match this server's encryption "
            "parameters -- the keys were generated for a different parameter set");
    }

    // Rung (a)-2: the key set must contain EVERY Galois element the rotation
    // schedule needs, checked eagerly and completely (not discovered lazily
    // the first time hoisted_tree_sum is called mid-inference).
    const auto *galois_tool = context->key_context_data()->galois_tool();
    const std::vector<int> steps(ROTATION_STEPS.begin(), ROTATION_STEPS.end());
    const auto required_elts = galois_tool->get_elts_from_steps(steps);
    for (std::size_t i = 0; i < required_elts.size(); ++i) {
        if (!candidate.has_key(required_elts[i])) {
            throw std::runtime_error(
                "EvalContext160: Galois keys are missing the key for rotation step " +
                std::to_string(ROTATION_STEPS[i]) + " (Galois element " +
                std::to_string(required_elts[i]) + "), which hoisted_tree_sum requires");
        }
    }

    // NOTE: structural validation can confirm the keys are well-formed for
    // this parameter set and cover the required rotations. It CANNOT confirm
    // they were generated under the bank's secret key (§1.2b / Bug B) -- a
    // Galois key set generated under a DIFFERENT secret key but the SAME
    // parameters passes every check above and still produces ~1e18-magnitude
    // garbage on rotation. Only the canary handshake (behavioral rung) can
    // catch that, because only the bank can decrypt the result.
    galois_keys = std::move(candidate);
    galois_keys_loaded = true;
}
