#!/usr/bin/env python3
"""
Multi-run HE circuit latency benchmark collector
Runs both 160-bit and 200-bit benchmarks multiple times and computes statistics
"""

import subprocess
import sys
import os
from pathlib import Path
from statistics import mean, stdev, median
from datetime import datetime

def run_benchmark(executable_path, num_runs):
    """Run benchmark multiple times and collect latencies"""
    results = []
    for i in range(num_runs):
        try:
            output = subprocess.check_output([executable_path], stderr=subprocess.STDOUT, text=True)
            # Extract avg_us value
            for line in output.split('\n'):
                if 'avg_us=' in line:
                    us_value = float(line.split('avg_us=')[1].split()[0])
                    results.append(us_value)
                    print(f"  Run {i+1}/{num_runs}: {us_value:.2f} µs ({us_value/1000:.4f} ms)")
                    break
        except subprocess.CalledProcessError as e:
            print(f"  Run {i+1}/{num_runs}: ERROR - {e}")
            continue
    
    return results

def compute_stats(values):
    """Compute statistics for a list of values"""
    if not values:
        return None
    
    return {
        'mean': mean(values),
        'median': median(values),
        'min': min(values),
        'max': max(values),
        'stdev': stdev(values) if len(values) > 1 else 0,
        'count': len(values)
    }

def format_stats(stats):
    """Format statistics for display"""
    if not stats:
        return "N/A"
    
    return {
        'mean_us': f"{stats['mean']:.2f}",
        'mean_ms': f"{stats['mean']/1000:.4f}",
        'median_us': f"{stats['median']:.2f}",
        'median_ms': f"{stats['median']/1000:.4f}",
        'min_us': f"{stats['min']:.2f}",
        'min_ms': f"{stats['min']/1000:.4f}",
        'max_us': f"{stats['max']:.2f}",
        'max_ms': f"{stats['max']/1000:.4f}",
        'stdev_us': f"{stats['stdev']:.2f}",
        'stdev_ms': f"{stats['stdev']/1000:.4f}",
    }

def main():
    # Parse arguments
    build_dir = sys.argv[1] if len(sys.argv) > 1 else "./build"
    num_runs = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    
    benchmark_160 = Path(build_dir) / "vendor_server" / "benchmark_160"
    benchmark_200 = Path(build_dir) / "vendor_server" / "benchmark"
    
    # Validate executables exist
    if not benchmark_160.exists():
        print(f"Error: {benchmark_160} not found")
        sys.exit(1)
    if not benchmark_200.exists():
        print(f"Error: {benchmark_200} not found")
        sys.exit(1)
    
    print("=" * 60)
    print("Multi-Run HE Circuit Latency Benchmark")
    print("=" * 60)
    print(f"Number of runs per circuit: {num_runs}")
    print(f"Build directory: {build_dir}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Run 160-bit benchmarks
    print("Running 160-bit circuit benchmarks...")
    results_160 = run_benchmark(str(benchmark_160), num_runs)
    print()
    
    # Run 200-bit benchmarks
    print("Running 200-bit circuit benchmarks...")
    results_200 = run_benchmark(str(benchmark_200), num_runs)
    print()
    
    # Compute statistics
    stats_160 = compute_stats(results_160)
    stats_200 = compute_stats(results_200)
    fmt_160 = format_stats(stats_160)
    fmt_200 = format_stats(stats_200)
    
    print("=" * 60)
    print("Statistical Analysis")
    print("=" * 60)
    print()
    
    print("160-bit Circuit (Encrypt + multiply_plain):")
    print(f"  Sample count:   {stats_160['count']} runs")
    print(f"  Mean latency:   {fmt_160['mean_us']} µs ({fmt_160['mean_ms']} ms)")
    print(f"  Median latency: {fmt_160['median_us']} µs ({fmt_160['median_ms']} ms)")
    print(f"  Min latency:    {fmt_160['min_us']} µs ({fmt_160['min_ms']} ms)")
    print(f"  Max latency:    {fmt_160['max_us']} µs ({fmt_160['max_ms']} ms)")
    print(f"  Std deviation:  {fmt_160['stdev_us']} µs ({fmt_160['stdev_ms']} ms)")
    print()
    
    print("200-bit Circuit (Encrypt + depth1_he_inference):")
    print(f"  Sample count:   {stats_200['count']} runs")
    print(f"  Mean latency:   {fmt_200['mean_us']} µs ({fmt_200['mean_ms']} ms)")
    print(f"  Median latency: {fmt_200['median_us']} µs ({fmt_200['median_ms']} ms)")
    print(f"  Min latency:    {fmt_200['min_us']} µs ({fmt_200['min_ms']} ms)")
    print(f"  Max latency:    {fmt_200['max_us']} µs ({fmt_200['max_ms']} ms)")
    print(f"  Std deviation:  {fmt_200['stdev_us']} µs ({fmt_200['stdev_ms']} ms)")
    print()
    
    # Compute speedup
    speedup = stats_200['mean'] / stats_160['mean']
    print(f"Performance Ratio (160-bit vs 200-bit): {speedup:.2f}x faster")
    print()
    
    # Latency ranges
    overall_min = min(stats_160['min'], stats_200['min'])
    overall_max = max(stats_160['max'], stats_200['max'])
    print(f"Overall latency range: {overall_min/1000:.4f} ms — {overall_max/1000:.4f} ms")
    print()
    
    print("=" * 60)
    print("Individual Run Details")
    print("=" * 60)
    print()
    print("160-bit runs (µs):")
    for i, val in enumerate(results_160, 1):
        print(f"  {i:2d}.  {val:10.2f}")
    print()
    print("200-bit runs (µs):")
    for i, val in enumerate(results_200, 1):
        print(f"  {i:2d}.  {val:10.2f}")

if __name__ == "__main__":
    main()
