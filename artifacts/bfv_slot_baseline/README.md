# artifacts/bfv_slot_baseline

Everything here came from the FV-slot baseline work (reports/bfv_slot_baseline_report.md). Host: 15.99 GB laptop, governor powersave.

**Only laptop_20261103T205043Z/ holds comparison timings: it ran on AC.** Every other run listed was ON BATTERY. For those, correctness, noise headroom, key counts, key bytes and peak RSS do not depend on the power source and may be cited; their wall-clock fields may not.

| Path | What it is |
|---|---|
| laptop_20261103T205043Z/ | Step 3 same-session comparison, AC, LogN 10 and 12, REPS=5. comparison.md, comparison.json, replicate_ranges.json, per-arm JSON and machine state, contention_monitor.jsonl |
| STOPPED_*.json | The Step 3 runner refusing to measure on battery |
| correctness_logn10/ | Slot path, LogN 10, full test set (56,962 rows), REPS=5. Step 2 evidence |
| level0_gate_stop_logn10/ | SLOT_EVAL_LEVEL=0, the literal round(c*q0/t) reading: stopped at the headroom gate |
| probe_logn10/ | TestSlotProbeLayout output |
| cnp_regression_logn10/ | Existing TestCNPRun (cnp + baseline), LogN 10, REPS=2, stratified 4096 rows, unmodified code |
| memory_scaling/ | Slot at LogN 12 and 14, CNP at LogN 12, 16 rows, REPS=1. Peak RSS, headroom and key counts for the LogN 16 estimate |

New comparisons go in laptop_<UTC>/ via scripts/bfv_slot_baseline/run_laptop_compare.py on AC power.
