#include "inference_service_160.h"

#include "eval_context_160.h"
#include "provisioning_state.h"
#include "rotation_hoisting.h"
#include "weight_loader.h"

#include <inference.grpc.pb.h>
#include <inference.pb.h>

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include <grpcpp/grpcpp.h>

using hrc = std::chrono::high_resolution_clock;
using us = std::chrono::microseconds;

template <class A, class B>
inline int64_t dur(A a, B b) {
    return std::chrono::duration_cast<us>(b - a).count();
}

// ─── Phase 1, §1.1/§1.2/§1.2b/§1.5 ──────────────────────────────────────────
//
// This service no longer holds a secret key, a Decryptor, or a KeyGenerator
// (EvalContext160 -- see eval_context_160.h). Galois keys are NOT read from a
// local file at startup; they arrive ONLY via ProvisionGaloisKeys, are
// structurally validated, and must then pass a canary rotation handshake
// (CanaryCheck / CanaryConfirm) -- run by the bank, which holds the secret
// key -- before RunInference will serve a single request. The provisioning
// state machine (provisioning_state.h) enforces:
//
//   PROV_AWAITING_KEYS -> PROV_VALIDATING -> PROV_READY, with PROV_FAULT
//   reachable from either of the first two on validation failure, and as a
//   terminal trip from PROV_READY if continuous per-request checks fail
//   too often.
//
// RunInference is permitted ONLY in PROV_READY; every other state returns
// ERR_NOT_PROVISIONED.
class FraudInferenceServiceImpl160 final
    : public ppfdaas::FraudInferenceService::Service {
    EvalContext160 ctx_;
    ProvisioningStateMachine sm_;
    double bias_ = 0.0;
    seal::Plaintext pt_weights_;
    seal::Plaintext pt_bias_;
    seal::Ciphertext acc_buf_;
    seal::Ciphertext canary_acc_buf_;
    static constexpr std::size_t CT_BUF = 320 * 1024;
    std::vector<char> ct_out_buf_;
    std::vector<char> canary_out_buf_;

public:
    // SINGLETON: constructed ONCE in RunVendorServer160. EvalContext160's
    // constructor (no keys involved) either succeeds or throws -- if it
    // throws, construction of this service fails, main() catches it and
    // exit(1)s (existing fail-closed startup path). There is therefore no
    // in-process PROV_INIT -> PROV_FAULT transition to model: a context that
    // cannot even be built never reaches a state where it could serve.
    explicit FraudInferenceServiceImpl160(const std::string &weights_path)
        : ctx_(),
          sm_(),
          pt_weights_(load_weights_as_plaintext(weights_path, *ctx_.encoder, ctx_.scale, bias_)),
          ct_out_buf_(CT_BUF),
          canary_out_buf_(CT_BUF) {
        // §1.3: encode the bias once, at construction, at the EXACT scale and
        // parms_id that acc_buf_ will have after
        // multiply_plain_inplace(pt_weights_) + rescale_to_next_inplace. CKKS
        // rescale sets new_scale = old_scale / q_last.value() where q_last is
        // the LAST modulus in the top-level coeff_modulus chain (the one
        // dropped by the first rescale) -- see SEAL's
        // Evaluator::mod_switch_scale_to_next. Both factors going into
        // multiply_plain have scale == ctx_.scale, so old_scale ==
        // ctx_.scale * ctx_.scale exactly. Computing bias_scale the same way
        // here makes add_plain_inplace's scale check pass exactly, with no
        // per-request re-encode.
        // NOTE: ctx_.params.coeff_modulus() is the FULL 3-prime chain as
        // configured ({60,40,60} bits), but rescale_to_next_inplace's
        // mod_switch_scale_to_next divides by
        // context_.get_context_data(encrypted.parms_id())->parms().coeff_modulus().back(),
        // where `encrypted.parms_id()` for a freshly-encrypted ciphertext is
        // first_parms_id() -- whose coeff_modulus() chain is the 2-prime
        // {60,40} DATA chain (the 60-bit "special" key-switching modulus is
        // excluded from first_context_data()). So q_last here is the 40-bit
        // prime (back of the 2-element chain), not the 60-bit prime that
        // would be .back() of the full 3-element ctx_.params chain.
        const double q_last = static_cast<double>(
            ctx_.context->first_context_data()->parms().coeff_modulus().back().value());
        const double bias_scale = (ctx_.scale * ctx_.scale) / q_last;
        std::vector<double> bias_vec(4096, 0.0);
        for (int k = 0; k < 16; ++k) {
            bias_vec[k * 256] = bias_;
        }
        ctx_.encoder->encode(bias_vec, ctx_.second_parms_id, bias_scale, pt_bias_);

        std::cout << "[Server-160] EvalContext160 ready (no secret key, no Galois keys yet); "
                     "state=PROV_AWAITING_KEYS -- awaiting ProvisionGaloisKeys\n";
    }

    // ─── ProvisionGaloisKeys (§1.4 / §1.5 rung a — structural) ─────────────
    grpc::Status ProvisionGaloisKeys(
        grpc::ServerContext *,
        const ppfdaas::ProvisionGaloisKeysRequest *req,
        ppfdaas::ProvisionGaloisKeysResponse *resp) override {
        if (sm_.current_state() != ppfdaas::PROV_AWAITING_KEYS) {
            resp->set_state(sm_.current_state());
            resp->set_message(
                "ProvisionGaloisKeys is only accepted in PROV_AWAITING_KEYS; current state is " +
                ppfdaas::ProvisioningState_Name(sm_.current_state()) +
                ". Re-provisioning requires a server restart.");
            return grpc::Status::OK;
        }

        bool ok = true;
        std::string message;
        try {
            std::istringstream gk_in(req->galois_keys(), std::ios::binary);
            ctx_.load_and_validate_galois_keys(gk_in);
            message = "structural validation passed (parms_id matches; all required Galois "
                      "elements present); awaiting canary handshake";
            std::cout << "[Server-160] ProvisionGaloisKeys: " << message << "\n";
        } catch (const std::exception &e) {
            ok = false;
            message = std::string("structural validation FAILED: ") + e.what();
            std::cerr << "[Server-160] ProvisionGaloisKeys: " << message << "\n";
        }

        sm_.on_structural_result(ok, message);
        resp->set_state(sm_.current_state());
        resp->set_message(message);
        return grpc::Status::OK;
    }

    // ─── CanaryCheck (§1.2b / §1.5 rung b — behavioral) ────────────────────
    //
    // Applies the PRODUCTION rotation schedule (hoisted_tree_sum) to a
    // bank-supplied ciphertext using the just-provisioned Galois keys, and
    // returns the result. The server cannot interpret this result -- it has
    // no secret key. Only the bank can decrypt it and confirm (via
    // CanaryConfirm) whether the Galois keys are consistent with its secret
    // key (Bug B / §1.2b).
    grpc::Status CanaryCheck(
        grpc::ServerContext *,
        const ppfdaas::CanaryRequest *req,
        ppfdaas::CanaryResponse *resp) override {
        if (sm_.current_state() != ppfdaas::PROV_VALIDATING) {
            resp->set_state(sm_.current_state());
            resp->set_message(
                "CanaryCheck is only accepted in PROV_VALIDATING; current state is " +
                ppfdaas::ProvisioningState_Name(sm_.current_state()));
            return grpc::Status::OK;
        }

        const auto &ct_bytes = req->ciphertext();
        if (ct_bytes.size() > CT_BUF) {
            sm_.force_fault("CanaryCheck: ciphertext exceeds " + std::to_string(CT_BUF) + " byte max");
            resp->set_state(sm_.current_state());
            resp->set_message(sm_.last_message());
            return grpc::Status::OK;
        }

        seal::Ciphertext ct;
        try {
            ct.load(*ctx_.context,
                    reinterpret_cast<const seal::seal_byte *>(ct_bytes.data()),
                    ct_bytes.size());
            if (ct.parms_id() != ctx_.context->first_parms_id()) {
                throw std::runtime_error("canary ciphertext parms_id mismatch");
            }

            hoisted_tree_sum(ct, ctx_.galois_keys, *ctx_.evaluator, canary_acc_buf_, 256);

            const std::size_t out_size = canary_acc_buf_.save_size(seal::compr_mode_type::none);
            canary_acc_buf_.save(reinterpret_cast<seal::seal_byte *>(canary_out_buf_.data()),
                                  out_size,
                                  seal::compr_mode_type::none);
            resp->set_result_ciphertext(canary_out_buf_.data(), out_size);
            resp->set_state(sm_.current_state());
            resp->set_message("canary rotation applied; awaiting CanaryConfirm from bank");
        } catch (const std::exception &e) {
            sm_.force_fault(std::string("CanaryCheck failed: ") + e.what());
            resp->set_state(sm_.current_state());
            resp->set_message(sm_.last_message());
        }
        return grpc::Status::OK;
    }

    // ─── CanaryConfirm (§1.2b / §1.5 rung b — behavioral, bank-attested) ───
    grpc::Status CanaryConfirm(
        grpc::ServerContext *,
        const ppfdaas::CanaryConfirmRequest *req,
        ppfdaas::CanaryConfirmResponse *resp) override {
        if (sm_.current_state() != ppfdaas::PROV_VALIDATING) {
            resp->set_state(sm_.current_state());
            resp->set_message(
                "CanaryConfirm is only accepted in PROV_VALIDATING; current state is " +
                ppfdaas::ProvisioningState_Name(sm_.current_state()));
            return grpc::Status::OK;
        }

        const std::string detail = req->passed()
            ? "bank confirmed canary decrypted correctly -- Galois keys are consistent "
              "with the bank's secret key"
            : "bank reported canary decryption MISMATCH (" + req->message() +
              ") -- Galois keys are NOT consistent with the bank's secret key (Bug B)";
        std::cout << "[Server-160] CanaryConfirm: passed=" << (req->passed() ? "true" : "false")
                  << " -- " << detail << "\n";

        sm_.on_canary_result(req->passed(), detail);
        resp->set_state(sm_.current_state());
        resp->set_message(detail);
        if (sm_.current_state() == ppfdaas::PROV_READY) {
            std::cout << "[Server-160] state=PROV_READY -- RunInference is now permitted\n";
        } else {
            std::cerr << "[Server-160] state=PROV_FAULT -- RunInference will be refused until "
                         "restart + re-provisioning\n";
        }
        return grpc::Status::OK;
    }

    // ─── GetProvisioningStatus ──────────────────────────────────────────────
    grpc::Status GetProvisioningStatus(
        grpc::ServerContext *,
        const ppfdaas::ProvisioningStatusRequest *,
        ppfdaas::ProvisioningStatusResponse *resp) override {
        resp->set_state(sm_.current_state());
        resp->set_detail(sm_.last_message());
        return grpc::Status::OK;
    }

    // ─── RunInference (§1.5 rung c — continuous) ───────────────────────────
    grpc::Status RunInference(
        grpc::ServerContext *,
        const ppfdaas::InferenceRequest *req,
        ppfdaas::InferenceResponse *resp) override {
        auto t_start = hrc::now();

        if (sm_.current_state() != ppfdaas::PROV_READY) {
            resp->set_status(ppfdaas::ERR_NOT_PROVISIONED);
            resp->set_error_message(
                "server is not provisioned (state=" +
                ppfdaas::ProvisioningState_Name(sm_.current_state()) +
                "); inference is refused until the provisioning handshake completes");
            resp->set_request_id(req->request_id());
            return grpc::Status::OK;
        }

        const auto &ct_bytes = req->ciphertext();
        if (ct_bytes.size() > CT_BUF) {
            sm_.record_request_and_maybe_trip(false, "RunInference: oversized ciphertext");
            resp->set_status(ppfdaas::ERR_MALFORMED_CIPHERTEXT);
            resp->set_error_message("Ciphertext exceeds 320 KB max");
            resp->set_request_id(req->request_id());
            return grpc::Status::OK;
        }

        auto pool = seal::MemoryManager::GetPool(seal::mm_prof_opt::mm_force_thread_local);
        seal::Ciphertext ct(pool);
        try {
            ct.load(*ctx_.context,
                    reinterpret_cast<const seal::seal_byte *>(ct_bytes.data()),
                    ct_bytes.size());
        } catch (const std::exception &e) {
            sm_.record_request_and_maybe_trip(false, "RunInference: malformed ciphertext");
            resp->set_status(ppfdaas::ERR_MALFORMED_CIPHERTEXT);
            resp->set_error_message(e.what());
            resp->set_request_id(req->request_id());
            return grpc::Status::OK;
        }
        auto t_deserialized = hrc::now();

        // Continuous validation (§1.5 rung c): every request re-checks
        // parms_id and ciphertext size/level. A run of
        // kMaxContinuousMismatches consecutive failures while READY trips
        // PROV_FAULT for all FUTURE requests.
        if (ct.parms_id() != ctx_.context->first_parms_id()) {
            const bool tripped = sm_.record_request_and_maybe_trip(
                false, "PROV_FAULT: repeated parms_id mismatches on RunInference -- "
                       "continuous validation (§1.5 rung c) tripped");
            resp->set_status(ppfdaas::ERR_PARAM_MISMATCH);
            resp->set_error_message("parms_id mismatch: client/server SEAL params differ");
            resp->set_request_id(req->request_id());
            if (tripped) {
                std::cerr << "[Server-160] " << sm_.last_message() << "\n";
            }
            return grpc::Status::OK;
        }
        sm_.record_request_and_maybe_trip(true, "");

        try {
            ctx_.evaluator->multiply_plain_inplace(ct, pt_weights_);
            ctx_.evaluator->rescale_to_next_inplace(ct);
            auto t_mul = hrc::now();

            hoisted_tree_sum(ct, ctx_.galois_keys, *ctx_.evaluator, acc_buf_, 256);
            auto t_rot = hrc::now();

            // §1.3: add the bias term server-side, at the lane-aligned slots
            // (k*256 for k=0..15) where hoisted_tree_sum has placed each
            // transaction's dot-product sum. pt_bias_ is zero everywhere else,
            // so this does not perturb the other 255 slots per lane. This is
            // the ONLY place bias enters the computation -- the bank never
            // sees it.
            ctx_.evaluator->add_plain_inplace(acc_buf_, pt_bias_);

            const std::size_t out_size = acc_buf_.save_size(seal::compr_mode_type::none);
            acc_buf_.save(reinterpret_cast<seal::seal_byte *>(ct_out_buf_.data()),
                          out_size,
                          seal::compr_mode_type::none);
            resp->set_result_ciphertext(ct_out_buf_.data(), out_size);
            resp->set_request_id(req->request_id());
            resp->set_status(ppfdaas::InferenceStatus::OK);
            auto t_end = hrc::now();

            auto *td = resp->mutable_timing();
            td->set_deserialization_us(dur(t_start, t_deserialized));
            td->set_multiply_plain_us(dur(t_deserialized, t_mul));
            td->set_rotation_hoisting_us(dur(t_mul, t_rot));
            td->set_serialization_us(dur(t_rot, t_end));
            td->set_total_inference_us(dur(t_start, t_end));
        } catch (const std::exception &e) {
            std::cerr << "[Server-160] RunInference: internal error: " << e.what() << "\n";
            resp->set_status(ppfdaas::ERR_INTERNAL);
            resp->set_error_message(e.what());
            resp->set_request_id(req->request_id());
        }
        return grpc::Status::OK;
    }
};

namespace {

// Reads a PEM file (cert/key) into a string, or throws if it cannot be read.
std::string ReadPemFile(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("RunVendorServer160: cannot open PEM file '" + path + "'");
    }
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

// §1.7 — mutually authenticated TLS. If PPFD_TLS_CERT / PPFD_TLS_KEY /
// PPFD_TLS_CA are all set, the server presents (PPFD_TLS_CERT, PPFD_TLS_KEY)
// as its identity and requires + verifies a client certificate signed by
// PPFD_TLS_CA (grpc::SslServerCredentialsOptions with
// force_client_auth = true). If none are set, the server falls back to
// InsecureServerCredentials() with a loud warning -- see docs/spec.md's
// threat model for the honest-but-curious / authenticated-transport
// assumption this is meant to satisfy.
std::shared_ptr<grpc::ServerCredentials> BuildServerCredentials() {
    const char *cert_path = std::getenv("PPFD_TLS_CERT");
    const char *key_path  = std::getenv("PPFD_TLS_KEY");
    const char *ca_path   = std::getenv("PPFD_TLS_CA");

    if (!cert_path && !key_path && !ca_path) {
        std::cerr << "[Server-160] WARNING: PPFD_TLS_CERT/KEY/CA not set -- listening with "
                     "InsecureServerCredentials() (no transport authentication or "
                     "confidentiality). See docs/spec.md §Threat Model: this is acceptable "
                     "ONLY for local dev/demo. Run scripts/generate_dev_certs.sh and set "
                     "PPFD_TLS_CERT/PPFD_TLS_KEY/PPFD_TLS_CA for mutually authenticated TLS.\n";
        return grpc::InsecureServerCredentials();
    }
    if (!cert_path || !key_path || !ca_path) {
        throw std::runtime_error(
            "RunVendorServer160: PPFD_TLS_CERT, PPFD_TLS_KEY, and PPFD_TLS_CA must all be set "
            "together (or all left unset for insecure dev mode)");
    }

    grpc::SslServerCredentialsOptions ssl_opts(
        GRPC_SSL_REQUEST_AND_REQUIRE_CLIENT_CERTIFICATE_AND_VERIFY);
    ssl_opts.pem_root_certs = ReadPemFile(ca_path);
    ssl_opts.pem_key_cert_pairs.push_back(
        grpc::SslServerCredentialsOptions::PemKeyCertPair{
            ReadPemFile(key_path), ReadPemFile(cert_path)});

    std::cout << "[Server-160] mTLS enabled: cert=" << cert_path << " ca=" << ca_path << "\n";
    return grpc::SslServerCredentials(ssl_opts);
}

}  // namespace

int RunVendorServer160(const std::string &weights_path, int port) {
    const std::string address = "0.0.0.0:" + std::to_string(port);
    FraudInferenceServiceImpl160 service(weights_path);

    grpc::ServerBuilder builder;
    builder.AddListeningPort(address, BuildServerCredentials());
    // §1.4: ProvisionGaloisKeys carries a serialized seal::GaloisKeys (~5.8 MB
    // for this 160-bit parameter set / 8 rotation steps), well over the
    // 384 KB needed for inference ciphertexts alone.
    builder.SetMaxReceiveMessageSize(8 * 1024 * 1024);
    builder.SetMaxSendMessageSize(8 * 1024 * 1024);
    builder.RegisterService(&service);

    std::unique_ptr<grpc::Server> server(builder.BuildAndStart());
    if (!server) {
        std::cerr << "[Server-160] failed to start on " << address << "\n";
        return 1;
    }

    std::cout << "[Server-160] listening on " << address << " (state=PROV_AWAITING_KEYS)\n";
    server->Wait();
    return 0;
}
