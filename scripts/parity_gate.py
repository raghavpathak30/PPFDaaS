#!/usr/bin/env python3
"""§5.5: in-band parity gate.

Formalizes the "verify the encrypted path against a plaintext oracle BEFORE
trusting any timing number" pattern already used ad hoc by
vendor_server/src/benchmark_160.cpp's correctness gate and
tests/test_inference.py's runtime validation.

Usage (callers, e.g. tests/benchmark_comparison.py /
tests/benchmark_throughput.py):

    weights, bias = load_model_weights()
    resp = client.run_inference(x_sample)          # NOT timed
    passed, max_abs_error = verify_encrypted_output(
        resp["fraud_probabilities"], x_sample, weights, bias)
    if not passed:
        raise RuntimeError(f"parity gate FAILED: max_abs_error={max_abs_error}")
    # ... only now start the timed warmup/measurement loop ...

The verification call's timing MUST be discarded -- it is not a sample of the
steady-state latency distribution (first request may include cold-cache
effects, and its purpose is correctness, not timing).
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from scipy.special import expit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_WEIGHTS_PATH = REPO_ROOT / "artifacts" / "model_weights.bin"
DEFAULT_TOLERANCE = 1e-4


def load_model_weights(path: Path | str | None = None) -> tuple[np.ndarray, float]:
    """Load (weights[256], bias) from model_weights.bin.

    Format (2060 bytes, little-endian): u32 n_features, f64 bias, 256 x f64
    weights -- see scripts/generate_roc.py::_load_depth1_weights.
    """
    p = Path(path) if path is not None else DEFAULT_MODEL_WEIGHTS_PATH
    raw = p.read_bytes()
    if len(raw) != 2060:
        raise RuntimeError(f"{p}: model_weights.bin must be exactly 2060 bytes, got {len(raw)}")

    n_features = struct.unpack_from("<I", raw, 0)[0]
    if n_features != 256:
        raise RuntimeError(f"{p}: expected n_features=256, got {n_features}")

    bias = struct.unpack_from("<d", raw, 4)[0]
    weights = np.array(struct.unpack_from("<256d", raw, 12), dtype=np.float64)
    return weights, float(bias)


def verify_encrypted_output(
    fraud_probabilities: np.ndarray,
    feature_vector: np.ndarray,
    weights: np.ndarray,
    bias: float,
    tol: float = DEFAULT_TOLERANCE,
) -> tuple[bool, float]:
    """Compare the encrypted path's decrypted+sigmoid output against the
    plaintext logistic-regression oracle for the SAME inputs.

    feature_vector: shape (n_txns, 256) or (256,) for a single transaction.
    fraud_probabilities: shape (n_txns,) (or broadcastable to it), one
    probability per transaction, aligned 1:1 with feature_vector's rows.

    Returns (passed, max_abs_error) where passed = max_abs_error < tol.
    """
    fv = np.asarray(feature_vector, dtype=np.float64)
    if fv.ndim == 1:
        fv = fv.reshape(1, -1)

    probs = np.asarray(fraud_probabilities, dtype=np.float64).reshape(-1)
    if fv.shape[0] != probs.shape[0]:
        raise ValueError(
            f"feature_vector has {fv.shape[0]} row(s) but fraud_probabilities "
            f"has {probs.shape[0]} entrie(s)"
        )
    if fv.shape[1] != weights.shape[0]:
        raise ValueError(
            f"feature_vector has {fv.shape[1]} column(s) but weights has "
            f"{weights.shape[0]} entrie(s)"
        )

    expected_logits = fv @ weights + bias
    expected_probs = expit(expected_logits)

    max_abs_error = float(np.max(np.abs(probs - expected_probs)))
    return max_abs_error < tol, max_abs_error


def _self_test() -> int:
    """Standalone smoke test: run one inference against a live 160-bit
    vendor server and check it against the plaintext oracle. Requires
    vendor_server_160 already running (default 127.0.0.1:50052) and the
    usual artifacts/ key material."""
    import sys

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from bank_client.bank_client import BankClient

    artifacts = REPO_ROOT / "artifacts"
    client = BankClient(
        "127.0.0.1:50052",
        public_key_path=str(artifacts / "public_key_160.bin"),
        secret_key_path=str(artifacts / "secret_key_160.bin"),
        use_tls=False,
        wrapper_module="seal_wrapper_160",
        grpc_max_message_length=8 * 1024 * 1024,
        galois_keys_path=str(artifacts / "galois_keys_160.bin"),
    )

    x_test = np.load(artifacts / "X_test.npy")
    x = np.asarray(x_test[0], dtype=np.float64).reshape(1, 256)

    weights, bias = load_model_weights()
    resp = client.run_inference(x)
    passed, max_abs_error = verify_encrypted_output(resp["fraud_probabilities"], x, weights, bias)

    print(f"[parity_gate] passed={passed} max_abs_error={max_abs_error:.3e} "
          f"(tol={DEFAULT_TOLERANCE:.0e})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(_self_test())
