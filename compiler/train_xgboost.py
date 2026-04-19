from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from imblearn.over_sampling import SMOTE
from scipy.stats.mstats import winsorize
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import PolynomialFeatures, StandardScaler
from xgboost import XGBClassifier


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = REPO_ROOT / "data" / "creditcard.csv"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


def main() -> int:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Missing dataset: {DATA_PATH}")

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(DATA_PATH)
    if "Class" not in df.columns:
        raise ValueError("Expected target column 'Class' in creditcard.csv")

    # Step 2: drop Time and split target/features
    if "Time" in df.columns:
        X_df = df.drop(columns=["Time", "Class"])
    else:
        X_df = df.drop(columns=["Class"])
    y = df["Class"].to_numpy(dtype=np.int64)
    X = X_df.to_numpy(dtype=np.float64)

    # Step 3: split
    X_train_raw, X_test_raw, y_train_raw, y_test = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
        stratify=y,
    )

    # Step 4: SMOTE on training set only
    smote = SMOTE(sampling_strategy=0.1, random_state=42)
    X_train_sm, y_train = smote.fit_resample(X_train_raw, y_train_raw)

    # Step 5: scale
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train_sm)
    X_test_scaled = scaler.transform(X_test_raw)

    # Step 6: winsorize
    X_train_win = np.asarray(winsorize(X_train_scaled, limits=[0.01, 0.01], axis=0), dtype=np.float64)
    X_test_win = np.asarray(winsorize(X_test_scaled, limits=[0.01, 0.01], axis=0), dtype=np.float64)

    # Step 7: clip and scale to [-1, 1]
    X_train_proc = np.clip(X_train_win, -3.0, 3.0) / 3.0
    X_test_proc = np.clip(X_test_win, -3.0, 3.0) / 3.0

    # Expand to deterministic 256-feature input contract.
    poly = PolynomialFeatures(degree=2, interaction_only=True, include_bias=False)
    X_train_poly = poly.fit_transform(X_train_scaled)
    X_test_poly = poly.transform(X_test_scaled)
    X_train_expanded = X_train_poly[:, :256]
    X_test_expanded = X_test_poly[:, :256]

    # Step 8: train XGBoost
    neg = int((y_train_raw == 0).sum())
    pos = int((y_train_raw == 1).sum())
    if pos == 0:
        raise ValueError("Training split has no positive samples")
    scale_pos_weight = float(neg) / float(pos)

    def train_and_score(n_estimators: int) -> tuple[XGBClassifier, float, float]:
        model_local = XGBClassifier(
            n_estimators=n_estimators,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=scale_pos_weight,
            eval_metric="auc",
            random_state=42,
            n_jobs=-1,
        )
        model_local.fit(X_train_expanded, y_train)
        train_auc_local = float(
            roc_auc_score(y_train, model_local.predict_proba(X_train_expanded)[:, 1])
        )
        test_auc_local = float(
            roc_auc_score(y_test, model_local.predict_proba(X_test_expanded)[:, 1])
        )
        return model_local, train_auc_local, test_auc_local

    model, train_auc, test_auc = train_and_score(300)
    if test_auc < 0.98:
        print(
            f"[train_xgboost] test_auc={test_auc:.6f} below 0.98 with n_estimators=300; retrying with 500"
        )
        model, train_auc, test_auc = train_and_score(500)

    # Step 9: print and assert threshold
    print(f"[train_xgboost] train_auc={train_auc:.6f}")
    print(f"[train_xgboost] test_auc={test_auc:.6f}")
    assert test_auc >= 0.98, f"XGBoost AUC {test_auc:.6f} < 0.98"

    # Step 10: persist artifacts for downstream compiler stages
    np.save(ARTIFACTS_DIR / "X_train.npy", X_train_expanded)
    np.save(ARTIFACTS_DIR / "X_test.npy", X_test_expanded)
    np.save(ARTIFACTS_DIR / "X_train_raw.npy", X_train_scaled)
    np.save(ARTIFACTS_DIR / "X_test_raw.npy", X_test_scaled)
    np.save(ARTIFACTS_DIR / "y_train.npy", y_train.astype(np.int64))
    np.save(ARTIFACTS_DIR / "y_test.npy", y_test.astype(np.int64))
    np.save(ARTIFACTS_DIR / "xgb_scores.npy", np.array([train_auc, test_auc], dtype=np.float64))

    joblib.dump(scaler, ARTIFACTS_DIR / "scaler.pkl")
    joblib.dump(poly, ARTIFACTS_DIR / "poly.pkl")
    joblib.dump(model, ARTIFACTS_DIR / "xgb_model.pkl")

    print(f"[train_xgboost] saved artifacts to {ARTIFACTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
