// he_core/src/rotation_hoisting_degree2.cpp  — NORMATIVE

#include "rotation_hoisting_degree2.h"

#include <seal/seal.h>

#include <stdexcept>

static constexpr int STEPS_D2[] = {1,2,4,8,16,32,64,128,256};

seal::Ciphertext hoisted_tree_sum_degree2(
        const seal::Ciphertext& ct,
        const seal::GaloisKeys& gk,
        seal::Evaluator& ev,
        int n_features)
{
    if (n_features != 512)
        throw std::invalid_argument("hoisted_tree_sum_degree2: n_features must be 512");
    seal::Ciphertext acc = ct, rotated;
    for (int r = 0; r < 9; ++r) {
        ev.rotate_vector(ct, STEPS_D2[r], gk, rotated);
        ev.add_inplace(acc, rotated);
    }
    return acc;
}
