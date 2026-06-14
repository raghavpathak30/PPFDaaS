#pragma once

#include <seal/seal.h>

#include <array>
#include <istream>
#include <memory>
#include <optional>
#include <cmath>

// ─── Phase 1, §1.1/§1.2/§1.2b — Evaluation-only server context ─────────────
//
// CKKSContext160 (ckks_context_160.{h,cpp}) constructed a SecretKey, a live
// Decryptor, and ran KeyGenerator -- giving the linked server process the
// CAPABILITY to decrypt any ciphertext, even though the server "intends" to
// only evaluate. A server's Trusted Computing Base is defined by the
// capabilities PRESENT in the linked process, not by what the surrounding
// code happens to call.
//
// EvalContext160 has:
//   - NO seal::SecretKey field
//   - NO seal::Decryptor
//   - NO seal::KeyGenerator anywhere (not even transiently in the constructor)
//   - NO seal::PublicKey / seal::Encryptor (the server never encrypts --
//     RunInference only receives ciphertexts and CanaryCheck returns a
//     ciphertext that is itself a transform of a bank-supplied ciphertext)
//
// What it CAN do: encode plaintexts (weights), and evaluate
// (multiply_plain, rescale, rotate via Galois keys). That is the entire
// capability surface required by the depth-1 inference circuit.
//
// Galois keys are NOT read from a local file at construction time (§1.2:
// the previous fail-open `else` branch that fabricated local Galois keys
// when galois_keys_160.bin was missing is gone -- there is no local-key
// fallback of any kind). They arrive ONLY via the provisioning protocol
// (ProvisionGaloisKeys RPC, see provisioning_state.h / inference_service_160.cpp)
// and are validated structurally here before being accepted.
struct EvalContext160 {
    // Restricted Galois rotation step set used by hoisted_tree_sum: log2(256) = 8 steps.
    static constexpr std::array<int, 8> ROTATION_STEPS = {1, 2, 4, 8, 16, 32, 64, 128};

    seal::EncryptionParameters         params;
    std::shared_ptr<seal::SEALContext> context;
    std::optional<seal::CKKSEncoder>   encoder;
    std::optional<seal::Evaluator>     evaluator;
    double                              scale = std::pow(2.0, 40);
    seal::parms_id_type                 second_parms_id;

    // Set true only after load_and_validate_galois_keys() succeeds.
    bool             galois_keys_loaded = false;
    seal::GaloisKeys galois_keys;

    explicit EvalContext160();
    EvalContext160(const EvalContext160&) = delete;
    EvalContext160& operator=(const EvalContext160&) = delete;

    // §1.2 structural validation rung (rung "a" of §1.5):
    //   1. The byte stream must deserialize as seal::GaloisKeys under `context`.
    //   2. galois_keys.parms_id() must equal the KEY-LEVEL parms_id of `context`
    //      (i.e. the keys were generated for THESE encryption parameters).
    //   3. The key set must contain EVERY Galois element ROTATION_STEPS needs,
    //      checked eagerly here -- not discovered lazily mid-rotation.
    //
    // Throws std::runtime_error with a descriptive message on any failure and
    // leaves galois_keys_loaded == false / galois_keys unchanged. The caller
    // (the provisioning state machine) is responsible for translating a thrown
    // exception into a PROV_FAULT transition. This function does NOT and
    // CANNOT verify that the keys were generated under the bank's secret key
    // (rung "b" -- the canary handshake -- is the only thing that can).
    void load_and_validate_galois_keys(std::istream& in);
};
