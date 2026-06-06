#!/usr/bin/env python3
from __future__ import annotations

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


def _start_server() -> subprocess.Popen:
    server_bin = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
    weights = REPO_ROOT / "artifacts" / "model_weights.bin"
    if not server_bin.exists():
        raise FileNotFoundError(f"Missing server binary: {server_bin}")

    if _is_port_open("127.0.0.1", 50052):
        raise RuntimeError(
            "Port 50052 is already in use. Stop the existing server process before running demo_e2e.py."
        )

    proc = subprocess.Popen(
        [str(server_bin), str(weights), "50052"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_remote(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_port_open(host, port, timeout=1.0):
            return True
        time.sleep(0.5)
    return False


def _wait_ready(proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited early with code {proc.returncode}")
        if _is_port_open("127.0.0.1", 50052):
            time.sleep(0.1)
            if proc.poll() is not None:
                raise RuntimeError(f"Server exited after bind attempt with code {proc.returncode}")
            return
        time.sleep(0.2)
    raise TimeoutError("Server did not become ready within timeout")


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main() -> int:
    server_addr = os.environ.get("SERVER_ADDR")   # set in the container; unset = local demo
    proc = None
    if server_addr:
        host, _, port = server_addr.partition(":")
        if not _wait_remote(host, int(port or "50052")):
            raise TimeoutError(f"server {server_addr} not reachable")
    else:
        server_addr = "localhost:50052"
        proc = _start_server()
        _wait_ready(proc)

    try:
        artifacts = REPO_ROOT / "artifacts"
        x_test = np.load(artifacts / "X_test.npy")

        client = BankClient(
            server_addr,
            weights_path=str(artifacts / "model_weights.bin"),
            public_key_path=str(artifacts / "public_key_160.bin"),
            secret_key_path=str(artifacts / "secret_key_160.bin"),
            use_tls=False,
            wrapper_module="seal_wrapper_160",
            grpc_max_message_length=384 * 1024,
        )

        print("\nTX | Encrypted | Vendor Saw Plaintext | Score | Fraud? | Total us")
        print("---|-----------|----------------------|-------|--------|---------")

        for i in range(5):
            x = np.asarray(x_test[i], dtype=np.float64).reshape(1, 256)
            resp = client.run_inference(x)
            score = float(resp["fraud_probabilities"][0])
            total_us = int(resp["timing_breakdown"]["total_inference_us"])
            fraud = "YES" if score > 0.5 else "NO"
            print(f"{i+1:>2} | YES       | NO                   | {score:0.4f} | {fraud:>6} | {total_us:>7}")

        print("\nVendor NEVER saw plaintext data")
        return 0
    finally:
        if proc is not None:
            _stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())