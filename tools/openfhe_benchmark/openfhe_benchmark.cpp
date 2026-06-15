// tools/openfhe_benchmark/openfhe_benchmark.cpp — Phase 4, §4.3/§4.4.
//
// Timing harness for openfhe_linear_eval.cpp's run_circuit_hoisted(): 20
// warmup + 100 measured end-to-end runs (same structure as
// vendor_server/src/benchmark_160.cpp and tests/benchmark_comparison.py),
// reporting mean/std/p50/p95/p99/min/max per stage. Writes
// tools/openfhe_benchmark/results/openfhe_results.json, consumed by
// scripts/rotation_strategy_comparison.py (Phase 4 §4.4).
//
// In-band parity gate (Phase 5.5 pattern, applied here in Phase 4): each
// run's decoded result is checked against a plaintext oracle by
// run_circuit_hoisted() itself (CircuitTiming::max_abs_error). If the LAST
// measured run's error exceeds kTolerance, this binary exits non-zero and
// the JSON's "correctness_passed" is false — timing numbers from a run that
// fails this gate must not be cited.

#include "openfhe_linear_eval.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

namespace {

using namespace ppfdaas_openfhe;

constexpr int kWarmupRounds = 20;
constexpr int kMeasureRounds = 100;
constexpr double kTolerance = 1e-3;

struct Stats {
    double mean = 0.0;
    double std = 0.0;
    double p50 = 0.0;
    double p95 = 0.0;
    double p99 = 0.0;
    double min = 0.0;
    double max = 0.0;
};

Stats compute_stats(std::vector<double> samples) {
    Stats s;
    if (samples.empty()) return s;
    std::sort(samples.begin(), samples.end());
    const std::size_t n = samples.size();

    double sum = 0.0;
    for (double v : samples) sum += v;
    s.mean = sum / static_cast<double>(n);

    double sq = 0.0;
    for (double v : samples) sq += (v - s.mean) * (v - s.mean);
    s.std = n > 1 ? std::sqrt(sq / static_cast<double>(n - 1)) : 0.0;

    auto pct = [&](double p) {
        double idx = p * static_cast<double>(n - 1);
        std::size_t lo = static_cast<std::size_t>(std::floor(idx));
        std::size_t hi = static_cast<std::size_t>(std::ceil(idx));
        double frac = idx - static_cast<double>(lo);
        return samples[lo] + frac * (samples[hi] - samples[lo]);
    };
    s.p50 = pct(0.50);
    s.p95 = pct(0.95);
    s.p99 = pct(0.99);
    s.min = samples.front();
    s.max = samples.back();
    return s;
}

void write_stat_block(std::ofstream& out, const std::string& name, const Stats& s, bool trailing_comma) {
    out << "    \"" << name << "\": {\n"
        << "      \"mean\": " << s.mean << ",\n"
        << "      \"std\": " << s.std << ",\n"
        << "      \"p50\": " << s.p50 << ",\n"
        << "      \"p95\": " << s.p95 << ",\n"
        << "      \"p99\": " << s.p99 << ",\n"
        << "      \"min\": " << s.min << ",\n"
        << "      \"max\": " << s.max << "\n"
        << "    }" << (trailing_comma ? ",\n" : "\n");
}

} // namespace

int main() {
    LinearEvalContext ctx = build_context();

    // Same fixed-seed plaintext oracle construction as
    // vendor_server/src/benchmark_160.cpp --strategy=bsgs.
    std::mt19937 rng(42);
    std::uniform_real_distribution<double> dist(-1.0, 1.0);
    std::vector<double> features(kSlotCount);
    std::vector<double> weights(kSlotCount);
    for (int i = 0; i < kSlotCount; ++i) {
        features[i] = dist(rng);
        weights[i] = dist(rng);
    }

    std::vector<double> decoded;

    for (int i = 0; i < kWarmupRounds; ++i) {
        run_circuit_hoisted(ctx, features, weights, decoded);
    }

    std::vector<double> encrypt_us, eval_mult_us, precompute_baby_us, rotations_baby_us,
            precompute_giant_us, rotations_giant_us, decrypt_us, total_us;
    encrypt_us.reserve(kMeasureRounds);
    eval_mult_us.reserve(kMeasureRounds);
    precompute_baby_us.reserve(kMeasureRounds);
    rotations_baby_us.reserve(kMeasureRounds);
    precompute_giant_us.reserve(kMeasureRounds);
    rotations_giant_us.reserve(kMeasureRounds);
    decrypt_us.reserve(kMeasureRounds);
    total_us.reserve(kMeasureRounds);

    double max_abs_error = 0.0;
    for (int i = 0; i < kMeasureRounds; ++i) {
        CircuitTiming t = run_circuit_hoisted(ctx, features, weights, decoded);
        encrypt_us.push_back(t.encrypt_us);
        eval_mult_us.push_back(t.eval_mult_us);
        precompute_baby_us.push_back(t.precompute_baby_us);
        rotations_baby_us.push_back(t.rotations_baby_us);
        precompute_giant_us.push_back(t.precompute_giant_us);
        rotations_giant_us.push_back(t.rotations_giant_us);
        decrypt_us.push_back(t.decrypt_us);
        total_us.push_back(t.total_us);
        max_abs_error = std::max(max_abs_error, t.max_abs_error);
    }

    const bool correctness_passed = max_abs_error < kTolerance;
    if (!correctness_passed) {
        std::cerr << "openfhe_benchmark: CORRECTNESS GATE FAILED: max_abs_error="
                  << max_abs_error << " >= tolerance=" << kTolerance << "\n";
    }

    const Stats total_stats = compute_stats(total_us);

    const std::string out_path = "results/openfhe_results.json";
    std::ofstream out(out_path);
    out << "{\n"
        << "  \"status\": \"MEASURED\",\n"
        << "  \"strategy\": \"hoisted_flat\",\n"
        << "  \"library\": \"OpenFHE\",\n"
        << "  \"rotations\": " << (kBabyStep - 1) + (kGiantStep - 1) << ",\n"
        << "  \"critical_path\": 1,\n"
        << "  \"galois_keys\": " << kBsgsRotationSteps.size() << ",\n"
        << "  \"n\": " << kMeasureRounds << ",\n"
        << "  \"warmup\": " << kWarmupRounds << ",\n"
        << "  \"correctness_max_abs_error\": " << max_abs_error << ",\n"
        << "  \"correctness_passed\": " << (correctness_passed ? "true" : "false") << ",\n"
        << "  \"latency_us\": {\n";
    write_stat_block(out, "encrypt", compute_stats(encrypt_us), true);
    write_stat_block(out, "eval_mult", compute_stats(eval_mult_us), true);
    write_stat_block(out, "precompute_baby", compute_stats(precompute_baby_us), true);
    write_stat_block(out, "rotations_baby_total", compute_stats(rotations_baby_us), true);
    write_stat_block(out, "precompute_giant", compute_stats(precompute_giant_us), true);
    write_stat_block(out, "rotations_giant_total", compute_stats(rotations_giant_us), true);
    write_stat_block(out, "decrypt", compute_stats(decrypt_us), true);
    write_stat_block(out, "total", total_stats, false);
    out << "  }\n"
        << "}\n";
    out.close();

    std::cout << "avg_us=" << total_stats.mean << " avg_ms=" << (total_stats.mean / 1000.0) << std::endl;
    std::cout << "Wrote " << out_path << std::endl;

    return correctness_passed ? 0 : 1;
}
