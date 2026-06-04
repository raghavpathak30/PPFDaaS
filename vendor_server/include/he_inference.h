#pragma once

#include "ckks_context.h"

#include <seal/seal.h>

// Depth-1 circuit: multiply_plain_inplace → rescale_to_next_inplace →

// hoisted_tree_sum(256). No relinearize.

seal::Ciphertext depth1_he_inference(
        CKKSContext& ctx,

        seal::Ciphertext& ct,

        const seal::Plaintext& pt_weights);

// Depth-1 circuit for 160-bit context (same operations as 200-bit)
#include "ckks_context_160.h"

seal::Ciphertext depth1_he_inference_160(
        CKKSContext160& ctx,

        seal::Ciphertext& ct,

        const seal::Plaintext& pt_weights);
