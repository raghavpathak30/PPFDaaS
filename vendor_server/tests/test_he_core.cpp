#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "ckks_context.h"
#include "rotation_hoisting.h"
#include "weight_loader.h"

#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
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

TEST_CASE("test_rotation_hoisting", "[he_core]") {
    CKKSContext ctx;

    const double value = 1.0 / 256.0;
    // One-hot weights + unit features: dot-product per txn = 1.0 = 256*value (value = 1/256).
    // A slot-wise constant post-multiply ciphertext degenerates the hoisted accumulator (~9×);
    // this layout matches Depth-1 score semantics (spec §4.5 / §2 hop 12).
    std::vector<double> features(4096, 1.0);
    std::vector<double> weights(4096);
    for (int k = 0; k < 16; ++k) {
        for (int j = 0; j < 256; ++j) {
            weights[k * 256 + j] = (j == 0) ? 1.0 : 0.0;
        }
    }

    seal::Plaintext pt_features;
    seal::Plaintext pt_weights;
    ctx.encoder->encode(features, ctx.scale, pt_features);
    ctx.encoder->encode(weights, ctx.scale, pt_weights);

    seal::Ciphertext ct;
    ctx.encryptor->encrypt(pt_features, ct);

    ctx.evaluator->multiply_plain_inplace(ct, pt_weights);
    ctx.evaluator->rescale_to_next_inplace(ct);

    seal::Ciphertext hoisted =
        hoisted_tree_sum(ct, ctx.galois_keys, *ctx.evaluator, 256);

    seal::Plaintext pt_out;
    ctx.decryptor->decrypt(hoisted, pt_out);
    std::vector<double> decoded;
    ctx.encoder->decode(pt_out, decoded);
    REQUIRE(decoded.size() >= 1);
    REQUIRE_THAT(decoded[0], Catch::Matchers::WithinAbs(256.0 * value, 1e-3));

    seal::KeyGenerator keygen(*ctx.context, ctx.secret_key);
    std::vector<int> naive_steps(255);
    for (int s = 0; s < 255; ++s) {
        naive_steps[s] = s + 1;
    }
    seal::GaloisKeys gk_full;
    keygen.create_galois_keys(naive_steps, gk_full);

    using clock = std::chrono::high_resolution_clock;
    constexpr int iters = 100;

    clock::time_point t_h0 = clock::now();
    for (int i = 0; i < iters; ++i) {
        (void)hoisted_tree_sum(ct, ctx.galois_keys, *ctx.evaluator, 256);
    }
    const double hoisted_us =
        std::chrono::duration<double, std::micro>(clock::now() - t_h0).count();

    clock::time_point t_n0 = clock::now();
    for (int i = 0; i < iters; ++i) {
        (void)naive_tree_sum(ct, gk_full, *ctx.evaluator, 256);
    }
    const double naive_us =
        std::chrono::duration<double, std::micro>(clock::now() - t_n0).count();

    REQUIRE(hoisted_us > 0.0);
    const double speedup = naive_us / hoisted_us;
    REQUIRE(speedup >= 2.0);
}
