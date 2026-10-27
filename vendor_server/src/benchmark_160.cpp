#include "ckks_context_160.h"

#include "cnp_codec.h"
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

namespace {

using clock_type = std::chrono::high_resolution_clock;

constexpr int kSlotCount = 4096;
constexpr int kLanes = 16;
constexpr int kFeatures = 256;
constexpr int kBabyStep = 16;
constexpr int kGiantStep = 16;

// Synthetic bias, added inside every timed circuit so that all four strategies
// time the same server work: the slot strategies do add_plain(pt_bias) after
// the reduction (as inference_service_160.cpp does); CNP carries the bias in
// its per-request mask. Precomputed plaintexts are built OUTSIDE the timer,
// exactly as the server builds pt_bias_ once at construction; the CNP mask is
// fresh randomness per request, so building it is INSIDE the timer.
constexpr double kBias = 0.37;

constexpr int kWarmupRounds = 20;
constexpr int kMeasureRounds = 100;

// §5.1: full 255-step naive baseline (NAIVE_ROTATION_STEPS) requires
// generating 255 Galois keys, which is itself slow. PPFD_BENCHMARK_ROUNDS
// lets callers (e.g. scripts/generate_ablation.py --fast-ablation) shrink the
// measured-round count without changing the compiled-in default.
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

// Runs multiply_plain -> rescale -> reduction(strategy) -> add_plain(bias) on
// `ct` (consumed), writing the lane-aligned score (dot product + bias) into `out`.
void run_circuit(
        CKKSContext160 &ctx,
        const std::string &strategy,
        const seal::Ciphertext &ct_in,
        const seal::Plaintext &pt_weights,
        const seal::Plaintext &pt_bias,
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
    ctx.evaluator->add_plain_inplace(out, pt_bias);
}

// CNP circuit: multiply_plain(w_rev) -> rescale -> fresh output mask carrying
// the bias (built here, per call: it is per-request randomness) -> add_plain.
// No rotations, no Galois keys. Same stages as the CNP branch of RunInference.
void run_circuit_cnp(
        CKKSContext160 &ctx,
        const seal::Ciphertext &ct_in,
        const seal::Plaintext &pt_wrev,
        seal::Ciphertext &out) {
    out = ct_in;
    ctx.evaluator->multiply_plain_inplace(out, pt_wrev);
    ctx.evaluator->rescale_to_next_inplace(out);
    seal::Plaintext mask;
    cnp::build_output_mask(*ctx.context, out.parms_id(), out.scale(), kBias, true, mask);
    ctx.evaluator->add_plain_inplace(out, mask);
}

} // namespace

int main(int argc, char **argv) {
    std::string strategy; // empty => legacy default behavior (backward-compatible)
    std::string samples_out; // optional: dump raw per-iteration latencies_us as a JSON array
    int cnp_lanes = static_cast<int>(cnp::kMaxLanes); // --lanes=N, cnp only (slot circuits always use 16)
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        const std::string strategy_prefix = "--strategy=";
        const std::string samples_prefix = "--samples-out=";
        const std::string lanes_prefix = "--lanes=";
        if (arg.rfind(strategy_prefix, 0) == 0) {
            strategy = arg.substr(strategy_prefix.size());
        } else if (arg.rfind(samples_prefix, 0) == 0) {
            samples_out = arg.substr(samples_prefix.size());
        } else if (arg.rfind(lanes_prefix, 0) == 0) {
            cnp_lanes = std::atoi(arg.substr(lanes_prefix.size()).c_str());
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

    // ── Phase 4/5 measurement path: --strategy=fold|bsgs|naive ───────────────
    if (strategy != "fold" && strategy != "bsgs" && strategy != "naive" && strategy != "cnp") {
        std::cerr << "benchmark_160: unknown --strategy='" << strategy
                  << "' (expected 'fold', 'bsgs', 'naive', or 'cnp')\n";
        return 2;
    }
    const bool is_cnp = strategy == "cnp";
    if (is_cnp && (cnp_lanes < 1 || cnp_lanes > static_cast<int>(cnp::kMaxLanes))) {
        std::cerr << "benchmark_160: --lanes must be 1..32 for cnp\n";
        return 2;
    }
    const int lanes_used = is_cnp ? cnp_lanes : kLanes;

    // Generate a fresh Galois key set for the requested strategy. This is a
    // BENCHMARK-ONLY operation (CKKSContext160 holds a live secret key and is
    // OUT OF TCB, see ckks_context_160.h / docs/spec.md §6.9). The deployed
    // server provisions only the fold's 8-element ROTATION_STEPS; BSGS_ROTATION_STEPS
    // (30 elements) and NAIVE_ROTATION_STEPS (255 elements, §5.1) are generated
    // here purely "for measurement purposes" (§4.1 / §5.1).
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
        // Each of the 255 rotations reads the ORIGINAL ct (independent
        // inputs, like bsgs's layers), but naive_tree_sum() issues them in a
        // single sequential loop with no batching -- this IS the "naive"
        // baseline: a developer with no knowledge of fold/BSGS would write
        // exactly this. critical_path=255 reflects the AS-IMPLEMENTED,
        // as-measured sequential cost, not a lower bound on parallelism.
        critical_path = 255;
        critical_path_note =
            "rotations are data-independent (each reads the original ct, as "
            "in bsgs's layers) but naive_tree_sum executes them in a single "
            "sequential loop with no hoisting/batching -- critical_path "
            "reflects the as-implemented sequential cost";
    } else if (is_cnp) {
        steps.clear();  // CNP: no rotations, no Galois keys, nothing to generate
        n_rotations = 0;
        critical_path = 0;
    } else {
        steps = {1, 2, 4, 8, 16, 32, 64, 128};
        n_rotations = 8;
        critical_path = 8;
    }

    const clock_type::time_point kg_t0 = clock_type::now();
    if (!is_cnp) keygen.create_galois_keys(steps, galois_keys);
    const clock_type::time_point kg_t1 = clock_type::now();
    const double galois_keygen_us =
        std::chrono::duration<double, std::micro>(kg_t1 - kg_t0).count();

    // ── Build a non-degenerate plaintext oracle: random per-slot weights and
    // features (fixed seed for reproducibility), one independent random
    // vector per lane (16 lanes x 256 features = 4096 slots). ──────────────
    std::mt19937 rng(42);
    std::uniform_real_distribution<double> dist(-1.0, 1.0);

    seal::Plaintext pt_features;
    seal::Plaintext pt_weights;  // slot: per-slot weights; cnp: w_rev(X)
    seal::Plaintext pt_bias;     // slot only (cnp carries the bias in its mask)
    std::vector<double> expected(lanes_used, 0.0);  // dot product + kBias per lane

    if (is_cnp) {
        // CNP oracle: ONE weight vector shared by all lanes (the model), an
        // independent random feature row per lane, dense in every coefficient.
        std::vector<double> w(kFeatures);
        for (double &v : w) v = dist(rng);
        std::vector<double> rows(static_cast<std::size_t>(lanes_used) * kFeatures);
        for (double &v : rows) v = dist(rng);
        for (int k = 0; k < lanes_used; ++k) {
            double acc = 0.0;
            for (int j = 0; j < kFeatures; ++j) acc += w[j] * rows[k * kFeatures + j];
            expected[k] = acc + kBias;
        }
        cnp::encode_coeffs(*ctx.context, cnp::pack_lanes(rows.data(), lanes_used), ctx.scale,
                           ctx.context->first_parms_id(), pt_features);
        cnp::encode_coeffs(*ctx.context, cnp::weights_to_wrev(w), ctx.scale,
                           ctx.context->first_parms_id(), pt_weights);
    } else {
        std::vector<double> features(kSlotCount);
        std::vector<double> weights(kSlotCount);
        for (int i = 0; i < kSlotCount; ++i) {
            features[i] = dist(rng);
            weights[i] = dist(rng);
        }
        for (int k = 0; k < kLanes; ++k) {
            double acc = 0.0;
            for (int j = 0; j < kFeatures; ++j) {
                acc += weights[k * kFeatures + j] * features[k * kFeatures + j];
            }
            expected[k] = acc + kBias;
        }
        ctx.encoder->encode(features, ctx.scale, pt_features);
        ctx.encoder->encode(weights, ctx.scale, pt_weights);

        // Bias plaintext at the post-rescale scale, built once (as the server's
        // pt_bias_ is): scale^2 / q_last, lane-aligned slots k*256.
        const double q_last = static_cast<double>(
            ctx.context->first_context_data()->parms().coeff_modulus().back().value());
        std::vector<double> bias_vec(kSlotCount, 0.0);
        for (int k = 0; k < kLanes; ++k) bias_vec[k * kFeatures] = kBias;
        ctx.encoder->encode(bias_vec, ctx.second_parms_id, (ctx.scale * ctx.scale) / q_last, pt_bias);
    }

    auto run = [&](const seal::Ciphertext &ct_in, seal::Ciphertext &o) {
        if (is_cnp) {
            run_circuit_cnp(ctx, ct_in, pt_weights, o);
        } else {
            run_circuit(ctx, strategy, ct_in, pt_weights, pt_bias, galois_keys, o);
        }
    };

    seal::Ciphertext ct_seed;
    ctx.encryptor->encrypt(pt_features, ct_seed);

    // ── In-band parity gate: bsgs_reduction (and hoisted_tree_sum) must match
    // the plaintext oracle to CKKS precision BEFORE any timing number is
    // trusted (Phase 4 instructions / Phase 5.5 pattern). ──────────────────
    seal::Ciphertext correctness_out;
    run(ct_seed, correctness_out);

    seal::Plaintext pt_decrypted;
    std::vector<double> got_lane(lanes_used, 0.0);
    ctx.decryptor->decrypt(correctness_out, pt_decrypted);
    if (is_cnp) {
        got_lane = cnp::decode_lane_outputs(*ctx.context, pt_decrypted, lanes_used);
    } else {
        std::vector<double> decoded;
        ctx.encoder->decode(pt_decrypted, decoded);
        for (int k = 0; k < kLanes; ++k) got_lane[k] = decoded[k * kFeatures];
    }

    double max_abs_error = 0.0;
    for (int k = 0; k < lanes_used; ++k) {
        const double got = got_lane[k];
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

    // ── Timing: 20 warmup + N measured full-circuit runs (N = measure_rounds(),
    // default kMeasureRounds=100, overridable via PPFD_BENCHMARK_ROUNDS) ─────
    const int n_measure = measure_rounds();
    for (int i = 0; i < kWarmupRounds; ++i) {
        seal::Ciphertext ct;
        ctx.encryptor->encrypt(pt_features, ct);
        seal::Ciphertext out;
        run(ct, out);
    }

    std::vector<double> latencies_us;
    latencies_us.reserve(n_measure);
    for (int i = 0; i < n_measure; ++i) {
        seal::Ciphertext ct;
        ctx.encryptor->encrypt(pt_features, ct);

        clock_type::time_point t0 = clock_type::now();
        seal::Ciphertext out;
        run(ct, out);
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
              << "  \"scope\": \"server_compute_only\",\n"
              << "  \"stages\": \"multiply_plain, rescale, "
              << (is_cnp ? "output mask (fresh CSPRNG, carries bias) + add_plain"
                         : "reduction, add_plain(bias)")
              << "; excludes (de)serialization, encrypt and decrypt\",\n"
              << "  \"lanes\": " << lanes_used << ",\n"
              << "  \"rotations\": " << n_rotations << ",\n"
              << "  \"critical_path\": " << critical_path << ",\n";
    if (!critical_path_note.empty()) {
        std::cout << "  \"critical_path_note\": \"" << critical_path_note << "\",\n";
    }
    std::cout << "  \"galois_keys\": " << steps.size() << ",\n"
              // §5.1: one-time key-generation cost, reported separately from
              // per-request latency_us below. For strategy=naive (255 keys)
              // this dominates wall-clock time but is NOT part of any
              // per-inference latency number.
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

    // avg_us= line preserved for benchmark_multirun.py-style consumers.
    std::cout << "avg_us=" << stats.mean_us << " avg_ms=" << (stats.mean_us / 1000.0) << std::endl;

    return correctness_passed ? 0 : 1;
}
