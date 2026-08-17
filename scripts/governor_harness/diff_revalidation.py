#!/usr/bin/env python3
"""
scripts/governor_harness/diff_revalidation.py

Diffs every artifact in artifacts/performance_revalidation/baseline_powersave/
against its counterpart in artifacts/performance_revalidation/performance/,
flagging any numeric field whose value moved by more than --threshold percent.

This does not judge whether a move is "good" or "bad" -- it only tells you
where the powersave-vs-performance gap is large enough that a paper claim
built on the powersave number would need re-deriving. Designed to be run
after scripts/governor_harness/revalidate_under_performance.py has produced
both directories.

Usage:
    python3 scripts/governor_harness/diff_revalidation.py
    python3 scripts/governor_harness/diff_revalidation.py --threshold 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REVAL_DIR = REPO_ROOT / "artifacts" / "performance_revalidation"
BASELINE_DIR = REVAL_DIR / "baseline_powersave"
PERFORMANCE_DIR = REVAL_DIR / "performance"

DEFAULT_THRESHOLD_PCT = 15.0

# Field-name substrings worth calling out explicitly even below threshold,
# because they are the specific ratios this project's headline claims rest on
# (RESEARCH_FINDINGS.md step 1/3: privacy-cost delta, amortization factor).
HEADLINE_PATTERNS = ("pct_delta", "pct_reduction", "amortization_factor", "delta")

# Top-level keys holding per-iteration raw samples (n=100-1000 each). Diffing
# every individual sample restates the same summary-level fact thousands of
# times and drowns the actual signal -- the summary/gates/statistical_tests
# fields already capture what moved. Excluded from the diff, not hidden from
# the artifact itself.
RAW_SAMPLE_KEYS = ("raw_results", "raw_samples")


def _flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix] = obj
    return out


def _strip_raw_samples(obj: dict) -> dict:
    if not isinstance(obj, dict):
        return obj
    return {k: v for k, v in obj.items() if k not in RAW_SAMPLE_KEYS}


def _diff_pair(name: str, old: dict, new: dict, threshold: float) -> list[dict]:
    old_flat = _flatten(_strip_raw_samples(old))
    new_flat = _flatten(_strip_raw_samples(new))
    rows = []
    for path in sorted(set(old_flat) & set(new_flat)):
        ov, nv = old_flat[path], new_flat[path]
        if ov == 0:
            pct = float("inf") if nv != 0 else 0.0
        else:
            pct = (nv - ov) / abs(ov) * 100.0
        flagged = abs(pct) > threshold
        headline = any(p in path for p in HEADLINE_PATTERNS)
        if flagged or headline:
            rows.append({
                "artifact": name, "path": path,
                "old": ov, "new": nv, "pct_change": pct,
                "flagged": flagged, "headline": headline,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                        help=f"flag fields whose |%% change| exceeds this (default {DEFAULT_THRESHOLD_PCT})")
    args = parser.parse_args()

    if not BASELINE_DIR.exists() or not PERFORMANCE_DIR.exists():
        print(
            f"ERROR: missing {BASELINE_DIR} or {PERFORMANCE_DIR}.\n"
            "Run scripts/governor_harness/revalidate_under_performance.py first.",
            file=sys.stderr,
        )
        return 1

    baseline_files = sorted(p.name for p in BASELINE_DIR.glob("*.json") if "governor_manifest" not in p.name)
    performance_files = sorted(p.name for p in PERFORMANCE_DIR.glob("*.json") if "governor_manifest" not in p.name)
    common = sorted(set(baseline_files) & set(performance_files))
    missing_in_perf = sorted(set(baseline_files) - set(performance_files))
    missing_in_base = sorted(set(performance_files) - set(baseline_files))

    if missing_in_perf:
        print(f"WARNING: not yet re-measured under performance: {missing_in_perf}")
    if missing_in_base:
        print(f"WARNING: present under performance but no baseline snapshot: {missing_in_base}")

    all_rows = []
    for fname in common:
        old = json.loads((BASELINE_DIR / fname).read_text())
        new = json.loads((PERFORMANCE_DIR / fname).read_text())
        all_rows.extend(_diff_pair(fname, old, new, args.threshold))

    flagged_rows = [r for r in all_rows if r["flagged"]]
    headline_rows = [r for r in all_rows if r["headline"]]

    print(f"\nCompared {len(common)} artifact(s) at threshold={args.threshold}%\n")

    if headline_rows:
        print("Headline ratios (privacy-cost delta / amortization factor / pct fields):")
        print(f"  {'artifact':<38} {'path':<45} {'old':>14} {'new':>14} {'%chg':>10}")
        for r in headline_rows:
            flag = " <<< FLAGGED" if r["flagged"] else ""
            print(f"  {r['artifact']:<38} {r['path']:<45} {r['old']:>14} {r['new']:>14} {r['pct_change']:>9.2f}%{flag}")
        print()

    other_flagged = [r for r in flagged_rows if not r["headline"]]
    if other_flagged:
        print(f"Other fields that moved >{args.threshold}%:")
        print(f"  {'artifact':<38} {'path':<45} {'old':>14} {'new':>14} {'%chg':>10}")
        for r in other_flagged:
            print(f"  {r['artifact']:<38} {r['path']:<45} {r['old']:>14} {r['new']:>14} {r['pct_change']:>9.2f}%")
        print()

    if not flagged_rows:
        print(f"No field moved by more than {args.threshold}% between powersave and performance.")

    print("=" * 70)
    if flagged_rows:
        print(f"RESULT: {len(flagged_rows)} field(s) flagged. Any paper claim built on a "
              "flagged powersave number must be re-derived from the performance-governor "
              "artifact before citing.")
        return 1
    print("RESULT: powersave numbers are within tolerance of the performance-governor "
          "re-run for every compared field.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
