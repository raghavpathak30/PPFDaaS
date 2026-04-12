#pragma once

#include "ckks_context.h"

#include <seal/seal.h>

// Depth-1 circuit: multiply_plain_inplace → rescale_to_next_inplace →

// hoisted_tree_sum(256). No relinearize.

seal::Ciphertext depth1_he_inference(
        CKKSContext& ctx,

        seal::Ciphertext& ct,

        const seal::Plaintext& pt_weights);
