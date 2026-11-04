#!/usr/bin/env python3
"""Prepare the integer inputs for the CNP-B harness (rtf_cnp_test.go) and the plaintext integer simulation.

Same quantisation as scripts/cnp_gate.py: xq = round_half_even(2^8 x), wq = round_half_even(2^6 w),
bias_q = round_half_even(bias * 2^14) (bias folded into the mask, server side), centre mod t.

Writes into --out (mounted read-only into the container as $CNP_DATA_DIR):
  meta.json, xq.i16 (n x 256), wq.i16 (256), y.u8, plain_logit.f64 (X@w+b, float),
  expected.i64 (centre((xq.wq + bias_q) mod t), the integer the bank must read for HERA),
  strat4096.i32 (all frauds + seeded random non-frauds = 4096 rows; the AUC subset if the full set is too slow).
"""
import argparse, json, struct
from pathlib import Path
import numpy as np

T = 0x1FC0001  # 33292289, tools/transciphering/cipher/hera.go:63, rubato.go:80
LX, LW = 8, 6
SEED = 20260617


def rhe(a):
    return np.rint(a)  # numpy rint = round half to even, as scripts/cnp_gate.py round_half_even


def center(v, t):
    v = np.mod(v, t)
    return np.where(v > t // 2, v - t, v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    art, out = Path(a.artifacts), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    X = np.load(art / "X_test.npy").astype(np.float64)
    y = np.load(art / "y_test.npy").astype(np.uint8)
    w = np.load(art / "weights.npy").astype(np.float64)
    raw = (art / "model_weights.bin").read_bytes()
    bias = struct.unpack("<d", raw[4:12])[0]
    assert X.shape[1] == 256 and w.shape == (256,)
    xq = rhe(X * 2**LX).astype(np.int64)
    wq = rhe(w * 2**LW).astype(np.int64)
    bq = int(rhe(np.float64(bias) * 2**(LX + LW)))
    assert np.abs(xq).max() <= 2**LX
    exp = center(xq @ wq + bq, T).astype(np.int64)
    bound = np.abs(wq).sum() * 2**LX + abs(bq)
    assert bound < T // 2, (bound, T // 2)
    rng = np.random.default_rng(SEED)
    fr = np.flatnonzero(y == 1)
    nf = np.flatnonzero(y == 0)
    pick = np.sort(np.concatenate([fr, rng.choice(nf, 4096 - len(fr), replace=False)]))
    xq.astype("<i2").tofile(out / "xq.i16")
    wq.astype("<i2").tofile(out / "wq.i16")
    y.tofile(out / "y.u8")
    (X @ w + bias).astype("<f8").tofile(out / "plain_logit.f64")
    exp.astype("<i8").tofile(out / "expected.i64")
    pick.astype("<i4").tofile(out / "strat4096.i32")
    (out / "meta.json").write_text(json.dumps({
        "n": int(X.shape[0]), "features": 256, "t": T, "log2_dx": LX, "log2_dw": LW, "bias": bias,
        "bias_q": bq, "worst_case_bound": int(bound), "t_half": T // 2, "frauds": int(len(fr)),
        "strat_rows": int(len(pick)), "seed": SEED}, indent=1))
    print(f"n={X.shape[0]} bias={bias} bias_q={bq} worst-case |s|<={bound} < t/2={T//2}; frauds={len(fr)}; wrote {out}")


if __name__ == "__main__":
    main()
