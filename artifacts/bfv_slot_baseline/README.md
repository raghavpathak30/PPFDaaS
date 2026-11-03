# artifacts/bfv_slot_baseline

Everything here came from the FV-slot baseline work (reports/bfv_slot_baseline_report.md). Host: 15.99 GB laptop, ON BATTERY for every run listed, governor powersave.

**No timing in this directory is a comparison result.** Correctness, noise headroom, key counts, key bytes and peak RSS do not depend on the power source and may be cited. Wall-clock fields in these JSON files may not.

| Path | What it is |
|---|---|
| STOPPED_*.json | The Step 3 runner refusing to measure on battery |
| correctness_logn10/ | Slot path, LogN 10, full test set (56,962 rows), REPS=5. Step 2 evidence |
| probe_logn10/ | TestSlotProbeLayout output |
| cnp_regression_logn10/ | Existing TestCNPRun (cnp + baseline), LogN 10, REPS=2, stratified 4096 rows, unmodified code |
| memory_scaling/ | Slot at LogN 12 and 14, CNP at LogN 12, 16 rows, REPS=1. Peak RSS, headroom and key counts for the LogN 16 estimate |

A real comparison goes in laptop_<UTC>/ via scripts/bfv_slot_baseline/run_laptop_compare.py on AC power.
