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

    // ─── §3.1 Security-level justification (Phase 3, Item 3.1) ─────────────
    //
    // Parameters: poly_modulus_degree (N) = 8192, coeff_modulus =
    // {60, 40, 40, 60} bits, total_coeff_modulus_bit_count = 200 bits (the
    // KEY-level chain, all 4 primes).
    //
    // HomomorphicEncryption.org Security Standard v1.1, Table 2 (ternary
    // secret, 128-bit classical security, "tc128") gives a maximum total
    // coeff_modulus bit count of 218 bits for N=8192. 200 <= 218: this chain
    // satisfies tc128 with 18 bits to spare.
    //
    // As in eval_context_160.cpp (the deployed eval-only context, this
    // file's 160-bit counterpart): SEAL hard-codes the 218-bit figure as
    // seal::util::seal_he_std_parms_128_tc(8192) (seal/util/hestdparms.h),
    // and SEALContext::Validate (context.cpp) compares
    // total_coeff_modulus_bit_count_ against
    // CoeffModulus::MaxBitCount(poly_modulus_degree, sec_level). On violation
    // it sets parameters_set() == false (qualifiers().parameter_error ==
    // error_type::invalid_parameters_insecure) rather than throwing directly;
    // the `if (!context->parameters_set()) throw ...` below is what makes
    // that fail-closed. sec_level_type::tc128 is SEALContext's default, but
    // is passed explicitly here for the same reason as in
    // eval_context_160.cpp: an auditable, grep-able security claim instead of
    // a dependency on an unstated library default.
    //
    // Depth budget: dropping the 60-bit special key-switching modulus from
    // the 4-prime {60,40,40,60} KEY chain leaves a 3-prime {60,40,40} =
    // 140-bit DATA chain at first_parms_id -- i.e. 2 data levels available
    // (2 rescales possible: {60,40,40} -> {60,40} -> {60}). This context's
    // depth-1 sanity check below performs exactly 1 rescale (to
    // `second_parms_id`, the {60,40}=100-bit level), so 1 of the 2 available
    // data levels is exercised here; the 200-bit chain carries one more
    // level of headroom than the 160-bit {60,40,60} chain used by
    // EvalContext160, at the cost of a wider (and thus slower / larger)
    // ciphertext -- this 40-bit gap is the "38-48%" ablation referenced in
    // the remediation plan (Phase 5).
    //
    // Citations:
    //   - HomomorphicEncryption.org Security Standard, v1.1, Table 2.
    //   - SEAL 4.x source: seal/util/hestdparms.h (seal_he_std_parms_128_tc),
    //     seal/context.cpp (SEALContext::Validate, tc128 enforcement).
    context = std::make_shared<seal::SEALContext>(params, true, seal::sec_level_type::tc128);
    if (!context->parameters_set())
        throw std::runtime_error("CKKSContext: SEAL rejected parameters (insecure or invalid)");

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
