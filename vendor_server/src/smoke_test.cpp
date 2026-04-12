#include "ckks_context.h"

#include <cassert>
#include <cstdlib>
#include <iostream>
#include <vector>

int main() {
    CKKSContext ctx;

    assert(ctx.second_parms_id != ctx.context->first_parms_id());

    std::vector<double> dummy(4096, 0.5);
    seal::Plaintext pt_plain;
    seal::Plaintext pt_weights;
    seal::Ciphertext ct;

    ctx.encoder->encode(dummy, ctx.scale, pt_plain);
    ctx.encoder->encode(dummy, ctx.scale, pt_weights);
    ctx.encryptor->encrypt(pt_plain, ct);

    ctx.evaluator->multiply_plain_inplace(ct, pt_weights);
    ctx.evaluator->rescale_to_next_inplace(ct);

    // CKKS: invariant_noise_budget unsupported in SEAL 4.x — decrypt+decode proxy for “noise OK”
    assert(ckks_ciphertext_decrypts_cleanly(
        *ctx.decryptor, *ctx.encoder, ct, ctx.encoder->slot_count()));

    std::cout << "[PASS]\n";
    return EXIT_SUCCESS;
}
