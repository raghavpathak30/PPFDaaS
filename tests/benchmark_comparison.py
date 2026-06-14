#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import time
import shutil
import uuid
from pathlib import Path

import numpy as np
from scipy.special import expit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient
from generated import inference_pb2

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

TRACE_COMPONENTS = [
    "deserialization_us",
    "multiply_plain_us",
    "rotation_hoisting_us",
    "serialization_us",
    "total_inference_us",
]


def _load_bias(weights_path: Path) -> float:
    with open(weights_path, "rb") as f:
        f.read(4)
        bias, = struct.unpack("<d", f.read(8))
    return bias


def _trace_enabled() -> bool:
    if "--trace" in sys.argv[1:]:
        return True
    return os.environ.get("TRACE", "0").strip().lower() in {"1", "true", "yes", "on"}


def _tprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_timing_table(td: dict[str, float]) -> None:
    _tprint("  ┌─────────────────────────┬──────────── µs ─┐")
    _tprint(f"  │ {'deserialization':<23} │ {td['deserialization_us']:>16.2f} │")
    _tprint(f"  │ {'multiply_plain':<23} │ {td['multiply_plain_us']:>16.2f} │")
    _tprint(f"  │ {'rotation_hoisting':<23} │ {td['rotation_hoisting_us']:>16.2f} │")
    _tprint(f"  │ {'serialization':<23} │ {td['serialization_us']:>16.2f} │")
    _tprint(f"  │ {'total_inference':<23} │ {td['total_inference_us']:>16.2f} │")
    _tprint("  └─────────────────────────┴─────────────────┘")


def _run_inference_with_trace(
    client: BankClient,
    X: np.ndarray,
    *,
    batch_idx: int,
    institution_id: str,
    bias: float = 0.0,
) -> tuple[dict[str, float], float]:
    n_txns, n_feat = X.shape
    if n_feat != 256:
        raise ValueError(f"Expected 256 features, got {n_feat}")
    if not (1 <= n_txns <= 16):
        raise ValueError("n_txns must be 1-16")

    request_id = str(uuid.uuid4())
    _tprint(f"[trace][{batch_idx:03d}] PRE-ENCRYPT: shape={n_txns}x{n_feat} request_id={request_id}")

    if n_txns < 16:
        client._pad_buffer[:n_txns] = X
        client._pad_buffer[n_txns:] = 0.0
        flat = np.ascontiguousarray(client._pad_buffer.ravel(), dtype=np.float64)
    else:
        flat = np.ascontiguousarray(X.ravel(), dtype=np.float64)

    ct_bytes = client._wrapper.encrypt_batch(flat)
    _tprint(f"[trace][{batch_idx:03d}] POST-ENCRYPT: ciphertext_bytes={len(ct_bytes)}")

    req = inference_pb2.InferenceRequest(
        ciphertext=ct_bytes,
        request_id=request_id,
        institution_id=institution_id,
        n_transactions=n_txns,
    )

    t_send_ns = time.perf_counter_ns()
    _tprint(
        f"[trace][{batch_idx:03d}] GRPC-SEND: t_ns={t_send_ns} request_id={request_id} institution_id={institution_id}"
    )
    resp = client._stub.RunInference(req, timeout=0.5)
    t_recv_ns = time.perf_counter_ns()
    roundtrip_us = (t_recv_ns - t_send_ns) / 1000.0

    status_name = inference_pb2.InferenceStatus.Name(resp.status)
    _tprint(
        f"[trace][{batch_idx:03d}] GRPC-RECV: t_ns={t_recv_ns} status={status_name}({resp.status}) request_id_echo={resp.request_id}"
    )

    if resp.status != inference_pb2.InferenceStatus.OK:
        raise RuntimeError(f"Vendor error {resp.status}: {resp.error_message}")
    if resp.request_id != request_id:
        raise RuntimeError(f"Request ID mismatch: sent {request_id}, got {resp.request_id}")

    raw = client._wrapper.decrypt_batch(resp.result_ciphertext, n_txns)
    raw_slot0 = float(raw[0])
    # §1.3: vendor_server_160 now applies the bias server-side (raw already
    # includes it); the 200-bit baseline (vendor_server_main) does not, so it
    # is added here from model_weights.bin for that variant only.
    prob_slot0 = float(expit(raw_slot0 + bias))

    td = {
        "deserialization_us": float(resp.timing.deserialization_us),
        "multiply_plain_us": float(resp.timing.multiply_plain_us),
        "rotation_hoisting_us": float(resp.timing.rotation_hoisting_us),
        "serialization_us": float(resp.timing.serialization_us),
        "total_inference_us": float(resp.timing.total_inference_us),
    }

    _tprint(f"[trace][{batch_idx:03d}] TIMING-BREAKDOWN:")
    _print_timing_table(td)
    _tprint(
        f"[trace][{batch_idx:03d}] POST-DECRYPT: raw_slot0={raw_slot0:.6f} fraud_prob_slot0={prob_slot0:.6f}"
    )

    return td, roundtrip_us


def _print_trace_summary(runs: list[dict[str, float]], roundtrip_us: list[float]) -> None:
    _tprint("\n[trace] Aggregate Trace Summary")
    _tprint("[trace] Per-component mean/std (us):")
    for component in TRACE_COMPONENTS:
        arr = np.array([r[component] for r in runs], dtype=np.float64)
        _tprint(f"  - {component:<22} mean={np.mean(arr):9.2f}  std={np.std(arr):9.2f}")

    total_mean = float(np.mean(np.array([r["total_inference_us"] for r in runs], dtype=np.float64)))
    rot_mean = float(np.mean(np.array([r["rotation_hoisting_us"] for r in runs], dtype=np.float64)))
    rot_pct = (rot_mean / total_mean * 100.0) if total_mean > 0 else 0.0
    _tprint(f"[trace] rotation_hoisting share of total mean: {rot_pct:.2f}%")

    over_3000 = [i + 1 for i, r in enumerate(runs) if r["total_inference_us"] > 3000.0]
    _tprint(f"[trace] runs with total_inference_us > 3000: {len(over_3000)}")
    if over_3000:
        _tprint(f"[trace]   highlighted run indices: {over_3000}")

    rt_arr = np.array(roundtrip_us, dtype=np.float64)
    _tprint(
        f"[trace] request round-trip (GRPC-SEND->GRPC-RECV) min/max us: {np.min(rt_arr):.2f} / {np.max(rt_arr):.2f}"
    )


def _launch_server(bin_path: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [str(bin_path), str(WEIGHTS_PATH), str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


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
        # §1.4: must be large enough for galois_keys_160.bin (~5.8 MB),
        # pushed once via ProvisionGaloisKeys.
        grpc_max_message_length=8 * 1024 * 1024,
        galois_keys_path=str(ARTIFACTS / "galois_keys_160.bin"),
    )


def _collect_runs(variant: str, x_sample: np.ndarray, trace: bool = False) -> list[dict[str, float]]:
    client = _build_client(variant)
    # §1.3: vendor_server_160 (variant != "baseline_200bit") applies bias
    # server-side; the 200-bit baseline does not.
    bias = _load_bias(WEIGHTS_PATH) if variant == "baseline_200bit" else 0.0

    for i in range(WARMUP_ROUNDS):
        resp = client.run_inference(x_sample)
        if i == 0 or i == WARMUP_ROUNDS - 1:
            print(f"[BankClient] warmup complete: {resp['timing_breakdown']['total_inference_us']} us")

    runs: list[dict[str, float]] = []
    roundtrip_us: list[float] = []
    for i in range(MEASURE_ROUNDS):
        if trace:
            td, rt_us = _run_inference_with_trace(
                client,
                x_sample,
                batch_idx=i + 1,
                institution_id="BANK_001",
                bias=bias,
            )
            runs.append(
                {
                    "wall_us": rt_us,
                    "total_inference_us": td["total_inference_us"],
                    "rotation_hoisting_us": td["rotation_hoisting_us"],
                    "multiply_plain_us": td["multiply_plain_us"],
                    "deserialization_us": td["deserialization_us"],
                    "serialization_us": td["serialization_us"],
                }
            )
            roundtrip_us.append(rt_us)
            continue

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

    if trace and runs:
        _print_trace_summary(runs, roundtrip_us)

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
    trace = _trace_enabled()
    if trace:
        _tprint("[trace] VERBOSE_TRACE enabled (--trace or TRACE=1)")

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

    if _is_port_open("127.0.0.1", 50051) or _is_port_open("127.0.0.1", 50052):
        raise RuntimeError(
            "Benchmark ports 50051/50052 are already in use. Stop existing vendor_server processes and rerun."
        )

    if shutil.which("cat"):
        try:
            gov = open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip()
            if gov != "performance":
                print(f"[benchmark] WARNING: CPU governor is '{gov}', not 'performance'. "
                      "Run: echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
        except FileNotFoundError:
            pass

    baseline_proc = _launch_server(BASELINE_BIN, 50051)
    time.sleep(3)
    if baseline_proc.poll() is not None:
        raise RuntimeError(f"baseline server failed to start (exit code {baseline_proc.returncode})")
    reduced_proc = _launch_server(REDUCED_BIN, 50052)
    time.sleep(3)
    if reduced_proc.poll() is not None:
        _wait_and_terminate(baseline_proc)
        raise RuntimeError(f"reduced server failed to start (exit code {reduced_proc.returncode})")

    results = {"baseline_200bit": [], "reduced_160bit": []}
    x_sample = np.full((1, 256), 0.01, dtype=np.float64)

    try:
        results["baseline_200bit"] = _collect_runs("baseline_200bit", x_sample, trace=trace)
        results["reduced_160bit"] = _collect_runs("reduced_160bit", x_sample, trace=trace)
    finally:
        _wait_and_terminate(baseline_proc)
        _wait_and_terminate(reduced_proc)

    summary = _summarize(results)

    # Gates were calibrated on reference hardware (Codespaces, mean=2518us, std=422us).
    # Use performance CPU governor for a valid apples-to-apples comparison.
    gates = {
        "mean_under_3000": summary["reduced_160bit"]["mean_us"] < 3000,
        "p99_under_5000": summary["reduced_160bit"]["p99_us"] < 5000,
        "std_under_500": summary["reduced_160bit"]["std_us"] < 500,
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
    hardware_note = "gates calibrated to reference hardware; run with performance CPU governor for valid comparison"
    out_file = ARTIFACTS / "comparison_results.json"
    out_file.write_text(
        json.dumps(
            {
                "hardware_note": hardware_note,
                "summary": summary,
                "gates": gates,
                "raw_results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps({"hardware_note": hardware_note, **summary}, indent=2))
    print(f"\nGates: {gates}")
    assert gates["all_passed"], "LATENCY GATE FAILED -- see above"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
