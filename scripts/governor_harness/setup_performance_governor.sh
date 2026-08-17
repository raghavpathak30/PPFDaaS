#!/usr/bin/env bash
# scripts/governor_harness/setup_performance_governor.sh
#
# Puts the host into the most deterministic state practical for HE
# microbenchmarks, per RESEARCH_FINDINGS.md Block D1:
#   - cpufreq governor = performance on every core
#   - turbo boost disabled (intel_pstate/no_turbo, falling back to
#     generic cpufreq/boost)
#   - min freq pinned to max freq (reduces frequency-scaling variance)
# Requires root. Run on the actual benchmark host (the ROG box), not in CI.
#
# Usage:
#   sudo ./scripts/governor_harness/setup_performance_governor.sh
#   sudo ./scripts/governor_harness/setup_performance_governor.sh --restore   # revert to powersave
set -euo pipefail

MODE="${1:-}"
NCPU=$(nproc)

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: must run as root (sudo)." >&2
    exit 1
fi

restore() {
    echo "Restoring powersave governor and re-enabling turbo on all $NCPU cores..."
    for ((i = 0; i < NCPU; i++)); do
        gov_path="/sys/devices/system/cpu/cpu$i/cpufreq/scaling_governor"
        minf_path="/sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_min_freq"
        scaling_min_path="/sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq"
        [[ -f "$gov_path" ]] && echo powersave > "$gov_path"
        if [[ -f "$minf_path" && -f "$scaling_min_path" ]]; then
            cat "$minf_path" > "$scaling_min_path"
        fi
    done
    if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
        echo 0 > /sys/devices/system/cpu/intel_pstate/no_turbo
    elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
        echo 1 > /sys/devices/system/cpu/cpufreq/boost
    fi
    echo "Done. Verify with: cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
    exit 0
}

if [[ "$MODE" == "--restore" ]]; then
    restore
fi

echo "Setting performance governor on all $NCPU cores..."
for ((i = 0; i < NCPU; i++)); do
    gov_path="/sys/devices/system/cpu/cpu$i/cpufreq/scaling_governor"
    if [[ ! -f "$gov_path" ]]; then
        echo "WARNING: $gov_path not found (cpu$i has no cpufreq?), skipping." >&2
        continue
    fi
    echo performance > "$gov_path"
done

echo "Pinning min freq == max freq on all $NCPU cores (reduces scaling variance)..."
for ((i = 0; i < NCPU; i++)); do
    maxf_path="/sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_max_freq"
    scaling_min_path="/sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq"
    if [[ -f "$maxf_path" && -f "$scaling_min_path" ]]; then
        cat "$maxf_path" > "$scaling_min_path"
    fi
done

echo "Disabling turbo boost..."
if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
    echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo
    echo "  intel_pstate/no_turbo = 1"
elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
    echo 0 > /sys/devices/system/cpu/cpufreq/boost
    echo "  cpufreq/boost = 0"
else
    echo "WARNING: no known turbo-control file found (neither intel_pstate/no_turbo" >&2
    echo "  nor cpufreq/boost exists). Turbo state unknown -- record this in the manifest." >&2
fi

echo
echo "Verification:"
echo "  governors: $(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u)"
if [[ -f /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
    echo "  intel_pstate/no_turbo: $(cat /sys/devices/system/cpu/intel_pstate/no_turbo)"
elif [[ -f /sys/devices/system/cpu/cpufreq/boost ]]; then
    echo "  cpufreq/boost: $(cat /sys/devices/system/cpu/cpufreq/boost)"
fi
echo
echo "All governors should read 'performance' above. If any core didn't change,"
echo "investigate before benchmarking (e.g. that core may be offline or use a"
echo "different driver)."
echo
echo "To revert after benchmarking: sudo $0 --restore"
