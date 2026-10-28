package ckks_fv

// Shared setup for the CNP-B (Coefficient-Native Packing on the RtF path) harness
// and probes. Staged into ckks_fv/ by third_party/fetch_rtf.sh.
//
// Only the FV side is built here: keygen, relinearisation key, the StC^FV
// matrices and their rotation keys, the FV evaluator. The HalfBoot rotation
// keys and the HalfBootstrapper are built separately (cnpBuildHalfBoot) so a
// CNP-only run never allocates them.
//
// Env: E2E_CIPHER=hera|rubato, E2E_HERA_PARAM_INDEX (2=128af default),
// E2E_LOGN (default 16; LogSlots = LogN-1 for the full-coefficient sets).

import (
	"os"
	"strconv"
	"testing"
	"time"
)

type cnpCfg struct {
	cipher     string
	paramSet   string
	paramIndex int
	numRound   int
	blocksize  int
	radix      int
	rubatoPar  int
	modDown    []int
	stcModDown []int
	hbtpParams *HalfBootParameters
}

type cnpCtx struct {
	cfg       cnpCfg
	params    *Parameters
	kgen      KeyGenerator
	sk        *SecretKey
	pk        *PublicKey
	fvEncoder MFVEncoder
	fvEnc     MFVEncryptor
	fvDec     MFVDecryptor
	fvEval    MFVEvaluator
	rlk       *RelinearizationKey
	stcRots   []int
	rotkeys   *RotationKeySet // StC^FV rotations only until cnpBuildHalfBoot adds more
	pDcds     [][]*PtDiagMatrixT
	setupMs   map[string]float64
	nBlocks   int // nonces per output ciphertext = N (full-coefficient packing)
	key       []uint64
}

func cnpEnvInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func cnpMs(d time.Duration) float64 { return float64(d.Microseconds()) / 1000.0 }

func cnpConfig(t *testing.T, cipher string, logN int) cnpCfg {
	c := cnpCfg{cipher: cipher, radix: 2}
	switch cipher {
	case "hera":
		c.paramIndex = cnpEnvInt("E2E_HERA_PARAM_INDEX", 2)
		if c.paramIndex != 2 {
			t.Fatalf("CNP needs full-coefficient packing: E2E_HERA_PARAM_INDEX must be 2 (128af), got %d", c.paramIndex)
		}
		c.paramSet = "128af"
		c.hbtpParams = RtFHeraParams[2].Copy()
		c.numRound, c.blocksize = 5, 16
		c.modDown = HeraModDownParams128[2].CipherModDown
		c.stcModDown = HeraModDownParams128[2].StCModDown
	case "rubato":
		c.paramSet = "128af"
		c.rubatoPar = RUBATO128L
		c.hbtpParams = RtFRubatoParams[0].Copy()
		c.blocksize = RubatoParams[c.rubatoPar].Blocksize
		c.numRound = RubatoParams[c.rubatoPar].NumRound
		c.modDown = RubatoModDownParams[c.rubatoPar].CipherModDown
		c.stcModDown = RubatoModDownParams[c.rubatoPar].StCModDown
	default:
		t.Fatalf("E2E_CIPHER must be hera or rubato, got %q", cipher)
	}
	if logN != c.hbtpParams.LogN {
		c.hbtpParams.LogN = logN
		c.hbtpParams.LogSlots = logN - 1 // full coefficients: Params() panics unless LogSlots <= LogN-1
	}
	return c
}

// cnpSetup builds the FV-only context (what CNP needs offline + online).
func cnpSetup(t *testing.T, cipher string, logN int) *cnpCtx {
	cfg := cnpConfig(t, cipher, logN)
	x := &cnpCtx{cfg: cfg, setupMs: map[string]float64{}}
	timed := func(name string, f func()) {
		t0 := time.Now()
		f()
		x.setupMs[name] = cnpMs(time.Since(t0))
	}
	params, err := cfg.hbtpParams.Params()
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	if cipher == "rubato" {
		params.SetPlainModulus(RubatoParams[cfg.rubatoPar].PlainModulus)
	}
	params.SetLogFVSlots(params.LogN()) // full coefficients, as the 128af harness does
	x.params = params
	timed("keygen", func() {
		x.kgen = NewKeyGenerator(params)
		x.sk, x.pk = x.kgen.GenKeyPairSparse(cfg.hbtpParams.H)
		x.fvEncoder = NewMFVEncoder(params)
		x.fvEnc = NewMFVEncryptorFromPk(params, x.pk)
		x.fvDec = NewMFVDecryptor(params, x.sk)
	})
	timed("stc_matrices_rotation_keys_rlk", func() {
		x.pDcds = x.fvEncoder.GenSlotToCoeffMatFV(cfg.radix)
		x.stcRots = x.kgen.GenRotationIndexesForSlotsToCoeffsMat(x.pDcds)
		x.rotkeys = x.kgen.GenRotationKeysForRotations(x.stcRots, true, x.sk)
		x.rlk = x.kgen.GenRelinearizationKey(x.sk)
	})
	timed("fv_evaluator", func() {
		x.fvEval = NewMFVEvaluator(params, EvaluationKey{Rlk: x.rlk, Rtks: x.rotkeys}, x.pDcds)
	})
	x.nBlocks = params.N()
	x.key = make([]uint64, cfg.blocksize)
	for i := range x.key {
		x.key[i] = uint64(i + 1)
	}
	return x
}

// cnpHalfBoot holds the baseline-only objects.
type cnpHalfBoot struct {
	hbtp        *HalfBootstrapper
	ckksEnc     CKKSEncoder
	ckksDec     CKKSDecryptor
	ckksEval    CKKSEvaluator
	rotkeysFull *RotationKeySet
}

// cnpBuildHalfBoot adds the HalfBoot CtS/EvalMod rotation keys and the HalfBootstrapper
// (baseline arm only) and records their setup time under setupMs.
func cnpBuildHalfBoot(t *testing.T, x *cnpCtx) *cnpHalfBoot {
	h := &cnpHalfBoot{}
	t0 := time.Now()
	rotsHB := x.kgen.GenRotationIndexesForHalfBoot(x.params.LogSlots(), x.cfg.hbtpParams)
	h.rotkeysFull = x.kgen.GenRotationKeysForRotations(append(rotsHB, x.stcRots...), true, x.sk)
	x.setupMs["baseline_halfboot_rotation_keys"] = cnpMs(time.Since(t0))
	t0 = time.Now()
	var err error
	h.hbtp, err = NewHalfBootstrapper(x.params, x.cfg.hbtpParams, BootstrappingKey{Rlk: x.rlk, Rtks: h.rotkeysFull})
	if err != nil {
		t.Fatalf("NewHalfBootstrapper: %v", err)
	}
	h.ckksEnc = NewCKKSEncoder(x.params)
	h.ckksDec = NewCKKSDecryptor(x.params, x.sk)
	h.ckksEval = NewCKKSEvaluator(x.params, EvaluationKey{Rlk: x.rlk, Rtks: h.rotkeysFull})
	x.setupMs["baseline_halfbootstrapper"] = cnpMs(time.Since(t0))
	return h
}
