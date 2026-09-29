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
	const radix = 2
	paramIndex := envIntOrDefault("RSS_HERA_PARAM_INDEX", 3)

	hb := ckks_fv.RtFHeraParams[paramIndex].Copy()
	if logN := envIntOrDefault("RSS_LOGN_OVERRIDE", 0); logN != 0 {
		hb.LogN = logN
	}

	ps := ParamState{
		Cipher:     "hera",
		ParamSet:   "128as", // this checkout's only HeraModDownParams128-compatible "as" entry at index 3
		ParamIndex: paramIndex,
		LogN:       hb.LogN,
		LogSlots:   hb.LogSlots,
		ModCount:   modCountOf(hb),
		Rounds:     numRound,
		BlockSize:  16,
	}
	w := newCheckpointWriter(t, ps)
	w.checkpoint("entry")

	params, err := hb.Params()
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	params.SetLogFVSlots(params.LogSlots()) // fullCoeffs=false, matches "128as" sparse packing
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
	rotations = append(rotations, params.Slots()/2)
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
	nonces := make([][]byte, params.Slots())
	for i := 0; i < params.Slots(); i++ {
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
	const radix = 2
	paramIndex := envIntOrDefault("RSS_RUBATO_PARAM_INDEX", 0)

	hb := ckks_fv.RtFRubatoParams[paramIndex].Copy()
	if logN := envIntOrDefault("RSS_LOGN_OVERRIDE", 0); logN != 0 {
		hb.LogN = logN
		hb.LogSlots = logN - 1 // "128af" is full-coefficient packing; Params() panics otherwise
	}

	blocksize := ckks_fv.RubatoParams[rubatoParam].Blocksize
	numRound := ckks_fv.RubatoParams[rubatoParam].NumRound
	plainModulus := ckks_fv.RubatoParams[rubatoParam].PlainModulus
	rubatoModDown := ckks_fv.RubatoModDownParams[rubatoParam].CipherModDown

	ps := ParamState{
		Cipher:     "rubato",
		ParamSet:   "128af",
		ParamIndex: paramIndex,
		LogN:       hb.LogN,
		LogSlots:   hb.LogSlots,
		ModCount:   modCountOf(hb),
		Rounds:     numRound,
		BlockSize:  blocksize,
	}
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
	nonces := make([][]byte, params.N())
	for i := 0; i < params.N(); i++ {
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
