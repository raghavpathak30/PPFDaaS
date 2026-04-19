// vendor_server/src/he_inference.cpp — Depth-1 HE inference (§2 hops 10–12)

#include "he_inference.h"

#include "rotation_hoisting.h"

seal::Ciphertext depth1_he_inference(
        CKKSContext& ctx,

        seal::Ciphertext& ct,

        const seal::Plaintext& pt_weights) {

    ctx.evaluator->multiply_plain_inplace(ct, pt_weights);

    ctx.evaluator->rescale_to_next_inplace(ct);

    seal::Ciphertext acc;
    hoisted_tree_sum(ct, ctx.galois_keys, *ctx.evaluator, acc, 256);
    return acc;

}
