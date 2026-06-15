#include "ckks_context_160.h"

#include "he_inference.h"
#include "rotation_hoisting.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>
#include <vector>

namespace {

using clock_type = std::chrono::high_resolution_clock;

constexpr int kSlotCount = 4096;
constexpr int kLanes = 16;
constexpr int kFeatures = 256;
constexpr int kBabyStep = 16;
constexpr int kGiantStep = 16;

constexpr int kWarmupRounds = 20;
constexpr int kMeasureRounds = 100;

struct Stats {
    double mean_us = 0.0;
    double std_us = 0.0;
    double p50_us = 0.0;
    double p95_us = 0.0;
    double p99_us = 0.0;
    double min_us = 0.0;
    double max_us = 0.0;
};

Stats compute_stats(std::vector<double> samples_us) {
    Stats s;
    if (samples_us.empty()) return s;
    std::sort(samples_us.begin(), samples_us.end());
    const std::size_t n = samples_us.size();

    double sum = 0.0;
    for (double v : samples_us) sum += v;
    s.mean_us = sum / static_cast<double>(n);

    double sq = 0.0;
    for (double v : samples_us) sq += (v - s.mean_us) * (v - s.mean_us);
    s.std_us = n > 1 ? std::sqrt(sq / static_cast<double>(n - 1)) : 0.0;

    auto pct = [&](double p) {
        double idx = p * static_cast<double>(n - 1);
        std::size_t lo = static_cast<std::size_t>(std::floor(idx));
        std::size_t hi = static_cast<std::size_t>(std::ceil(idx));
        double frac = idx - static_cast<double>(lo);
        return samples_us[lo] + frac * (samples_us[hi] - samples_us[lo]);
    };
    s.p50_us = pct(0.50);
    s.p95_us = pct(0.95);
    s.p99_us = pct(0.99);
    s.min_us = samples_us.front();
    s.max_us = samples_us.back();
    return s;
}

// Runs multiply_plain -> rescale -> reduction(strategy) on `ct` (consumed),
// writing the lane-aligned dot-product result into `out`.
void run_circuit(
        CKKSContext160 &ctx,
        const std::string &strategy,
        const seal::Ciphertext &ct_in,
        const seal::Plaintext &pt_weights,
        const seal::GaloisKeys &galois_keys,
        seal::Ciphertext &out) {
    seal::Ciphertext ct = ct_in;
    ctx.evaluator->multiply_plain_inplace(ct, pt_weights);
    ctx.evaluator->rescale_to_next_inplace(ct);

    if (strategy == "bsgs") {
        bsgs_reduction(ct, galois_keys, *ctx.evaluator, out, kFeatures, kBabyStep, kGiantStep);
    } else {
        hoisted_tree_sum(ct, galois_keys, *ctx.evaluator, out, kFeatures);
    }
}

} // namespace

int main(int argc, char **argv) {
    std::string strategy; // empty => legacy default behavior (backward-compatible)
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        const std::string prefix = "--strategy=";
        if (arg.rfind(prefix, 0) == 0) {
            strategy = arg.substr(prefix.size());
        }
    }

    CKKSContext160 ctx;

    if (strategy.empty()) {
        // ── Legacy path: unchanged from pre-Phase-4 benchmark_160 ───────────
        // benchmark_multirun.py parses "avg_us=" from stdout; preserved
        // verbatim for backward compatibility when invoked with no flags.
        std::vector<double> features(kSlotCount, 0.42);
        std::vector<double> weights(kSlotCount, 0.001);

        seal::Plaintext pt_features;
        seal::Plaintext pt_weights;
        ctx.encoder->encode(features, ctx.scale, pt_features);
        ctx.encoder->encode(weights, ctx.scale, pt_weights);

        constexpr int kRuns = 1000;
        clock_type::time_point t0 = clock_type::now();
        for (int i = 0; i < kRuns; ++i) {
            seal::Ciphertext ct;
            ctx.encryptor->encrypt(pt_features, ct);
            seal::Ciphertext out = depth1_he_inference_160(ctx, ct, pt_weights);
            (void)out;
        }
        clock_type::time_point t1 = clock_type::now();
        const double total_us = std::chrono::duration<double, std::micro>(t1 - t0).count();
        const double avg_us = total_us / static_cast<double>(kRuns);
        const double avg_ms = avg_us / 1000.0;
        std::cout << "avg_us=" << avg_us << " avg_ms=" << avg_ms << std::endl;
        return avg_us > 10000.0 ? 1 : 0;
    }

    // ── Phase 4 measurement path: --strategy=fold|bsgs ──────────────────────
    if (strategy != "fold" && strategy != "bsgs") {
        std::cerr << "benchmark_160: unknown --strategy='" << strategy
                  << "' (expected 'fold' or 'bsgs')\n";
        return 2;
    }

    // Generate a fresh Galois key set for the requested strategy. This is a
    // BENCHMARK-ONLY operation (CKKSContext160 holds a live secret key and is
    // OUT OF TCB, see ckks_context_160.h / docs/spec.md §6.9). The deployed
    // server provisions only the fold's 8-element ROTATION_STEPS; BSGS_ROTATION_STEPS
    // (30 elements) is generated here purely "for measurement purposes" (§4.1).
    seal::KeyGenerator keygen(*ctx.context, ctx.secret_key);
    seal::GaloisKeys galois_keys;
    std::vector<int> steps;
    int n_rotations = 0;
    int critical_path = 0;
    if (strategy == "bsgs") {
        steps.assign(BSGS_ROTATION_STEPS.begin(), BSGS_ROTATION_STEPS.end());
        n_rotations = (kBabyStep - 1) + (kGiantStep - 1); // 15 + 15 = 30
        critical_path = 2;
    } else {
        steps = {1, 2, 4, 8, 16, 32, 64, 128};
        n_rotations = 8;
        critical_path = 8;
    }
    keygen.create_galois_keys(steps, galois_keys);

    // ── Build a non-degenerate plaintext oracle: random per-slot weights and
    // features (fixed seed for reproducibility), one independent random
    // vector per lane (16 lanes x 256 features = 4096 slots). ──────────────
    std::mt19937 rng(42);
    std::uniform_real_distribution<double> dist(-1.0, 1.0);
    std::vector<double> features(kSlotCount);
    std::vector<double> weights(kSlotCount);
    for (int i = 0; i < kSlotCount; ++i) {
        features[i] = dist(rng);
        weights[i] = dist(rng);
    }

    std::vector<double> expected(kLanes, 0.0);
    for (int k = 0; k < kLanes; ++k) {
        double acc = 0.0;
        for (int j = 0; j < kFeatures; ++j) {
            acc += weights[k * kFeatures + j] * features[k * kFeatures + j];
        }
        expected[k] = acc;
    }

    seal::Plaintext pt_features;
    seal::Plaintext pt_weights;
    ctx.encoder->encode(features, ctx.scale, pt_features);
    ctx.encoder->encode(weights, ctx.scale, pt_weights);

    seal::Ciphertext ct_seed;
    ctx.encryptor->encrypt(pt_features, ct_seed);

    // ── In-band parity gate: bsgs_reduction (and hoisted_tree_sum) must match
    // the plaintext oracle to CKKS precision BEFORE any timing number is
    // trusted (Phase 4 instructions / Phase 5.5 pattern). ──────────────────
    seal::Ciphertext correctness_out;
    run_circuit(ctx, strategy, ct_seed, pt_weights, galois_keys, correctness_out);

    seal::Plaintext pt_decrypted;
    std::vector<double> decoded;
    ctx.decryptor->decrypt(correctness_out, pt_decrypted);
    ctx.encoder->decode(pt_decrypted, decoded);

    double max_abs_error = 0.0;
    for (int k = 0; k < kLanes; ++k) {
        const double got = decoded[k * kFeatures];
        const double err = std::fabs(got - expected[k]);
        max_abs_error = std::max(max_abs_error, err);
    }

    constexpr double kTolerance = 1e-3;
    const bool correctness_passed = max_abs_error < kTolerance;
    if (!correctness_passed) {
        std::cerr << "benchmark_160: CORRECTNESS GATE FAILED for strategy='" << strategy
                  << "': max_abs_error=" << max_abs_error
                  << " >= tolerance=" << kTolerance << "\n";
    }

    // ── Timing: 20 warmup + 100 measured full-circuit runs ──────────────────
    for (int i = 0; i < kWarmupRounds; ++i) {
        seal::Ciphertext ct;
        ctx.encryptor->encrypt(pt_features, ct);
        seal::Ciphertext out;
        run_circuit(ctx, strategy, ct, pt_weights, galois_keys, out);
    }

    std::vector<double> latencies_us;
    latencies_us.reserve(kMeasureRounds);
    for (int i = 0; i < kMeasureRounds; ++i) {
        seal::Ciphertext ct;
        ctx.encryptor->encrypt(pt_features, ct);

        clock_type::time_point t0 = clock_type::now();
        seal::Ciphertext out;
        run_circuit(ctx, strategy, ct, pt_weights, galois_keys, out);
        clock_type::time_point t1 = clock_type::now();

        latencies_us.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
    }

    const Stats stats = compute_stats(latencies_us);

    std::cout << "{\n"
              << "  \"strategy\": \"" << strategy << "\",\n"
              << "  \"rotations\": " << n_rotations << ",\n"
              << "  \"critical_path\": " << critical_path << ",\n"
              << "  \"galois_keys\": " << steps.size() << ",\n"
              << "  \"n\": " << kMeasureRounds << ",\n"
              << "  \"warmup\": " << kWarmupRounds << ",\n"
              << "  \"correctness_max_abs_error\": " << max_abs_error << ",\n"
              << "  \"correctness_passed\": " << (correctness_passed ? "true" : "false") << ",\n"
              << "  \"latency_us\": {\n"
              << "    \"mean\": " << stats.mean_us << ",\n"
              << "    \"std\": " << stats.std_us << ",\n"
              << "    \"p50\": " << stats.p50_us << ",\n"
              << "    \"p95\": " << stats.p95_us << ",\n"
              << "    \"p99\": " << stats.p99_us << ",\n"
              << "    \"min\": " << stats.min_us << ",\n"
              << "    \"max\": " << stats.max_us << "\n"
              << "  }\n"
              << "}" << std::endl;

    // avg_us= line preserved for benchmark_multirun.py-style consumers.
    std::cout << "avg_us=" << stats.mean_us << " avg_ms=" << (stats.mean_us / 1000.0) << std::endl;

    return correctness_passed ? 0 : 1;
}
