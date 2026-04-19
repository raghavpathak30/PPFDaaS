#pragma once

#include <seal/seal.h>

seal::Ciphertext hoisted_tree_sum_degree2(
        const seal::Ciphertext& ct,
        const seal::GaloisKeys& gk,
        seal::Evaluator& ev,
        int n_features);
