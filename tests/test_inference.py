from pathlib import Path
import importlib
import json
import pathlib
import re
import socket
import struct
import subprocess
import sys
import time
import uuid

import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTO_FILE = REPO_ROOT / "proto" / "inference.proto"
SERVICE_CPP = REPO_ROOT / "vendor_server" / "src" / "inference_service.cpp"

# Make `bank_client`, `generated`, etc. importable regardless of how this
# file is invoked (pytest from repo root, or `python3 tests/test_inference.py`).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_BANK_CLIENT_AVAILABLE = importlib.util.find_spec("bank_client") is not None
_ARTIFACTS_AVAILABLE = any(
    pathlib.Path(p).exists()
    for p in [
        "artifacts/weights.npy", "artifacts/model_weights.bin",
        "vendor_server/artifacts/weights.npy",
        "vendor_server/artifacts/model_weights.bin",
    ]
)
_TEST_SET_AVAILABLE = (REPO_ROOT / "artifacts" / "X_test.npy").exists() and \
    (REPO_ROOT / "artifacts" / "y_test.npy").exists()
_RUNTIME_READY = _BANK_CLIENT_AVAILABLE and _ARTIFACTS_AVAILABLE and _TEST_SET_AVAILABLE


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
    assert "rpc RunInference (InferenceRequest) returns (InferenceResponse);" in text


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
    # value-member declarations live in the header, not the .cpp
    src_h = _read(REPO_ROOT / "vendor_server" / "include" / "ckks_context.h")
    # encoder/encryptor are std::optional<seal::...> value members (constructed
    # in-place via emplace() once the context is known), not unique_ptr/raw
    # pointers. std::optional<T> still stores T inline (no heap indirection),
    # so this satisfies the "value member, not unique_ptr" requirement.
    assert "std::optional<seal::CKKSEncoder> encoder" in src_h or \
           "seal::CKKSEncoder encoder" in src_h or \
           "CKKSEncoder encoder" in src_h, \
        'FAIL: encoder must be a value member (std::optional<seal::CKKSEncoder> or plain value), not unique_ptr'
    assert "std::optional<seal::Encryptor> encryptor" in src_h or \
           "seal::Encryptor encryptor" in src_h or \
           "Encryptor encryptor" in src_h, \
        'FAIL: encryptor must be a value member (std::optional<seal::Encryptor> or plain value)'
    assert "unique_ptr" not in src_h, \
        'FAIL: unique_ptr found in header — all SEAL objects must be value members'

    # noise-budget check and SEAL params stay in the .cpp
    src_cpp = _read(SERVICE_CPP)
    assert "invariant_noise_budget" in src_cpp, \
        'FAIL: sanity check must use invariant_noise_budget()'
    assert "{60,40,40,60}" in src_cpp, \
        'FAIL: coeff_modulus must be {60,40,40,60}'
    assert "8192" in src_cpp, \
        'FAIL: poly_modulus_degree must be 8192'
    assert "{1,2,4,8,16,32,64,128}" in src_cpp, \
        'FAIL: Galois key set must be {1,2,4,8,16,32,64,128}'
    print('PASS: ckks_context — value members in header, correct params in .cpp')

    src2 = _read(REPO_ROOT / "vendor_server" / "src" / "ckks_context_depth2.cpp")
    assert '16384' in src2, 'FAIL: must use n=16384'
    assert '{60,40,40,40,60}' in src2, 'FAIL: five-prime modulus required'
    assert '{1,2,4,8,16,32,64,128,256}' in src2, \
        'FAIL: nine Galois keys required'
    print('PASS: ckks_context_depth2.cpp')

    # hoisted_tree_sum(ct,
    # set_deserialization_us(


def test_service_uses_direct_pointer_serialization_path_only():
    text = _read(SERVICE_CPP)

    assert "reinterpret_cast<const seal::seal_byte*>(ct_bytes.data())" in text
    assert "reinterpret_cast<seal::seal_byte*>(ct_out_buf_.data())" in text

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


def _resolve_test_set() -> tuple[Path, Path]:
    x_candidates = [
        REPO_ROOT / "artifacts" / "X_test.npy",
        REPO_ROOT / "vendor_server" / "artifacts" / "X_test.npy",
        REPO_ROOT / "compiler" / "artifacts" / "X_test.npy",
    ]
    y_candidates = [
        REPO_ROOT / "artifacts" / "y_test.npy",
        REPO_ROOT / "vendor_server" / "artifacts" / "y_test.npy",
        REPO_ROOT / "compiler" / "artifacts" / "y_test.npy",
    ]

    x_test = next((p for p in x_candidates if p.exists()), None)
    y_test = next((p for p in y_candidates if p.exists()), None)

    assert x_test is not None, (
        "X_test.npy not found. Checked: " + ", ".join(str(p) for p in x_candidates)
    )
    assert y_test is not None, (
        "y_test.npy not found. Checked: " + ", ".join(str(p) for p in y_candidates)
    )
    return x_test, y_test


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


# ─── Phase 0.3: end-to-end HE-vs-plaintext logit parity harness ───────────
#
# The previous `run_runtime_validation` compared a PLAINTEXT LOGIT
# (`X @ w + b`) against an ENCRYPTED-PATH SIGMOID PROBABILITY
# (`fraud_probabilities[0] = expit(decrypted_raw + bias)`) -- a
# dimensionally-incoherent comparison that asserted nothing meaningful.
#
# This harness instead compares LOGIT vs LOGIT:
#   plaintext_logit  = X @ w + b
#   encrypted_logit  = decrypt(raw_score) + b     (raw_score = HE dot product)
# over the FULL held-out test set, reports error statistics, writes
# `artifacts/errors.json`, and computes ROC-AUC / PR-AUC from the
# encrypted-path logits -- the first real accuracy number for the
# encrypted path.

_VENDOR_160 = {
    "address": "127.0.0.1:50052",
    "binary": REPO_ROOT / "vendor_server" / "build" / "vendor_server_160",
    "weights_path": REPO_ROOT / "artifacts" / "model_weights.bin",
    "public_key_path": REPO_ROOT / "artifacts" / "public_key_160.bin",
    "secret_key_path": REPO_ROOT / "artifacts" / "secret_key_160.bin",
    "galois_keys_path": REPO_ROOT / "artifacts" / "galois_keys_160.bin",
    "wrapper_module": "seal_wrapper_160",
    # §1.4: must be large enough for galois_keys_160.bin (~5.8 MB), pushed
    # once via ProvisionGaloisKeys.
    "grpc_max_message_length": 8 * 1024 * 1024,
}

ERRORS_JSON_PATH = REPO_ROOT / "artifacts" / "errors.json"

BATCH_SIZE = 16  # protocol packs 16 transactions x 256 features = 4096 slots


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _launch_server(cfg: dict) -> subprocess.Popen:
    host, port = cfg["address"].split(":")
    proc = subprocess.Popen(
        [str(cfg["binary"]), str(cfg["weights_path"]), port],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _is_port_open(host, int(port)):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(
                f"server process exited early (code={proc.returncode}); "
                f"binary={cfg['binary']}"
            )
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"server did not open {cfg['address']} in time")


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_runtime_validation(max_samples: int | None = None) -> dict:
    """Phase 0.3 end-to-end logit-vs-logit parity + accuracy report.

    Returns the report dict (also written to `artifacts/errors.json`).
    """
    from generated import inference_pb2
    from sklearn.metrics import average_precision_score, roc_auc_score

    BankClient = _load_bank_client_class()
    cfg = _VENDOR_160

    weights_npy, model_bin = _resolve_runtime_paths()
    weights = np.load(weights_npy)
    bias = _load_bias_from_model_weights_bin(model_bin)
    assert weights.ndim == 1 and weights.shape[0] == 256, (
        f"weights.npy must be shape (256,), got {weights.shape}"
    )

    x_test_path, y_test_path = _resolve_test_set()
    X_test = np.load(x_test_path).astype(np.float64)
    y_test = np.load(y_test_path)
    assert X_test.ndim == 2 and X_test.shape[1] == 256, (
        f"X_test.npy must be shape (N, 256), got {X_test.shape}"
    )
    if max_samples is not None:
        X_test = X_test[:max_samples]
        y_test = y_test[:max_samples]
    n_samples = X_test.shape[0]

    # Plaintext reference logits: X @ w + b
    plain_logits = X_test @ weights + bias

    host, port_s = cfg["address"].split(":")
    port = int(port_s)
    own_server = None
    if not _is_port_open(host, port):
        own_server = _launch_server(cfg)

    try:
        client = BankClient(
            cfg["address"],
            public_key_path=str(cfg["public_key_path"]),
            secret_key_path=str(cfg["secret_key_path"]),
            wrapper_module=cfg["wrapper_module"],
            grpc_max_message_length=cfg["grpc_max_message_length"],
            galois_keys_path=str(cfg["galois_keys_path"]),
        )

        # Encrypted-path logits: decrypt(raw_score) + b
        encrypted_logits = np.empty(n_samples, dtype=np.float64)
        n_batches = (n_samples + BATCH_SIZE - 1) // BATCH_SIZE
        pad_buffer = np.zeros((BATCH_SIZE, 256), dtype=np.float64)

        for b in range(n_batches):
            lo = b * BATCH_SIZE
            hi = min(lo + BATCH_SIZE, n_samples)
            chunk = X_test[lo:hi]
            if chunk.shape[0] < BATCH_SIZE:
                buf = pad_buffer.copy()
                buf[: chunk.shape[0]] = chunk
            else:
                buf = chunk

            ct_bytes = client._wrapper.encrypt_batch(
                np.ascontiguousarray(buf.ravel(), dtype=np.float64))
            req = inference_pb2.InferenceRequest(
                ciphertext=ct_bytes,
                request_id=str(uuid.uuid4()),
                institution_id="PARITY_HARNESS",
                n_transactions=BATCH_SIZE,
            )
            resp = client._stub.RunInference(req, timeout=5.0)
            if resp.status != inference_pb2.InferenceStatus.OK:
                raise RuntimeError(f"Vendor error {resp.status}: {resp.error_message}")

            raw = client._wrapper.decrypt_batch(resp.result_ciphertext, BATCH_SIZE)
            # §1.3: vendor_server_160 now applies the bias term server-side
            # (inference_service_160.cpp, add_plain_inplace(acc_buf_, pt_bias_)),
            # so `raw` already includes it -- do not add `bias` again here.
            encrypted_logits[lo:hi] = raw[: hi - lo]
    finally:
        if own_server is not None:
            _stop_server(own_server)

    abs_err = np.abs(plain_logits - encrypted_logits)

    report = {
        "n_samples": int(n_samples),
        "max_abs_error": float(np.max(abs_err)),
        "mean_abs_error": float(np.mean(abs_err)),
        "median_abs_error": float(np.median(abs_err)),
        "abs_error_distribution": {
            "p50": float(np.percentile(abs_err, 50)),
            "p90": float(np.percentile(abs_err, 90)),
            "p99": float(np.percentile(abs_err, 99)),
            "p99.9": float(np.percentile(abs_err, 99.9)),
            "min": float(np.min(abs_err)),
            "max": float(np.max(abs_err)),
        },
        "roc_auc_encrypted": float(roc_auc_score(y_test, encrypted_logits)),
        "pr_auc_encrypted": float(average_precision_score(y_test, encrypted_logits)),
    }

    ERRORS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ERRORS_JSON_PATH, "w") as f:
        json.dump(report, f, indent=2)

    print(
        "encrypted_path_report "
        f"n_samples={report['n_samples']} "
        f"MAE={report['mean_abs_error']:.6e} "
        f"MedianAE={report['median_abs_error']:.6e} "
        f"MaxAE={report['max_abs_error']:.6e} "
        f"ROC-AUC={report['roc_auc_encrypted']:.6f} "
        f"PR-AUC={report['pr_auc_encrypted']:.6f}"
    )
    print(f"errors.json written to {ERRORS_JSON_PATH}")

    return report


@pytest.mark.skipif(
    not _RUNTIME_READY,
    reason="Runtime deps absent: BankClient not yet implemented / artifacts not generated",
)
def test_runtime_validation():
    # Small slice for fast CI; the full held-out test set is run via __main__.
    report = run_runtime_validation(max_samples=160)
    assert report["max_abs_error"] < 1e-3, (
        f"encrypted-path logit error too large: {report['max_abs_error']}"
    )


if __name__ == "__main__":
    run_runtime_validation()
