// vendor_server/src/inference_service.cpp  — NORMATIVE
// v1.1 change: t_deserialized captured after ct.load(); deserialization_us set.

#include "inference_service.h"

#include "ckks_context.h"

#include "weight_loader.h"

#include "rotation_hoisting.h"

#include <inference.pb.h>
#include <inference.grpc.pb.h>

#include <chrono>
#include <cstdlib>

#include <iostream>

#include <vector>

#include <grpcpp/grpcpp.h>

using hrc = std::chrono::high_resolution_clock;

using us  = std::chrono::microseconds;

static constexpr std::size_t CT_BUF_SIZE = 420 * 1024;

template<class A, class B>
inline int64_t dur(A a, B b){ return std::chrono::duration_cast<us>(b-a).count(); }

class FraudInferenceServiceImpl final
    : public ppfdaas::FraudInferenceService::Service
{
    CKKSContext     ctx_;
    seal::Plaintext pt_weights_;

    void run_warmup(int rounds = 5)
    {
        std::vector<double> dummy(4096, 0.01);
        seal::Plaintext pt_dummy;
        ctx_.encoder->encode(dummy, ctx_.scale, pt_dummy);
        seal::Ciphertext ct_dummy;
        ctx_.encryptor->encrypt(pt_dummy, ct_dummy);

        for (int i = 0; i < rounds; ++i) {
            seal::Ciphertext ct_work = ct_dummy;
            ctx_.evaluator->multiply_plain_inplace(ct_work, pt_weights_);
            ctx_.evaluator->rescale_to_next_inplace(ct_work);

            seal::Ciphertext acc;
            hoisted_tree_sum(ct_work, ctx_.galois_keys, *ctx_.evaluator, acc, 256);

            static thread_local std::vector<char> tl_ct_buf;
            if (tl_ct_buf.size() < CT_BUF_SIZE) {
                tl_ct_buf.resize(CT_BUF_SIZE);
            }
            const std::size_t sz = acc.save_size(seal::compr_mode_type::none);
            acc.save(
                reinterpret_cast<seal::seal_byte*>(tl_ct_buf.data()),
                sz,
                seal::compr_mode_type::none);
        }
        std::cout << "[Server] Warmup: " << rounds
                  << " full inference rounds complete\n";
        std::cout << "[Server] Warmup complete\n";
    }

public:
    explicit FraudInferenceServiceImpl(const std::string& weights_path)
                : ctx_(), pt_weights_(load_weights_as_plaintext(weights_path, *ctx_.encoder, ctx_.scale))
    {
        run_warmup(5);

        std::cout << "[Server] CKKSContext + Galois keys loaded\n";
    }

    grpc::Status RunInference(
            grpc::ServerContext*,
            const ppfdaas::InferenceRequest* req,
            ppfdaas::InferenceResponse* resp) override
    {
        auto t_start = hrc::now();
        std::cout << "[RunInference] id=" << req->request_id()
                  << " inst=" << req->institution_id() << "\n";

        const auto& ct_bytes = req->ciphertext();
        if (ct_bytes.size() > CT_BUF_SIZE) {
            resp->set_status(ppfdaas::ERR_MALFORMED_CIPHERTEXT);
            resp->set_error_message("Ciphertext exceeds 420 KB max");
            resp->set_request_id(req->request_id());
            return grpc::Status::OK;
        }

        auto pool = seal::MemoryManager::GetPool(seal::mm_prof_opt::mm_force_thread_local);
        seal::Ciphertext ct(pool);
        try {
            ct.load(*ctx_.context,
                reinterpret_cast<const seal::seal_byte*>(ct_bytes.data()),
            ct_bytes.size());
        } catch (const std::exception& e) {
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

        static thread_local seal::Ciphertext tl_acc_buf;
        hoisted_tree_sum(ct, ctx_.galois_keys, *ctx_.evaluator, tl_acc_buf, 256);
        auto t_rot = hrc::now();

        static thread_local std::vector<char> tl_ct_buf;
        if (tl_ct_buf.size() < CT_BUF_SIZE) {
            tl_ct_buf.resize(CT_BUF_SIZE);
        }

        const std::size_t out_size = tl_acc_buf.save_size(seal::compr_mode_type::none);
        tl_acc_buf.save(reinterpret_cast<seal::seal_byte*>(tl_ct_buf.data()),
                out_size, seal::compr_mode_type::none);
        resp->set_result_ciphertext(tl_ct_buf.data(), out_size);
        resp->set_request_id(req->request_id());
        resp->set_status(ppfdaas::InferenceStatus::OK);
        auto t_end = hrc::now();

        auto* td = resp->mutable_timing();
        td->set_deserialization_us(   dur(t_start,        t_deserialized));
        td->set_multiply_plain_us(    dur(t_deserialized, t_mul));
        td->set_rotation_hoisting_us( dur(t_mul,          t_rot));
        td->set_serialization_us(     dur(t_rot,          t_end));
        td->set_total_inference_us(   dur(t_start,        t_end));
        return grpc::Status::OK;
    }
};

int RunVendorServer(const std::string& weights_path, int port)
{
    const std::string address = "0.0.0.0:" + std::to_string(port);
    FraudInferenceServiceImpl service(weights_path);
    const int pool_size = []() {
        const char* e = std::getenv("PPFD_GRPC_THREADS");
        const int v = e ? std::atoi(e) : 4;
        return v > 0 ? v : 4;
    }();

    grpc::ServerBuilder builder;
    builder.AddListeningPort(address, grpc::InsecureServerCredentials());
    builder.RegisterService(&service);
    builder.SetSyncServerOption(grpc::ServerBuilder::SyncServerOption::NUM_CQS, pool_size);
    builder.SetSyncServerOption(grpc::ServerBuilder::SyncServerOption::MIN_POLLERS, pool_size);
    builder.SetSyncServerOption(grpc::ServerBuilder::SyncServerOption::MAX_POLLERS, pool_size);

    std::unique_ptr<grpc::Server> server(builder.BuildAndStart());
    if (!server) {
        std::cerr << "[Server] failed to start on " << address << "\n";
        return 1;
    }

    std::cout << "[Server] listening on " << address << "\n";
    server->Wait();
    return 0;
}
