#!/usr/bin/env python3
"""
scripts/reproduce_all.py — single-command artifact regeneration for PPFDaaS.

Usage:
    python3 scripts/reproduce_all.py            # run full pipeline
    python3 scripts/reproduce_all.py --dry-run  # print plan, exit 0
    python3 scripts/reproduce_all.py --from 5   # resume from step N

Prerequisites (build must exist before running this script):
    cmake -B vendor_server/build -S vendor_server -DCMAKE_BUILD_TYPE=Release
    cmake --build vendor_server/build --parallel

Steps that require a running server are marked with [SERVER].
The script starts/stops vendor_server_160 and vendor_server_main automatically.

Expected total wall time (fresh run on the reference 20-core machine):
    Steps 1-4   (model + keys + verification): ~5-10 min
    Steps 5-6   (benchmark_comparison + throughput): ~45-60 min
    Steps 7-13  (secondary measurements + figures): ~20-30 min
    Total: ~75-100 min
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
SCRIPTS = REPO_ROOT / "scripts"
COMPILER = REPO_ROOT / "compiler"
TESTS = REPO_ROOT / "tests"
BUILD = REPO_ROOT / "vendor_server" / "build"

SERVER_160_BIN = BUILD / "vendor_server_160"
SERVER_200_BIN = BUILD / "vendor_server_main"
MODEL_WEIGHTS = ARTIFACTS / "model_weights.bin"
PORT_160 = 50052
PORT_200 = 50051

STEPS = [
    {
        "n": 1,
        "label": "Train XGBoost + fit LR surrogate",
        "server": False,
        "wall_time_est": "3-5 min",
        "commands": [
            [sys.executable, str(COMPILER / "train_xgboost.py")],
            [sys.executable, str(COMPILER / "train_logistic_regression.py")],
        ],
        "outputs": [
            ARTIFACTS / "X_train.npy",
            ARTIFACTS / "X_test.npy",
            ARTIFACTS / "y_train.npy",
            ARTIFACTS / "y_test.npy",
            ARTIFACTS / "model_weights.bin",
            ARTIFACTS / "weights.npy",
            ARTIFACTS / "scaler.pkl",
            ARTIFACTS / "poly.pkl",
            ARTIFACTS / "xgb_model.pkl",
        ],
    },
    {
        "n": 2,
        "label": "Generate HE keys (160-bit)",
        "server": False,
        "wall_time_est": "2-4 min",
        "commands": [
            [sys.executable, str(COMPILER / "gen_keys_160.py")],
        ],
        "outputs": [
            ARTIFACTS / "secret_key_160.bin",
            ARTIFACTS / "public_key_160.bin",
            ARTIFACTS / "galois_keys_160.bin",
            ARTIFACTS / "model_weights.bin",
        ],
    },
    {
        "n": 3,
        "label": "Structural verification (verify_all.py)",
        "server": False,
        "wall_time_est": "< 1 min",
        "commands": [
            [sys.executable, str(TESTS / "verify_all.py")],
        ],
        "outputs": [],
    },
    {
        "n": 4,
        "label": "C++ unit tests (ctest)",
        "server": False,
        "wall_time_est": "< 1 min",
        "commands": [
            ["ctest", "--test-dir", str(BUILD), "--output-on-failure"],
        ],
        "outputs": [],
    },
    {
        "n": 5,
        "label": "Latency benchmark (benchmark_comparison.py) [SERVER]",
        "server": True,
        "wall_time_est": "40-50 min (1000 iterations × 2 variants)",
        "commands": [
            [sys.executable, str(TESTS / "benchmark_comparison.py")],
        ],
        "outputs": [ARTIFACTS / "comparison_results.json"],
    },
    {
        "n": 6,
        "label": "Throughput benchmark (benchmark_throughput.py) [SERVER]",
        "server": True,
        "wall_time_est": "10-15 min (30s × 4 concurrency levels + occupancy sweep)",
        "commands": [
            [sys.executable, str(TESTS / "benchmark_throughput.py")],
        ],
        "outputs": [ARTIFACTS / "throughput_results.json"],
    },
    {
        "n": 7,
        "label": "Rotation strategy comparison [SERVER]",
        "server": True,
        "wall_time_est": "3-5 min",
        "commands": [
            [sys.executable, str(SCRIPTS / "rotation_strategy_comparison.py")],
        ],
        "outputs": [ARTIFACTS / "rotation_strategy_comparison.json"],
    },
    {
        "n": 8,
        "label": "Ciphertext wire sizes",
        "server": False,
        "wall_time_est": "< 1 min",
        "commands": [
            [sys.executable, str(SCRIPTS / "measure_wire_size.py")],
        ],
        "outputs": [ARTIFACTS / "wire_sizes.json"],
    },
    {
        "n": 9,
        "label": "Amortization table (from throughput artifacts)",
        "server": False,
        "wall_time_est": "< 1 min",
        "commands": [
            [sys.executable, str(SCRIPTS / "generate_amortization_table.py")],
        ],
        "outputs": [ARTIFACTS / "amortization_table.json"],
    },
    {
        "n": 10,
        "label": "Privacy cost analysis [SERVER]",
        "server": True,
        "wall_time_est": "< 1 min (single-inference parity gate × 2 servers)",
        "commands": [
            [sys.executable, str(SCRIPTS / "privacy_cost_analysis.py")],
        ],
        "outputs": [ARTIFACTS / "privacy_cost_analysis.json"],
    },
    {
        "n": 11,
        "label": "Ablation methodology (naive vs fold) [SERVER]",
        "server": True,
        "wall_time_est": "5-10 min (100 naive + 100 fold iterations)",
        "commands": [
            [sys.executable, str(SCRIPTS / "generate_ablation.py")],
        ],
        "outputs": [ARTIFACTS / "ablation_methodology.json"],
    },
    {
        "n": 12,
        "label": "Execution matrix [SERVER]",
        "server": True,
        "wall_time_est": "3-5 min",
        "commands": [
            [sys.executable, str(SCRIPTS / "build_execution_matrix.py")],
        ],
        "outputs": [ARTIFACTS / "execution_matrix.json"],
    },
    {
        "n": 13,
        "label": "Research figures and tables (generate_research_artifacts.py) [SERVER]",
        "server": True,
        "wall_time_est": "< 2 min",
        "commands": [
            [sys.executable, str(SCRIPTS / "generate_research_artifacts.py")],
        ],
        "outputs": [],
    },
]


def _print_plan() -> None:
    print("=" * 70)
    print("PPFDaaS reproduce_all.py — full pipeline plan")
    print("=" * 70)
    print()
    print("Prerequisites:")
    print("  cmake -B vendor_server/build -S vendor_server -DCMAKE_BUILD_TYPE=Release")
    print("  cmake --build vendor_server/build --parallel")
    print("  data/creditcard.csv present (see scripts/fetch_creditcard_dataset.sh)")
    print()
    print(f"{'Step':<4}  {'Label':<54}  {'Est. time'}")
    print("-" * 80)
    for s in STEPS:
        print(f"  {s['n']:<3}  {s['label']:<54}  {s['wall_time_est']}")
    print("-" * 80)
    print()
    print("Steps marked [SERVER] require vendor_server_160 (and in some cases")
    print("vendor_server_main). This script starts/stops them automatically.")
    print()
    print("Artifacts written:")
    for s in STEPS:
        for o in s["outputs"]:
            rel = o.relative_to(REPO_ROOT)
            print(f"  {s['n']:>2}. {rel}")
    print()


def _check_prerequisites() -> None:
    required_files = [
        (REPO_ROOT / "data" / "creditcard.csv", "data/creditcard.csv — run scripts/fetch_creditcard_dataset.sh"),
        (SERVER_160_BIN, "vendor_server/build/vendor_server_160 — run cmake --build vendor_server/build --parallel"),
        (SERVER_200_BIN, "vendor_server/build/vendor_server_main — same cmake --build command"),
    ]
    missing = []
    for path, hint in required_files:
        if not path.exists():
            missing.append(f"  MISSING: {hint}")
    if missing:
        print("ERROR: prerequisites not met:")
        for m in missing:
            print(m)
        sys.exit(1)


def _start_server(binary: Path, port: int, label: str) -> subprocess.Popen:
    model = str(MODEL_WEIGHTS)
    env = {**os.environ, "PPFD_GRPC_THREADS": "8"}
    proc = subprocess.Popen(
        [str(binary), model, str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Poll until port is open (max 30 s)
    import socket
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError(f"{label} did not bind on port {port} within 30 s")
    print(f"  [server] {label} started (pid={proc.pid}, port={port})")
    return proc


def _stop_server(proc: subprocess.Popen, label: str) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"  [server] {label} stopped")


def _run_step(step: dict) -> None:
    for cmd in step["commands"]:
        print(f"  $ {' '.join(str(c) for c in cmd)}")
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        if result.returncode != 0:
            print(f"\nFAILED at step {step['n']}: {step['label']}")
            print(f"Command returned exit code {result.returncode}")
            sys.exit(result.returncode)
    for output_path in step["outputs"]:
        if not output_path.exists():
            print(f"\nFAILED: step {step['n']} did not produce expected output: {output_path}")
            sys.exit(1)
        # JSON outputs: validate parse
        if output_path.suffix == ".json":
            try:
                with open(output_path) as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                print(f"\nFAILED: {output_path} is not valid JSON: {e}")
                sys.exit(1)
    print(f"  [ok] step {step['n']} complete")


def main() -> int:
    parser = argparse.ArgumentParser(description="PPFDaaS full artifact reproduction pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Print plan and exit 0")
    parser.add_argument("--from", dest="from_step", type=int, default=1,
                        metavar="N", help="Start from step N (1-13)")
    args = parser.parse_args()

    _print_plan()

    if args.dry_run:
        print("Dry-run mode: no commands executed.")
        return 0

    _check_prerequisites()

    steps_to_run = [s for s in STEPS if s["n"] >= args.from_step]
    if not steps_to_run:
        print(f"No steps to run (--from {args.from_step} exceeds step count)")
        return 1

    server_160: subprocess.Popen | None = None
    server_200: subprocess.Popen | None = None
    servers_running = False

    try:
        for step in steps_to_run:
            print()
            print(f"[Step {step['n']}/13] {step['label']}  (est. {step['wall_time_est']})")

            if step["server"] and not servers_running:
                print("  Starting servers...")
                server_160 = _start_server(SERVER_160_BIN, PORT_160, "vendor_server_160")
                server_200 = _start_server(SERVER_200_BIN, PORT_200, "vendor_server_main")
                servers_running = True
            elif not step["server"] and servers_running:
                print("  Stopping servers (not needed for this step)...")
                if server_160:
                    _stop_server(server_160, "vendor_server_160")
                    server_160 = None
                if server_200:
                    _stop_server(server_200, "vendor_server_main")
                    server_200 = None
                servers_running = False

            _run_step(step)

    finally:
        if server_160 and server_160.poll() is None:
            _stop_server(server_160, "vendor_server_160")
        if server_200 and server_200.poll() is None:
            _stop_server(server_200, "vendor_server_main")

    print()
    print("=" * 70)
    print("All steps complete. Artifacts:")
    for s in STEPS:
        for o in s["outputs"]:
            if o.exists():
                rel = o.relative_to(REPO_ROOT)
                size = o.stat().st_size
                print(f"  {rel}  ({size:,} bytes)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
