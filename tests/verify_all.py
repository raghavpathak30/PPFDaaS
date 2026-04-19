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


def step4_depth1_ckks() -> None:
    h = read_text(REPO / "vendor_server" / "include" / "ckks_context.h")
    cpp = read_text(REPO / "vendor_server" / "src" / "ckks_context.cpp")
    must("set_poly_modulus_degree(8192)" in cpp, "depth1 poly degree is 8192", "depth1 poly degree mismatch")
    must("{60,40,40,60}" in cpp, "depth1 coeff modulus matches", "depth1 coeff modulus mismatch")
    must(
        "invariant_noise_budget" in cpp
        or ("decryptor->decrypt" in cpp and "encoder->decode" in cpp),
        "depth1 noise sanity check present",
        "depth1 noise sanity check missing",
    )
    must("CKKSEncoder" in h and "Encryptor" in h and "Decryptor" in h and "Evaluator" in h,
         "SEAL members declared in header",
         "SEAL members missing from ckks_context.h")


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
