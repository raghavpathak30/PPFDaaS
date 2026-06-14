// vendor_server/src/rotation_hoisting.cpp  — NORMATIVE (§4.5)

#include "rotation_hoisting.h"

#include <stdexcept>

// Restricted step set: {1,2,4,8,16,32,64,128} = log2(256) = 8 steps.
//
// CORRECTED LOG-FOLD INVARIANT (Phase 0.1):
//
//   acc = ct
//   for step in {1,2,4,8,16,32,64,128}:
//       acc = acc + rotate(acc, step)     // rotation acts on the ACCUMULATOR
//
// By induction, after the step==2^k iteration, acc.slot[i] holds the sum of
// the 2^(k+1) consecutive (cyclically-wrapped) slots ct.slot[i .. i+2^(k+1)-1].
// After all 8 iterations (2^8 == 256):
//
//   acc.slot[i] = sum_{j=0}^{255} ct.slot[(i+j) mod 4096]
//
// For lane-aligned offsets i = k*256 (k = 0..15), the 256-slot window
// [k*256, k*256+255] is exactly the per-transaction feature block, so:
//
//   acc.slot[k*256] == sum_{j=0}^{255} ct.slot[k*256 + j]
//                    == sum_{j=0}^{255} w[j] * x[j]    (post multiply_plain)
//
// which is the required Depth-1 dot-product post-condition. All other slots
// hold window sums that are not lane-aligned and MUST be ignored by callers.
//
// The fold is a sequential dependency chain (each step consumes the previous
// step's accumulator) and is therefore NOT safely parallelizable across the
// 8 rotations -- the prior OpenMP `parallel for` rotated the ORIGINAL
// ciphertext at each step (acc += rotate(ct, step)), which only ever sums the
// 9-slot subset {0,1,2,4,8,16,32,64,128} of each window, not the full 256.

static constexpr int STEPS[] = {1, 2, 4, 8, 16, 32, 64, 128};

seal::Ciphertext& hoisted_tree_sum(
        const seal::Ciphertext& ct,  // MUST be post-rescale (second_parms_id)
        const seal::GaloisKeys& gk,
        seal::Evaluator& ev,
        seal::Ciphertext& acc_out,
        int n_features)              // must be 256
{
    if (n_features != 256)
        throw std::invalid_argument("hoisted_tree_sum: n_features must be 256");

    seal::Ciphertext acc = ct;
    seal::Ciphertext tmp;

    for (int r = 0; r < 8; ++r) {
        ev.rotate_vector(acc, STEPS[r], gk, tmp);
        ev.add_inplace(acc, tmp);
    }

    // Post-condition: acc.slot[k*256] = dot-product for transaction k
    acc_out = std::move(acc);
    return acc_out;
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
