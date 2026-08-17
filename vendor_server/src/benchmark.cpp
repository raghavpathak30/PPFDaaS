#include "ckks_context.h"

#include "he_inference.h"
#include "rotation_hoisting.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

// ─── Phase 3 (privacy-cost de-confound) ────────────────────────────────────
//
// This binary is the 200-bit ({60,40,40,60}) twin of benchmark_160.cpp
// (160-bit, {60,40,60}). Both use a secret-key-holding context (CKKSContext /
// CKKSContext160, identical struct layout, OUT OF TCB -- see ckks_context.h /
// docs/spec.md §6.9) purely for local self-encrypt/decrypt latency
// measurement, both call the SAME rotation_hoisting.{h,cpp} functions
// (hoisted_tree_sum / bsgs_reduction / naive_tree_sum), and both go through
// the SAME depth1_he_inference-style sequence: multiply_plain_inplace ->
// rescale_to_next_inplace -> reduction(strategy). The ONLY designed
// difference between this binary and benchmark_160 is ctx.params.coeff_modulus
// (200 bits / 4 primes here vs 160 bits / 3 primes there). This binary exists
// so that artifacts/privacy_cost_analysis.py can measure "the cost of one
// additional 40-bit RNS prime" on a matched pair, instead of across the
// vendor_server_main (legacy CKKSContext-in-RPC-service) vs vendor_server_160
// (EvalContext160, eval-only, different service/concurrency/provisioning
// code) deployed-binary confound documented in AUDIT.md.

namespace {

using clock_type = std::chrono::high_resolution_clock;

constexpr int kSlotCount = 4096;
constexpr int kLanes = 16;
constexpr int kFeatures = 256;
constexpr int kBabyStep = 16;
constexpr int kGiantStep = 16;

constexpr int kWarmupRounds = 20;
constexpr int kMeasureRounds = 100;

int measure_rounds() {
    const char *e = std::getenv("PPFD_BENCHMARK_ROUNDS");
    if (!e) return kMeasureRounds;
    const int v = std::atoi(e);
    return v > 0 ? v : kMeasureRounds;
}

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

void run_circuit(
        CKKSContext &ctx,
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
    } else if (strategy == "naive") {
        out = naive_tree_sum(ct, galois_keys, *ctx.evaluator, kFeatures);
    } else {
        hoisted_tree_sum(ct, galois_keys, *ctx.evaluator, out, kFeatures);
    }
}

} // namespace

int main(int argc, char **argv) {
    std::string strategy; // empty => legacy default behavior (backward-compatible)
    std::string samples_out; // optional: dump raw per-iteration latencies_us as a JSON array
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        const std::string strategy_prefix = "--strategy=";
        const std::string samples_prefix = "--samples-out=";
        if (arg.rfind(strategy_prefix, 0) == 0) {
            strategy = arg.substr(strategy_prefix.size());
        } else if (arg.rfind(samples_prefix, 0) == 0) {
            samples_out = arg.substr(samples_prefix.size());
        }
    }

    CKKSContext ctx;

    if (strategy.empty()) {
        // ── Legacy path: unchanged from pre-Phase-3 benchmark.cpp ───────────
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
            seal::Ciphertext out = depth1_he_inference(ctx, ct, pt_weights);
            (void)out;
        }
        clock_type::time_point t1 = clock_type::now();
        const double total_us = std::chrono::duration<double, std::micro>(t1 - t0).count();
        const double avg_us = total_us / static_cast<double>(kRuns);
        const double avg_ms = avg_us / 1000.0;
        std::cout << "avg_us=" << avg_us << " avg_ms=" << avg_ms << std::endl;
        return avg_us > 10000.0 ? 1 : 0;
    }

    // ── Phase 3 matched-pair measurement path: --strategy=fold|bsgs|naive ──
    if (strategy != "fold" && strategy != "bsgs" && strategy != "naive") {
        std::cerr << "benchmark: unknown --strategy='" << strategy
                  << "' (expected 'fold', 'bsgs', or 'naive')\n";
        return 2;
    }

    // Fresh Galois key set for the requested strategy, generated from this
    // BENCHMARK-ONLY context's own secret key (CKKSContext is OUT OF TCB for
    // any deployed binary; see ckks_context.h). Mirrors benchmark_160.cpp
    // exactly so the only designed difference between the two binaries is
    // ctx.params.coeff_modulus.
    seal::KeyGenerator keygen(*ctx.context, ctx.secret_key);
    seal::GaloisKeys galois_keys;
    std::vector<int> steps;
    int n_rotations = 0;
    int critical_path = 0;
    std::string critical_path_note;
    if (strategy == "bsgs") {
        steps.assign(BSGS_ROTATION_STEPS.begin(), BSGS_ROTATION_STEPS.end());
        n_rotations = (kBabyStep - 1) + (kGiantStep - 1); // 15 + 15 = 30
        critical_path = 2;
    } else if (strategy == "naive") {
        steps.assign(NAIVE_ROTATION_STEPS.begin(), NAIVE_ROTATION_STEPS.end());
        n_rotations = 255;
        critical_path = 255;
        critical_path_note =
            "rotations are data-independent (each reads the original ct, as "
            "in bsgs's layers) but naive_tree_sum executes them in a single "
            "sequential loop with no hoisting/batching -- critical_path "
            "reflects the as-implemented sequential cost";
    } else {
        steps = {1, 2, 4, 8, 16, 32, 64, 128};
        n_rotations = 8;
        critical_path = 8;
    }

    const clock_type::time_point kg_t0 = clock_type::now();
    keygen.create_galois_keys(steps, galois_keys);
    const clock_type::time_point kg_t1 = clock_type::now();
    const double galois_keygen_us =
        std::chrono::duration<double, std::micro>(kg_t1 - kg_t0).count();

    // Same fixed-seed, non-degenerate plaintext oracle as benchmark_160.cpp,
    // so the parity-gate tolerance comparison is apples-to-apples.
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

    // In-band parity gate against the plaintext oracle BEFORE any timing
    // number is trusted (same discipline as benchmark_160.cpp).
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
        std::cerr << "benchmark: CORRECTNESS GATE FAILED for strategy='" << strategy
                  << "': max_abs_error=" << max_abs_error
                  << " >= tolerance=" << kTolerance << "\n";
    }

    const int n_measure = measure_rounds();
    for (int i = 0; i < kWarmupRounds; ++i) {
        seal::Ciphertext ct;
        ctx.encryptor->encrypt(pt_features, ct);
        seal::Ciphertext out;
        run_circuit(ctx, strategy, ct, pt_weights, galois_keys, out);
    }

    std::vector<double> latencies_us;
    latencies_us.reserve(n_measure);
    for (int i = 0; i < n_measure; ++i) {
        seal::Ciphertext ct;
        ctx.encryptor->encrypt(pt_features, ct);

        clock_type::time_point t0 = clock_type::now();
        seal::Ciphertext out;
        run_circuit(ctx, strategy, ct, pt_weights, galois_keys, out);
        clock_type::time_point t1 = clock_type::now();

        latencies_us.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
    }

    const Stats stats = compute_stats(latencies_us);

    if (!samples_out.empty()) {
        std::ofstream samples_file(samples_out);
        samples_file << "[";
        for (std::size_t i = 0; i < latencies_us.size(); ++i) {
            if (i) samples_file << ",";
            samples_file << latencies_us[i];
        }
        samples_file << "]";
    }

    std::cout << "{\n"
              << "  \"strategy\": \"" << strategy << "\",\n"
              << "  \"rotations\": " << n_rotations << ",\n"
              << "  \"critical_path\": " << critical_path << ",\n";
    if (!critical_path_note.empty()) {
        std::cout << "  \"critical_path_note\": \"" << critical_path_note << "\",\n";
    }
    std::cout << "  \"galois_keys\": " << steps.size() << ",\n"
              << "  \"galois_keygen_us\": " << galois_keygen_us << ",\n"
              << "  \"n\": " << n_measure << ",\n"
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

    std::cout << "avg_us=" << stats.mean_us << " avg_ms=" << (stats.mean_us / 1000.0) << std::endl;

    return correctness_passed ? 0 : 1;
}
