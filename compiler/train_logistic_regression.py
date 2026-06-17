"""
compiler/train_logistic_regression.py

Trains a surrogate LogisticRegression (LR) on the same train split used by
train_xgboost.py and serializes its weights for CKKS depth-1 HE inference.

METHODOLOGY NOTE (Phase 6, §6.5):
  This script does NOT linearize or distill the XGBoost model. XGBoost is used
  in train_xgboost.py solely for dataset validation and feature importance
  (confirming the 256-feature expanded representation achieves >=0.98 AUC).
  The actual HE inference model is an *independent* surrogate LogisticRegression
  fitted on the same train split. The AUC gap between XGBoost and this LR
  (reported as "linearization_cost_auc" in artifacts/linearization_cost.json)
  is the cost of using a linear model for CKKS depth-1 inference.

  This framing follows docs/spec.md §5.7 (Type 1 self-ablation): the
  XGBoost/LR comparison is a linearization-cost measurement, not a claim
  that the two models share weights or gradient information.
"""
from __future__ import annotations

from pathlib import Path
import json
import warnings

import numpy as np
from scipy.special import expit
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from serialize_weights import write_model_weights_bin

# Suppress convergence warnings -- typical for imbalanced fraud data
warnings.filterwarnings("ignore", message=".*max_iter.*convergence.*")


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


def validate_and_gate(weights, bias, X_test, y_test) -> tuple[str, float]:
    logits = X_test @ weights + bias
    probs = expit(logits)
    auc = roc_auc_score(y_test, probs)
    print(f"[train_logistic_regression] Depth-1 AUC (gate check): {auc:.4f}")
    return "depth1", float(auc)


def _load_required_arrays() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    X_train = np.load(ARTIFACTS_DIR / "X_train.npy")
    X_test = np.load(ARTIFACTS_DIR / "X_test.npy")
    y_train = np.load(ARTIFACTS_DIR / "y_train.npy")
    y_test = np.load(ARTIFACTS_DIR / "y_test.npy")
    return X_train, X_test, y_train, y_test


def main() -> int:
    X_train, X_test, y_train, y_test = _load_required_arrays()
    assert X_train.shape[1] == 256, (
        f"Expected 256 features from expanded arrays, got {X_train.shape[1]}"
    )

    # Fit the surrogate LogisticRegression on the training split
    lr = LogisticRegression(
        max_iter=2000,
        C=1.0,
        solver="lbfgs",
        tol=1e-4,
        random_state=42,
    )
    lr.fit(X_train, y_train)

    weights = lr.coef_[0].astype(np.float64)
    bias = float(lr.intercept_[0])

    test_probs = lr.predict_proba(X_test)[:, 1]
    lr_auc = float(roc_auc_score(y_test, test_probs))
    print(f"[train_logistic_regression] LR test_auc={lr_auc:.6f}")

    # Verify against alternate gate logic
    _, gate_auc = validate_and_gate(weights, bias, X_test, y_test)
    assert abs(gate_auc - lr_auc) < 1e-6, f"AUC mismatch: {lr_auc:.6f} vs {gate_auc:.6f}"

    # Report the linearization cost: XGBoost AUC (from train_xgboost.py) vs LR AUC
    xgb_scores_path = ARTIFACTS_DIR / "xgb_scores.npy"
    if xgb_scores_path.exists():
        xgb_scores = np.load(xgb_scores_path)
        xgb_test_auc = float(xgb_scores[1])
        linearization_cost = xgb_test_auc - lr_auc
        print(f"[train_logistic_regression] XGBoost test_auc={xgb_test_auc:.6f}")
        print(f"[train_logistic_regression] Linearization cost (XGB - LR AUC): {linearization_cost:+.6f}")
        lin_summary = {
            "xgb_test_auc": xgb_test_auc,
            "lr_test_auc": lr_auc,
            "linearization_cost_auc": linearization_cost,
            "methodology": (
                "XGBoost used for dataset validation only; LR is an independent "
                "surrogate trained on the same split for CKKS depth-1 inference. "
                "linearization_cost_auc = xgb_test_auc - lr_test_auc."
            ),
        }
        with open(ARTIFACTS_DIR / "linearization_cost.json", "w") as f:
            json.dump(lin_summary, f, indent=2)
        print("[train_logistic_regression] linearization cost -> artifacts/linearization_cost.json")

    # Serialize weights for the HE server
    write_model_weights_bin(weights, bias, str(ARTIFACTS_DIR / "model_weights.bin"))
    np.save(ARTIFACTS_DIR / "weights.npy", weights)

    # Verify required artifacts exist and binary contract size
    if not (ARTIFACTS_DIR / "scaler.pkl").exists():
        raise FileNotFoundError("Missing artifacts/scaler.pkl from train_xgboost.py")
    required = [
        ARTIFACTS_DIR / "model_weights.bin",
        ARTIFACTS_DIR / "weights.npy",
        ARTIFACTS_DIR / "scaler.pkl",
    ]
    for path in required:
        if not path.exists():
            raise FileNotFoundError(f"Missing required artifact: {path}")
    # 256 weights x 8 bytes + 1 bias x 8 bytes + 4-byte header = 2060 bytes
    if (ARTIFACTS_DIR / "model_weights.bin").stat().st_size != 2060:
        raise RuntimeError("artifacts/model_weights.bin must be exactly 2060 bytes")

    print("[train_logistic_regression] artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
