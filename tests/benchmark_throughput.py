#!/usr/bin/env python3
"""§5.4 throughput benchmark: closed-loop concurrency sweep against
vendor_server_160 (reduced 160-bit).

This is the THROUGHPUT half of the §5.4 latency-vs-throughput split:

  - tests/benchmark_comparison.py measures LATENCY -- a single request in
    flight at a time, no concurrent load.
  - This script measures THROUGHPUT under closed-loop concurrent load:
    n_clients in {1, 4, 8, 16}, each client looping run_inference() as fast
    as it can for DURATION_SECONDS, against the SAME vendor_server_160
    binary and circuit (hoisted_tree_sum, 160-bit chain).

It also runs a single-client BATCH-OCCUPANCY sweep (lanes in {1, 4, 8, 16},
no concurrent load) used by scripts/generate_amortization_table.py (§5.7
Part B) to compute per-transaction amortized cost.

§5.5: a parity gate (verify_encrypted_output against the plaintext oracle)
is run once at startup, before any timing, and its result is discarded.

§2.1: PPFD_GRPC_THREADS sizes vendor_server_160's sync gRPC thread pool
(NUM_CQS/MIN_POLLERS/MAX_POLLERS). This script asserts it is >= 4 so the
n_clients=16 sweep point can exercise real concurrent dispatch.

Writes artifacts/throughput_results.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient
from scripts.parity_gate import load_model_weights, verify_encrypted_output

ARTIFACTS = REPO_ROOT / "artifacts"
REDUCED_ADDR = "127.0.0.1:50052"
REDUCED_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
WEIGHTS_PATH = REPO_ROOT / "artifacts" / "model_weights.bin"
X_TEST_PATH = REPO_ROOT / "artifacts" / "X_test.npy"
REDUCED_KEYS = {
    "public": REPO_ROOT / "artifacts" / "public_key_160.bin",
    "secret": REPO_ROOT / "artifacts" / "secret_key_160.bin",
}
GALOIS_KEYS_PATH = ARTIFACTS / "galois_keys_160.bin"

# §5.4: closed-loop concurrency sweep.
N_CLIENTS_SWEEP = [1, 4, 8, 16]
DURATION_SECONDS = 30.0

# §5.4/§5.7: single-client batch-occupancy sweep (no concurrent load).
OCCUPANCY_LANES_SWEEP = [1, 4, 8, 16]
OCCUPANCY_ROUNDS = 100
OCCUPANCY_WARMUP = 10

INPUT_SEED = 7777


def _fast() -> bool:
    return "--fast" in sys.argv[1:]


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _launch_server(threads: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PPFD_GRPC_THREADS"] = str(threads)
    return subprocess.Popen(
        [str(REDUCED_BIN), str(WEIGHTS_PATH), "50052"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _wait_and_terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _build_provisioning_client() -> BankClient:
    return BankClient(
        REDUCED_ADDR,
        public_key_path=str(REDUCED_KEYS["public"]),
        secret_key_path=str(REDUCED_KEYS["secret"]),
        wrapper_module="seal_wrapper_160",
        grpc_max_message_length=8 * 1024 * 1024,
        galois_keys_path=str(GALOIS_KEYS_PATH),
    )


def _build_worker_client() -> BankClient:
    # §1.4: Galois keys are already provisioned server-side by
    # _build_provisioning_client(); subsequent connections skip
    # ProvisionGaloisKeys/CanaryCheck (galois_keys_path=None).
    return BankClient(
        REDUCED_ADDR,
        public_key_path=str(REDUCED_KEYS["public"]),
        secret_key_path=str(REDUCED_KEYS["secret"]),
        wrapper_module="seal_wrapper_160",
        grpc_max_message_length=8 * 1024 * 1024,
    )


def _load_x_test() -> np.ndarray:
    if not X_TEST_PATH.exists():
        raise FileNotFoundError(f"Missing required file: {X_TEST_PATH}")
    return np.load(X_TEST_PATH).astype(np.float64)


def _batches(x_test: np.ndarray, lanes: int, n: int, seed: int) -> list[np.ndarray]:
    """n distinct (lanes, 256) real-data batches drawn from the held-out test
    set, in a fixed-seed random order, wrapping via modulo if n*lanes exceeds
    the dataset size."""
    n_total = x_test.shape[0]
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n_total)
    out = []
    pos = 0
    for _ in range(n):
        idx = perm[np.arange(pos, pos + lanes) % n_total]
        out.append(x_test[idx])
        pos += lanes
    return out


# ─── §5.5: in-band parity gate ──────────────────────────────────────────────

def _run_parity_gate(client: BankClient, x: np.ndarray) -> None:
    weights, bias = load_model_weights(WEIGHTS_PATH)
    resp = client.run_inference(x)  # NOT timed -- verification only
    passed, max_abs_error = verify_encrypted_output(resp["fraud_probabilities"], x, weights, bias)
    print(f"[parity_gate] variant=reduced_160bit passed={passed} max_abs_error={max_abs_error:.3e}")
    if not passed:
        raise RuntimeError(f"§5.5 parity gate FAILED: max_abs_error={max_abs_error} >= tol")


# ─── §5.4 Part 1: closed-loop n_clients sweep (16-lane batches, full occupancy) ──

def _client_worker(
    batches: list[np.ndarray],
    deadline: float,
    latencies_ms: list[float],
) -> None:
    client = _build_worker_client()
    i = 0
    n = len(batches)
    while time.time() < deadline:
        x = batches[i % n]
        t0 = time.perf_counter()
        client.run_inference(x)
        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000.0)
        i += 1


def _run_client_sweep(x_test: np.ndarray, duration_s: float) -> list[dict]:
    results = []
    # 64 distinct full (16, 256) batches shared (read-only) across all worker
    # threads -- BankClient instances are per-thread, batches are not mutated.
    shared_batches = _batches(x_test, 16, 64, seed=INPUT_SEED)

    for n_clients in N_CLIENTS_SWEEP:
        per_thread_latencies: list[list[float]] = [[] for _ in range(n_clients)]
        deadline = time.time() + duration_s
        threads = [
            threading.Thread(target=_client_worker, args=(shared_batches, deadline, per_thread_latencies[i]))
            for i in range(n_clients)
        ]
        t_start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall_s = time.time() - t_start

        all_latencies = np.array([v for lst in per_thread_latencies for v in lst], dtype=np.float64)
        n_requests = int(all_latencies.size)
        req_per_sec = n_requests / wall_s
        # Each request carries a full 16-lane batch (16 real transactions).
        per_tx_us = float(np.mean(all_latencies)) * 1000.0 / 16.0

        results.append(
            {
                "n_clients": n_clients,
                "duration_s": wall_s,
                "n_requests": n_requests,
                "req_per_sec": req_per_sec,
                "mean_latency_ms": float(np.mean(all_latencies)),
                "p99_latency_ms": float(np.percentile(all_latencies, 99)),
                "p50_latency_ms": float(np.percentile(all_latencies, 50)),
                "amortized_per_tx_us": per_tx_us,
                "lanes_per_request": 16,
            }
        )
        print(
            f"[throughput] n_clients={n_clients:2d} n_requests={n_requests:5d} "
            f"req/s={req_per_sec:8.2f} mean_ms={np.mean(all_latencies):8.3f} "
            f"p99_ms={np.percentile(all_latencies, 99):8.3f} "
            f"per_tx_us={per_tx_us:8.2f}"
        )

    return results


# ─── §5.4/§5.7 Part 2: single-client batch-occupancy sweep ──────────────────

def _run_occupancy_sweep(x_test: np.ndarray, rounds: int, warmup: int) -> list[dict]:
    client = _build_worker_client()
    results = []
    for lanes in OCCUPANCY_LANES_SWEEP:
        batches = _batches(x_test, lanes, warmup + rounds, seed=INPUT_SEED + lanes)
        for i in range(warmup):
            client.run_inference(batches[i])

        latencies_ms = []
        for i in range(rounds):
            x = batches[warmup + i]
            t0 = time.perf_counter()
            client.run_inference(x)
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)

        arr = np.array(latencies_ms, dtype=np.float64)
        batch_latency_us = float(np.median(arr)) * 1000.0
        results.append(
            {
                "lanes": lanes,
                "n": rounds,
                "batch_latency_us": batch_latency_us,
                "per_tx_us": batch_latency_us / lanes,
                "mean_latency_us": float(np.mean(arr)) * 1000.0,
                "p99_latency_us": float(np.percentile(arr, 99)) * 1000.0,
            }
        )
        print(
            f"[occupancy] lanes={lanes:2d} batch_latency_us={batch_latency_us:9.2f} "
            f"per_tx_us={batch_latency_us / lanes:9.2f}"
        )

    return results


def main() -> int:
    fast = _fast()
    duration_s = 2.0 if fast else DURATION_SECONDS
    occupancy_rounds = 20 if fast else OCCUPANCY_ROUNDS
    occupancy_warmup = 5 if fast else OCCUPANCY_WARMUP

    required = [REDUCED_BIN, WEIGHTS_PATH, X_TEST_PATH, REDUCED_KEYS["public"], REDUCED_KEYS["secret"], GALOIS_KEYS_PATH]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        print("Missing required files:")
        for m in missing:
            print(f"  {m}")
        return 1

    # §2.1: PPFD_GRPC_THREADS sizes vendor_server_160's sync gRPC thread pool.
    # Must be >= 4 for the n_clients=16 sweep point to exercise real
    # concurrent dispatch (ServerBuilder defaults cap it at ~2).
    threads = int(os.environ.get("PPFD_GRPC_THREADS", "4"))
    assert threads >= 4, (
        f"PPFD_GRPC_THREADS={threads} < 4 -- the n_clients sweep up to 16 "
        f"requires at least 4 server threads to exercise real concurrency"
    )

    if _is_port_open("127.0.0.1", 50052):
        raise RuntimeError("Benchmark port 50052 is already in use. Stop existing vendor_server_160 and rerun.")

    server_proc = _launch_server(threads)
    time.sleep(3)
    if server_proc.poll() is not None:
        raise RuntimeError(f"vendor_server_160 failed to start (exit code {server_proc.returncode})")

    try:
        x_test = _load_x_test()

        provisioning_client = _build_provisioning_client()

        # §5.5: verify against the plaintext oracle BEFORE any timing.
        _run_parity_gate(provisioning_client, x_test[:1])

        client_sweep = _run_client_sweep(x_test, duration_s)
        occupancy_sweep = _run_occupancy_sweep(x_test, occupancy_rounds, occupancy_warmup)
    finally:
        _wait_and_terminate(server_proc)

    out = {
        "framing": {
            "description": (
                "§5.4 throughput (closed-loop, concurrent load) vs "
                "tests/benchmark_comparison.py's latency (single request in "
                "flight). Same vendor_server_160 binary/circuit (hoisted_tree_sum, "
                "160-bit chain)."
            ),
            "fast": fast,
        },
        "ppfd_grpc_threads": threads,
        "client_sweep": client_sweep,
        "occupancy_sweep": occupancy_sweep,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "throughput_results.json"
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
