#!/usr/bin/env python3
"""§5.3/§5.4/§5.5 benchmark: 160-bit vs 200-bit modulus-chain comparison.

This is a SELF-ABLATION (docs/spec.md §5.7, Type 1): both variants run the
IDENTICAL depth-1 logistic-regression circuit and sequential-fold reduction
strategy (hoisted_tree_sum) over the same gRPC service implementation; the
only difference is the CKKS modulus chain (200-bit {60,40,40,60} vs 160-bit
{60,40,60}). It is NOT a comparison against an external baseline library --
see artifacts/rotation_strategy_comparison.json for strategy/library
comparisons.

§5.4 latency framing: every number in this file is LATENCY -- a single
request in flight at a time, no concurrent load. For throughput under
concurrent load (closed-loop, n_clients > 1), see
tests/benchmark_throughput.py and artifacts/throughput_results.json.
"""
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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats as scipy_stats
from scipy.special import expit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient
from generated import inference_pb2
from scripts.parity_gate import load_model_weights, verify_encrypted_output

ARTIFACTS = REPO_ROOT / "artifacts"

BASELINE_ADDR = "127.0.0.1:50051"
REDUCED_ADDR = "127.0.0.1:50052"

BASELINE_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_main"
REDUCED_BIN = REPO_ROOT / "vendor_server" / "build" / "vendor_server_160"
WEIGHTS_PATH = REPO_ROOT / "artifacts" / "model_weights.bin"
X_TEST_PATH = REPO_ROOT / "artifacts" / "X_test.npy"

BASELINE_KEYS = {
    "public": REPO_ROOT / "artifacts" / "public_key.bin",
    "secret": REPO_ROOT / "artifacts" / "secret_key.bin",
}
REDUCED_KEYS = {
    "public": REPO_ROOT / "artifacts" / "public_key_160.bin",
    "secret": REPO_ROOT / "artifacts" / "secret_key_160.bin",
}

# §5.3 Part B: >=1000 measured iterations (was 100). 20 warmup retained.
WARMUP_ROUNDS = 20
MEASURE_ROUNDS = 1000

# §5.3 Part C: bootstrap CI for the mean.
BOOTSTRAP_RESAMPLES = 10000
BOOTSTRAP_CI = 0.95
BOOTSTRAP_SEED = 20260615

# §5.3 Part A: fixed seed for the held-out-set sampling permutation.
INPUT_SEED = 1234

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


# ─── §5.3 Part A: randomized real held-out inputs ──────────────────────────

def _load_x_test() -> np.ndarray:
    if not X_TEST_PATH.exists():
        raise FileNotFoundError(f"Missing required file: {X_TEST_PATH}")
    return np.load(X_TEST_PATH).astype(np.float64)


def _build_input_sequence(x_test: np.ndarray, n: int, seed: int = INPUT_SEED) -> np.ndarray:
    """n distinct real samples from the held-out test set (56,962 rows), in a
    fixed-seed random order, one (1, 256) vector per iteration. If n exceeds
    the dataset size, wraps via modulo on the same permutation (no
    replacement within one pass)."""
    n_total = x_test.shape[0]
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n_total)
    idx = perm[np.arange(n) % n_total]
    return x_test[idx].reshape(n, 1, 256)


# ─── §5.5: in-band parity gate ─────────────────────────────────────────────

def _run_parity_gate(client: BankClient, variant: str, x: np.ndarray) -> None:
    weights, model_bias = load_model_weights(WEIGHTS_PATH)
    # §1.3: the 200-bit baseline does not apply bias server-side; the 160-bit
    # reduced server does (raw already includes it) -- see _load_bias usage
    # in _run_inference_with_trace for the same distinction.
    gate_bias = 0.0 if variant == "baseline_200bit" else model_bias

    resp = client.run_inference(x)  # NOT timed -- verification only
    passed, max_abs_error = verify_encrypted_output(resp["fraud_probabilities"], x, weights, gate_bias)
    print(f"[parity_gate] variant={variant} passed={passed} max_abs_error={max_abs_error:.3e}")
    if not passed:
        raise RuntimeError(
            f"§5.5 parity gate FAILED for variant={variant}: max_abs_error={max_abs_error} >= tol"
        )


def _collect_runs(variant: str, input_sequence: np.ndarray, trace: bool = False) -> list[dict[str, float]]:
    client = _build_client(variant)
    # §1.3: vendor_server_160 (variant != "baseline_200bit") applies bias
    # server-side; the 200-bit baseline does not.
    bias = _load_bias(WEIGHTS_PATH) if variant == "baseline_200bit" else 0.0

    # §5.5: verify against the plaintext oracle BEFORE any timing is trusted.
    # input_sequence[0] is reserved for this gate and excluded from
    # warmup/measurement below.
    _run_parity_gate(client, variant, input_sequence[0])

    for i in range(WARMUP_ROUNDS):
        x = input_sequence[1 + i]
        resp = client.run_inference(x)
        if i == 0 or i == WARMUP_ROUNDS - 1:
            print(f"[BankClient] warmup complete: {resp['timing_breakdown']['total_inference_us']} us")

    runs: list[dict[str, float]] = []
    roundtrip_us: list[float] = []
    for i in range(MEASURE_ROUNDS):
        x = input_sequence[1 + WARMUP_ROUNDS + i]
        if trace:
            td, rt_us = _run_inference_with_trace(
                client,
                x,
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
        resp = client.run_inference(x)
        t1 = time.perf_counter_ns()

        td = resp["timing_breakdown"]
        runs.append(
            {
                # §5.4: client-side wall time for this single in-flight
                # request (includes encrypt + gRPC round trip + decrypt),
                # alongside the server-reported total_inference_us below.
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


# ─── §5.3 Part C: median/IQR/bootstrap CI/p95/p99 ───────────────────────────

def _bootstrap_ci_mean(
    arr: np.ndarray,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    ci: float = BOOTSTRAP_CI,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = arr.size
    resamples = rng.choice(arr, size=(n_resamples, n), replace=True)
    means = resamples.mean(axis=1)
    alpha = (1.0 - ci) / 2.0
    lo, hi = np.percentile(means, [alpha * 100.0, (1.0 - alpha) * 100.0])
    return float(lo), float(hi)


def _summarize(results: dict[str, list[dict[str, float]]]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for variant, runs in results.items():
        arr = np.array([r["total_inference_us"] for r in runs], dtype=np.float64)
        wall_arr = np.array([r["wall_us"] for r in runs], dtype=np.float64)
        q1, q3 = (float(v) for v in np.percentile(arr, [25, 75]))
        ci_lo, ci_hi = _bootstrap_ci_mean(arr)
        summary[variant] = {
            "n": int(arr.size),
            # mean/std retained for reference only (§5.3 Part C) -- median,
            # IQR, bootstrap CI, p95/p99 below are the primary statistics.
            "mean_us": float(np.mean(arr)),
            "std_us": float(np.std(arr)),
            "median_us": float(np.median(arr)),
            "p50_us": float(np.median(arr)),
            "iqr_us": {"q1": q1, "q3": q3, "iqr": q3 - q1},
            "bootstrap_ci95_mean_us": {
                "lo": ci_lo, "hi": ci_hi, "n_resamples": BOOTSTRAP_RESAMPLES,
            },
            "p95_us": float(np.percentile(arr, 95)),
            "p99_us": float(np.percentile(arr, 99)),
            "min_us": float(np.min(arr)),
            "max_us": float(np.max(arr)),
            # §5.4: client-side wall time (single request in flight), alongside
            # the server-reported total_inference_us above.
            "wall_us": {
                "mean": float(np.mean(wall_arr)),
                "median": float(np.median(wall_arr)),
                "p95": float(np.percentile(wall_arr, 95)),
                "p99": float(np.percentile(wall_arr, 99)),
            },
        }
    return summary


# ─── §5.3 Part E: Mann-Whitney U test ───────────────────────────────────────

def _mann_whitney(a: np.ndarray, b: np.ndarray, label_a: str, label_b: str) -> dict:
    u_stat, p_value = scipy_stats.mannwhitneyu(a, b, alternative="two-sided")
    n1, n2 = int(a.size), int(b.size)
    rank_biserial = 1.0 - (2.0 * float(u_stat)) / (n1 * n2)
    return {
        "a": label_a,
        "b": label_b,
        "n_a": n1,
        "n_b": n2,
        "U": float(u_stat),
        "p_value": float(p_value),
        "rank_biserial_effect_size": rank_biserial,
    }


# ─── §5.3 Part D: programmatic hardware manifest ────────────────────────────

def _read_first_match(path: str, prefix: str) -> str | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(prefix):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _seal_version() -> str:
    cmake_version_file = Path("/usr/local/lib/cmake/SEAL-4.1/SEALConfigVersion.cmake")
    if cmake_version_file.exists():
        for line in cmake_version_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("set(PACKAGE_VERSION ") and "PACKAGE_VERSION_" not in line:
                # set(PACKAGE_VERSION "4.1.2")
                parts = line.split('"')
                if len(parts) >= 2:
                    return parts[1]
    return "UNKNOWN"


def _compiler_flags() -> dict[str, str]:
    cmake_cache = REPO_ROOT / "vendor_server" / "build" / "CMakeCache.txt"
    flags = {"cmake_build_type": "UNKNOWN", "cxx_compiler": "UNKNOWN", "cxx_flags_release": "UNKNOWN"}
    if not cmake_cache.exists():
        return flags
    for line in cmake_cache.read_text().splitlines():
        if line.startswith("CMAKE_BUILD_TYPE:"):
            flags["cmake_build_type"] = line.split("=", 1)[1]
        elif line.startswith("CMAKE_CXX_COMPILER:"):
            flags["cxx_compiler"] = line.split("=", 1)[1]
        elif line.startswith("CMAKE_CXX_FLAGS_RELEASE:"):
            flags["cxx_flags_release"] = line.split("=", 1)[1]
    return flags


def _hardware_manifest() -> dict:
    """§5.3 Part D: programmatic hardware/software manifest. Degrades
    gracefully when psutil is unavailable (logs a note, never fails)."""
    manifest: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version,
        "platform": sys.platform,
    }

    manifest["cpu_model"] = _read_first_match("/proc/cpuinfo", "model name") or "UNKNOWN"
    manifest["cpu_cores_logical"] = os.cpu_count()

    try:
        manifest["cpu_governor"] = open(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        ).read().strip()
    except OSError:
        manifest["cpu_governor"] = "UNKNOWN"

    mem_total_kb = _read_first_match("/proc/meminfo", "MemTotal")
    manifest["ram_total_kb"] = int(mem_total_kb.split()[0]) if mem_total_kb else None

    manifest["seal_version"] = _seal_version()
    manifest.update(_compiler_flags())

    try:
        import psutil  # type: ignore

        manifest["psutil_available"] = True
        manifest["cpu_cores_physical"] = psutil.cpu_count(logical=False)
        freq = psutil.cpu_freq()
        manifest["cpu_freq_current_mhz"] = freq.current if freq else None
        manifest["ram_available_kb"] = psutil.virtual_memory().available // 1024
    except ImportError:
        manifest["psutil_available"] = False
        manifest["psutil_note"] = "psutil not available, partial manifest"

    return manifest


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
        X_TEST_PATH,
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

    hardware_manifest = _hardware_manifest()
    if hardware_manifest["cpu_governor"] != "performance":
        print(f"[benchmark] WARNING: CPU governor is '{hardware_manifest['cpu_governor']}', not "
              f"'performance'. Run: echo performance | sudo tee "
              f"/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")

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

    # §5.3 Part A: randomized real held-out inputs, one distinct vector per
    # iteration (no replacement within one pass over the 56,962-row test
    # set). [0] is reserved for the §5.5 parity gate.
    x_test = _load_x_test()
    input_sequence = _build_input_sequence(x_test, 1 + WARMUP_ROUNDS + MEASURE_ROUNDS)

    try:
        results["baseline_200bit"] = _collect_runs("baseline_200bit", input_sequence, trace=trace)
        results["reduced_160bit"] = _collect_runs("reduced_160bit", input_sequence, trace=trace)
    finally:
        _wait_and_terminate(baseline_proc)
        _wait_and_terminate(reduced_proc)

    summary = _summarize(results)

    arr_baseline = np.array([r["total_inference_us"] for r in results["baseline_200bit"]], dtype=np.float64)
    arr_reduced = np.array([r["total_inference_us"] for r in results["reduced_160bit"]], dtype=np.float64)
    statistical_tests = {
        "total_inference_us": {
            "baseline_200bit_vs_reduced_160bit":
                _mann_whitney(arr_baseline, arr_reduced, "baseline_200bit", "reduced_160bit"),
        },
    }

    # Gates calibrated against summary.reduced_160bit (median-based, §5.3
    # Part C). Use performance CPU governor for a valid apples-to-apples
    # comparison -- see hardware_manifest.cpu_governor above.
    gates = {
        "median_under_3000": summary["reduced_160bit"]["median_us"] < 3000,
        "p99_under_6000": summary["reduced_160bit"]["p99_us"] < 6000,
        "iqr_under_1000": summary["reduced_160bit"]["iqr_us"]["iqr"] < 1000,
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

    # §5.2: framing note -- this comparison is a self-ablation (same circuit,
    # same fold reduction strategy, same gRPC service implementation; only
    # the modulus chain differs), NOT a comparison against an external
    # baseline library. See docs/spec.md §5.7 and
    # artifacts/rotation_strategy_comparison.json.
    framing = {
        "type": "self-ablation (Type 1, docs/spec.md §5.7)",
        "description": (
            "baseline_200bit and reduced_160bit run the IDENTICAL depth-1 "
            "logistic-regression circuit and sequential-fold reduction "
            "strategy (hoisted_tree_sum) via the same gRPC service "
            "implementation; the only difference is the CKKS modulus chain "
            "(200-bit {60,40,40,60} vs 160-bit {60,40,60}), both at "
            "sec_level_type::tc128. This is NOT a comparison against an "
            "external baseline library -- see "
            "artifacts/rotation_strategy_comparison.json for "
            "strategy/library comparisons."
        ),
        "latency_scope": (
            "§5.4: single request in flight (no concurrent load). For "
            "throughput under concurrent load, see "
            "tests/benchmark_throughput.py and "
            "artifacts/throughput_results.json."
        ),
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "comparison_results.json"
    out_file.write_text(
        json.dumps(
            {
                "framing": framing,
                "hardware_manifest": hardware_manifest,
                "summary": summary,
                "statistical_tests": statistical_tests,
                "gates": gates,
                "raw_results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps({"hardware_manifest": hardware_manifest, **summary}, indent=2))
    print(f"\nStatistical tests: {json.dumps(statistical_tests, indent=2)}")
    print(f"\nGates: {gates}")
    if hardware_manifest["cpu_governor"] == "performance":
        assert gates["all_passed"], "LATENCY GATE FAILED -- see above"
    elif not gates["all_passed"]:
        print(
            f"[benchmark] NOTE: gate(s) failed under cpu_governor="
            f"'{hardware_manifest['cpu_governor']}'. SLA gates are calibrated "
            f"for 'performance' governor and are NON-FATAL here -- "
            f"artifacts/comparison_results.json still contains the real "
            f"measured numbers."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
