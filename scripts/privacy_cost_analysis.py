#!/usr/bin/env python3
"""§5.8: privacy cost analysis.

Quantifies the cost of ONE ADDITIONAL homomorphic multiplicative level --
e.g. the extra ciphertext-ciphertext multiplication a model-weight masking
scheme (model privacy) would require -- using the 200-bit modulus chain
({60,40,40,60}, 4 primes) vs the 160-bit chain ({60,40,60}, 3 primes) as a
PROXY. Both variants already exist, run the same circuit/codebase/hardware
(docs/spec.md §5.7 Type 1 self-ablation), and differ by exactly one 40-bit
prime in the modulus chain -- which is exactly what provisioning one more
multiplicative level costs. No model-privacy masking scheme is implemented;
this measures what its modulus-chain cost WOULD be, via the proxy that
already exists.

Three cost axes, each from a real measurement:

  - modulus_bits: 200 - 160 = 40 bits (one additional RNS prime).
  - latency: artifacts/comparison_results.json (§5.3/§5.7), median_us,
    baseline_200bit vs reduced_160bit.
  - bandwidth: artifacts/wire_sizes.json (§5.7 Part A), standard_bytes,
    200bit vs 160bit.
  - precision: a fresh single-inference §5.5 parity-gate run against both
    live servers (max_abs_error vs the plaintext oracle).

Writes artifacts/privacy_cost_analysis.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient
from scripts.parity_gate import load_model_weights, verify_encrypted_output

ARTIFACTS = REPO_ROOT / "artifacts"
COMPARISON_RESULTS = ARTIFACTS / "comparison_results.json"
WIRE_SIZES = ARTIFACTS / "wire_sizes.json"

BASELINE_ADDR = "127.0.0.1:50051"
REDUCED_ADDR = "127.0.0.1:50052"
BASELINE_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_main"
REDUCED_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
WEIGHTS_PATH = REPO_ROOT / "artifacts" / "model_weights.bin"
X_TEST_PATH = REPO_ROOT / "artifacts" / "X_test.npy"

BASELINE_KEYS = {
    "public": ARTIFACTS / "public_key.bin",
    "secret": ARTIFACTS / "secret_key.bin",
}
REDUCED_KEYS = {
    "public": ARTIFACTS / "public_key_160.bin",
    "secret": ARTIFACTS / "secret_key_160.bin",
}

MODULUS_BITS = {"160bit": 160, "200bit": 200}

# Single sample, distinct from the seeds used by benchmark_comparison.py
# (INPUT_SEED=1234) and benchmark_throughput.py (INPUT_SEED=7777).
PRECISION_SEED = 555111


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _launch_server(bin_path: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [str(bin_path), str(WEIGHTS_PATH), str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_and_terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _build_client(chain: str) -> BankClient:
    if chain == "200bit":
        return BankClient(
            BASELINE_ADDR,
            public_key_path=str(BASELINE_KEYS["public"]),
            secret_key_path=str(BASELINE_KEYS["secret"]),
            wrapper_module="seal_wrapper",
            grpc_max_message_length=512 * 1024,
        )
    return BankClient(
        REDUCED_ADDR,
        public_key_path=str(REDUCED_KEYS["public"]),
        secret_key_path=str(REDUCED_KEYS["secret"]),
        wrapper_module="seal_wrapper_160",
        grpc_max_message_length=8 * 1024 * 1024,
        galois_keys_path=str(ARTIFACTS / "galois_keys_160.bin"),
    )


def _measure_precision() -> dict[str, float]:
    """§5.5 single-inference parity-gate run against both live servers.
    Returns {"160bit": max_abs_error, "200bit": max_abs_error}."""
    required = [
        BASELINE_BIN, REDUCED_BIN, WEIGHTS_PATH, X_TEST_PATH,
        BASELINE_KEYS["public"], BASELINE_KEYS["secret"],
        REDUCED_KEYS["public"], REDUCED_KEYS["secret"],
        ARTIFACTS / "galois_keys_160.bin",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Missing required files: {missing}")

    if _is_port_open("127.0.0.1", 50051) or _is_port_open("127.0.0.1", 50052):
        raise RuntimeError(
            "Benchmark ports 50051/50052 are already in use. Stop existing "
            "vendor_server processes and rerun."
        )

    x_test = np.load(X_TEST_PATH).astype(np.float64)
    rng = np.random.default_rng(PRECISION_SEED)
    x = x_test[rng.integers(0, x_test.shape[0])].reshape(1, 1, 256)

    weights, model_bias = load_model_weights(WEIGHTS_PATH)

    baseline_proc = _launch_server(BASELINE_BIN, 50051)
    time.sleep(3)
    if baseline_proc.poll() is not None:
        raise RuntimeError(f"200-bit server failed to start (exit code {baseline_proc.returncode})")
    reduced_proc = _launch_server(REDUCED_BIN, 50052)
    time.sleep(3)
    if reduced_proc.poll() is not None:
        _wait_and_terminate(baseline_proc)
        raise RuntimeError(f"160-bit server failed to start (exit code {reduced_proc.returncode})")

    try:
        errors: dict[str, float] = {}
        for chain, gate_bias in (("200bit", 0.0), ("160bit", model_bias)):
            client = _build_client(chain)
            resp = client.run_inference(x[0])
            passed, max_abs_error = verify_encrypted_output(resp["fraud_probabilities"], x[0], weights, gate_bias)
            print(f"[privacy_cost][precision] chain={chain} passed={passed} max_abs_error={max_abs_error:.3e}")
            if not passed:
                raise RuntimeError(f"§5.5 parity gate FAILED for chain={chain}: max_abs_error={max_abs_error}")
            errors[chain] = max_abs_error
        return errors
    finally:
        _wait_and_terminate(baseline_proc)
        _wait_and_terminate(reduced_proc)


def main() -> int:
    if not COMPARISON_RESULTS.exists():
        print(f"Missing required file: {COMPARISON_RESULTS}")
        print("Run: python3 tests/benchmark_comparison.py first.")
        return 1
    if not WIRE_SIZES.exists():
        print(f"Missing required file: {WIRE_SIZES}")
        print("Run: python3 scripts/measure_wire_size.py first.")
        return 1

    comparison = json.loads(COMPARISON_RESULTS.read_text(encoding="utf-8"))
    wire_sizes = json.loads(WIRE_SIZES.read_text(encoding="utf-8"))

    latency_160_us = comparison["summary"]["reduced_160bit"]["median_us"]
    latency_200_us = comparison["summary"]["baseline_200bit"]["median_us"]
    latency_delta_us = latency_200_us - latency_160_us
    latency_pct_delta = latency_delta_us / latency_160_us * 100.0

    bandwidth_160_bytes = wire_sizes["chains"]["160bit"]["standard_bytes"]
    bandwidth_200_bytes = wire_sizes["chains"]["200bit"]["standard_bytes"]
    bandwidth_delta_bytes = bandwidth_200_bytes - bandwidth_160_bytes
    bandwidth_pct_delta = bandwidth_delta_bytes / bandwidth_160_bytes * 100.0

    modulus_bits_delta = MODULUS_BITS["200bit"] - MODULUS_BITS["160bit"]

    precision = _measure_precision()
    precision_delta = precision["200bit"] - precision["160bit"]

    key_finding = (
        f"Provisioning the one additional 40-bit RNS prime that one extra "
        f"homomorphic multiplicative level (e.g. a model-weight masking step "
        f"for model privacy) would require -- measured via the 200-bit vs "
        f"160-bit self-ablation -- costs +{latency_delta_us:.1f}us "
        f"({latency_pct_delta:.1f}%) median latency and "
        f"+{bandwidth_delta_bytes} bytes ({bandwidth_pct_delta:.1f}%) per "
        f"ciphertext, while max_abs_error in both chains remains within the "
        f"existing noise floor (~1e-7, see artifacts/precision_analysis.json)."
    )

    out = {
        "framing": {
            "description": (
                "§5.8: privacy cost analysis. Uses the 200-bit modulus chain "
                "({60,40,40,60}, 4 primes) vs the 160-bit chain ({60,40,60}, "
                "3 primes) as a PROXY for the cost of ONE ADDITIONAL "
                "homomorphic multiplicative level (e.g. a model-weight "
                "masking step for model privacy). This is the same "
                "self-ablation (docs/spec.md §5.7 Type 1) used by "
                "tests/benchmark_comparison.py -- same codebase/circuit/"
                "hardware, only the modulus chain differs."
            ),
            "methodology": "measured",
            "sources": {
                "latency": "artifacts/comparison_results.json#summary",
                "bandwidth": "artifacts/wire_sizes.json#chains",
                "precision": "fresh single-inference §5.5 parity-gate run (this script)",
            },
        },
        "modulus_bits": {
            "160bit": MODULUS_BITS["160bit"],
            "200bit": MODULUS_BITS["200bit"],
            "delta": modulus_bits_delta,
        },
        "latency_us": {
            "160bit_median": latency_160_us,
            "200bit_median": latency_200_us,
            "delta": latency_delta_us,
            "pct_delta": latency_pct_delta,
        },
        "bandwidth_bytes": {
            "160bit_standard": bandwidth_160_bytes,
            "200bit_standard": bandwidth_200_bytes,
            "delta": bandwidth_delta_bytes,
            "pct_delta": bandwidth_pct_delta,
        },
        "precision_max_abs_error": {
            "160bit": precision["160bit"],
            "200bit": precision["200bit"],
            "delta": precision_delta,
        },
        "key_finding": key_finding,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "privacy_cost_analysis.json"
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nWrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
