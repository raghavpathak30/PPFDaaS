"""
scripts/quantisation_sweep.py

Measures the effect of feature quantisation on the depth-1 LR surrogate's
AUC/AUPRC. Motivation: the transciphering (HERA-in-BFV) path carries a
plaintext modulus t = 2^26, so features entering that path are bounded to
26-bit integers with less accumulation headroom than full float precision.
Nobody had previously measured what quantising the *features* (not the
model weights) does to the surrogate's discrimination quality.

Does NOT retrain anything. Loads the existing trained LR surrogate
(artifacts/weights.npy + bias from artifacts/model_weights.bin) and the
existing test split (artifacts/X_test.npy, artifacts/y_test.npy), matching
the exact scoring methodology in compiler/train_logistic_regression.py
(logits = X @ weights + bias, prob = sigmoid(logits)).

Quantisation model: per-feature uniform quantisation. For each of the 256
feature columns, the "observed range" is the column's [min, max] over the
union of X_train and X_test (the full observed dataset, since a real
deployment's quantiser would be calibrated over all data it has seen, not
just the test split). A column is quantised to B bits by mapping its range
onto 2^B - 1 uniform levels, rounding, then de-quantising back to float
(i.e. this measures the AUC/AUPRC impact of the quantisation *error*, using
the existing float-precision LR weights -- weight quantisation is out of
scope for this sweep). Constant columns (max == min) are left unchanged at
any bit depth since they carry no information to quantise.

OPERATING THRESHOLD: this test set is ~0.17% positive (98/56962), so a flat
0.5 probability threshold is not a meaningful classification cutoff -- it
was a placeholder in an earlier version of this script. Searched the repo
for a fixed operating threshold (docs/spec.md, bank_client/bank_client.py,
vendor_server/src/inference_service_160.cpp): none exists. The 0.94/0.92
constants in compiler/auc_dispatch.py gate *AUC-based path selection*
(depth-1 vs degree-2 fallback), not per-sample fraud/not-fraud decisions.
In the absence of a fixed operating point, this script computes three from
the float baseline's precision-recall curve: max-F1, and the highest
threshold achieving >=90% / >=95% recall. All three are reported; none is
privileged as "the" threshold because none is authoritative.
"""
from __future__ import annotations

import json
from pathlib import Path
import struct

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.special import expit
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_curve

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"

BIT_DEPTHS = [10, 12, 14, 16, 18, 20, 24, 26]
DEGRADATION_TOLERANCE = 0.005
PLAINTEXT_MODULUS_BITS = 26


def load_weights_and_bias() -> tuple[np.ndarray, float]:
    weights = np.load(ARTIFACTS_DIR / "weights.npy")
    with open(ARTIFACTS_DIR / "model_weights.bin", "rb") as f:
        f.read(4)  # 4-byte header
        (bias,) = struct.unpack("<d", f.read(8))
    return weights, bias


def quantise(X: np.ndarray, bits: int, col_min: np.ndarray, col_max: np.ndarray) -> np.ndarray:
    levels = (2 ** bits) - 1
    span = col_max - col_min
    constant = span == 0
    span_safe = np.where(constant, 1.0, span)

    normalised = (X - col_min) / span_safe
    normalised = np.clip(normalised, 0.0, 1.0)
    q = np.round(normalised * levels)
    dequantised = q / levels * span_safe + col_min

    # Constant columns carry no information at any bit depth -- leave as-is.
    return np.where(constant, X, dequantised)


def logits_of(X: np.ndarray, weights: np.ndarray, bias: float) -> np.ndarray:
    return X @ weights + bias


def compute_operating_points(y_true: np.ndarray, probs: np.ndarray) -> dict:
    precision, recall, thresholds = precision_recall_curve(y_true, probs)
    # precision/recall have one more element than thresholds (the last point
    # precision=1,recall=0 has no corresponding threshold); align by dropping it.
    precision, recall = precision[:-1], recall[:-1]

    f1 = np.where(
        (precision + recall) > 0,
        2 * precision * recall / np.where((precision + recall) > 0, precision + recall, 1.0),
        0.0,
    )
    max_f1_idx = int(np.argmax(f1))

    def highest_threshold_at_recall(target: float):
        candidates = np.where(recall >= target)[0]
        if len(candidates) == 0:
            return None
        # thresholds is increasing with index; recall is non-increasing with
        # index, so the largest index still meeting the recall floor gives
        # the highest (most precision-favourable) threshold satisfying it.
        idx = int(candidates.max())
        return {
            "threshold": float(thresholds[idx]),
            "recall": float(recall[idx]),
            "precision": float(precision[idx]),
        }

    return {
        "max_f1": {
            "threshold": float(thresholds[max_f1_idx]),
            "f1": float(f1[max_f1_idx]),
            "precision": float(precision[max_f1_idx]),
            "recall": float(recall[max_f1_idx]),
            "source": "computed from float baseline precision_recall_curve (argmax F1)",
        },
        "recall_90": {
            **(highest_threshold_at_recall(0.90) or {}),
            "source": "computed from float baseline precision_recall_curve (highest threshold with recall>=0.90)",
        },
        "recall_95": {
            **(highest_threshold_at_recall(0.95) or {}),
            "source": "computed from float baseline precision_recall_curve (highest threshold with recall>=0.95)",
        },
        "note": (
            "No fixed operating threshold exists in the repo (checked docs/spec.md, "
            "bank_client/bank_client.py, vendor_server/src/inference_service_160.cpp). "
            "The 0.94/0.92 constants in compiler/auc_dispatch.py gate AUC-based path "
            "selection, not per-sample classification. These three thresholds are "
            "derived from the float baseline and none is authoritative."
        ),
    }


def flip_breakdown(y_true: np.ndarray, float_probs: np.ndarray, q_probs: np.ndarray, threshold: float) -> dict:
    float_labels = float_probs >= threshold
    q_labels = q_probs >= threshold
    is_fraud = y_true == 1
    is_legit = y_true == 0

    fraud_missed = int(np.sum(is_fraud & float_labels & ~q_labels))
    fraud_gained = int(np.sum(is_fraud & ~float_labels & q_labels))
    legit_flagged = int(np.sum(is_legit & ~float_labels & q_labels))
    legit_cleared = int(np.sum(is_legit & float_labels & ~q_labels))

    n_fraud = int(np.sum(is_fraud))
    n_legit = int(np.sum(is_legit))

    return {
        "threshold": float(threshold),
        "fraud_missed_by_quantisation": fraud_missed,
        "fraud_missed_by_quantisation_frac_of_fraud": fraud_missed / n_fraud if n_fraud else None,
        "fraud_gained_by_quantisation": fraud_gained,
        "legit_flagged_by_quantisation": legit_flagged,
        "legit_flagged_by_quantisation_frac_of_legit": legit_flagged / n_legit if n_legit else None,
        "legit_cleared_by_quantisation": legit_cleared,
        "total_flips": fraud_missed + fraud_gained + legit_flagged + legit_cleared,
    }


def main() -> int:
    X_train = np.load(ARTIFACTS_DIR / "X_train.npy")
    X_test = np.load(ARTIFACTS_DIR / "X_test.npy")
    y_test = np.load(ARTIFACTS_DIR / "y_test.npy")
    weights, bias = load_weights_and_bias()

    assert X_test.shape[1] == weights.shape[0] == 256, (
        f"shape mismatch: X_test {X_test.shape}, weights {weights.shape}"
    )

    col_min = np.minimum(X_train.min(axis=0), X_test.min(axis=0))
    col_max = np.maximum(X_train.max(axis=0), X_test.max(axis=0))

    float_logits = logits_of(X_test, weights, bias)
    float_probs = expit(float_logits)
    float_auc = float(roc_auc_score(y_test, float_probs))
    float_auprc = float(average_precision_score(y_test, float_probs))

    print(f"[quantisation_sweep] float baseline: AUC={float_auc:.6f} AUPRC={float_auprc:.6f}")
    print(f"[quantisation_sweep] test set class balance: {int(y_test.sum())} fraud / {len(y_test)} total "
          f"({100 * y_test.mean():.3f}%)")

    operating_points = compute_operating_points(y_test, float_probs)
    for name, op in operating_points.items():
        if name == "note":
            continue
        print(f"[quantisation_sweep] operating point '{name}': threshold={op['threshold']:.6f} "
              f"(precision={op.get('precision', float('nan')):.4f}, recall={op.get('recall', float('nan')):.4f})")

    op_names = [n for n in operating_points if n != "note"]

    results = []
    for bits in BIT_DEPTHS:
        Xq = quantise(X_test, bits, col_min, col_max)
        q_logits = logits_of(Xq, weights, bias)
        q_probs = expit(q_logits)
        q_auc = float(roc_auc_score(y_test, q_probs))
        q_auprc = float(average_precision_score(y_test, q_probs))

        logit_delta = np.abs(float_logits - q_logits)
        max_abs_logit_delta = float(np.max(logit_delta))
        p999_abs_logit_delta = float(np.percentile(logit_delta, 99.9))

        flips = {name: flip_breakdown(y_test, float_probs, q_probs, operating_points[name]["threshold"])
                 for name in op_names}

        row = {
            "bits": bits,
            "auc": q_auc,
            "auprc": q_auprc,
            "auc_degradation": float_auc - q_auc,
            "auprc_degradation": float_auprc - q_auprc,
            "logit_delta_max_abs": max_abs_logit_delta,
            "logit_delta_p999_abs": p999_abs_logit_delta,
            "flips_by_operating_point": flips,
        }
        results.append(row)

        fraud_missed_str = ", ".join(
            f"{name}={flips[name]['fraud_missed_by_quantisation']}" for name in op_names
        )
        print(
            f"[quantisation_sweep] bits={bits:2d} AUC={q_auc:.6f} (Δ={float_auc - q_auc:+.6f}) "
            f"AUPRC={q_auprc:.6f} logit_delta[max={max_abs_logit_delta:.4e}, "
            f"p999={p999_abs_logit_delta:.4e}] fraud_missed[{fraud_missed_str}]"
        )

    verdict_bits = None
    for row in results:
        if row["auc_degradation"] < DEGRADATION_TOLERANCE:
            verdict_bits = row["bits"]
            break

    headroom = {
        "plaintext_modulus_bits": PLAINTEXT_MODULUS_BITS,
        "sufficient_bits": verdict_bits,
        "margin_bits": (PLAINTEXT_MODULUS_BITS - verdict_bits) if verdict_bits is not None else None,
        "basis": (
            f"sufficient_bits is the lowest tested bit depth with AUC degradation < "
            f"{DEGRADATION_TOLERANCE} absolute vs. the float baseline (the same "
            f"criterion as verdict_lowest_bits_within_tolerance below). margin_bits = "
            f"{PLAINTEXT_MODULUS_BITS} - sufficient_bits is how much of t=2^{PLAINTEXT_MODULUS_BITS}'s "
            f"headroom is unused by feature precision alone, before any accumulation cost."
        ),
    }

    out = {
        "methodology": (
            "Per-feature uniform quantisation over the observed [min,max] range "
            "(union of X_train and X_test), rounded to 2^bits-1 levels, then "
            "de-quantised back to float. Scored with the existing float-precision "
            "LR surrogate (artifacts/weights.npy). No retraining, no weight "
            "quantisation."
        ),
        "float_baseline": {"auc": float_auc, "auprc": float_auprc},
        "test_set_class_balance": {
            "n_fraud": int(y_test.sum()),
            "n_total": int(len(y_test)),
            "fraud_frac": float(y_test.mean()),
        },
        "operating_points": operating_points,
        "degradation_tolerance": DEGRADATION_TOLERANCE,
        "note": (
            "logit_delta_max_abs / logit_delta_p999_abs are the metrics that degrade "
            "smoothly with bit depth and reflect margin; flips_by_operating_point is "
            "the metric that matters more than AUC for production risk, because it is "
            "broken out by class -- fraud_missed_by_quantisation (a fraud case the "
            "float model caught that quantisation causes to be missed) is the "
            "consequential direction, not the aggregate flip count."
        ),
        "results": results,
        "verdict_lowest_bits_within_tolerance": verdict_bits,
        "headroom_vs_plaintext_modulus": headroom,
    }

    out_path = ARTIFACTS_DIR / "quantisation_sweep.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"[quantisation_sweep] wrote {out_path}")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    ax1 = axes[0]
    bits_list = [r["bits"] for r in results]
    auc_list = [r["auc"] for r in results]
    ax1.plot(bits_list, auc_list, marker="o", color="tab:blue", label="AUC")
    ax1.axhline(float_auc, color="tab:blue", linestyle="--", alpha=0.5, label="float AUC baseline")
    ax1.set_xlabel("Feature quantisation bit depth")
    ax1.set_ylabel("AUC")
    ax1.set_title("AUC vs. bit depth")
    ax1.legend()

    ax2 = axes[1]
    max_delta = [r["logit_delta_max_abs"] for r in results]
    p999_delta = [r["logit_delta_p999_abs"] for r in results]
    ax2.plot(bits_list, max_delta, marker="o", color="tab:red", label="max |Δlogit|")
    ax2.plot(bits_list, p999_delta, marker="s", color="tab:orange", label="p99.9 |Δlogit|")
    ax2.set_yscale("log")
    ax2.set_xlabel("Feature quantisation bit depth")
    ax2.set_ylabel("|logit_float - logit_quantised| (log scale)")
    ax2.set_title("Logit delta vs. bit depth")
    ax2.legend()

    fig.suptitle("Feature quantisation sweep")
    fig.tight_layout()
    png_path = ARTIFACTS_DIR / "quantisation_sweep.png"
    fig.savefig(png_path, dpi=150)
    print(f"[quantisation_sweep] wrote {png_path}")

    if verdict_bits is not None:
        print(
            f"[quantisation_sweep] VERDICT: lowest bit depth with AUC degradation "
            f"< {DEGRADATION_TOLERANCE} absolute is {verdict_bits} bits "
            f"({PLAINTEXT_MODULUS_BITS - verdict_bits} bits of margin under t=2^{PLAINTEXT_MODULUS_BITS})."
        )
    else:
        print(
            f"[quantisation_sweep] VERDICT: no tested bit depth kept AUC degradation "
            f"< {DEGRADATION_TOLERANCE} absolute."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
