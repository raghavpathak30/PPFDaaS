// vendor_server/src/ckks_context.cpp  — NORMATIVE
#include "ckks_context.h"

#include <cmath>

#include <stdexcept>

#include <vector>


CKKSContext::CKKSContext() : params(seal::scheme_type::ckks) {

    // Hard constraints from §0 of the blueprint:

    // coeff_modulus {60,40,40,60} = 200 bits total

    // depth budget = 2 middle primes; we use exactly 1

    params.set_poly_modulus_degree(8192);

    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60,40,40,60}));

    context = std::make_shared<seal::SEALContext>(params);

    if (!context->parameters_set())

        throw std::runtime_error("CKKSContext: SEAL rejected parameters");

    seal::KeyGenerator keygen(*context);

    secret_key = keygen.secret_key();

    keygen.create_public_key(public_key);

    const std::vector<int> galois_steps{1, 2, 4, 8, 16, 32, 64, 128};

    keygen.create_galois_keys(galois_steps, galois_keys);

    encoder   = std::make_unique<seal::CKKSEncoder>(*context);

    encryptor = std::make_unique<seal::Encryptor>(*context, public_key);

    decryptor = std::make_unique<seal::Decryptor>(*context, secret_key);

    evaluator = std::make_unique<seal::Evaluator>(*context);

    // Compute second_parms_id: the parms_id AFTER one rescale_to_next

    second_parms_id =

        context->first_context_data()->next_context_data()->parms_id();

    // Runtime sanity: verify depth-1 circuit leaves noise budget > 0

    std::vector<double> dummy(4096, 0.5);

    seal::Plaintext pt1, pt2;

    seal::Ciphertext ct;

    encoder->encode(dummy, scale, pt1);

    encoder->encode(dummy, scale, pt2);

    encryptor->encrypt(pt1, ct);

    evaluator->multiply_plain_inplace(ct, pt2);

    evaluator->rescale_to_next_inplace(ct);

    // CKKS: invariant_noise_budget is unsupported in SEAL 4.x; decrypt+decode sanity instead
    const std::size_t slots = encoder->slot_count();
    if (!ckks_ciphertext_decrypts_cleanly(*decryptor, *encoder, ct, slots)) {
        throw std::runtime_error(
            "CKKSContext: depth-1 circuit decrypt/decode failed or non-finite output");
    }

}

bool ckks_ciphertext_decrypts_cleanly(
        seal::Decryptor& decryptor,
        seal::CKKSEncoder& encoder,
        const seal::Ciphertext& ct,
        std::size_t slot_count) {
    seal::Plaintext pt;
    decryptor.decrypt(ct, pt);
    std::vector<double> decoded;
    encoder.decode(pt, decoded);
    if (decoded.size() < slot_count) {
        return false;
    }
    for (std::size_t i = 0; i < slot_count; ++i) {
        if (!std::isfinite(decoded[i])) {
            return false;
        }
    }
    return true;
}
