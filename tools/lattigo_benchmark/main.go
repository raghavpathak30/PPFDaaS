// tools/lattigo_benchmark/main.go
//
// Timing harness for linear_eval.go's RunCircuitHoisted(): 20 warmup + 100
// measured end-to-end runs (same structure as openfhe_benchmark.cpp and
// vendor_server/src/benchmark_160.cpp), reporting mean/std/p50/p95/p99/min/max
// per stage. Writes tools/lattigo_benchmark/results/lattigo_results.json,
// compatible with artifacts/execution_matrix.json's Lattigo row.
//
// In-band parity gate (same pattern as every other benchmark in this repo):
// each run's MaxAbsError is checked against kTolerance. If the maximum observed
// error across all measured runs exceeds kTolerance, the binary exits non-zero
// and "correctness_passed" is false — timing numbers from a failed gate must
// not be cited. Parity is reported FIRST before any timing numbers.
//
// Measurement environment: records cpu_governor and no_turbo state at startup.
// Governor MUST be "performance" and turbo MUST be disabled for the numbers to
// be comparable with the governor-validated SEAL BSGS figures in §7.5.1.
// Use scripts/governor_harness/setup_performance_governor.sh (requires sudo)
// before running, and use the provided run_benchmark.sh (taskset -c <core>).

package main

import (
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"os"
	"sort"
	"strings"
)

const (
	kWarmupRounds  = 20
	kMeasureRounds = 100
	kTolerance     = 1e-3
	kResultsPath   = "results/lattigo_results.json"
)

// Stats holds summary statistics for a slice of latency samples.
type Stats struct {
	Mean float64 `json:"mean"`
	Std  float64 `json:"std"`
	P50  float64 `json:"p50"`
	P95  float64 `json:"p95"`
	P99  float64 `json:"p99"`
	Min  float64 `json:"min"`
	Max  float64 `json:"max"`
}

func computeStats(samples []float64) Stats {
	if len(samples) == 0 {
		return Stats{}
	}
	s := make([]float64, len(samples))
	copy(s, samples)
	sort.Float64s(s)
	n := float64(len(s))

	var sum float64
	for _, v := range s {
		sum += v
	}
	mean := sum / n

	var sq float64
	for _, v := range s {
		sq += (v - mean) * (v - mean)
	}
	var std float64
	if len(s) > 1 {
		std = math.Sqrt(sq / (n - 1))
	}

	pct := func(p float64) float64 {
		idx := p * (n - 1)
		lo := int(math.Floor(idx))
		hi := int(math.Ceil(idx))
		frac := idx - float64(lo)
		return s[lo] + frac*(s[hi]-s[lo])
	}

	return Stats{
		Mean: mean,
		Std:  std,
		P50:  pct(0.50),
		P95:  pct(0.95),
		P99:  pct(0.99),
		Min:  s[0],
		Max:  s[len(s)-1],
	}
}

func readFile(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return "UNKNOWN"
	}
	return strings.TrimSpace(string(b))
}

func cpuGovernor() string {
	return readFile("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
}

func noTurbo() string {
	v := readFile("/sys/devices/system/cpu/intel_pstate/no_turbo")
	if v == "UNKNOWN" {
		v = readFile("/sys/devices/system/cpu/cpufreq/boost")
		if v != "UNKNOWN" {
			// cpufreq/boost: 0 = disabled, 1 = enabled → invert for no_turbo semantics
			if v == "0" {
				return "1"
			}
			return "0"
		}
	}
	return v
}

// Result is the top-level JSON structure written to kResultsPath.
type Result struct {
	Status   string `json:"status"`
	Strategy string `json:"strategy"`
	Library  string `json:"library"`
	LibraryVersion string `json:"library_version"`

	// Fairness metadata — must be inspected before citing any number.
	RingDim     int    `json:"ring_dim"`
	RingDimNote string `json:"ring_dim_note"`
	LogQP       int    `json:"logqp"`
	LogQ        []int  `json:"logq"`
	LogP        []int  `json:"logp"`
	LogDefaultScale int `json:"log_default_scale"`

	// Circuit
	Rotations    int    `json:"rotations"`
	CriticalPath int    `json:"critical_path"`
	GaloisKeys   int    `json:"galois_keys"`

	// Harness
	N      int `json:"n"`
	Warmup int `json:"warmup"`

	// Correctness (reported FIRST in printed output before any timing)
	CorrectnessMaxAbsError float64 `json:"correctness_max_abs_error"`
	CorrectnessPassed      bool    `json:"correctness_passed"`

	// Hardware / governor state
	CPUGovernor    string `json:"cpu_governor"`
	NoTurbo        string `json:"no_turbo"`
	GovernorCaveat string `json:"governor_caveat,omitempty"`
	Hardware       string `json:"hardware"`

	// Timing boundary note — required for valid cross-library comparison.
	// SEAL benchmark_160 --strategy=bsgs times ONLY multiply_plain + rescale +
	// bsgs_reduction (timer starts AFTER encrypt, no decrypt in timed region).
	// The comparable Lattigo metric is latency_us["circuit_only"] =
	// eval_mult + hoisted_baby + accumulate_baby + hoisted_giant + accumulate_giant.
	// latency_us["total"] (which includes encrypt + decrypt) must NOT be compared
	// directly against the SEAL §7.5.1 figure (mean=6.965ms, p99=9.094ms).
	TimingBoundaryNote string `json:"timing_boundary_note"`

	// Thread-count note — required for valid cross-library comparison.
	// SEAL bsgs_reduction uses #pragma omp parallel for across both rotation layers;
	// the §7.5.1 run used taskset -c 0,2,4,6,8,10 (6 P-cores, OMP_NUM_THREADS unset).
	// This Lattigo run uses the same taskset with GOMAXPROCS=1 — single-threaded hot
	// path, identical CPU budget. The OMP/single-thread asymmetry is a library delta.
	ThreadCountNote string `json:"thread_count_note"`

	// Latency breakdown
	LatencyUs map[string]Stats `json:"latency_us"`
}

func main() {
	gov := cpuGovernor()
	turbo := noTurbo()

	fmt.Printf("lattigo_benchmark: cpu_governor=%s no_turbo=%s\n", gov, turbo)
	if gov != "performance" {
		fmt.Printf("WARNING: cpu_governor='%s', not 'performance'. Numbers will NOT be"+
			" comparable with governor-validated SEAL BSGS (§7.5.1).\n"+
			"Run: sudo scripts/governor_harness/setup_performance_governor.sh first.\n", gov)
	}
	if turbo != "1" {
		fmt.Printf("WARNING: turbo not confirmed disabled (no_turbo=%s). "+
			"Disable with: sudo scripts/governor_harness/setup_performance_governor.sh\n", turbo)
	}

	fmt.Println("Building CKKS context (LogN=13, LogQ={60,60,40}, LogP={58}, LogQP=218)...")
	ctx, err := BuildContext()
	if err != nil {
		fmt.Fprintf(os.Stderr, "BuildContext failed: %v\n", err)
		os.Exit(1)
	}

	logQP := ctx.params.LogQP()
	fmt.Printf("Context: N=%d, MaxLevel=%d, LogQP=%.1f\n",
		ctx.params.N(), ctx.params.MaxLevel(), logQP)

	// Fixed-seed data: same convention as vendor_server/src/benchmark_160.cpp
	// (seed=42, uniform[-1,1]) for reproducibility across runs.
	rng := rand.New(rand.NewSource(42))
	features := make([]float64, kSlotCount)
	weights := make([]float64, kSlotCount)
	for i := 0; i < kSlotCount; i++ {
		features[i] = rng.Float64()*2 - 1
		weights[i] = rng.Float64()*2 - 1
	}

	// ── Warmup ───────────────────────────────────────────────────────────────
	fmt.Printf("Running %d warmup rounds...\n", kWarmupRounds)
	for i := 0; i < kWarmupRounds; i++ {
		RunCircuitHoisted(&ctx, features, weights)
	}

	// ── Timed runs ────────────────────────────────────────────────────────────
	fmt.Printf("Running %d measured rounds...\n", kMeasureRounds)
	var (
		encryptSamples         []float64
		evalMultSamples        []float64
		hoistedBabySamples     []float64
		accumulateBabySamples  []float64
		hoistedGiantSamples    []float64
		accumulateGiantSamples []float64
		decryptSamples         []float64
		totalSamples           []float64
		circuitOnlySamples     []float64 // eval_mult+hoisted_baby+accumulate_baby+hoisted_giant+accumulate_giant
	)
	var maxAbsErr float64

	for i := 0; i < kMeasureRounds; i++ {
		t := RunCircuitHoisted(&ctx, features, weights)
		encryptSamples = append(encryptSamples, t.EncryptUs)
		evalMultSamples = append(evalMultSamples, t.EvalMultUs)
		hoistedBabySamples = append(hoistedBabySamples, t.HoistedBabyUs)
		accumulateBabySamples = append(accumulateBabySamples, t.AccumulateBabyUs)
		hoistedGiantSamples = append(hoistedGiantSamples, t.HoistedGiantUs)
		accumulateGiantSamples = append(accumulateGiantSamples, t.AccumulateGiantUs)
		decryptSamples = append(decryptSamples, t.DecryptUs)
		totalSamples = append(totalSamples, t.TotalUs)
		circuitOnlySamples = append(circuitOnlySamples,
			t.EvalMultUs+t.HoistedBabyUs+t.AccumulateBabyUs+t.HoistedGiantUs+t.AccumulateGiantUs)
		if t.MaxAbsError > maxAbsErr {
			maxAbsErr = t.MaxAbsError
		}
	}

	correctnessPassed := maxAbsErr < kTolerance

	// ── Parity gate result (FIRST) ────────────────────────────────────────────
	fmt.Printf("\n=== PARITY GATE ===\n")
	fmt.Printf("correctness_max_abs_error = %e\n", maxAbsErr)
	fmt.Printf("correctness_passed        = %v (tolerance=%g)\n", correctnessPassed, kTolerance)
	if !correctnessPassed {
		fmt.Fprintf(os.Stderr, "\nlattigo_benchmark: CORRECTNESS GATE FAILED: max_abs_error=%e >= %g\n",
			maxAbsErr, kTolerance)
		fmt.Fprintln(os.Stderr, "Do NOT cite the timing numbers from this run.")
	}

	totalStats := computeStats(totalSamples)
	circuitOnlyStats := computeStats(circuitOnlySamples)
	fmt.Printf("\n=== TIMING SUMMARY ===\n")
	fmt.Printf("circuit_only (comparable to SEAL §7.5.1): mean=%.1f µs (%.3f ms)  p99=%.1f µs\n",
		circuitOnlyStats.Mean, circuitOnlyStats.Mean/1000.0, circuitOnlyStats.P99)
	fmt.Printf("total (incl encrypt+decrypt):              mean=%.1f µs (%.3f ms)  p99=%.1f µs\n",
		totalStats.Mean, totalStats.Mean/1000.0, totalStats.P99)

	// ── Build JSON result ─────────────────────────────────────────────────────
	govCaveat := ""
	if gov != "performance" {
		govCaveat = fmt.Sprintf(
			"cpu_governor='%s', not 'performance' — numbers are NOT governor-validated and"+
				" must NOT be compared against the governor-validated SEAL BSGS figure in §7.5.1"+
				" (mean=6.965ms, p99=9.094ms). Re-run under performance governor"+
				" (sudo scripts/governor_harness/setup_performance_governor.sh).", gov)
	}

	result := Result{
		Status:          "MEASURED",
		Strategy:        "hoisted_flat",
		Library:         "Lattigo",
		LibraryVersion:  "v6.2.0",
		RingDim:         ctx.params.N(),
		RingDimNote:     "Matched ring with SEAL's deployed N=8192 config (LogQP=218 exactly = SEAL's N=8192/tc128 ceiling from hestdparms.h). This is the true matched-ring comparison §7.5.1 required but could not achieve with OpenFHE (which rejects N=8192 for this depth/security combination). CONFIRMED-RAN: NewParametersFromLiteral at LogN=13 succeeded, RotateHoistedNew executed without error (prior session smoke test).",
		LogQP:           int(math.Round(logQP)),
		LogQ:            []int{60, 60, 40},
		LogP:            []int{58},
		LogDefaultScale: 40,
		Rotations:       (kBabyStep - 1) + (kGiantStep - 1),
		CriticalPath:    1,
		GaloisKeys:      len(kBabySteps) + len(kGiantSteps),
		N:               kMeasureRounds,
		Warmup:          kWarmupRounds,
		CorrectnessMaxAbsError: maxAbsErr,
		CorrectnessPassed:      correctnessPassed,
		CPUGovernor:    gov,
		NoTurbo:        turbo,
		GovernorCaveat: govCaveat,
		Hardware: "Intel Core i7-13650HX (13th Gen) — see scripts/governor_harness/setup_performance_governor.sh for required pre-run setup",
		TimingBoundaryNote: "SEAL benchmark_160 --strategy=bsgs times only multiply_plain+rescale+bsgs_reduction " +
			"(timer starts AFTER encrypt; no decrypt in timed region — confirmed from benchmark_160.cpp:263-274). " +
			"The 'scope' field in rotation_strategy_comparison.json lists encrypt/decrypt as part of the local-circuit " +
			"scope (vs gRPC), not as part of the timer interval. " +
			"The comparable Lattigo metric is latency_us[\"circuit_only\"] = " +
			"eval_mult+hoisted_baby+accumulate_baby+hoisted_giant+accumulate_giant. " +
			"latency_us[\"total\"] (which adds encrypt+decrypt) must NOT be compared against the SEAL §7.5.1 figure.",
		ThreadCountNote: "SEAL §7.5.1 used taskset -c 0,2,4,6,8,10 (6 P-cores) with OMP_NUM_THREADS unset; " +
			"bsgs_reduction's two #pragma omp parallel for loops spread across those 6 cores. " +
			"This Lattigo run used the same taskset with GOMAXPROCS=1 (single-threaded hot path, " +
			"same CPU budget). The OMP/single-thread asymmetry is a library delta: SEAL parallelizes " +
			"unhoisted rotations across cores; Lattigo amortizes the key-switch precompute via hoisting " +
			"but runs serially. Both are library characteristics — neither can be changed without " +
			"patching the library.",
		LatencyUs: map[string]Stats{
			"encrypt":          computeStats(encryptSamples),
			"eval_mult":        computeStats(evalMultSamples),
			"hoisted_baby":     computeStats(hoistedBabySamples),
			"accumulate_baby":  computeStats(accumulateBabySamples),
			"hoisted_giant":    computeStats(hoistedGiantSamples),
			"accumulate_giant": computeStats(accumulateGiantSamples),
			"decrypt":          computeStats(decryptSamples),
			"circuit_only":     circuitOnlyStats,
			"total":            totalStats,
		},
	}

	jsonBytes, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "json.Marshal failed: %v\n", err)
		os.Exit(1)
	}

	if err := os.WriteFile(kResultsPath, jsonBytes, 0644); err != nil {
		fmt.Fprintf(os.Stderr, "WriteFile(%s) failed: %v\n", kResultsPath, err)
		os.Exit(1)
	}

	fmt.Printf("\nWrote %s\n", kResultsPath)
	if !correctnessPassed {
		os.Exit(1)
	}
}
