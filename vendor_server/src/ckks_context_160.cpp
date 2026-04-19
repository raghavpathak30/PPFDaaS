#include "ckks_context_160.h"

#include <algorithm>
#include <cmath>
#include <numeric>
#include <stdexcept>
#include <vector>

CKKSContext160::CKKSContext160() : params(seal::scheme_type::ckks) {
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60, 40, 60}));

    const auto coeffs = params.coeff_modulus();
    int total_coeff_bits = 0;
    for (const auto &mod : coeffs) {
        total_coeff_bits += static_cast<int>(mod.bit_count());
    }
    if (total_coeff_bits != 160) {
        throw std::runtime_error("CKKSContext160: total coeff modulus bits must be 160");
    }

    context = std::make_shared<seal::SEALContext>(params);
    if (!context->parameters_set()) {
        throw std::runtime_error("CKKSContext160: SEAL rejected parameters");
    }

    seal::KeyGenerator keygen(*context);
    secret_key = keygen.secret_key();
    keygen.create_public_key(public_key);
    keygen.create_galois_keys(std::vector<int>{1, 2, 4, 8, 16, 32, 64, 128}, galois_keys);

    encoder.emplace(*context);
    encryptor.emplace(*context, public_key);
    decryptor.emplace(*context, secret_key);
    evaluator.emplace(*context);

    second_parms_id = context->first_context_data()->next_context_data()->parms_id();

    // Keep the same depth-1 sanity gate: one multiply_plain + one rescale must remain decryptable.
    std::vector<double> dummy(4096, 0.5);
    seal::Plaintext pt1;
    seal::Plaintext pt2;
    seal::Ciphertext ct;
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
    if (any_non_finite) {
        throw std::runtime_error("CKKSContext160: depth-1 sanity check failed after rescale");
    }
}
