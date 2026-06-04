#include "ckks_context_160.h"

#include "he_inference.h"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <vector>

int main() {
    using clock = std::chrono::high_resolution_clock;

    CKKSContext160 ctx;

    std::vector<double> features(4096, 0.42);

    std::vector<double> weights(4096, 0.001);

    seal::Plaintext pt_features;

    seal::Plaintext pt_weights;

    ctx.encoder->encode(features, ctx.scale, pt_features);

    ctx.encoder->encode(weights, ctx.scale, pt_weights);

    constexpr int kRuns = 1000;

    clock::time_point t0 = clock::now();

    for (int i = 0; i < kRuns; ++i) {

        seal::Ciphertext ct;

        ctx.encryptor->encrypt(pt_features, ct);

        seal::Ciphertext out = depth1_he_inference_160(ctx, ct, pt_weights);

        (void)out;

    }

    clock::time_point t1 = clock::now();

    const double total_us =

        std::chrono::duration<double, std::micro>(t1 - t0).count();

    const double avg_us = total_us / static_cast<double>(kRuns);

    const double avg_ms = avg_us / 1000.0;

    std::cout << "avg_us=" << avg_us << " avg_ms=" << avg_ms << std::endl;

    if (avg_us > 10000.0) {

        return 1;

    }

    return 0;

}
