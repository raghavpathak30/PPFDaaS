// Benchmark harness for the base CKKS arm (SEAL 4.1.2): one request packs
// 16 transactions x 256 features into one n=8192 ciphertext; the vendor does
// multiply_plain + rescale + a 256-slot reduction; scores land at slots k*256.
//
// Self-contained: generates its own keys in-process (client side: secret and
// public key, encrypt, decrypt; vendor side: evaluator + Galois keys only), so
// it needs no artifact files and no gRPC. The reductions are the production
// ones, linked from vendor_server/src/rotation_hoisting.cpp unchanged.
//
// Targets (see README.md for the JSON schema):
//   parity   plaintext parity gate only (both param sets, fold and bsgs)
//   a1       per-request stage breakdown + bytes on the wire (deployed fold)
//   a2       fold vs BSGS, interleaved in one invocation
//   a3       thread scaling of one vendor request (fold and bsgs)
//   a4       throughput: N concurrent in-process requests

#include "json_writer.h"
#include "packing.h"
#include "rotation_hoisting.h"

#include <seal/seal.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <omp.h>
#include <sched.h>
#include <unistd.h>

namespace {

using clk = std::chrono::steady_clock;
constexpr double kTolerance = 1e-3;  // same gate as vendor_server/src/benchmark_160.cpp
constexpr int kBaby = 16, kGiant = 16;

double us_since(clk::time_point t0) {
    return std::chrono::duration<double, std::micro>(clk::now() - t0).count();
}

// ---- options --------------------------------------------------------------------------

struct Options {
    std::string target;
    std::string params = "both";            // 160 | 200 | both
    int reps = 100;
    int warmup = 20;
    unsigned seed = 42;
    std::string out_dir = ".";
    std::vector<int> threads = {1, 2, 4, 8, 16, 20};       // a3
    std::vector<int> concurrency = {1, 2, 4, 8, 16, 20};   // a4
    std::vector<std::string> strategies;    // empty = per-target default
    int omp_threads = 1;                    // a1, a2 (a3/a4 set their own)
    double duration_s = 10.0;               // a4, per concurrency level
    int a4_warmup = 5;                      // a4, per worker
    int block = 256;                        // b; only 256 is implemented
};

std::vector<int> parse_ints(const std::string &s) {
    std::vector<int> v;
    std::stringstream ss(s);
    for (std::string tok; std::getline(ss, tok, ',');)
        if (!tok.empty()) v.push_back(std::stoi(tok));
    return v;
}

Options parse_args(int argc, char **argv) {
    Options o;
    if (argc < 2) throw std::invalid_argument("usage: ckks_bench <parity|a1|a2|a3|a4> [--flag=value ...]");
    o.target = argv[1];
    for (int i = 2; i < argc; ++i) {
        const std::string a = argv[i];
        const auto eq = a.find('=');
        if (a.rfind("--", 0) != 0 || eq == std::string::npos)
            throw std::invalid_argument("bad argument (want --flag=value): " + a);
        const std::string k = a.substr(2, eq - 2), v = a.substr(eq + 1);
        if (k == "params") o.params = v;
        else if (k == "reps") o.reps = std::stoi(v);
        else if (k == "warmup") o.warmup = std::stoi(v);
        else if (k == "seed") o.seed = static_cast<unsigned>(std::stoul(v));
        else if (k == "out") o.out_dir = v;
        else if (k == "threads") o.threads = parse_ints(v);
        else if (k == "concurrency") o.concurrency = parse_ints(v);
        else if (k == "omp-threads") o.omp_threads = std::stoi(v);
        else if (k == "duration") o.duration_s = std::stod(v);
        else if (k == "a4-warmup") o.a4_warmup = std::stoi(v);
        else if (k == "block") o.block = std::stoi(v);
        else if (k == "strategies") {
            std::stringstream ss(v);
            for (std::string t; std::getline(ss, t, ',');) o.strategies.push_back(t);
        } else throw std::invalid_argument("unknown flag: --" + k);
    }
    if (o.params != "160" && o.params != "200" && o.params != "both")
        throw std::invalid_argument("--params must be 160, 200 or both");
    if (o.reps < 1 || o.warmup < 0) throw std::invalid_argument("--reps >= 1, --warmup >= 0");
    for (const auto &s : o.strategies)
        if (s != "fold" && s != "bsgs") throw std::invalid_argument("strategy must be fold or bsgs: " + s);
    return o;
}

// ---- statistics -----------------------------------------------------------------------

struct Stats {
    double median = 0, min = 0, max = 0, mean = 0, p95 = 0;
    std::size_t n = 0;
};

Stats stats_of(std::vector<double> v) {
    Stats s;
    if (v.empty()) return s;
    std::sort(v.begin(), v.end());
    s.n = v.size();
    s.min = v.front();
    s.max = v.back();
    double sum = 0;
    for (double x : v) sum += x;
    s.mean = sum / static_cast<double>(s.n);
    s.median = (s.n % 2) ? v[s.n / 2] : 0.5 * (v[s.n / 2 - 1] + v[s.n / 2]);
    s.p95 = v[static_cast<std::size_t>(0.95 * static_cast<double>(s.n - 1))];
    return s;
}

// Writes {median,min,max,mean,p95} converted from microseconds to ms, scaled by `div`.
void write_stats_ms(JsonWriter &j, const std::string &key, const Stats &s, double div = 1.0) {
    const double f = 1.0 / (1000.0 * div);
    j.begin_object(key)
        .num("median", s.median * f).num("min", s.min * f).num("max", s.max * f)
        .num("mean", s.mean * f).num("p95", s.p95 * f)
        .end_object();
}

// ---- CKKS setup: one client, one vendor ------------------------------------------------

struct ParamSet {
    std::string name;
    std::vector<int> bits;
};

const ParamSet &param_set(const std::string &name) {
    static const ParamSet p160{"160", {60, 40, 60}};
    static const ParamSet p200{"200", {60, 40, 40, 60}};
    return name == "160" ? p160 : p200;
}

enum class Strategy { Fold, Bsgs };
const char *strategy_name(Strategy s) { return s == Strategy::Fold ? "fold" : "bsgs"; }
Strategy strategy_from(const std::string &s) { return s == "fold" ? Strategy::Fold : Strategy::Bsgs; }
int strategy_rotations(Strategy s) { return s == Strategy::Fold ? 8 : (kBaby - 1) + (kGiant - 1); }

struct Workload {
    std::vector<std::vector<double>> x, w;       // [txn][feature], unpacked
    std::vector<std::vector<double>> x_slots, w_slots;   // [ct][slot]
    std::vector<double> expected;                // plaintext oracle, one per txn
};

Workload make_workload(const PackingLayout &L, unsigned seed) {
    std::mt19937 rng(seed);
    std::uniform_real_distribution<double> dist(-1.0, 1.0);
    Workload wl;
    auto fill = [&](std::vector<std::vector<double>> &m) {
        m.assign(L.txns_per_ct(), std::vector<double>(L.features));
        for (auto &row : m) for (auto &v : row) v = dist(rng);
    };
    fill(wl.x);
    fill(wl.w);
    wl.x_slots = L.pack(wl.x);
    wl.w_slots = L.pack(wl.w);
    wl.expected = L.expected_scores(wl.x, wl.w);
    return wl;
}

struct Setup {
    ParamSet ps;
    PackingLayout layout;
    double scale = std::pow(2.0, 40);
    seal::EncryptionParameters parms{seal::scheme_type::ckks};
    std::shared_ptr<seal::SEALContext> context;
    // client
    std::unique_ptr<seal::SecretKey> sk;
    std::unique_ptr<seal::PublicKey> pk;
    std::unique_ptr<seal::CKKSEncoder> encoder;
    std::unique_ptr<seal::Encryptor> encryptor;
    std::unique_ptr<seal::Decryptor> decryptor;
    // vendor
    std::unique_ptr<seal::Evaluator> evaluator;
    seal::GaloisKeys gk_fold, gk_bsgs;
    std::vector<seal::Plaintext> pt_weights;
    // one-time costs
    double keygen_ms = 0, galois_fold_keygen_ms = 0, galois_bsgs_keygen_ms = 0, encode_weights_ms = 0;
    std::size_t galois_fold_bytes = 0, galois_bsgs_bytes = 0, public_key_bytes = 0;
    Workload wl;

    Setup(const std::string &name, const Options &o) : ps(param_set(name)), layout(o.block) {
        layout.require_deployed_layout();
        parms.set_poly_modulus_degree(8192);
        parms.set_coeff_modulus(seal::CoeffModulus::Create(8192, ps.bits));
        context = std::make_shared<seal::SEALContext>(parms, true, seal::sec_level_type::tc128);
        if (!context->parameters_set()) throw std::runtime_error("SEAL rejected parameters " + name);

        auto t0 = clk::now();
        seal::KeyGenerator keygen(*context);
        sk = std::make_unique<seal::SecretKey>(keygen.secret_key());
        pk = std::make_unique<seal::PublicKey>();
        keygen.create_public_key(*pk);
        keygen_ms = us_since(t0) / 1000.0;
        public_key_bytes = static_cast<std::size_t>(pk->save_size(seal::compr_mode_type::none));

        t0 = clk::now();
        keygen.create_galois_keys(layout.fold_steps(), gk_fold);
        galois_fold_keygen_ms = us_since(t0) / 1000.0;
        galois_fold_bytes = static_cast<std::size_t>(gk_fold.save_size(seal::compr_mode_type::none));
        t0 = clk::now();
        keygen.create_galois_keys(std::vector<int>(BSGS_ROTATION_STEPS.begin(), BSGS_ROTATION_STEPS.end()), gk_bsgs);
        galois_bsgs_keygen_ms = us_since(t0) / 1000.0;
        galois_bsgs_bytes = static_cast<std::size_t>(gk_bsgs.save_size(seal::compr_mode_type::none));

        encoder = std::make_unique<seal::CKKSEncoder>(*context);
        encryptor = std::make_unique<seal::Encryptor>(*context, *pk);
        decryptor = std::make_unique<seal::Decryptor>(*context, *sk);
        evaluator = std::make_unique<seal::Evaluator>(*context);

        wl = make_workload(layout, o.seed);
        // Test hook, off by default: corrupt one server-side weight AFTER the oracle was computed, so the
        // parity gate must fail. Used to prove that a bad run exits non-zero and writes no timings.
        if (const char *d = std::getenv("PPFDAAS_TEST_PERTURB_WEIGHT")) {
            wl.w_slots[0][0] += std::stod(d);
            std::cerr << "WARNING: PPFDAAS_TEST_PERTURB_WEIGHT=" << d << " set; weight [0][0] corrupted\n";
        }
        // Model weights are static per deployment: encoded once, not per request.
        t0 = clk::now();
        pt_weights.resize(wl.w_slots.size());
        for (std::size_t c = 0; c < wl.w_slots.size(); ++c) encoder->encode(wl.w_slots[c], scale, pt_weights[c]);
        encode_weights_ms = us_since(t0) / 1000.0;
    }

    const seal::GaloisKeys &keys_for(Strategy s) const { return s == Strategy::Fold ? gk_fold : gk_bsgs; }
};

// ---- the request pipeline, stage by stage -----------------------------------------------

struct ClientEncryptStages { double encode_us = 0, encrypt_us = 0; };
struct VendorStages { double multiply_us = 0, rescale_us = 0, reduce_us = 0; };
struct ClientDecryptStages { double decrypt_us = 0, decode_us = 0; };

std::vector<seal::Ciphertext> client_encrypt(Setup &S, ClientEncryptStages &st) {
    std::vector<seal::Ciphertext> cts(S.wl.x_slots.size());
    for (std::size_t c = 0; c < cts.size(); ++c) {
        seal::Plaintext pt;
        auto t0 = clk::now();
        S.encoder->encode(S.wl.x_slots[c], S.scale, pt);
        st.encode_us += us_since(t0);
        t0 = clk::now();
        S.encryptor->encrypt(pt, cts[c]);
        st.encrypt_us += us_since(t0);
    }
    return cts;
}

// Consumes `cts`. The cross-ciphertext add (cts_per_batch > 1) is counted under reduce.
seal::Ciphertext vendor_eval(Setup &S, Strategy strat, std::vector<seal::Ciphertext> cts, VendorStages &st) {
    const seal::GaloisKeys &gk = S.keys_for(strat);
    seal::Ciphertext total;
    for (std::size_t c = 0; c < cts.size(); ++c) {
        auto t0 = clk::now();
        S.evaluator->multiply_plain_inplace(cts[c], S.pt_weights[c]);
        st.multiply_us += us_since(t0);
        t0 = clk::now();
        S.evaluator->rescale_to_next_inplace(cts[c]);
        st.rescale_us += us_since(t0);
        t0 = clk::now();
        seal::Ciphertext part;
        if (strat == Strategy::Fold) hoisted_tree_sum(cts[c], gk, *S.evaluator, part, 256);
        else bsgs_reduction(cts[c], gk, *S.evaluator, part, 256, kBaby, kGiant);
        if (c == 0) total = std::move(part);
        else S.evaluator->add_inplace(total, part);
        st.reduce_us += us_since(t0);
    }
    return total;
}

std::vector<double> client_decrypt(Setup &S, const seal::Ciphertext &ct, ClientDecryptStages &st) {
    seal::Plaintext pt;
    std::vector<double> decoded;
    auto t0 = clk::now();
    S.decryptor->decrypt(ct, pt);
    st.decrypt_us += us_since(t0);
    t0 = clk::now();
    S.encoder->decode(pt, decoded);
    st.decode_us += us_since(t0);
    return decoded;
}

double max_abs_error(const Setup &S, const std::vector<double> &decoded) {
    const auto got = S.layout.read_scores(decoded);
    double e = 0;
    for (std::size_t k = 0; k < got.size(); ++k) e = std::max(e, std::fabs(got[k] - S.wl.expected[k]));
    return e;
}

struct Parity {
    std::string strategy;
    int threads = 1;
    double max_abs_error = 0;
    bool passed = false;
};

Parity parity_gate(Setup &S, Strategy strat) {
    VendorStages vs; ClientEncryptStages es; ClientDecryptStages ds;
    auto out = vendor_eval(S, strat, client_encrypt(S, es), vs);
    Parity p;
    p.strategy = strategy_name(strat);
    p.threads = omp_get_max_threads();
    p.max_abs_error = max_abs_error(S, client_decrypt(S, out, ds));
    p.passed = p.max_abs_error < kTolerance;
    return p;
}

void write_parity(JsonWriter &j, const std::string &key, const std::vector<Parity> &ps) {
    j.begin_array(key);
    for (const auto &p : ps)
        j.begin_object().str("strategy", p.strategy).integer("omp_threads", p.threads)
            .num("max_abs_error", p.max_abs_error).num("tolerance", kTolerance)
            .boolean("passed", p.passed).end_object();
    j.end_array();
}

bool all_passed(const std::vector<Parity> &ps) {
    return std::all_of(ps.begin(), ps.end(), [](const Parity &p) { return p.passed; });
}

// ---- run metadata -------------------------------------------------------------------------

const char *env_or(const char *k, const char *d) { const char *v = std::getenv(k); return v ? v : d; }

// CPUs this process may use, from libgomp's view at startup. sched_getaffinity(0) is the MAIN THREAD's
// mask, which OMP_PROC_BIND narrows to one place (and which std::threads then inherit).
int process_cpus() { return omp_get_num_procs(); }

int main_thread_affinity_cpus() {
    cpu_set_t set;
    CPU_ZERO(&set);
    return sched_getaffinity(0, sizeof(set), &set) == 0 ? CPU_COUNT(&set) : -1;
}

void write_run_header(JsonWriter &j, const Options &o, const std::string &target, int omp_threads_at_start) {
    char host[256] = "unknown";
    gethostname(host, sizeof(host) - 1);
    j.str("target", target);
    j.str("test_hook_perturb_weight", env_or("PPFDAAS_TEST_PERTURB_WEIGHT", "off"));
    j.str("timestamp_utc", [] {
        char b[32]; std::time_t t = std::time(nullptr);
        std::strftime(b, sizeof(b), "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&t));
        return std::string(b);
    }());
    j.str("hostname", host);
    j.begin_object("build")
        .str("repo_sha", env_or("PPFDAAS_REPO_SHA", "unknown"))
        .str("seal_version", SEAL_VERSION)
        .str("compiler", __VERSION__)
#ifdef NDEBUG
        .str("build_type", "Release (-O3 -DNDEBUG)")
#else
        .str("build_type", "NOT RELEASE (NDEBUG undefined) - do not use for timing")
#endif
        .integer("openmp_version", _OPENMP)
        .end_object();
    j.begin_object("threading")
        .integer("omp_max_threads_at_start", omp_threads_at_start)
        .integer("process_cpus", process_cpus())
        .integer("main_thread_affinity_cpus", main_thread_affinity_cpus())
        .integer("hardware_concurrency", std::thread::hardware_concurrency())
        .begin_object("env");
    for (const char *k : {"OMP_NUM_THREADS", "OMP_PROC_BIND", "OMP_PLACES", "OMP_WAIT_POLICY", "OMP_DYNAMIC",
                          "OMP_NESTED", "OMP_THREAD_LIMIT", "GOMP_CPU_AFFINITY", "GOMP_SPINCOUNT"})
        j.str(k, env_or(k, "unset"));
    j.end_object().end_object();
    j.begin_object("seed")
        .integer("data_seed", o.seed)
        .str("data_rng", "std::mt19937(seed), uniform_real_distribution(-1,1); x rows first, then w rows, [txn][feature]")
        .str("seal_randomness", "not seeded by this harness: keygen and encryption noise use SEAL's default "
                                "random generator (system entropy), so ciphertexts differ run to run; the "
                                "plaintext workload is identical for a given data_seed")
        .end_object();
}

void write_setup(JsonWriter &j, const Setup &S, const Options &o) {
    const auto &L = S.layout;
    j.begin_object("params")
        .str("param_set", S.ps.name + "-bit")
        .integer("poly_modulus_degree", 8192)
        .integer("slots", L.slots)
        .begin_array("coeff_modulus_bits");
    for (int b : S.ps.bits) j.integer("", b);
    j.end_array()
        .integer("coeff_modulus_total_bits", [&] { int t = 0; for (int b : S.ps.bits) t += b; return t; }())
        .num("scale_bits", 40)
        .str("security", "tc128 (SEAL-enforced)")
        .end_object();
    j.begin_object("packing")
        .integer("block_width_b", L.block)
        .integer("transactions_per_request", L.txns_per_ct())
        .integer("features_per_txn", L.features)
        .integer("ciphertexts_per_request", L.cts_per_batch())
        .integer("fold_rotations_log2_b", L.fold_rotations())
        .str("score_slots", "k*256 for k = 0..15")
        .end_object();
    j.begin_object("setup_one_time")
        .num("keygen_ms", S.keygen_ms)
        .num("encode_weights_ms", S.encode_weights_ms)
        .integer("public_key_bytes", S.public_key_bytes)
        .integer("galois_keys_fold_bytes", S.galois_fold_bytes)
        .num("galois_keys_fold_keygen_ms", S.galois_fold_keygen_ms)
        .integer("galois_keys_bsgs_bytes", S.galois_bsgs_bytes)
        .num("galois_keys_bsgs_keygen_ms", S.galois_bsgs_keygen_ms)
        .end_object();
    (void)o;
}

constexpr const char *kAccountingNote =
    "One request = one ciphertext carrying 16 transactions x 256 features (slot t*256+f). "
    "per_request_ms = statistic (median/min/max) over repetitions of the end-to-end time of one request, "
    "where each repetition's time is the sum of its own stages. "
    "per_transaction_ms = per_request_ms / 16 (transactions_per_request), applied to each statistic. "
    "Wire bytes are seal::Ciphertext::save_size(); per_transaction bytes = bytes / 16. "
    "Client = encode+encrypt and decrypt+decode; vendor = multiply_plain + rescale + reduction. "
    "Weight encoding and key generation are one-time setup, reported under setup_one_time, not per request.";

std::ofstream open_out(const Options &o, const std::string &file) {
    std::ofstream f(o.out_dir + "/" + file);
    if (!f) throw std::runtime_error("cannot write " + o.out_dir + "/" + file);
    return f;
}

std::vector<std::string> param_names(const Options &o) {
    return o.params == "both" ? std::vector<std::string>{"160", "200"} : std::vector<std::string>{o.params};
}

std::vector<Strategy> strategies_or(const Options &o, std::vector<Strategy> dflt) {
    if (o.strategies.empty()) return dflt;
    std::vector<Strategy> v;
    for (const auto &s : o.strategies) v.push_back(strategy_from(s));
    return v;
}

// ---- targets -----------------------------------------------------------------------------------

// Runs the parity gate for the given strategies; returns false (and the caller skips timing) on failure.
bool gate(Setup &S, const std::vector<Strategy> &strats, std::vector<Parity> &out) {
    for (auto s : strats) {
        out.push_back(parity_gate(S, s));
        const Parity &p = out.back();
        if (!p.passed)
            std::cerr << "PARITY GATE FAILED: " << S.ps.name << "-bit " << p.strategy << " omp_threads=" << p.threads
                      << " max_abs_error=" << p.max_abs_error << " >= tolerance " << kTolerance << "\n";
    }
    return all_passed(out);
}

int run_parity(const Options &o, int omp0) {
    auto f = open_out(o, "parity.json");
    JsonWriter j(f);
    j.begin_object();
    write_run_header(j, o, "parity", omp0);
    bool ok = true;
    j.begin_array("results");
    for (const auto &name : param_names(o)) {
        Setup S(name, o);
        std::vector<Parity> ps;
        ok &= gate(S, {Strategy::Fold, Strategy::Bsgs}, ps);
        j.begin_object().str("param_set", name + "-bit");
        write_parity(j, "parity_gate", ps);
        j.end_object();
        for (const auto &p : ps)
            std::cerr << "parity " << name << "-bit " << p.strategy << ": max_abs_error=" << p.max_abs_error
                      << (p.passed ? " PASS" : " FAIL") << "\n";
    }
    j.end_array().boolean("all_passed", ok).end_object();
    return ok ? 0 : 1;
}

int run_a1(const Options &o, int omp0) {
    const Strategy strat = strategies_or(o, {Strategy::Fold}).front();
    omp_set_dynamic(0);
    omp_set_num_threads(o.omp_threads);
    auto f = open_out(o, "a1_stage_breakdown.json");
    JsonWriter j(f);
    j.begin_object();
    write_run_header(j, o, "a1", omp0);
    j.integer("repetitions", o.reps).integer("warmup", o.warmup).str("strategy", strategy_name(strat))
        .integer("omp_threads", omp_get_max_threads())
        .str("accounting_note", kAccountingNote);
    bool ok = true;
    j.begin_array("results");
    for (const auto &name : param_names(o)) {
        Setup S(name, o);
        std::vector<Parity> ps;
        const bool passed = gate(S, {strat}, ps);
        ok &= passed;
        j.begin_object().str("param_set", name + "-bit");
        write_setup(j, S, o);
        write_parity(j, "parity_gate", ps);
        j.boolean("timing_skipped", !passed);
        if (!passed) { std::cerr << "parity FAILED for " << name << "-bit; skipping timing\n"; j.end_object(); continue; }

        std::vector<double> enc, encr, mul, resc, red, dec, decd, client, vendor, total;
        double worst_err = 0;
        std::size_t req_bytes = 0, resp_bytes = 0, req_zstd = 0, resp_zstd = 0;
        for (int i = -o.warmup; i < o.reps; ++i) {
            ClientEncryptStages es; VendorStages vs; ClientDecryptStages ds;
            auto cts = client_encrypt(S, es);
            if (i == 0) {  // wire sizes of this request/response (same for every repetition)
                for (const auto &c : cts) {
                    req_bytes += static_cast<std::size_t>(c.save_size(seal::compr_mode_type::none));
                    req_zstd += static_cast<std::size_t>(c.save_size(seal::compr_mode_type::zstd));
                }
            }
            auto out = vendor_eval(S, strat, std::move(cts), vs);
            if (i == 0) {
                resp_bytes = static_cast<std::size_t>(out.save_size(seal::compr_mode_type::none));
                resp_zstd = static_cast<std::size_t>(out.save_size(seal::compr_mode_type::zstd));
            }
            auto decoded = client_decrypt(S, out, ds);
            if (i < 0) continue;
            worst_err = std::max(worst_err, max_abs_error(S, decoded));
            enc.push_back(es.encode_us); encr.push_back(es.encrypt_us);
            mul.push_back(vs.multiply_us); resc.push_back(vs.rescale_us); red.push_back(vs.reduce_us);
            dec.push_back(ds.decrypt_us); decd.push_back(ds.decode_us);
            const double c = es.encode_us + es.encrypt_us + ds.decrypt_us + ds.decode_us;
            const double v = vs.multiply_us + vs.rescale_us + vs.reduce_us;
            client.push_back(c); vendor.push_back(v); total.push_back(c + v);
        }
        const double txns = S.layout.txns_per_ct();
        j.num("max_abs_error_over_timed_reps", worst_err);
        j.begin_object("stages_ms");
        write_stats_ms(j, "client_encode", stats_of(enc));
        write_stats_ms(j, "client_encrypt", stats_of(encr));
        write_stats_ms(j, "vendor_multiply_plain", stats_of(mul));
        write_stats_ms(j, "vendor_rescale", stats_of(resc));
        write_stats_ms(j, "vendor_reduction", stats_of(red));
        write_stats_ms(j, "client_decrypt", stats_of(dec));
        write_stats_ms(j, "client_decode", stats_of(decd));
        j.end_object();
        j.begin_object("per_request_ms");
        write_stats_ms(j, "client_total", stats_of(client));
        write_stats_ms(j, "vendor_total", stats_of(vendor));
        write_stats_ms(j, "total", stats_of(total));
        j.end_object();
        j.begin_object("per_transaction_ms");
        write_stats_ms(j, "client_total", stats_of(client), txns);
        write_stats_ms(j, "vendor_total", stats_of(vendor), txns);
        write_stats_ms(j, "total", stats_of(total), txns);
        j.end_object();
        j.begin_object("wire_bytes")
            .integer("request_ciphertext", req_bytes).integer("response_ciphertext", resp_bytes)
            .integer("request_ciphertext_zstd", req_zstd).integer("response_ciphertext_zstd", resp_zstd)
            .num("request_per_transaction", static_cast<double>(req_bytes) / txns)
            .num("response_per_transaction", static_cast<double>(resp_bytes) / txns)
            .str("note", "save_size(compr_mode_none); *_zstd = save_size(zstd), SEAL's default save() mode")
            .end_object();
        j.end_object();
        std::cerr << "a1 " << name << "-bit: total median " << stats_of(total).median / 1000.0 << " ms/request\n";
    }
    j.end_array().boolean("parity_all_passed", ok).end_object();
    return ok ? 0 : 1;
}

int run_a2(const Options &o, int omp0) {
    omp_set_dynamic(0);
    omp_set_num_threads(o.omp_threads);
    const std::vector<Strategy> strats = {Strategy::Fold, Strategy::Bsgs};
    auto f = open_out(o, "a2_fold_vs_bsgs.json");
    JsonWriter j(f);
    j.begin_object();
    write_run_header(j, o, "a2", omp0);
    j.integer("repetitions", o.reps).integer("warmup", o.warmup).integer("omp_threads", o.omp_threads)
        .str("scope", "vendor circuit only: multiply_plain + rescale + reduction; strategies interleaved "
                      "within each repetition on the same input ciphertext")
        .str("accounting_note", kAccountingNote);
    bool ok = true;
    j.begin_array("results");
    for (const auto &name : param_names(o)) {
        Setup S(name, o);
        std::vector<Parity> ps;
        const bool passed = gate(S, strats, ps);
        ok &= passed;
        j.begin_object().str("param_set", name + "-bit");
        write_setup(j, S, o);
        write_parity(j, "parity_gate", ps);
        j.boolean("timing_skipped", !passed);
        if (!passed) { std::cerr << "parity FAILED for " << name << "-bit; skipping timing\n"; j.end_object(); continue; }

        ClientEncryptStages es;
        const auto input = client_encrypt(S, es);
        struct Acc { std::vector<double> mul, resc, red, total; } acc[2];
        for (int i = -o.warmup; i < o.reps; ++i)
            for (int s = 0; s < 2; ++s) {
                VendorStages vs;
                vendor_eval(S, strats[s], input, vs);
                if (i < 0) continue;
                acc[s].mul.push_back(vs.multiply_us); acc[s].resc.push_back(vs.rescale_us);
                acc[s].red.push_back(vs.reduce_us);
                acc[s].total.push_back(vs.multiply_us + vs.rescale_us + vs.reduce_us);
            }
        const double txns = S.layout.txns_per_ct();
        j.begin_array("strategies");
        for (int s = 0; s < 2; ++s) {
            j.begin_object().str("strategy", strategy_name(strats[s]))
                .integer("rotations", strategy_rotations(strats[s]))
                .integer("galois_keys", static_cast<std::int64_t>(s == 0 ? 8 : BSGS_ROTATION_STEPS.size()))
                .integer("galois_keys_bytes", s == 0 ? S.galois_fold_bytes : S.galois_bsgs_bytes);
            j.begin_object("stages_ms");
            write_stats_ms(j, "vendor_multiply_plain", stats_of(acc[s].mul));
            write_stats_ms(j, "vendor_rescale", stats_of(acc[s].resc));
            write_stats_ms(j, "vendor_reduction", stats_of(acc[s].red));
            j.end_object();
            j.begin_object("per_request_ms");
            write_stats_ms(j, "vendor_total", stats_of(acc[s].total));
            j.end_object();
            j.begin_object("per_transaction_ms");
            write_stats_ms(j, "vendor_total", stats_of(acc[s].total), txns);
            j.end_object();
            j.end_object();
        }
        j.end_array();
        j.num("bsgs_over_fold_median_ratio", stats_of(acc[1].total).median / stats_of(acc[0].total).median);
        j.end_object();
        std::cerr << "a2 " << name << "-bit: fold " << stats_of(acc[0].total).median / 1000.0 << " ms, bsgs "
                  << stats_of(acc[1].total).median / 1000.0 << " ms (threads=" << o.omp_threads << ")\n";
    }
    j.end_array().boolean("parity_all_passed", ok).end_object();
    return ok ? 0 : 1;
}

int run_a3(const Options &o, int omp0) {
    omp_set_dynamic(0);
    const auto strats = strategies_or(o, {Strategy::Fold, Strategy::Bsgs});
    const int avail = process_cpus();
    auto f = open_out(o, "a3_thread_scaling.json");
    JsonWriter j(f);
    j.begin_object();
    write_run_header(j, o, "a3", omp0);
    j.integer("repetitions", o.reps).integer("warmup", o.warmup)
        .str("scope", "latency of ONE vendor request (multiply_plain + rescale + reduction) vs OpenMP thread count; "
                      "only bsgs_reduction contains parallel regions (SEAL 4.1.2 and the sequential fold are "
                      "single-threaded), so fold is expected to be flat")
        .str("accounting_note", kAccountingNote);
    bool ok = true;
    j.begin_array("results");
    for (const auto &name : param_names(o)) {
        Setup S(name, o);
        j.begin_object().str("param_set", name + "-bit");
        write_setup(j, S, o);
        ClientEncryptStages es;
        const auto input = client_encrypt(S, es);
        const double txns = S.layout.txns_per_ct();
        j.begin_array("points");
        std::vector<std::pair<std::string, double>> base;   // strategy -> median at 1 thread
        for (auto strat : strats) {
            for (int t : o.threads) {
                omp_set_num_threads(t);
                std::vector<Parity> ps;
                const bool passed = gate(S, {strat}, ps);
                ok &= passed;
                j.begin_object().str("strategy", strategy_name(strat)).integer("omp_threads", t)
                    .integer("omp_max_threads_effective", omp_get_max_threads())
                    .boolean("oversubscribed", avail > 0 && t > avail);
                write_parity(j, "parity_gate", ps);
                j.boolean("timing_skipped", !passed);
                if (passed) {
                    std::vector<double> tot;
                    for (int i = -o.warmup; i < o.reps; ++i) {
                        VendorStages vs;
                        vendor_eval(S, strat, input, vs);
                        if (i >= 0) tot.push_back(vs.multiply_us + vs.rescale_us + vs.reduce_us);
                    }
                    const Stats st = stats_of(tot);
                    if (t == o.threads.front()) base.emplace_back(strategy_name(strat), st.median);
                    double b = st.median;
                    for (auto &p : base) if (p.first == strategy_name(strat)) b = p.second;
                    j.begin_object("per_request_ms");
                    write_stats_ms(j, "vendor_total", st);
                    j.end_object();
                    j.begin_object("per_transaction_ms");
                    write_stats_ms(j, "vendor_total", st, txns);
                    j.end_object();
                    j.num("speedup_vs_first_thread_count", b / st.median);
                    std::cerr << "a3 " << name << "-bit " << strategy_name(strat) << " threads=" << t << ": "
                              << st.median / 1000.0 << " ms\n";
                }
                j.end_object();
            }
        }
        j.end_array().end_object();
    }
    j.end_array().boolean("parity_all_passed", ok).end_object();
    return ok ? 0 : 1;
}

// Start gate: workers park until the main thread has checked their warmup outputs.
class Latch {
public:
    explicit Latch(int n) : n_(n) {}
    void arrive_and_wait() {
        std::unique_lock<std::mutex> lk(m_);
        if (--n_ == 0) cv_.notify_all();
        else cv_.wait(lk, [&] { return n_ <= 0; });
    }
    void wait() { std::unique_lock<std::mutex> lk(m_); cv_.wait(lk, [&] { return n_ <= 0; }); }
private:
    std::mutex m_; std::condition_variable cv_; int n_;
};

int run_a4(const Options &o, int omp0) {
    omp_set_dynamic(0);
    const auto strats = strategies_or(o, {Strategy::Fold});
    auto f = open_out(o, "a4_throughput.json");
    JsonWriter j(f);
    j.begin_object();
    write_run_header(j, o, "a4", omp0);
    j.num("duration_s_per_point", o.duration_s).integer("warmup_requests_per_worker", o.a4_warmup)
        .integer("repetitions", 0)
        .str("repetitions_note", "closed loop for duration_s per point; the completed request count per point "
                                 "is reported as completed_requests")
        .str("scope", "vendor circuit only (multiply_plain + rescale + reduction), N worker threads each running "
                      "back-to-back requests on its own input copy; OpenMP threads per worker = 1; shared "
                      "Evaluator and Galois keys; one process, so SEAL's global memory pool is shared")
        .str("accounting_note", std::string(kAccountingNote) +
             " For a4, per_request_ms is the per-request latency under load (pooled over workers); "
             "throughput_txn_per_s = completed_requests * 16 / elapsed_s; "
             "amortized_wall_ms_per_txn = 1000 / throughput_txn_per_s.");
    bool ok = true;
    j.begin_array("results");
    for (const auto &name : param_names(o)) {
        Setup S(name, o);
        j.begin_object().str("param_set", name + "-bit");
        write_setup(j, S, o);
        ClientEncryptStages es;
        const auto input = client_encrypt(S, es);
        const double txns = S.layout.txns_per_ct();
        j.begin_array("points");
        for (auto strat : strats) {
            for (int N : o.concurrency) {
                std::vector<std::vector<double>> lat(N);
                std::vector<seal::Ciphertext> warm_out(N);
                std::vector<std::string> err(N);
                Latch ready(N + 1), go(1);
                std::atomic<bool> stop{false};
                std::vector<clk::time_point> ends(N);
                std::vector<std::thread> workers;
                for (int w = 0; w < N; ++w)
                    workers.emplace_back([&, w] {
                        try {
                            omp_set_num_threads(1);
                            VendorStages vs;
                            for (int i = 0; i < std::max(1, o.a4_warmup); ++i) warm_out[w] = vendor_eval(S, strat, input, vs);
                            ready.arrive_and_wait();   // main checks warm_out[*] while we are parked
                            go.wait();
                            while (!stop.load(std::memory_order_relaxed)) {
                                const auto t0 = clk::now();
                                vendor_eval(S, strat, input, vs);
                                lat[w].push_back(us_since(t0));
                            }
                            ends[w] = clk::now();
                        } catch (const std::exception &e) { err[w] = e.what(); ends[w] = clk::now(); }
                    });
                ready.arrive_and_wait();
                // Parity of every worker's output, under concurrent execution, before any timing.
                std::vector<Parity> ps;
                double worst = 0;
                for (int w = 0; w < N; ++w) {
                    ClientDecryptStages ds;
                    if (!err[w].empty()) { worst = 1e9; continue; }
                    worst = std::max(worst, max_abs_error(S, client_decrypt(S, warm_out[w], ds)));
                }
                Parity p; p.strategy = strategy_name(strat); p.threads = 1; p.max_abs_error = worst; p.passed = worst < kTolerance;
                ps.push_back(p);
                ok &= p.passed;
                const auto t_begin = clk::now();
                if (p.passed) {
                    go.arrive_and_wait();
                    std::this_thread::sleep_for(std::chrono::duration<double>(o.duration_s));
                }
                stop = true;
                if (!p.passed) go.arrive_and_wait();   // release workers; they see stop and exit
                for (auto &t : workers) t.join();

                j.begin_object().str("strategy", strategy_name(strat)).integer("concurrent_requests", N)
                    .integer("omp_threads_per_worker", 1)
                    .boolean("oversubscribed", N > process_cpus())
                    .integer("main_thread_affinity_cpus", main_thread_affinity_cpus());
                write_parity(j, "parity_gate", ps);
                j.boolean("timing_skipped", !p.passed);
                if (p.passed) {
                    clk::time_point last = t_begin;
                    std::vector<double> all;
                    std::vector<std::int64_t> per_worker;
                    for (int w = 0; w < N; ++w) {
                        if (ends[w] > last) last = ends[w];
                        per_worker.push_back(static_cast<std::int64_t>(lat[w].size()));
                        all.insert(all.end(), lat[w].begin(), lat[w].end());
                    }
                    const double elapsed = std::chrono::duration<double>(last - t_begin).count();
                    const Stats st = stats_of(all);
                    const double rps = static_cast<double>(all.size()) / elapsed;
                    j.integer("completed_requests", static_cast<std::int64_t>(all.size()))
                        .num("elapsed_s", elapsed)
                        .num("throughput_requests_per_s", rps)
                        .num("throughput_txn_per_s", rps * txns)
                        .num("amortized_wall_ms_per_txn", 1000.0 / (rps * txns));
                    j.begin_array("completed_per_worker");
                    for (auto c : per_worker) j.integer("", c);
                    j.end_array();
                    j.begin_object("per_request_ms");
                    write_stats_ms(j, "vendor_total", st);
                    j.end_object();
                    j.begin_object("per_transaction_ms");
                    write_stats_ms(j, "vendor_total", st, txns);
                    j.end_object();
                    std::cerr << "a4 " << name << "-bit " << strategy_name(strat) << " N=" << N << ": " << rps
                              << " req/s, median latency " << st.median / 1000.0 << " ms\n";
                } else {
                    std::cerr << "parity FAILED at N=" << N << "; timing skipped\n";
                }
                j.end_object();
            }
        }
        j.end_array().end_object();
    }
    j.end_array().boolean("parity_all_passed", ok).end_object();
    return ok ? 0 : 1;
}

}  // namespace

int main(int argc, char **argv) {
    try {
        const int omp0 = omp_get_max_threads();
        const Options o = parse_args(argc, argv);
        if (o.target == "parity") return run_parity(o, omp0);
        if (o.target == "a1") return run_a1(o, omp0);
        if (o.target == "a2") return run_a2(o, omp0);
        if (o.target == "a3") return run_a3(o, omp0);
        if (o.target == "a4") return run_a4(o, omp0);
        throw std::invalid_argument("unknown target: " + o.target);
    } catch (const std::exception &e) {
        std::cerr << "ckks_bench: " << e.what() << "\n";
        return 2;
    }
}
