from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.special import expit
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from serialize_weights import write_model_weights_bin


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


def validate_and_gate(weights, bias, X_test, y_test) -> tuple[str, float]:
    idx = np.load(ARTIFACTS_DIR / "feature_idx.npy")
    logits = X_test[:, idx] @ weights + bias
    probs = expit(logits)
    auc = roc_auc_score(y_test, probs)
    print(f"[linearize] Depth-1 AUC: {auc:.4f}")
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

    # Step 2: fit initial model on all available features
    lr_full = LogisticRegression(
        max_iter=2000,
        C=1.0,
        solver="saga",
        tol=1e-4,
        random_state=42,
    )
    lr_full.fit(X_train, y_train)

    # Step 3 + 4: coefficient extraction and top-256 selection
    coef = lr_full.coef_[0]
    top_idx = np.argsort(np.abs(coef))[-256:]

    # Step 5: refit on selected features only
    lr_top = LogisticRegression(
        max_iter=2000,
        C=1.0,
        solver="saga",
        tol=1e-4,
        random_state=42,
    )
    lr_top.fit(X_train[:, top_idx], y_train)

    # Step 6: extract linear weights and bias
    weights = lr_top.coef_[0].astype(np.float64)
    bias = float(lr_top.intercept_[0])

    # Step 7: test AUC on selected features
    test_probs = lr_top.predict_proba(X_test[:, top_idx])[:, 1]
    auc = float(roc_auc_score(y_test, test_probs))
    print(f"[linearize] test_auc={auc:.6f}")

    # Step 8..10: serialize and save artifacts
    write_model_weights_bin(weights, bias, str(ARTIFACTS_DIR / "model_weights.bin"))
    np.save(ARTIFACTS_DIR / "weights.npy", weights)
    np.save(ARTIFACTS_DIR / "feature_idx.npy", top_idx.astype(np.int64))

    # Step 11: scaler should already exist from training
    if not (ARTIFACTS_DIR / "scaler.pkl").exists():
        raise FileNotFoundError("Missing artifacts/scaler.pkl from train_xgboost.py")

    # Step 12: verify required artifacts and binary contract size
    required = [
        ARTIFACTS_DIR / "model_weights.bin",
        ARTIFACTS_DIR / "weights.npy",
        ARTIFACTS_DIR / "feature_idx.npy",
        ARTIFACTS_DIR / "scaler.pkl",
    ]
    for path in required:
        if not path.exists():
            raise FileNotFoundError(f"Missing required artifact: {path}")
    if (ARTIFACTS_DIR / "model_weights.bin").stat().st_size != 2060:
        raise RuntimeError("artifacts/model_weights.bin must be exactly 2060 bytes")

    print("[linearize] artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
