#pragma once

#include <inference.pb.h>

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>

// ─── Phase 1, §1.5 — Fail-closed provisioning state machine ────────────────
//
//   PROV_INIT -> PROV_AWAITING_KEYS -> PROV_VALIDATING -> PROV_READY
//                        \                   |                |
//                         \------------> PROV_FAULT <---------/
//
// PROV_FAULT is terminal. No transition out of PROV_FAULT exists in this
// class -- once tripped, the only recovery is a process restart followed by
// full re-provisioning. There is no auto-exit from PROV_FAULT and no
// degraded/substitute-key serving mode at any state.
//
// RunInference (inference_service_160.cpp) MUST check current_state() ==
// PROV_READY before doing any work; every other state (including PROV_FAULT)
// returns ERR_NOT_PROVISIONED.
class ProvisioningStateMachine {
public:
    // The object is constructed only after EvalContext160's constructor has
    // already succeeded (it would otherwise throw and main() would exit(1) --
    // the existing fail-closed startup path). So PROV_INIT -> PROV_AWAITING_KEYS
    // happens unconditionally and immediately.
    ProvisioningStateMachine() : state_(ppfdaas::PROV_AWAITING_KEYS) {}

    ppfdaas::ProvisioningState current_state() const {
        return state_.load(std::memory_order_acquire);
    }

    std::string last_message() const {
        std::lock_guard<std::mutex> lock(msg_mutex_);
        return last_message_;
    }

    // §1.5 rung (a): ProvisionGaloisKeys structural validation outcome.
    // Legal only from PROV_AWAITING_KEYS. On success -> PROV_VALIDATING
    // (awaiting the canary handshake). On failure -> PROV_FAULT.
    bool on_structural_result(bool ok, const std::string &message) {
        std::lock_guard<std::mutex> lock(msg_mutex_);
        last_message_ = message;
        ppfdaas::ProvisioningState expected = ppfdaas::PROV_AWAITING_KEYS;
        if (state_.load(std::memory_order_acquire) != expected) {
            return false;  // not in a state where this transition is legal
        }
        state_.store(ok ? ppfdaas::PROV_VALIDATING : ppfdaas::PROV_FAULT, std::memory_order_release);
        return true;
    }

    // §1.5 rung (b): CanaryConfirm outcome from the bank. Legal only from
    // PROV_VALIDATING. passed=true -> PROV_READY. passed=false -> PROV_FAULT.
    bool on_canary_result(bool passed, const std::string &message) {
        std::lock_guard<std::mutex> lock(msg_mutex_);
        last_message_ = message;
        ppfdaas::ProvisioningState expected = ppfdaas::PROV_VALIDATING;
        if (state_.load(std::memory_order_acquire) != expected) {
            return false;
        }
        state_.store(passed ? ppfdaas::PROV_READY : ppfdaas::PROV_FAULT, std::memory_order_release);
        return true;
    }

    // §1.5 rung (c): continuous validation. Call once per RunInference
    // request with whether the per-request checks (parms_id, ciphertext
    // size/level) passed. Once the cumulative mismatch count while READY
    // exceeds kMaxContinuousMismatches, trips PROV_READY -> PROV_FAULT and
    // returns true (caller should log/alert). All subsequent requests are
    // then rejected via ERR_NOT_PROVISIONED until restart + re-provisioning.
    bool record_request_and_maybe_trip(bool checks_passed, const std::string &reason_if_tripped) {
        ++total_requests_;
        if (checks_passed) {
            return false;
        }
        const uint64_t mismatches = ++mismatch_count_;
        if (mismatches < kMaxContinuousMismatches) {
            return false;
        }
        std::lock_guard<std::mutex> lock(msg_mutex_);
        ppfdaas::ProvisioningState expected = ppfdaas::PROV_READY;
        if (!state_.compare_exchange_strong(expected, ppfdaas::PROV_FAULT, std::memory_order_acq_rel)) {
            return false;  // already FAULT (or not READY) -- nothing to trip
        }
        last_message_ = reason_if_tripped;
        return true;
    }

    // Unconditional escape hatch to PROV_FAULT (e.g. an unexpected exception
    // during provisioning or canary handling). FAULT -> FAULT is a no-op.
    void force_fault(const std::string &message) {
        std::lock_guard<std::mutex> lock(msg_mutex_);
        last_message_ = message;
        state_.store(ppfdaas::PROV_FAULT, std::memory_order_release);
    }

    static constexpr uint64_t kMaxContinuousMismatches = 3;

private:
    std::atomic<ppfdaas::ProvisioningState> state_;
    std::atomic<uint64_t> total_requests_{0};
    std::atomic<uint64_t> mismatch_count_{0};
    mutable std::mutex msg_mutex_;
    std::string last_message_;
};
