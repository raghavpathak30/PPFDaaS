import numpy as np, joblib

from sklearn.linear_model import LogisticRegression

from sklearn.metrics import roc_auc_score

from pathlib import Path

N_TOP_LINEAR   = 256
N_TOP_INTERACT = 32
N_INTERACT     = 496
N_PAD          = 16
N_FEATURES_D2  = 512

def build_degree2_features(
        X: np.ndarray,
        top_linear_idx: np.ndarray,
        top_interact_idx: np.ndarray
) -> np.ndarray:
    """
    Build the 512-element feature vector per transaction:

      Slots   0–255: X[:, top_linear_idx]            (linear terms)
      Slots 256–751: pairwise X[:,i]*X[:,j] for top-32 pairs
      Slots 752–767: zero-padding to reach 512
    """
    X_lin = X[:, top_linear_idx]
    pairs = []
    for i in range(N_TOP_INTERACT):
        for j in range(i + 1, N_TOP_INTERACT):
            pairs.append(X[:, top_interact_idx[i]] * X[:, top_interact_idx[j]])
    X_inter = np.column_stack(pairs)
    pad_cols = np.zeros((X.shape[0], N_FEATURES_D2 - N_TOP_LINEAR - N_INTERACT),
                         dtype=np.float64)
    return np.hstack([X_lin, X_inter, pad_cols])

def linearize_degree2(model_artifacts_dir='artifacts') -> float:
    """
    Fit degree-2 logistic regression. Returns AUC.

    Loads X_train, X_test, y_train, y_test from saved npy files.
    """
    print('[Degree2] Loading saved train/test splits...')
    X_train = np.load(f'{model_artifacts_dir}/X_train.npy')
    X_test  = np.load(f'{model_artifacts_dir}/X_test.npy')
    y_train = np.load(f'{model_artifacts_dir}/y_train.npy')
    y_test  = np.load(f'{model_artifacts_dir}/y_test.npy')

    d1_weights = np.load(f'{model_artifacts_dir}/weights.npy')
    top_linear_idx = np.load(f'{model_artifacts_dir}/feature_idx.npy')

    top32_local = np.argsort(np.abs(d1_weights))[-N_TOP_INTERACT:]
    top_interact_idx = top_linear_idx[top32_local]

    print(f'[Degree2] Building {N_FEATURES_D2}-feature polynomial expansion...')
    X_tr_d2 = build_degree2_features(X_train, top_linear_idx, top_interact_idx)
    X_te_d2 = build_degree2_features(X_test,  top_linear_idx, top_interact_idx)
    print(f'[Degree2] X_tr_d2 shape: {X_tr_d2.shape}  X_te_d2: {X_te_d2.shape}')

    lr = LogisticRegression(max_iter=1000, C=1.0, solver='saga',
                             n_jobs=-1, random_state=42)
    lr.fit(X_tr_d2, y_train)
    probs = lr.predict_proba(X_te_d2)[:, 1]
    auc = roc_auc_score(y_test, probs)
    print(f'[Degree2] Logistic Regression AUC: {auc:.4f} (target >= 0.96)')
    if auc < 0.96:
        raise AssertionError(f'Degree-2 AUC {auc:.4f} < 0.96 — increase C or add features')

    w_512 = lr.coef_[0].astype(np.float64)
    bias  = float(lr.intercept_[0])
    assert w_512.shape == (N_FEATURES_D2,), f'Expected (512,), got {w_512.shape}'

    from serialize_degree2_weights import write_degree2_weights_bin

    write_degree2_weights_bin(w_512, bias,
                              f'{model_artifacts_dir}/degree2_weights.bin')
    np.save(f'{model_artifacts_dir}/degree2_linear_idx.npy',  top_linear_idx)
    np.save(f'{model_artifacts_dir}/degree2_interact_idx.npy', top_interact_idx)
    joblib.dump(lr, f'{model_artifacts_dir}/degree2_model.pkl')
    print(f'[Degree2] All artifacts saved. AUC: {auc:.4f}')
    return auc
