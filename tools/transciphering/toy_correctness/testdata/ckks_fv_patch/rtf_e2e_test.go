package ckks_fv

// End-to-end server-side online path for ONE batch, per stage, repeated:
//
//	keystream_crypt        homomorphic HERA/Rubato evaluation (hera.Crypt / rubato.Crypt)
//	stc_modswitch          SlotsToCoeffs + ModSwitchMany on keystream ciphertext 0
//	sub_ntt_setscale       ciphertext = plaintext - keystream; TransformToNTT; SetScale
//	halfboot               HalfBoot (ModRaise, SubSum, CtS, EvalMod) -- the FV->CKKS step
//	ckks_eval_2x_plus_1    the 2x+1 CKKS circuit (MultByConst, Rescale, AddConst)
//
// Setup (keygen, rotation keys, HalfBootstrapper, StC matrices, client-side
// plaintext+keystream, EncKey) is timed separately and never mixed into the
// online stages. Mirrors benchmarkRtFHera / benchmarkRtFRubato
// (RtF_bench_test.go:84, :311) and the toy harnesses' 2x+1 circuit; unlike
// the benchmarks it repeats each stage and writes JSON.
//
// Gated: skipped unless E2E_RUN=1 (a full-scale run needs tens of GB).
// Env: E2E_CIPHER=hera|rubato (default hera), E2E_HERA_PARAM_INDEX (2=128af
// default, 3=128as), E2E_LOGN (default 16; for the full-coefficient sets
// LogSlots = LogN-1), E2E_REPS (default 5), RESULTS_DIR (default /results),
// E2E_OUT (default $RESULTS_DIR/e2e_<cipher>.json).
//
// Staged into ckks_fv/ by third_party/fetch_rtf.sh; uses readVmHWMKB from the
// RSS patch (third_party/patches/0001-rss-checkpoints.patch).

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"testing"
	"time"

	"github.com/ldsec/lattigo/v2/utils"
)

type e2eParamState struct {
	Cipher     string `json:"cipher"`
	ParamSet   string `json:"param_set"`
	ParamIndex int    `json:"param_index"`
	LogN       int    `json:"log_n"`
	LogSlots   int    `json:"log_slots"`
	ModCount   int    `json:"mod_count"`
	Rounds     int    `json:"rounds"`
	BlockSize  int    `json:"block_size"`
	Radix      int    `json:"radix"`
	FullCoeffs bool   `json:"full_coeffs"`
}

type e2eStage struct {
	RunsMs []float64 `json:"runs_ms"`
	Median float64   `json:"median_ms"`
	Min    float64   `json:"min_ms"`
	Max    float64   `json:"max_ms"`
	// VmHWM after the last repetition of this stage (peak so far, not per stage).
	VmHWMKBAfter int64 `json:"vm_hwm_kb_after"`
}

type e2eOut struct {
	DateUTC    string               `json:"date_utc"`
	ParamState e2eParamState        `json:"param_state"`
	Slots      int                  `json:"slots"`
	NCoeffs    int                  `json:"n_coeffs_per_input_ct"`
	OutputCts  int                  `json:"halfboot_output_cts"`
	Reps       int                  `json:"reps"`
	SetupMs    map[string]float64   `json:"setup_ms"`
	Online     map[string]*e2eStage `json:"online_stages"`

	// Sums of per-stage medians. "per_batch" = all five stages; "client_dependent"
	// = the three stages that need the client's ciphertext (keystream_crypt and
	// stc_modswitch depend only on the nonces, so an RtF deployment can run them
	// ahead of the upload).
	PerBatchTotalMedianMs     float64 `json:"per_batch_total_median_ms"`
	ClientDependentMedianMs   float64 `json:"client_dependent_median_ms"`
	PerTransactionOnlineMs    float64 `json:"per_transaction_online_ms"`
	PerTransactionClientDepMs float64 `json:"per_transaction_client_dependent_ms"`
	PerTransactionNote        string  `json:"per_transaction_note"`

	MaxAbsError  []float64 `json:"max_abs_error_per_output_ct"`
	VmHWMKB      int64     `json:"vm_hwm_kb_end"`
	TotalAllocKB int64     `json:"total_alloc_kb_end"`
	HeapAllocKB  int64     `json:"heap_alloc_kb_end"`
	GOGC         string    `json:"gogc_env"`
	GOMEMLIMIT   string    `json:"gomemlimit_env"`
	Seeding      string    `json:"seeding_note"`
}

func e2eEnvInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func e2eMs(d time.Duration) float64 { return float64(d.Microseconds()) / 1000.0 }

func e2eSummarise(runs []float64) *e2eStage {
	s := append([]float64(nil), runs...)
	sort.Float64s(s)
	n := len(s)
	med := s[n/2]
	if n%2 == 0 {
		med = (s[n/2-1] + s[n/2]) / 2
	}
	return &e2eStage{RunsMs: runs, Median: med, Min: s[0], Max: s[n-1], VmHWMKBAfter: readVmHWMKB()}
}

func TestRtFE2E(t *testing.T) {
	if os.Getenv("E2E_RUN") != "1" {
		t.Skip("set E2E_RUN=1 to run (full scale allocates tens of GB)")
	}
	cipherName := os.Getenv("E2E_CIPHER")
	if cipherName == "" {
		cipherName = "hera"
	}
	reps := e2eEnvInt("E2E_REPS", 5)
	logN := e2eEnvInt("E2E_LOGN", 16)
	resultsDir := os.Getenv("RESULTS_DIR")
	if resultsDir == "" {
		resultsDir = "/results"
	}
	outPath := os.Getenv("E2E_OUT")
	if outPath == "" {
		outPath = filepath.Join(resultsDir, "e2e_"+cipherName+".json")
	}

	// ---- configuration (mirrors the benchmarks' per-cipher setup) --------------
	var (
		hbtpParams *HalfBootParameters
		paramSet   string
		paramIndex int
		numRound   int
		blocksize  int
		outputsize int
		radix      int
		fullCoeffs bool
		modDown    []int
		stcModDown []int
		rubatoPar  int
	)
	heraNames := []string{"128f", "128s", "128af", "128as"}
	switch cipherName {
	case "hera":
		paramIndex = e2eEnvInt("E2E_HERA_PARAM_INDEX", 2)
		if paramIndex != 2 && paramIndex != 3 {
			t.Fatalf("E2E_HERA_PARAM_INDEX must be 2 (128af) or 3 (128as), got %d", paramIndex)
		}
		paramSet = heraNames[paramIndex]
		hbtpParams = RtFHeraParams[paramIndex].Copy()
		numRound, blocksize, outputsize, radix = 5, 16, 16, 2
		fullCoeffs = paramIndex == 2
		modDown = HeraModDownParams128[paramIndex].CipherModDown
		stcModDown = HeraModDownParams128[paramIndex].StCModDown
	case "rubato":
		paramIndex, paramSet = 0, "128af"
		rubatoPar = RUBATO128L
		hbtpParams = RtFRubatoParams[0].Copy()
		blocksize = RubatoParams[rubatoPar].Blocksize
		outputsize = blocksize - 4
		numRound = RubatoParams[rubatoPar].NumRound
		radix, fullCoeffs = 2, true
		modDown = RubatoModDownParams[rubatoPar].CipherModDown
		stcModDown = RubatoModDownParams[rubatoPar].StCModDown
	default:
		t.Fatalf("E2E_CIPHER must be hera or rubato, got %q", cipherName)
	}
	if logN != hbtpParams.LogN {
		hbtpParams.LogN = logN
		if fullCoeffs {
			hbtpParams.LogSlots = logN - 1 // Params() panics unless LogSlots <= LogN-1
		}
	}

	out := &e2eOut{
		DateUTC: time.Now().UTC().Format(time.RFC3339), Reps: reps,
		SetupMs: map[string]float64{}, Online: map[string]*e2eStage{},
		GOGC: os.Getenv("GOGC"), GOMEMLIMIT: os.Getenv("GOMEMLIMIT"),
		Seeding: "Unseeded: HE keys, message data, nonces, Rubato counter and noise come from crypto/rand each run.",
	}
	setup := func(name string, f func()) {
		t0 := time.Now()
		f()
		out.SetupMs[name] = e2eMs(time.Since(t0))
	}

	// ---- setup (timed, kept apart from the online stages) -----------------------
	params, err := hbtpParams.Params()
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	if cipherName == "rubato" {
		params.SetPlainModulus(RubatoParams[rubatoPar].PlainModulus)
	}
	if fullCoeffs {
		params.SetLogFVSlots(params.LogN())
	} else {
		params.SetLogFVSlots(params.LogSlots())
	}
	messageScaling := float64(params.PlainModulus()) / hbtpParams.MessageRatio

	var (
		kgen          KeyGenerator
		sk            *SecretKey
		pk            *PublicKey
		fvEncoder     MFVEncoder
		ckksEncoder   CKKSEncoder
		fvEncryptor   MFVEncryptor
		ckksDecryptor CKKSDecryptor
		fvEvaluator   MFVEvaluator
		hbtp          *HalfBootstrapper
		rlk           *RelinearizationKey
		rotkeys       *RotationKeySet
	)
	setup("keygen", func() {
		kgen = NewKeyGenerator(params)
		sk, pk = kgen.GenKeyPairSparse(hbtpParams.H)
		fvEncoder = NewMFVEncoder(params)
		ckksEncoder = NewCKKSEncoder(params)
		fvEncryptor = NewMFVEncryptorFromPk(params, pk)
		ckksDecryptor = NewCKKSDecryptor(params, sk)
	})
	var pDcds [][]*PtDiagMatrixT
	setup("rotation_keys_halfboot_stc_matrices", func() {
		rotationsHalfBoot := kgen.GenRotationIndexesForHalfBoot(params.LogSlots(), hbtpParams)
		pDcds = fvEncoder.GenSlotToCoeffMatFV(radix)
		rotationsStC := kgen.GenRotationIndexesForSlotsToCoeffsMat(pDcds)
		rotations := append(rotationsHalfBoot, rotationsStC...)
		if !fullCoeffs {
			rotations = append(rotations, params.Slots()/2)
		}
		rotkeys = kgen.GenRotationKeysForRotations(rotations, true, sk)
		rlk = kgen.GenRelinearizationKey(sk)
	})
	setup("halfbootstrapper_and_fv_evaluator", func() {
		var err error
		hbtp, err = NewHalfBootstrapper(params, hbtpParams, BootstrappingKey{Rlk: rlk, Rtks: rotkeys})
		if err != nil {
			t.Fatalf("NewHalfBootstrapper: %v", err)
		}
		fvEvaluator = NewMFVEvaluator(params, EvaluationKey{Rlk: rlk, Rtks: rotkeys}, pDcds)
	})

	nBlocks := params.Slots() // one nonce per sparse slot
	if fullCoeffs {
		nBlocks = params.N() // one nonce per coefficient
	}
	key := make([]uint64, blocksize)
	for i := range key {
		key[i] = uint64(i + 1)
	}
	var (
		nonces    [][]byte
		counter   []byte
		data0     []float64
		plaintext *Plaintext
	)
	// Client side, output element 0 only (as in the benchmarks): data -> keystream -> data+keystream.
	setup("client_plaintext_and_keystream_ct0", func() {
		nonceLen := 64
		if cipherName == "rubato" {
			nonceLen = 8
			counter = make([]byte, 8)
			if _, err := rand.Read(counter); err != nil {
				t.Fatalf("rand: %v", err)
			}
		}
		nonces = make([][]byte, nBlocks)
		for i := range nonces {
			nonces[i] = make([]byte, nonceLen)
			if _, err := rand.Read(nonces[i]); err != nil {
				t.Fatalf("rand: %v", err)
			}
		}
		data0 = make([]float64, nBlocks)
		for i := range data0 {
			data0[i] = utils.RandFloat64(-1, 1)
		}
		coeffs := make([]float64, params.N())
		for i := 0; i < nBlocks/2; i++ {
			j := utils.BitReverse64(uint64(i), uint64(params.LogN()-1))
			coeffs[j] = data0[i]
			coeffs[j+uint64(params.N()/2)] = data0[i+nBlocks/2]
		}
		plainRingT := ckksEncoder.EncodeCoeffsRingTNew(coeffs, messageScaling)
		poly := plainRingT.Value()[0]
		for i := 0; i < nBlocks; i++ {
			var ks []uint64
			if cipherName == "hera" {
				ks = plainHera(numRound, nonces[i], key, params.PlainModulus())
			} else {
				ks = plainRubato(blocksize, numRound, nonces[i], counter, key, params.PlainModulus(), RubatoParams[rubatoPar].Sigma)
			}
			j := utils.BitReverse64(uint64(i), uint64(params.LogN()))
			poly.Coeffs[0][j] = (poly.Coeffs[0][j] + ks[0]) % params.PlainModulus()
		}
		plaintext = NewPlaintextFVLvl(params, 0)
		fvEncoder.FVScaleUp(plainRingT, plaintext)
	})

	var (
		crypt func() []*Ciphertext
		reset func() // Crypt consumes the cipher's state; Reset re-initialises it between repetitions
	)
	setup("cipher_init_and_enckey", func() {
		if cipherName == "hera" {
			hera := NewMFVHera(numRound, params, fvEncoder, fvEncryptor, fvEvaluator, modDown[0])
			kCt := hera.EncKey(key)
			crypt = func() []*Ciphertext { return hera.Crypt(nonces, kCt, modDown) }
			reset = func() { hera.Reset(modDown[0]) }
		} else {
			rubato := NewMFVRubato(rubatoPar, params, fvEncoder, fvEncryptor, fvEvaluator, modDown[0])
			kCt := rubato.EncKey(key)
			crypt = func() []*Ciphertext { return rubato.Crypt(nonces, counter, kCt, modDown) }
			reset = func() { rubato.Reset(modDown[0]) }
		}
	})

	// ---- online stages, each repeated reps times --------------------------------
	timeStage := func(name string, f func()) {
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			t0 := time.Now()
			f()
			runs = append(runs, e2eMs(time.Since(t0)))
		}
		out.Online[name] = e2eSummarise(runs)
	}

	var rawKeystream, ks0 *Ciphertext
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			if r > 0 {
				reset() // untimed; the first Crypt uses the state built in cipher_init_and_enckey
			}
			t0 := time.Now()
			all := crypt()
			runs = append(runs, e2eMs(time.Since(t0)))
			rawKeystream = all[0] // keep one; the rest are garbage once the slice dies
		}
		out.Online["keystream_crypt"] = e2eSummarise(runs)
	}
	// SlotsToCoeffs may reuse its input, so every repetition gets a fresh copy (copy untimed).
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			in := rawKeystream.CopyNew().Ciphertext()
			t0 := time.Now()
			ks0 = fvEncoder2StC(fvEvaluator, in, stcModDown)
			runs = append(runs, e2eMs(time.Since(t0)))
		}
		out.Online["stc_modswitch"] = e2eSummarise(runs)
	}
	var prepared *Ciphertext
	timeStage("sub_ntt_setscale", func() {
		ct := NewCiphertextFVLvl(params, 1, 0)
		ct.Value()[0] = plaintext.Value()[0].CopyNew()
		fvEvaluator.Sub(ct, ks0, ct)
		fvEvaluator.TransformToNTT(ct, ct)
		ct.SetScale(math.Exp2(math.Round(math.Log2(float64(params.Qi()[0]) / float64(params.PlainModulus()) * messageScaling))))
		prepared = ct
	})
	var boot0, boot1 *Ciphertext
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			in := prepared.CopyNew().Ciphertext() // HalfBoot drops levels of its input in place
			t0 := time.Now()
			boot0, boot1 = hbtp.HalfBoot(in, !fullCoeffs)
			runs = append(runs, e2eMs(time.Since(t0)))
		}
		out.Online["halfboot"] = e2eSummarise(runs)
	}
	ckksEvaluator := NewCKKSEvaluator(params, EvaluationKey{Rlk: rlk, Rtks: rotkeys})
	var results []*Ciphertext
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			ins := []*Ciphertext{boot0}
			if boot1 != nil {
				ins = append(ins, boot1)
			}
			t0 := time.Now()
			results = results[:0]
			for _, in := range ins {
				res := ckksEvaluator.MultByConstNew(in, 2.0)
				if err := ckksEvaluator.Rescale(res, params.Scale(), res); err != nil {
					t.Fatalf("Rescale: %v", err)
				}
				ckksEvaluator.AddConst(res, 1.0, res)
				results = append(results, res)
			}
			runs = append(runs, e2eMs(time.Since(t0)))
		}
		out.Online["ckks_eval_2x_plus_1"] = e2eSummarise(runs)
	}

	// ---- correctness check (untimed) -------------------------------------------
	const tolerance = 5e-2
	for k, res := range results {
		got := ckksEncoder.DecodeComplex(ckksDecryptor.DecryptNew(res), params.LogSlots())
		maxErr := 0.0
		for i := 0; i < params.Slots(); i++ {
			want := 2*data0[k*params.Slots()+i] + 1
			if e := math.Abs(real(got[i]) - want); e > maxErr {
				maxErr = e
			}
		}
		out.MaxAbsError = append(out.MaxAbsError, maxErr)
		if maxErr > tolerance {
			t.Errorf("output ct %d: max abs error %e exceeds %e", k, maxErr, tolerance)
		}
	}

	// ---- assemble + write -------------------------------------------------------
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	out.ParamState = e2eParamState{Cipher: cipherName, ParamSet: paramSet, ParamIndex: paramIndex,
		LogN: params.LogN(), LogSlots: params.LogSlots(), ModCount: len(hbtpParams.ResidualModuli) + len(hbtpParams.DiffScaleModulus),
		Rounds: numRound, BlockSize: blocksize, Radix: radix, FullCoeffs: fullCoeffs}
	out.Slots, out.NCoeffs, out.OutputCts = params.Slots(), params.N(), len(results)
	for _, n := range []string{"keystream_crypt", "stc_modswitch", "sub_ntt_setscale", "halfboot", "ckks_eval_2x_plus_1"} {
		out.PerBatchTotalMedianMs += out.Online[n].Median
	}
	for _, n := range []string{"sub_ntt_setscale", "halfboot", "ckks_eval_2x_plus_1"} {
		out.ClientDependentMedianMs += out.Online[n].Median
	}
	out.PerTransactionOnlineMs = out.PerBatchTotalMedianMs / float64(out.Slots)
	out.PerTransactionClientDepMs = out.ClientDependentMedianMs / float64(out.Slots)
	out.PerTransactionNote = fmt.Sprintf("per_transaction = stage-median sum / slots (%d). The batch processed is output element 0 of the cipher only (one of %d state/output ciphertexts, as in the upstream benchmarks); one input ciphertext carries %d coefficient values, returned as %d CKKS ciphertext(s) of %d slots each. Divide by n_coeffs_per_input_ct instead if a transaction is one coefficient.",
		out.Slots, outputsize, out.NCoeffs, out.OutputCts, out.Slots)
	out.VmHWMKB = readVmHWMKB()
	out.TotalAllocKB, out.HeapAllocKB = int64(ms.TotalAlloc/1024), int64(ms.HeapAlloc/1024)

	if err := os.MkdirAll(filepath.Dir(outPath), 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	b, _ := json.MarshalIndent(out, "", "  ")
	if err := os.WriteFile(outPath, b, 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Logf("e2e %s %s LogN=%d slots=%d: per-batch median %.1f ms, per-transaction %.4f ms, max err %v, VmHWM %d kB -> %s",
		cipherName, paramSet, params.LogN(), out.Slots, out.PerBatchTotalMedianMs, out.PerTransactionOnlineMs, out.MaxAbsError, out.VmHWMKB, outPath)
}

// fvEncoder2StC is the benchmarks' per-ciphertext StC step: SlotsToCoeffs then
// ModSwitchMany down to level 0 (RtF_bench_test.go:196-197).
func fvEncoder2StC(ev MFVEvaluator, ct *Ciphertext, stcModDown []int) *Ciphertext {
	out := ev.SlotsToCoeffs(ct, stcModDown)
	ev.ModSwitchMany(out, out, out.Level())
	return out
}
