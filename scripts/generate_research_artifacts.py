#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bank_client.bank_client import BankClient


WARMUP_ROUNDS = 20
MEASURE_ROUNDS = 1000


def _percentiles(values: list[float]) -> dict[str, float]:
    arr = np.array(values, dtype=np.float64)
    return {
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
    }


def main() -> int:
    artifacts_dir = REPO_ROOT / "artifacts"
    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    comparison_path = artifacts_dir / "comparison_results.json"
    if not comparison_path.exists():
        raise FileNotFoundError(f"Missing required file: {comparison_path}")
    _ = json.loads(comparison_path.read_text(encoding="utf-8"))

    x_test_path = artifacts_dir / "X_test.npy"
    if not x_test_path.exists():
        raise FileNotFoundError(f"Missing required file: {x_test_path}")
    x_test = np.load(x_test_path)

    client = BankClient(
        "localhost:50052",
        public_key_path=str(artifacts_dir / "public_key_160.bin"),
        secret_key_path=str(artifacts_dir / "secret_key_160.bin"),
        use_tls=False,
        wrapper_module="seal_wrapper_160",
        # §1.4: must be large enough for galois_keys_160.bin (~5.8 MB),
        # pushed once via ProvisionGaloisKeys.
        grpc_max_message_length=8 * 1024 * 1024,
        galois_keys_path=str(artifacts_dir / "galois_keys_160.bin"),
    )

    sample = np.asarray(x_test[0], dtype=np.float64).reshape(1, 256)
    for _ in range(WARMUP_ROUNDS):
        client.run_inference(sample)

    rows: list[dict[str, float]] = []
    for run_id in range(1, MEASURE_ROUNDS + 1):
        sample_idx = (run_id - 1) % x_test.shape[0]
        x = np.asarray(x_test[sample_idx], dtype=np.float64).reshape(1, 256)
        resp = client.run_inference(x)
        td = resp["timing_breakdown"]
        rows.append(
            {
                "run_id": float(run_id),
                "deserialization_us": float(td["deserialization_us"]),
                "multiply_plain_us": float(td["multiply_plain_us"]),
                "rotation_hoisting_us": float(td["rotation_hoisting_us"]),
                "serialization_us": float(td["serialization_us"]),
                "total_inference_us": float(td["total_inference_us"]),
            }
        )

    csv_path = results_dir / "latency_breakdown.csv"
    fieldnames = [
        "run_id",
        "deserialization_us",
        "multiply_plain_us",
        "rotation_hoisting_us",
        "serialization_us",
        "total_inference_us",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    summary = {
        "deserialization_us": _percentiles([r["deserialization_us"] for r in rows]),
        "multiply_plain_us": _percentiles([r["multiply_plain_us"] for r in rows]),
        "rotation_hoisting_us": _percentiles([r["rotation_hoisting_us"] for r in rows]),
        "serialization_us": _percentiles([r["serialization_us"] for r in rows]),
        "total_inference_us": _percentiles([r["total_inference_us"] for r in rows]),
    }

    print(json.dumps(summary, indent=2))

    summary_path = results_dir / "latency_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"[research] wrote {csv_path}")
    print(f"[research] wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
