// bench measures the client-side plaintext path (client CPU + upload-size)
// for either symmetric cipher backend (HERA-16 or Rubato-128L).
//
// This covers the "online_encrypt_ms" and "upload_bytes_symmetric" columns
// in the Phase 7 break-even map (scripts/hhe_breakeven.py).
//
// PENDING columns (require vendor_server integration, currently stub-only):
//   - online_transcipher_time_ms  (BFV evaluation of the cipher inside the vendor)
//   - repacking_time_ms           (StC + CKKS modular reduction)
//   - full RtF end-to-end latency
//
// Usage:
//
//	go run ./bench [--cipher hera|rubato] [--features N] [--lanes L] [--rounds R]
//	Defaults: cipher=hera, features=256, lanes=16, rounds=100
//
// Output: JSON to stdout + ./results/<cipher>_bench.json
package main

import (
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"math/big"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/raghavpathak30/ppfdaas/transciphering/cipher"
)

// MachineState captures the host conditions the "absolute latencies are not
// reproducible across sessions" house rule (PROJECT_STATE.md) says matter:
// governor, power source, load average, and CPU clock. Best-effort — Linux
// sysfs only; fields are empty/zero if a file isn't readable (e.g. no
// battery, no root), never fabricated.
type MachineState struct {
	CPUGovernor      string  `json:"cpu_governor"`
	PowerSource      string  `json:"power_source"` // "AC", "battery", or "unknown"
	LoadAvg1         float64 `json:"load_avg_1"`
	LoadAvg5         float64 `json:"load_avg_5"`
	LoadAvg15        float64 `json:"load_avg_15"`
	CPUCount         int     `json:"cpu_count"`
	ScalingCurMHzMin float64 `json:"scaling_cur_mhz_min"`
	ScalingCurMHzMax float64 `json:"scaling_cur_mhz_max"`
}

func readMachineState() MachineState {
	ms := MachineState{PowerSource: "unknown"}

	if b, err := os.ReadFile("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"); err == nil {
		ms.CPUGovernor = strings.TrimSpace(string(b))
	}

	if b, err := os.ReadFile("/proc/loadavg"); err == nil {
		fields := strings.Fields(string(b))
		if len(fields) >= 3 {
			ms.LoadAvg1, _ = strconv.ParseFloat(fields[0], 64)
			ms.LoadAvg5, _ = strconv.ParseFloat(fields[1], 64)
			ms.LoadAvg15, _ = strconv.ParseFloat(fields[2], 64)
		}
	}

	// AC/battery: ADP0 (or any AC-like power_supply entry with an "online"
	// file) wins if present and online=1; otherwise fall back to whether any
	// battery reports "Discharging".
	if entries, err := os.ReadDir("/sys/class/power_supply"); err == nil {
		acOnline := false
		acFound := false
		batteryDischarging := false
		for _, e := range entries {
			base := "/sys/class/power_supply/" + e.Name()
			if b, err := os.ReadFile(base + "/online"); err == nil {
				acFound = true
				if strings.TrimSpace(string(b)) == "1" {
					acOnline = true
				}
			}
			if b, err := os.ReadFile(base + "/status"); err == nil {
				if strings.TrimSpace(string(b)) == "Discharging" {
					batteryDischarging = true
				}
			}
		}
		switch {
		case acFound && acOnline:
			ms.PowerSource = "AC"
		case batteryDischarging:
			ms.PowerSource = "battery"
		case acFound:
			ms.PowerSource = "AC" // no battery discharging and an AC node exists but reads 0 -- treat cautiously, still best-effort
		}
	}

	ms.CPUCount = 0
	minMHz, maxMHz := math.Inf(1), math.Inf(-1)
	for i := 0; ; i++ {
		path := fmt.Sprintf("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq", i)
		b, err := os.ReadFile(path)
		if err != nil {
			break
		}
		ms.CPUCount++
		khz, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
		if err != nil {
			continue
		}
		mhz := khz / 1000.0
		if mhz < minMHz {
			minMHz = mhz
		}
		if mhz > maxMHz {
			maxMHz = mhz
		}
	}
	if ms.CPUCount > 0 {
		ms.ScalingCurMHzMin = minMHz
		ms.ScalingCurMHzMax = maxMHz
	}
	return ms
}

type BenchResult struct {
	Cipher       string       `json:"cipher"`
	Status       string       `json:"status"`
	NFeatures    int          `json:"n_features"`
	NLanes       int          `json:"n_lanes"`
	NRounds      int          `json:"n_rounds"`
	Methodology  string       `json:"methodology"`
	MachineState MachineState `json:"machine_state"`
	TimestampUTC string       `json:"timestamp_utc"`

	// Online client-side measurements (plaintext path, MEASURED).
	OnlineEncryptMeanMs   float64 `json:"online_encrypt_mean_ms"`
	OnlineEncryptMedianMs float64 `json:"online_encrypt_median_ms"`
	OnlineEncryptP99Ms    float64 `json:"online_encrypt_p99_ms"`
	OnlineEncryptStdMs    float64 `json:"online_encrypt_std_ms"`

	// Upload size (per batch of n_lanes transactions).
	UploadSymmetricBytes int     `json:"upload_symmetric_bytes"`
	UploadCKKSBytes      int     `json:"upload_ckks_standard_bytes"`
	UploadRatioVsCKKS    float64 `json:"upload_ratio_vs_ckks_standard"`

	// Key expansion (amortized offline cost, plaintext path).
	KeyExpansionMeanMs float64 `json:"key_expansion_mean_ms"`

	// PENDING: server-side HE evaluation.
	OnlineTranscipherTimeMs string `json:"online_transcipher_time_ms"`
	RepackingTimeMs         string `json:"repacking_time_ms"`
	PendingReason           string `json:"pending_reason"`
}

func main() {
	cipherName := flag.String("cipher", "hera", "symmetric cipher backend: hera or rubato")
	features := flag.Int("features", 256, "features per transaction")
	lanes := flag.Int("lanes", 16, "transactions per batch (batch occupancy)")
	rounds := flag.Int("rounds", 100, "benchmark rounds")
	flag.Parse()

	h, err := selectCipher(*cipherName)
	if err != nil {
		log.Fatalf("%v", err)
	}
	n := (*features) * (*lanes) // total uint64 elements per batch

	// Generate a random key and nonce.
	key := randUint64Slice(h.KeySize(), h.Modulus())
	nonce := randUint64Slice(h.NonceSize(), h.Modulus())

	// Synthetic feature data (uniform random in [0, t)).
	plaintext := randUint64Slice(n, h.Modulus())

	// --- Key expansion benchmark ---
	warmup := 10
	keTimes := make([]float64, *rounds)
	for i := 0; i < warmup+*rounds; i++ {
		t0 := time.Now()
		_, err := h.EvalKeyExpansion(key, nonce, nil)
		elapsed := float64(time.Since(t0).Microseconds()) / 1000.0
		if err != nil {
			log.Fatalf("EvalKeyExpansion: %v", err)
		}
		if i >= warmup {
			keTimes[i-warmup] = elapsed
		}
	}

	// Machine state captured right before the timed online-encrypt loop
	// (the metric it's meant to explain), not at process start.
	ms := readMachineState()
	timestamp := time.Now().UTC().Format(time.RFC3339)

	// --- Online encrypt benchmark ---
	encTimes := make([]float64, *rounds)
	var lastCT []uint64
	for i := 0; i < warmup+*rounds; i++ {
		t0 := time.Now()
		ct, err := h.Encrypt(key, nonce, plaintext)
		elapsed := float64(time.Since(t0).Microseconds()) / 1000.0
		if err != nil {
			log.Fatalf("Encrypt: %v", err)
		}
		lastCT = ct
		if i >= warmup {
			encTimes[i-warmup] = elapsed
		}
	}
	_ = lastCT

	// Wire-size calculation.
	uploadSym := h.OnlineCiphertextBytes(n)
	uploadCKKS := 262257 // 160-bit standard from artifacts/wire_sizes.json

	res := BenchResult{
		Cipher:       h.Name(),
		Status:       "MEASURED",
		NFeatures:    *features,
		NLanes:       *lanes,
		NRounds:      *rounds,
		Methodology:  "plaintext path: " + h.Name() + " stream cipher, client CPU only, no HE evaluation",
		MachineState: ms,
		TimestampUTC: timestamp,

		OnlineEncryptMeanMs:   mean(encTimes),
		OnlineEncryptMedianMs: percentile(encTimes, 50),
		OnlineEncryptP99Ms:    percentile(encTimes, 99),
		OnlineEncryptStdMs:    stddev(encTimes),

		UploadSymmetricBytes: uploadSym,
		UploadCKKSBytes:      uploadCKKS,
		UploadRatioVsCKKS:    float64(uploadCKKS) / float64(uploadSym),

		KeyExpansionMeanMs: mean(keTimes),

		OnlineTranscipherTimeMs: "PENDING",
		RepackingTimeMs:         "PENDING",
		PendingReason: "PENDING: vendor_server's BFV evaluation of " + h.Name() +
			" is stub-only. The online transcipher time (BFV evaluation of " +
			h.Name() + " inside vendor_server) and repacking time (StC + " +
			"CKKS modular reduction) cannot be measured until vendor_server " +
			"integration lands. All plaintext-path columns above are MEASURED.",
	}

	out, _ := json.MarshalIndent(res, "", "  ")
	fmt.Println(string(out))

	if err := os.MkdirAll("results", 0755); err != nil {
		log.Fatalf("mkdir results: %v", err)
	}
	path := filepath.Join("results", *cipherName+"_bench.json")
	if err := os.WriteFile(path, out, 0644); err != nil {
		log.Fatalf("write %s: %v", path, err)
	}
	fmt.Fprintf(os.Stderr, "results written to %s\n", path)
}

// selectCipher maps a --cipher flag value to a CipherBackend.
func selectCipher(name string) (cipher.CipherBackend, error) {
	switch name {
	case "hera":
		return cipher.NewHERA16(), nil
	case "rubato":
		return cipher.NewRubato128L(), nil
	default:
		return nil, fmt.Errorf("unknown --cipher %q: want hera or rubato", name)
	}
}

// --- helpers ---

func randUint64Slice(n int, mod uint64) []uint64 {
	s := make([]uint64, n)
	modBig := new(big.Int).SetUint64(mod)
	for i := range s {
		v, _ := rand.Int(rand.Reader, modBig)
		s[i] = v.Uint64()
	}
	return s
}

func mean(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	var s float64
	for _, x := range xs {
		s += x
	}
	return s / float64(len(xs))
}

func stddev(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	m := mean(xs)
	var s float64
	for _, x := range xs {
		d := x - m
		s += d * d
	}
	return math.Sqrt(s / float64(len(xs)))
}

func percentile(xs []float64, p float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	sorted := make([]float64, len(xs))
	copy(sorted, xs)
	sort.Float64s(sorted)
	idx := int(math.Ceil(p/100.0*float64(len(sorted)))) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}
