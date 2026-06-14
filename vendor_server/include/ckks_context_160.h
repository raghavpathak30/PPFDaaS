#pragma once

#include <seal/seal.h>

#include <optional>
#include <memory>
#include <cmath>

// ─── Phase 1, §1.1 scope note ──────────────────────────────────────────────
//
// CKKSContext160 holds a secret key, a live Decryptor, and runs a
// KeyGenerator -- the exact capabilities §1.1 forbids in the deployed
// server's TCB. As of Phase 1 this type is used ONLY by `benchmark_160`
// (see CMakeLists.txt), a standalone latency-measurement binary that is NOT
// linked into `vendor_server_160` (the binary Dockerfile.server builds and
// deploys). benchmark_160 legitimately needs to self-encrypt/decrypt to time
// the circuit end-to-end, and its `PPFDAAS_ALLOW_LOCAL_GALOIS=1` fail-open
// path remains scoped to "local benchmarking only" (score validity is
// irrelevant there -- only latency is measured).
//
// The deployed server uses EvalContext160 (eval_context_160.h), which has NO
// secret key, NO Decryptor, NO KeyGenerator, and loads Galois keys ONLY via
// the provisioning protocol (provisioning_state.h / inference_service_160.cpp),
// never from a local file.
struct CKKSContext160 {
    seal::EncryptionParameters         params;
    std::shared_ptr<seal::SEALContext> context;
    seal::SecretKey    secret_key;
    seal::PublicKey    public_key;
    seal::GaloisKeys   galois_keys;
    std::optional<seal::CKKSEncoder> encoder;
    std::optional<seal::Encryptor> encryptor;
    std::optional<seal::Decryptor> decryptor;
    std::optional<seal::Evaluator> evaluator;
    double             scale = std::pow(2.0, 40);
    seal::parms_id_type second_parms_id;

    explicit CKKSContext160();
    CKKSContext160(const CKKSContext160&) = delete;
    CKKSContext160& operator=(const CKKSContext160&) = delete;
};
