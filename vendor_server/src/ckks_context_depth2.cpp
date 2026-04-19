// he_core/src/ckks_context_depth2.cpp  — NORMATIVE

#include "ckks_context_depth2.h"

#include <algorithm>

#include <cmath>

#include <stdexcept>

#include <vector>

CKKSContextDepth2::CKKSContextDepth2() : params(seal::scheme_type::ckks) {
    params.set_poly_modulus_degree(16384);
    params.set_coeff_modulus(seal::CoeffModulus::Create(16384, {60,40,40,40,60}));
    context = std::make_shared<seal::SEALContext>(params);
    if (!context->parameters_set())
        throw std::runtime_error("CKKSContextDepth2: SEAL rejected parameters");

    seal::KeyGenerator keygen(*context);
    secret_key = keygen.secret_key();
    keygen.create_public_key(public_key);
    keygen.create_galois_keys(std::vector<int>{1,2,4,8,16,32,64,128,256}, galois_keys);

    encoder.emplace(*context);
    encryptor.emplace(*context, public_key);
    decryptor.emplace(*context, secret_key);
    evaluator.emplace(*context);
    second_parms_id =
        context->first_context_data()->next_context_data()->parms_id();

    std::vector<double> dummy(8192, 0.5);
    seal::Plaintext pt1, pt2; seal::Ciphertext ct;
    encoder->encode(dummy, scale, pt1);
    encoder->encode(dummy, scale, pt2);
    encryptor->encrypt(pt1, ct);
    evaluator->multiply_plain_inplace(ct, pt2);
    evaluator->rescale_to_next_inplace(ct);
    seal::Plaintext pt_out;
    std::vector<double> decoded;
    decryptor->decrypt(ct, pt_out);
    encoder->decode(pt_out, decoded);
    const bool any_non_finite =
        std::any_of(decoded.begin(), decoded.end(), [](double v) { return !std::isfinite(v); });
    if (any_non_finite)
        throw std::runtime_error("CKKSContextDepth2: decrypt/decode sanity check failed");
}
