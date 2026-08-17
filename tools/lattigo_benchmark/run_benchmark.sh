#!/usr/bin/env bash
# tools/lattigo_benchmark/run_benchmark.sh
#
# Governor-validated run of the Lattigo hoisting benchmark.
# Must be called after the performance governor is set up:
#   sudo ../../scripts/governor_harness/setup_performance_governor.sh
#
# Taskset: -c 0,2,4,6,8,10 (the six P-cores, even IDs, on i7-13650HX).
# This is the SAME taskset used for all prior governor-validated SEAL/OpenFHE
# runs (confirmed in artifacts/performance_revalidation/performance/*.governor_manifest.json:
# "taskset_cpus": "0,2,4,6,8,10"). Using the same CPU budget makes the
# cross-library comparison valid; note that SEAL's bsgs_reduction uses OpenMP
# across all 6 cores, while Lattigo is single-threaded (GOMAXPROCS=1).
#
# GOMAXPROCS=1: forces Go's runtime to a single OS thread during the hot path.
# Lattigo's RotateHoistedNew has no internal parallelism; without this, Go's
# GC goroutines could interfere with benchmark timings on the other 5 cores.
#
# Usage (from tools/lattigo_benchmark/):
#   bash run_benchmark.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CPUS="0,2,4,6,8,10"

# Sanity-check governor on ALL benchmark cores before running.
FAIL=0
for cpu in 0 2 4 6 8 10; do
    GOV=$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_governor 2>/dev/null || echo UNKNOWN)
    if [[ "$GOV" != "performance" ]]; then
        echo "ERROR: cpu${cpu} governor='${GOV}', expected 'performance'." >&2
        FAIL=1
    fi
done
if [[ "$FAIL" -ne 0 ]]; then
    echo "Run: sudo ../../scripts/governor_harness/setup_performance_governor.sh" >&2
    exit 1
fi

mkdir -p results
echo "GOMAXPROCS=1 taskset -c ${CPUS} go run . (governor=performance on all benchmark cores)"
GOMAXPROCS=1 taskset -c "${CPUS}" go run .
