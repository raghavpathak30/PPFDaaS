#!/usr/bin/env python3
"""§5.6 execution matrix.

Cross-references the reduction strategy (fold/BSGS/naive), CKKS modulus
chain (160-bit/200-bit), gRPC thread-pool parallelism (PPFD_GRPC_THREADS),
batch occupancy (lanes used in the 16-slot batch), and HE library
(SEAL/OpenFHE) axes.

Every number in this file is either:
  (a) pulled directly from an existing measured artifact written by another
      §5.x script (results/ablation_methodology.json,
      artifacts/rotation_strategy_comparison.json,
      artifacts/comparison_results.json, artifacts/throughput_results.json),
  (b) produced by a NEW, real execution performed by this script
      (vendor_server/build/benchmark for the 200-bit local-circuit fold cell,
      and a short PPFD_GRPC_THREADS sweep at fixed n_clients=8/lanes=16), or
  (c) PENDING, with an explicit reason -- never estimated.

§5.1: NEVER estimate. PENDING is correct for cells that genuinely cannot be
produced (OpenFHE not installed, or an axis not wired into the relevant
binary).

Writes artifacts/execution_matrix.json and prints a summary table.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ARTIFACTS = REPO_ROOT / "artifacts"
RESULTS = REPO_ROOT / "results"

BENCHMARK_200_BIN = REPO_ROOT / "vendor_server" / "build" / "benchmark"
VENDOR_160_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
WEIGHTS_PATH = ARTIFACTS / "model_weights.bin"
GALOIS_KEYS_160 = ARTIFACTS / "galois_keys_160.bin"
REDUCED_KEYS = {
    "public": ARTIFACTS / "public_key_160.bin",
    "secret": ARTIFACTS / "secret_key_160.bin",
}
REDUCED_ADDR = "127.0.0.1:50052"

# §5.6: a short, real PPFD_GRPC_THREADS sweep at fixed n_clients=8,
# lanes=16. threads=4 is NOT re-measured here -- it is already covered by
# artifacts/throughput_results.json's n_clients=8 row (§5.4, 30s).
PARALLELISM_SWEEP_THREADS = [1, 2, 8]
PARALLELISM_SWEEP_DURATION_S = 3.0
PARALLELISM_SWEEP_N_CLIENTS = 8


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run_benchmark_200_fold() -> dict:
    """(b): SEAL/200bit/fold local-circuit cell -- real execution of
    vendor_server/build/benchmark (depth1_he_inference == hoisted_tree_sum,
    200-bit chain {60,40,40,60})."""
    if not BENCHMARK_200_BIN.exists():
        return {"status": "PENDING", "reason": f"{BENCHMARK_200_BIN} not built"}

    proc = subprocess.run([str(BENCHMARK_200_BIN)], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"status": "PENDING", "reason": f"benchmark exited {proc.returncode}: {proc.stdout.strip()}"}

    # Output: "avg_us=9531.39 avg_ms=9.53139"
    avg_us = None
    for tok in proc.stdout.split():
        if tok.startswith("avg_us="):
            avg_us = float(tok.split("=", 1)[1])
    if avg_us is None:
        return {"status": "PENDING", "reason": f"could not parse benchmark output: {proc.stdout!r}"}

    return {
        "status": "MEASURED",
        "source": "vendor_server/build/benchmark (depth1_he_inference, 200-bit chain {60,40,40,60})",
        "scope": "local circuit only: encrypt, multiply_plain, rescale, hoisted_tree_sum, decrypt (no gRPC/serialization); n=1000, no separate warmup",
        "latency_us": {"mean": avg_us},
    }


def _from_ablation_methodology() -> tuple[dict, dict]:
    """SEAL/160bit/fold and SEAL/160bit/naive from results/ablation_methodology.json (§5.1/§5.2)."""
    path = RESULTS / "ablation_methodology.json"
    if not path.exists():
        pending = {"status": "PENDING", "reason": f"{path} missing -- run scripts/generate_ablation.py"}
        return pending, pending

    d = json.loads(path.read_text())
    fold = {
        "status": "MEASURED",
        "source": "vendor_server/build/benchmark_160 --strategy=fold (via scripts/generate_ablation.py)",
        "scope": "local circuit only: encrypt, multiply_plain, rescale, hoisted_tree_sum, decrypt (no gRPC/serialization)",
        "rotations": d["hoisted_rotations"],
        "critical_path_steps": d["hoisted_critical_path"],
        "galois_keygen_us": d["hoisted_galois_keygen_us"],
        "correctness_max_abs_error": d["hoisted_correctness_max_abs_error"],
        "n": d["measure_rounds"],
        "latency_us": d["hoisted_latency_us"],
    }
    naive = {
        "status": "MEASURED",
        "source": "vendor_server/build/benchmark_160 --strategy=naive (via scripts/generate_ablation.py)",
        "scope": "local circuit only: encrypt, multiply_plain, rescale, naive_tree_sum, decrypt (no gRPC/serialization)",
        "rotations": d["naive_rotations"],
        "critical_path_steps": d["naive_critical_path"],
        "galois_keygen_us": d["naive_galois_keygen_us"],
        "correctness_max_abs_error": d["naive_correctness_max_abs_error"],
        "n": d["measure_rounds"],
        "latency_us": d["naive_latency_us"],
    }
    return fold, naive


def _from_rotation_strategy_comparison() -> dict:
    """SEAL/160bit/bsgs from artifacts/rotation_strategy_comparison.json."""
    path = ARTIFACTS / "rotation_strategy_comparison.json"
    if not path.exists():
        return {"status": "PENDING", "reason": f"{path} missing -- run scripts/rotation_strategy_comparison.py"}

    d = json.loads(path.read_text())
    for entry in d["strategies"]:
        if "BSGS" in entry["name"] and "local circuit" in entry["scope"]:
            return {
                "status": "MEASURED",
                "source": entry["source"],
                "scope": entry["scope"],
                "rotations": entry["rotations"],
                "critical_path_steps": entry["critical_path_steps"],
                "correctness_max_abs_error": entry.get("correctness_max_abs_error"),
                "n": entry["n"],
                "latency_us": {k: v * 1000.0 for k, v in entry["latency_ms"].items()},
                "note": entry.get("note"),
            }
    return {"status": "PENDING", "reason": f"no local-circuit BSGS row found in {path}"}


def _openfhe_pending() -> dict:
    path = REPO_ROOT / "tools" / "openfhe_benchmark" / "results" / "openfhe_results.json"
    reason = "OpenFHE not installed in this environment"
    if path.exists():
        d = json.loads(path.read_text())
        reason = d.get("reason", reason)
    return {"status": "PENDING", "reason": reason}


# ─── §5.6 parallelism axis (PPFD_GRPC_THREADS) ──────────────────────────────

def _launch_server(threads: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PPFD_GRPC_THREADS"] = str(threads)
    return subprocess.Popen(
        [str(VENDOR_160_BIN), str(WEIGHTS_PATH), "50052"],
        cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )


def _wait_and_terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _measure_threads_point(threads: int, n_clients: int, duration_s: float, x_test: np.ndarray) -> dict:
    import threading

    from bank_client.bank_client import BankClient

    if _is_port_open("127.0.0.1", 50052):
        raise RuntimeError("port 50052 already in use")

    server_proc = _launch_server(threads)
    time.sleep(3)
    if server_proc.poll() is not None:
        raise RuntimeError(f"vendor_server_160 failed to start (PPFD_GRPC_THREADS={threads}, exit {server_proc.returncode})")

    try:
        # Provision once (galois keys), then build n_clients worker clients
        # without re-provisioning.
        prov = BankClient(
            REDUCED_ADDR,
            public_key_path=str(REDUCED_KEYS["public"]),
            secret_key_path=str(REDUCED_KEYS["secret"]),
            wrapper_module="seal_wrapper_160",
            grpc_max_message_length=8 * 1024 * 1024,
            galois_keys_path=str(GALOIS_KEYS_160),
        )
        del prov

        rng = np.random.default_rng(9000 + threads)
        idx = rng.permutation(x_test.shape[0])[:16]
        batch = x_test[idx]

        latencies_ms: list[list[float]] = [[] for _ in range(n_clients)]
        deadline = time.time() + duration_s

        def _worker(out: list[float]) -> None:
            client = BankClient(
                REDUCED_ADDR,
                public_key_path=str(REDUCED_KEYS["public"]),
                secret_key_path=str(REDUCED_KEYS["secret"]),
                wrapper_module="seal_wrapper_160",
                grpc_max_message_length=8 * 1024 * 1024,
            )
            while time.time() < deadline:
                t0 = time.perf_counter()
                client.run_inference(batch)
                t1 = time.perf_counter()
                out.append((t1 - t0) * 1000.0)

        threads_list = [threading.Thread(target=_worker, args=(latencies_ms[i],)) for i in range(n_clients)]
        t_start = time.time()
        for t in threads_list:
            t.start()
        for t in threads_list:
            t.join()
        wall_s = time.time() - t_start

        all_lat = np.array([v for lst in latencies_ms for v in lst], dtype=np.float64)
        return {
            "status": "MEASURED",
            "source": "tests/benchmark_throughput.py-style sweep (scripts/build_execution_matrix.py, §5.6)",
            "ppfd_grpc_threads": threads,
            "n_clients": n_clients,
            "lanes_per_request": 16,
            "duration_s": wall_s,
            "n_requests": int(all_lat.size),
            "req_per_sec": float(all_lat.size / wall_s),
            "mean_latency_ms": float(np.mean(all_lat)),
            "p99_latency_ms": float(np.percentile(all_lat, 99)),
        }
    finally:
        _wait_and_terminate(server_proc)


def _parallelism_axis() -> list[dict]:
    points: list[dict] = []

    # threads=4 already measured in §5.4's n_clients=8 row (30s, full sweep).
    throughput_path = ARTIFACTS / "throughput_results.json"
    if throughput_path.exists():
        d = json.loads(throughput_path.read_text())
        for row in d["client_sweep"]:
            if row["n_clients"] == PARALLELISM_SWEEP_N_CLIENTS:
                points.append({
                    "status": "MEASURED",
                    "source": "artifacts/throughput_results.json: client_sweep (§5.4, 30s)",
                    "ppfd_grpc_threads": d["ppfd_grpc_threads"],
                    "n_clients": row["n_clients"],
                    "lanes_per_request": row["lanes_per_request"],
                    "duration_s": row["duration_s"],
                    "n_requests": row["n_requests"],
                    "req_per_sec": row["req_per_sec"],
                    "mean_latency_ms": row["mean_latency_ms"],
                    "p99_latency_ms": row["p99_latency_ms"],
                })
                break

    if not (VENDOR_160_BIN.exists() and GALOIS_KEYS_160.exists() and REDUCED_KEYS["public"].exists()):
        for threads in PARALLELISM_SWEEP_THREADS:
            points.append({"status": "PENDING", "ppfd_grpc_threads": threads, "reason": "vendor_server_160 or 160-bit key artifacts missing"})
        return points

    x_test_path = ARTIFACTS / "X_test.npy"
    x_test = np.load(x_test_path).astype(np.float64)

    for threads in PARALLELISM_SWEEP_THREADS:
        try:
            point = _measure_threads_point(threads, PARALLELISM_SWEEP_N_CLIENTS, PARALLELISM_SWEEP_DURATION_S, x_test)
        except Exception as e:
            point = {"status": "PENDING", "ppfd_grpc_threads": threads, "reason": str(e)}
        points.append(point)
        print(f"[execution_matrix] parallelism threads={threads}: {point.get('req_per_sec', point.get('reason'))}")

    points.sort(key=lambda p: p["ppfd_grpc_threads"])
    return points


# ─── §5.6 batch occupancy axis ──────────────────────────────────────────────

def _occupancy_axis() -> list[dict]:
    path = ARTIFACTS / "throughput_results.json"
    if not path.exists():
        return [{"status": "PENDING", "reason": f"{path} missing -- run tests/benchmark_throughput.py"}]

    d = json.loads(path.read_text())
    out = []
    for row in d["occupancy_sweep"]:
        out.append({
            "status": "MEASURED",
            "source": "artifacts/throughput_results.json: occupancy_sweep (§5.4/§5.7)",
            "ppfd_grpc_threads": d["ppfd_grpc_threads"],
            "lanes": row["lanes"],
            "batch_latency_us": row["batch_latency_us"],
            "per_tx_us": row["per_tx_us"],
        })
    return out


def main() -> int:
    fold_160, naive_160 = _from_ablation_methodology()
    bsgs_160 = _from_rotation_strategy_comparison()
    fold_200 = _run_benchmark_200_fold()
    openfhe = _openfhe_pending()

    reduction_x_chain_x_library = {
        "SEAL": {
            "160bit": {"fold": fold_160, "bsgs": bsgs_160, "naive": naive_160},
            "200bit": {
                "fold": fold_200,
                "bsgs": {
                    "status": "PENDING",
                    "reason": (
                        "vendor_server/build/benchmark (200-bit local circuit) does not "
                        "implement a --strategy dispatch; only depth1_he_inference "
                        "(== hoisted-fold) is wired. §5.1 scoped the naive-strategy "
                        "addition to benchmark_160 (160-bit) only -- extending the 200-bit "
                        "binary's strategy dispatch is PHASE 5 ITEM: out of scope, not "
                        "fixed here."
                    ),
                },
                "naive": {
                    "status": "PENDING",
                    "reason": (
                        "vendor_server/build/benchmark (200-bit local circuit) does not "
                        "implement a --strategy dispatch; only depth1_he_inference "
                        "(== hoisted-fold) is wired. §5.1 scoped the naive-strategy "
                        "addition to benchmark_160 (160-bit) only -- extending the 200-bit "
                        "binary's strategy dispatch is PHASE 5 ITEM: out of scope, not "
                        "fixed here."
                    ),
                },
            },
        },
        "OpenFHE": {
            "160bit": {"fold": openfhe, "bsgs": openfhe, "naive": openfhe},
            "200bit": {"fold": openfhe, "bsgs": openfhe, "naive": openfhe},
        },
    }

    parallelism_axis = _parallelism_axis()
    occupancy_axis = _occupancy_axis()

    matrix = {
        "axes": {
            "reduction_strategy": ["fold", "bsgs", "naive"],
            "modulus_chain": ["160bit", "200bit"],
            "library": ["SEAL", "OpenFHE"],
            "parallelism_ppfd_grpc_threads": [1, 2, 4, 8],
            "batch_occupancy_lanes": [1, 4, 8, 16],
        },
        "notes": [
            "Cells in reduction_strategy x modulus_chain x library are LOCAL-CIRCUIT "
            "latencies (encrypt..decrypt, no gRPC). They do not vary with "
            "parallelism_ppfd_grpc_threads or batch_occupancy_lanes: PPFD_GRPC_THREADS "
            "only sizes vendor_server_160's gRPC sync thread pool (not used by the "
            "local-circuit benchmark binaries), and CKKS ciphertext operations cost the "
            "same regardless of how many of the 16 logical lanes hold real data "
            "(confirmed by occupancy_axis below: batch_latency_us is ~constant across "
            "lanes=1..16).",
            "parallelism_axis and occupancy_axis are gRPC-layer measurements "
            "(reduced_160bit / fold only -- PPFD_GRPC_THREADS is wired only into "
            "inference_service_160.cpp).",
            "library=OpenFHE is PENDING for every cell: OpenFHE is not installed in "
            "this environment (no OpenFHEConfig.cmake / pkg-config). "
            "tools/openfhe_benchmark/ is a compile-ready, never-built scaffold.",
        ],
        "reduction_strategy_x_modulus_chain_x_library": reduction_x_chain_x_library,
        "parallelism_axis": parallelism_axis,
        "occupancy_axis": occupancy_axis,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "execution_matrix.json"
    out_file.write_text(json.dumps(matrix, indent=2), encoding="utf-8")

    print(f"\nWrote {out_file}\n")
    print("reduction_strategy x modulus_chain x library (local circuit, mean latency_us or PENDING):")
    print(f"{'':10s} {'160bit':>22s} {'200bit':>22s}")
    for strat in ["fold", "bsgs", "naive"]:
        row = []
        for chain in ["160bit", "200bit"]:
            cell = reduction_x_chain_x_library["SEAL"][chain][strat]
            if cell["status"] == "MEASURED":
                row.append(f"{cell['latency_us']['mean']:>18.2f} us")
            else:
                row.append(f"{'PENDING':>22s}")
        print(f"{strat:10s} {row[0]:>22s} {row[1]:>22s}")
    print(f"{'OpenFHE':10s} {'PENDING':>22s} {'PENDING':>22s}")

    print("\nparallelism axis (n_clients=8, lanes=16):")
    for p in parallelism_axis:
        if p["status"] == "MEASURED":
            print(f"  threads={p['ppfd_grpc_threads']}: req/s={p['req_per_sec']:.2f} mean_ms={p['mean_latency_ms']:.3f} p99_ms={p['p99_latency_ms']:.3f}")
        else:
            print(f"  threads={p.get('ppfd_grpc_threads')}: PENDING ({p['reason']})")

    print("\noccupancy axis (PPFD_GRPC_THREADS=4):")
    for o in occupancy_axis:
        if o["status"] == "MEASURED":
            print(f"  lanes={o['lanes']:2d}: batch_latency_us={o['batch_latency_us']:.2f} per_tx_us={o['per_tx_us']:.2f}")
        else:
            print(f"  PENDING ({o['reason']})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
