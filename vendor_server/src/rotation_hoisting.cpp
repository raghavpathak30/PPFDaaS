// vendor_server/src/rotation_hoisting.cpp  — NORMATIVE (§4.5)

#include "rotation_hoisting.h"

#include "seal/util/galois.h"

#include <stdexcept>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

// TERMINOLOGY NOTE (Phase 4, §4.2):
// This file is named rotation_hoisting.cpp for historical reasons but does
// NOT implement Halevi-Shoup rotation hoisting. True hoisting shares the
// key-switch digit decomposition (ModDown) across multiple automorphisms,
// amortizing the most expensive part of each rotation. SEAL's public API
// does not expose the digit decomposition step; genuine hoisting is not
// possible without leaving SEAL's public interface. hoisted_tree_sum()
// below implements a sequential dependency-chain fold: 8 rotations of the
// ACCUMULATOR in log2(256)=8 critical-path steps, which is not
// parallelizable. bsgs_reduction() implements a parallelizable-but-still-
// unhoisted alternative (30 rotations: 15 baby + 15 giant, in 2 independent
// layers). See tools/openfhe_benchmark/ for a cross-library comparison
// including genuine hoisting via OpenFHE's EvalFastRotation API, and
// docs/spec.md §7 for the full three-strategy taxonomy.

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

// ─── BSGS (Baby-Step Giant-Step) two-layer reduction (Phase 4, §4.1) ───────
//
// Restructures the same 256-length reduction as hoisted_tree_sum() into two
// layers of mutually-independent rotations:
//
//   Layer 1 (baby steps, j=0..baby_step-1): rotate the ORIGINAL ct_in by j
//   and accumulate -> baby_acc.slot[i] = sum_{j=0}^{b-1} ct_in.slot[i+j]
//
//   Layer 2 (giant steps, i=0..giant_step-1): rotate baby_acc by i*baby_step
//   and accumulate -> ct_out.slot[idx] = sum_{i=0}^{g-1} baby_acc.slot[idx+i*b]
//                                       = sum_{m=0}^{b*g-1} ct_in.slot[idx+m]
//
// For baby_step=giant_step=16, b*g=256, so ct_out.slot[k*256] equals the
// same 256-term dot product hoisted_tree_sum() produces. Total: 30 rotations
// (15 baby + 15 giant) across 2 critical-path layers, vs hoisted_tree_sum's
// 8 rotations in 8 critical-path steps.
//
// Each #pragma omp parallel for below is over rotations of a SINGLE shared
// source ciphertext (ct_in for baby steps, baby_acc for giant steps) -- the
// rotations within a layer do not depend on each other, so this parallelism
// is correct. Contrast with hoisted_tree_sum's fold, where
// acc = acc + rotate(acc, step) makes each step depend on the previous
// step's output; parallelizing THAT loop (the pre-Phase-0 bug) silently
// computed a degenerate partial sum instead of the full 256-term reduction.
void bsgs_reduction(
        const seal::Ciphertext& ct_in,
        const seal::GaloisKeys& galois_keys,
        seal::Evaluator& evaluator,
        seal::Ciphertext& ct_out,
        int n_features,
        int baby_step,
        int giant_step)
{
    if (n_features != 256)
        throw std::invalid_argument("bsgs_reduction: n_features must be 256");
    if (baby_step * giant_step != n_features)
        throw std::invalid_argument("bsgs_reduction: baby_step * giant_step must equal n_features");

    // ── Verify galois_keys contains every BSGS Galois element ──────────────
    // bsgs_reduction requires a superset of the key material needed by
    // hoisted_tree_sum (BSGS_ROTATION_STEPS, 30 steps, vs the fold's 8). The
    // deployed server provisions only the fold's key set
    // (EvalContext160::ROTATION_STEPS); bsgs_reduction is therefore not
    // usable against the deployed server's provisioned keys. For production
    // BSGS deployment, reprovisioning with BSGS_ROTATION_STEPS is required
    // -- see docs/spec.md §4. coeff_count_power=13 <-> poly_modulus_degree
    // 8192, the fixed ring dimension for this 160-bit context (§5.2).
    {
        seal::util::GaloisTool galois_tool(13, seal::MemoryPoolHandle::Global());
        const std::vector<int> steps(BSGS_ROTATION_STEPS.begin(), BSGS_ROTATION_STEPS.end());
        const auto required_elts = galois_tool.get_elts_from_steps(steps);
        for (std::size_t i = 0; i < required_elts.size(); ++i) {
            if (!galois_keys.has_key(required_elts[i])) {
                throw std::runtime_error(
                        "bsgs_reduction: Galois keys are missing the key for BSGS rotation step " +
                        std::to_string(steps[i]) + " (Galois element " +
                        std::to_string(required_elts[i]) + "). bsgs_reduction requires the "
                        "BSGS_ROTATION_STEPS key set (30 steps); the deployed server provisions "
                        "only the sequential fold's 8-step ROTATION_STEPS set. Reprovisioning "
                        "with BSGS_ROTATION_STEPS is required -- see docs/spec.md §4.");
            }
        }
    }

    // ── Layer 1: baby steps -- independent rotations of ct_in ───────────────
    std::vector<seal::Ciphertext> baby_rot(baby_step > 0 ? baby_step - 1 : 0);
    #pragma omp parallel for
    for (int j = 1; j < baby_step; ++j) {
        evaluator.rotate_vector(ct_in, j, galois_keys, baby_rot[j - 1]);
    }

    seal::Ciphertext baby_acc = ct_in;
    for (int j = 1; j < baby_step; ++j) {
        evaluator.add_inplace(baby_acc, baby_rot[j - 1]);
    }

    // ── Layer 2: giant steps -- independent rotations of baby_acc ───────────
    std::vector<seal::Ciphertext> giant_rot(giant_step > 0 ? giant_step - 1 : 0);
    #pragma omp parallel for
    for (int i = 1; i < giant_step; ++i) {
        evaluator.rotate_vector(baby_acc, i * baby_step, galois_keys, giant_rot[i - 1]);
    }

    seal::Ciphertext acc = std::move(baby_acc);
    for (int i = 1; i < giant_step; ++i) {
        evaluator.add_inplace(acc, giant_rot[i - 1]);
    }

    // Post-condition (same as hoisted_tree_sum): acc.slot[k*256] = dot-product for transaction k
    ct_out = std::move(acc);
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
