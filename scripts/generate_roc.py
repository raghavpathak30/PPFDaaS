#!/usr/bin/env python3
from __future__ import annotations

import struct
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.special import expit
from sklearn.metrics import auc, roc_curve


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
RESULTS = REPO_ROOT / "results"


def _load_depth1_weights(path: Path) -> tuple[np.ndarray, float]:
    raw = path.read_bytes()
    if len(raw) != 2060:
        raise RuntimeError(f"model_weights.bin must be exactly 2060 bytes, got {len(raw)}")

    n_features = struct.unpack_from("<I", raw, 0)[0]
    if n_features != 256:
        raise RuntimeError(f"Expected n_features=256 in model_weights.bin, got {n_features}")

    bias = struct.unpack_from("<d", raw, 4)[0]
    weights = np.array(struct.unpack_from("<256d", raw, 12), dtype=np.float64)
    return weights, float(bias)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)

    xgb_model = joblib.load(ARTIFACTS / "xgb_model.pkl")
    x_test = np.load(ARTIFACTS / "X_test.npy")
    y_test = np.load(ARTIFACTS / "y_test.npy")
    feature_idx = np.load(ARTIFACTS / "feature_idx.npy").astype(np.int64)
    _ = joblib.load(ARTIFACTS / "scaler.pkl")

    weights, bias = _load_depth1_weights(ARTIFACTS / "model_weights.bin")

    xgb_probs = xgb_model.predict_proba(x_test)[:, 1]
    depth1_logits = x_test[:, feature_idx] @ weights + bias
    depth1_probs = expit(depth1_logits)

    fpr_xgb, tpr_xgb, _ = roc_curve(y_test, xgb_probs)
    fpr_lr, tpr_lr, _ = roc_curve(y_test, depth1_probs)

    auc_xgb = float(auc(fpr_xgb, tpr_xgb))
    auc_lr = float(auc(fpr_lr, tpr_lr))

    fig, ax = plt.subplots(figsize=(7.5, 6))
    ax.plot(fpr_xgb, tpr_xgb, linewidth=2.2, label=f"XGBoost (AUC={auc_xgb:.4f})")
    ax.plot(fpr_lr, tpr_lr, linewidth=2.2, label=f"Depth-1 LR (AUC={auc_lr:.4f})")
    ax.plot([0, 1], [0, 1], linestyle="--", linewidth=1.2, color="gray", label="Random")

    ax.set_title("ROC Comparison: XGBoost vs HE-Compatible Depth-1 LR")
    ax.set_xlabel("False Positive Rate")
    ax.set_ylabel("True Positive Rate")
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right")

    png_path = RESULTS / "roc_comparison.png"
    pdf_path = RESULTS / "roc_comparison.pdf"
    fig.tight_layout()
    fig.savefig(png_path, dpi=180)
    fig.savefig(pdf_path)

    print(f"[roc] XGBoost AUC: {auc_xgb:.6f}")
    print(f"[roc] Depth-1 LR AUC: {auc_lr:.6f}")
    print(f"[roc] wrote {png_path}")
    print(f"[roc] wrote {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
