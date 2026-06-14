// vendor_server/src/weight_loader.cpp  — NORMATIVE (§4.3)

#include "weight_loader.h"

#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <vector>

// ── NORMATIVE: Platform endianness guard ─────────────────────────────────

static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
    "weight_loader: big-endian platform detected. File format is explicit LE.");

static constexpr uint32_t     EXPECTED_N = 256;

static constexpr std::size_t  EXPECTED_SZ = 2060; // 4+8+256*8

seal::Plaintext load_weights_as_plaintext(
        const std::string& path, seal::CKKSEncoder& enc, double scale) {
    double unused_bias;
    return load_weights_as_plaintext(path, enc, scale, unused_bias);
}

seal::Plaintext load_weights_as_plaintext(
        const std::string& path, seal::CKKSEncoder& enc, double scale, double& bias_out) {

    std::ifstream f(path, std::ios::binary | std::ios::ate);

    if (!f.is_open()) throw std::runtime_error("weight_loader: cannot open '" + path + "'");

    const auto fsz = static_cast<std::size_t>(f.tellg());

    if (fsz != EXPECTED_SZ)

        throw std::runtime_error("weight_loader: expected 2060 bytes, got " + std::to_string(fsz));

    f.seekg(0);

    uint32_t n = 0;
    double bias = 0.0;

    f.read(reinterpret_cast<char*>(&n),    4);

    f.read(reinterpret_cast<char*>(&bias), 8);

    if (n != EXPECTED_N)

        throw std::runtime_error("weight_loader: n=" + std::to_string(n) + " expected 256");

    bias_out = bias;

    std::vector<double> w(n);

    f.read(reinterpret_cast<char*>(w.data()), n * sizeof(double));

    // SIMD tiling: replicate w[0..255] across all 16 transaction lanes

    // tiled[k*256 + j] = w[j]  for k=0..15, j=0..255

    // max index = 15*256+255 = 4095 < 4096 — no off-by-one

    std::vector<double> tiled(4096);

    for (int k = 0; k < 16; ++k)

        for (int j = 0; j < 256; ++j)

            tiled[k * 256 + j] = w[j];

    seal::Plaintext pt;

    enc.encode(tiled, scale, pt);

    // §1.3: bias is returned via bias_out, NOT encoded into this plaintext.
    // The caller (inference_service_160.cpp) applies it as a separate
    // add_plain after the rotation-sum, so it lands once per lane instead of
    // once per slot (see ctx_.params.coeff_modulus().back() comment there).

    return pt;

}
