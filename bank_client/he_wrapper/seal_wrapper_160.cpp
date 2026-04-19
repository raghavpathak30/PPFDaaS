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

PYBIND11_MODULE(seal_wrapper_160, m) {
    m.doc() = "PPFDaaS SEAL wrapper (160-bit CKKS)";
    m.def("init_seal", &init_seal, py::arg("pk_bytes"), py::arg("sk_bytes"));
    m.def("generate_keys_160", &generate_keys_160);
    m.def("encrypt_batch", &encrypt_batch, py::arg("features"));
    m.def("decrypt_batch", &decrypt_batch, py::arg("ct_bytes"), py::arg("n_txns"));
}
