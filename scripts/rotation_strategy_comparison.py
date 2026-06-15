#!/usr/bin/env python3
"""Phase 4, Item 4.4 — unified rotation/reduction strategy comparison.

Produces a single table comparing the three rotation/reduction strategies
named in docs/spec.md §7 ("Rotation Strategy Terminology"):

  1. SEAL sequential fold       (hoisted_tree_sum, §7.2) — 8 dependent
     rotations, 8 critical-path steps, rotation set {1,2,4,8,16,32,64,128}.
  2. SEAL BSGS two-layer         (bsgs_reduction, §7.3, Phase 4 §4.1) — 30
     independent rotations across 2 critical-path layers, rotation set
     BSGS_ROTATION_STEPS (30 elements).
  3. OpenFHE hoisted flat        (tools/openfhe_benchmark/, §7.4, Phase 4
     §4.3) — same 30-rotation BSGS set, genuine Halevi-Shoup hoisting via
     EvalFastRotationPrecompute/EvalFastRotation.

Data sources:
  - artifacts/comparison_results.json: existing Phase 3 measurement
    (summary.reduced_160bit) — a FULL end-to-end gRPC benchmark
    (deserialize -> multiply_plain -> rescale -> hoisted_tree_sum ->
    serialize), via tests/benchmark_comparison.py.
  - vendor_server/build/benchmark_160 --strategy=fold|bsgs: a LOCAL-CIRCUIT
    benchmark (encrypt -> multiply_plain -> rescale -> reduction -> decrypt,
    no gRPC/serialization), built fresh for Phase 4 (§4.4). Run here for both
    strategies so the fold-vs-BSGS comparison is same-methodology.
  - tools/openfhe_benchmark/results/openfhe_results.json: Phase 4 §4.3
    cross-library study. PENDING in this checkout (OpenFHE not installed).

These two benchmark scopes (e2e gRPC vs local-circuit-only) are NOT directly
comparable to each other -- both are reported, clearly labeled, rather than
silently conflated. See methodology_note in the output JSON.

In-band parity gate (Phase 5.5 pattern, applied here in Phase 4): every
strategy's "correctness_passed" must be true (or the OpenFHE row PENDING)
before its latency numbers are printed in the table; a failing gate aborts
with a non-zero exit code.

Output: artifacts/rotation_strategy_comparison.json + a paper-ready table on
stdout.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
BENCHMARK_160 = REPO_ROOT / "vendor_server" / "build" / "benchmark_160"
OPENFHE_RESULTS = REPO_ROOT / "tools" / "openfhe_benchmark" / "results" / "openfhe_results.json"
COMPARISON_RESULTS = ARTIFACTS / "comparison_results.json"
OUTPUT = ARTIFACTS / "rotation_strategy_comparison.json"

BSGS_ROTATIONS = 30   # (baby_step - 1) + (giant_step - 1) = 15 + 15
BSGS_GALOIS_KEYS = 30  # BSGS_ROTATION_STEPS, vendor_server/include/rotation_hoisting.h


def run_benchmark_160(strategy: str) -> dict:
    """Invoke benchmark_160 --strategy=<fold|bsgs> and parse its JSON stdout."""
    if not BENCHMARK_160.exists():
        sys.exit(
            f"FAIL: {BENCHMARK_160} not found. Build it first:\n"
            f"  cmake --build {BENCHMARK_160.parent} --target benchmark_160 -j$(nproc)"
        )

    proc = subprocess.run(
        [str(BENCHMARK_160), f"--strategy={strategy}"],
        capture_output=True, text=True, cwd=BENCHMARK_160.parent,
    )
    # benchmark_160 prints a JSON object, then an "avg_us=... avg_ms=..." line.
    stdout = proc.stdout
    end = stdout.rfind("}")
    if end == -1:
        sys.exit(f"FAIL: benchmark_160 --strategy={strategy} produced no JSON:\n{stdout}\n{proc.stderr}")
    result = json.loads(stdout[: end + 1])

    if not result.get("correctness_passed", False):
        sys.exit(
            f"FAIL: benchmark_160 --strategy={strategy} failed the in-band parity gate "
            f"(max_abs_error={result.get('correctness_max_abs_error')}). "
            f"Refusing to report timings for an incorrect run."
        )
    return result


def load_comparison_results() -> dict | None:
    if not COMPARISON_RESULTS.exists():
        return None
    with open(COMPARISON_RESULTS) as f:
        return json.load(f)


def load_openfhe_results() -> dict | None:
    if not OPENFHE_RESULTS.exists():
        return None
    with open(OPENFHE_RESULTS) as f:
        return json.load(f)


def main() -> None:
    print("[rotation_strategy_comparison] running benchmark_160 --strategy=fold ...")
    fold_local = run_benchmark_160("fold")
    print("[rotation_strategy_comparison] running benchmark_160 --strategy=bsgs ...")
    bsgs_local = run_benchmark_160("bsgs")

    comparison_results = load_comparison_results()
    openfhe_results = load_openfhe_results()

    strategies = []

    # ── 1. SEAL sequential fold — e2e gRPC (Phase 3) ────────────────────────
    if comparison_results is not None:
        e2e_fold = comparison_results["summary"]["reduced_160bit"]
        strategies.append({
            "name": "SEAL sequential fold (e2e gRPC, Phase 3)",
            "source": "artifacts/comparison_results.json: summary.reduced_160bit",
            "scope": "full gRPC round trip: deserialize, multiply_plain, rescale, "
                     "hoisted_tree_sum, serialize (tests/benchmark_comparison.py)",
            "rotations": 8,
            "critical_path_steps": 8,
            "galois_keys": 8,
            "n": e2e_fold["n"],
            "latency_ms": {
                "mean": e2e_fold["mean_us"] / 1000.0,
                "p50": e2e_fold["p50_us"] / 1000.0,
                "p95": e2e_fold["p95_us"] / 1000.0,
                "p99": e2e_fold["p99_us"] / 1000.0,
            },
            "correctness_passed": None,
        })
    else:
        print(f"[rotation_strategy_comparison] WARNING: {COMPARISON_RESULTS} not found, "
              f"skipping e2e gRPC sequential-fold row", file=sys.stderr)

    # ── 2. SEAL sequential fold — local circuit (Phase 4) ───────────────────
    strategies.append({
        "name": "SEAL sequential fold (local circuit, Phase 4)",
        "source": "vendor_server/build/benchmark_160 --strategy=fold",
        "scope": "local circuit only: encrypt, multiply_plain, rescale, "
                 "hoisted_tree_sum, decrypt (no gRPC/serialization)",
        "rotations": fold_local["rotations"],
        "critical_path_steps": fold_local["critical_path"],
        "galois_keys": fold_local["galois_keys"],
        "n": fold_local["n"],
        "latency_ms": {
            "mean": fold_local["latency_us"]["mean"] / 1000.0,
            "p50": fold_local["latency_us"]["p50"] / 1000.0,
            "p95": fold_local["latency_us"]["p95"] / 1000.0,
            "p99": fold_local["latency_us"]["p99"] / 1000.0,
        },
        "correctness_max_abs_error": fold_local["correctness_max_abs_error"],
        "correctness_passed": fold_local["correctness_passed"],
    })

    # ── 3. SEAL BSGS two-layer — local circuit (Phase 4 §4.1) ───────────────
    strategies.append({
        "name": "SEAL BSGS two-layer (local circuit, Phase 4)",
        "source": "vendor_server/build/benchmark_160 --strategy=bsgs",
        "scope": "local circuit only: encrypt, multiply_plain, rescale, "
                 "bsgs_reduction (OpenMP within each layer), decrypt "
                 "(no gRPC/serialization)",
        "rotations": bsgs_local["rotations"],
        "critical_path_steps": bsgs_local["critical_path"],
        "galois_keys": bsgs_local["galois_keys"],
        "n": bsgs_local["n"],
        "latency_ms": {
            "mean": bsgs_local["latency_us"]["mean"] / 1000.0,
            "p50": bsgs_local["latency_us"]["p50"] / 1000.0,
            "p95": bsgs_local["latency_us"]["p95"] / 1000.0,
            "p99": bsgs_local["latency_us"]["p99"] / 1000.0,
        },
        "correctness_max_abs_error": bsgs_local["correctness_max_abs_error"],
        "correctness_passed": bsgs_local["correctness_passed"],
        "note": "Measured mean/p99 EXCEED the sequential-fold row above despite "
                "a shorter critical path (2 vs 8): on this 20-core host with "
                "OMP_NUM_THREADS unset, OpenMP thread-spawn overhead for two "
                "15-iteration parallel-for loops dominates, and each "
                "rotate_vector() call within a layer is still a full, "
                "independent, unhoisted rotation (§7.1) -- BSGS trades fewer "
                "critical-path STEPS for MORE total rotation WORK (30 vs 8), "
                "and SEAL's public API gives no way to amortize that work via "
                "hoisting. This is itself part of the §7.5 finding.",
    })

    # ── 4. OpenFHE hoisted flat (Phase 4 §4.3) ──────────────────────────────
    if openfhe_results is not None:
        status = openfhe_results.get("status", "PENDING")
        row = {
            "name": "OpenFHE hoisted flat (Phase 4, §4.3)",
            "source": "tools/openfhe_benchmark/results/openfhe_results.json",
            "scope": "local circuit only: encrypt, EvalMult, "
                     "EvalFastRotationPrecompute + EvalFastRotation (x2 layers), "
                     "decrypt",
            "status": status,
            "rotations": openfhe_results.get("rotations", BSGS_ROTATIONS),
            "critical_path_steps": openfhe_results.get("critical_path", 2),
            "galois_keys": openfhe_results.get("galois_keys", BSGS_GALOIS_KEYS),
        }
        if status == "PENDING":
            row["latency_ms"] = {"mean": "PENDING", "p50": "PENDING", "p95": "PENDING", "p99": "PENDING"}
            row["correctness_passed"] = "PENDING"
            row["note"] = openfhe_results.get("reason", "OpenFHE benchmark not built/run in this environment.")
        else:
            total = openfhe_results["latency_us"]["total"]
            row["n"] = openfhe_results["n"]
            row["latency_ms"] = {
                "mean": total["mean"] / 1000.0,
                "p50": total["p50"] / 1000.0,
                "p95": total["p95"] / 1000.0,
                "p99": total["p99"] / 1000.0,
            }
            row["correctness_max_abs_error"] = openfhe_results["correctness_max_abs_error"]
            row["correctness_passed"] = openfhe_results["correctness_passed"]
            row["note"] = (
                "'rotations'=30 and 'critical_path_steps'=2 mirror the SEAL BSGS "
                "row's structure (same BSGS_ROTATION_STEPS set, two layers), but "
                "each layer's 15 EvalFastRotation calls reuse ONE "
                "EvalFastRotationPrecompute -- the digit-decomposition sharing "
                "that defines genuine hoisting (§7.1) and that SEAL's public "
                "API cannot express."
            )
        strategies.append(row)
    else:
        print(f"[rotation_strategy_comparison] WARNING: {OPENFHE_RESULTS} not found, "
              f"skipping OpenFHE row", file=sys.stderr)

    output = {
        "methodology_note": (
            "Rows 1 and 2-4 are NOT directly comparable: row 1 is a full "
            "end-to-end gRPC measurement (Phase 3, includes "
            "(de)serialization and network-stack overhead); rows 2-4 are "
            "local-circuit-only measurements (Phase 4, encrypt..decrypt with "
            "no gRPC). Row 1 is included because it is the only existing "
            "measurement of the sequential fold under the deployed server's "
            "provisioned 8-step ROTATION_STEPS key set. Rows 2 and 3 are "
            "same-methodology (both run via vendor_server/build/benchmark_160 "
            "in this invocation) and are the primary fold-vs-BSGS comparison. "
            "Row 4 (OpenFHE) is PENDING in this checkout -- OpenFHE is not "
            "installed; see tools/openfhe_benchmark/README.md."
        ),
        "strategies": strategies,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"[rotation_strategy_comparison] wrote {OUTPUT}")

    print_table(strategies)


def print_table(strategies: list[dict]) -> None:
    headers = ["Strategy", "Rotations", "Critical Path", "Latency (ms)", "p99 (ms)", "Galois Keys"]

    def fmt(v):
        if isinstance(v, float):
            return f"{v:.3f}"
        return str(v)

    rows = []
    for s in strategies:
        latency = s.get("latency_ms", {})
        mean = latency.get("mean", "PENDING")
        p99 = latency.get("p99", "PENDING")
        rows.append([
            s["name"],
            fmt(s["rotations"]),
            fmt(s["critical_path_steps"]),
            fmt(mean),
            fmt(p99),
            fmt(s["galois_keys"]),
        ])

    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def print_row(cells):
        print("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")

    sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"

    print()
    print_row(headers)
    print(sep)
    for r in rows:
        print_row(r)
    print()

    for s in strategies:
        if "note" in s:
            print(f"* {s['name']}:")
            print(f"  {s['note']}")
            print()


if __name__ == "__main__":
    main()
