#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "ckks_context.h"
#include "rotation_hoisting.h"
#include "weight_loader.h"

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <random>
#include <vector>

TEST_CASE("test_weight_loader", "[he_core]") {
    CKKSContext ctx;

    const auto tmp = std::filesystem::temp_directory_path() / "ppfdaas_weights_test.bin";

    {
        std::ofstream f(tmp, std::ios::binary);
        REQUIRE(f.is_open());
        constexpr uint32_t n = 256;
        const double bias = 0.0;
        f.write(reinterpret_cast<const char*>(&n), sizeof(n));
        f.write(reinterpret_cast<const char*>(&bias), sizeof(bias));
        std::vector<double> w(256);
        for (int j = 0; j < 256; ++j) {
            w[j] = static_cast<double>(j) * 0.01 + 3.14159;
        }
        f.write(reinterpret_cast<const char*>(w.data()), 256 * sizeof(double));
        f.close();
        REQUIRE(std::filesystem::file_size(tmp) == 2060);
    }

    seal::Plaintext pt = load_weights_as_plaintext(tmp.string(), *ctx.encoder, ctx.scale);

    std::vector<double> decoded;
    ctx.encoder->decode(pt, decoded);
    REQUIRE(decoded.size() >= 4096);

    const double expected0 = 3.14159;
    REQUIRE(std::abs(decoded[0] - decoded[256]) < 1e-4);
    REQUIRE(std::abs(decoded[0] - decoded[512]) < 1e-4);
    REQUIRE(std::abs(decoded[0] - expected0) < 1e-3);

    std::error_code ec;
    std::filesystem::remove(tmp, ec);
}

// ─── Phase 0.2: randomized oracle parity tests ────────────────────────────
//
// The previous "test_rotation_hoisting" used one-hot weights at slot 0 with
// unit features -- the unique layout where the broken 9-term partial-sum
// fold (acc += rotate(ORIGINAL ct, step) for step in {1,2,4,8,16,32,64,128})
// and the correct 256-term log-fold (acc += rotate(ACCUMULATOR, step))
// happen to agree, because every contribution to the dot product collapses
// onto slot 0 itself. That test therefore validated nothing about the fold's
// ability to sum 256 distinct slots.
//
// Below we replace it with:
//   1) a randomized dense-vector oracle: N=100 random (w, x) pairs, compared
//      against the plaintext dot product;
//   2) structured basis probes at slot positions that are NOT in the
//      rotation step set, which the broken fold silently dropped.

namespace {

// CKKS precision tolerance for this parameter set
// (poly_modulus_degree=8192, coeff_modulus={60,40,40,60}, scale=2^40) after
// one multiply_plain + rescale_to_next, followed by the 8-step sequential
// rotate-and-add fold. Each rotation is a key-switch operation that adds a
// small amount of independent rounding noise; across 8 sequential steps at a
// 40-bit scale (~1.1e12) the empirically observed max absolute error for
// operands of O(1) magnitude (dot products of 256 terms drawn from [-1,1],
// magnitude up to ~O(10)) is ~5e-7. 1e-3 leaves >1000x margin over that
// observed error while still catching any gross fold regression.
constexpr double TOL = 1e-3;

// Encrypt features/weights (tiled across all 16 lanes), run the Depth-1
// kernel (multiply_plain -> rescale -> hoisted_tree_sum), and decode.
std::vector<double> run_kernel(
        CKKSContext& ctx,
        const std::vector<double>& features4096,
        const std::vector<double>& weights4096) {
    seal::Plaintext pt_features;
    seal::Plaintext pt_weights;
    ctx.encoder->encode(features4096, ctx.scale, pt_features);
    ctx.encoder->encode(weights4096, ctx.scale, pt_weights);

    seal::Ciphertext ct;
    ctx.encryptor->encrypt(pt_features, ct);
    ctx.evaluator->multiply_plain_inplace(ct, pt_weights);
    ctx.evaluator->rescale_to_next_inplace(ct);

    seal::Ciphertext acc;
    hoisted_tree_sum(ct, ctx.galois_keys, *ctx.evaluator, acc, 256);

    seal::Plaintext pt_out;
    ctx.decryptor->decrypt(acc, pt_out);
    std::vector<double> decoded;
    ctx.encoder->decode(pt_out, decoded);
    return decoded;
}

}  // namespace

TEST_CASE("rotation_hoisting: randomized dense-vector oracle parity (N=100)", "[he_core]") {
    CKKSContext ctx;

    // Reproducible seed -- deterministic across runs.
    std::mt19937_64 rng(0xC0FFEEULL);
    std::uniform_real_distribution<double> dist(-1.0, 1.0);

    constexpr int N_TRIALS = 100;
    double max_abs_error = 0.0;

    for (int trial = 0; trial < N_TRIALS; ++trial) {
        std::vector<double> w(256), x(256);
        for (int j = 0; j < 256; ++j) {
            w[j] = dist(rng);
            x[j] = dist(rng);
        }

        // Plaintext oracle: expected = dot(w, x). (The bias term `b` from
        // the spec's `expected = dot(w,x) + b` is applied client-side
        // post-decryption -- see weight_loader.cpp -- and is not part of
        // the HE kernel under test here.)
        double expected = 0.0;
        for (int j = 0; j < 256; ++j) expected += w[j] * x[j];

        // Tile the same (w, x) across all 16 packed transaction lanes.
        std::vector<double> features(4096), weights(4096);
        for (int k = 0; k < 16; ++k) {
            for (int j = 0; j < 256; ++j) {
                features[k * 256 + j] = x[j];
                weights[k * 256 + j] = w[j];
            }
        }

        std::vector<double> decoded = run_kernel(ctx, features, weights);
        REQUIRE(decoded.size() >= 4096);

        for (int k = 0; k < 16; ++k) {
            const double actual = decoded[k * 256];
            max_abs_error = std::max(max_abs_error, std::abs(actual - expected));
            REQUIRE_THAT(actual, Catch::Matchers::WithinAbs(expected, TOL));
        }
    }

    WARN("max_abs_error over " << N_TRIALS << " trials (1600 lane checks) = " << max_abs_error);
    REQUIRE(max_abs_error < TOL);
}

TEST_CASE("rotation_hoisting: structured basis probes at non-degenerate slots", "[he_core]") {
    CKKSContext ctx;

    // Probe positions are NOT members of the rotation step set
    // {1,2,4,8,16,32,64,128}. The broken (pre-Phase-0.1) fold computed
    // acc.slot[i] = ct.slot[i] + sum_{step in STEPS} ct.slot[(i+step) mod N],
    // i.e. only a 9-element subset of each 256-slot window. A weight that is
    // one-hot at one of these probe positions decodes to ~0 under the broken
    // fold but to w[p]*x[p] under the correct 256-term fold.
    const std::vector<int> probe_positions = {7, 31, 113, 255};
    const double feature_value = 1.0;
    const double weight_value = 1.0;
    const double expected = weight_value * feature_value;

    for (int p : probe_positions) {
        std::vector<double> features(4096, 0.0);
        std::vector<double> weights(4096, 0.0);
        for (int k = 0; k < 16; ++k) {
            features[k * 256 + p] = feature_value;
            weights[k * 256 + p] = weight_value;
        }

        std::vector<double> decoded = run_kernel(ctx, features, weights);
        REQUIRE(decoded.size() >= 4096);

        for (int k = 0; k < 16; ++k) {
            INFO("probe slot p=" << p << " lane k=" << k);
            REQUIRE_THAT(decoded[k * 256], Catch::Matchers::WithinAbs(expected, TOL));
        }
    }
}
