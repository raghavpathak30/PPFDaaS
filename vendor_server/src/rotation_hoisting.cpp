// vendor_server/src/rotation_hoisting.cpp  — NORMATIVE (§4.5)

#include "rotation_hoisting.h"

#include <stdexcept>

// Restricted step set: {1,2,4,8,16,32,64,128} = log2(256)=8 steps

// After 8 rotate-and-add operations:

//   acc.slot[k*256] = sum(ct.slot[k*256..k*256+255])  for k=0..15

// All other slots hold partial sums — caller MUST ignore them.

static constexpr int STEPS[] = {1, 2, 4, 8, 16, 32, 64, 128};

seal::Ciphertext hoisted_tree_sum(
        const seal::Ciphertext& ct,  // MUST be post-rescale (second_parms_id)

        const seal::GaloisKeys& gk,

        seal::Evaluator& ev,

        int n_features)              // must be 256
{

    if (n_features != 256)

        throw std::invalid_argument("hoisted_tree_sum: n_features must be 256");

    seal::Ciphertext acc = ct, rotated;

    for (int r = 0; r < 8; ++r) {

        ev.rotate_vector(ct, STEPS[r], gk, rotated);

        ev.add_inplace(acc, rotated);

    }

    // Post-condition: acc.slot[k*256] = dot-product for transaction k

    return acc;

}

// ─── Naive baseline (for ablation benchmark only) ─────────────────────────

// Requires full GaloisKeys (steps 1..255). NEVER use in production.

seal::Ciphertext naive_tree_sum(
        const seal::Ciphertext& ct,

        const seal::GaloisKeys& gk_full,

        seal::Evaluator& ev,
        int n_features) {

    if (n_features != 256)

        throw std::invalid_argument("naive_tree_sum: n_features must be 256");

    seal::Ciphertext acc = ct, rotated;

    for (int step = 1; step < n_features; ++step) {

        ev.rotate_vector(ct, step, gk_full, rotated);

        ev.add_inplace(acc, rotated);

    }

    return acc;

}
