#!/usr/bin/env python3
"""CNP Day-1 gate: plaintext-only simulation of Coefficient-Native Packing.

Reproduce:  python scripts/cnp_gate.py [--artifacts DIR]
Writes:     artifacts/cnp_gate.json   (docs/cnp_gate.md is written by hand from it)

Plaintext only. Nothing here says anything about real HE timing or noise.
Inputs (read-only): X_test.npy, y_test.npy, weights.npy, model_weights.bin
(+ X_train.npy for the centering fallback).
"""
from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path

import numpy as np
from scipy import stats
from sklearn.metrics import roc_auc_score

SEED = 20260617
REPO = Path(__file__).resolve().parent.parent

# --- Parameters, each with the repo location it came from (verified by grep) ---
T_HERA = 0x1FC0001  # tools/transciphering/cipher/hera.go:63 (== RtFHeraParams[2,3].PlainModulus)
T_RUBATO = 0x1FC0001  # tools/transciphering/cipher/rubato.go:80
RUBATO_SIGMA = 1.6356633496458739795537788457309656607510203877762320964302959  # rubato.go:82
RUBATO_BOUND = int(6 * RUBATO_SIGMA)  # rubato.go:345 (6*sigma = 9), noise is integer-valued
PARAM_SOURCES = {
    "t_hera": {"value": T_HERA, "source": "tools/transciphering/cipher/hera.go:63",
               "cross_check": "third_party/RtF-Transciphering/ckks_fv/rtf_params.go:414,482 (PlainModulus 33292289)"},
    "t_rubato": {"value": T_RUBATO, "source": "tools/transciphering/cipher/rubato.go:80",
                 "cross_check": "tools/transciphering/README.md:110"},
    "rubato_sigma": {"value": RUBATO_SIGMA, "source": "tools/transciphering/cipher/rubato.go:82",
                     "cross_check": "TestRubatoNoiseStatistics, tools/transciphering/cipher/rubato_test.go:138 "
                                    "(measures integer deltas, |d|<=int(6*sigma)=9, stddev within 15% of sigma)"},
    "rubato_noise_model": "round(N(0,sigma)) clipped to +-9 (integer; approximates ckks_fv discrete Gaussian AGN)",
    "x_bound_source": "compiler/train_xgboost.py:60-61 (clip to [-3,3]/3) => |x|<=1; interactions of |x|<=1 stay <=1",
}

LOG_RANGE = range(4, 15)


# ----------------------------------------------------------------- helpers
def center(v, t):
    """Centre integers mod t into (-t/2, t/2]."""
    v = np.mod(v, t)
    return np.where(v > t // 2, v - t, v)


def load(art: Path):
    X = np.load(art / "X_test.npy")
    y = np.load(art / "y_test.npy")
    w = np.load(art / "weights.npy").ravel()
    raw = (art / "model_weights.bin").read_bytes()
    n, = struct.unpack("<I", raw[:4])
    bias, = struct.unpack("<d", raw[4:12])
    wbin = np.frombuffer(raw[12:12 + 8 * n], dtype="<f8")
    assert n == 256 and len(raw) == 12 + 8 * 256, (n, len(raw))
    assert np.array_equal(wbin, w), "weights.npy != weights in model_weights.bin"
    return X, y, w, float(bias)


def round_half_even(a):
    return np.rint(a)


# ----------------------------------------------------------------- 1. audit
def feature_audit(X):
    std = X.std(0)
    mx = np.abs(X).max(0)
    out = {
        "shape": list(X.shape),
        "global_max_abs": float(mx.max()),
        "global_min": float(X.min()), "global_max": float(X.max()),
        "n_constant_cols": int((std == 0).sum()),
        "n_all_zero_cols": int((mx == 0).sum()),
        "cols_with_abs_gt_1": int((mx > 1).sum()),
        "per_col": [{"min": float(X[:, j].min()), "max": float(X[:, j].max()), "std": float(std[j])}
                    for j in range(X.shape[1])],
        "claim_abs_le_1": bool(mx.max() <= 1.0),
        "claim_zero_padding": bool((mx == 0).any()),
    }
    return out


# ----------------------------------------------------------------- 2. layout
def negacyclic_mul_exact(a, b, N):
    """a (len N, entries in [0,2^31)), b (short, same range). Exact product
    mod X^N+1 over Z, via Kronecker substitution on Python big ints (64-bit
    slots). Each output slot is a sum of <=256 products of <2^25 values,
    i.e. <2^58, so 64 bits cannot overflow into the neighbouring slot.
    Returns (folded int64 array of length N, unfolded wrap part c[N:])."""
    assert len(b) <= 256 and a.max() < 2**25 and b.max() < 2**25 and a.min() >= 0 and b.min() >= 0
    A = int.from_bytes(np.asarray(a, dtype="<u8").tobytes(), "little")
    B = int.from_bytes(np.asarray(b, dtype="<u8").tobytes(), "little")
    C = A * B
    nbytes = 8 * (len(a) + len(b))
    c = np.frombuffer(C.to_bytes(nbytes, "little"), dtype="<u8").astype(object)
    c = np.array([int(v) for v in c], dtype=object)
    lo, hi = c[:N], c[N:]
    folded = lo.copy()
    folded[:len(hi)] -= hi
    return folded, hi


def naive_negacyclic(a, b, N):
    """Independent shift-and-add reference (exact int64; sums <2^58)."""
    out = np.zeros(N, dtype=np.int64)
    a = a.astype(np.int64)
    for d, bd in enumerate(b):
        bd = int(bd)
        out[d:] += a[:N - d] * bd
        out[:d] -= a[N - d:] * bd
    return out


def layout_trials(N, L, trials, rng, t):
    res = {"N": N, "L": L, "trials": trials, "pass": True, "wrap_target_range": None}
    out_idx = np.arange(L) * 256 + 255
    # wrap terms: x idx i (<N) times w_rev idx d (<=255) land on i+d-N when i+d>=N
    # -> targets are in [0, 254]; 256k+255 >= 255 so never an output index.
    wrap_targets = set(range(0, 255))
    assert not (wrap_targets & set(out_idx.tolist())), "wrap target hits an output index"
    res["wrap_target_range"] = [0, 254]
    for _ in range(trials):
        x = rng.integers(0, t, size=N, dtype=np.int64)
        w = rng.integers(0, t, size=256, dtype=np.int64)
        wrev = w[::-1].copy()  # coefficient d=255-j holds w_j
        folded, hi = negacyclic_mul_exact(x, wrev, N)
        ref = naive_negacyclic(x, wrev, N)
        assert all(int(folded[i]) == int(ref[i]) for i in range(N)), "kronecker != naive negacyclic"
        for k in range(L):
            dot = sum(int(x[256 * k + j]) * int(w[j]) for j in range(256))
            assert int(folded[256 * k + 255]) == dot, f"lane {k}: coeff != dot"
        # no wrap term on any output index: hi only feeds indices 0..len(hi)-1 <= 254
        # true product degree is N+254; slot N+255 is Kronecker padding and must be exactly 0.
        # Wrap terms c[N+m] fold onto index m in 0..254, never onto 256k+255.
        assert hi[255] == 0 and len(hi) == 256
        wrap_targets_seen = [m for m in range(255) if int(hi[m]) != 0]
        assert wrap_targets_seen and all(m % 256 != 255 for m in wrap_targets_seen)
    return res


# ----------------------------------------------------------------- 3. CNP-A
def cnp_a(X, y, w, b, auc0):
    D = 2.0 ** 40
    xq = round_half_even(X * D)
    wq = round_half_even(w * D)
    s = (xq @ wq) / (D * D)  # float64, as CKKS would carry it
    ref = X @ w
    err = np.abs(s - ref)
    auc = roc_auc_score(y, s + b)
    return {"delta_x": "2^40", "delta_w": "2^40", "max_abs_err": float(err.max()),
            "mean_abs_err": float(err.mean()), "auc": float(auc), "delta_auc": float(auc - auc0),
            "note": "float64 simulation; no CKKS noise/rescale modelled"}


# ----------------------------------------------------------------- 4. CNP-B
def sweep_cell(X, y, w, b, auc0, t, lx, lw, *, noise_seeds=None, blocks=1, xbound=None, bias=None):
    """One (dx,dw) cell. xbound: per-column max|x| guarantee (default 1.0).
    blocks: 1 or 2 (features split in halves, each centred separately)."""
    dx, dw = 2.0 ** lx, 2.0 ** lw
    bias = b if bias is None else bias
    xb = np.ones(X.shape[1]) if xbound is None else xbound
    xq = round_half_even(X * dx)
    wq = round_half_even(w * dw)
    xqmax_col = np.ceil(dx * xb)  # worst-case |xq_j| (guaranteed, not observed)
    noise = RUBATO_BOUND if noise_seeds is not None else 0
    sl = [slice(0, X.shape[1])] if blocks == 1 else [slice(0, 128), slice(128, 256)]
    # worst-case integer bound per output block
    wc = [float(np.sum(np.abs(wq[s]) * (xqmax_col[s] + noise))) for s in sl]
    wc_safe = all(v <= t // 2 for v in wc)

    def run(xq_):
        tot = np.zeros(len(xq_))
        smax = 0.0
        ovf = 0
        for s in sl:
            sb = xq_[:, s] @ wq[s]  # exact in float64: all partial sums < 2^53
            assert np.abs(sb).max() < 2 ** 53
            sbi = sb.astype(np.int64)
            ovf += int((np.abs(sbi) > t // 2).sum())  # outside (-t/2, t/2]
            smax = max(smax, float(np.abs(sbi).max()))
            tot += center(sbi, t)
        return tot / (dx * dw), smax, ovf

    cell = {"log2_dx": lx, "log2_dw": lw, "blocks": blocks,
            "worst_case_bound": max(wc), "t_half": t // 2, "worst_case_safe": bool(wc_safe),
            "sum_abs_wq": float(np.abs(wq).sum())}
    if noise_seeds is None:
        sc, smax, ovf = run(xq)
        auc = roc_auc_score(y, sc + bias)
        cell.update(emp_max_abs_s=smax, headroom_bits=math.log2((t // 2) / max(smax, 1.0)),
                    overflow_count=ovf, auc=float(auc), delta_auc=float(auc - auc0),
                    abs_delta_auc=float(abs(auc - auc0)))
    else:
        aucs, smaxs, ovfs = [], [], 0
        for sd in noise_seeds:
            r = np.random.default_rng(sd)
            e = np.clip(np.rint(r.normal(0, RUBATO_SIGMA, size=xq.shape)), -RUBATO_BOUND, RUBATO_BOUND)
            sc, smax, ovf = run(xq + e)
            aucs.append(roc_auc_score(y, sc + bias))
            smaxs.append(smax)
            ovfs += ovf
        m, sdv = float(np.mean(aucs)), float(np.std(aucs, ddof=1))
        cell.update(emp_max_abs_s=float(max(smaxs)),
                    headroom_bits=math.log2((t // 2) / max(max(smaxs), 1.0)), overflow_count=ovfs,
                    auc_mean=m, auc_std=sdv, auc=m, delta_auc=m - auc0, abs_delta_auc=abs(m - auc0),
                    aucs=[float(a) for a in aucs])
    return cell


def sweep(X, y, w, b, auc0, t, **kw):
    return [sweep_cell(X, y, w, b, auc0, t, lx, lw, **kw) for lx in LOG_RANGE for lw in LOG_RANGE]


def pick(cells, tol=1e-3):
    ok = [c for c in cells if c["worst_case_safe"] and c["abs_delta_auc"] <= tol]
    ok.sort(key=lambda c: (c["abs_delta_auc"], -(c["t_half"] / c["worst_case_bound"])))
    return ok


def fallbacks(Xte, y, w, b, auc0, t, noise_seeds, Xtr):
    """Only used if no pair passes. Reported separately."""
    out = {}
    kw = {} if noise_seeds is None else {"noise_seeds": noise_seeds}
    c1 = sweep(Xte, y, w, b, auc0, t, blocks=2, **kw)
    out["i_two_blocks_128"] = {"cells": c1, "passing": pick(c1)[:5],
                               "note": "two separate outputs per lane; bank sums two centred decrypted scalars"}
    mu = Xtr.mean(0)
    Xc = Xte - mu
    xb = 1.0 + np.abs(mu)  # |x-mu|<=1+|mu_j| given |x|<=1
    b2 = b + float(mu @ w)  # score = (x-mu).w + (b + mu.w) == x.w + b
    c2 = sweep(Xc, y, w, b, auc0, t, xbound=xb, bias=b2, **kw)
    out["ii_centred"] = {"cells": c2, "passing": pick(c2)[:5],
                         "bank_correction": "bank adds bias' = b + mu_train.w (plaintext, after decrypt+divide by dx*dw); "
                                            "client subtracts mu_train (public) before quantising"}
    return out


# ----------------------------------------------------------------- 5. mask
def mask_check(X, w, t, rng):
    N, L = 8192, 32
    dx, dw = 2.0 ** 8, 2.0 ** 8
    xq = round_half_even(X[:L] * dx).astype(np.int64)  # L real rows, one per lane
    wq = round_half_even(w * dw).astype(np.int64)
    xpoly = (xq.reshape(-1) % t).astype(np.int64)
    wrev = (wq[::-1] % t).astype(np.int64)
    prod = naive_negacyclic(xpoly, wrev, N) % t  # exact int64 (sums < 2^58), then mod t
    out_idx = np.arange(L) * 256 + 255
    nonout = np.setdiff1d(np.arange(N), out_idx)
    # outputs equal true dot products (mod t)
    for k in range(L):
        assert int(prod[out_idx[k]]) == int(xq[k] @ wq) % t
    pool, trials = [], 20
    for _ in range(trials):
        m = rng.integers(0, t, size=N, dtype=np.int64)
        m[out_idx] = 0
        assert (m[nonout] != 0).all()  # nonzero everywhere else (p(fail) ~ 2^-25 per draw; asserted)
        masked = (prod + m) % t
        assert np.array_equal(masked[out_idx], prod[out_idx]), "mask changed an output coefficient"
        pool.append(masked[nonout])
    pool = np.concatenate(pool)
    bins = 64

    def chi(v):
        h = np.bincount((v.astype(object) * bins // t).astype(np.int64), minlength=bins)
        c = stats.chisquare(h)
        return float(c.statistic), float(c.pvalue)

    ms, mp = chi(pool)
    us, up = chi(np.tile(prod[nonout], trials))
    assert mp > 1e-3, f"masked coefficients not uniform (p={mp})"
    # leak: single-lane query (other lanes zero), unmasked product
    lane_x = xq[0]
    assert lane_x[0] % t != 0
    xp = np.zeros(N, dtype=np.int64)
    xp[:256] = lane_x % t
    c = naive_negacyclic(xp, wrev, N) % t  # no wrap/spill: only lane 0 is nonzero
    x0inv = pow(int(lane_x[0]) % t, -1, t)
    rec = [0] * 256  # rec[j] = w_j mod t
    for m_ in range(256):  # c[m] = sum_{i=0..m} x[i] * w_{255-m+i}
        acc = int(c[m_])
        for i in range(1, m_ + 1):
            acc -= int(lane_x[i]) * rec[255 - m_ + i]
        rec[255 - m_] = (acc * x0inv) % t
    rec_c = center(np.array(rec, dtype=np.int64), t)
    assert np.array_equal(rec_c, wq), "triangular solve did not recover wq"
    return {"N": N, "L": L, "masked_chi2": ms, "masked_p": mp, "unmasked_chi2": us, "unmasked_p": up,
            "bins": bins, "n_samples_masked": int(len(pool)), "outputs_unchanged": True,
            "recovered_all_256_weights_exactly": True,
            "leak_caveat": "triangular form needs the other lanes zero (attacker-chosen query) or known; with a fully "
                           "packed batch lane-0 outputs also mix lane L-1 via the negacyclic wrap and neighbours via "
                           "spill, giving a general (non-triangular) 256x256 Toeplitz system, not tested here"}


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default=str(REPO / "artifacts"))
    ap.add_argument("--out", default=str(REPO / "artifacts" / "cnp_gate.json"))
    ap.add_argument("--force-fallbacks", action="store_true", help="exercise fallbacks even if a pair passes")
    a = ap.parse_args()
    art = Path(a.artifacts)
    rng = np.random.default_rng(SEED)

    X, y, w, b = load(art)
    auc0 = float(roc_auc_score(y, X @ w + b))
    print(f"baseline AUC {auc0:.6f} bias {b}")
    assert abs(auc0 - 0.9794) < 5e-4, f"baseline AUC {auc0} != ~0.9794; stopping"

    R = {"seed": SEED, "params": PARAM_SOURCES, "baseline_auc": auc0, "bias": b}
    R["audit"] = feature_audit(X)
    assert R["audit"]["claim_abs_le_1"], "audit: |x|>1 present"
    print("audit:", {k: v for k, v in R["audit"].items() if k != "per_col"})

    R["layout"] = [layout_trials(8192, 32, 20, rng, T_HERA), layout_trials(65536, 256, 20, rng, T_HERA)]
    print("layout asserts pass for N=8192 L=32 and N=65536 L=256 (20 trials each)")

    R["cnp_a"] = cnp_a(X, y, w, b, auc0)
    print("CNP-A", R["cnp_a"])

    Xtr = np.load(art / "X_train.npy")
    seeds = [SEED + i for i in range(5)]
    R["cnp_b"] = {}
    for name, t, ns in (("hera", T_HERA, None), ("rubato", T_RUBATO, seeds)):
        cells = sweep(X, y, w, b, auc0, t, **({} if ns is None else {"noise_seeds": ns}))
        good = pick(cells)
        entry = {"t": t, "cells": cells, "passing": good[:5], "n_passing": len(good),
                 "best": good[0] if good else None}
        if not good or a.force_fallbacks:
            entry["fallbacks"] = fallbacks(X, y, w, b, auc0, t, ns, Xtr)
        R["cnp_b"][name] = entry
        print(name, "n_passing", len(good), "best", good[0] if good else None)

    R["mask"] = mask_check(X, w, T_HERA, rng)
    print("mask:", R["mask"])

    Path(a.out).write_text(json.dumps(R, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
