#!/bin/bash

# Multi-run benchmark collector for HE circuit latency analysis
# Runs both 160-bit and 200-bit benchmarks multiple times and computes statistics

BUILD_DIR="${1:-.}/build"
NUM_RUNS="${2:-10}"

if [ ! -f "$BUILD_DIR/vendor_server/benchmark" ] || [ ! -f "$BUILD_DIR/vendor_server/benchmark_160" ]; then
    echo "Error: Benchmark executables not found in $BUILD_DIR/vendor_server/"
    echo "Usage: $0 [BUILD_DIR] [NUM_RUNS]"
    exit 1
fi

echo "========================================="
echo "Multi-Run HE Circuit Latency Benchmark"
echo "========================================="
echo "Number of runs per circuit: $NUM_RUNS"
echo "Build directory: $BUILD_DIR"
echo "Date: $(date)"
echo ""

# Function to extract microseconds from benchmark output
extract_us() {
    echo "$1" | grep "avg_us=" | awk -F'=' '{print $2}'
}

# Function to compute mean, min, max, stddev using Python
compute_stats() {
    local values=("$@")
    python3 << 'PYTHON_STATS'
import sys
import math

values = [float(x) for x in sys.argv[1:]]
n = len(values)

mean = sum(values) / n
min_val = min(values)
max_val = max(values)

variance = sum((x - mean) ** 2 for x in values) / n
stddev = math.sqrt(variance)

print(f"{mean:.2f} {min_val:.2f} {max_val:.2f} {stddev:.2f}")
PYTHON_STATS
}

# Run 160-bit benchmarks
echo "Running 160-bit circuit benchmarks..."
declare -a results_160
for i in $(seq 1 $NUM_RUNS); do
    echo -n "  Run $i/$NUM_RUNS... "
    output=$("$BUILD_DIR/vendor_server/benchmark_160" 2>&1)
    us=$(extract_us "$output")
    results_160+=("$us")
    ms=$(echo "scale=4; $us / 1000" | bc -l)
    echo "avg_us=$us (${ms}ms)"
done

echo ""
echo "Running 200-bit circuit benchmarks..."
declare -a results_200
for i in $(seq 1 $NUM_RUNS); do
    echo -n "  Run $i/$NUM_RUNS... "
    output=$("$BUILD_DIR/vendor_server/benchmark" 2>&1)
    us=$(extract_us "$output")
    results_200+=("$us")
    ms=$(echo "scale=4; $us / 1000" | bc -l)
    echo "avg_us=$us (${ms}ms)"
done

echo ""
echo "========================================="
echo "Statistical Analysis"
echo "========================================="
echo ""

# Compute stats for 160-bit
stats_160=$(compute_stats "${results_160[@]}")
mean_160=$(echo "$stats_160" | awk '{print $1}')
min_160=$(echo "$stats_160" | awk '{print $2}')
max_160=$(echo "$stats_160" | awk '{print $3}')
stddev_160=$(echo "$stats_160" | awk '{print $4}')

echo "160-bit Circuit (Encrypt + multiply_plain):"
echo "  Mean latency:   ${mean_160} µs ($(python3 -c "print(f'{${mean_160}/1000:.2f}')")ms)"
echo "  Min latency:    ${min_160} µs ($(python3 -c "print(f'{${min_160}/1000:.2f}')")ms)"
echo "  Max latency:    ${max_160} µs ($(python3 -c "print(f'{${max_160}/1000:.2f}')")ms)"
echo "  Std deviation:  ${stddev_160} µs ($(python3 -c "print(f'{${stddev_160}/1000:.2f}')")ms)"
echo ""

# Compute stats for 200-bit
stats_200=$(compute_stats "${results_200[@]}")
mean_200=$(echo "$stats_200" | awk '{print $1}')
min_200=$(echo "$stats_200" | awk '{print $2}')
max_200=$(echo "$stats_200" | awk '{print $3}')
stddev_200=$(echo "$stats_200" | awk '{print $4}')

echo "200-bit Circuit (Encrypt + depth1_he_inference):"
echo "  Mean latency:   ${mean_200} µs ($(python3 -c "print(f'{${mean_200}/1000:.2f}')")ms)"
echo "  Min latency:    ${min_200} µs ($(python3 -c "print(f'{${min_200}/1000:.2f}')")ms)"
echo "  Max latency:    ${max_200} µs ($(python3 -c "print(f'{${max_200}/1000:.2f}')")ms)"
echo "  Std deviation:  ${stddev_200} µs ($(python3 -c "print(f'{${stddev_200}/1000:.2f}')")ms)"
echo ""

# Compute speedup
speedup=$(python3 -c "print(f'{${mean_200}/${mean_160}:.2f}')")
echo "Speedup (160-bit vs 200-bit): ${speedup}x"
echo ""

# Latency range
min_overall=$(python3 -c "print(min(${min_160}, ${min_200}))")
max_overall=$(python3 -c "print(max(${max_160}, ${max_200}))")
echo "Overall latency range: $(python3 -c "print(f'{${min_overall}/1000:.2f}')")ms — $(python3 -c "print(f'{${max_overall}/1000:.2f}')")ms"
echo ""

# All individual runs
echo "========================================="
echo "Individual Run Details"
echo "========================================="
echo ""
echo "160-bit runs (µs):"
printf '%s\n' "${results_160[@]}" | nl
echo ""
echo "200-bit runs (µs):"
printf '%s\n' "${results_200[@]}" | nl
