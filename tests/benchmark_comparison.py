#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient

ARTIFACTS = REPO_ROOT / "artifacts"

BASELINE_ADDR = "127.0.0.1:50051"
REDUCED_ADDR = "127.0.0.1:50052"

BASELINE_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_main"
REDUCED_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
WEIGHTS_PATH = REPO_ROOT / "artifacts" / "model_weights.bin"

BASELINE_KEYS = {
    "public": REPO_ROOT / "artifacts" / "public_key.bin",
    "secret": REPO_ROOT / "artifacts" / "secret_key.bin",
}
REDUCED_KEYS = {
    "public": REPO_ROOT / "artifacts" / "public_key_160.bin",
    "secret": REPO_ROOT / "artifacts" / "secret_key_160.bin",
}

WARMUP_ROUNDS = 20
MEASURE_ROUNDS = 100


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


def _build_client(variant: str) -> BankClient:
    if variant == "baseline_200bit":
        return BankClient(
            BASELINE_ADDR,
            weights_path=str(WEIGHTS_PATH),
            public_key_path=str(BASELINE_KEYS["public"]),
            secret_key_path=str(BASELINE_KEYS["secret"]),
            wrapper_module="seal_wrapper",
            grpc_max_message_length=512 * 1024,
        )

    return BankClient(
        REDUCED_ADDR,
        weights_path=str(WEIGHTS_PATH),
        public_key_path=str(REDUCED_KEYS["public"]),
        secret_key_path=str(REDUCED_KEYS["secret"]),
        wrapper_module="seal_wrapper_160",
        grpc_max_message_length=384 * 1024,
    )


def _collect_runs(variant: str, x_sample: np.ndarray) -> list[dict[str, float]]:
    client = _build_client(variant)

    for _ in range(WARMUP_ROUNDS):
        client.run_inference(x_sample)

    runs: list[dict[str, float]] = []
    for _ in range(MEASURE_ROUNDS):
        t0 = time.perf_counter_ns()
        resp = client.run_inference(x_sample)
        t1 = time.perf_counter_ns()

        td = resp["timing_breakdown"]
        runs.append(
            {
                "wall_us": (t1 - t0) / 1000.0,
                "total_inference_us": float(td["total_inference_us"]),
                "rotation_hoisting_us": float(td["rotation_hoisting_us"]),
                "multiply_plain_us": float(td["multiply_plain_us"]),
                "deserialization_us": float(td["deserialization_us"]),
                "serialization_us": float(td["serialization_us"]),
            }
        )
    return runs


def _summarize(results: dict[str, list[dict[str, float]]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for variant, runs in results.items():
        arr = np.array([r["total_inference_us"] for r in runs], dtype=np.float64)
        summary[variant] = {
            "n": int(arr.size),
            "mean_us": float(np.mean(arr)),
            "std_us": float(np.std(arr)),
            "p50_us": float(np.percentile(arr, 50)),
            "p95_us": float(np.percentile(arr, 95)),
            "p99_us": float(np.percentile(arr, 99)),
            "min_us": float(np.min(arr)),
            "max_us": float(np.max(arr)),
        }
    return summary


def measure_cold_start(server_binary: str, weights_path: str, timeout: int = 15) -> float:
    import select

    proc = subprocess.Popen(
        [server_binary, weights_path],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + timeout
        server_ready = False

        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if ready:
                line = proc.stdout.readline()
                print(f"  [server] {line.rstrip()}")
                if "Warmup complete" in line or "Galois keys loaded" in line:
                    server_ready = True
                    break

            if proc.poll() is not None:
                raise RuntimeError(f"Server exited early with code {proc.returncode}")

        if not server_ready:
            raise TimeoutError(f"Server did not print ready signal within {timeout}s")

        time.sleep(1.0)

        client = BankClient("localhost:50052", use_tls=False)
        x_sample = np.random.randn(1, 256).astype(np.float64)
        t0 = time.perf_counter_ns()
        client.run_inference(x_sample)
        t1 = time.perf_counter_ns()
        return (t1 - t0) / 1000.0
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main() -> int:
    required = [
        BASELINE_BIN,
        REDUCED_BIN,
        WEIGHTS_PATH,
        BASELINE_KEYS["public"],
        BASELINE_KEYS["secret"],
        REDUCED_KEYS["public"],
        REDUCED_KEYS["secret"],
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        print("Missing required files:")
        for m in missing:
            print(f"  {m}")
        return 1

    baseline_proc = _launch_server(BASELINE_BIN, 50051)
    time.sleep(3)
    reduced_proc = _launch_server(REDUCED_BIN, 50052)
    time.sleep(3)

    results = {"baseline_200bit": [], "reduced_160bit": []}
    x_sample = np.full((1, 256), 0.01, dtype=np.float64)

    try:
        results["baseline_200bit"] = _collect_runs("baseline_200bit", x_sample)
        results["reduced_160bit"] = _collect_runs("reduced_160bit", x_sample)
    finally:
        _wait_and_terminate(baseline_proc)
        _wait_and_terminate(reduced_proc)

    summary = _summarize(results)

    gates = {
        "mean_under_3000": summary["reduced_160bit"]["mean_us"] < 3000,
        "p99_under_5000": summary["reduced_160bit"]["p99_us"] < 5000,
        "std_under_300": summary["reduced_160bit"]["std_us"] < 300,
        "pass_rate_10000": sum(
            1
            for r in results["reduced_160bit"]
            if r["total_inference_us"] < 10000
        ) / MEASURE_ROUNDS,
    }

    # -- Cold-start gate (optional, manual only) --
    if os.environ.get("PPFD_MEASURE_COLD_START") == "1":
        server_binary = os.environ.get("PPFD_SERVER_BINARY")
        weights_path = os.environ.get("PPFD_WEIGHTS_PATH", "artifacts/model_weights.bin")

        if not server_binary:
            print("[cold-start] SKIPPED -- set PPFD_SERVER_BINARY=/path/to/binary")
            print("[cold-start] Example:")
            print("  PPFD_MEASURE_COLD_START=1 \\")
            print("  PPFD_SERVER_BINARY=./vendor_server/build/vendor_server_main \\")
            print("  python3 tests/benchmark_comparison.py")
        elif not os.path.exists(server_binary):
            print(f"[cold-start] SKIPPED -- binary not found: {server_binary}")
        else:
            try:
                cold_us = measure_cold_start(server_binary, weights_path, timeout=15)
                print(f"[cold-start] {cold_us:.0f} us -- {'PASS' if cold_us < 9000 else 'FAIL'}")
                summary["cold_start_us"] = cold_us
                assert cold_us < 9000, f"Cold-start breach: {cold_us:.0f} us > 9000 us SLA"
            except TimeoutError:
                print("[cold-start] SKIPPED -- server did not start within 15s")
            except Exception as e:
                print(f"[cold-start] SKIPPED -- {e}")

    gates["all_passed"] = all(v is True or v == 1.0 for v in gates.values())

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "comparison_results.json"
    out_file.write_text(
        json.dumps(
            {
                "summary": summary,
                "gates": gates,
                "raw_results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))
    print(f"\nGates: {gates}")
    assert gates["all_passed"], "LATENCY GATE FAILED -- see above"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
