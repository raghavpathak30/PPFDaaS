#include "inference_service_160.h"

#include "ckks_context_160.h"
#include "rotation_hoisting.h"
#include "weight_loader.h"

#include <inference.grpc.pb.h>
#include <inference.pb.h>

#include <chrono>
#include <iostream>
#include <memory>
#include <vector>

#include <grpcpp/grpcpp.h>

using hrc = std::chrono::high_resolution_clock;
using us = std::chrono::microseconds;

template <class A, class B>
inline int64_t dur(A a, B b) {
    return std::chrono::duration_cast<us>(b - a).count();
}

class FraudInferenceServiceImpl160 final
    : public ppfdaas::FraudInferenceService::Service {
    CKKSContext160 ctx_;
    seal::Plaintext pt_weights_;
    seal::Ciphertext acc_buf_;
    static constexpr std::size_t CT_BUF = 320 * 1024;
    std::vector<char> ct_out_buf_;

public:
    explicit FraudInferenceServiceImpl160(const std::string &weights_path)
        : ctx_(),
          pt_weights_(load_weights_as_plaintext(weights_path, *ctx_.encoder, ctx_.scale)),
          ct_out_buf_(CT_BUF) {
        std::vector<double> zeros(4096, 0.0);
        seal::Plaintext warmup_pt;
        seal::Ciphertext warmup_ct;
        ctx_.encoder->encode(zeros, ctx_.scale, warmup_pt);
        ctx_.encryptor->encrypt(warmup_pt, warmup_ct);
        hoisted_tree_sum(warmup_ct, ctx_.galois_keys, *ctx_.evaluator, acc_buf_, 256);

        std::cout << "[Server-160] CKKSContext160 + Galois keys loaded\n";
    }

    grpc::Status RunInference(
        grpc::ServerContext *,
        const ppfdaas::InferenceRequest *req,
        ppfdaas::InferenceResponse *resp) override {
        auto t_start = hrc::now();

        const auto &ct_bytes = req->ciphertext();
        if (ct_bytes.size() > CT_BUF) {
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
            resp->set_status(ppfdaas::ERR_MALFORMED_CIPHERTEXT);
            resp->set_error_message(e.what());
            resp->set_request_id(req->request_id());
            return grpc::Status::OK;
        }
        auto t_deserialized = hrc::now();

        if (ct.parms_id() != ctx_.context->first_parms_id()) {
            resp->set_status(ppfdaas::ERR_PARAM_MISMATCH);
            resp->set_error_message("parms_id mismatch: client/server SEAL params differ");
            resp->set_request_id(req->request_id());
            return grpc::Status::OK;
        }

        ctx_.evaluator->multiply_plain_inplace(ct, pt_weights_);
        ctx_.evaluator->rescale_to_next_inplace(ct);
        auto t_mul = hrc::now();

        hoisted_tree_sum(ct, ctx_.galois_keys, *ctx_.evaluator, acc_buf_, 256);
        auto t_rot = hrc::now();

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
        return grpc::Status::OK;
    }
};

int RunVendorServer160(const std::string &weights_path, int port) {
    const std::string address = "0.0.0.0:" + std::to_string(port);
    FraudInferenceServiceImpl160 service(weights_path);

    grpc::ServerBuilder builder;
    builder.AddListeningPort(address, grpc::InsecureServerCredentials());
    builder.SetMaxReceiveMessageSize(384 * 1024);
    builder.SetMaxSendMessageSize(384 * 1024);
    builder.RegisterService(&service);

    std::unique_ptr<grpc::Server> server(builder.BuildAndStart());
    if (!server) {
        std::cerr << "[Server-160] failed to start on " << address << "\n";
        return 1;
    }

    std::cout << "[Server-160] listening on " << address << "\n";
    server->Wait();
    return 0;
}
