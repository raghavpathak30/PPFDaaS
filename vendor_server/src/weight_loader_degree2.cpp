// he_core/src/weight_loader_degree2.cpp  — NORMATIVE

// Identical structure to weight_loader.cpp with N=512, SIMD for n=16384.

#include "weight_loader_degree2.h"

#include <fstream>

#include <stdexcept>

#include <vector>

#include <cstdint>

static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
    "weight_loader_degree2: big-endian platform not supported.");

static constexpr uint32_t    EXPECTED_N_D2 = 512;
static constexpr std::size_t EXPECTED_SZ_D2 = 4108;

seal::Plaintext load_degree2_weights_as_plaintext(
        const std::string& path, seal::CKKSEncoder& enc, double scale)
{
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f.is_open()) throw std::runtime_error("weight_loader_d2: cannot open '" + path + "'");
    const auto fsz = static_cast<std::size_t>(f.tellg());
    if (fsz != EXPECTED_SZ_D2)
        throw std::runtime_error("weight_loader_d2: expected 4108 bytes, got " + std::to_string(fsz));
    f.seekg(0);
    uint32_t n = 0; double bias = 0.0;
    f.read(reinterpret_cast<char*>(&n), 4);
    f.read(reinterpret_cast<char*>(&bias), 8);
    if (n != EXPECTED_N_D2)
        throw std::runtime_error("weight_loader_d2: n=" + std::to_string(n) + " expected 512");
    std::vector<double> w(n);
    f.read(reinterpret_cast<char*>(w.data()), n * sizeof(double));
    const int SLOTS = 8192;
    std::vector<double> tiled(SLOTS);
    for (int k = 0; k < 16; ++k)
        for (int j = 0; j < 512; ++j)
            tiled[k * 512 + j] = w[j];
    seal::Plaintext pt;
    enc.encode(tiled, scale, pt);
    return pt;
}
