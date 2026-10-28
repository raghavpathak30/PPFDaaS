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
	"math"
	"math/big"
	"os"
	"runtime"
	"strconv"
	"testing"
	"time"

	"github.com/ldsec/lattigo/v2/ring"
	"github.com/ldsec/lattigo/v2/rlwe"
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
	heapKB    map[string]int64 // retained heap (after GC) added by each setup step
	ringLvl   map[int]*ring.Ring
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
	x := &cnpCtx{cfg: cfg, setupMs: map[string]float64{}, heapKB: map[string]int64{}, ringLvl: map[int]*ring.Ring{}}
	timed := func(name string, f func()) { x.timedMem(name, f) }
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
	timed("stc_matrices", func() {
		x.pDcds = x.fvEncoder.GenSlotToCoeffMatFV(cfg.radix)
		x.stcRots = x.kgen.GenRotationIndexesForSlotsToCoeffsMat(x.pDcds)
	})
	timed("stc_rotation_keys", func() { x.rotkeys = x.kgen.GenRotationKeysForRotations(x.stcRots, true, x.sk) })
	timed("relinearization_key", func() { x.rlk = x.kgen.GenRelinearizationKey(x.sk) })
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

// timedMem times f and records the heap it retains (HeapAlloc after a forced GC, before and after).
func (x *cnpCtx) timedMem(name string, f func()) {
	var m0, m1 runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&m0)
	t0 := time.Now()
	f()
	x.setupMs[name] = cnpMs(time.Since(t0))
	runtime.GC()
	runtime.ReadMemStats(&m1)
	x.heapKB[name] = (int64(m1.HeapAlloc) - int64(m0.HeapAlloc)) / 1024
}

func (x *cnpCtx) ringAt(level int) *ring.Ring {
	if r, ok := x.ringLvl[level]; ok {
		return r
	}
	r, err := ring.NewRing(x.params.N(), x.params.Qi()[:level+1])
	if err != nil {
		panic(err)
	}
	x.ringLvl[level] = r
	return r
}

// cnpNoise: log2 of the max |FV decryption noise| and the headroom to the decryption bound Q_l/(2t),
// in bits, for a decrypted Plaintext (any level). Needs the UNTOUCHED decryption (see cnpDecryptRingT).
func cnpNoise(x *cnpCtx, raw *Plaintext) (noiseBits, headroomBits float64) {
	poly := raw.Value()[0]
	level := poly.Level()
	rq := x.ringAt(level)
	coeffs := make([]*big.Int, x.params.N())
	rq.PolyToBigint(poly, coeffs)
	Q := rq.ModulusBigint
	tt := new(big.Int).SetUint64(x.params.PlainModulus())
	half := new(big.Int).Rsh(Q, 1)
	maxV := new(big.Int)
	for _, p := range coeffs {
		tp := new(big.Int).Mul(tt, p)
		m := new(big.Int).Div(new(big.Int).Add(tp, half), Q)
		v := tp.Sub(tp, m.Mul(m, Q))
		if v.Abs(v).Cmp(maxV) > 0 {
			maxV.Set(v)
		}
	}
	nf, _ := new(big.Float).Quo(new(big.Float).SetInt(maxV), new(big.Float).SetInt(tt)).Float64()
	if nf < 1 {
		nf = 1
	}
	qf, _ := new(big.Float).SetInt(Q).Float64()
	return math.Log2(nf), math.Log2(qf / (2 * float64(tt.Uint64())) / nf)
}

// cnpHalfBoot holds the baseline-only objects.
type cnpHalfBoot struct {
	hbtp     *HalfBootstrapper
	ckksEnc  CKKSEncoder
	ckksDec  CKKSDecryptor
	ckksEval CKKSEvaluator
	hbKeys   int // extra rotation keys the baseline adds on top of the StC ones
}

// cnpBuildHalfBoot adds ONLY the HalfBoot CtS/EvalMod rotation keys (the StC ones already exist in
// x.rotkeys; the merged set shares them, nothing is duplicated) plus the HalfBootstrapper.
func cnpBuildHalfBoot(t *testing.T, x *cnpCtx) *cnpHalfBoot {
	h := &cnpHalfBoot{}
	have := map[int]bool{}
	for _, r := range x.stcRots {
		have[r] = true
	}
	var extra []int
	for _, r := range x.kgen.GenRotationIndexesForHalfBoot(x.params.LogSlots(), x.cfg.hbtpParams) {
		if !have[r] {
			have[r] = true
			extra = append(extra, r)
		}
	}
	var hbKeys *RotationKeySet
	x.timedMem("baseline_halfboot_rotation_keys", func() { hbKeys = x.kgen.GenRotationKeysForRotations(extra, false, x.sk) })
	merged := &RotationKeySet{}
	merged.Keys = map[uint64]*rlwe.SwitchingKey{}
	for k, v := range x.rotkeys.Keys {
		merged.Keys[k] = v
	}
	for k, v := range hbKeys.Keys {
		merged.Keys[k] = v
	}
	h.hbKeys = len(hbKeys.Keys)
	x.timedMem("baseline_halfbootstrapper_and_ckks_objects", func() {
		var err error
		h.hbtp, err = NewHalfBootstrapper(x.params, x.cfg.hbtpParams, BootstrappingKey{Rlk: x.rlk, Rtks: merged})
		if err != nil {
			t.Fatalf("NewHalfBootstrapper: %v", err)
		}
		h.ckksEnc = NewCKKSEncoder(x.params)
		h.ckksDec = NewCKKSDecryptor(x.params, x.sk)
		h.ckksEval = NewCKKSEvaluator(x.params, EvaluationKey{Rlk: x.rlk, Rtks: merged})
	})
	return h
}
