// he_core/src/rotation_hoisting_degree2.cpp  — NORMATIVE

#include "rotation_hoisting_degree2.h"

#include <seal/seal.h>

#include <stdexcept>

// Restricted step set: {1,2,4,8,16,32,64,128,256} = log2(512) = 9 steps.
//
// CORRECTED LOG-FOLD INVARIANT (Phase 0.1, degree-2 variant):
//
//   acc = ct
//   for step in {1,2,4,8,16,32,64,128,256}:
//       acc = acc + rotate(acc, step)     // rotation acts on the ACCUMULATOR
//
// As in rotation_hoisting.cpp, after all 9 iterations (2^9 == 512):
//
//   acc.slot[i] = sum_{j=0}^{511} ct.slot[(i+j) mod 16384]
//
// For lane-aligned offsets i = k*512, this is the per-transaction 512-term
// dot-product post-condition:
//
//   acc.slot[k*512] == sum_{j=0}^{511} w[j] * x[j]
//
// The fold is a sequential dependency chain and is NOT parallelizable.

static constexpr int STEPS_D2[] = {1, 2, 4, 8, 16, 32, 64, 128, 256};

seal::Ciphertext hoisted_tree_sum_degree2(
        const seal::Ciphertext& ct,
        const seal::GaloisKeys& gk,
        seal::Evaluator& ev,
        int n_features)
{
    if (n_features != 512)
        throw std::invalid_argument("hoisted_tree_sum_degree2: n_features must be 512");

    seal::Ciphertext acc = ct;
    seal::Ciphertext tmp;

    for (int r = 0; r < 9; ++r) {
        ev.rotate_vector(acc, STEPS_D2[r], gk, tmp);
        ev.add_inplace(acc, tmp);
    }

    // Post-condition: acc.slot[k*512] = dot-product for transaction k
    return acc;
}
