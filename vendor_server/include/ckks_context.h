#pragma once

#include <seal/seal.h>

#include <optional>

#include <memory>

#include <cmath>

struct CKKSContext {
    seal::EncryptionParameters         params;
    std::shared_ptr<seal::SEALContext>  context;
    seal::SecretKey    secret_key;
    seal::PublicKey    public_key;
    seal::GaloisKeys   galois_keys;
    std::optional<seal::CKKSEncoder> encoder;
    std::optional<seal::Encryptor> encryptor;
    std::optional<seal::Decryptor> decryptor;
    std::optional<seal::Evaluator> evaluator;
    double             scale = std::pow(2.0, 40);
    seal::parms_id_type second_parms_id;
    explicit CKKSContext();
    CKKSContext(const CKKSContext&) = delete;
    CKKSContext& operator=(const CKKSContext&) = delete;
};
