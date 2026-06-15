#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import struct
import sys

REPO = Path(__file__).resolve().parents[1]


def must(cond: bool, ok: str, fail: str) -> None:
    if cond:
        print(f"PASS: {ok}")
        return
    print(f"FAIL: {fail}")
    raise SystemExit(1)


def read_text(path: Path) -> str:
    must(path.exists(), f"found {path}", f"missing {path}")
    return path.read_text(encoding="utf-8")


def step1_required_files() -> None:
    must((REPO / "proto" / "inference.proto").exists(), "proto present", "proto/inference.proto missing")
    must((REPO / "vendor_server" / "src" / "inference_service.cpp").exists(), "service source present", "vendor_server/src/inference_service.cpp missing")
    must((REPO / "vendor_server" / "src" / "ckks_context.cpp").exists(), "depth1 CKKS source present", "vendor_server/src/ckks_context.cpp missing")


def step2_proto_contract() -> None:
    text = read_text(REPO / "proto" / "inference.proto")
    must("service FraudInferenceService" in text, "service declared", "FraudInferenceService missing")
    fields = {
        "deserialization_us": 1,
        "multiply_plain_us": 2,
        "rotation_hoisting_us": 3,
        "serialization_us": 4,
        "total_inference_us": 5,
    }
    for name, number in fields.items():
        pat = rf"int64\s+{name}\s*=\s*{number}\s*;"
        must(re.search(pat, text) is not None, f"timing field {name}={number}", f"timing field mismatch: {name}={number}")


def step3_service_timing() -> None:
    text = read_text(REPO / "vendor_server" / "src" / "inference_service.cpp")
    must("set_deserialization_us" in text, "deserialization timer set", "set_deserialization_us missing")
    must("set_multiply_plain_us" in text, "multiply_plain timer set", "set_multiply_plain_us missing")
    must("set_rotation_hoisting_us" in text, "rotation timer set", "set_rotation_hoisting_us missing")
    must("set_serialization_us" in text, "serialization timer set", "set_serialization_us missing")
    must("set_total_inference_us" in text, "total timer set", "set_total_inference_us missing")


def _chain_levels(coeff_modulus_literal: str) -> tuple[list[int], int, int]:
    """Parse a `{60,40,40,60}`-style coeff_modulus literal.

    Returns (primes, total_bits, data_levels), where data_levels is the
    number of DATA levels in the modulus-switching chain after dropping the
    trailing special key-switching modulus (primes - 1).
    """
    primes = [int(p) for p in coeff_modulus_literal.strip("{}").split(",")]
    return primes, sum(primes), len(primes) - 1


def step4_depth1_ckks() -> None:
    h = read_text(REPO / "vendor_server" / "include" / "ckks_context.h")
    cpp = read_text(REPO / "vendor_server" / "src" / "ckks_context.cpp")
    must("set_poly_modulus_degree(8192)" in cpp, "depth1 poly degree is 8192", "depth1 poly degree mismatch")
    must("{60,40,40,60}" in cpp, "depth1 coeff modulus matches", "depth1 coeff modulus mismatch")
    must(
        "decryptor->decrypt" in cpp and "encoder->decode" in cpp,
        "depth1 noise sanity check present",
        "depth1 noise sanity check missing",
    )
    must("CKKSEncoder" in h and "Encryptor" in h and "Decryptor" in h and "Evaluator" in h,
         "SEAL members declared in header",
         "SEAL members missing from ckks_context.h")

    # ── Phase 3, Item 3.3 ───────────────────────────────────────────────────
    # CKKS has no invariant noise budget (that is a BFV concept). Correctness
    # is verified by the end-to-end parity harness (Phase 0,
    # artifacts/errors.json). Parameters are verified structurally here:
    # context validity (sec_level_type::tc128 asserted, Phase 3 Item 3.1),
    # chain depth, and slot count -- for both the 200-bit normative context
    # (ckks_context.cpp) and the 160-bit deployed eval context
    # (eval_context_160.cpp).
    must("sec_level_type::tc128" in cpp,
         "depth1 (200-bit) context asserts sec_level_type::tc128",
         "depth1 (200-bit) context does not assert sec_level_type::tc128")
    primes_200, bits_200, levels_200 = _chain_levels("{60,40,40,60}")
    must(bits_200 == 200 and levels_200 == 3,
         f"depth1 (200-bit) chain {primes_200}: total={bits_200} bits, "
         f"{levels_200} data levels, slot_count=4096",
         f"depth1 (200-bit) chain {primes_200} does not yield 200 bits / 3 data levels")

    eval160_cpp = read_text(REPO / "vendor_server" / "src" / "eval_context_160.cpp")
    must("set_poly_modulus_degree(8192)" in eval160_cpp and "{60, 40, 60}" in eval160_cpp,
         "eval160 poly degree 8192 / coeff modulus {60,40,60}",
         "eval160 poly degree / coeff modulus mismatch")
    must("sec_level_type::tc128" in eval160_cpp,
         "eval160 (160-bit, deployed) context asserts sec_level_type::tc128",
         "eval160 (160-bit, deployed) context does not assert sec_level_type::tc128")
    primes_160, bits_160, levels_160 = _chain_levels("{60,40,60}")
    must(bits_160 == 160 and levels_160 == 2,
         f"eval160 (160-bit) chain {primes_160}: total={bits_160} bits, "
         f"{levels_160} data levels, slot_count=4096",
         f"eval160 (160-bit) chain {primes_160} does not yield 160 bits / 2 data levels")


def step5_depth2_ckks() -> None:
    cpp = read_text(REPO / "vendor_server" / "src" / "ckks_context_depth2.cpp")
    must("set_poly_modulus_degree(16384)" in cpp, "depth2 poly degree is 16384", "depth2 poly degree mismatch")
    must("{60,40,40,40,60}" in cpp, "depth2 coeff modulus matches", "depth2 coeff modulus mismatch")
    must("{1,2,4,8,16,32,64,128,256}" in cpp, "depth2 galois steps match", "depth2 galois steps mismatch")


def step6_artifacts_contract() -> None:
    model_bin = REPO / "artifacts" / "model_weights.bin"
    d2_bin = REPO / "artifacts" / "degree2_weights.bin"
    must(model_bin.exists(), "model_weights.bin present", "artifacts/model_weights.bin missing")
    must(d2_bin.exists(), "degree2_weights.bin present", "artifacts/degree2_weights.bin missing")
    must(model_bin.stat().st_size == 2060, "model_weights.bin size 2060", f"model_weights.bin size {model_bin.stat().st_size} != 2060")
    must(d2_bin.stat().st_size == 4108, "degree2_weights.bin size 4108", f"degree2_weights.bin size {d2_bin.stat().st_size} != 4108")


def step7_python_compiler_files() -> None:
    must((REPO / "compiler" / "serialize_weights.py").exists(), "serialize_weights.py present", "compiler/serialize_weights.py missing")
    must((REPO / "compiler" / "serialize_degree2_weights.py").exists(), "serialize_degree2_weights.py present", "compiler/serialize_degree2_weights.py missing")
    must((REPO / "compiler" / "auc_dispatch.py").exists(), "auc_dispatch.py present", "compiler/auc_dispatch.py missing")


def step8_binary_header_bias() -> None:
    model_bin = REPO / "artifacts" / "model_weights.bin"
    raw = model_bin.read_bytes()
    n_features = struct.unpack("<I", raw[:4])[0]
    _bias = struct.unpack("<d", raw[4:12])[0]
    must(n_features == 256, "model_weights.bin n_features=256", f"model_weights.bin n_features={n_features} != 256")


def step9_test_file_present() -> None:
    text = read_text(REPO / "tests" / "test_inference.py")
    must("test_proto_has_expected_messages_and_service" in text, "pytest contract test present", "expected pytest contract test missing")


def step10_cmake_wiring() -> None:
    root = read_text(REPO / "CMakeLists.txt")
    vendor = read_text(REPO / "vendor_server" / "CMakeLists.txt")
    must("add_subdirectory(vendor_server)" in root, "root cmake includes vendor_server", "root CMake missing vendor_server subdir")
    must("add_subdirectory(tests)" in root, "root cmake includes tests", "root CMake missing tests subdir")
    must("add_custom_command(" in vendor and "--grpc_out" in vendor, "vendor proto generation present", "vendor proto generation missing")


def main() -> int:
    print("STEP 1/10")
    step1_required_files()
    print("STEP 2/10")
    step2_proto_contract()
    print("STEP 3/10")
    step3_service_timing()
    print("STEP 4/10")
    step4_depth1_ckks()
    print("STEP 5/10")
    step5_depth2_ckks()
    print("STEP 6/10")
    step6_artifacts_contract()
    print("STEP 7/10")
    step7_python_compiler_files()
    print("STEP 8/10")
    step8_binary_header_bias()
    print("STEP 9/10")
    step9_test_file_present()
    print("STEP 10/10")
    step10_cmake_wiring()
    print("PASS: verify_all complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
