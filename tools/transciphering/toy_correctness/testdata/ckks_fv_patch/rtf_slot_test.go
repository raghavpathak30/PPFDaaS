package ckks_fv

// FV-slot baseline for Pipeline B: transcipher into FV SLOTS and evaluate the same logistic-regression
// model there. No SlotToCoeff, no HalfBoot, no CKKS. Design and citations: docs/bfv_slot_baseline_design.md.
// Staged into ckks_fv/ by third_party/fetch_rtf.sh. Nothing in the CNP or HalfBoot arms calls this file.
//
// Layout: a group is G keystream ciphertexts starting at Base, S = 256/G slots per lane. Feature j of lane l
// is slot(l, j/G) of ciphertext Base + j%G. HERA: one group (G=16, S=16). Rubato: 32+16+8+4 (S=8,16,32,64),
// the 60 usable elements with none wasted.
//
// Online, per group: for each ciphertext, slot-encode the upload (inverse NTT mod t), exact scale-up, Sub,
// pmul by its weight vector, add into the group sum (level 1). Then modswitch to level 0, rotate-and-sum by
// doubling over S slots, add a fresh slot mask (uniform on non-output slots, bias at output slots).
// Output of lane l is slot S*l of its row. The bank divides by 2^14, as in CNP-B.
//
// Gated: E2E_RUN=1. Env as rtf_cnp_test.go (E2E_CIPHER, E2E_LOGN, E2E_REPS, CNP_DATA_DIR, CNP_ROWS,
// CNP_BUDGET_S, CNP_HEADROOM_GATE_BITS, RESULTS_DIR, E2E_OUT) plus SLOT_EVAL_LEVEL (default 1). Level 0 is
// the literal round(c*q0/t) reading of the brief; it is expected to fail the headroom gate and exists so that
// the failure is measured rather than asserted. The gate applies at every level.

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"math"
	"math/big"
	"math/bits"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"
	"time"

	"github.com/ldsec/lattigo/v2/ring"
)

// ---- layout (pure arithmetic) -----------------------------------------------------------------------

type slotGroup struct {
	Base  int `json:"base_ct"`
	G     int `json:"cts_G"`
	S     int `json:"slots_per_lane_S"`
	Lanes int `json:"lanes"`
}

// slotLayout returns the groups for a cipher. Group sizes are powers of two so that G*S = 256 exactly, and
// use every usable element, which keeps transactions per Crypt (hence client cost) equal to CNP-B's.
func slotLayout(cipher string, N int) ([]slotGroup, error) {
	var sizes []int
	switch cipher {
	case "hera":
		sizes = []int{16}
	case "rubato":
		sizes = []int{32, 16, 8, 4}
	default:
		return nil, fmt.Errorf("slot layout: unknown cipher %q", cipher)
	}
	groups := make([]slotGroup, 0, len(sizes))
	base := 0
	for _, g := range sizes {
		s := cnpFeatures / g
		if (N/2)%s != 0 {
			return nil, fmt.Errorf("slot layout: S=%d does not divide the row length %d", s, N/2)
		}
		groups = append(groups, slotGroup{Base: base, G: g, S: s, Lanes: N / s})
		base += g
	}
	return groups, nil
}

// slot is the slot index of offset r of lane l. Rows are [0,N/2) and [N/2,N) (mfv_encoder.go:150-160).
func (g slotGroup) slot(l, r, N int) int {
	perRow := (N / 2) / g.S
	if l < perRow {
		return g.S*l + r
	}
	return N/2 + g.S*(l-perRow) + r
}

func (g slotGroup) outputSlot(l, N int) int { return g.slot(l, 0, N) }

// steps is the doubling sequence 1, 2, ..., S/2.
func (g slotGroup) steps() []int {
	var out []int
	for s := 1; s < g.S; s <<= 1 {
		out = append(out, s)
	}
	return out
}

func slotRotationKeys(groups []slotGroup) []int {
	set := map[int]bool{}
	for _, g := range groups {
		for _, s := range g.steps() {
			set[s] = true
		}
	}
	out := make([]int, 0, len(set))
	for s := range set {
		out = append(out, s)
	}
	sort.Ints(out)
	return out
}

// ---- setup (FV only, no StC matrices and no StC keys) --------------------------------------------------

func slotSetup(t *testing.T, cipher string, logN int, groups []slotGroup) *cnpCtx {
	cfg := cnpConfig(t, cipher, logN)
	x := &cnpCtx{cfg: cfg, setupMs: map[string]float64{}, heapKB: map[string]int64{}, ringLvl: map[int]*ring.Ring{}}
	params, err := cfg.hbtpParams.Params()
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	if cipher == "rubato" {
		params.SetPlainModulus(RubatoParams[cfg.rubatoPar].PlainModulus)
	}
	params.SetLogFVSlots(params.LogN())
	x.params = params
	x.timedMem("keygen", func() {
		x.kgen = NewKeyGenerator(params)
		x.sk, x.pk = x.kgen.GenKeyPairSparse(cfg.hbtpParams.H)
		x.fvEncoder = NewMFVEncoder(params)
		x.fvEnc = NewMFVEncryptorFromPk(params, x.pk)
		x.fvDec = NewMFVDecryptor(params, x.sk)
	})
	x.timedMem("slot_rotation_keys", func() { x.rotkeys = x.kgen.GenRotationKeysForRotations(slotRotationKeys(groups), false, x.sk) })
	x.timedMem("relinearization_key", func() { x.rlk = x.kgen.GenRelinearizationKey(x.sk) })
	x.timedMem("fv_evaluator", func() {
		x.fvEval = NewMFVEvaluator(params, EvaluationKey{Rlk: x.rlk, Rtks: x.rotkeys}, nil)
	})
	x.nBlocks = params.N()
	x.key = make([]uint64, cfg.blocksize)
	for i := range x.key {
		x.key[i] = uint64(i + 1)
	}
	return x
}

// slotKeyBytes walks the allocated key objects: this is what the fork actually holds.
func slotKeyBytes(rks *RotationKeySet) int64 {
	var n int64
	for _, swk := range rks.Keys {
		for _, block := range swk.Value {
			for _, p := range block {
				for _, limb := range p.Coeffs {
					n += int64(len(limb)) * 8
				}
			}
		}
	}
	return n
}

// ---- exact scale-up ------------------------------------------------------------------------------------

// slotScaler computes round(c*Q_l/t) in RNS: with Q_l = t*D + r, round(c*Q_l/t) = c*D + round(c*r/t), and
// c, r < 2^25 so the second term fits in 64 bits. At level 0 this is CNP-B's round(c*q0/t) bit for bit
// (TestSlotScaleUpExact). Keyless and public. The D residues are precomputed once, offline.
type slotScaler struct {
	x     *cnpCtx
	level int
	r, t  uint64
	dMont []uint64
	q     []uint64
	mred  []uint64
}

func newSlotScaler(x *cnpCtx, level int) *slotScaler {
	rq := x.ringAt(level)
	t := x.params.PlainModulus()
	D, r := new(big.Int).QuoRem(rq.ModulusBigint, new(big.Int).SetUint64(t), new(big.Int))
	s := &slotScaler{x: x, level: level, r: r.Uint64(), t: t}
	for i, q := range rq.Modulus {
		d := new(big.Int).Mod(D, new(big.Int).SetUint64(q)).Uint64()
		s.dMont = append(s.dMont, ring.MForm(d, q, rq.BredParams[i]))
		s.q = append(s.q, q)
		s.mred = append(s.mred, rq.MredParams[i])
	}
	return s
}

func (s *slotScaler) scale(coeffs []uint64) *Plaintext {
	pt := NewPlaintextFVLvl(s.x.params, s.level)
	rows := pt.Value()[0].Coeffs
	half := s.t / 2
	for i, c := range coeffs {
		k := (c*s.r + half) / s.t
		for li := range rows {
			ki := k
			if ki >= s.q[li] { // k < t, and the chain has a 22-bit prime smaller than t (rtf_params.go DiffScaleModulus)
				ki %= s.q[li]
			}
			rows[li][i] = ring.CRed(ring.MRed(c, s.dMont[li], s.q[li], s.mred[li])+ki, s.q[li])
		}
	}
	return pt
}

// ---- weights and mask, in the slot domain --------------------------------------------------------------

// slotWeightPoly slot-encodes the weight vector of ciphertext Base+c (slot offset r holds wq[r*G+c], in every
// lane and both rows) and returns the polynomial with the signed lift applied: coefficients centred mod t.
// The vector is periodic, so only 2S coefficients are nonzero, but those are ~t in size (design section 4.4).
func slotWeightPoly(x *cnpCtx, g slotGroup, c int, wq []int64) []int64 {
	N, t := x.params.N(), int64(x.params.PlainModulus())
	vals := make([]uint64, N)
	for l := 0; l < g.Lanes; l++ {
		for r := 0; r < g.S; r++ {
			vals[g.slot(l, r, N)] = uint64(((wq[r*g.G+c] % t) + t) % t)
		}
	}
	ptRt := NewPlaintextRingT(x.params)
	x.fvEncoder.EncodeUintRingT(vals, ptRt)
	out := make([]int64, N)
	for i, v := range ptRt.Value()[0].Coeffs[0] {
		out[i] = cnpCentre(int64(v), t)
	}
	return out
}

// slotMaskSlots is the mask in the slot domain: uniform mod t (cnpUniformModT, the CNP-B generator) on every
// slot, then round(bias*2^14) mod t at the output slots. randomize=false gives zeros off-output (test-only).
func slotMaskSlots(x *cnpCtx, g slotGroup, biasQ int64, randomize bool) []uint64 {
	N, t := x.params.N(), x.params.PlainModulus()
	v := make([]uint64, N)
	if randomize {
		cnpUniformModT(v, t)
	}
	b := uint64(((biasQ % int64(t)) + int64(t)) % int64(t))
	for l := 0; l < g.Lanes; l++ {
		v[g.outputSlot(l, N)] = b
	}
	return v
}

// slotMaskFV encodes a slot mask with the same exact scale-up as CNP-B's mask (cnpScaleUp, level 0).
func slotMaskFV(x *cnpCtx, slots []uint64) *Plaintext {
	ptRt := NewPlaintextRingT(x.params)
	x.fvEncoder.EncodeUintRingT(slots, ptRt)
	return cnpScaleUp(x, 0, ptRt.Value()[0].Coeffs[0])
}

// slotRawMaskPlaintext is the negative control: the mask polynomial NOT scaled by Q/t.
func slotRawMaskPlaintext(x *cnpCtx, slots []uint64) *Plaintext {
	ptRt := NewPlaintextRingT(x.params)
	x.fvEncoder.EncodeUintRingT(slots, ptRt)
	pt := NewPlaintextFVLvl(x.params, 0)
	copy(pt.Value()[0].Coeffs[0], ptRt.Value()[0].Coeffs[0])
	return pt
}

// ---- client, server, bank ------------------------------------------------------------------------------

// slotClientSym builds the upload for keystream ciphertext Base+c: element i = x[i] + ks[nonce i][Base+c] mod t,
// the same one-add-mod-t per element as cnpClientSym, with no bit reversal (no StC on this path).
// rows[l] is the quantised row of lane l, or nil for an unfilled lane (message 0).
func slotClientSym(x *cnpCtx, g slotGroup, c int, rows [][]int16, ks [][]uint64) []uint64 {
	N, t := x.params.N(), int64(x.params.PlainModulus())
	kk := g.Base + c
	sym := make([]uint64, N)
	for i := range sym {
		sym[i] = ks[i][kk] // message 0, overwritten for filled lanes
	}
	for l, row := range rows {
		if row == nil {
			continue
		}
		for r := 0; r < g.S; r++ {
			i := g.slot(l, r, N)
			v := (int64(row[r*g.G+c]) + int64(ks[i][kk])) % t
			if v < 0 {
				v += t
			}
			sym[i] = uint64(v)
		}
	}
	return sym
}

type slotTimes struct{ encode, scaleUp, sub, pmulAdd, modSwitch, rotate time.Duration }

func (s *slotTimes) total() time.Duration {
	return s.encode + s.scaleUp + s.sub + s.pmulAdd + s.modSwitch + s.rotate
}

type slotModel struct {
	x      *cnpCtx
	groups []slotGroup
	level  int
	scaler *slotScaler
	ptMul  [][]*PlaintextMul // [group][c]
}

func newSlotModel(x *cnpCtx, groups []slotGroup, level int, wq []int64) (*slotModel, []int) {
	m := &slotModel{x: x, groups: groups, level: level, scaler: newSlotScaler(x, level)}
	var nonzero []int
	for _, g := range groups {
		row := make([]*PlaintextMul, g.G)
		nz := 0
		for c := 0; c < g.G; c++ {
			poly := slotWeightPoly(x, g, c, wq)
			for _, v := range poly {
				if v != 0 {
					nz++
				}
			}
			row[c] = cnpPtMulSigned(x, level, poly) // the CNP-B signed-lift plaintext, reused unchanged
		}
		m.ptMul = append(m.ptMul, row)
		nonzero = append(nonzero, nz/g.G)
	}
	return m, nonzero
}

// preMask runs one group up to (not including) the mask. ks holds every keystream ciphertext at m.level,
// indexed by state element. probe, if not nil, sees each intermediate ciphertext (noise checkpoints only).
func (m *slotModel) preMask(gi int, syms [][]uint64, ks []*Ciphertext, st *slotTimes, probe func(string, *Ciphertext)) *Ciphertext {
	x, g := m.x, m.groups[gi]
	var acc *Ciphertext
	for c := 0; c < g.G; c++ {
		kk := g.Base + c
		t0 := time.Now()
		ptRt := NewPlaintextRingT(x.params)
		x.fvEncoder.EncodeUintRingT(syms[c], ptRt)
		st.encode += time.Since(t0)

		t0 = time.Now()
		pt := m.scaler.scale(ptRt.Value()[0].Coeffs[0])
		st.scaleUp += time.Since(t0)

		t0 = time.Now()
		ct := NewCiphertextFVLvl(x.params, 1, m.level)
		ct.Value()[0] = pt.Value()[0]
		x.fvEval.Sub(ct, ks[kk], ct)
		st.sub += time.Since(t0)
		if probe != nil && c == 0 {
			probe("before_sub", ks[kk])
			probe("after_sub", ct)
		}

		t0 = time.Now()
		prod := NewCiphertextFVLvl(x.params, 1, m.level)
		x.fvEval.Mul(ct, m.ptMul[gi][c], prod)
		if acc == nil {
			acc = prod
		} else {
			x.fvEval.Add(acc, prod, acc)
		}
		st.pmulAdd += time.Since(t0)
		if probe != nil && c == 0 {
			probe("after_pmul_first_ct", prod)
		}
	}
	if probe != nil {
		probe("after_group_sum", acc)
	}
	if m.level > 0 {
		t0 := time.Now()
		x.fvEval.ModSwitchMany(acc, acc, m.level)
		st.modSwitch += time.Since(t0)
		if probe != nil {
			probe("after_modswitch_to_level0", acc)
		}
	}
	t0 := time.Now()
	tmp := NewCiphertextFVLvl(x.params, 1, 0)
	for _, step := range g.steps() {
		x.fvEval.RotateColumns(acc, step, tmp)
		x.fvEval.Add(acc, tmp, acc)
	}
	st.rotate += time.Since(t0)
	if probe != nil {
		probe("after_rotate_sum", acc)
	}
	return acc
}

// slotDecode decrypts to all N slot values.
func slotDecode(x *cnpCtx, ct *Ciphertext) []uint64 {
	coeffs, _ := cnpDecryptRingT(x, ct)
	ptRt := NewPlaintextRingT(x.params)
	copy(ptRt.Value()[0].Coeffs[0], coeffs)
	out := make([]uint64, x.params.N())
	x.fvEncoder.DecodeUint(ptRt, out)
	return out
}

// slotBankRead: decrypt, decode the slots, centre mod t, read each lane's output slot.
func slotBankRead(x *cnpCtx, ct *Ciphertext, g slotGroup) []int64 {
	slots := slotDecode(x, ct)
	N, t := x.params.N(), int64(x.params.PlainModulus())
	out := make([]int64, g.Lanes)
	for l := range out {
		out[l] = cnpCentre(int64(slots[g.outputSlot(l, N)]), t)
	}
	return out
}

// slotKeystreamAtLevel is the OFFLINE step on this path: ModSwitchMany only. No StC.
func slotKeystreamAtLevel(x *cnpCtx, ct *Ciphertext, level int) *Ciphertext {
	out := ct.CopyNew().Ciphertext()
	if d := out.Level() - level; d > 0 {
		x.fvEval.ModSwitchMany(out, out, d)
	} else if d < 0 {
		panic(fmt.Sprintf("keystream at level %d is below the evaluation level %d", out.Level(), level))
	}
	return out
}

// slotRowsFor picks the lanes of one group from the row cursor.
func slotRowsFor(d *cnpData, g slotGroup, first int) [][]int16 {
	rows := make([][]int16, g.Lanes)
	for l := range rows {
		if first+l < len(d.rows) {
			rows[l] = d.xq[d.rows[first+l]]
		}
	}
	return rows
}

// ---- output --------------------------------------------------------------------------------------------

type slotOut struct {
	DateUTC       string                 `json:"date_utc"`
	Arm           string                 `json:"arm"`
	ParamState    e2eParamState          `json:"param_state"`
	N             int                    `json:"n"`
	T             uint64                 `json:"plain_modulus"`
	Q0Bits        float64                `json:"q0_bits"`
	QLevelBits    float64                `json:"q_bits_at_eval_level"`
	ScaleUp       string                 `json:"scale_up_server_side"`
	Groups        []slotGroup            `json:"groups"`
	LevelAfter    int                    `json:"keystream_level_after_crypt"`
	EvalLevel     int                    `json:"eval_level"`
	FinalLevel    int                    `json:"final_ct_level"`
	LanesPerCrypt int                    `json:"lanes_per_crypt"`
	OutPerCrypt   int                    `json:"output_cts_per_crypt"`
	CtsPerCrypt   int                    `json:"keystream_cts_per_crypt"`
	LanesPerOut   float64                `json:"lanes_per_output_ct_mean"`
	RotPerCrypt   int                    `json:"rotations_per_crypt"`
	PtNonzero     []int                  `json:"weight_poly_nonzero_coeffs_per_group"`
	RotKeyIdx     []int                  `json:"rotation_key_indexes"`
	RotKeys       map[string]int         `json:"rotation_key_counts"`
	RotKeyBytes   int64                  `json:"rotation_key_bytes_allocated"`
	RotLevel0Used int64                  `json:"rotation_key_bytes_touched_at_level0_computed"`
	Wire          map[string]interface{} `json:"wire_bytes"`
	Reps          int                    `json:"reps"`
	DataSource    string                 `json:"data_source"`
	DataCheck     string                 `json:"data_check,omitempty"`
	Seeding       string                 `json:"seeding_note"`
	LoadAvgStart  string                 `json:"host_loadavg_start"`
	LoadAvgEnd    string                 `json:"host_loadavg_end"`
	GOMAXPROCS    int                    `json:"gomaxprocs"`
	GOGC          string                 `json:"gogc_env"`
	GOMEMLIMIT    string                 `json:"gomemlimit_env"`
	Provenance    map[string]string      `json:"provenance"`
	SetupMs       map[string]float64     `json:"setup_ms"`
	SetupHeapKB   map[string]int64       `json:"setup_retained_heap_kb"`
	Stages        map[string]*e2eStage   `json:"slot_stages_per_crypt_batch"`
	PerBatchMs    map[string]float64     `json:"per_crypt_batch_ms"`
	PerCtMs       map[string]float64     `json:"per_keystream_ct_ms"`
	PerOutMs      map[string]float64     `json:"per_output_ct_ms"`
	PerTxnMs      map[string]float64     `json:"per_transaction_ms"`
	OfflineNote   string                 `json:"offline_note"`
	Noise         []cnpNoisePoint        `json:"noise_headroom"`
	GateBits      float64                `json:"headroom_gate_bits"`
	Gate          string                 `json:"headroom_gate"`
	Checkpoints   []cnpCheckpoint        `json:"memory_checkpoints"`
	Correctness   map[string]interface{} `json:"correctness"`
	MaskTests     map[string]interface{} `json:"mask_tests,omitempty"`
	CryptCalls    int                    `json:"crypt_calls"`
	RowSet        string                 `json:"row_set"`
	RowEstimateS  float64                `json:"row_set_estimate_s"`
	VmHWMKBEnd    int64                  `json:"vm_hwm_kb_end"`
	TotalAllocKB  int64                  `json:"total_alloc_kb_end"`
}

func slotLogQ(x *cnpCtx, level int) float64 {
	f, _ := new(big.Float).SetInt(x.ringAt(level).ModulusBigint).Float64()
	return math.Log2(f)
}

// ---- the run -------------------------------------------------------------------------------------------

func TestSlotRun(t *testing.T) {
	if os.Getenv("E2E_RUN") != "1" {
		t.Skip("set E2E_RUN=1 to run (full scale allocates tens of GB)")
	}
	cipher := cnpEnvStr("E2E_CIPHER", "hera")
	logN := cnpEnvInt("E2E_LOGN", 16)
	reps := cnpEnvInt("E2E_REPS", 5)
	evalLevel := cnpEnvInt("SLOT_EVAL_LEVEL", 1)
	gateBits := float64(cnpEnvInt("CNP_HEADROOM_GATE_BITS", 5))
	resultsDir := cnpEnvStr("RESULTS_DIR", "/results")
	outPath := cnpEnvStr("E2E_OUT", filepath.Join(resultsDir, "slot_"+cipher+".json"))
	if s := cnpEnvStr("CNP_SCALEUP", "exact"); s != "exact" {
		t.Fatalf("the slot path uses the exact scale-up only; CNP_SCALEUP=%s is not valid here", s)
	}
	groups, err := slotLayout(cipher, 1<<uint(logN))
	if err != nil {
		t.Fatal(err)
	}

	out := &slotOut{DateUTC: time.Now().UTC().Format(time.RFC3339), Arm: "slot", Reps: reps, Groups: groups, EvalLevel: evalLevel,
		GateBits: gateBits, LoadAvgStart: cnpLoad(), GOMAXPROCS: runtime.GOMAXPROCS(0),
		GOGC: os.Getenv("GOGC"), GOMEMLIMIT: os.Getenv("GOMEMLIMIT"),
		ScaleUp: "exact: round(c*Q_l/t) at the evaluation level (slotScaler); level-0 mask uses cnpScaleUp",
		Stages:  map[string]*e2eStage{}, Correctness: map[string]interface{}{}, RotKeys: map[string]int{},
		Seeding: "Unseeded: HE keys, nonces, Rubato counter and noise, and the masks come from crypto/rand each run. Symmetric key is fixed (1..blocksize). Synthetic data (if used) is seeded.",
		Provenance: map[string]string{"image_ref": os.Getenv("PPFDAAS_IMAGE_REF"), "repo_sha": os.Getenv("PPFDAAS_REPO_SHA"),
			"rtf_sha": os.Getenv("PPFDAAS_RTF_SHA"), "rtf_patch_sha256": os.Getenv("PPFDAAS_PATCH_SHA256"),
			"build_date": os.Getenv("PPFDAAS_BUILD_DATE")},
		OfflineNote: "Offline (keystream_crypt, keystream_modswitch) depends only on nonces. No SlotToCoeff on this path. Online = client-dependent stages only.",
	}
	cp := func(name string) {
		var ms runtime.MemStats
		runtime.ReadMemStats(&ms)
		out.Checkpoints = append(out.Checkpoints, cnpCheckpoint{name, readVmHWMKB(), int64(ms.TotalAlloc / 1024), int64(ms.HeapAlloc / 1024)})
	}
	write := func() {
		var ms runtime.MemStats
		runtime.ReadMemStats(&ms)
		out.VmHWMKBEnd, out.TotalAllocKB, out.LoadAvgEnd = readVmHWMKB(), int64(ms.TotalAlloc/1024), cnpLoad()
		_ = os.MkdirAll(filepath.Dir(outPath), 0755)
		b, _ := json.MarshalIndent(out, "", "  ")
		_ = ioutil.WriteFile(outPath, b, 0644)
	}
	defer write()

	// ---- setup ----
	x := slotSetup(t, cipher, logN, groups)
	out.SetupMs, out.SetupHeapKB = x.setupMs, x.heapKB
	cp("after_fv_setup")
	N, tmod := x.params.N(), x.params.PlainModulus()
	out.N, out.T = N, tmod
	out.Q0Bits = math.Log2(float64(x.params.Qi()[0]))
	out.QLevelBits = slotLogQ(x, evalLevel)
	out.ParamState = e2eParamState{Cipher: cipher, ParamSet: x.cfg.paramSet, ParamIndex: x.cfg.paramIndex, LogN: x.params.LogN(),
		LogSlots: x.params.LogSlots(), ModCount: x.params.QiCount(), Rounds: x.cfg.numRound, BlockSize: x.cfg.blocksize, Radix: x.cfg.radix, FullCoeffs: true}
	out.RotKeyIdx = slotRotationKeys(groups)
	out.RotKeys["slot_rotations"] = len(x.rotkeys.Keys)
	out.RotKeyBytes = slotKeyBytes(x.rotkeys)
	out.RotLevel0Used = int64(len(x.rotkeys.Keys)) * 2 * int64(1+x.params.Alpha()) * int64(N) * 8
	for _, g := range groups {
		out.LanesPerCrypt += g.Lanes
		out.CtsPerCrypt += g.G
		out.RotPerCrypt += len(g.steps())
	}
	out.OutPerCrypt = len(groups)
	out.LanesPerOut = float64(out.LanesPerCrypt) / float64(out.OutPerCrypt)

	// ---- data ----
	d := cnpLoadData(t, os.Getenv("CNP_DATA_DIR"), out.LanesPerCrypt+7, tmod)
	out.DataSource, out.DataCheck = d.source, d.pyCheck

	nonces, counter, ks := cnpClientKS(t, x)
	crypt, reset, outputs := cnpCrypt(t, x, nonces, counter)
	if outputs != out.CtsPerCrypt {
		t.Fatalf("layout uses %d ciphertexts, the cipher yields %d usable outputs", out.CtsPerCrypt, outputs)
	}
	cp("after_cipher_init_and_enckey")

	// ---- offline, REPS times ----
	var all []*Ciphertext
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			if r > 0 {
				reset()
			}
			t0 := time.Now()
			all = crypt()
			runs = append(runs, cnpMs(time.Since(t0)))
		}
		out.Stages["keystream_crypt (offline)"] = cnpSummarise(runs)
	}
	cp("after_crypt")
	out.LevelAfter = all[0].Level()
	if evalLevel > out.LevelAfter {
		t.Fatalf("SLOT_EVAL_LEVEL=%d above the keystream level %d after Crypt", evalLevel, out.LevelAfter)
	}
	var ksL []*Ciphertext
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			cur := make([]*Ciphertext, outputs)
			t0 := time.Now()
			for k := 0; k < outputs; k++ {
				cur[k] = slotKeystreamAtLevel(x, all[k], evalLevel)
			}
			runs = append(runs, cnpMs(time.Since(t0)))
			ksL = cur
		}
		out.Stages["keystream_modswitch (offline, all usable cts)"] = cnpSummarise(runs)
	}
	cp("after_keystream_modswitch")

	// ---- server model (offline, once) ----
	var m *slotModel
	x.timedMem("encode_weights_ptmul_signed_lift", func() { m, out.PtNonzero = newSlotModel(x, groups, evalLevel, d.wq) })
	cp("after_weight_plaintexts")
	{
		bpe := bits.Len64(tmod - 1)
		perLane := float64(out.LanesPerCrypt)
		upBits := outputs * N * bpe
		out.Wire = map[string]interface{}{
			"upload":                  "RtF symmetric ciphertext: N elements mod t per keystream ciphertext; the server does the slot encoding and scale-up",
			"upload_bits_per_element": bpe, "upload_bytes_per_crypt_bitpacked": (upBits + 7) / 8,
			"upload_bytes_per_transaction_bitpacked":   float64((upBits+7)/8) / perLane,
			"upload_bytes_per_transaction_uint32_wire": float64(4*outputs*N) / perLane,
			"download":       "FV ciphertext at level 0: 2 polys x 1 limb x N; no compression",
			"download_limbs": 1, "download_bytes_per_output_ct_uint64_limbs": 2 * N * 8,
			"download_bytes_per_output_ct_60bit_packed":   2 * N * 60 / 8,
			"download_bytes_per_transaction_uint64_limbs": float64(out.OutPerCrypt*2*N*8) / perLane,
			"download_bytes_per_transaction_60bit_packed": float64(out.OutPerCrypt*2*N*60/8) / perLane,
			"note": "computed from the object layout (the fork has no ciphertext marshaller)",
		}
	}

	// ---- client data for batch 0 ----
	buildSyms := func(ksB [][]uint64, first int) [][][]uint64 {
		syms := make([][][]uint64, len(groups))
		cur := first
		for gi, g := range groups {
			rows := slotRowsFor(d, g, cur)
			cur += g.Lanes
			syms[gi] = make([][]uint64, g.G)
			for c := 0; c < g.G; c++ {
				syms[gi][c] = slotClientSym(x, g, c, rows, ksB)
			}
		}
		return syms
	}
	syms0 := buildSyms(ks, 0)

	// ---- noise at each point, every group, gate BEFORE any more work ----
	noiseAt := func(point string, ct *Ciphertext) float64 {
		nb, hb := cnpNoise(x, x.fvDec.DecryptNew(ct))
		out.Noise = append(out.Noise, cnpNoisePoint{point, ct.Level(), nb, hb})
		return hb
	}
	prep := make([]*Ciphertext, len(groups))
	masked := make([]*Ciphertext, len(groups))
	minFinal, minPre := math.Inf(1), math.Inf(1)
	for gi, g := range groups {
		var st slotTimes
		label := fmt.Sprintf("group%d(G=%d,S=%d) ", gi, g.G, g.S)
		prep[gi] = m.preMask(gi, syms0[gi], ksL, &st, func(p string, ct *Ciphertext) { noiseAt(label+p, ct) })
		if h := noiseAt(label+"before_mask", prep[gi]); h < minPre {
			minPre = h
		}
		masked[gi] = prep[gi].CopyNew().Ciphertext()
		x.fvEval.Add(masked[gi], slotMaskFV(x, slotMaskSlots(x, g, d.biasQ, true)), masked[gi])
		if h := noiseAt(label+"after_mask_add", masked[gi]); h < minFinal {
			minFinal = h
		}
	}
	out.FinalLevel = masked[0].Level()
	if minFinal < gateBits {
		out.Gate = fmt.Sprintf("STOPPED: headroom of the FINAL ciphertext (after the mask add) %.2f bits < %.0f (eval level %d). Nothing beyond batch 0 was run.", minFinal, gateBits, evalLevel)
		write()
		t.Fatalf("%s", out.Gate)
	}
	out.Gate = fmt.Sprintf("passed: min headroom of the final ciphertexts (after mask add) %.2f bits >= %.0f (before mask %.2f)", minFinal, gateBits, minPre)
	cp("after_noise_checks")

	// ---- online stages, REPS times, the whole batch each time ----
	var encRuns, scaleRuns, subRuns, mulRuns, msRuns, rotRuns, genRuns, addRuns, totRuns, clientRuns, bankRuns []float64
	for r := 0; r < reps; r++ {
		t0 := time.Now()
		cl := buildSyms(ks, 0) // client work, reported separately, not in the server total
		clientRuns = append(clientRuns, cnpMs(time.Since(t0)))

		var st slotTimes
		var tGen, tAdd, tBank time.Duration
		for gi, g := range groups {
			ct := m.preMask(gi, cl[gi], ksL, &st, nil)
			ts := time.Now()
			mk := slotMaskSlots(x, g, d.biasQ, true)
			tGen += time.Since(ts)
			ts = time.Now()
			x.fvEval.Add(ct, slotMaskFV(x, mk), ct)
			tAdd += time.Since(ts)
			ts = time.Now()
			_ = slotBankRead(x, ct, g)
			tBank += time.Since(ts)
		}
		encRuns = append(encRuns, cnpMs(st.encode))
		scaleRuns = append(scaleRuns, cnpMs(st.scaleUp))
		subRuns = append(subRuns, cnpMs(st.sub))
		mulRuns = append(mulRuns, cnpMs(st.pmulAdd))
		msRuns = append(msRuns, cnpMs(st.modSwitch))
		rotRuns = append(rotRuns, cnpMs(st.rotate))
		genRuns = append(genRuns, cnpMs(tGen))
		addRuns = append(addRuns, cnpMs(tAdd))
		totRuns = append(totRuns, cnpMs(st.total()+tGen+tAdd))
		bankRuns = append(bankRuns, cnpMs(tBank))
	}
	s := out.Stages
	s["slot_encode (inverse NTT mod t)"] = cnpSummarise(encRuns)
	s["scaleup (exact)"] = cnpSummarise(scaleRuns)
	s["sub"] = cnpSummarise(subRuns)
	s["pmul_and_group_add"] = cnpSummarise(mulRuns)
	s["modswitch_to_level0"] = cnpSummarise(msRuns)
	s["rotate_and_sum"] = cnpSummarise(rotRuns)
	s["mask_generate (CSPRNG, rejection sampling)"] = cnpSummarise(genRuns)
	s["mask_encode_scaleup_add"] = cnpSummarise(addRuns)
	s["online_total"] = cnpSummarise(totRuns)
	s["client_build_symmetric_cts (not server)"] = cnpSummarise(clientRuns)
	s["bank_decrypt_and_read (not server)"] = cnpSummarise(bankRuns)
	med := func(n string) float64 { return s[n].Median }
	offB := med("keystream_crypt (offline)") + med("keystream_modswitch (offline, all usable cts)")
	onB := med("online_total")
	out.PerBatchMs = map[string]float64{"offline": offB, "online": onB, "total": offB + onB}
	out.PerCtMs = map[string]float64{"offline": offB / float64(outputs), "online": onB / float64(outputs), "total": (offB + onB) / float64(outputs)}
	out.PerOutMs = map[string]float64{"offline": offB / float64(out.OutPerCrypt), "online": onB / float64(out.OutPerCrypt), "total": (offB + onB) / float64(out.OutPerCrypt)}
	lanesF := float64(out.LanesPerCrypt)
	out.PerTxnMs = map[string]float64{"offline": offB / lanesF, "online": onB / lanesF, "total": (offB + onB) / lanesF}
	cp("after_online_stages")

	// ---- mask tests (ported from CNP-B to slots), on batch 0 ----
	mt := map[string]interface{}{}
	out.MaskTests = mt
	{
		bad, badRaw := 0, 0
		var non, nonU []uint64
		for gi, g := range groups {
			base := prep[gi]
			uSlots := slotDecode(x, base)
			mSlots := slotDecode(x, masked[gi])
			raw := base.CopyNew().Ciphertext()
			x.fvEval.Add(raw, slotRawMaskPlaintext(x, slotMaskSlots(x, g, d.biasQ, false)), raw)
			rSlots := slotDecode(x, raw)
			isOut := make([]bool, N)
			for l := 0; l < g.Lanes; l++ {
				p := g.outputSlot(l, N)
				isOut[p] = true
				want := cnpCentre(int64(uSlots[p])+d.biasQ, int64(tmod))
				if cnpCentre(int64(mSlots[p]), int64(tmod)) != want {
					bad++
				}
				if cnpCentre(int64(rSlots[p]), int64(tmod)) != want {
					badRaw++
				}
			}
			for p := 0; p < N; p++ {
				if !isOut[p] {
					non = append(non, mSlots[p])
					nonU = append(nonU, uSlots[p])
				}
			}
		}
		mt["test5_masked_equals_unmasked_plus_bias_mismatching_lanes"] = bad
		mt["test5_negative_control_raw_unscaled_mask_mismatching_lanes"] = badRaw
		st, df, p := cnpChiSquare(non, tmod, 64)
		mt["chi_square_masked_nonoutput_slots"] = map[string]interface{}{"stat": st, "df": df, "p": p, "samples": len(non)}
		st, df, p = cnpChiSquare(nonU, tmod, 64)
		mt["chi_square_unmasked_nonoutput_slots"] = map[string]interface{}{"stat": st, "df": df, "p": p, "samples": len(nonU)}
		if bad != 0 {
			write()
			t.Fatalf("mask test 5 failed: %d lanes differ from unmasked+bias", bad)
		}
		if badRaw == 0 {
			write()
			t.Fatalf("mask test 5 would not catch a non-FV-encoded mask (negative control matched)")
		}
	}
	{
		// Weight recovery from NON-output slots. Query a one-hot feature in every even lane of group 0 (odd lanes
		// zero, so a window reaching into the next lane adds nothing). Slot r>=1 of that lane then holds wq[f] exactly
		// when unmasked. Query minus zero query cancels the keystream, including Rubato's added noise.
		g := groups[0]
		qRows := make([][]int16, g.Lanes)
		zRows := make([][]int16, g.Lanes)
		zeroRow := make([]int16, cnpFeatures)
		type target struct{ slot, f int }
		var targets []target
		for l := range qRows {
			zRows[l] = zeroRow
			qRows[l] = zeroRow
			if l%2 != 0 {
				continue
			}
			r := (l/2)%(g.S-1) + 1
			f := r*g.G + (l/2)%g.G
			row := make([]int16, cnpFeatures)
			row[f] = 1
			qRows[l] = row
			targets = append(targets, target{g.slot(l, r, N), f})
		}
		run := func(rows [][]int16, mask bool) []uint64 {
			sy := make([][]uint64, g.G)
			for c := range sy {
				sy[c] = slotClientSym(x, g, c, rows, ks)
			}
			var st slotTimes
			ct := m.preMask(0, sy, ksL, &st, nil)
			if mask {
				x.fvEval.Add(ct, slotMaskFV(x, slotMaskSlots(x, g, d.biasQ, true)), ct)
			}
			return slotDecode(x, ct)
		}
		u1, u0 := run(qRows, false), run(zRows, false)
		m1, m0 := run(qRows, true), run(zRows, true)
		T := int64(tmod)
		okU, okM := 0, 0
		for _, tg := range targets {
			if cnpCentre(int64(u1[tg.slot])-int64(u0[tg.slot]), T) == d.wq[tg.f] {
				okU++
			}
			if cnpCentre(int64(m1[tg.slot])-int64(m0[tg.slot]), T) == d.wq[tg.f] {
				okM++
			}
		}
		mt["weight_recovery_targets_nonoutput_slots"] = len(targets)
		mt["weight_recovery_unmasked_exact"] = okU
		mt["weight_recovery_masked_exact (chance ~ targets/t)"] = okM
		if okU != len(targets) {
			write()
			t.Fatalf("unmasked weight recovery got %d/%d: the test has no power", okU, len(targets))
		}
		if okM != 0 {
			write()
			t.Fatalf("masked output still leaks weights: %d/%d exact", okM, len(targets))
		}
	}
	cp("after_mask_tests")

	// ---- correctness over the selected rows ----
	real := len(d.source) >= 4 && d.source[:4] == "real"
	rowMode := cnpEnvStr("CNP_ROWS", "auto")
	var rowsSel []int
	if real {
		nTot := len(d.xq)
		ncrypt := (nTot + out.LanesPerCrypt - 1) / out.LanesPerCrypt
		est := float64(ncrypt) * (offB + onB + med("client_build_symmetric_cts (not server)") + med("bank_decrypt_and_read (not server)")) / 1000
		out.RowEstimateS = est
		budget := float64(cnpEnvInt("CNP_BUDGET_S", 7200))
		switch {
		case rowMode == "all" || (rowMode == "auto" && est <= budget):
			out.RowSet = fmt.Sprintf("full test set (%d rows, %d Crypt calls); estimate %.0f s <= budget %.0f s", nTot, ncrypt, est, budget)
			for r := 0; r < nTot; r++ {
				rowsSel = append(rowsSel, r)
			}
		case rowMode == "auto" || rowMode == "strat":
			idx, err := ioutil.ReadFile(filepath.Join(os.Getenv("CNP_DATA_DIR"), "strat4096.i32"))
			if err != nil {
				t.Fatalf("strat4096.i32: %v", err)
			}
			for i := 0; i+4 <= len(idx); i += 4 {
				rowsSel = append(rowsSel, int(binary.LittleEndian.Uint32(idx[i:])))
			}
			out.RowSet = fmt.Sprintf("STRATIFIED subset (%d rows: all frauds + seeded random non-frauds); full-set estimate %.0f s", len(rowsSel), est)
		default:
			var n int
			fmt.Sscanf(rowMode, "%d", &n)
			for r := 0; r < n && r < nTot; r++ {
				rowsSel = append(rowsSel, r)
			}
			out.RowSet = fmt.Sprintf("first %d rows (CNP_ROWS=%s)", len(rowsSel), rowMode)
		}
		d.rows = rowsSel
	} else {
		out.RowSet = fmt.Sprintf("synthetic %d rows", len(d.rows))
	}
	t.Logf("row set: %s", out.RowSet)

	decRows := make([]int32, 0, len(d.rows))
	decVals := make([]int64, 0, len(d.rows))
	mismatches, maxAbsInt := 0, int64(0)
	var sumAbs float64
	cryptCalls := 0
	cursor := 0
	for b := 0; cursor < len(d.rows); b++ {
		curKs, curKsL := ks, ksL
		if b > 0 { // fresh nonces + a fresh homomorphic keystream for every batch
			nn, cc, kss := cnpClientKS(t, x)
			cr, _, _ := cnpCrypt(t, x, nn, cc)
			fresh := cr()
			curKs = kss
			curKsL = make([]*Ciphertext, outputs)
			for k := 0; k < outputs; k++ {
				curKsL[k] = slotKeystreamAtLevel(x, fresh[k], evalLevel)
			}
			cp(fmt.Sprintf("after_crypt_batch_%d", b))
		}
		cryptCalls++
		for gi, g := range groups {
			if cursor >= len(d.rows) {
				break
			}
			laneRows := slotRowsFor(d, g, cursor)
			sy := make([][]uint64, g.G)
			for c := range sy {
				sy[c] = slotClientSym(x, g, c, laneRows, curKs)
			}
			var st slotTimes
			ct := m.preMask(gi, sy, curKsL, &st, nil)
			x.fvEval.Add(ct, slotMaskFV(x, slotMaskSlots(x, g, d.biasQ, true)), ct)
			vals := slotBankRead(x, ct, g)
			for l := 0; l < g.Lanes; l++ {
				r := cursor + l
				if r >= len(d.rows) {
					break
				}
				want := cnpSim(d.xq[d.rows[r]], d.wq, d.biasQ, d.t)
				diff := vals[l] - want
				if diff != 0 {
					mismatches++
				}
				a := diff
				if a < 0 {
					a = -a
				}
				if a > maxAbsInt {
					maxAbsInt = a
				}
				sumAbs += float64(a)
				decRows = append(decRows, int32(d.rows[r]))
				decVals = append(decVals, vals[l])
			}
			cursor += g.Lanes
			if cipher == "hera" && mismatches > 0 {
				out.Correctness["status"] = "STOPPED: HERA must be bit-exact"
				out.Correctness["mismatching_rows_so_far"] = mismatches
				out.Correctness["first_bad_batch_group"] = []int{b, gi}
				write()
				t.Fatalf("HERA bit-exactness violated: %d rows differ from the integer simulation (batch %d, group %d)", mismatches, b, gi)
			}
		}
	}
	nRows := len(decVals)
	out.CryptCalls = cryptCalls
	out.Correctness["rows"] = nRows
	out.Correctness["rows_not_equal_to_integer_simulation"] = mismatches
	out.Correctness["bit_exact"] = mismatches == 0
	out.Correctness["max_abs_integer_diff"] = maxAbsInt
	out.Correctness["mean_abs_integer_diff"] = sumAbs / math.Max(1, float64(nRows))
	out.Correctness["integer_to_logit"] = "logit = integer / 2^14 (bias included server-side)"
	if len(d.plain) > 0 {
		var maxE, sumE, qmax float64
		for i, v := range decVals {
			e := math.Abs(float64(v)/16384 - d.plain[decRows[i]])
			sumE += e
			if e > maxE {
				maxE = e
			}
			w := cnpSim(d.xq[decRows[i]], d.wq, d.biasQ, d.t)
			if q := math.Abs(float64(w)/16384 - d.plain[decRows[i]]); q > qmax {
				qmax = q
			}
		}
		out.Correctness["max_abs_logit_error_vs_float_plaintext"] = maxE
		out.Correctness["mean_abs_logit_error_vs_float_plaintext"] = sumE / float64(nRows)
		out.Correctness["max_abs_quantisation_error_integer_sim_vs_float (no HE)"] = qmax
	}
	{
		rb := make([]byte, 4*len(decRows))
		vb := make([]byte, 8*len(decVals))
		for i := range decVals {
			binary.LittleEndian.PutUint32(rb[4*i:], uint32(decRows[i]))
			binary.LittleEndian.PutUint64(vb[8*i:], uint64(decVals[i]))
		}
		_ = os.MkdirAll(resultsDir, 0755)
		_ = ioutil.WriteFile(filepath.Join(resultsDir, "slot_"+cipher+"_rows.i32"), rb, 0644)
		_ = ioutil.WriteFile(filepath.Join(resultsDir, "slot_"+cipher+"_dec.i64"), vb, 0644)
	}
	cp("after_correctness")
	write()
	t.Logf("slot %s LogN=%d: %d lanes per Crypt in %d output cts; online %.3f ms per batch, offline %.3f ms; %s; %s",
		cipher, x.params.LogN(), out.LanesPerCrypt, out.OutPerCrypt, onB, offB, out.Gate, out.RowSet)
}

// ---- layout probe -------------------------------------------------------------------------------------

// TestSlotProbeLayout checks the design's source-derived claims by running them:
// (a) decode(ct[k])[i] = keystream[nonce i][element k] after Crypt, before any StC;
// (b) a one-hot in element i of the upload shows up only at slot i after encode + scale-up + Sub;
// (c) RotateColumns(k) is a left rotation by k inside each row.
//
//	E2E_RUN=1 E2E_CIPHER=hera|rubato E2E_LOGN=10 go test -run '^TestSlotProbeLayout$' -v
func TestSlotProbeLayout(t *testing.T) {
	if os.Getenv("E2E_RUN") != "1" {
		t.Skip("set E2E_RUN=1 to run")
	}
	cipher := cnpEnvStr("E2E_CIPHER", "hera")
	logN := cnpEnvInt("E2E_LOGN", 10)
	groups, err := slotLayout(cipher, 1<<uint(logN))
	if err != nil {
		t.Fatal(err)
	}
	x := slotSetup(t, cipher, logN, groups)
	N, tmod := x.params.N(), x.params.PlainModulus()
	nonces, counter, ks := cnpClientKS(t, x)
	crypt, _, outputs := cnpCrypt(t, x, nonces, counter)
	all := crypt()
	tol := int64(0)
	if cipher == "rubato" {
		tol = 9 // the client keystream carries the cipher's added noise, |d| <= 9 (rubato.go:345)
	}
	rep := map[string]interface{}{"cipher": cipher, "log_n": logN, "n": N, "t": tmod,
		"level_after_crypt": all[0].Level(), "usable_outputs": outputs, "q_count": x.params.QiCount(),
		"t_minus_1_mod_2N": (tmod - 1) % uint64(2*N)}
	mism := 0
	for _, k := range []int{0, outputs / 2, outputs - 1} {
		dec := slotDecode(x, all[k])
		for i := 0; i < N; i++ {
			if e := cnpCentre(int64(dec[i])-int64(ks[i][k]), int64(tmod)); e > tol || e < -tol {
				mism++
			}
		}
	}
	rep["slots_not_matching_keystream"] = mism
	if mism != 0 {
		t.Errorf("%d slots do not hold keystream[nonce i][element k]", mism)
	}

	level := 1
	ks0 := slotKeystreamAtLevel(x, all[0], level)
	sc := newSlotScaler(x, level)
	subDecode := func(hot int) []uint64 {
		sym := make([]uint64, N)
		for i := range sym {
			sym[i] = ks[i][0]
		}
		if hot >= 0 {
			sym[hot] = (sym[hot] + 1) % tmod
		}
		ptRt := NewPlaintextRingT(x.params)
		x.fvEncoder.EncodeUintRingT(sym, ptRt)
		ct := NewCiphertextFVLvl(x.params, 1, level)
		ct.Value()[0] = sc.scale(ptRt.Value()[0].Coeffs[0]).Value()[0]
		x.fvEval.Sub(ct, ks0, ct)
		return slotDecode(x, ct)
	}
	base := subDecode(-1)
	if cipher == "hera" {
		for i, v := range base {
			if v != 0 {
				t.Fatalf("HERA: Sub residual nonzero at slot %d (%d)", i, v)
			}
		}
	}
	oneHotBad := 0
	for _, i := range []int{0, 1, 5, N/2 - 1, N / 2, N - 1} {
		d := subDecode(i)
		for p := range d {
			want := uint64(0)
			if p == i {
				want = 1
			}
			if (d[p]+tmod-base[p])%tmod != want {
				oneHotBad++
			}
		}
	}
	rep["one_hot_wrong_slots"] = oneHotBad
	if oneHotBad != 0 {
		t.Errorf("one-hot probe: %d slots wrong", oneHotBad)
	}

	vals := make([]uint64, N)
	for i := range vals {
		vals[i] = uint64(i + 1)
	}
	ptRt := NewPlaintextRingT(x.params)
	x.fvEncoder.EncodeUintRingT(vals, ptRt)
	ct := NewCiphertextFVLvl(x.params, 1, 0)
	ct.Value()[0] = newSlotScaler(x, 0).scale(ptRt.Value()[0].Coeffs[0]).Value()[0]
	rotBad := 0
	for _, step := range slotRotationKeys(groups) {
		out := NewCiphertextFVLvl(x.params, 1, 0)
		x.fvEval.RotateColumns(ct, step, out)
		dec := slotDecode(x, out)
		for i := 0; i < N/2; i++ {
			if dec[i] != vals[(i+step)%(N/2)] || dec[N/2+i] != vals[N/2+(i+step)%(N/2)] {
				rotBad++
			}
		}
	}
	rep["rotation_wrong_slots"] = rotBad
	if rotBad != 0 {
		t.Errorf("rotation probe: %d slots wrong", rotBad)
	}
	b, _ := json.MarshalIndent(rep, "", "  ")
	t.Logf("probe result:\n%s", b)
	if dir := os.Getenv("RESULTS_DIR"); dir != "" {
		_ = os.MkdirAll(dir, 0755)
		_ = ioutil.WriteFile(filepath.Join(dir, "slot_probe_"+cipher+".json"), b, 0644)
	}
}
