// vendor_server/include/ckks_context.h  — NORMATIVE
#pragma once

#include <seal/seal.h>

#include <memory>

#include <cmath>

struct CKKSContext {

    seal::EncryptionParameters         params;

    std::shared_ptr<seal::SEALContext>  context;

    seal::SecretKey    secret_key;    // bank client only — NEVER on vendor server

    seal::PublicKey    public_key;

    seal::GaloisKeys   galois_keys;   // restricted set {1,2,4,8,16,32,64,128}

    // SEAL 4.1: these types are not default-constructible; hold behind unique_ptr
    std::unique_ptr<seal::CKKSEncoder> encoder;

    std::unique_ptr<seal::Encryptor>    encryptor;

    std::unique_ptr<seal::Decryptor>   decryptor;

    std::unique_ptr<seal::Evaluator>   evaluator;

    double             scale = std::pow(2.0, 40);

    // second_parms_id: parms_id AFTER one rescale_to_next_inplace

    seal::parms_id_type second_parms_id;

    explicit CKKSContext();

    CKKSContext(const CKKSContext&) = delete;

    CKKSContext& operator=(const CKKSContext&) = delete;

};

// CKKS: SEAL 4.x has no invariant_noise_budget; use decrypt+decode sanity as proxy
bool ckks_ciphertext_decrypts_cleanly(
        seal::Decryptor& decryptor,
        seal::CKKSEncoder& encoder,
        const seal::Ciphertext& ct,
        std::size_t slot_count);
