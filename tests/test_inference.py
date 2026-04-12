from pathlib import Path
import importlib
import pathlib
import re
import struct

import numpy as np
import pytest


_BANK_CLIENT_AVAILABLE = importlib.util.find_spec("bank_client") is not None
_ARTIFACTS_AVAILABLE = any(
    pathlib.Path(p).exists()
    for p in [
        "artifacts/weights.npy", "artifacts/model_weights.bin",
        "vendor_server/artifacts/weights.npy",
        "vendor_server/artifacts/model_weights.bin",
    ]
)
_RUNTIME_READY = _BANK_CLIENT_AVAILABLE and _ARTIFACTS_AVAILABLE


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTO_FILE = REPO_ROOT / "proto" / "inference.proto"
SERVICE_CPP = REPO_ROOT / "vendor_server" / "src" / "inference_service.cpp"


def _read(path: Path) -> str:
    assert path.exists(), f"Missing required file: {path}"
    return path.read_text(encoding="utf-8")


def test_proto_has_expected_messages_and_service():
    text = _read(PROTO_FILE)

    assert "syntax = \"proto3\";" in text
    assert "package ppfdaas;" in text

    assert "message InferenceRequest" in text
    assert "message InferenceResponse" in text
    assert "message TimingBreakdown" in text

    assert "service FraudInferenceService" in text
    assert "rpc RunInference(InferenceRequest) returns (InferenceResponse);" in text


def test_proto_timing_breakdown_field_numbers_are_stable():
    text = _read(PROTO_FILE)

    expected = {
        "deserialization_us": 1,
        "multiply_plain_us": 2,
        "rotation_hoisting_us": 3,
        "serialization_us": 4,
        "total_inference_us": 5,
    }

    for field, number in expected.items():
        pattern = rf"int64\s+{field}\s*=\s*{number}\s*;"
        assert re.search(pattern, text), f"Expected field '{field} = {number}' not found"


def test_proto_request_response_contract_field_numbers_are_stable():
    text = _read(PROTO_FILE)

    request_expected = {
        "ciphertext": 1,
        "request_id": 2,
        "institution_id": 3,
        "n_transactions": 4,
    }
    for field, number in request_expected.items():
        pattern = rf"\b{field}\b\s*=\s*{number}\s*;"
        assert re.search(pattern, text), f"InferenceRequest field mismatch: {field}={number}"

    response_expected = {
        "status": 1,
        "result_ciphertext": 2,
        "request_id": 3,
        "error_message": 4,
        "timing": 5,
    }
    for field, number in response_expected.items():
        pattern = rf"\b{field}\b\s*=\s*{number}\s*;"
        assert re.search(pattern, text), f"InferenceResponse field mismatch: {field}={number}"


def test_service_uses_spec_timing_boundaries_and_debug_invariant():
    text = _read(SERVICE_CPP)

    # Required timer checkpoints in logical order
    order = [
        "const auto t_start = hrc::now();",
        "ct.load(",
        "const auto t_deserialized = hrc::now();",
        "ctx_.evaluator->multiply_plain_inplace(ct, pt_weights_);",
        "ctx_.evaluator->rescale_to_next_inplace(ct);",
        "const auto t_mul = hrc::now();",
        "hoisted_tree_sum_inplace(",
        "const auto t_rot = hrc::now();",
        "acc.save(",
        "const auto t_end = hrc::now();",
    ]

    pos = -1
    for token in order:
        idx = text.find(token)
        assert idx != -1, f"Missing required timing token: {token}"
        assert idx > pos, f"Timing token out of order: {token}"
        pos = idx

    # DEBUG-only residual check must exist and stay strict.
    assert "#ifndef NDEBUG" in text
    assert "const int64_t residual = std::abs(sum - td->total_inference_us());" in text
    assert "assert(residual < 300);" in text


def test_service_uses_direct_pointer_serialization_path_only():
    text = _read(SERVICE_CPP)

    assert "reinterpret_cast<const seal::seal_byte*>(ct_bytes.data())" in text
    assert "reinterpret_cast<seal::seal_byte*>(serialized_out_buf_.data())" in text

    # Explicitly guard against accidental high-overhead paths.
    assert "stringstream" not in text


def _load_bias_from_model_weights_bin(model_weights_bin: Path) -> float:
    raw = model_weights_bin.read_bytes()
    assert len(raw) >= 12, f"model_weights.bin too small: {model_weights_bin}"
    # Layout from spec: uint32 n_features at offset 0, float64 bias at offset 4.
    return struct.unpack("<d", raw[4:12])[0]


def _resolve_runtime_paths() -> tuple[Path, Path]:
    weights_npy_candidates = [
        REPO_ROOT / "artifacts" / "weights.npy",
        REPO_ROOT / "vendor_server" / "artifacts" / "weights.npy",
        REPO_ROOT / "compiler" / "artifacts" / "weights.npy",
    ]
    model_bin_candidates = [
        REPO_ROOT / "artifacts" / "model_weights.bin",
        REPO_ROOT / "vendor_server" / "artifacts" / "model_weights.bin",
        REPO_ROOT / "compiler" / "artifacts" / "model_weights.bin",
    ]

    weights_npy = next((p for p in weights_npy_candidates if p.exists()), None)
    model_bin = next((p for p in model_bin_candidates if p.exists()), None)

    assert weights_npy is not None, (
        "weights.npy not found. Checked: "
        + ", ".join(str(p) for p in weights_npy_candidates)
    )
    assert model_bin is not None, (
        "model_weights.bin not found. Checked: "
        + ", ".join(str(p) for p in model_bin_candidates)
    )
    return weights_npy, model_bin


def _load_bank_client_class():
    module_candidates = [
        "bank_client.backend.bank_client",
        "bank_client.bank_client",
        "bank_client",
    ]
    for module_name in module_candidates:
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, "BankClient"):
                return module.BankClient
        except ModuleNotFoundError:
            continue
    raise ImportError(
        "BankClient class not importable. Tried: " + ", ".join(module_candidates)
    )


def run_runtime_validation():
    # 1) Create client
    BankClient = _load_bank_client_class()
    client = BankClient("127.0.0.1:50051")

    # 2) Generate synthetic input X shape (1, 256), float64
    rng = np.random.default_rng(12345)
    X = rng.normal(size=(1, 256)).astype(np.float64)

    # 5) Load weights.npy and bias from model_weights.bin
    weights_npy, model_bin = _resolve_runtime_paths()
    weights = np.load(weights_npy)
    bias = _load_bias_from_model_weights_bin(model_bin)

    assert weights.ndim == 1 and weights.shape[0] == 256, (
        f"weights.npy must be shape (256,), got {weights.shape}"
    )

    # 3) Call inference
    result = client.run_inference(X)

    # 4) Print latency and timing fields
    latency_ms = float(result["latency_ms"])
    timing = result["timing_breakdown"]
    deser = int(timing["deserialization_us"])
    mul = int(timing["multiply_plain_us"])
    rot = int(timing["rotation_hoisting_us"])
    ser = int(timing["serialization_us"])
    total = int(timing["total_inference_us"])

    print(f"latency_ms={latency_ms:.3f}")
    print(
        "timing_us "
        f"deserialization={deser} multiply_plain={mul} "
        f"rotation_hoisting={rot} serialization={ser} total={total}"
    )

    # 6) Plaintext reference score
    plain = float(X[0] @ weights + bias)

    # 7) HE output score
    he_score = float(result["fraud_probabilities"][0])

    # 8-10) Error comparison and threshold
    error = abs(plain - he_score)
    print(f"plain={plain:.12f} he_score={he_score:.12f} error={error:.12e}")
    assert error < 1e-3, f"HE/plain mismatch too high: {error}"

    # 11) Timing invariant
    sum_parts = deser + mul + rot + ser
    residual = abs(sum_parts - total)
    assert residual < 300, f"Timing residual too large: {residual} us"

    # 12) Multi-run summary (20 runs)
    latencies = [latency_ms]
    for _ in range(19):
        Xr = rng.normal(size=(1, 256)).astype(np.float64)
        rr = client.run_inference(Xr)
        latencies.append(float(rr["latency_ms"]))

    arr = np.asarray(latencies, dtype=np.float64)
    mean_ms = float(np.mean(arr))
    std_ms = float(np.std(arr))
    min_ms = float(np.min(arr))
    max_ms = float(np.max(arr))

    # 13) Print summary
    print(
        "runtime_summary "
        f"runs={len(latencies)} mean_ms={mean_ms:.3f} std_ms={std_ms:.3f} "
        f"min_ms={min_ms:.3f} max_ms={max_ms:.3f}"
    )


@pytest.mark.skipif(
    not _RUNTIME_READY,
    reason="Runtime deps absent: BankClient not yet implemented / artifacts not generated",
)
def test_runtime_validation():
    run_runtime_validation()


if __name__ == "__main__":
    run_runtime_validation()
