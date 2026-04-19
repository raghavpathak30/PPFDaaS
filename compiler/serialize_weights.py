import struct, numpy as np

from pathlib import Path

N_FEATURES     = 256
EXPECTED_BYTES = 2060

def write_model_weights_bin(weights: np.ndarray, bias: float, path) -> None:
    if weights.ndim != 1 or len(weights) != N_FEATURES:
        raise ValueError(f'weights must be 1D({N_FEATURES},); got {weights.shape}')
    if not np.isfinite(weights).all() or not np.isfinite(bias):
        raise ValueError('NaN or Inf in weights/bias — check linearization')
    w_le = weights.astype('<f8')
    with open(path, 'wb') as f:
        f.write(struct.pack('<I', N_FEATURES))
        f.write(struct.pack('<d', float(bias)))
        f.write(w_le.tobytes())
    written = Path(path).stat().st_size
    if written != EXPECTED_BYTES:
        raise RuntimeError(f'BUG: expected {EXPECTED_BYTES} bytes, wrote {written}')
    print(f'[serialize_weights] {written} bytes → {path}')

def load_and_verify(path) -> tuple:
    with open(path, 'rb') as f:
        n,    = struct.unpack('<I', f.read(4))
        bias, = struct.unpack('<d', f.read(8))
        w = np.frombuffer(f.read(n * 8), dtype='<f8').copy()
    assert n == N_FEATURES
    return w, bias
