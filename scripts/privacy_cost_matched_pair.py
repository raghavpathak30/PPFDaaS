#!/usr/bin/env python3
"""§5.8 Phase 3 (this session) — architecture-matched privacy-cost measurement.

AUDIT.md documented that the existing artifacts/privacy_cost_analysis.json
latency delta conflates two things: (a) the 200-bit vs 160-bit modulus chain,
and (b) an entirely different server architecture (vendor_server_main's
decrypt-capable, legacy CKKSContext/inference_service.cpp vs vendor_server_160's
eval-only EvalContext160/inference_service_160.cpp -- different RPC service
code, provisioning state machine, and Phase 2 concurrency fix).

This script isolates (a) alone, using vendor_server/build/benchmark
(CKKSContext, 200-bit) and vendor_server/build/benchmark_160 (CKKSContext160,
160-bit): both are secret-key-holding, OUT-OF-TCB, local-circuit-only
benchmark binaries that share the same rotation_hoisting.{h,cpp} functions and
the same --strategy=fold|bsgs|naive dispatch. The ONLY designed difference
between them is ctx.params.coeff_modulus (200 bits / 4 primes vs 160 bits / 3
primes). strategy=fold is used because that is the deployed strategy.

Methodology: each binary's own in-band parity gate (against a plaintext
oracle) must pass before its samples are used. Raw per-iteration latencies
are dumped via --samples-out and combined here for median/IQR/bootstrap-CI
(matching tests/benchmark_comparison.py §5.3 Part C) and a Mann-Whitney U test
(§5.3 Part E) on the two distributions.

Writes artifacts/privacy_cost_matched_pair.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats as scipy_stats

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
BENCH_200 = REPO_ROOT / "vendor_server" / "build" / "benchmark"
BENCH_160 = REPO_ROOT / "vendor_server" / "build" / "benchmark_160"
OUTPUT = ARTIFACTS / "privacy_cost_matched_pair.json"

STRATEGY = "fold"  # the deployed reduction strategy
PPFD_BENCHMARK_ROUNDS = "1000"  # overrides each binary's default n=100

BOOTSTRAP_RESAMPLES = 10000
BOOTSTRAP_CI = 0.95
BOOTSTRAP_SEED = 20260619


def _bootstrap_ci_mean(arr: np.ndarray, seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = arr.size
    resamples = rng.choice(arr, size=(BOOTSTRAP_RESAMPLES, n), replace=True)
    means = resamples.mean(axis=1)
    alpha = (1.0 - BOOTSTRAP_CI) / 2.0
    lo, hi = np.percentile(means, [alpha * 100.0, (1.0 - alpha) * 100.0])
    return float(lo), float(hi)


def _run(binary: Path, label: str) -> dict:
    if not binary.exists():
        sys.exit(f"FAIL: {binary} not found. Build it: cmake --build {binary.parent} --target {binary.name} -j$(nproc)")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        samples_path = Path(tf.name)
    import os
    env = dict(__import__("os").environ)
    env["PPFD_BENCHMARK_ROUNDS"] = PPFD_BENCHMARK_ROUNDS
    proc = subprocess.run(
        [str(binary), f"--strategy={STRATEGY}", f"--samples-out={samples_path}"],
        capture_output=True, text=True, cwd=str(binary.parent), env=env,
    )
    stdout = proc.stdout
    end = stdout.rfind("}")
    if end == -1:
        sys.exit(f"FAIL: {label} produced no JSON:\n{stdout}\n{proc.stderr}")
    result = json.loads(stdout[: end + 1])
    if not result.get("correctness_passed", False):
        sys.exit(
            f"FAIL: {label} failed the in-band parity gate "
            f"(max_abs_error={result.get('correctness_max_abs_error')}). Refusing to report timings."
        )
    samples = np.array(json.loads(samples_path.read_text()), dtype=np.float64)
    samples_path.unlink(missing_ok=True)
    if samples.size != result["n"]:
        sys.exit(f"FAIL: {label} samples-out size {samples.size} != reported n {result['n']}")
    return {"result": result, "samples": samples}


def _summarize(samples: np.ndarray) -> dict:
    q1, q3 = (float(v) for v in np.percentile(samples, [25, 75]))
    ci_lo, ci_hi = _bootstrap_ci_mean(samples)
    return {
        "n": int(samples.size),
        "mean_us": float(np.mean(samples)),
        "std_us": float(np.std(samples)),
        "median_us": float(np.median(samples)),
        "iqr_us": {"q1": q1, "q3": q3, "iqr": q3 - q1},
        "bootstrap_ci95_mean_us": {"lo": ci_lo, "hi": ci_hi, "n_resamples": BOOTSTRAP_RESAMPLES},
        "p95_us": float(np.percentile(samples, 95)),
        "p99_us": float(np.percentile(samples, 99)),
        "min_us": float(np.min(samples)),
        "max_us": float(np.max(samples)),
    }


def _governor() -> str:
    try:
        return open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip()
    except OSError:
        return "UNKNOWN"


def main() -> int:
    governor = _governor()
    print(f"[privacy_cost_matched_pair] cpu_governor={governor}"
          + (" (WARNING: not 'performance' -- see AUDIT.md Phase 1)" if governor != "performance" else ""))

    print(f"[privacy_cost_matched_pair] running benchmark (200-bit) --strategy={STRATEGY} n={PPFD_BENCHMARK_ROUNDS} ...")
    r200 = _run(BENCH_200, "benchmark(200bit)")
    print(f"[privacy_cost_matched_pair] running benchmark_160 (160-bit) --strategy={STRATEGY} n={PPFD_BENCHMARK_ROUNDS} ...")
    r160 = _run(BENCH_160, "benchmark_160(160bit)")

    s200, s160 = r200["samples"], r160["samples"]
    u_stat, p_value = scipy_stats.mannwhitneyu(s200, s160, alternative="two-sided")

    summary_200 = _summarize(s200)
    summary_160 = _summarize(s160)
    delta_median_us = summary_200["median_us"] - summary_160["median_us"]
    delta_pct = delta_median_us / summary_160["median_us"] * 100.0

    out = {
        "framing": {
            "description": (
                "Architecture-matched §5.8 privacy-cost re-measurement (this session). "
                "Both binaries are local-circuit-only (encrypt..decrypt, no gRPC), share "
                "vendor_server/include/rotation_hoisting.h, and differ ONLY in "
                "ctx.params.coeff_modulus: 200-bit {60,40,40,60} (vendor_server/build/benchmark, "
                "CKKSContext) vs 160-bit {60,40,60} (vendor_server/build/benchmark_160, "
                "CKKSContext160). strategy=fold is the deployed reduction strategy. "
                "Both are OUT OF TCB (hold a live secret key for self-timing only); see "
                "ckks_context.h / ckks_context_160.h."
            ),
            "supersedes": (
                "artifacts/privacy_cost_analysis.json's latency_us field, which measured "
                "vendor_server_main (200-bit, legacy decrypt-capable CKKSContext-in-RPC-service) "
                "vs vendor_server_160 (160-bit, eval-only EvalContext160) -- a different-server-"
                "architecture confound documented in AUDIT.md. That file's bandwidth_bytes and "
                "precision_max_abs_error fields are NOT superseded (they are functions of the "
                "modulus chain, not the binary, and remain valid)."
            ),
            "methodology": "measured",
            "cpu_governor": governor,
            "governor_caveat": (
                "PENDING under 'performance' governor -- no root in this environment, see "
                "AUDIT.md Phase 1. This delta is reported under 'powersave' and should not be "
                "treated as final until re-run under 'performance'."
            ) if governor != "performance" else None,
        },
        "strategy": STRATEGY,
        "200bit": {**summary_200, "correctness_max_abs_error": r200["result"]["correctness_max_abs_error"]},
        "160bit": {**summary_160, "correctness_max_abs_error": r160["result"]["correctness_max_abs_error"]},
        "delta": {
            "median_us": delta_median_us,
            "pct_delta_vs_160bit_median": delta_pct,
            "mann_whitney_u": float(u_stat),
            "mann_whitney_p_value": float(p_value),
            "significant_at_p05": bool(p_value < 0.05),
        },
        "key_finding": (
            f"Architecture-matched (same codebase, same rotation_hoisting.cpp, same "
            f"strategy=fold, differ only in coeff_modulus): one additional 40-bit RNS prime "
            f"(200-bit vs 160-bit chain) costs +{delta_median_us:.1f}us ({delta_pct:.1f}%) "
            f"median latency (Mann-Whitney p={p_value:.2e}), measured under cpu_governor="
            f"'{governor}'. This supersedes the deployed-binary delta, which mixed this cost "
            f"with an unrelated server-architecture difference -- see "
            f"artifacts/privacy_cost_analysis.json's 'deployed_cross_architecture_e2e_delta_DEPRECATED' "
            f"field for that figure, kept for transparency, not for citation."
        ),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nWrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
