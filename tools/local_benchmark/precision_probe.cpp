// tools/local_benchmark/precision_probe.cpp — OUT OF TCB (Phase 3, Item 3.2)
//
// Standalone, self-contained precision-characterization tool. Builds its OWN
// SEALContext + SecretKey + Decryptor (a capability the deployed
// EvalContext160 / vendor_server_160 MUST NEVER have, per Phase 1 / §1.1) and
// runs the depth-1 inference circuit on a representative batch, decrypting
// the ciphertext after EACH stage so the error against the plaintext oracle
// can be measured stage-by-stage. This is exactly the kind of standalone
// benchmarking/measurement use case tools/local_benchmark exists for.
//
// Parameters mirror EvalContext160 (vendor_server/src/eval_context_160.cpp):
// N=8192, coeff_modulus={60,40,60}, scale=2^40, sec_level_type::tc128.
//
// Usage: precision_probe <features.bin> <model_weights.bin>
//   features.bin:      4096 little-endian float64, layout features[k*256+j]
//                       = transaction k's feature j, for k=0..15, j=0..255.
//   model_weights.bin: PPFDaaS weight file (uint32 n=256, float64 bias,
//                       256x float64 weights), same format as
//                       artifacts/model_weights.bin.
//
// Prints a single JSON object to stdout with the decrypted per-slot values
// after each circuit stage. Stage-vs-plaintext error computation and
// reporting is done by scripts/precision_analysis.py.

#include <seal/seal.h>

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {

constexpr int N_SLOTS = 4096;
constexpr int N_FEATURES = 256;
constexpr int N_TXNS = 16;
constexpr int STEPS[] = {1, 2, 4, 8, 16, 32, 64, 128};

std::vector<double> read_doubles(const std::string &path, std::size_t count) {
    std::ifstream f(path, std::ios::binary);
    if (!f.is_open()) {
        throw std::runtime_error("precision_probe: cannot open '" + path + "'");
    }
    std::vector<double> out(count);
    f.read(reinterpret_cast<char *>(out.data()), static_cast<std::streamsize>(count * sizeof(double)));
    if (!f) {
        throw std::runtime_error("precision_probe: short read on '" + path + "'");
    }
    return out;
}

// Loads model_weights.bin (uint32 n=256, float64 bias, 256x float64 weights)
// and tiles the weights across all 16 lanes: tiled[k*256+j] = w[j].
std::vector<double> read_tiled_weights(const std::string &path, double &bias_out) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f.is_open()) {
        throw std::runtime_error("precision_probe: cannot open '" + path + "'");
    }
    const auto fsz = static_cast<std::size_t>(f.tellg());
    if (fsz != 2060) {
        throw std::runtime_error("precision_probe: expected 2060-byte model_weights.bin, got " + std::to_string(fsz));
    }
    f.seekg(0);
    uint32_t n = 0;
    f.read(reinterpret_cast<char *>(&n), 4);
    f.read(reinterpret_cast<char *>(&bias_out), 8);
    if (n != N_FEATURES) {
        throw std::runtime_error("precision_probe: n=" + std::to_string(n) + " expected 256");
    }
    std::vector<double> w(N_FEATURES);
    f.read(reinterpret_cast<char *>(w.data()), N_FEATURES * sizeof(double));

    std::vector<double> tiled(N_SLOTS);
    for (int k = 0; k < N_TXNS; ++k) {
        for (int j = 0; j < N_FEATURES; ++j) {
            tiled[k * N_FEATURES + j] = w[j];
        }
    }
    return tiled;
}

void print_json_array(const std::vector<double> &v) {
    std::printf("[");
    for (std::size_t i = 0; i < v.size(); ++i) {
        std::printf("%.17g%s", v[i], (i + 1 < v.size()) ? "," : "");
    }
    std::printf("]");
}

} // namespace

int main(int argc, char **argv) {
    if (argc != 3) {
        std::cerr << "usage: precision_probe <features.bin> <model_weights.bin>\n";
        return 2;
    }

    seal::EncryptionParameters params(seal::scheme_type::ckks);
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60, 40, 60}));
    seal::SEALContext context(params, true, seal::sec_level_type::tc128);
    if (!context.parameters_set()) {
        throw std::runtime_error("precision_probe: SEAL rejected parameters");
    }

    seal::KeyGenerator keygen(context);
    seal::SecretKey secret_key = keygen.secret_key();
    seal::PublicKey public_key;
    keygen.create_public_key(public_key);
    seal::GaloisKeys galois_keys;
    keygen.create_galois_keys(std::vector<int>(std::begin(STEPS), std::end(STEPS)), galois_keys);

    seal::CKKSEncoder encoder(context);
    seal::Encryptor encryptor(context, public_key);
    seal::Decryptor decryptor(context, secret_key);
    seal::Evaluator evaluator(context);

    const double scale = std::pow(2.0, 40);

    const std::vector<double> features = read_doubles(argv[1], N_SLOTS);
    double bias = 0.0;
    const std::vector<double> tiled_weights = read_tiled_weights(argv[2], bias);

    seal::Plaintext pt_x, pt_w;
    encoder.encode(features, scale, pt_x);
    encoder.encode(tiled_weights, scale, pt_w);

    seal::Ciphertext ct;
    encryptor.encrypt(pt_x, ct);

    // Stage 1: after multiply_plain (scale -> scale^2, same level).
    evaluator.multiply_plain_inplace(ct, pt_w);
    seal::Plaintext pt_stage1;
    std::vector<double> stage1;
    decryptor.decrypt(ct, pt_stage1);
    encoder.decode(pt_stage1, stage1);

    // Stage 2: after rescale_to_next (drops the 40-bit prime, scale -> ~2^40).
    evaluator.rescale_to_next_inplace(ct);
    seal::Plaintext pt_stage2;
    std::vector<double> stage2;
    decryptor.decrypt(ct, pt_stage2);
    encoder.decode(pt_stage2, stage2);

    // Stage 3: after hoisted_tree_sum (acc = acc + rotate(acc, step), doubling).
    {
        seal::Ciphertext acc = ct, tmp;
        for (int step : STEPS) {
            evaluator.rotate_vector(acc, step, galois_keys, tmp);
            evaluator.add_inplace(acc, tmp);
        }
        ct = std::move(acc);
    }
    seal::Plaintext pt_stage3;
    std::vector<double> stage3;
    decryptor.decrypt(ct, pt_stage3);
    encoder.decode(pt_stage3, stage3);

    // Stage 4: after add_plain (bias), matching inference_service_160.cpp's
    // §1.3 bias-scale computation: bias_scale = scale^2 / q_last, where
    // q_last is the trailing prime of the DATA chain at first_parms_id (the
    // 40-bit prime dropped by the rescale above), encoded at ct's (post-
    // rescale) parms_id, with bias placed only at lane-aligned slots k*256.
    const double q_last = static_cast<double>(
        context.first_context_data()->parms().coeff_modulus().back().value());
    const double bias_scale = (scale * scale) / q_last;
    std::vector<double> bias_vec(N_SLOTS, 0.0);
    for (int k = 0; k < N_TXNS; ++k) {
        bias_vec[k * N_FEATURES] = bias;
    }
    seal::Plaintext pt_bias;
    encoder.encode(bias_vec, ct.parms_id(), bias_scale, pt_bias);
    evaluator.add_plain_inplace(ct, pt_bias);

    seal::Plaintext pt_stage4;
    std::vector<double> stage4;
    decryptor.decrypt(ct, pt_stage4);
    encoder.decode(pt_stage4, stage4);

    std::printf("{\n");
    std::printf("  \"scale\": %.17g,\n", scale);
    std::printf("  \"q_last\": %.17g,\n", q_last);
    std::printf("  \"bias_scale\": %.17g,\n", bias_scale);
    std::printf("  \"bias\": %.17g,\n", bias);
    std::printf("  \"stage1_after_multiply_plain\": ");
    print_json_array(stage1);
    std::printf(",\n  \"stage2_after_rescale\": ");
    print_json_array(stage2);
    std::printf(",\n  \"stage3_after_hoisted_tree_sum\": ");
    print_json_array(stage3);
    std::printf(",\n  \"stage4_after_add_plain_bias\": ");
    print_json_array(stage4);
    std::printf("\n}\n");

    return 0;
}
