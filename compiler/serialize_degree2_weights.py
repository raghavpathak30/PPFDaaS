import struct, numpy as np

from pathlib import Path

N_FEATURES_D2   = 512
EXPECTED_BYTES  = 4108

def write_degree2_weights_bin(weights, bias, path) -> None:
    if weights.ndim != 1 or len(weights) != N_FEATURES_D2:
        raise ValueError(f'Expected (512,), got {weights.shape}')
    if not np.isfinite(weights).all() or not np.isfinite(bias):
        raise ValueError('NaN/Inf in degree-2 weights')
    w_le = weights.astype('<f8')
    with open(path, 'wb') as f:
        f.write(struct.pack('<I', N_FEATURES_D2))
        f.write(struct.pack('<d', float(bias)))
        f.write(w_le.tobytes())
    written = Path(path).stat().st_size
    if written != EXPECTED_BYTES:
        raise RuntimeError(f'Expected {EXPECTED_BYTES} bytes, wrote {written}')
    print(f'[serialize_d2] {written} bytes → {path}')
