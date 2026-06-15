// §5.7 Part A: ciphertext wire-size probe.
//
// Standalone, OUT-OF-TCB binary (mirrors benchmark_160's local-only scope --
// see tools/local_benchmark/ckks_context_160.h's TCB note). For both the
// 160-bit ({60,40,60}) and 200-bit ({60,40,40,60}) chains, measures:
//
//   - "standard": seal::Ciphertext::save_size() for a freshly public-key
//     encrypted ciphertext (the wire format actually sent by
//     bank_client/bank_client.py).
//   - "seeded": seal::Serializable<seal::Ciphertext>::save_size() for a
//     symmetric-key (encrypt_symmetric) encryption of the same plaintext --
//     SEAL's PRNG-seed-based compact form, which omits the second
//     (pseudo-random) polynomial and regenerates it from a stored seed on
//     load. Requires the secret key (encrypt_symmetric), so it is NOT what
//     the bank's public-key encryption path produces today -- it is reported
//     as a "what if" comparison point.
//   - compressed sizes (zlib, zstd) of the standard ciphertext, and the
//     resulting compression ratios.
//
// Prints one JSON object to stdout: {"160bit": {...}, "200bit": {...}}.

#include "ckks_context.h"
#include "ckks_context_160.h"

#include <iostream>
#include <vector>

namespace {

struct WireSizes {
    std::size_t standard_bytes = 0;
    std::size_t seeded_bytes = 0;
    std::size_t standard_zlib_bytes = 0;
    std::size_t standard_zstd_bytes = 0;
};

template <typename Ctx>
WireSizes measure(Ctx &ctx) {
    std::vector<double> features(ctx.encoder->slot_count(), 0.42);

    seal::Plaintext pt;
    ctx.encoder->encode(features, ctx.scale, pt);

    seal::Ciphertext ct;
    ctx.encryptor->encrypt(pt, ct);

    WireSizes sizes;
    sizes.standard_bytes = static_cast<std::size_t>(ct.save_size(seal::compr_mode_type::none));
    sizes.standard_zlib_bytes = static_cast<std::size_t>(ct.save_size(seal::compr_mode_type::zlib));
    sizes.standard_zstd_bytes = static_cast<std::size_t>(ct.save_size(seal::compr_mode_type::zstd));

    // §5.7: "seeded" via Serializable<Ciphertext> -- requires the secret key
    // (symmetric-key encryption). ctx.secret_key is available because both
    // CKKSContext and CKKSContext160 are OUT-OF-TCB benchmark-only contexts
    // that generate and hold their own (throwaway) secret key.
    seal::Encryptor sym_encryptor(*ctx.context, ctx.secret_key);
    seal::Serializable<seal::Ciphertext> sct = sym_encryptor.encrypt_symmetric(pt);
    sizes.seeded_bytes = static_cast<std::size_t>(sct.save_size(seal::compr_mode_type::none));

    return sizes;
}

void print_chain(const char *name, const char *coeff_modulus, const WireSizes &s) {
    const double zlib_ratio = static_cast<double>(s.standard_bytes) / static_cast<double>(s.standard_zlib_bytes);
    const double zstd_ratio = static_cast<double>(s.standard_bytes) / static_cast<double>(s.standard_zstd_bytes);
    const double seeded_ratio = static_cast<double>(s.standard_bytes) / static_cast<double>(s.seeded_bytes);

    std::cout << "  \"" << name << "\": {\n";
    std::cout << "    \"coeff_modulus\": \"" << coeff_modulus << "\",\n";
    std::cout << "    \"standard_bytes\": " << s.standard_bytes << ",\n";
    std::cout << "    \"seeded_bytes\": " << s.seeded_bytes << ",\n";
    std::cout << "    \"standard_zlib_bytes\": " << s.standard_zlib_bytes << ",\n";
    std::cout << "    \"standard_zstd_bytes\": " << s.standard_zstd_bytes << ",\n";
    std::cout << "    \"seeded_vs_standard_ratio\": " << seeded_ratio << ",\n";
    std::cout << "    \"zlib_compression_ratio\": " << zlib_ratio << ",\n";
    std::cout << "    \"zstd_compression_ratio\": " << zstd_ratio << "\n";
    std::cout << "  }";
}

}  // namespace

int main() {
    CKKSContext160 ctx160;
    CKKSContext ctx200;

    const WireSizes s160 = measure(ctx160);
    const WireSizes s200 = measure(ctx200);

    std::cout << "{\n";
    print_chain("160bit", "{60,40,60}", s160);
    std::cout << ",\n";
    print_chain("200bit", "{60,40,40,60}", s200);
    std::cout << "\n}\n";

    return 0;
}
