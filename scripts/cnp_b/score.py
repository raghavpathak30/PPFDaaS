#!/usr/bin/env python3
"""Score a CNP-B run: AUC / logit error of the decrypted outputs against the plaintext.

  python3 scripts/cnp_b/score.py --data <prepare_inputs.py dir> --results <results dir> --cipher hera|rubato

Reads cnp_<cipher>_rows.i32 + cnp_<cipher>_dec.i64 (written by rtf_cnp_test.go), logit = integer / 2^14
(the bias is already inside the integer: folded into the server-side mask). AUC on logits equals AUC on
sigmoid(logits) (monotone). Compared against the plaintext float logit X@w+b on the same rows.
For HERA the integers must equal expected.i64 exactly.
"""
import argparse, json
from pathlib import Path
import numpy as np
from sklearn.metrics import roc_auc_score, average_precision_score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--cipher", required=True, choices=["hera", "rubato"])
    a = ap.parse_args()
    d, r = Path(a.data), Path(a.results)
    rows = np.fromfile(r / f"cnp_{a.cipher}_rows.i32", dtype="<i4")
    dec = np.fromfile(r / f"cnp_{a.cipher}_dec.i64", dtype="<i8")
    assert len(rows) == len(dec) and len(rows) > 0
    y = np.fromfile(d / "y.u8", dtype=np.uint8)[rows]
    plain = np.fromfile(d / "plain_logit.f64", dtype="<f8")[rows]
    exp = np.fromfile(d / "expected.i64", dtype="<i8")[rows]
    logit = dec / 2.0**14
    err = np.abs(logit - plain)
    rep = {
        "cipher": a.cipher, "rows": int(len(rows)), "frauds": int(y.sum()),
        "bit_exact_vs_integer_simulation": bool(np.array_equal(dec, exp)),
        "rows_differing_from_integer_simulation": int((dec != exp).sum()),
        "max_abs_integer_diff": int(np.abs(dec - exp).max()),
        "max_abs_logit_error": float(err.max()), "mean_abs_logit_error": float(err.mean()),
        "median_abs_logit_error": float(np.median(err)),
        "roc_auc_encrypted": float(roc_auc_score(y, logit)),
        "roc_auc_plaintext_float": float(roc_auc_score(y, plain)),
        "pr_auc_encrypted": float(average_precision_score(y, logit)),
        "pr_auc_plaintext_float": float(average_precision_score(y, plain)),
    }
    rep["delta_roc_auc"] = rep["roc_auc_encrypted"] - rep["roc_auc_plaintext_float"]
    (r / f"cnp_{a.cipher}_score.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
