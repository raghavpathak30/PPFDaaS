#!/usr/bin/env python3
"""CNP-B vs slot per-transaction times with replicate ranges, from one run_laptop_compare.py directory.

  python3 scripts/bfv_slot_baseline/replicate_ranges.py artifacts/bfv_slot_baseline/laptop_<UTC>

Each arm has two runs in the session. Slot: slot and slot_repeat. CNP-B: cnp, plus the CNP stages inside the halfboot
arm, which is the same code on 16 rows (so fewer output ciphertexts). A range is the lowest and highest ratio over all
pairs of those runs. Writes replicate_ranges.json next to comparison.json. Reads run outputs only.
"""
import json
import sys
from pathlib import Path

METRICS = ("offline", "online", "total")


def slot_times(path):
    t = json.loads(path.read_text())["per_transaction_ms"]
    return {m: t[m] for m in METRICS}


def cnp_times(path):
    d = json.loads(path.read_text())
    stages, lanes, outs = d["cnp_stages"], d["lanes_per_output_ct"], d["outputs_per_crypt"]
    offline = (stages["keystream_crypt (offline)"]["median_ms"] / outs + stages["stc_modswitch (offline)"]["median_ms"]) / lanes
    online = d["per_output_ct_ms"]["total"] / lanes
    return {"offline": offline, "online": online, "total": offline + online}


def cell_ranges(cell):
    cipher = cell.name
    slot = [slot_times(cell / arm / f"slot_{cipher}.json") for arm in ("slot", "slot_repeat")]
    cnp = [cnp_times(cell / arm / f"cnp_{cipher}.json") for arm in ("cnp", "halfboot")]
    out = {"log_n": int(cell.parent.name.removeprefix("logn")), "cipher": cipher}
    for m in METRICS:
        s, c = [x[m] for x in slot], [x[m] for x in cnp]
        slow, fast = (s, c) if s[0] > c[0] else (c, s)
        out[m] = {
            "slot_ms": s, "cnp_ms": c, "winner": "cnp" if slow is s else "slot",
            "winner_faster_by": slow[0] / fast[0],
            "winner_faster_by_range": [min(slow) / max(fast), max(slow) / min(fast)],
        }
    return out


def main(run_dir):
    run = Path(run_dir)
    cells = [cell_ranges(d) for d in sorted(run.glob("logn*/*")) if d.is_dir()]
    (run / "replicate_ranges.json").write_text(json.dumps(cells, indent=1))
    for c in cells:
        for m in METRICS:
            r = c[m]
            lo, hi = r["winner_faster_by_range"]
            print(f"LogN {c['log_n']} {c['cipher']:6} {m:7} {r['winner']:4} faster by {r['winner_faster_by']:.2f}x (range {lo:.2f} to {hi:.2f})")


if __name__ == "__main__":
    main(sys.argv[1])
