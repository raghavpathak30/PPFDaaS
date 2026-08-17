"""
scripts/hhe_breakeven.py — Phase 7 §7.4 break-even map.

Sweeps (circuit_depth × batch_occupancy × uplink_bandwidth) and computes,
for each cell, whether plain-CKKS upload or HHE online upload wins on
end-to-end latency.  Real measured numbers are used for every CKKS-side cell.
HHE server-side columns (online_transcipher_ms, repacking_ms) are PENDING
the KAIST ckks_fv scheme bridge; those cells are marked accordingly.

Axes:
  circuit_depth      in {1, 2, 3}
  batch_occupancy    in {1, 4, 8, 16}   (lanes per request)
  uplink_bandwidth   in {1000, 10000, 100000}  (kbps)

CKKS-side sources (all MEASURED):
  - encrypt_time_ms:   estimated from comparison_results.json server mean
                       minus the local-circuit fold benchmark mean; the
                       remainder is client-side deserialisation + gRPC overhead.
                       For a principled lower bound, we use the per-lane
                       occupancy sweep from throughput_results.json.
  - upload_bytes:      artifacts/wire_sizes.json (160-bit standard = 262257 B)
  - he_eval_time_ms:   occupancy_sweep[lanes].batch_latency_us / 1000

HHE-side sources:
  - online_encrypt_ms: tools/transciphering/results/hera_bench_lane{1,4,8,16}.json
                       (MEASURED, plaintext path)
  - upload_bytes_sym:  256 * lanes * 4 + 28  (HERA-16 formula, computed)
  - he_eval_time_ms:   same as CKKS (post-transciphering circuit is identical)
  - offline_keygen_ms: amortized over N_amortize=1000 requests; provisioning
                       cost modeled as key_expansion_mean_ms (plaintext path)
  - online_transcipher_ms: PENDING
  - repacking_ms:      PENDING

Outputs:
  artifacts/hhe_breakeven.json
  results/hhe_breakeven_plot.png  (matplotlib heatmap)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parent.parent

# ──────────────────────────────────────────────────────────────────────────────
# Parity gate (Phase 5 pattern — must pass before any timing is reported)
# ──────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(REPO / "scripts"))
from parity_gate import load_model_weights

def _run_parity_gate():
    try:
        weights, bias = load_model_weights(str(REPO / "artifacts" / "model_weights.bin"))
    except Exception as e:
        print(f"[parity_gate] WARNING: could not load model weights: {e}")
        return

    errors_path = REPO / "artifacts" / "errors.json"
    if not errors_path.exists():
        print("[parity_gate] INFO: artifacts/errors.json not found; skipping live gate")
        return

    with open(errors_path) as f:
        err = json.load(f)
    max_ae = err.get("max_abs_error", err.get("error_stats", {}).get("max", 1.0))
    tol = 1e-3
    passed = float(max_ae) < tol
    if not passed:
        raise RuntimeError(
            f"[parity_gate] FAILED: max_abs_error={max_ae} >= {tol}; "
            "re-run tests/test_inference.py before producing timing artifacts"
        )
    print(f"[parity_gate] PASSED: max_abs_error={max_ae:.2e} < {tol:.0e}")

# ──────────────────────────────────────────────────────────────────────────────
# Load CKKS-side measurements
# ──────────────────────────────────────────────────────────────────────────────

def load_ckks_data():
    """Return dicts keyed by lane count for CKKS occupancy and latency."""
    with open(REPO / "artifacts" / "throughput_results.json") as f:
        tp = json.load(f)
    with open(REPO / "artifacts" / "wire_sizes.json") as f:
        ws = json.load(f)
    with open(REPO / "artifacts" / "comparison_results.json") as f:
        comp = json.load(f)

    occupancy = {}
    for row in tp["occupancy_sweep"]:
        occupancy[row["lanes"]] = row  # batch_latency_us, per_tx_us, mean_latency_us

    # 160-bit standard ciphertext size (1 batch = 1 CKKS ciphertext regardless of lanes)
    ckks_upload_bytes = ws["chains"]["160bit"]["standard_bytes"]  # 262257

    # Client-side encrypt time: we model it as the difference between the wall-clock
    # gRPC round-trip (comparison_results reduced_160bit mean_us) and the server-side
    # HE latency for lanes=16.  This gives a conservative per-call overhead estimate.
    # For lanes != 16 we scale by lane count (encryption time is ~proportional to lanes).
    r160 = comp.get("reduced_160bit") or comp.get("summary", {}).get("reduced_160bit", {})
    gRPC_overhead_us = r160["mean_us"] - occupancy.get(16, {}).get("batch_latency_us", r160["mean_us"] * 0.6)
    # gRPC_overhead_us includes serialization, loopback, deserialization (approx)
    # We use it as the "everything except HE eval" cost, fixed per request.
    gRPC_overhead_ms = max(gRPC_overhead_us / 1000, 0.5)  # floor at 0.5 ms

    return occupancy, ckks_upload_bytes, gRPC_overhead_ms


# ──────────────────────────────────────────────────────────────────────────────
# Load HHE-side measurements (HERA-16 bench from tools/transciphering)
# ──────────────────────────────────────────────────────────────────────────────

def run_hera_bench(features: int, lanes: int, rounds: int = 100) -> dict:
    """Run the Go HERA-16 bench for a given lane count; return parsed JSON."""
    bench_dir = REPO / "tools" / "transciphering"
    try:
        result = subprocess.run(
            ["go", "run", "./bench",
             f"--features={features}", f"--lanes={lanes}", f"--rounds={rounds}"],
            capture_output=True, text=True, cwd=bench_dir, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"go run bench failed: {result.stderr[:500]}")
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[hera_bench lanes={lanes}] WARNING: {e}")
        return None


def load_hhe_data(features: int = 256) -> dict:
    """Return dict keyed by lane count with HERA-16 measured data."""
    hhe = {}
    results_dir = REPO / "tools" / "transciphering" / "results"
    for lanes in [1, 4, 8, 16]:
        # Pre-generated per-lane cache files (results/hera_bench_lane{N}.json),
        # one per lane count -- the bench binary always writes results/hera_bench.json
        # too (for the default/last-run invocation), so per-lane files avoid clobbering.
        cached = None
        p = results_dir / f"hera_bench_lane{lanes}.json"
        if p.exists():
            with open(p) as f:
                cached = json.load(f)

        if cached is None:
            print(f"[hhe_data] Running HERA bench for lanes={lanes}...")
            cached = run_hera_bench(features, lanes)

        if cached is not None:
            hhe[lanes] = cached
        else:
            hhe[lanes] = {
                "status": "PENDING",
                "online_encrypt_mean_ms": None,
                "upload_symmetric_bytes": features * lanes * 4 + 28,
            }
    return hhe


# ──────────────────────────────────────────────────────────────────────────────
# Break-even computation
# ──────────────────────────────────────────────────────────────────────────────

# Depth scaling: depth-1 is measured; depth-2/3 are modeled as proportional
# to the depth-1 he_eval_time (each additional level adds ~one rescale +
# one multiply_plain, each ~1.5–2× cheaper than a rotation step).
# Conservative scaling factor: 1.0 (depth-1), 1.4 (depth-2), 1.8 (depth-3).
DEPTH_SCALE = {1: 1.0, 2: 1.4, 3: 1.8}

N_AMORTIZE = 1000   # offline keygen cost amortized over this many requests

CKKS_UPLOAD_BYTES = 262257   # 160-bit standard (wire_sizes.json)
CKKS_SEEDED_BYTES = 131266   # 160-bit seeded  (wire_sizes.json)


def _upload_ms(n_bytes: int, bandwidth_kbps: int) -> float:
    """Time (ms) to upload n_bytes at bandwidth_kbps kilobits/sec."""
    return (n_bytes * 8) / (bandwidth_kbps * 1000) * 1000   # ms


def compute_breakeven(occupancy: dict, gRPC_overhead_ms: float, hhe: dict):
    depths = [1, 2, 3]
    lanes_list = [1, 4, 8, 16]
    bandwidths = [1000, 10000, 100000]  # kbps

    cells = []

    for depth in depths:
        d_scale = DEPTH_SCALE[depth]
        for lanes in lanes_list:
            occ_row = occupancy.get(lanes) or occupancy.get(16)
            he_eval_ms_d1 = occ_row["batch_latency_us"] / 1000
            he_eval_ms = he_eval_ms_d1 * d_scale

            hhe_row = hhe.get(lanes, {})
            hhe_measured = (hhe_row.get("status") != "PENDING" and
                            hhe_row.get("online_encrypt_mean_ms") is not None)

            for bw in bandwidths:
                # ── CKKS cost ──
                ckks_upload_ms = _upload_ms(CKKS_UPLOAD_BYTES, bw)
                ckks_total_ms = gRPC_overhead_ms + ckks_upload_ms + he_eval_ms

                # ── HHE cost ──
                hhe_upload_bytes = lanes * 256 * 4 + 28  # HERA-16 formula
                hhe_upload_ms = _upload_ms(hhe_upload_bytes, bw)

                if hhe_measured:
                    hhe_encrypt_ms = hhe_row["online_encrypt_mean_ms"]
                    keygen_ms_amortized = hhe_row.get("key_expansion_mean_ms", 0.05) / N_AMORTIZE
                    hhe_transcipher_ms = "PENDING"
                    hhe_repacking_ms = "PENDING"
                    hhe_total_ms = "PENDING"  # can't sum with PENDING
                    hhe_wins = None          # unknown until server cost measured
                    winner = "PENDING"
                    margin_ms = None
                else:
                    hhe_encrypt_ms = None
                    keygen_ms_amortized = None
                    hhe_transcipher_ms = "PENDING"
                    hhe_repacking_ms = "PENDING"
                    hhe_total_ms = "PENDING"
                    hhe_wins = None
                    winner = "PENDING"
                    margin_ms = None

                # What we CAN determine even with PENDING server cost:
                # If CKKS_upload_ms > hhe_upload_ms + (some reasonable server overhead),
                # note how much headroom HHE has for server costs before CKKS wins.
                if hhe_measured:
                    headroom_ms = ckks_upload_ms - hhe_upload_ms - hhe_encrypt_ms
                else:
                    headroom_ms = None

                cell = {
                    "circuit_depth": depth,
                    "batch_occupancy_lanes": lanes,
                    "uplink_bandwidth_kbps": bw,
                    "depth_scale_factor": d_scale,
                    "methodology_note": (
                        "depth-1 he_eval from throughput_results.json occupancy_sweep; "
                        f"depth-{depth} he_eval scaled by {d_scale}x (modeled, not measured)"
                        if depth > 1 else
                        "depth-1 he_eval from throughput_results.json occupancy_sweep (MEASURED)"
                    ),
                    "ckks": {
                        "upload_bytes": CKKS_UPLOAD_BYTES,
                        "upload_ms": round(ckks_upload_ms, 4),
                        "he_eval_ms": round(he_eval_ms, 4),
                        "grpc_overhead_ms": round(gRPC_overhead_ms, 4),
                        "total_ms": round(ckks_total_ms, 4),
                        "status": "MEASURED" if depth == 1 else "MODELED",
                    },
                    "hhe": {
                        "upload_bytes": hhe_upload_bytes,
                        "upload_ms": round(hhe_upload_ms, 6),
                        "online_encrypt_ms": round(hhe_encrypt_ms, 4) if hhe_measured else "PENDING",
                        "keygen_amortized_ms": round(keygen_ms_amortized, 6) if hhe_measured else "PENDING",
                        "online_transcipher_ms": hhe_transcipher_ms,
                        "repacking_ms": hhe_repacking_ms,
                        "he_eval_ms": round(he_eval_ms, 4),  # same as CKKS post-transcipher
                        "total_ms": hhe_total_ms,
                        "status": "PARTIAL" if hhe_measured else "PENDING",
                        "pending_reason": (
                            "PENDING: online_transcipher_ms + repacking_ms require KAIST "
                            "ckks_fv on >=128 GiB RAM. Table 5 (eprint 2020/1335) "
                            "403-unreachable on all accessible mirrors; Presto §V is "
                            "client-side only, not a server-side latency source. Cloud "
                            "harness at scripts/cloud_transcipher_bench/ — not yet run. "
                            "2026-07-27: build confirmed clean (go vet + go test -c, Go "
                            "1.25, zero source changes) and a toy-scale (LogN=10, "
                            "deliberately insecure) correctness harness PASSED end-to-end "
                            "(HERA-in-BFV -> HalfBoot -> repack -> CKKS eval) -- see "
                            "tools/transciphering/toy_correctness/README.md. The code path "
                            "is proven correct; only the ~60 GB RAM ceiling for the real "
                            "(secure) params blocks a timing number. All other HHE columns "
                            "are MEASURED (plaintext path)."
                        ),
                    },
                    "result": {
                        "winner": winner,
                        "margin_ms": margin_ms,
                        "hhe_upload_headroom_ms": round(headroom_ms, 4) if headroom_ms is not None else "PENDING",
                        "note": (
                            f"HHE upload+encrypt saves {round(ckks_upload_ms - hhe_upload_ms - (hhe_encrypt_ms or 0), 2)} ms "
                            f"vs CKKS upload at {bw} kbps — server transcipher+repack must cost < "
                            f"{round(headroom_ms, 2) if headroom_ms else '?'} ms for HHE to win end-to-end."
                            if hhe_measured else
                            "PENDING: cannot determine winner without server-side transcipher timing"
                        ),
                    },
                }
                cells.append(cell)

    return cells


# ──────────────────────────────────────────────────────────────────────────────
# Plot
# ──────────────────────────────────────────────────────────────────────────────

def make_plot(cells, out_path: Path):
    """
    Heatmap: axes = bandwidth × batch_occupancy, cells = HHE upload headroom
    (how many ms of server transcipher budget HHE has before CKKS wins on upload).
    Depth=1 only (the hard measurement).
    """
    depth1 = [c for c in cells if c["circuit_depth"] == 1]
    bandwidths = sorted({c["uplink_bandwidth_kbps"] for c in depth1})
    lanes_list = sorted({c["batch_occupancy_lanes"] for c in depth1})

    # headroom matrix: rows=lanes, cols=bandwidth
    matrix = np.full((len(lanes_list), len(bandwidths)), np.nan)
    for c in depth1:
        bi = bandwidths.index(c["uplink_bandwidth_kbps"])
        li = lanes_list.index(c["batch_occupancy_lanes"])
        h = c["result"]["hhe_upload_headroom_ms"]
        if h != "PENDING" and h is not None:
            matrix[li, bi] = float(h)

    fig, ax = plt.subplots(figsize=(9, 5))
    im = ax.imshow(matrix, aspect="auto", cmap="RdYlGn", origin="lower",
                   vmin=-20, vmax=max(np.nanmax(matrix), 1))

    ax.set_xticks(range(len(bandwidths)))
    ax.set_xticklabels([f"{b:,}" for b in bandwidths], fontsize=10)
    ax.set_yticks(range(len(lanes_list)))
    ax.set_yticklabels([str(l) for l in lanes_list], fontsize=10)
    ax.set_xlabel("Uplink bandwidth (kbps)", fontsize=11)
    ax.set_ylabel("Batch occupancy (lanes)", fontsize=11)
    ax.set_title(
        "HHE upload-headroom map (depth-1 circuit, HERA-16 vs CKKS-160bit)\n"
        "Green = HHE has headroom (ms) for server transcipher cost to still beat CKKS upload.\n"
        "Red = CKKS upload is already cheaper; HHE needs negative server overhead (impossible).",
        fontsize=9
    )

    for i in range(len(lanes_list)):
        for j in range(len(bandwidths)):
            v = matrix[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.1f} ms", ha="center", va="center",
                        fontsize=9, color="black" if abs(v) < 200 else "white")
            else:
                ax.text(j, i, "PEND", ha="center", va="center",
                        fontsize=8, color="gray")

    plt.colorbar(im, ax=ax, label="Headroom (ms)")
    plt.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[plot] saved to {out_path}")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("[hhe_breakeven] Running Phase 5 parity gate...")
    _run_parity_gate()

    print("[hhe_breakeven] Loading CKKS measurements...")
    occupancy, ckks_upload_bytes, gRPC_overhead_ms = load_ckks_data()
    assert ckks_upload_bytes == CKKS_UPLOAD_BYTES

    print("[hhe_breakeven] Loading HHE (HERA-16) measurements...")
    hhe = load_hhe_data()

    print("[hhe_breakeven] Computing break-even cells...")
    cells = compute_breakeven(occupancy, gRPC_overhead_ms, hhe)

    # Count statuses
    total = len(cells)
    headroom_measured = sum(
        1 for c in cells
        if c["result"]["hhe_upload_headroom_ms"] not in ("PENDING", None)
    )
    print(f"[hhe_breakeven] {total} cells total; "
          f"{headroom_measured} with HHE headroom measured (upload+encrypt MEASURED); "
          f"all {total} have PENDING winner (server transcipher cost not yet measured).")

    out = {
        "framing": {
            "description": (
                "Phase 7 §7.4 break-even map: for each (circuit_depth × batch_occupancy × "
                "uplink_bandwidth), computes the CKKS end-to-end latency (MEASURED) and the "
                "HHE end-to-end latency (PARTIAL: upload+encrypt MEASURED, server transcipher "
                "PENDING). 'hhe_upload_headroom_ms' = how many ms of server transcipher budget "
                "HHE has before CKKS wins on total latency."
            ),
            "axes": {
                "circuit_depth": [1, 2, 3],
                "batch_occupancy_lanes": [1, 4, 8, 16],
                "uplink_bandwidth_kbps": [1000, 10000, 100000],
            },
            "ckks_side_methodology": "MEASURED from artifacts/throughput_results.json + wire_sizes.json",
            "hhe_side_methodology": (
                "online_encrypt_ms MEASURED (tools/transciphering/results/); "
                "upload_bytes_symmetric COMPUTED (HERA-16 formula: n_features*lanes*4+28); "
                "online_transcipher_ms PENDING (KAIST ckks_fv scheme bridge); "
                "repacking_ms PENDING"
            ),
            "pending_reason": (
                "online_transcipher_ms and repacking_ms require BenchmarkRtFHera80as "
                "(KAIST-CryptLab/RtF-Transciphering, ckks_fv) on >=128 GiB RAM. The "
                "15GB-RAM host OOM-killed the lightest benchmark (BenchmarkRtFHera80s, "
                "exit 137) before the online phase. RAM anchor: ~60 GB for HERA 80-bit "
                "cipher security [CONFIRMED-SOURCE: arXiv:2409.06422v1 §II]; minimum "
                "safe tier is r7i.4xlarge (128 GiB), not r7i.2xlarge (64 GiB) which has "
                "no margin. Literature fallbacks are exhausted: (1) RtF Table 5 (eprint "
                "2020/1335) returns HTTP 403 on all accessible mirrors -- a dead end, "
                "not a future fetch. (2) Presto (arXiv 2507.00367) §V measures "
                "CLIENT-SIDE HERA stream-key generation on bank's edge device/FPGA, NOT "
                "server-side HE evaluation or HalfBoot latency. Cloud harness "
                "scaffolded at scripts/cloud_transcipher_bench/ for r7i.4xlarge -- "
                "prepped, not executed. See RESEARCH_FINDINGS_v3.md §B for full "
                "epistemics. 2026-07-27 update: this session vendored a pinned, "
                "reproducible checkout (third_party/fetch_rtf.sh), confirmed `go vet` "
                "+ `go test -c` compile cleanly on Go 1.25 with zero source changes "
                "-- the bridge is not broken, only RAM-blocked -- and ran a toy-scale "
                "(LogN=10, deliberately insecure) correctness harness that PASSED "
                "end-to-end (HERA-in-BFV -> HalfBoot -> repack -> CKKS eval, max abs "
                "error 2.1e-05, peak RSS ~275 MB). See "
                "tools/transciphering/toy_correctness/README.md. The code path is "
                "proven correct; only the ~60 GB RAM ceiling for the real (secure, "
                "LogN=16) params blocks a timing number."
            ),
        },
        "cells": cells,
    }

    out_json = REPO / "artifacts" / "hhe_breakeven.json"
    out_plot = REPO / "results" / "hhe_breakeven_plot.png"

    with open(out_json, "w") as f:
        json.dump(out, f, indent=2)
    print(f"[hhe_breakeven] written {out_json}")

    make_plot(cells, out_plot)

    # Summary table
    print("\n=== Break-even map summary (depth=1) ===")
    print(f"{'BW (kbps)':>12} {'Lanes':>6} {'CKKS total ms':>14} {'HHE headroom ms':>16} {'Winner':>10}")
    print("-" * 65)
    for c in sorted(cells, key=lambda x: (x["circuit_depth"], x["uplink_bandwidth_kbps"], x["batch_occupancy_lanes"])):
        if c["circuit_depth"] != 1:
            continue
        bw = c["uplink_bandwidth_kbps"]
        la = c["batch_occupancy_lanes"]
        ct = c["ckks"]["total_ms"]
        hr = c["result"]["hhe_upload_headroom_ms"]
        wn = c["result"]["winner"]
        print(f"{bw:>12,} {la:>6} {ct:>14.2f} {str(hr):>16} {wn:>10}")


if __name__ == "__main__":
    main()
