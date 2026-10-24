// Command toyrep runs the LogN=10 toy correctness harnesses (the ckks_fv
// test binary built by Dockerfile.bench) N times per cipher and records the
// per-run max |got-want| plus min/median/max.
//
// Why this exists: the paper's single-run values (HERA 1.39e-5, Rubato
// 3.42e-5) vary run to run. Nothing in the harnesses is seeded -- the HE
// secret/public keys (utils.NewPRNG, seeded from crypto/rand), the CKKS
// message data (utils.RandFloat64, crypto/rand), the per-slot nonces and
// Rubato counter (crypto/rand), and Rubato's Gaussian noise
// (plainRubato's utils.NewPRNG) are all fresh randomness on every run. Only
// the symmetric key (key[i] = i+1) is fixed. A spread is therefore expected,
// and one run's value is a single draw from it.
//
// Config (env): TOY_REPEAT_N (default 30), TOY_CIPHERS (default
// "hera,rubato"), TOY_TEST_BIN (default /opt/bench/ckks_fv.test),
// RESULTS_DIR (default /results). Output: $RESULTS_DIR/toy_error_repetition.json.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var cipherTests = map[string]string{
	"hera":   "TestRtFHera128asToyCorrectness",
	"rubato": "TestRtFRubato128LToyCorrectness",
}

var errRe = regexp.MustCompile(`max\|got-want\|=([0-9.eE+-]+)`)

type cipherResult struct {
	Test     string    `json:"test"`
	N        int       `json:"n_requested"`
	Errors   []float64 `json:"max_abs_error_per_run"`
	Failures []string  `json:"failures,omitempty"`
	Min      float64   `json:"min"`
	Median   float64   `json:"median"`
	Max      float64   `json:"max"`
}

type output struct {
	Date      string                  `json:"date_utc"`
	Binary    string                  `json:"test_binary"`
	LogN      int                     `json:"log_n"`
	Tolerance float64                 `json:"tolerance_in_test"`
	Seeding   string                  `json:"seeding_note"`
	Results   map[string]cipherResult `json:"results"`
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func median(sorted []float64) float64 {
	n := len(sorted)
	if n == 0 {
		return 0
	}
	if n%2 == 1 {
		return sorted[n/2]
	}
	return (sorted[n/2-1] + sorted[n/2]) / 2
}

func main() {
	n, err := strconv.Atoi(envOr("TOY_REPEAT_N", "30"))
	if err != nil || n < 1 {
		fmt.Fprintf(os.Stderr, "toyrep: bad TOY_REPEAT_N\n")
		os.Exit(2)
	}
	bin := envOr("TOY_TEST_BIN", "/opt/bench/ckks_fv.test")
	dir := envOr("RESULTS_DIR", "/results")
	out := output{
		Date:      time.Now().UTC().Format(time.RFC3339),
		Binary:    bin,
		LogN:      10,
		Tolerance: 5e-2,
		Seeding: "Unseeded: HE keys (KeyedPRNG from crypto/rand), CKKS message data " +
			"(utils.RandFloat64 via crypto/rand), per-slot nonces and Rubato counter " +
			"(crypto/rand), Rubato Gaussian noise (utils.NewPRNG) are fresh each run. " +
			"Only the symmetric key (key[i]=i+1) is fixed.",
		Results: map[string]cipherResult{},
	}

	failed := false
	for _, c := range strings.Split(envOr("TOY_CIPHERS", "hera,rubato"), ",") {
		c = strings.TrimSpace(c)
		test, ok := cipherTests[c]
		if !ok {
			fmt.Fprintf(os.Stderr, "toyrep: unknown cipher %q\n", c)
			os.Exit(2)
		}
		res := cipherResult{Test: test, N: n}
		for i := 0; i < n; i++ {
			cmd := exec.Command(bin, "-test.run", "^"+test+"$", "-test.count=1", "-test.v")
			var buf bytes.Buffer
			cmd.Stdout, cmd.Stderr = &buf, &buf
			runErr := cmd.Run()
			m := errRe.FindSubmatch(buf.Bytes())
			if runErr != nil || m == nil {
				res.Failures = append(res.Failures, fmt.Sprintf("run %d: err=%v parsed=%v", i, runErr, m != nil))
				failed = true
				fmt.Fprintf(os.Stderr, "%s run %d FAILED\n%s\n", c, i, buf.String())
				continue
			}
			v, _ := strconv.ParseFloat(string(m[1]), 64)
			res.Errors = append(res.Errors, v)
			fmt.Printf("%s run %d/%d: max|got-want|=%e\n", c, i+1, n, v)
		}
		if len(res.Errors) > 0 {
			s := append([]float64(nil), res.Errors...)
			sort.Float64s(s)
			res.Min, res.Median, res.Max = s[0], median(s), s[len(s)-1]
			fmt.Printf("%s: n=%d min=%e median=%e max=%e\n", c, len(s), res.Min, res.Median, res.Max)
		}
		out.Results[c] = res
	}

	if err := os.MkdirAll(dir, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "toyrep: %v\n", err)
		os.Exit(1)
	}
	b, _ := json.MarshalIndent(out, "", "  ")
	path := filepath.Join(dir, "toy_error_repetition.json")
	if err := os.WriteFile(path, b, 0644); err != nil {
		fmt.Fprintf(os.Stderr, "toyrep: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("wrote %s\n", path)
	if failed {
		os.Exit(1)
	}
}
