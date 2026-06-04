// he_core/src/ckks_context.cpp  — NORMATIVE

#include "ckks_context.h"

#include <algorithm>

#include <cmath>

#include <fstream>

#include <iostream>

#include <stdexcept>

#include <string>

#include <vector>

CKKSContext::CKKSContext() : params(seal::scheme_type::ckks) {
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60,40,40,60}));
    context = std::make_shared<seal::SEALContext>(params);
    if (!context->parameters_set())
        throw std::runtime_error("CKKSContext: SEAL rejected parameters");

    seal::KeyGenerator keygen(*context);
    secret_key = keygen.secret_key();
    keygen.create_public_key(public_key);

    const std::string galois_path = std::string(PPFDAAS_REPO_ROOT) + "/artifacts/galois_keys.bin";
    std::ifstream galois_in(galois_path, std::ios::binary);
    if (galois_in.is_open()) {
        try {
            galois_keys.load(*context, galois_in);
        } catch (const std::exception& e) {
            throw std::runtime_error(std::string("CKKSContext: failed to load galois_keys.bin: ") + e.what());
        }
    } else {
        std::cerr << "[Server] WARNING: missing " << galois_path
                  << "; generating local Galois keys (results may be semantically incorrect for client ciphertexts)\n";
        keygen.create_galois_keys(std::vector<int>{1,2,4,8,16,32,64,128}, galois_keys);
    }

    encoder.emplace(*context);
    encryptor.emplace(*context, public_key);
    decryptor.emplace(*context, secret_key);
    evaluator.emplace(*context);

    second_parms_id =
        context->first_context_data()->next_context_data()->parms_id();

    std::vector<double> dummy(4096, 0.5);
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
        throw std::runtime_error("CKKSContext: decrypt/decode sanity check failed after depth-1");
}
