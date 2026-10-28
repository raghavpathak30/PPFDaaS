#include "cnp_codec.h"

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <cmath>
#include <memory>
#include <seal/seal.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

static std::unique_ptr<seal::SEALContext> g_context;
static std::unique_ptr<seal::CKKSEncoder> g_encoder;
static std::unique_ptr<seal::Encryptor> g_encryptor;
static std::unique_ptr<seal::Decryptor> g_decryptor;
static double g_scale = std::pow(2.0, 40);

void init_seal(py::bytes pk_bytes, py::bytes sk_bytes) {
    seal::EncryptionParameters params(seal::scheme_type::ckks);
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60, 40, 60}));

    g_context = std::make_unique<seal::SEALContext>(params);
    g_encoder = std::make_unique<seal::CKKSEncoder>(*g_context);

    std::string pk_s(PyBytes_AS_STRING(pk_bytes.ptr()), PyBytes_Size(pk_bytes.ptr()));
    std::string sk_s(PyBytes_AS_STRING(sk_bytes.ptr()), PyBytes_Size(sk_bytes.ptr()));
    std::stringstream pks(pk_s), sks(sk_s);

    seal::PublicKey pk;
    pk.load(*g_context, pks);
    seal::SecretKey sk;
    sk.load(*g_context, sks);

    g_encryptor = std::make_unique<seal::Encryptor>(*g_context, pk);
    g_decryptor = std::make_unique<seal::Decryptor>(*g_context, sk);
}

py::dict generate_keys_160() {
    seal::EncryptionParameters params(seal::scheme_type::ckks);
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60, 40, 60}));

    auto context = std::make_shared<seal::SEALContext>(params);
    if (!context->parameters_set()) {
        throw std::runtime_error("SEAL rejected 160-bit CKKS parameters");
    }

    seal::KeyGenerator keygen(*context);
    seal::PublicKey public_key;
    seal::GaloisKeys galois_keys;
    keygen.create_public_key(public_key);
    keygen.create_galois_keys(std::vector<int>{1, 2, 4, 8, 16, 32, 64, 128}, galois_keys);

    std::stringstream pk_ss;
    std::stringstream sk_ss;
    std::stringstream gk_ss;
    public_key.save(pk_ss);
    keygen.secret_key().save(sk_ss);
    galois_keys.save(gk_ss);

    py::dict out;
    out["public_key"] = py::bytes(pk_ss.str());
    out["secret_key"] = py::bytes(sk_ss.str());
    out["galois_keys"] = py::bytes(gk_ss.str());
    return out;
}

// CNP-A: keypair WITHOUT Galois keys -- a CNP deployment performs no rotations,
// so none are generated, sent or stored. (Used by the bench to time CNP keygen.)
py::dict generate_keys_160_no_galois() {
    seal::EncryptionParameters params(seal::scheme_type::ckks);
    params.set_poly_modulus_degree(8192);
    params.set_coeff_modulus(seal::CoeffModulus::Create(8192, {60, 40, 60}));

    auto context = std::make_shared<seal::SEALContext>(params);
    if (!context->parameters_set()) {
        throw std::runtime_error("SEAL rejected 160-bit CKKS parameters");
    }

    seal::KeyGenerator keygen(*context);
    seal::PublicKey public_key;
    keygen.create_public_key(public_key);

    std::stringstream pk_ss;
    std::stringstream sk_ss;
    public_key.save(pk_ss);
    keygen.secret_key().save(sk_ss);

    py::dict out;
    out["public_key"] = py::bytes(pk_ss.str());
    out["secret_key"] = py::bytes(sk_ss.str());
    return out;
}

py::bytes encrypt_batch(py::array_t<double, py::array::c_style> features) {
    if (!g_encryptor) {
        throw std::runtime_error("call init_seal() first");
    }

    py::buffer_info buf = features.request();
    if (buf.ndim != 1 || buf.size != 4096) {
        throw std::invalid_argument("encrypt_batch: expected (4096,) float64");
    }

    const double *p = static_cast<const double *>(buf.ptr);
    std::vector<double> data(p, p + 4096);

    seal::Plaintext pt;
    g_encoder->encode(data, g_scale, pt);

    seal::Ciphertext ct;
    g_encryptor->encrypt(pt, ct);

    const std::size_t ct_size = ct.save_size(seal::compr_mode_type::none);
    py::bytes result = py::reinterpret_steal<py::bytes>(
        PyBytes_FromStringAndSize(nullptr, static_cast<Py_ssize_t>(ct_size)));
    if (!result) {
        throw std::runtime_error("PyBytes allocation failed");
    }

    ct.save(reinterpret_cast<seal::seal_byte *>(PyBytes_AS_STRING(result.ptr())),
            ct_size,
            seal::compr_mode_type::none);
    return result;
}

py::array_t<double> decrypt_batch(py::bytes ct_bytes, int n_txns) {
    if (!g_decryptor) {
        throw std::runtime_error("call init_seal() first");
    }
    if (n_txns < 1 || n_txns > 16) {
        throw std::invalid_argument("decrypt_batch: n_txns must be 1-16");
    }

    const char *raw = PyBytes_AS_STRING(ct_bytes.ptr());
    const std::size_t sz = static_cast<std::size_t>(PyBytes_Size(ct_bytes.ptr()));

    seal::Ciphertext ct;
    ct.load(*g_context, reinterpret_cast<const seal::seal_byte *>(raw), sz);

    seal::Plaintext pt;
    g_decryptor->decrypt(ct, pt);

    std::vector<double> decoded;
    g_encoder->decode(pt, decoded);

    auto result = py::array_t<double>(n_txns);
    auto out = result.mutable_unchecked<1>();
    for (int k = 0; k < n_txns; ++k) {
        out(k) = decoded[k * 256];
    }
    return result;
}


// ─── CNP-A (Coefficient-Native Packing) — bank side ────────────────────────
//
// Lane k, feature j -> coefficient 256k+j; the server returns the dot product
// for lane k at coefficient 256k+255 (see vendor_server/include/cnp_codec.h).
// The server adds the bias inside its mask (inference_service_160.cpp), so the
// decoded lane outputs already include it.

py::bytes save_ct(const seal::Ciphertext &ct) {
    const std::size_t ct_size = ct.save_size(seal::compr_mode_type::none);
    py::bytes result = py::reinterpret_steal<py::bytes>(
        PyBytes_FromStringAndSize(nullptr, static_cast<Py_ssize_t>(ct_size)));
    if (!result) throw std::runtime_error("PyBytes allocation failed");
    ct.save(reinterpret_cast<seal::seal_byte *>(PyBytes_AS_STRING(result.ptr())), ct_size,
            seal::compr_mode_type::none);
    return result;
}

seal::Ciphertext load_ct(py::bytes ct_bytes) {
    seal::Ciphertext ct;
    ct.load(*g_context, reinterpret_cast<const seal::seal_byte *>(PyBytes_AS_STRING(ct_bytes.ptr())),
            static_cast<std::size_t>(PyBytes_Size(ct_bytes.ptr())));
    return ct;
}

// (n_lanes, 256) float64, 1 <= n_lanes <= 32 -> ciphertext at first_parms_id.
py::bytes encrypt_cnp_batch(py::array_t<double, py::array::c_style | py::array::forcecast> features) {
    if (!g_encryptor) throw std::runtime_error("call init_seal() first");
    py::buffer_info buf = features.request();
    if (buf.ndim != 2 || buf.shape[1] != 256 || buf.shape[0] < 1 || buf.shape[0] > 32) {
        throw std::invalid_argument("encrypt_cnp_batch: expected (n_lanes<=32, 256) float64");
    }
    const auto poly = cnp::pack_lanes(static_cast<const double *>(buf.ptr), static_cast<std::size_t>(buf.shape[0]));
    seal::Plaintext pt;
    cnp::encode_coeffs(*g_context, poly, g_scale, g_context->first_parms_id(), pt);
    seal::Ciphertext ct;
    g_encryptor->encrypt(pt, ct);
    return save_ct(ct);
}

py::array_t<double> decrypt_cnp_batch(py::bytes ct_bytes, int n_txns) {
    if (!g_decryptor) throw std::runtime_error("call init_seal() first");
    if (n_txns < 1 || n_txns > 32) throw std::invalid_argument("decrypt_cnp_batch: n_txns must be 1-32");
    const seal::Ciphertext ct = load_ct(ct_bytes);
    seal::Plaintext pt;
    g_decryptor->decrypt(ct, pt);
    const auto lanes = cnp::decode_lane_outputs(*g_context, pt, static_cast<std::size_t>(n_txns));
    auto result = py::array_t<double>(n_txns);
    auto out = result.mutable_unchecked<1>();
    for (int k = 0; k < n_txns; ++k) out(k) = lanes[k];
    return result;
}

// ── test helpers (bank side only; the server never decrypts) ──────────────

// Encode -> decode with NO encryption, at the single-limb level (second_parms_id).
py::array_t<double> cnp_encode_decode(py::array_t<double, py::array::c_style | py::array::forcecast> coeffs) {
    if (!g_context) throw std::runtime_error("call init_seal() first");
    py::buffer_info buf = coeffs.request();
    if (buf.ndim != 1 || buf.size != static_cast<py::ssize_t>(cnp::kN)) {
        throw std::invalid_argument("cnp_encode_decode: expected (8192,) float64");
    }
    const double *p = static_cast<const double *>(buf.ptr);
    std::vector<double> v(p, p + cnp::kN);
    const auto second = g_context->first_context_data()->next_context_data()->parms_id();
    seal::Plaintext pt;
    cnp::encode_coeffs(*g_context, v, g_scale, second, pt);
    const auto back = cnp::decode_coeffs(*g_context, pt);
    auto result = py::array_t<double>(cnp::kN);
    std::copy(back.begin(), back.end(), result.mutable_data());
    return result;
}

// Encrypt -> (mod-switch to the single-limb level, no evaluation) -> decrypt -> decode.
py::array_t<double> cnp_encrypt_decrypt(py::array_t<double, py::array::c_style | py::array::forcecast> coeffs) {
    if (!g_encryptor || !g_decryptor) throw std::runtime_error("call init_seal() first");
    py::buffer_info buf = coeffs.request();
    if (buf.ndim != 1 || buf.size != static_cast<py::ssize_t>(cnp::kN)) {
        throw std::invalid_argument("cnp_encrypt_decrypt: expected (8192,) float64");
    }
    const double *p = static_cast<const double *>(buf.ptr);
    std::vector<double> v(p, p + cnp::kN);
    seal::Plaintext pt;
    cnp::encode_coeffs(*g_context, v, g_scale, g_context->first_parms_id(), pt);
    seal::Ciphertext ct;
    g_encryptor->encrypt(pt, ct);
    seal::Evaluator ev(*g_context);
    ev.mod_switch_to_next_inplace(ct);  // public op, no key material; 2 limbs -> 1 so the decoder applies
    seal::Plaintext dec;
    g_decryptor->decrypt(ct, dec);
    const auto back = cnp::decode_coeffs(*g_context, dec);
    auto result = py::array_t<double>(cnp::kN);
    std::copy(back.begin(), back.end(), result.mutable_data());
    return result;
}

// Decrypt a (post-rescale, single-limb) result and return ALL 8192 coefficients, scaled.
py::array_t<double> cnp_decrypt_all(py::bytes ct_bytes) {
    if (!g_decryptor) throw std::runtime_error("call init_seal() first");
    const seal::Ciphertext ct = load_ct(ct_bytes);
    seal::Plaintext pt;
    g_decryptor->decrypt(ct, pt);
    const auto all = cnp::decode_coeffs(*g_context, pt);
    auto result = py::array_t<double>(cnp::kN);
    std::copy(all.begin(), all.end(), result.mutable_data());
    return result;
}

// Same, but the raw residues mod q0 in [0, q0) (for uniformity tests), plus q0 and the scale.
py::tuple cnp_decrypt_raw(py::bytes ct_bytes) {
    if (!g_decryptor) throw std::runtime_error("call init_seal() first");
    const seal::Ciphertext ct = load_ct(ct_bytes);
    seal::Plaintext pt;
    g_decryptor->decrypt(ct, pt);
    std::uint64_t q0 = 0;
    const auto raw = cnp::inverse_ntt_single_limb(*g_context, pt, &q0);
    auto arr = py::array_t<std::uint64_t>(cnp::kN);
    std::copy(raw.begin(), raw.end(), arr.mutable_data());
    return py::make_tuple(arr, q0, pt.scale());
}

// Number of RNS limbs of a serialized ciphertext (1 after rescale).
int cnp_ct_limbs(py::bytes ct_bytes) {
    if (!g_context) throw std::runtime_error("call init_seal() first");
    const seal::Ciphertext ct = load_ct(ct_bytes);
    return static_cast<int>(g_context->get_context_data(ct.parms_id())->parms().coeff_modulus().size());
}

PYBIND11_MODULE(seal_wrapper_160, m) {
    m.doc() = "PPFDaaS SEAL wrapper (160-bit CKKS)";
    m.def("init_seal", &init_seal, py::arg("pk_bytes"), py::arg("sk_bytes"));
    m.def("generate_keys_160", &generate_keys_160);
    m.def("generate_keys_160_no_galois", &generate_keys_160_no_galois);
    m.def("encrypt_batch", &encrypt_batch, py::arg("features"));
    m.def("decrypt_batch", &decrypt_batch, py::arg("ct_bytes"), py::arg("n_txns"));
    // CNP-A
    m.def("encrypt_cnp_batch", &encrypt_cnp_batch, py::arg("features"));
    m.def("decrypt_cnp_batch", &decrypt_cnp_batch, py::arg("ct_bytes"), py::arg("n_txns"));
    m.def("cnp_encode_decode", &cnp_encode_decode, py::arg("coeffs"));
    m.def("cnp_encrypt_decrypt", &cnp_encrypt_decrypt, py::arg("coeffs"));
    m.def("cnp_decrypt_all", &cnp_decrypt_all, py::arg("ct_bytes"));
    m.def("cnp_decrypt_raw", &cnp_decrypt_raw, py::arg("ct_bytes"));
    m.def("cnp_ct_limbs", &cnp_ct_limbs, py::arg("ct_bytes"));
}
