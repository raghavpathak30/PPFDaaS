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

    context = std::make_shared<seal::SEALContext>(params);
    if (!context->parameters_set()) {
        throw std::runtime_error("EvalContext160: SEAL rejected parameters");
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
