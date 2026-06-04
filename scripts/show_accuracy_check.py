#!/usr/bin/env python3
from __future__ import annotations

import json
import struct
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy.special import expit
from sklearn.metrics import average_precision_score, roc_auc_score


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
DATA_PATH = REPO_ROOT / "data" / "creditcard.csv"


def _load_depth1_weights(path: Path) -> tuple[np.ndarray, float]:
    raw = path.read_bytes()
    if len(raw) != 2060:
        raise RuntimeError(f"model_weights.bin must be 2060 bytes, got {len(raw)}")

    n_features = struct.unpack_from("<I", raw, 0)[0]
    if n_features != 256:
        raise RuntimeError(f"Expected n_features=256 in model_weights.bin, got {n_features}")

    bias = struct.unpack_from("<d", raw, 4)[0]
    weights = np.array(struct.unpack_from("<256d", raw, 12), dtype=np.float64)
    return weights, float(bias)


def main() -> int:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Missing dataset: {DATA_PATH}")

    required = [
        ARTIFACTS / "X_test.npy",
        ARTIFACTS / "y_test.npy",
        ARTIFACTS / "xgb_model.pkl",
        ARTIFACTS / "model_weights.bin",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing required artifacts:\n" + "\n".join(f"  - {m}" for m in missing))

    df = pd.read_csv(DATA_PATH)
    if "Class" not in df.columns:
        raise ValueError("Expected target column 'Class' in creditcard.csv")

    total = int(len(df))
    fraud = int((df["Class"] == 1).sum())
    normal = total - fraud
    fraud_pct = (fraud / total) * 100.0 if total > 0 else 0.0

    x_test = np.load(ARTIFACTS / "X_test.npy")
    y_test = np.load(ARTIFACTS / "y_test.npy")

    xgb_model = joblib.load(ARTIFACTS / "xgb_model.pkl")
    xgb_probs = xgb_model.predict_proba(x_test)[:, 1]

    weights, bias = _load_depth1_weights(ARTIFACTS / "model_weights.bin")
    depth1_probs = expit(x_test @ weights + bias)

    report = {
        "dataset": {
            "source": str(DATA_PATH),
            "period_note": "European cardholder transactions over 2 days in September 2013",
            "total_transactions": total,
            "fraud_transactions": fraud,
            "non_fraud_transactions": normal,
            "fraud_rate_percent": round(fraud_pct, 6),
        },
        "evaluation_set": {
            "x_test_shape": [int(x_test.shape[0]), int(x_test.shape[1])],
            "y_test_size": int(y_test.shape[0]),
        },
        "metrics": {
            "xgboost": {
                "roc_auc": float(roc_auc_score(y_test, xgb_probs)),
                "pr_auc": float(average_precision_score(y_test, xgb_probs)),
            },
            "depth1_lr": {
                "roc_auc": float(roc_auc_score(y_test, depth1_probs)),
                "pr_auc": float(average_precision_score(y_test, depth1_probs)),
            },
        },
    }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
