#!/usr/bin/env python3
"""
scripts/c3_client_server_comparison.py — C3 re-derivation machinery
(RESEARCH_FINDINGS.md Block C3).

Builds the three explicit comparisons needed to decide which thesis the
data supports -- "client-encrypt dominates" vs "small HE circuits make
transciphering's overhead not worth it at low depth" -- WITHOUT issuing
that verdict itself. This script only emits tables; it never states a
conclusion about which framing the numbers favor.

Reads from a single named snapshot directory, never from production
artifacts/ directly, so the same invocation can be pointed at either the
powersave plumbing snapshot or the governor-validated performance snapshot
once scripts/governor_harness/revalidate_under_performance.py has produced
artifacts/performance_revalidation/<snapshot>/.

Usage:
    python3 scripts/c3_client_server_comparison.py --snapshot baseline_powersave
    python3 scripts/c3_client_server_comparison.py --snapshot performance
    python3 scripts/c3_client_server_comparison.py --snapshot performance --json-out artifacts/c3_comparison.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REVAL_DIR = REPO_ROOT / "artifacts" / "performance_revalidation"

HERA_LANES = [1, 4, 8, 16]
AMORT_LANES = [1, 4, 8, 16]

# Snapshot directory name -> the cpu_governor value every file inside it
# must be stamped with. Mirrors SNAPSHOT_EXPECTED_GOVERNOR in
# scripts/governor_harness/revalidate_under_performance.py -- keep in sync.
SNAPSHOT_EXPECTED_GOVERNOR = {
    "baseline_powersave": "powersave",
    "performance": "performance",
}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _assert_manifest_consistent(snapshot_dir: Path, snapshot_label: str, name: str) -> None:
    """Hard-error if name's *.governor_manifest.json sidecar doesn't say
    cpu_governor == the governor this snapshot directory claims to hold.
    This is what stops a stray powersave-stamped file (e.g. left over from
    a plumbing/--allow-non-performance run) from silently being read as if
    it were a real performance-governor measurement."""
    expected = SNAPSHOT_EXPECTED_GOVERNOR[snapshot_label]
    data_path = snapshot_dir / name
    stem = Path(name).stem
    manifest_path = snapshot_dir / f"{stem}.governor_manifest.json"
    if not manifest_path.exists():
        sys.exit(
            f"ERROR: {_rel(manifest_path)} missing.\n"
            f"{_rel(data_path)} has no governor manifest sidecar -- "
            "refusing to read it without knowing what conditions produced it. "
            "Re-run scripts/governor_harness/revalidate_under_performance.py, which "
            "writes a sidecar for every file it places in a snapshot directory."
        )
    actual = json.loads(manifest_path.read_text()).get("cpu_governor")
    if actual != expected:
        sys.exit(
            f"ERROR: manifest/snapshot mismatch for {_rel(data_path)}.\n"
            f"  snapshot requested : {snapshot_label!r} (expects cpu_governor={expected!r})\n"
            f"  manifest says       : cpu_governor={actual!r}\n"
            f"Refusing to read this file as part of a {snapshot_label!r} snapshot. "
            "This usually means a file from one governor condition ended up in the "
            "wrong snapshot directory -- fix the snapshot, don't bypass this check."
        )


def _load(snapshot_dir: Path, snapshot_label: str, name: str) -> dict:
    path = snapshot_dir / name
    if not path.exists():
        sys.exit(
            f"ERROR: {_rel(path)} not found.\n"
            "Run scripts/governor_harness/revalidate_under_performance.py "
            "first (it populates this snapshot directory)."
        )
    _assert_manifest_consistent(snapshot_dir, snapshot_label, name)
    return json.loads(path.read_text())


def _table_a_single_request(e2e: dict) -> list[dict]:
    """(a) Single-request: client encode+encrypt vs server inference, per chain."""
    rows = []
    for chain in ("160bit", "200bit"):
        d = e2e[chain]
        client_us = d["client_stage_us"]["encode_encrypt_us"]["median_us"]
        server_us = d["server_stage_us"]["total_inference_us"]["median_us"]
        rows.append({
            "chain": chain,
            "client_encode_encrypt_median_us": round(client_us, 1),
            "server_total_inference_median_us": round(server_us, 1),
            "client_minus_server_us": round(client_us - server_us, 1),
            "client_over_server_ratio": round(client_us / server_us, 3),
            "which_is_larger": "client" if client_us > server_us else "server",
        })
    return rows


def _table_b_amortized(e2e: dict, amort: dict) -> list[dict]:
    """(b) Amortized-at-occupancy: fixed per-request client encrypt vs
    server compute that amortizes with batch occupancy (lanes)."""
    # Client encrypt is paid once per independently-encrypted bank request,
    # regardless of how many lanes the server batches server-side -- so it
    # does not amortize with lanes the way server compute does.
    client_us_160 = e2e["160bit"]["client_stage_us"]["encode_encrypt_us"]["median_us"]
    by_lanes = {row["lanes"]: row for row in amort["amortization"]}
    rows = []
    for lanes in AMORT_LANES:
        if lanes not in by_lanes:
            continue
        server_per_tx_us = by_lanes[lanes]["per_tx_us"]
        rows.append({
            "lanes": lanes,
            "client_encrypt_us_fixed_per_request": round(client_us_160, 1),
            "server_amortized_per_tx_us": round(server_per_tx_us, 1),
            "amortization_factor_server": by_lanes[lanes]["amortization_factor"],
            "client_over_server_ratio": round(client_us_160 / server_per_tx_us, 3),
            "which_is_larger": "client" if client_us_160 > server_per_tx_us else "server",
        })
    return rows


def _table_c_lane_for_lane_hera(e2e: dict, snapshot_dir: Path, snapshot_label: str) -> list[dict]:
    """(c) Lane-for-lane: CKKS client encrypt (fixed per request) vs HERA-16
    client encrypt (scales with lanes -- each lane is its own symmetric block)."""
    ckks_us_160 = e2e["160bit"]["client_stage_us"]["encode_encrypt_us"]["median_us"]
    rows = []
    for lanes in HERA_LANES:
        name = f"hera_bench_lane{lanes}.json"
        hera_path = snapshot_dir / name
        if not hera_path.exists():
            rows.append({
                "lanes": lanes,
                "ckks_encrypt_median_us": round(ckks_us_160, 1),
                "hera_encrypt_median_us": "MISSING",
                "note": f"{_rel(hera_path)} not found in this snapshot",
            })
            continue
        hera = _load(snapshot_dir, snapshot_label, name)
        hera_us = hera["online_encrypt_median_ms"] * 1000.0
        rows.append({
            "lanes": lanes,
            "ckks_encrypt_median_us": round(ckks_us_160, 1),
            "hera_encrypt_median_us": round(hera_us, 1),
            "ckks_minus_hera_us": round(ckks_us_160 - hera_us, 1),
            "ckks_over_hera_ratio": round(ckks_us_160 / hera_us, 3),
            "which_is_larger": "ckks" if ckks_us_160 > hera_us else "hera",
            "hera_upload_bytes": hera.get("upload_symmetric_bytes"),
            "ckks_upload_bytes": hera.get("upload_ckks_standard_bytes"),
        })
    return rows


def _print_table(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if not rows:
        print("  (no rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, choices=["baseline_powersave", "performance"],
                        help="which artifacts/performance_revalidation/<snapshot>/ directory to read from")
    parser.add_argument("--json-out", default=None,
                        help="optional path to also write the three tables as JSON")
    parser.add_argument("--plumbing-ok", action="store_true",
                        help="acknowledge that a --snapshot baseline_powersave run is "
                             "PLUMBING ONLY, not a thesis-deciding measurement, and "
                             "suppress nothing else -- the banner prints regardless")
    args = parser.parse_args()

    snapshot_dir = REVAL_DIR / args.snapshot
    if not snapshot_dir.exists():
        sys.exit(
            f"ERROR: {_rel(snapshot_dir)} does not exist.\n"
            "Run scripts/governor_harness/revalidate_under_performance.py first."
        )

    e2e = _load(snapshot_dir, args.snapshot, "e2e_latency_breakdown.json")
    amort = _load(snapshot_dir, args.snapshot, "amortization_table.json")

    table_a = _table_a_single_request(e2e)
    table_b = _table_b_amortized(e2e, amort)
    table_c = _table_c_lane_for_lane_hera(e2e, snapshot_dir, args.snapshot)

    print("=" * 70)
    if args.snapshot == "baseline_powersave":
        print("PLUMBING -- powersave, NOT a thesis verdict.")
        print("These numbers are governor-sensitive and not valid for citation.")
        print("Re-run with --snapshot performance after the governor harness pass.")
    else:
        print(f"C3 comparison tables -- snapshot: {args.snapshot}")
        print("This script does not state which thesis the data supports.")
        print("That call is made separately, by a human, after reviewing these tables.")
    print("=" * 70)

    _print_table("(a) Single-request: client encode+encrypt vs server inference (per chain)", table_a)
    _print_table("(b) Amortized-at-occupancy: fixed client encrypt vs amortized server compute (160-bit)", table_b)
    _print_table("(c) Lane-for-lane: CKKS client encrypt vs HERA-16 client encrypt (160-bit CKKS chain)", table_c)

    if args.json_out:
        out = {
            "snapshot": args.snapshot,
            "is_plumbing_only": args.snapshot == "baseline_powersave",
            "table_a_single_request": table_a,
            "table_b_amortized_occupancy": table_b,
            "table_c_lane_for_lane_hera": table_c,
        }
        out_path = REPO_ROOT / args.json_out
        out_path.write_text(json.dumps(out, indent=2))
        print(f"\nWrote {_rel(out_path)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
