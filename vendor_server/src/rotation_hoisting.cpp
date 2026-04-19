// vendor_server/src/rotation_hoisting.cpp  — NORMATIVE (§4.5)

#include "rotation_hoisting.h"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <stdexcept>
#include <vector>

// Restricted step set: {1,2,4,8,16,32,64,128} = log2(256)=8 steps

// After 8 rotate-and-add operations:

//   acc.slot[k*256] = sum(ct.slot[k*256..k*256+255])  for k=0..15

// All other slots hold partial sums — caller MUST ignore them.

static constexpr int STEPS[] = {1, 2, 4, 8, 16, 32, 64, 128};
static const int N_OMP = []() {
    const char* e = std::getenv("PPFD_OMP_THREADS");
    return e ? std::max(1, std::atoi(e)) : 2;
}();

seal::Ciphertext& hoisted_tree_sum(
        const seal::Ciphertext& ct,  // MUST be post-rescale (second_parms_id)

        const seal::GaloisKeys& gk,

        seal::Evaluator& ev,

    seal::Ciphertext& acc_out,

        int n_features)              // must be 256
{

    if (n_features != 256)

        throw std::invalid_argument("hoisted_tree_sum: n_features must be 256");

    std::array<seal::Ciphertext, 8> rotated_arr;

#pragma omp parallel for schedule(static) num_threads(N_OMP)
    for (int r = 0; r < 8; ++r) {
        ev.rotate_vector(ct, STEPS[r], gk, rotated_arr[r]);
    }

    seal::Ciphertext acc = ct;

    for (int r = 0; r < 8; ++r) {

        ev.add_inplace(acc, rotated_arr[r]);

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
