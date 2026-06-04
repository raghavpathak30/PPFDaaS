#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient


WARMUP_ROUNDS = 20
MEASURE_ROUNDS = 200
HOISTED_ROTATIONS = 8
NAIVE_ROTATIONS = 255
DEFAULT_HOISTED_ADDR = "127.0.0.1:50052"


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_host_port(address: str) -> tuple[str, int]:
    if ":" not in address:
        raise ValueError(f"Address must be host:port, got: {address}")
    host, port_str = address.rsplit(":", 1)
    return host, int(port_str)


def _launch_160_server(port: int) -> subprocess.Popen[str]:
    server_bin = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
    weights_path = REPO_ROOT / "artifacts" / "model_weights.bin"

    missing = [str(p) for p in (server_bin, weights_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Cannot autostart vendor server; missing required file(s):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    return subprocess.Popen(
        [str(server_bin), str(weights_path), str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _wait_until_ready(host: str, port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _is_port_open(host, port):
            return True
        time.sleep(0.2)
    return False


def _terminate_proc(proc: subprocess.Popen[str]) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _stats(values: list[float]) -> dict[str, float]:
    arr = np.array(values, dtype=np.float64)
    return {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr)),
        "p99": float(np.percentile(arr, 99)),
    }


def _measure_rotation_us(client: BankClient, x_test: np.ndarray) -> list[float]:
    first = np.asarray(x_test[0], dtype=np.float64).reshape(1, 256)
    for _ in range(WARMUP_ROUNDS):
        client.run_inference(first)

    out: list[float] = []
    for i in range(MEASURE_ROUNDS):
        x = np.asarray(x_test[i % x_test.shape[0]], dtype=np.float64).reshape(1, 256)
        resp = client.run_inference(x)
        out.append(float(resp["timing_breakdown"]["rotation_hoisting_us"]))
    return out


def _build_160_client() -> BankClient:
    artifacts = REPO_ROOT / "artifacts"
    hoisted_addr = os.environ.get("PPFD_HOISTED_ADDR", DEFAULT_HOISTED_ADDR)
    return BankClient(
        hoisted_addr,
        weights_path=str(artifacts / "model_weights.bin"),
        public_key_path=str(artifacts / "public_key_160.bin"),
        secret_key_path=str(artifacts / "secret_key_160.bin"),
        use_tls=False,
        wrapper_module="seal_wrapper_160",
        grpc_max_message_length=384 * 1024,
    )


def main() -> int:
    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    x_test_path = REPO_ROOT / "artifacts" / "X_test.npy"
    if not x_test_path.exists():
        raise FileNotFoundError(f"Missing required file: {x_test_path}")
    x_test = np.load(x_test_path)

    managed_proc: subprocess.Popen[str] | None = None
    try:
        hoisted_addr = os.environ.get("PPFD_HOISTED_ADDR", DEFAULT_HOISTED_ADDR)
        host, port = _parse_host_port(hoisted_addr)
        autostart_enabled = os.environ.get("PPFD_ABLATION_AUTOSTART", "1") != "0"

        if not _is_port_open(host, port):
            if autostart_enabled and hoisted_addr == DEFAULT_HOISTED_ADDR:
                managed_proc = _launch_160_server(port)
                if not _wait_until_ready(host, port, timeout_s=15.0):
                    raise RuntimeError(
                        "Autostarted vendor_server_160, but it did not become ready within 15s."
                    )
                print(f"[ablation] auto-started vendor_server_160 on {hoisted_addr}")
            else:
                raise RuntimeError(
                    f"Vendor server unavailable at {hoisted_addr}. "
                    "Start it first (for example: ./vendor_server/build/vendor_server_160 artifacts/model_weights.bin 50052), "
                    "or set PPFD_ABLATION_AUTOSTART=1 and use default PPFD_HOISTED_ADDR=127.0.0.1:50052."
                )

        hoisted_client = _build_160_client()
        hoisted_values = _measure_rotation_us(hoisted_client, x_test)
        hoisted_stats = _stats(hoisted_values)

        naive_values: list[float]
        methodology = "estimated"

        naive_addr = os.environ.get("PPFD_NAIVE_ADDR")
        if naive_addr:
            try:
                artifacts = REPO_ROOT / "artifacts"
                naive_client = BankClient(
                    naive_addr,
                    weights_path=str(artifacts / "model_weights.bin"),
                    public_key_path=str(artifacts / "public_key_160.bin"),
                    secret_key_path=str(artifacts / "secret_key_160.bin"),
                    use_tls=False,
                    wrapper_module="seal_wrapper_160",
                    grpc_max_message_length=384 * 1024,
                )
                naive_values = _measure_rotation_us(naive_client, x_test)
                methodology = "measured-via-endpoint"
            except Exception:
                naive_values = [v * (NAIVE_ROTATIONS / HOISTED_ROTATIONS) for v in hoisted_values]
                methodology = "estimated-linear-rotation-model"
        else:
            # No direct naive endpoint exists in the current server API.
            # Estimate naive cost by scaling measured hoisted rotation time by
            # the rotation-count ratio (255 sequential / 8 hoisted rotations).
            naive_values = [v * (NAIVE_ROTATIONS / HOISTED_ROTATIONS) for v in hoisted_values]
            methodology = "estimated-linear-rotation-model"

        naive_stats = _stats(naive_values)
        speedup = naive_stats["mean"] / hoisted_stats["mean"] if hoisted_stats["mean"] > 0 else 0.0

        csv_path = results_dir / "ablation_hoisting.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["method", "n_rotations", "mean_rotation_us", "std_rotation_us", "p99_rotation_us"])
            writer.writerow([
                "naive",
                NAIVE_ROTATIONS,
                f"{naive_stats['mean']:.6f}",
                f"{naive_stats['std']:.6f}",
                f"{naive_stats['p99']:.6f}",
            ])
            writer.writerow([
                "hoisted",
                HOISTED_ROTATIONS,
                f"{hoisted_stats['mean']:.6f}",
                f"{hoisted_stats['std']:.6f}",
                f"{hoisted_stats['p99']:.6f}",
            ])

        details_path = results_dir / "ablation_methodology.json"
        details_path.write_text(
            json.dumps(
                {
                    "methodology": methodology,
                    "measure_rounds": MEASURE_ROUNDS,
                    "warmup_rounds": WARMUP_ROUNDS,
                    "hoisted_rotations": HOISTED_ROTATIONS,
                    "naive_rotations": NAIVE_ROTATIONS,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        print(f"[ablation] methodology: {methodology}")
        print(f"[ablation] wrote {csv_path}")
        print(f"[ablation] wrote {details_path}")
        print(f"Hoisted speedup vs naive: {speedup:.2f}x")
        return 0
    finally:
        if managed_proc is not None:
            _terminate_proc(managed_proc)


if __name__ == "__main__":
    raise SystemExit(main())
