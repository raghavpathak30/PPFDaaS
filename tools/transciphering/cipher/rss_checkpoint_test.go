package cipher

// This file is the committed fix for the "missing harness" defect recorded
// in docs/MEASUREMENT_PROVENANCE.md: both the 2026-08-04 HERA full-scale
// trace (artifacts/hera_crypt_rss_full_run.jsonl) and the 2026-08-19 Rubato
// full-scale attempt (artifacts/rubato_crypt_rss_full_run.jsonl) were
// produced by a *_test.go file written directly into
// third_party/RtF-Transciphering/ckks_fv/ (package ckks_fv, gitignored by
// design — see third_party/*'s .gitignore rule) and deleted after each run,
// per the convention documented in tools/transciphering/RSS_INSTRUMENTATION.md.
// The code always existed; it just never left a gitignored directory, so
// neither trace could be regenerated from anything `git log` could find.
//
// This version lives in tools/transciphering (committed, tracked) and talks
// to ckks_fv only through its exported API (github.com/ldsec/lattigo/v2/ckks_fv,
// routed to third_party/RtF-Transciphering by go.mod's replace directive) —
// no package-internal patching required for the checkpoint granularity this
// file reproduces (entry through crypt-return). The finer per-round-substep
// checkpoints the original 2026-08-04 HERA investigation added by patching
// fv_hera.go's Crypt() directly (round_N_linlayer / round_N_cube / ...) are
// NOT reproduced here — that still requires editing the vendored package in
// place and remains a separate, legitimately ephemeral technique; see
// RSS_INSTRUMENTATION.md §2c for that trace if it's ever needed again.
//
// NOT run by `go test ./...` — both tests below allocate real CKKS/RtF
// parameter sets and will use single-digit-to-tens-of-GB of RAM depending on
// LogN/LogSlots (see docs/RUBATO_FULLSCALE_PLAN.md for what fits on which
// host). Each is gated behind an explicit env var so `go test ./...` and
// `go vet ./...` stay cheap and safe to run anywhere:
//
//   RSS_CHECKPOINT_RUN=1 RSS_CHECKPOINT_PATH=artifacts/foo.jsonl \
//     GOMEMLIMIT=11GiB GOGC=50 \
//     go test ./cipher/ -run TestRSSCheckpointHeraCrypt -v -timeout 30m
//
// Optional overrides (both default to the historically-used full-scale
// values, i.e. running with no overrides reproduces the 2026-08-04 HERA
// config exactly):
//   RSS_LOGN_OVERRIDE      — e.g. "12" or "14" for an intermediate rung.
//                            Rubato's only param set ("128af") requires
//                            LogSlots = LogN-1 (Params() panics otherwise —
//                            see toyRtFRubatoParams's comment in
//                            third_party), so overriding LogN for Rubato
//                            necessarily shrinks LogSlots too. HERA's "128as"
//                            keeps LogSlots fixed at 4 independent of LogN.
//   RSS_HERA_PARAM_INDEX   — index into ckks_fv.RtFHeraParams (default 3,
//                            "128as", LogSlots=4).
//   RSS_RUBATO_PARAM_INDEX — index into ckks_fv.RtFRubatoParams (default 0,
//                            "128af", the only entry, LogSlots=LogN-1).
//
// Do not set RSS_CHECKPOINT_RUN=1 with LogN=16/full LogSlots on a host with
// less than the headroom docs/RUBATO_FULLSCALE_PLAN.md derives from the
// 13.28 GB Rubato lower bound and the 9.54 GB HERA peak — both were measured
// at LogSlots=4; LogSlots=15 is unmeasured and, per that plan, expected to
// cost substantially more.

import (
	"bufio"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ldsec/lattigo/v2/ckks_fv"
)

// ParamState captures the CKKS/RtF configuration a checkpoint trace ran
// under — the configuration-side counterpart to bench/main.go's
// MachineState (host side). The LogSlots=4-vs-15 mismatch recorded in
// docs/MEASUREMENT_PROVENANCE.md is exactly the class of gap this exists to
// close: two runs whose host state matched but whose parameters silently
// didn't, discovered only by reading source after the fact. Written into
// every checkpoint record from now on so that comparison never has to be
// reconstructed again.
type ParamState struct {
	Cipher     string `json:"cipher"`
	ParamSet   string `json:"param_set"`   // e.g. "128as", "128af"
	ParamIndex int    `json:"param_index"` // index into RtFHeraParams/RtFRubatoParams
	LogN       int    `json:"log_n"`
	LogSlots   int    `json:"log_slots"`
	// ModCount is len(ResidualModuli)+len(DiffScaleModulus) — the exact
	// input halfboot_params.go's Params() feeds into params.qi, which is
	// what GenSlotToCoeffMatFV(radix) loops over (mfv_encoder.go:603). NOT
	// the same as the full CtS/Sine chain length.
	ModCount  int `json:"mod_count"`
	Rounds    int `json:"rounds"`
	BlockSize int `json:"block_size"`

	// The setup choices the upstream benchmark makes per param set, recorded so
	// a trace shows which configuration it ran (an index-2 HERA run once used
	// the sparse 128as setup; see TestRSSSetupMatchesUpstream).
	FullCoeffs        bool `json:"full_coeffs"`         // SetLogFVSlots(LogN) vs SetLogFVSlots(LogSlots)
	NumNonces         int  `json:"num_nonces"`          // N if FullCoeffs else Slots
	ExtraRotationKeys bool `json:"extra_rotation_keys"` // the Slots/2 rotation key, sparse packing only
	OutputBlockSize   int  `json:"output_block_size"`   // keystream outputs per block
	Radix             int  `json:"radix"`
}

// rssSetup is the per-param-set configuration of the upstream benchmarks
// (third_party/RtF-Transciphering/ckks_fv/RtF_bench_test.go: benchmarkRtFHera
// :84, benchmarkRtFRubato :311). It is derived from the param set rather than
// branched on by hand: FullCoeffs is LogSlots == LogN-1 of the UNMODIFIED
// entry, and everything else follows from it exactly as in the benchmarks.
type rssSetup struct {
	FullCoeffs        bool
	Radix             int
	NumNonces         int
	ExtraRotationKeys bool
	OutputBlockSize   int
}

// heraBenchRadix: the radix argument of BenchmarkRtFHera128f/128s/128af/128as
// (RtF_bench_test.go:35-52): 2, 0, 2, 2.
var heraBenchRadix = []int{2, 0, 2, 2}

// heraRSSSetup resolves the setup for RtFHeraParams[index]. hb is the entry
// after any LogN override, used only for the nonce count.
func heraRSSSetup(index int, hb *ckks_fv.HalfBootParameters) rssSetup {
	orig := ckks_fv.RtFHeraParams[index]
	full := orig.LogSlots == orig.LogN-1
	n := 1 << uint(hb.LogSlots)
	if full {
		n = 1 << uint(hb.LogN)
	}
	return rssSetup{FullCoeffs: full, Radix: heraBenchRadix[index], NumNonces: n,
		ExtraRotationKeys: !full, OutputBlockSize: 16}
}

// rubatoRSSSetup: benchmarkRtFRubato always uses full coefficients, radix 2,
// N nonces, no extra rotation key; 60 of the 64 block ciphertexts are outputs
// (fv_rubato.go Crypt: finAddRoundKey(blocksize-4)).
func rubatoRSSSetup(hb *ckks_fv.HalfBootParameters, blocksize int) rssSetup {
	return rssSetup{FullCoeffs: true, Radix: 2, NumNonces: 1 << uint(hb.LogN),
		ExtraRotationKeys: false, OutputBlockSize: blocksize - 4}
}

func (s rssSetup) paramState(ps ParamState) ParamState {
	ps.FullCoeffs, ps.NumNonces, ps.ExtraRotationKeys = s.FullCoeffs, s.NumNonces, s.ExtraRotationKeys
	ps.OutputBlockSize, ps.Radix = s.OutputBlockSize, s.Radix
	return ps
}

// RtF parameter-set names by index, in rtf_params.go order. "f"/"s" = full-
// coefficient (LogSlots = LogN-1) vs sparse packing; a leading "a" = arcsine
// SineEval (ArcSineDeg != 0). TestRSSParamSetNamesMatchIndex derives the name
// from the actual parameters and fails if this table and the index disagree.
var (
	heraParamSetNames   = []string{"128f", "128s", "128af", "128as"}
	rubatoParamSetNames = []string{"128af"}
)

func paramSetName(names []string, kind string, index int) string {
	if index < 0 || index >= len(names) {
		panic(fmt.Sprintf("%s param index %d out of range (have %d sets)", kind, index, len(names)))
	}
	return names[index]
}

// derivedParamSetName reads the name off the parameters themselves, not the index.
func derivedParamSetName(hb *ckks_fv.HalfBootParameters) string {
	name := "128"
	if hb.ArcSineDeg != 0 {
		name += "a"
	}
	if hb.LogSlots == hb.LogN-1 {
		return name + "f"
	}
	return name + "s"
}

func readVmHWMKB() int64 {
	f, err := os.Open("/proc/self/status")
	if err != nil {
		return -1
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "VmHWM:") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				if v, err := strconv.ParseInt(fields[1], 10, 64); err == nil {
					return v
				}
			}
		}
	}
	return -1
}

type checkpointWriter struct {
	path string
	ps   ParamState
}

// checkpoint appends one JSON line, flushed immediately (O_SYNC-equivalent
// via explicit Sync) so a SIGKILL mid-run — exactly what ended the
// 2026-08-19 Rubato attempt — still leaves every prior checkpoint intact.
//
// heap_alloc_kb and total_alloc_kb are runtime.MemStats.HeapAlloc/TotalAlloc
// respectively, correctly labelled and correctly assigned — this was
// checked directly against source (both the original rss_checkpoint.go and
// this file) before writing this comment. heap_alloc_kb has, independently,
// been observed to exceed vm_hwm_kb in both the 2026-08-04 HERA trace
// (RSS_INSTRUMENTATION.md §2b: 14.4 GB heap_alloc vs 9.0 GB VmHWM at
// round_0, "partial swap" HYPOTHESIS, never confirmed) and the 2026-08-19
// Rubato trace (20.26 GB heap_alloc vs 13.28 GB VmHWM at slot_to_coeff_mat).
// Two independent occurrences of the same open, unconfirmed anomaly — not a
// labelling bug, and not something to build a claim on. Any RSS-based
// conclusion should rest on vm_hwm_kb alone.
func (w *checkpointWriter) checkpoint(label string) {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	vmHWM := readVmHWMKB()

	rec := map[string]interface{}{
		"label":          label,
		"timestamp":      time.Now().Format(time.RFC3339Nano),
		"vm_hwm_kb":      vmHWM,
		"heap_alloc_kb":  int64(ms.HeapAlloc / 1024),
		"total_alloc_kb": int64(ms.TotalAlloc / 1024),
		"gomemlimit_env": os.Getenv("GOMEMLIMIT"),
		"gogc_env":       os.Getenv("GOGC"),
		"param_state":    w.ps,
	}
	b, err := json.Marshal(rec)
	if err != nil {
		fmt.Fprintf(os.Stderr, "checkpoint: marshal error: %v\n", err)
		return
	}

	f, err := os.OpenFile(w.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "checkpoint: open error: %v\n", err)
		return
	}
	defer f.Close()
	f.Write(b)
	f.Write([]byte("\n"))
	f.Sync()
}

func newCheckpointWriter(t *testing.T, ps ParamState) *checkpointWriter {
	path := os.Getenv("RSS_CHECKPOINT_PATH")
	if path == "" {
		path = "rss_checkpoints.jsonl"
	}
	t.Logf("rss checkpoint trace -> %s (param_state=%+v)", path, ps)
	return &checkpointWriter{path: path, ps: ps}
}

func envIntOrDefault(name string, def int) int {
	v := os.Getenv(name)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

// modCountOf mirrors halfboot_params.go's Params(): Qi :=
// append(hb.ResidualModuli, hb.DiffScaleModulus...) — the exact chain
// GenSlotToCoeffMatFV(radix) loops over.
func modCountOf(hb *ckks_fv.HalfBootParameters) int {
	return len(hb.ResidualModuli) + len(hb.DiffScaleModulus)
}

// TestRSSCheckpointHeraCrypt reproduces the 2026-08-04 full-scale HERA
// measurement (artifacts/hera_crypt_rss_full_run.jsonl): RtFHeraParams[3]
// ("128as"), numRound=5, radix=2, matching BenchmarkRtFHera128as and this
// module's HeraRounds=5 exactly. See the package doc above for env vars.
func TestRSSCheckpointHeraCrypt(t *testing.T) {
	if os.Getenv("RSS_CHECKPOINT_RUN") != "1" {
		t.Skip("set RSS_CHECKPOINT_RUN=1 to run — allocates real CKKS/RtF parameter sets, see file doc comment")
	}

	const numRound = 5
	paramIndex := envIntOrDefault("RSS_HERA_PARAM_INDEX", 3)

	hb := ckks_fv.RtFHeraParams[paramIndex].Copy()
	fullCoeffs := hb.LogSlots == hb.LogN-1 // decided on the unmodified entry
	if logN := envIntOrDefault("RSS_LOGN_OVERRIDE", 0); logN != 0 {
		hb.LogN = logN
		if fullCoeffs {
			hb.LogSlots = logN - 1 // full-coefficient sets need LogSlots = LogN-1; Params() panics otherwise
		}
	}
	setup := heraRSSSetup(paramIndex, hb)
	radix := setup.Radix

	ps := setup.paramState(ParamState{
		Cipher:     "hera",
		ParamSet:   paramSetName(heraParamSetNames, "hera", paramIndex),
		ParamIndex: paramIndex,
		LogN:       hb.LogN,
		LogSlots:   hb.LogSlots,
		ModCount:   modCountOf(hb),
		Rounds:     numRound,
		BlockSize:  16,
	})
	w := newCheckpointWriter(t, ps)
	w.checkpoint("entry")

	params, err := hb.Params()
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	if setup.FullCoeffs { // benchmarkRtFHera: fullCoeffs ? LogFVSlots=LogN : LogFVSlots=LogSlots
		params.SetLogFVSlots(params.LogN())
	} else {
		params.SetLogFVSlots(params.LogSlots())
	}
	w.checkpoint("params_ready")

	heraModDown := ckks_fv.HeraModDownParams128[paramIndex].CipherModDown

	kgen := ckks_fv.NewKeyGenerator(params)
	sk, pk := kgen.GenKeyPairSparse(hb.H)
	w.checkpoint("keypair_sparse")

	fvEncoder := ckks_fv.NewMFVEncoder(params)
	fvEncryptor := ckks_fv.NewMFVEncryptorFromPk(params, pk)
	_ = sk
	w.checkpoint("encoders_ready")

	rotationsHalfBoot := kgen.GenRotationIndexesForHalfBoot(params.LogSlots(), hb)
	w.checkpoint("rotation_indexes_halfboot")

	pDcds := fvEncoder.GenSlotToCoeffMatFV(radix)
	w.checkpoint("slot_to_coeff_mat")

	rotationsStC := kgen.GenRotationIndexesForSlotsToCoeffsMat(pDcds)
	rotations := append(rotationsHalfBoot, rotationsStC...)
	if setup.ExtraRotationKeys { // benchmarkRtFHera: only if !fullCoeffs
		rotations = append(rotations, params.Slots()/2)
	}
	rotkeys := kgen.GenRotationKeysForRotations(rotations, true, sk)
	w.checkpoint("rotation_keys")

	rlk := kgen.GenRelinearizationKey(sk)
	w.checkpoint("relin_key")

	fvEvaluator := ckks_fv.NewMFVEvaluator(params, ckks_fv.EvaluationKey{Rlk: rlk, Rtks: rotkeys}, pDcds)
	w.checkpoint("fv_evaluator_ready")

	key := make([]uint64, 16)
	for i := 0; i < 16; i++ {
		key[i] = uint64(i + 1)
	}
	if setup.NumNonces != map[bool]int{true: params.N(), false: params.Slots()}[setup.FullCoeffs] {
		t.Fatalf("setup.NumNonces=%d disagrees with params (N=%d, Slots=%d, full=%v)", setup.NumNonces, params.N(), params.Slots(), setup.FullCoeffs)
	}
	nonces := make([][]byte, setup.NumNonces)
	for i := 0; i < setup.NumNonces; i++ {
		nonces[i] = make([]byte, 64)
		if _, err := rand.Read(nonces[i]); err != nil {
			t.Fatalf("rand.Read: %v", err)
		}
	}
	w.checkpoint("keystream_inputs_ready")

	hera := ckks_fv.NewMFVHera(numRound, params, fvEncoder, fvEncryptor, fvEvaluator, heraModDown[0])
	w.checkpoint("mfv_hera_ready")

	kCt := hera.EncKey(key)
	w.checkpoint("enckey_ready")

	fvKeystreams := hera.Crypt(nonces, kCt, heraModDown)
	if fvKeystreams == nil {
		t.Fatalf("hera.Crypt returned nil")
	}
	w.checkpoint("crypt_done")

	w.checkpoint("exit")
	t.Logf("HERA-16 hera.Crypt completed, numRound=%d LogN=%d LogSlots=%d", numRound, params.LogN(), params.LogSlots())
}

// TestRSSCheckpointRubatoCrypt reproduces the 2026-08-19 Rubato full-scale
// attempt (artifacts/rubato_crypt_rss_full_run.jsonl): RtFRubatoParams[0]
// ("128af", the only Rubato RtF param set in this checkout), radix=2. Died
// SIGKILLed mid-slot_to_coeff_mat on a ~16 GB host last time — do not run
// with LogN/LogSlots at their defaults (16/15) on this host; see
// docs/RUBATO_FULLSCALE_PLAN.md.
func TestRSSCheckpointRubatoCrypt(t *testing.T) {
	if os.Getenv("RSS_CHECKPOINT_RUN") != "1" {
		t.Skip("set RSS_CHECKPOINT_RUN=1 to run — allocates real CKKS/RtF parameter sets, see file doc comment")
	}

	const rubatoParam = ckks_fv.RUBATO128L
	paramIndex := envIntOrDefault("RSS_RUBATO_PARAM_INDEX", 0)

	hb := ckks_fv.RtFRubatoParams[paramIndex].Copy()
	if logN := envIntOrDefault("RSS_LOGN_OVERRIDE", 0); logN != 0 {
		hb.LogN = logN
		hb.LogSlots = logN - 1 // "128af" is full-coefficient packing; Params() panics otherwise
	}
	setup := rubatoRSSSetup(hb, ckks_fv.RubatoParams[rubatoParam].Blocksize)
	radix := setup.Radix

	blocksize := ckks_fv.RubatoParams[rubatoParam].Blocksize
	numRound := ckks_fv.RubatoParams[rubatoParam].NumRound
	plainModulus := ckks_fv.RubatoParams[rubatoParam].PlainModulus
	rubatoModDown := ckks_fv.RubatoModDownParams[rubatoParam].CipherModDown

	ps := setup.paramState(ParamState{
		Cipher:     "rubato",
		ParamSet:   paramSetName(rubatoParamSetNames, "rubato", paramIndex),
		ParamIndex: paramIndex,
		LogN:       hb.LogN,
		LogSlots:   hb.LogSlots,
		ModCount:   modCountOf(hb),
		Rounds:     numRound,
		BlockSize:  blocksize,
	})
	w := newCheckpointWriter(t, ps)
	w.checkpoint("entry")

	params, err := hb.Params()
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	params.SetPlainModulus(plainModulus)
	params.SetLogFVSlots(params.LogN())
	w.checkpoint("params_ready")

	kgen := ckks_fv.NewKeyGenerator(params)
	sk, pk := kgen.GenKeyPairSparse(hb.H)
	w.checkpoint("keypair_sparse")

	fvEncoder := ckks_fv.NewMFVEncoder(params)
	fvEncryptor := ckks_fv.NewMFVEncryptorFromPk(params, pk)
	w.checkpoint("encoders_ready")

	rotationsHalfBoot := kgen.GenRotationIndexesForHalfBoot(params.LogSlots(), hb)
	w.checkpoint("rotation_indexes_halfboot")

	pDcds := fvEncoder.GenSlotToCoeffMatFV(radix)
	w.checkpoint("slot_to_coeff_mat")

	rotationsStC := kgen.GenRotationIndexesForSlotsToCoeffsMat(pDcds)
	rotations := append(rotationsHalfBoot, rotationsStC...)
	rotkeys := kgen.GenRotationKeysForRotations(rotations, true, sk)
	w.checkpoint("rotation_keys")

	rlk := kgen.GenRelinearizationKey(sk)
	w.checkpoint("relin_key")

	fvEvaluator := ckks_fv.NewMFVEvaluator(params, ckks_fv.EvaluationKey{Rlk: rlk, Rtks: rotkeys}, pDcds)
	w.checkpoint("fv_evaluator_ready")

	key := make([]uint64, blocksize)
	for i := 0; i < blocksize; i++ {
		key[i] = uint64(i + 1)
	}
	nonces := make([][]byte, setup.NumNonces)
	for i := 0; i < setup.NumNonces; i++ {
		nonces[i] = make([]byte, 8)
		if _, err := rand.Read(nonces[i]); err != nil {
			t.Fatalf("rand.Read (nonce): %v", err)
		}
	}
	counter := make([]byte, 8)
	if _, err := rand.Read(counter); err != nil {
		t.Fatalf("rand.Read (counter): %v", err)
	}
	w.checkpoint("keystream_inputs_ready")

	rubato := ckks_fv.NewMFVRubato(rubatoParam, params, fvEncoder, fvEncryptor, fvEvaluator, rubatoModDown[0])
	w.checkpoint("mfv_rubato_ready")

	kCt := rubato.EncKey(key)
	w.checkpoint("enckey_ready")

	fvKeystreams := rubato.Crypt(nonces, counter, kCt, rubatoModDown)
	if fvKeystreams == nil {
		t.Fatalf("rubato.Crypt returned nil")
	}
	w.checkpoint("crypt_done")

	w.checkpoint("exit")
	t.Logf("Rubato-128L rubato.Crypt completed, blocksize=%d numRound=%d LogN=%d LogSlots=%d", blocksize, numRound, params.LogN(), params.LogSlots())
}

// TestRSSParamSetNamesMatchIndex guards the param_set label written into every
// checkpoint record: for each index, the name in the table must equal the name
// derived from the real RtFHeraParams / RtFRubatoParams entry (arcsine or not,
// full or sparse packing). Regression: HERA index 2 ("128af", LogSlots 15) was
// once recorded as "128as" because the label was a hardcoded string.
func TestRSSParamSetNamesMatchIndex(t *testing.T) {
	if len(heraParamSetNames) != len(ckks_fv.RtFHeraParams) {
		t.Fatalf("heraParamSetNames has %d entries, RtFHeraParams has %d", len(heraParamSetNames), len(ckks_fv.RtFHeraParams))
	}
	if len(rubatoParamSetNames) != len(ckks_fv.RtFRubatoParams) {
		t.Fatalf("rubatoParamSetNames has %d entries, RtFRubatoParams has %d", len(rubatoParamSetNames), len(ckks_fv.RtFRubatoParams))
	}
	for i, hb := range ckks_fv.RtFHeraParams {
		if got, want := paramSetName(heraParamSetNames, "hera", i), derivedParamSetName(hb); got != want {
			t.Errorf("HERA index %d: labelled %q but parameters (ArcSineDeg=%d, LogN=%d, LogSlots=%d) are %q", i, got, hb.ArcSineDeg, hb.LogN, hb.LogSlots, want)
		}
	}
	for i, hb := range ckks_fv.RtFRubatoParams {
		if got, want := paramSetName(rubatoParamSetNames, "rubato", i), derivedParamSetName(hb); got != want {
			t.Errorf("Rubato index %d: labelled %q but parameters (ArcSineDeg=%d, LogN=%d, LogSlots=%d) are %q", i, got, hb.ArcSineDeg, hb.LogN, hb.LogSlots, want)
		}
	}
}

// TestRSSSetupMatchesUpstream fails if a RSS test would run a param set with
// the wrong packing. Expected values are read off the upstream benchmarks
// (RtF_bench_test.go): BenchmarkRtFHera128f -> (idx 0, radix 2, fullCoeffs),
// 128s -> (1, radix 0, sparse), 128af -> (2, radix 2, fullCoeffs), 128as ->
// (3, radix 2, sparse); fullCoeffs => SetLogFVSlots(LogN), N nonces, no
// Slots/2 rotation key; sparse => SetLogFVSlots(LogSlots), Slots nonces, plus
// that rotation key. benchmarkRtFRubato: always full, radix 2, N nonces.
// Regression: HERA index 2 was run with the sparse (128as) settings.
func TestRSSSetupMatchesUpstream(t *testing.T) {
	type want struct {
		full  bool
		radix int
	}
	heraWant := []want{{true, 2}, {false, 0}, {true, 2}, {false, 2}}
	for i, w := range heraWant {
		hb := ckks_fv.RtFHeraParams[i]
		got := heraRSSSetup(i, hb)
		wantNonces := 1 << uint(hb.LogSlots)
		if w.full {
			wantNonces = 1 << uint(hb.LogN)
		}
		if got.FullCoeffs != w.full || got.Radix != w.radix || got.NumNonces != wantNonces ||
			got.ExtraRotationKeys != !w.full || got.OutputBlockSize != 16 {
			t.Errorf("HERA index %d (%s): got %+v, want full=%v radix=%d nonces=%d extraRot=%v outputs=16",
				i, heraParamSetNames[i], got, w.full, w.radix, wantNonces, !w.full)
		}
	}
	// Index 2 explicitly: the case that was wrong.
	if g := heraRSSSetup(2, ckks_fv.RtFHeraParams[2]); !g.FullCoeffs || g.NumNonces != 65536 || g.ExtraRotationKeys {
		t.Errorf("HERA index 2 (128af) must be full-coefficient, 65536 nonces, no extra rotation key; got %+v", g)
	}
	// Under a LogN override the full sets keep LogSlots = LogN-1 and N tracks LogN.
	ov := ckks_fv.RtFHeraParams[2].Copy()
	ov.LogN, ov.LogSlots = 12, 11
	if g := heraRSSSetup(2, ov); !g.FullCoeffs || g.NumNonces != 4096 || g.ExtraRotationKeys {
		t.Errorf("HERA index 2 at LogN=12: got %+v, want full, 4096 nonces, no extra rotation key", g)
	}
	rb := ckks_fv.RtFRubatoParams[0]
	blocksize := ckks_fv.RubatoParams[ckks_fv.RUBATO128L].Blocksize
	if g := rubatoRSSSetup(rb, blocksize); !g.FullCoeffs || g.Radix != 2 || g.NumNonces != 1<<uint(rb.LogN) ||
		g.ExtraRotationKeys || g.OutputBlockSize != 60 {
		t.Errorf("Rubato-128L: got %+v, want full, radix 2, N nonces, no extra rotation key, 60 outputs", g)
	}
}
