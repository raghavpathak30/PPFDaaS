#!/usr/bin/env python3
"""§5.7 Part B: per-transaction amortization table.

Reads the batch-occupancy sweep already measured by
tests/benchmark_throughput.py (single client, no concurrent load, lanes in
{1, 4, 8, 16}, written to artifacts/throughput_results.json's
"occupancy_sweep") and derives, for each occupancy point:

  - batch_latency_us: median wall-clock latency for one RunInference call
    carrying `lanes` real transactions.
  - per_tx_us: batch_latency_us / lanes -- the amortized per-transaction cost.
  - amortization_factor: per_tx_us(lanes=1) / per_tx_us(lanes) -- how many
    times cheaper, per transaction, this occupancy is relative to a
    single-transaction (lanes=1) request.

Writes artifacts/amortization_table.json. Requires
artifacts/throughput_results.json to already exist (run
tests/benchmark_throughput.py first).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
THROUGHPUT_RESULTS = ARTIFACTS / "throughput_results.json"


def main() -> int:
    if not THROUGHPUT_RESULTS.exists():
        print(f"Missing required file: {THROUGHPUT_RESULTS}")
        print("Run: python3 tests/benchmark_throughput.py first.")
        return 1

    throughput = json.loads(THROUGHPUT_RESULTS.read_text(encoding="utf-8"))
    occupancy_sweep = throughput["occupancy_sweep"]

    baseline = next(row for row in occupancy_sweep if row["lanes"] == 1)
    baseline_per_tx_us = baseline["per_tx_us"]

    rows = []
    for row in occupancy_sweep:
        per_tx_us = row["per_tx_us"]
        rows.append(
            {
                "lanes": row["lanes"],
                "n": row["n"],
                "batch_latency_us": row["batch_latency_us"],
                "per_tx_us": per_tx_us,
                "amortization_factor": baseline_per_tx_us / per_tx_us,
            }
        )
        print(
            f"[amortization] lanes={row['lanes']:2d} "
            f"batch_latency_us={row['batch_latency_us']:9.2f} "
            f"per_tx_us={per_tx_us:9.2f} "
            f"amortization_factor={baseline_per_tx_us / per_tx_us:6.2f}x"
        )

    out = {
        "framing": {
            "description": (
                "§5.7 Part B: per-transaction amortization table, derived from "
                "the single-client batch-occupancy sweep in "
                "artifacts/throughput_results.json's 'occupancy_sweep' "
                "(vendor_server_160, hoisted_tree_sum, 160-bit chain, "
                "PPFD_GRPC_THREADS="
                f"{throughput['ppfd_grpc_threads']}). amortization_factor is "
                "per_tx_us(lanes=1) / per_tx_us(lanes=N): how many times "
                "cheaper, per transaction, a batch of N is relative to a "
                "single-transaction request."
            ),
            "methodology": "measured",
            "source": "artifacts/throughput_results.json#occupancy_sweep",
        },
        "amortization": rows,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "amortization_table.json"
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
