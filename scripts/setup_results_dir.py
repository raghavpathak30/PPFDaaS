#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
RESULTS = REPO_ROOT / "results"

REQUIRED_ARTIFACTS = [
    "model_weights.bin",
    "degree2_weights.bin",
    "feature_idx.npy",
    "weights.npy",
    "xgb_model.pkl",
    "scaler.pkl",
    "X_test.npy",
    "y_test.npy",
    "comparison_results.json",
]


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)

    readme = RESULTS / "README.md"
    readme.write_text(
        """# Results Artifacts

This directory stores benchmark and research outputs used for validation and demo.

- latency_breakdown.csv: per-run timing breakdown for 160-bit warm inference.
- latency_summary.json: p50/p95/p99 summary derived from latency_breakdown.csv.
- ablation_hoisting.csv: naive-vs-hoisted rotation comparison table.
- ablation_methodology.json: notes on how ablation measurements were produced.
- roc_comparison.png: ROC comparison figure for quick viewing.
- roc_comparison.pdf: publication-quality ROC comparison figure.

Use scripts/ to regenerate artifacts deterministically.
""",
        encoding="utf-8",
    )

    print("[setup] results directory ready")
    print(f"[setup] wrote {readme}")
    print("\nApril 22 Demo Checklist")
    print("-----------------------")

    missing = 0
    for name in REQUIRED_ARTIFACTS:
        path = ARTIFACTS / name
        ok = path.exists()
        print(f"[{ 'x' if ok else ' ' }] {name}")
        if not ok:
            missing += 1

    if missing:
        print(f"\n[setup] Missing {missing} required artifact(s).")
        return 1

    print("\n[setup] All required artifacts are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
