#pragma once

// Row-major slot packing for the CKKS arm, parameterised by block width b.
//
// A transaction has `features` (256) values. A block is b consecutive slots
// holding b of those values, and the fold sums the b slots of a block into the
// block's first slot:
//
//   transactions per ciphertext  = slots / b        (4096 / b)
//   ciphertexts per batch        = features / b     (256 / b)
//   fold rotations per ciphertext = log2(b)
//   feature f of transaction t   -> ciphertext f / b, slot t*b + f % b
//   score of transaction t       -> slot t*b of the ciphertext sum
//
// b = 256 is the deployed layout: 16 transactions, 1 ciphertext, 8 rotations.
// The block-width sweep (b < 256) is not implemented: the reductions in
// vendor_server/src/rotation_hoisting.cpp are hard-wired to 256, and
// require_deployed_layout() makes any other b fail loudly instead of silently
// timing the wrong circuit.

#include <cstddef>
#include <stdexcept>
#include <string>
#include <vector>

struct PackingLayout {
    int slots = 4096;     // N/2 for N = 8192
    int features = 256;
    int block = 256;      // b

    PackingLayout() = default;
    explicit PackingLayout(int b) : block(b) {
        if (b <= 0 || (b & (b - 1)) != 0 || b > features || slots % b != 0)
            throw std::invalid_argument("PackingLayout: b must be a power of two dividing 256");
    }

    int txns_per_ct() const { return slots / block; }
    int cts_per_batch() const { return features / block; }
    int fold_rotations() const {
        int r = 0;
        while ((1 << r) < block) ++r;
        return r;
    }
    // Rotation steps of the sequential fold: 1, 2, 4, ..., b/2.
    std::vector<int> fold_steps() const {
        std::vector<int> s;
        for (int i = 1; i < block; i <<= 1) s.push_back(i);
        return s;
    }
    int score_slot(int txn) const { return txn * block; }

    void require_deployed_layout() const {
        if (block != 256)
            throw std::logic_error("block-width sweep not implemented: b=" + std::to_string(block) +
                                   " (only the deployed b=256 is supported)");
    }

    // rows[t][f] -> cts_per_batch() slot vectors of length `slots`.
    // Only the first txns_per_ct() rows are used; unused blocks stay zero.
    std::vector<std::vector<double>> pack(const std::vector<std::vector<double>> &rows) const {
        if (static_cast<int>(rows.size()) != txns_per_ct())
            throw std::invalid_argument("pack: need exactly txns_per_ct() rows");
        std::vector<std::vector<double>> cts(cts_per_batch(), std::vector<double>(slots, 0.0));
        for (int t = 0; t < txns_per_ct(); ++t) {
            if (static_cast<int>(rows[t].size()) != features)
                throw std::invalid_argument("pack: row length != features");
            for (int f = 0; f < features; ++f) cts[f / block][t * block + f % block] = rows[t][f];
        }
        return cts;
    }

    // Plaintext oracle, computed from the unpacked rows so it checks the packing too.
    std::vector<double> expected_scores(const std::vector<std::vector<double>> &x,
                                        const std::vector<std::vector<double>> &w) const {
        std::vector<double> out(txns_per_ct(), 0.0);
        for (int t = 0; t < txns_per_ct(); ++t)
            for (int f = 0; f < features; ++f) out[t] += x[t][f] * w[t][f];
        return out;
    }

    std::vector<double> read_scores(const std::vector<double> &decoded) const {
        std::vector<double> out(txns_per_ct());
        for (int t = 0; t < txns_per_ct(); ++t) out[t] = decoded[score_slot(t)];
        return out;
    }
};
