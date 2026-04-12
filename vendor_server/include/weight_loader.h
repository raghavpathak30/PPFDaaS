#pragma once

#include <seal/seal.h>

#include <string>

seal::Plaintext load_weights_as_plaintext(
        const std::string& path, seal::CKKSEncoder& enc, double scale);
