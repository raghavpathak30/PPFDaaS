#pragma once

#include <seal/seal.h>

seal::Ciphertext hoisted_tree_sum(
        const seal::Ciphertext& ct,  // MUST be post-rescale (second_parms_id)

        const seal::GaloisKeys& gk,

        seal::Evaluator& ev,

        int n_features);              // must be 256

// ─── Naive baseline (for ablation benchmark only) ─────────────────────────

// Requires full GaloisKeys (steps 1..255). NEVER use in production.

seal::Ciphertext naive_tree_sum(
        const seal::Ciphertext& ct,

        const seal::GaloisKeys& gk_full,

        seal::Evaluator& ev,
        int n_features);
