#!/usr/bin/env python3
"""§5.1 / §5.2: self-ablation comparison of the naive (255-rotation, no
hoisting/BSGS) vs hoisted (8-rotation sequential fold) reduction strategies.

Both rows are produced by EXECUTING vendor_server/build/benchmark_160
--strategy=<naive|fold> -- the SAME local CKKS circuit (encrypt,
multiply_plain, rescale, reduction, decrypt), same codebase, same binary, same
hardware, differing ONLY in which reduction strategy is selected. This is a
Type 1 "self-ablation" per docs/spec.md §5.7 (Benchmark Framing), NOT a
comparison against an external baseline library -- for strategy/library
comparisons see artifacts/rotation_strategy_comparison.json.

methodology is always "measured": prior revisions of this script fell back to
"estimated-linear-rotation-model" (naive cost = hoisted cost * 255/8) when no
live naive endpoint was reachable. That estimate has been removed entirely
(§5.1): naive_tree_sum() is now a real --strategy=naive code path in
benchmark_160, so both numbers come from executing the thing they describe.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

HOISTED_ROTATIONS = 8
NAIVE_ROTATIONS = 255

BENCHMARK_BIN = REPO_ROOT / "vendor_server" / "build" / "benchmark_160"


def _run_benchmark(strategy: str, fast: bool) -> dict:
    if not BENCHMARK_BIN.exists():
        raise FileNotFoundError(
            f"Missing {BENCHMARK_BIN}. Build it first: "
            "cmake --build vendor_server/build --target benchmark_160"
        )

    env = dict(os.environ)
    if fast:
        # §5.1: --fast-ablation shrinks benchmark_160's measured-round count
        # (n=20 instead of the compiled-in default of 100) via
        # PPFD_BENCHMARK_ROUNDS. Does not change benchmark_160's default.
        env["PPFD_BENCHMARK_ROUNDS"] = "20"

    proc = subprocess.run(
        [str(BENCHMARK_BIN), f"--strategy={strategy}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"benchmark_160 --strategy={strategy} exited {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

    json_end = proc.stdout.rindex("}") + 1
    result = json.loads(proc.stdout[: json_end])

    if not result.get("correctness_passed"):
        raise RuntimeError(
            f"benchmark_160 --strategy={strategy} FAILED its in-band "
            f"correctness gate: max_abs_error={result.get('correctness_max_abs_error')}"
        )
    return result


def main() -> int:
    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    fast = "--fast-ablation" in sys.argv

    hoisted = _run_benchmark("fold", fast)
    naive = _run_benchmark("naive", fast)

    hoisted_mean = hoisted["latency_us"]["mean"]
    naive_mean = naive["latency_us"]["mean"]
    ratio = naive_mean / hoisted_mean if hoisted_mean > 0 else 0.0

    csv_path = results_dir / "ablation_hoisting.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "method", "n_rotations", "critical_path",
            "mean_latency_us", "std_latency_us", "p99_latency_us",
        ])
        writer.writerow([
            "naive", naive["rotations"], naive["critical_path"],
            f"{naive['latency_us']['mean']:.6f}",
            f"{naive['latency_us']['std']:.6f}",
            f"{naive['latency_us']['p99']:.6f}",
        ])
        writer.writerow([
            "hoisted", hoisted["rotations"], hoisted["critical_path"],
            f"{hoisted['latency_us']['mean']:.6f}",
            f"{hoisted['latency_us']['std']:.6f}",
            f"{hoisted['latency_us']['p99']:.6f}",
        ])

    details_path = results_dir / "ablation_methodology.json"
    details_path.write_text(
        json.dumps(
            {
                "methodology": "measured",
                "source": "vendor_server/build/benchmark_160 --strategy=<naive|fold>",
                "scope": "local CKKS circuit only: encrypt, multiply_plain, "
                         "rescale, reduction, decrypt (no gRPC/serialization)",
                "framing": "Type 1 self-ablation (docs/spec.md §5.7): same "
                           "algorithm family, codebase, binary, and hardware; "
                           "only the reduction strategy differs. NOT a "
                           "comparison against an external baseline library.",
                "fast_ablation": fast,
                "measure_rounds": naive["n"],
                "warmup_rounds": naive["warmup"],
                "hoisted_rotations": hoisted["rotations"],
                "naive_rotations": naive["rotations"],
                "hoisted_critical_path": hoisted["critical_path"],
                "naive_critical_path": naive["critical_path"],
                "hoisted_galois_keygen_us": hoisted["galois_keygen_us"],
                "naive_galois_keygen_us": naive["galois_keygen_us"],
                "hoisted_correctness_max_abs_error": hoisted["correctness_max_abs_error"],
                "naive_correctness_max_abs_error": naive["correctness_max_abs_error"],
                "naive_latency_us": naive["latency_us"],
                "hoisted_latency_us": hoisted["latency_us"],
                "naive_to_hoisted_ratio": ratio,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("[ablation] methodology: measured")
    print(f"[ablation] wrote {csv_path}")
    print(f"[ablation] wrote {details_path}")
    print(
        f"[ablation] self-ablation (160-bit local circuit): naive "
        f"({naive['rotations']} rotations) mean={naive_mean:.2f}us vs hoisted "
        f"({hoisted['rotations']} rotations) mean={hoisted_mean:.2f}us "
        f"-> naive/hoisted ratio={ratio:.2f}x"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
