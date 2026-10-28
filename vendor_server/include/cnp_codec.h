#pragma once

// ─── CNP-A: Coefficient-Native Packing codec (SEAL 4.1.2, CKKS, N=8192) ────
//
// Header-only and key-free: it contains NO SecretKey / Decryptor / Encryptor,
// so it is safe to include from the vendor server (out of the secret-key TCB)
// as well as from the bank-side pybind wrapper.
//
// Layout: lane k (0 <= k < 32), feature j (0 <= j < 256) -> coefficient
// 256k + j. The weight polynomial is w_rev(X) = sum_j w_j X^(255-j), so in the
// negacyclic product x(X) * w_rev(X) the coefficient at 256k + 255 is exactly
// lane k's dot product, and no wrap-around term lands on any 256k + 255
// (docs/cnp_gate.md; scripts/cnp_gate.py layout asserts).
//
// A CKKS plaintext here is NOT produced by CKKSEncoder (no canonical
// embedding): each coefficient is written directly as round(scale * v) mod q_i
// per RNS limb, then every limb is forward-NTT'd and the plaintext is tagged
// with its parms_id and scale, exactly as SEAL's own encode_internal finishes.

#include <seal/seal.h>
#include <seal/util/iterator.h>
#include <seal/util/ntt.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace cnp {

constexpr std::size_t kN        = 8192;
constexpr std::size_t kLane     = 256;
constexpr std::size_t kMaxLanes = kN / kLane;  // 32

inline std::size_t output_index(std::size_t lane) { return kLane * lane + (kLane - 1); }

// round(scale * v) as a residue mod q, negatives mapped to q - |r| mod q.
inline std::uint64_t residue_of(double scaled, const seal::Modulus &q) {
    if (!std::isfinite(scaled) || std::fabs(scaled) >= 4.0e18) {
        throw std::invalid_argument("cnp: scaled coefficient out of int64 range / not finite");
    }
    const long long r = std::llround(scaled);
    const std::uint64_t m = q.value();
    const std::uint64_t a = static_cast<std::uint64_t>(r < 0 ? -r : r) % m;
    return (r < 0 && a != 0) ? m - a : a;
}

// Forward NTT every limb in place and tag `pt` as an NTT-form plaintext at
// `pid` with `scale`. `pt` must hold kN * limb_count residues, limb-major, and
// must not yet be tagged (resize() throws on NTT-form plaintexts).
inline void finish_ntt_plaintext(const seal::SEALContext &ctx, seal::parms_id_type pid,
                                 double scale, seal::Plaintext &pt) {
    auto cd = ctx.get_context_data(pid);
    if (!cd) throw std::invalid_argument("cnp: unknown parms_id");
    const auto &mods = cd->parms().coeff_modulus();
    const seal::util::NTTTables *tables = cd->small_ntt_tables();
    for (std::size_t i = 0; i < mods.size(); ++i) {
        seal::util::ntt_negacyclic_harvey(seal::util::CoeffIter(pt.data() + i * kN), tables[i]);
    }
    pt.parms_id() = pid;
    pt.scale() = scale;
}

// Coefficient-encode `values` (length kN) at (`pid`, `scale`).
inline void encode_coeffs(const seal::SEALContext &ctx, const std::vector<double> &values,
                          double scale, seal::parms_id_type pid, seal::Plaintext &pt) {
    if (values.size() != kN) throw std::invalid_argument("cnp::encode_coeffs: need exactly 8192 values");
    auto cd = ctx.get_context_data(pid);
    if (!cd) throw std::invalid_argument("cnp: unknown parms_id");
    const auto &mods = cd->parms().coeff_modulus();
    pt.parms_id() = seal::parms_id_zero;
    pt.resize(kN * mods.size());
    for (std::size_t i = 0; i < mods.size(); ++i) {
        std::uint64_t *limb = pt.data() + i * kN;
        for (std::size_t c = 0; c < kN; ++c) limb[c] = residue_of(scale * values[c], mods[i]);
    }
    finish_ntt_plaintext(ctx, pid, scale, pt);
}

// Weight polynomial: coefficient 255-j holds w_j, everything else zero.
inline std::vector<double> weights_to_wrev(const std::vector<double> &w) {
    if (w.size() != kLane) throw std::invalid_argument("cnp::weights_to_wrev: need 256 weights");
    std::vector<double> poly(kN, 0.0);
    for (std::size_t j = 0; j < kLane; ++j) poly[kLane - 1 - j] = w[j];
    return poly;
}

// Pack up to kMaxLanes rows of 256 features: row k, feature j -> 256k + j.
inline std::vector<double> pack_lanes(const double *rows, std::size_t n_lanes) {
    if (n_lanes < 1 || n_lanes > kMaxLanes) throw std::invalid_argument("cnp::pack_lanes: lanes must be 1..32");
    std::vector<double> poly(kN, 0.0);
    std::copy(rows, rows + n_lanes * kLane, poly.begin());
    return poly;
}

// Inverse-NTT a (copy of a) plaintext into raw residues of its single limb.
// Throws unless exactly ONE RNS limb remains: after multiply_plain + one
// rescale the 160-bit chain {60,40,60} leaves only the 60-bit q0, so a decode
// with more limbs means the circuit's level assumption is broken. No CRT
// reconstruction is implemented on purpose.
inline std::vector<std::uint64_t> inverse_ntt_single_limb(const seal::SEALContext &ctx,
                                                          const seal::Plaintext &pt,
                                                          std::uint64_t *q0_out = nullptr) {
    if (!pt.is_ntt_form()) throw std::invalid_argument("cnp: plaintext is not in NTT form");
    auto cd = ctx.get_context_data(pt.parms_id());
    if (!cd) throw std::invalid_argument("cnp: plaintext parms_id unknown to context");
    const auto &mods = cd->parms().coeff_modulus();
    if (mods.size() != 1) {
        throw std::runtime_error("cnp decode: expected exactly 1 RNS limb after rescale, found " +
                                 std::to_string(mods.size()) + " (CRT decode not implemented)");
    }
    if (pt.coeff_count() != kN) throw std::invalid_argument("cnp: unexpected plaintext coefficient count");
    std::vector<std::uint64_t> raw(pt.data(), pt.data() + kN);  // copy: caller's plaintext untouched
    seal::util::inverse_ntt_negacyclic_harvey(seal::util::CoeffIter(raw.data()), cd->small_ntt_tables()[0]);
    if (q0_out) *q0_out = mods[0].value();
    return raw;
}

// Decode every coefficient: centre mod q0 into (-q0/2, q0/2], divide by scale.
inline std::vector<double> decode_coeffs(const seal::SEALContext &ctx, const seal::Plaintext &pt) {
    std::uint64_t q0 = 0;
    const auto raw = inverse_ntt_single_limb(ctx, pt, &q0);
    std::vector<double> out(kN);
    for (std::size_t c = 0; c < kN; ++c) {
        const std::int64_t centred = raw[c] > q0 / 2 ? static_cast<std::int64_t>(raw[c]) - static_cast<std::int64_t>(q0)
                                                     : static_cast<std::int64_t>(raw[c]);
        out[c] = static_cast<double>(centred) / pt.scale();
    }
    return out;
}

// Per-lane outputs: the values at 256k + 255 for k in [0, n_lanes).
inline std::vector<double> decode_lane_outputs(const seal::SEALContext &ctx, const seal::Plaintext &pt,
                                               std::size_t n_lanes) {
    if (n_lanes < 1 || n_lanes > kMaxLanes) throw std::invalid_argument("cnp: lanes must be 1..32");
    const auto all = decode_coeffs(ctx, pt);
    std::vector<double> out(n_lanes);
    for (std::size_t k = 0; k < n_lanes; ++k) out[k] = all[output_index(k)];
    return out;
}

// Uniform in [0, q) by rejection sampling on 64-bit draws: a draw r is kept
// only if r < 2^64 - (2^64 mod q), so r % q has no modulo bias.
class UniformModQ {
public:
    UniformModQ(std::shared_ptr<seal::UniformRandomGenerator> prng, std::uint64_t q)
        : prng_(std::move(prng)), q_(q),
          reject_from_(std::numeric_limits<std::uint64_t>::max() - ((std::numeric_limits<std::uint64_t>::max() % q + 1) % q)) {}

    std::uint64_t next() {
        for (;;) {
            if (pos_ == buf_.size()) {
                prng_->generate(buf_.size() * sizeof(std::uint64_t), reinterpret_cast<seal::seal_byte *>(buf_.data()));
                pos_ = 0;
            }
            const std::uint64_t r = buf_[pos_++];
            if (r <= reject_from_) return r % q_;
        }
    }

private:
    std::shared_ptr<seal::UniformRandomGenerator> prng_;
    std::uint64_t q_;
    std::uint64_t reject_from_;  // largest accepted draw
    std::vector<std::uint64_t> buf_ = std::vector<std::uint64_t>(2048);
    std::size_t pos_ = 2048;
};

// Build the additive mask plaintext at (`pid`, `scale`): uniform mod q_j on
// every COEFFICIENT-domain index except 256k+255 (k < 32), which carries
// `output_value` (the bias, scaled) -- the same value for all 32 outputs,
// regardless of how many lanes the request filled. The polynomial is built in
// coefficient form and only then forward-NTT'd: a mask drawn uniformly in NTT
// form would be uniform at the output coefficients too.
// `randomize_non_outputs=false` is the test-only no-mask variant (zeros off
// the outputs); it is only compiled into the test server (see inference_service_160.cpp).
inline void build_output_mask(const seal::SEALContext &ctx, seal::parms_id_type pid, double scale,
                              double output_value, bool randomize_non_outputs, seal::Plaintext &pt) {
    auto cd = ctx.get_context_data(pid);
    if (!cd) throw std::invalid_argument("cnp: unknown parms_id");
    const auto &mods = cd->parms().coeff_modulus();
    pt.parms_id() = seal::parms_id_zero;
    pt.resize(kN * mods.size());
    auto prng = seal::UniformRandomGeneratorFactory::DefaultFactory()->create();  // fresh /dev/urandom seed per call
    for (std::size_t i = 0; i < mods.size(); ++i) {
        std::uint64_t *limb = pt.data() + i * kN;
        if (randomize_non_outputs) {
            UniformModQ sampler(prng, mods[i].value());
            for (std::size_t c = 0; c < kN; ++c) limb[c] = sampler.next();
        } else {
            for (std::size_t c = 0; c < kN; ++c) limb[c] = 0;
        }
        const std::uint64_t out_res = residue_of(output_value * scale, mods[i]);
        for (std::size_t k = 0; k < kMaxLanes; ++k) limb[output_index(k)] = out_res;
    }
    finish_ntt_plaintext(ctx, pid, scale, pt);
}

}  // namespace cnp
