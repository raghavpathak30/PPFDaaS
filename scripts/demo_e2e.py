#!/usr/bin/env python3
from __future__ import annotations

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


def _start_server() -> subprocess.Popen:
    server_bin = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
    weights = REPO_ROOT / "artifacts" / "model_weights.bin"
    if not server_bin.exists():
        raise FileNotFoundError(f"Missing server binary: {server_bin}")

    proc = subprocess.Popen(
        [str(server_bin), str(weights), "50052"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def _wait_ready(proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            print(f"[server] {line.rstrip()}")
            if "listening on" in line:
                return
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited early with code {proc.returncode}")
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
    proc = _start_server()
    try:
        _wait_ready(proc)

        artifacts = REPO_ROOT / "artifacts"
        x_test = np.load(artifacts / "X_test.npy")

        client = BankClient(
            "localhost:50052",
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
        _stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
