#pragma once

#include <seal/seal.h>

#include <array>

// ─── TERMINOLOGY NOTE (Phase 4, §4.2) ──────────────────────────────────────
//
// hoisted_tree_sum() is named for historical reasons but does NOT implement
// Halevi-Shoup rotation hoisting (Halevi & Shoup, "Algorithms in HElib",
// 2014, §3). True hoisting shares the key-switching digit decomposition
// (ModDown) across multiple automorphisms applied to the SAME ciphertext,
// amortizing the dominant cost of each rotation. SEAL's public API does not
// expose that digit-decomposition step, so genuine hoisting cannot be
// implemented without leaving SEAL's public interface.
//
// What hoisted_tree_sum() implements is a sequential dependency-chain fold:
// 8 rotations of the ACCUMULATOR in log2(256) = 8 critical-path steps, which
// is not parallelizable (each step's rotation depends on the previous step's
// result). See bsgs_reduction() below for a parallelizable-but-still-unhoisted
// alternative, and tools/openfhe_benchmark/ for a cross-library comparison
// against genuine hoisting via OpenFHE's EvalFastRotation API.
//
// See docs/spec.md §7 for the full three-strategy taxonomy and rotation-step
// requirements of each strategy.
seal::Ciphertext& hoisted_tree_sum(
        const seal::Ciphertext& ct,  // MUST be post-rescale (second_parms_id)

        const seal::GaloisKeys& gk,

        seal::Evaluator& ev,

        seal::Ciphertext& acc_out,

        int n_features);              // must be 256

// ─── BSGS (Baby-Step Giant-Step) two-layer reduction (Phase 4, §4.1) ───────
//
// Restructures the 256-length slot reduction into two layers of MUTUALLY
// INDEPENDENT rotations instead of hoisted_tree_sum()'s 8-step sequential
// fold:
//
//   - Baby steps  (j = 0 .. baby_step-1):  rotate(ct_in, j),  accumulate
//   - Giant steps (i = 0 .. giant_step-1): rotate(baby_acc, i*baby_step), accumulate
//
// where baby_step * giant_step == n_features (canonical choice for n=256:
// baby_step = giant_step = 16).
//
// Total work: 30 rotations (15 baby + 15 giant) across 2 critical-path
// layers (vs 8 rotations in 8 critical-path steps for the sequential fold)
// -- MORE total rotations,
// but each layer's rotations are independent of one another and can be
// computed in parallel (OpenMP `parallel for` within each layer).
//
// OpenMP is legitimate here because, within each layer, every rotation acts
// on the SAME source ciphertext (ct_in for baby steps, baby_acc for giant
// steps) -- these rotations do not depend on each other. This is NOT the
// case in hoisted_tree_sum()'s sequential fold, where each rotation depends
// on the previous step's accumulated result (acc = acc + rotate(acc, step)),
// which is why that fold's earlier OpenMP `parallel for` was a correctness
// bug (Phase 0.1), not a valid optimization.
//
// Post-condition (identical to hoisted_tree_sum()):
//   ct_out.slot[k*256] == sum_{j=0}^{255} ct_in.slot[k*256 + j]   for k=0..15
// All other slots hold window sums that are not lane-aligned and MUST be
// ignored by callers.
//
// GALOIS KEY REQUIREMENT: bsgs_reduction requires a SUPERSET of the key
// material needed by hoisted_tree_sum(). Baby steps need Galois elements for
// rotation steps {1,...,baby_step-1}; giant steps need elements for
// {baby_step, 2*baby_step, ..., (giant_step-1)*baby_step}. For the canonical
// baby_step = giant_step = 16, this is BSGS_ROTATION_STEPS below: steps
// {1..15} ∪ {16,32,...,240} = 30 distinct steps, vs hoisted_tree_sum's 8
// steps {1,2,4,8,16,32,64,128}.
//
// The deployed server (EvalContext160, eval_context_160.h) provisions ONLY
// EvalContext160::ROTATION_STEPS (the fold's 8-element set) -- bsgs_reduction
// is NOT used by the deployed server. For production BSGS deployment,
// reprovisioning with the expanded BSGS_ROTATION_STEPS key set would be
// required -- see docs/spec.md §4. bsgs_reduction is available as a
// measurable variant in the benchmark binary only (tools/local_benchmark /
// benchmark_160, Phase 4.4).
//
// Throws std::runtime_error if `galois_keys` is missing any Galois element
// required by BSGS_ROTATION_STEPS, or std::invalid_argument if
// n_features != 256 or baby_step * giant_step != n_features.
inline constexpr std::array<int, 30> BSGS_ROTATION_STEPS = {
    // Baby steps: rotations of the ORIGINAL ciphertext, j = 1..15
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    // Giant steps: rotations of the baby-step PARTIAL SUM, i*16 for i = 1..15
    16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240,
};

void bsgs_reduction(
        const seal::Ciphertext& ct_in,  // MUST be post-rescale (second_parms_id)

        const seal::GaloisKeys& galois_keys,

        seal::Evaluator& evaluator,

        seal::Ciphertext& ct_out,

        int n_features,    // must be 256

        int baby_step,     // canonical: 16 (sqrt(256))

        int giant_step);   // canonical: 16 (sqrt(256))

// ─── Naive baseline (for ablation benchmark only) ─────────────────────────

// Requires full GaloisKeys (steps 1..255). NEVER use in production.

seal::Ciphertext naive_tree_sum(
        const seal::Ciphertext& ct,

        const seal::GaloisKeys& gk_full,

        seal::Evaluator& ev,
        int n_features);
