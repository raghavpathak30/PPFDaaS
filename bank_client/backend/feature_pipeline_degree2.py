import numpy as np, joblib

from scipy.stats import mstats

from pathlib import Path

N_FEATURES_D2  = 512
N_TOP_LINEAR   = 256
N_TOP_INTERACT = 32

class FeaturePipelineDegree2:
    def __init__(self, scaler_path, linear_idx_path, interact_idx_path):
        self.scaler       = joblib.load(scaler_path)
        self.linear_idx   = np.load(linear_idx_path)
        self.interact_idx = np.load(interact_idx_path)
        assert len(self.linear_idx)   == N_TOP_LINEAR,   "linear_idx must be (256,)"
        assert len(self.interact_idx) == N_TOP_INTERACT, "interact_idx must be (32,)"

    def transform(self, df) -> np.ndarray:
        X = df.values
        X = mstats.winsorize(X, limits=[0.01, 0.01], axis=0).data
        X = self.scaler.transform(X)
        X = np.clip(X, -3.0, 3.0) / 3.0
        X_lin = X[:, self.linear_idx]
        pairs = []
        for i in range(N_TOP_INTERACT):
            for j in range(i + 1, N_TOP_INTERACT):
                pairs.append(X[:, self.interact_idx[i]] *
                             X[:, self.interact_idx[j]])
        X_inter = np.column_stack(pairs)
        pad = np.zeros((X.shape[0], N_FEATURES_D2 - N_TOP_LINEAR - 496),
                        dtype=np.float64)
        result = np.hstack([X_lin, X_inter, pad])
        return result.astype(np.float64)
