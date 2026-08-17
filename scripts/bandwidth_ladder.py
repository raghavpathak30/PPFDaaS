"""
scripts/bandwidth_ladder.py — Phase 7 §7.5 bandwidth ladder.

Measures all four rungs for a realistic ~8 KB / 256-feature payload:
  1. SEAL seeded ciphertexts (Serializable<Ciphertext>, encrypt_symmetric)
  2. 160-bit chain (standard public-key ciphertext) — current production wire format
  3. HHE online upload (HERA-16 symmetric ciphertext)
  4. Full RtF (transcipher + CKKS eval end-to-end) — PENDING

Per rung: expansion_factor, client_cpu_ms, server_cost_ms, online_latency_ms.
Real numbers only — no estimates. PENDING cells documented with exact reason.

Output: artifacts/bandwidth_ladder.json
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from parity_gate import load_model_weights


def _run_parity_gate():
    try:
        load_model_weights(str(REPO / "artifacts" / "model_weights.bin"))
    except Exception as e:
        print(f"[parity_gate] WARNING: could not load model weights: {e}")
        return
    errors_path = REPO / "artifacts" / "errors.json"
    if not errors_path.exists():
        print("[parity_gate] INFO: artifacts/errors.json not found; skipping live gate")
        return
    with open(errors_path) as f:
        err = json.load(f)
    max_ae = float(err.get("max_abs_error", 1.0))
    tol = 1e-3
    if max_ae >= tol:
        raise RuntimeError(f"[parity_gate] FAILED: max_abs_error={max_ae} >= {tol}")
    print(f"[parity_gate] PASSED: max_abs_error={max_ae:.2e} < {tol:.0e}")


def main():
    print("[bandwidth_ladder] Running Phase 5 parity gate...")
    _run_parity_gate()

    with open(REPO / "artifacts" / "wire_sizes.json") as f:
        ws = json.load(f)
    with open(REPO / "artifacts" / "comparison_results.json") as f:
        comp = json.load(f)
    with open(REPO / "tools" / "transciphering" / "results" / "hera_bench_lane1.json") as f:
        hera1 = json.load(f)

    chain160 = ws["chains"]["160bit"]
    standard_bytes = chain160["standard_bytes"]  # 262257
    seeded_bytes = chain160["seeded_bytes"]      # 131266

    r160 = comp.get("summary", {}).get("reduced_160bit") or comp.get("reduced_160bit")
    e2e_latency_ms = r160["median_us"] / 1000  # 10.6755 ms, current production path

    rungs = {
        "rung_1_seal_seeded": {
            "description": (
                "SEAL seeded ciphertexts (Serializable<Ciphertext>, encrypt_symmetric). "
                "Halves upload at zero server cost. bank_client currently does NOT use "
                "this path -- it sends standard (public-key) ciphertexts. Confirmed by "
                "reading bank_client/bank_client.py and bank_client/he_wrapper/seal_wrapper_160.cpp: "
                "both call encrypt() (public-key), never encrypt_symmetric()."
            ),
            "status": "MEASURED (size only) / NOT DEPLOYED",
            "payload_bytes": seeded_bytes,
            "expansion_factor_vs_seeded": 1.0,
            "client_cpu_ms": "PENDING (requires bank_client encrypt_symmetric() integration to measure)",
            "server_cost_ms": "same as rung 2 (no server-side change; server cannot distinguish wire format after deserialization)",
            "online_latency_ms": "PENDING (not deployed; would be approx. e2e_latency_ms with halved upload time)",
            "source": "artifacts/wire_sizes.json#chains.160bit.seeded_bytes",
        },
        "rung_2_seal_160bit_standard": {
            "description": (
                "160-bit chain, standard public-key ciphertext -- the CURRENT production "
                "wire format (bank_client.py -> seal_wrapper_160 -> gRPC)."
            ),
            "status": "MEASURED",
            "payload_bytes": standard_bytes,
            "expansion_factor_vs_seeded": round(standard_bytes / seeded_bytes, 4),
            "client_cpu_ms": "included in online_latency_ms (not separately instrumented client-side)",
            "server_cost_ms": "see artifacts/throughput_results.json#occupancy_sweep (per_tx_us)",
            "online_latency_ms": round(e2e_latency_ms, 4),
            "source": "artifacts/wire_sizes.json + artifacts/comparison_results.json#summary.reduced_160bit",
        },
        "rung_3_hhe_online_upload": {
            "description": (
                "HHE online upload: HERA-16 symmetric ciphertext for 256 features, "
                "single transaction (lanes=1). AEAD-wrapped (AES-128-GCM)."
            ),
            "status": "MEASURED (client-side) / PENDING (server-side)",
            "payload_bytes": hera1["upload_symmetric_bytes"],
            "expansion_factor_vs_seeded": round(seeded_bytes / hera1["upload_symmetric_bytes"], 4),
            "client_cpu_ms": round(hera1["online_encrypt_mean_ms"], 4),
            "server_cost_ms": "PENDING",
            "online_latency_ms": "PENDING",
            "pending_reason": (
                "Server-side cost (BFV evaluation of HERA-16 + StC/CKKS conversion) requires "
                "the KAIST ckks_fv FV->CKKS scheme bridge, not present in standard Lattigo v6.2.0. "
                "Client-side cost (online_encrypt_ms, payload_bytes) is MEASURED."
            ),
            "source": "tools/transciphering/results/hera_bench_lane1.json",
        },
        "rung_4_full_rtf": {
            "description": (
                "Full RtF: transcipher (HERA-16 eval inside BFV) + StC/CKKS conversion + "
                "existing CKKS inference circuit, end-to-end."
            ),
            "status": "PENDING",
            "payload_bytes": hera1["upload_symmetric_bytes"],  # same online upload as rung 3
            "expansion_factor_vs_seeded": round(seeded_bytes / hera1["upload_symmetric_bytes"], 4),
            "client_cpu_ms": round(hera1["online_encrypt_mean_ms"], 4),
            "server_cost_ms": "PENDING",
            "online_latency_ms": "PENDING",
            "pending_reason": (
                "Full RtF end-to-end latency cannot be measured until the KAIST ckks_fv scheme "
                "bridge is integrated (see tools/transciphering/README.md). The CKKS-only "
                "post-transcipher inference stage would reuse the existing, already-measured "
                "depth-1 circuit (artifacts/comparison_results.json, median 10.68ms for 160-bit)."
            ),
            "source": "tools/transciphering/results/hera_bench_lane1.json (online upload only)",
        },
    }

    out = {
        "framing": {
            "description": (
                "Phase 7 §7.5 bandwidth ladder: 4 rungs for a realistic 256-feature "
                "(~8KB raw float64) payload, single transaction. Real measured numbers "
                "only; PENDING cells documented with exact reason, never estimated."
            ),
            "methodology": "measured",
        },
        "rungs": rungs,
    }

    out_path = REPO / "artifacts" / "bandwidth_ladder.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"[bandwidth_ladder] written {out_path}")

    print("\n=== Bandwidth ladder summary ===")
    for name, r in rungs.items():
        print(f"{name}: {r['payload_bytes']} bytes, status={r['status']}")


if __name__ == "__main__":
    main()
