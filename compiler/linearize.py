from __future__ import annotations

from pathlib import Path
import warnings

import numpy as np
from scipy.special import expit
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from serialize_weights import write_model_weights_bin

# Suppress convergence warnings—typical for imbalanced fraud data
warnings.filterwarnings("ignore", message=".*max_iter.*convergence.*")


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


def validate_and_gate(weights, bias, X_test, y_test) -> tuple[str, float]:
    logits = X_test @ weights + bias
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

    # Step 2: fit on all 256 available features
    lr = LogisticRegression(
        max_iter=2000,
        C=1.0,
        solver="lbfgs",
        tol=1e-4,
        random_state=42,
    )
    lr.fit(X_train, y_train)

    # Step 3: extract linear weights and bias
    weights = lr.coef_[0].astype(np.float64)
    bias = float(lr.intercept_[0])

    # Step 4: test AUC
    test_probs = lr.predict_proba(X_test)[:, 1]
    auc = float(roc_auc_score(y_test, test_probs))
    print(f"[linearize] test_auc={auc:.6f}")

    # Step 5: validate via alternate gate logic
    _, gate_auc = validate_and_gate(weights, bias, X_test, y_test)
    assert abs(gate_auc - auc) < 1e-6, f"AUC mismatch: {auc:.6f} vs {gate_auc:.6f}"

    # Step 6: serialize and save artifacts
    write_model_weights_bin(weights, bias, str(ARTIFACTS_DIR / "model_weights.bin"))
    np.save(ARTIFACTS_DIR / "weights.npy", weights)

    # Step 7: scaler should already exist from training
    if not (ARTIFACTS_DIR / "scaler.pkl").exists():
        raise FileNotFoundError("Missing artifacts/scaler.pkl from train_xgboost.py")

    # Step 8: verify required artifacts and binary contract size
    required = [
        ARTIFACTS_DIR / "model_weights.bin",
        ARTIFACTS_DIR / "weights.npy",
        ARTIFACTS_DIR / "scaler.pkl",
    ]
    for path in required:
        if not path.exists():
            raise FileNotFoundError(f"Missing required artifact: {path}")
    # 256 weights × 8 bytes + 1 bias × 8 bytes + 4-byte header = 2060 bytes
    if (ARTIFACTS_DIR / "model_weights.bin").stat().st_size != 2060:
        raise RuntimeError("artifacts/model_weights.bin must be exactly 2060 bytes")

    print("[linearize] artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
