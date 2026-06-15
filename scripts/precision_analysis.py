#!/usr/bin/env python3
"""Phase 3, Item 3.2 — precision characterization for the CKKS parameter choice.

Answers "why scale=2^40, and how much headroom do you have?" with measurements
instead of assertions:

  - Full held-out test set (n=56,962, Phase 0 artifacts/errors.json): the
    end-to-end logit-vs-logit error distribution of the encrypted path,
    already measured by tests/test_inference.py::run_runtime_validation.
  - Representative batch (the first 16 transactions = one 4096-slot packed
    ciphertext): per-circuit-stage decrypted values from
    tools/local_benchmark/precision_probe, compared against a plaintext
    oracle, giving a per-stage error distribution (mean/std/percentiles) that
    Phase 0's aggregate-only artifacts/errors.json does not provide.

Output: artifacts/precision_analysis.json + a human-readable table on stdout.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
PROBE_SRC = REPO_ROOT / "tools" / "local_benchmark" / "precision_probe.cpp"
PROBE_BIN = REPO_ROOT / "tools" / "local_benchmark" / "precision_probe"

SCALE = 2.0 ** 40
N_TXNS = 16
N_FEATURES = 256


def build_probe() -> None:
    if PROBE_BIN.exists() and PROBE_BIN.stat().st_mtime >= PROBE_SRC.stat().st_mtime:
        return
    cmd = [
        "g++", "-O2", "-std=c++17", str(PROBE_SRC),
        "-I", "/usr/local/include/SEAL-4.1",
        "-L", "/usr/local/lib", "-lseal-4.1", "-lpthread",
        "-o", str(PROBE_BIN),
    ]
    print(f"[precision_analysis] building precision_probe: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


def run_probe(features_path: Path, weights_path: Path) -> dict:
    out = subprocess.run(
        [str(PROBE_BIN), str(features_path), str(weights_path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def error_stats(errors: np.ndarray) -> dict:
    return {
        "n": int(errors.size),
        "min": float(errors.min()),
        "max": float(errors.max()),
        "mean": float(errors.mean()),
        "std": float(errors.std()),
        "p50": float(np.percentile(errors, 50)),
        "p95": float(np.percentile(errors, 95)),
        "p99": float(np.percentile(errors, 99)),
    }


def main() -> int:
    errors_json_path = ARTIFACTS / "errors.json"
    if not errors_json_path.exists():
        print(f"FAIL: missing {errors_json_path} (Phase 0 parity harness output)", file=sys.stderr)
        return 1
    full_dataset = json.loads(errors_json_path.read_text())

    X_test = np.load(ARTIFACTS / "X_test.npy")
    weights = np.load(ARTIFACTS / "weights.npy")
    model_weights_bin = ARTIFACTS / "model_weights.bin"

    raw = model_weights_bin.read_bytes()
    import struct
    bias = struct.unpack("<d", raw[4:12])[0]

    # Representative batch: first 16 transactions == one 4096-slot ciphertext.
    batch = X_test[:N_TXNS].astype("<f8")
    features_path = REPO_ROOT / "artifacts" / "_precision_batch_features.bin"
    batch.tofile(features_path)

    build_probe()
    probe = run_probe(features_path, model_weights_bin)
    features_path.unlink(missing_ok=True)

    # ── Plaintext oracle for the representative batch ──────────────────────
    elementwise_product = (batch.reshape(N_TXNS, N_FEATURES) * weights[None, :]).reshape(-1)
    dot_products = elementwise_product.reshape(N_TXNS, N_FEATURES).sum(axis=1)
    logits = dot_products + bias

    stage1 = np.array(probe["stage1_after_multiply_plain"])
    stage2 = np.array(probe["stage2_after_rescale"])
    stage3 = np.array(probe["stage3_after_hoisted_tree_sum"])
    stage4 = np.array(probe["stage4_after_add_plain_bias"])

    lane_idx = np.arange(N_TXNS) * N_FEATURES

    stage_errors = {
        "stage1_after_multiply_plain": error_stats(np.abs(stage1 - elementwise_product)),
        "stage2_after_rescale": error_stats(np.abs(stage2 - elementwise_product)),
        "stage3_after_hoisted_tree_sum": error_stats(np.abs(stage3[lane_idx] - dot_products)),
        "stage4_after_add_plain_bias": error_stats(np.abs(stage4[lane_idx] - logits)),
    }

    # ── Headline numbers from the full held-out set (Phase 0) ──────────────
    max_ae_full = full_dataset["max_abs_error"]
    noise_floor_full = full_dataset["abs_error_distribution"]["min"]
    scale_headroom_bits = math.log2(SCALE / max_ae_full)
    error_bits_below_unity = -math.log2(max_ae_full)
    scale_bits_consumed = math.log2(SCALE) - error_bits_below_unity

    result = {
        "scale": SCALE,
        "scale_log2": math.log2(SCALE),
        "middle_prime_bits": 40,
        "full_dataset": {
            "source": "artifacts/errors.json (Phase 0 parity harness, n=56962, "
                      "logit-vs-logit over the full held-out test set)",
            "n_samples": full_dataset["n_samples"],
            "max_abs_error": max_ae_full,
            "mean_abs_error": full_dataset["mean_abs_error"],
            "median_abs_error": full_dataset["median_abs_error"],
            "abs_error_distribution": full_dataset["abs_error_distribution"],
            "noise_floor_min_abs_error": noise_floor_full,
            "noise_ceiling_max_abs_error": max_ae_full,
            "scale_headroom_bits_log2_scale_over_max_ae": scale_headroom_bits,
            "max_ae_bits_below_unity": error_bits_below_unity,
            "scale_bits_consumed_reaching_max_ae": scale_bits_consumed,
            "scale_bits_remaining_headroom": math.log2(SCALE) - scale_bits_consumed,
        },
        "representative_batch": {
            "source": "first 16 transactions of artifacts/X_test.npy "
                      "(= one 4096-slot packed ciphertext), via "
                      "tools/local_benchmark/precision_probe",
            "n_transactions": N_TXNS,
            "bias": bias,
            "q_last": probe["q_last"],
            "bias_scale": probe["bias_scale"],
            "stage_errors_abs": stage_errors,
        },
    }

    out_path = ARTIFACTS / "precision_analysis.json"
    out_path.write_text(json.dumps(result, indent=2))

    # ── Human-readable table ────────────────────────────────────────────────
    print("=" * 78)
    print("PPFDaaS Phase 3.2 — Precision characterization")
    print("=" * 78)
    print(f"scale = 2^40 = {SCALE:.6e}  (middle prime = 40 bits)")
    print()
    print("Full held-out test set (n=%d, Phase 0 artifacts/errors.json):" % full_dataset["n_samples"])
    print(f"  noise floor  (min |enc-plain|) = {noise_floor_full:.6e}")
    print(f"  noise ceiling (max |enc-plain|, MaxAE) = {max_ae_full:.6e}")
    print(f"  mean |enc-plain|   = {full_dataset['mean_abs_error']:.6e}")
    print(f"  median |enc-plain| = {full_dataset['median_abs_error']:.6e}")
    print(f"  p90 / p99 / p99.9  = "
          f"{full_dataset['abs_error_distribution']['p90']:.6e} / "
          f"{full_dataset['abs_error_distribution']['p99']:.6e} / "
          f"{full_dataset['abs_error_distribution']['p99.9']:.6e}")
    print(f"  scale headroom log2(scale/MaxAE) = {scale_headroom_bits:.2f} bits")
    print(f"  MaxAE is {error_bits_below_unity:.2f} bits below 1.0 "
          f"=> ~{scale_bits_consumed:.1f} of the 40 scale bits consumed, "
          f"~{math.log2(SCALE) - scale_bits_consumed:.1f} bits remaining headroom")
    print()
    print("Representative batch (first 16 transactions, 4096-slot ciphertext):")
    header = f"{'stage':<32}{'n':>6}{'min':>12}{'max':>12}{'mean':>12}{'std':>12}{'p50':>12}{'p95':>12}{'p99':>12}"
    print(header)
    for stage_name, st in stage_errors.items():
        print(f"{stage_name:<32}{st['n']:>6}{st['min']:>12.3e}{st['max']:>12.3e}"
              f"{st['mean']:>12.3e}{st['std']:>12.3e}{st['p50']:>12.3e}"
              f"{st['p95']:>12.3e}{st['p99']:>12.3e}")
    print()
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
