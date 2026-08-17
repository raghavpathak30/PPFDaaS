#!/usr/bin/env python3
"""Phase 4 (this session) — whole-circuit, bank's-perspective end-to-end latency.

§5.4 previously reported only the server-side TimingBreakdown
(deserialize -> multiply_plain -> rotation_hoisting -> serialize ->
total_inference_us). This script measures the FULL wall-clock path from the
bank's perspective:

  client: encode+encrypt -> serialize -> gRPC upload
  server: deserialize -> multiply_plain -> rotation_hoisting -> serialize
  client: gRPC download -> deserialize -> decrypt -> sigmoid

using bank_client.BankClient's new client_timing_breakdown fields
(bank_client/bank_client.py, this session) alongside the existing 5-field
server TimingBreakdown proto. Single request in flight (no concurrent load --
see tests/benchmark_throughput.py for that), parity-gated, n=1000 measured +
20 warmup per chain, for both the 160-bit and 200-bit chains.

Writes artifacts/e2e_latency_breakdown.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats as scipy_stats

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient
from scripts.parity_gate import load_model_weights, verify_encrypted_output

ARTIFACTS = REPO_ROOT / "artifacts"
OUTPUT = ARTIFACTS / "e2e_latency_breakdown.json"

BASELINE_ADDR = "127.0.0.1:50051"
REDUCED_ADDR = "127.0.0.1:50052"
BASELINE_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_main"
REDUCED_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
WEIGHTS_PATH = ARTIFACTS / "model_weights.bin"
X_TEST_PATH = ARTIFACTS / "X_test.npy"

BASELINE_KEYS = {"public": ARTIFACTS / "public_key.bin", "secret": ARTIFACTS / "secret_key.bin"}
REDUCED_KEYS = {"public": ARTIFACTS / "public_key_160.bin", "secret": ARTIFACTS / "secret_key_160.bin"}

WARMUP_ROUNDS = 20
MEASURE_ROUNDS = 1000
INPUT_SEED = 314159

STAGE_FIELDS = [
    "encode_encrypt_us", "grpc_roundtrip_us", "network_and_grpc_overhead_us",
    "decode_decrypt_us", "sigmoid_us", "client_wall_total_us",
]
SERVER_FIELDS = [
    "deserialization_us", "multiply_plain_us", "rotation_hoisting_us",
    "serialization_us", "total_inference_us",
]


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
        cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
            BASELINE_ADDR, public_key_path=str(BASELINE_KEYS["public"]),
            secret_key_path=str(BASELINE_KEYS["secret"]), wrapper_module="seal_wrapper",
            grpc_max_message_length=512 * 1024,
        )
    return BankClient(
        REDUCED_ADDR, public_key_path=str(REDUCED_KEYS["public"]),
        secret_key_path=str(REDUCED_KEYS["secret"]), wrapper_module="seal_wrapper_160",
        grpc_max_message_length=8 * 1024 * 1024, galois_keys_path=str(ARTIFACTS / "galois_keys_160.bin"),
    )


def _summarize(values: list[float]) -> dict:
    arr = np.array(values, dtype=np.float64)
    return {
        "mean_us": float(np.mean(arr)), "median_us": float(np.median(arr)),
        "p95_us": float(np.percentile(arr, 95)), "p99_us": float(np.percentile(arr, 99)),
        "min_us": float(np.min(arr)), "max_us": float(np.max(arr)),
    }


def _measure_chain(chain: str, port: int, x_test: np.ndarray, weights: np.ndarray, model_bias: float) -> dict:
    rng = np.random.default_rng(INPUT_SEED + (0 if chain == "160bit" else 1))
    client = _build_client(chain)

    # In-band parity gate BEFORE any timed sample (§5.5 pattern).
    x0 = x_test[rng.integers(0, x_test.shape[0])].reshape(1, 256)
    resp0 = client.run_inference(x0[0].reshape(1, 256))
    gate_bias = model_bias if chain == "160bit" else 0.0
    passed, max_abs_error = verify_encrypted_output(resp0["fraud_probabilities"], x0[0], weights, gate_bias)
    if not passed:
        raise RuntimeError(f"§5.5 parity gate FAILED for chain={chain}: max_abs_error={max_abs_error}")
    print(f"[e2e_latency_breakdown] chain={chain} parity gate passed (max_abs_error={max_abs_error:.3e})")

    for _ in range(WARMUP_ROUNDS):
        idx = rng.integers(0, x_test.shape[0])
        client.run_inference(x_test[idx].reshape(1, 256))

    client_samples: dict[str, list[float]] = {f: [] for f in STAGE_FIELDS}
    server_samples: dict[str, list[float]] = {f: [] for f in SERVER_FIELDS}
    for _ in range(MEASURE_ROUNDS):
        idx = rng.integers(0, x_test.shape[0])
        resp = client.run_inference(x_test[idx].reshape(1, 256))
        for f in STAGE_FIELDS:
            client_samples[f].append(resp["client_timing_breakdown"][f])
        for f in SERVER_FIELDS:
            server_samples[f].append(resp["timing_breakdown"][f])

    return {
        "correctness_max_abs_error": max_abs_error,
        "client_stage_us": {f: _summarize(v) for f, v in client_samples.items()},
        "server_stage_us": {f: _summarize(v) for f, v in server_samples.items()},
    }


def _governor() -> str:
    try:
        return open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip()
    except OSError:
        return "UNKNOWN"


def main() -> int:
    governor = _governor()
    print(f"[e2e_latency_breakdown] cpu_governor={governor}")

    if _is_port_open("127.0.0.1", 50051) or _is_port_open("127.0.0.1", 50052):
        sys.exit("FAIL: ports 50051/50052 already in use. Stop existing vendor_server processes.")

    x_test = np.load(X_TEST_PATH).astype(np.float64)
    weights, model_bias = load_model_weights(WEIGHTS_PATH)

    baseline_proc = _launch_server(BASELINE_BIN, 50051)
    time.sleep(3)
    if baseline_proc.poll() is not None:
        sys.exit(f"FAIL: 200-bit server failed to start (exit {baseline_proc.returncode})")
    reduced_proc = _launch_server(REDUCED_BIN, 50052)
    time.sleep(3)
    if reduced_proc.poll() is not None:
        _wait_and_terminate(baseline_proc)
        sys.exit(f"FAIL: 160-bit server failed to start (exit {reduced_proc.returncode})")

    try:
        print("[e2e_latency_breakdown] measuring 200bit chain ...")
        result_200 = _measure_chain("200bit", 50051, x_test, weights, model_bias)
        print("[e2e_latency_breakdown] measuring 160bit chain ...")
        result_160 = _measure_chain("160bit", 50052, x_test, weights, model_bias)
    finally:
        _wait_and_terminate(baseline_proc)
        _wait_and_terminate(reduced_proc)

    out = {
        "framing": {
            "description": (
                "Phase 4 (this session): full bank's-perspective end-to-end latency -- "
                "client encode+encrypt -> serialize -> gRPC upload -> server deserialize+"
                "multiply_plain+rotation_hoisting+serialize -> gRPC download -> client "
                "deserialize+decrypt+sigmoid. Single request in flight, no concurrent load "
                "(see artifacts/throughput_results.json for that). n=1000 measured + 20 "
                "warmup per chain, in-band parity-gated before any timed sample."
            ),
            "cpu_governor": governor,
            "governor_caveat": (
                "PENDING under 'performance' governor -- no root in this environment, see "
                "AUDIT.md Phase 1. Reported under 'powersave'; not final."
            ) if governor != "performance" else None,
            "note_on_grpc_roundtrip": (
                "grpc_roundtrip_us is upload+server+download combined (Python's grpc API does "
                "not expose separate upload-complete/download-start timestamps for a unary "
                "call). network_and_grpc_overhead_us = grpc_roundtrip_us - server's own "
                "total_inference_us is the client-observable residual: network + gRPC framing "
                "+ server-side (de)serialization of the request/response messages themselves "
                "(distinct from the server's internal deserialization_us/serialization_us "
                "stages, which time SEAL ciphertext (de)serialization, not protobuf/gRPC "
                "framing)."
            ),
        },
        "200bit": result_200,
        "160bit": result_160,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {OUTPUT}")

    for chain, result in (("200bit", result_200), ("160bit", result_160)):
        print(f"\n=== {chain} (cpu_governor={governor}) ===")
        print(f"{'stage':35s} {'median_us':>12s} {'p99_us':>12s}")
        for f in STAGE_FIELDS:
            s = result["client_stage_us"][f]
            print(f"{'client.' + f:35s} {s['median_us']:>12.1f} {s['p99_us']:>12.1f}")
        for f in SERVER_FIELDS:
            s = result["server_stage_us"][f]
            print(f"{'server.' + f:35s} {s['median_us']:>12.1f} {s['p99_us']:>12.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
