#!/usr/bin/env python3
"""Logit-vs-logit encrypted parity over the held-out test set, SLOT or CNP.

Same method as tests/test_inference.py::run_runtime_validation (plaintext
logit X@w+b vs decrypt(server result), which already includes the bias because
the server applies it), but with its own server process on a free port and an
explicit output path -- so it never touches the tracked artifacts/errors.json.

  python scripts/cnp_parity.py --mode cnp  --out artifacts/errors_cnp.json
  python scripts/cnp_parity.py --mode slot --out /tmp/errors_slot_rerun.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cnp_common as C  # noqa: E402


def run_parity(mode: str, max_samples: int | None = None, out_path: str | None = None,
               address: str | None = None) -> dict:
    w, bias = C.load_model()
    X, y = C.load_test_set()
    if max_samples is not None:
        X, y = X[:max_samples], y[:max_samples]
    plain = X @ w + bias

    proc = None
    if address is None:
        proc, address = C.launch_server(mode)
    try:
        client = C.make_client(address, mode)
        enc = C.encrypted_logits(client, X, mode)
    finally:
        if proc is not None:
            C.stop_server(proc)

    rep = C.error_report(plain, enc, y)
    from sklearn.metrics import roc_auc_score
    rep["roc_auc_plaintext"] = float(roc_auc_score(y, plain))
    rep["packing"] = mode
    rep["batch_size"] = C.CNP_BATCH if mode == "cnp" else C.SLOT_BATCH
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(rep, indent=2))
    return rep


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["slot", "cnp"], required=True)
    ap.add_argument("--max-samples", type=int, default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    r = run_parity(a.mode, a.max_samples, a.out)
    print(f"[{a.mode}] n={r['n_samples']} MAE={r['mean_abs_error']:.3e} median={r['median_abs_error']:.3e} "
          f"max={r['max_abs_error']:.3e} ROC-AUC={r['roc_auc_encrypted']:.6f} PR-AUC={r['pr_auc_encrypted']:.6f}")
