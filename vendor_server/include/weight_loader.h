#pragma once

#include <seal/seal.h>

#include <string>

seal::Plaintext load_weights_as_plaintext(
        const std::string& path, seal::CKKSEncoder& enc, double scale);

// §1.3: overload that also returns the bias term, so the server can apply it
// (see inference_service_160.cpp). bias_out is the raw <d> bias stored in the
// model_weights.bin header (compiler/serialize_weights.py).
seal::Plaintext load_weights_as_plaintext(
        const std::string& path, seal::CKKSEncoder& enc, double scale, double& bias_out);
