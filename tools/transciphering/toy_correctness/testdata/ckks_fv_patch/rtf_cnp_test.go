package ckks_fv

// CNP-B: Coefficient-Native Packing on the RtF path. Online server: scale-up + Sub -> FV
// plaintext multiply by w_rev(X) -> add a fresh mask -> return the FV ciphertext.
// No HalfBoot, no CKKS. Staged into ckks_fv/ by third_party/fetch_rtf.sh.
//
// Layout (docs/cnp_gate.md): lane k, feature j -> coefficient 256k+j; w_rev(X)=sum wq_j X^(255-j);
// lane k's output is coefficient 256k+255. lanes = N/256 (256 at LogN 16).
// Precision pair (log2 dx, log2 dw) = (8,6); the bank divides by 2^14. bias_q = round(bias*2^14) is
// folded into the mask at every 256k+255; every other mask coefficient is uniform mod t.
//
// Index mapping (probe, rtf_cnp_probe_test.go): after StC^FV, symmetric element i is FV coefficient
// bitrev_LogN(i). The CLIENT therefore puts feature coefficient c at symmetric index bitrev(c) (it
// computes the keystream for nonce bitrev(c)); the server applies no permutation.
//
// Gated: E2E_RUN=1. Env: E2E_CIPHER=hera|rubato, E2E_LOGN (16), E2E_REPS (5), CNP_ARMS="cnp baseline"
// (default both; "cnp" alone gives a CNP-only process), CNP_DATA_DIR (scripts/cnp_b/prepare_inputs.py
// output; unset = seeded synthetic data), CNP_ROWS=auto|all|strat|<n>, CNP_BUDGET_S (7200: auto picks
// "all" only if its estimate fits), CNP_STC_MODE=lowest|keep (lowest = RtF's ModSwitchMany to level 0),
// CNP_HEADROOM_GATE_BITS (5), RESULTS_DIR, E2E_OUT.

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"math"
	"math/bits"
	mrand "math/rand"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/ldsec/lattigo/v2/utils"
)

const (
	cnpFeatures = 256
	cnpLogDx    = 8
	cnpLogDw    = 6
)

// ---- integer model (the plaintext simulation the bank must match) ----------------------------------

type cnpData struct {
	xq      [][]int16 // rows x 256
	wq      []int64   // 256
	biasQ   int64
	y       []uint8
	plain   []float64 // X@w+b
	rows    []int     // global row ids used
	expect  []int64   // integer simulation per used row
	t       int64
	source  string
	pyCheck string
}

func cnpCentre(v, t int64) int64 {
	v %= t
	if v < 0 {
		v += t
	}
	if v > t/2 {
		v -= t
	}
	return v
}

func cnpSim(xq []int16, wq []int64, biasQ, t int64) int64 {
	var s int64
	for j := range wq {
		s += int64(xq[j]) * wq[j]
	}
	return cnpCentre(s+biasQ, t)
}

func cnpReadI16(p string) []int16 {
	b, err := ioutil.ReadFile(p)
	if err != nil {
		panic(err)
	}
	o := make([]int16, len(b)/2)
	for i := range o {
		o[i] = int16(binary.LittleEndian.Uint16(b[2*i:]))
	}
	return o
}

func cnpLoadData(t *testing.T, dir string, nSynth int, tmod uint64) *cnpData {
	d := &cnpData{t: int64(tmod)}
	if dir == "" {
		rng := mrand.New(mrand.NewSource(20260617))
		d.source = "synthetic (seeded): xq in [-256,256], wq in [-64,64], bias_q=-137080"
		d.wq = make([]int64, cnpFeatures)
		for j := range d.wq {
			d.wq[j] = int64(rng.Intn(129) - 64)
		}
		d.biasQ = -137080
		for r := 0; r < nSynth; r++ {
			row := make([]int16, cnpFeatures)
			for j := range row {
				row[j] = int16(rng.Intn(513) - 256)
			}
			d.xq = append(d.xq, row)
			d.rows = append(d.rows, r)
		}
	} else {
		var meta struct {
			N      int     `json:"n"`
			T      int64   `json:"t"`
			BiasQ  int64   `json:"bias_q"`
			Bias   float64 `json:"bias"`
			Frauds int     `json:"frauds"`
		}
		b, err := ioutil.ReadFile(filepath.Join(dir, "meta.json"))
		if err != nil {
			t.Fatalf("meta.json: %v", err)
		}
		_ = json.Unmarshal(b, &meta)
		if meta.T != d.t {
			t.Fatalf("meta t=%d, params t=%d", meta.T, d.t)
		}
		d.source = "real test set from " + dir
		for r := 0; r < meta.N; r++ {
			d.rows = append(d.rows, r)
		}
		flat := cnpReadI16(filepath.Join(dir, "xq.i16"))
		for r := 0; r < meta.N; r++ {
			d.xq = append(d.xq, flat[r*cnpFeatures:(r+1)*cnpFeatures])
		}
		for _, v := range cnpReadI16(filepath.Join(dir, "wq.i16")) {
			d.wq = append(d.wq, int64(v))
		}
		d.biasQ = meta.BiasQ
		d.y, _ = ioutil.ReadFile(filepath.Join(dir, "y.u8"))
		pb, _ := ioutil.ReadFile(filepath.Join(dir, "plain_logit.f64"))
		for i := 0; i < len(pb)/8; i++ {
			d.plain = append(d.plain, math.Float64frombits(binary.LittleEndian.Uint64(pb[8*i:])))
		}
		// the Python integer simulation and this Go one must agree on every row
		eb, _ := ioutil.ReadFile(filepath.Join(dir, "expected.i64"))
		bad := 0
		for r := 0; r < meta.N; r++ {
			if int64(binary.LittleEndian.Uint64(eb[8*r:])) != cnpSim(d.xq[r], d.wq, d.biasQ, d.t) {
				bad++
			}
		}
		d.pyCheck = fmt.Sprintf("Go integer sim vs expected.i64 (numpy): %d mismatching rows of %d", bad, meta.N)
		if bad != 0 {
			t.Fatalf("%s", d.pyCheck)
		}
	}
	return d
}

// ---- weights polynomial, with the signed lift -----------------------------------------------------

// cnpWRev returns w_rev(X) = sum_j wq_j X^(255-j) as a length-N integer vector (signed, |.| <= 2^6).
func cnpWRev(wq []int64, n int) []int64 {
	v := make([]int64, n)
	for j, w := range wq {
		v[cnpFeatures-1-j] = w
	}
	return v
}

// cnpPtMulSigned builds the NTT+Montgomery plaintext for Mul from the SIGNED integer polynomial:
// limb i holds (w mod q_i), i.e. a small-norm element of R_q. Decryption only needs the result mod t,
// and the noise grows with ||w||_1 (~2^11.5) instead of ~t*256.
func cnpPtMulSigned(x *cnpCtx, level int, w []int64) *PlaintextMul {
	rq := x.ringAt(level)
	pt := NewPlaintextMulLvl(x.params, level)
	for i := 0; i <= level; i++ {
		q := int64(rq.Modulus[i])
		row := pt.Value()[0].Coeffs[i]
		for c, v := range w {
			if v < 0 {
				row[c] = uint64(q + v)
			} else {
				row[c] = uint64(v)
			}
		}
	}
	rq.NTTLazyLvl(level, pt.Value()[0], pt.Value()[0])
	rq.MFormLvl(level, pt.Value()[0], pt.Value()[0])
	return pt
}

// cnpPtMulUnsignedLift is the fork's own path: coefficients lifted to [0,t) (negative w -> t-|w|)
// then RingTToMul. Same result mod t, ~2^18 x more noise; kept only as the documented negative control.
func cnpPtMulUnsignedLift(x *cnpCtx, level int, w []int64) *PlaintextMul {
	t := int64(x.params.PlainModulus())
	ptRt := NewPlaintextRingT(x.params)
	for c, v := range w {
		ptRt.Value()[0].Coeffs[0][c] = uint64(((v % t) + t) % t)
	}
	pt := NewPlaintextMulLvl(x.params, level)
	x.fvEncoder.RingTToMul(ptRt, pt)
	return pt
}

// ---- mask ------------------------------------------------------------------------------------------

// cnpUniformModT fills dst with uniform values in [0,t) from crypto/rand by rejection sampling on
// 32-bit draws (accept r < floor(2^32/t)*t, so no modulo bias).
func cnpUniformModT(dst []uint64, t uint64) {
	limit := (uint64(1) << 32) / t * t
	buf := make([]byte, 4*len(dst))
	i := 0
	for i < len(dst) {
		if _, err := rand.Read(buf); err != nil {
			panic(err)
		}
		for k := 0; k+4 <= len(buf) && i < len(dst); k += 4 {
			if r := uint64(binary.LittleEndian.Uint32(buf[k:])); r < limit {
				dst[i] = r % t
				i++
			}
		}
		buf = buf[:4*(len(dst)-i)]
		if len(buf) == 0 {
			break
		}
	}
}

// cnpMaskRingT is the mask in R_t coefficient form: uniform mod t everywhere except 256k+255, which
// holds round(bias*2^14) mod t. randomize=false gives the all-zero mask (test-only: raw product).
func cnpMaskRingT(x *cnpCtx, biasQ int64, lanes int, randomize bool) *PlaintextRingT {
	t := x.params.PlainModulus()
	ptRt := NewPlaintextRingT(x.params)
	c := ptRt.Value()[0].Coeffs[0]
	if randomize {
		cnpUniformModT(c, t)
	}
	for k := 0; k < lanes; k++ {
		c[cnpFeatures*k+cnpFeatures-1] = uint64(((biasQ % int64(t)) + int64(t)) % int64(t))
	}
	return ptRt
}

// cnpMaskFV FV-encodes the mask: FVScaleUp multiplies by Q_l/t per limb at the ciphertext's level.
// A raw coefficient add would be wrong (cnpRawMaskPlaintext is the negative control for it).
func cnpMaskFV(x *cnpCtx, level int, biasQ int64, lanes int, randomize bool) *Plaintext {
	ptRt := cnpMaskRingT(x, biasQ, lanes, randomize)
	pt := NewPlaintextFVLvl(x.params, level)
	x.fvEncoder.FVScaleUp(ptRt, pt)
	return pt
}

func cnpRawMaskPlaintext(x *cnpCtx, level int, biasQ int64, lanes int) *Plaintext {
	ptRt := cnpMaskRingT(x, biasQ, lanes, false)
	pt := NewPlaintextFVLvl(x.params, level)
	for i := 0; i <= level; i++ {
		copy(pt.Value()[0].Coeffs[i], ptRt.Value()[0].Coeffs[0]) // NOT scaled by Q/t
	}
	return pt
}

// ---- client, online server, bank -------------------------------------------------------------------

// cnpClientSym is the CLIENT's upload for output ct kk: RtF's symmetric ciphertext, one element per
// coefficient, each (xq_c + keystream[bitrev(c)][kk]) mod t (~25 bits). Nothing is scaled up here.
func cnpClientSym(x *cnpCtx, data []int64, ks [][]uint64, kk int) []uint64 {
	t := int64(x.params.PlainModulus())
	logn := uint64(x.params.LogN())
	sym := make([]uint64, len(data))
	for i := range sym {
		v := (data[i] + int64(ks[utils.BitReverse64(uint64(i), logn)][kk])) % t
		if v < 0 {
			v += t
		}
		sym[i] = uint64(v)
	}
	return sym
}

// cnpScaleUp is the SERVER's public, keyless scale-up of the uploaded elements into R_q at `level`.
// CNP_SCALEUP=exact (default, level 0): limb = round(m*q0/t), computed with 128-bit arithmetic. For
// full-range m (the keystream makes every coefficient uniform mod t) FVScaleUp's m*floor(q0/t) costs
// ~2^20 of noise at level 0, which after pmul left 4.75 bits of headroom at LogN 10.
// CNP_SCALEUP=fvscaleup: the fork's FVScaleUp (any level; use with CNP_STC_MODE=keep).
func cnpScaleUp(x *cnpCtx, level int, sym []uint64) *Plaintext {
	t := x.params.PlainModulus()
	pt := NewPlaintextFVLvl(x.params, level)
	if cnpEnvStr("CNP_SCALEUP", "exact") == "exact" {
		if level != 0 {
			panic("CNP_SCALEUP=exact is implemented for level 0 only (use CNP_SCALEUP=fvscaleup with CNP_STC_MODE=keep)")
		}
		q0 := x.params.Qi()[0]
		row := pt.Value()[0].Coeffs[0]
		for i, v := range sym {
			hi, lo := bits.Mul64(v, q0)
			lo, carry := bits.Add64(lo, t/2, 0)
			quo, _ := bits.Div64(hi+carry, lo, t)
			row[i] = quo % q0
		}
		return pt
	}
	ptRt := NewPlaintextRingT(x.params)
	copy(ptRt.Value()[0].Coeffs[0], sym)
	x.fvEncoder.FVScaleUp(ptRt, pt)
	return pt
}

// cnpSubStage is the online "scale-up + Sub" step: server scale-up of the uploaded symmetric
// elements, then ciphertext(0, scaled) - keystream ciphertext.
func cnpSubStage(x *cnpCtx, sym []uint64, ks0 *Ciphertext) *Ciphertext {
	pt := cnpScaleUp(x, ks0.Level(), sym)
	ct := NewCiphertextFVLvl(x.params, 1, ks0.Level())
	ct.Value()[0] = pt.Value()[0].CopyNew()
	x.fvEval.Sub(ct, ks0, ct)
	return ct
}

func cnpPmulStage(x *cnpCtx, ct *Ciphertext, ptMul *PlaintextMul) *Ciphertext {
	out := NewCiphertextFVLvl(x.params, 1, ct.Level())
	x.fvEval.Mul(ct, ptMul, out)
	return out
}

// bank: decrypt, centre mod t, read 256k+255.
func cnpBankRead(x *cnpCtx, ct *Ciphertext, lanes int) []int64 {
	coeffs, _ := cnpDecryptRingT(x, ct)
	t := int64(x.params.PlainModulus())
	out := make([]int64, lanes)
	for k := range out {
		out[k] = cnpCentre(int64(coeffs[cnpFeatures*k+cnpFeatures-1]), t)
	}
	return out
}

func cnpDataVec(d *cnpData, n, lanes, firstRow int) []int64 {
	v := make([]int64, n)
	for k := 0; k < lanes; k++ {
		r := firstRow + k
		if r >= len(d.rows) {
			break
		}
		for j := 0; j < cnpFeatures; j++ {
			v[cnpFeatures*k+j] = int64(d.xq[d.rows[r]][j])
		}
	}
	return v
}

// ---- statistics ------------------------------------------------------------------------------------

func cnpChiSquare(vals []uint64, t uint64, bins int) (stat float64, df int, p float64) {
	cnt := make([]float64, bins)
	for _, v := range vals {
		cnt[int(v*uint64(bins)/t)]++
	}
	// bin widths are equal to within 1/t; expected counts use the exact bin masses
	var total float64
	exp := make([]float64, bins)
	for b := 0; b < bins; b++ {
		lo := (uint64(b)*t + uint64(bins) - 1) / uint64(bins)
		hi := (uint64(b+1)*t + uint64(bins) - 1) / uint64(bins)
		exp[b] = float64(hi-lo) / float64(t) * float64(len(vals))
		total += cnt[b]
	}
	for b := 0; b < bins; b++ {
		stat += (cnt[b] - exp[b]) * (cnt[b] - exp[b]) / exp[b]
	}
	df = bins - 1
	return stat, df, cnpGammaQ(float64(df)/2, stat/2)
}

// cnpGammaQ is the regularised upper incomplete gamma Q(a,x) (series / continued fraction).
func cnpGammaQ(a, x float64) float64 {
	if x <= 0 {
		return 1
	}
	lg, _ := math.Lgamma(a)
	if x < a+1 {
		sum, term, ap := 1/a, 1/a, a
		for n := 0; n < 1000; n++ {
			ap++
			term *= x / ap
			sum += term
			if math.Abs(term) < math.Abs(sum)*1e-15 {
				break
			}
		}
		return 1 - sum*math.Exp(-x+a*math.Log(x)-lg)
	}
	b := x + 1 - a
	c := 1 / 1e-300
	d := 1 / b
	h := d
	for i := 1; i < 1000; i++ {
		an := -float64(i) * (float64(i) - a)
		b += 2
		d = an*d + b
		if math.Abs(d) < 1e-300 {
			d = 1e-300
		}
		c = b + an/c
		if math.Abs(c) < 1e-300 {
			c = 1e-300
		}
		d = 1 / d
		del := d * c
		h *= del
		if math.Abs(del-1) < 1e-15 {
			break
		}
	}
	return math.Exp(-x+a*math.Log(x)-lg) * h
}

func cnpSummarise(runs []float64) *e2eStage { return e2eSummarise(runs) }

// ---- output ----------------------------------------------------------------------------------------

type cnpCheckpoint struct {
	Name         string `json:"name"`
	VmHWMKB      int64  `json:"vm_hwm_kb"`
	TotalAllocKB int64  `json:"total_alloc_kb"`
	HeapAllocKB  int64  `json:"heap_alloc_kb"`
}

type cnpNoisePoint struct {
	Point        string  `json:"point"`
	Level        int     `json:"level"`
	NoiseBits    float64 `json:"log2_max_noise"`
	HeadroomBits float64 `json:"headroom_bits_to_Q_over_2t"`
}

type cnpOut struct {
	DateUTC       string                 `json:"date_utc"`
	Cipher        string                 `json:"cipher"`
	ParamSet      string                 `json:"param_set"`
	LogN          int                    `json:"log_n"`
	N             int                    `json:"n"`
	Lanes         int                    `json:"lanes_per_output_ct"`
	T             uint64                 `json:"plain_modulus"`
	Q0Bits        float64                `json:"q0_bits"`
	StCMode       string                 `json:"stc_modswitch_mode"`
	ScaleUp       string                 `json:"scale_up_server_side"`
	Wire          map[string]interface{} `json:"wire_bytes"`
	Level         int                    `json:"fv_level_at_pmul"`
	Arms          string                 `json:"arms"`
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
	RotKeys       map[string]int         `json:"rotation_key_counts"`
	Online        map[string]*e2eStage   `json:"cnp_stages"`
	Baseline      map[string]*e2eStage   `json:"baseline_halfboot_stages,omitempty"`
	PerOutputMs   map[string]float64     `json:"per_output_ct_ms"`
	PerTxnMs      map[string]float64     `json:"per_transaction_ms"`
	BaselineLB    map[string]float64     `json:"baseline_lower_bound_per_output_ct_ms,omitempty"`
	BaselineLBTxn map[string]float64     `json:"baseline_lower_bound_per_transaction_ms,omitempty"`
	BaselineNote  string                 `json:"baseline_note,omitempty"`
	OfflineNote   string                 `json:"offline_note"`
	Noise         []cnpNoisePoint        `json:"noise_headroom"`
	GateBits      float64                `json:"headroom_gate_bits"`
	Gate          string                 `json:"headroom_gate"`
	Checkpoints   []cnpCheckpoint        `json:"memory_checkpoints"`
	Correctness   map[string]interface{} `json:"correctness"`
	MaskTests     map[string]interface{} `json:"mask_tests,omitempty"`
	BlockOutputs  int                    `json:"outputs_per_crypt"`
	CryptCalls    int                    `json:"crypt_calls"`
	OutputCts     int                    `json:"output_cts_processed"`
	AUCSet        string                 `json:"row_set"`
	AUCEstimateS  float64                `json:"row_set_estimate_s"`
	VmHWMKBEnd    int64                  `json:"vm_hwm_kb_end"`
	TotalAllocKB  int64                  `json:"total_alloc_kb_end"`
}

func cnpLoad() string {
	b, _ := ioutil.ReadFile("/proc/loadavg")
	return strings.TrimSpace(string(b))
}

func cnpEnvStr(n, def string) string {
	if v := os.Getenv(n); v != "" {
		return v
	}
	return def
}

// ---- the run ---------------------------------------------------------------------------------------

func TestCNPRun(t *testing.T) {
	if os.Getenv("E2E_RUN") != "1" {
		t.Skip("set E2E_RUN=1 to run (full scale allocates tens of GB)")
	}
	cipher := cnpEnvStr("E2E_CIPHER", "hera")
	logN := cnpEnvInt("E2E_LOGN", 16)
	reps := cnpEnvInt("E2E_REPS", 5)
	arms := cnpEnvStr("CNP_ARMS", "cnp baseline")
	stcMode := cnpEnvStr("CNP_STC_MODE", "lowest")
	gateBits := float64(cnpEnvInt("CNP_HEADROOM_GATE_BITS", 5))
	resultsDir := cnpEnvStr("RESULTS_DIR", "/results")
	outPath := cnpEnvStr("E2E_OUT", filepath.Join(resultsDir, "cnp_"+cipher+".json"))
	wantBaseline := strings.Contains(arms, "baseline")

	out := &cnpOut{DateUTC: time.Now().UTC().Format(time.RFC3339), Cipher: cipher, Arms: arms, Reps: reps,
		StCMode: stcMode, ScaleUp: cnpEnvStr("CNP_SCALEUP", "exact"), GateBits: gateBits, LoadAvgStart: cnpLoad(), GOMAXPROCS: runtime.GOMAXPROCS(0),
		GOGC: os.Getenv("GOGC"), GOMEMLIMIT: os.Getenv("GOMEMLIMIT"),
		Online: map[string]*e2eStage{}, Correctness: map[string]interface{}{}, RotKeys: map[string]int{},
		Seeding: "Unseeded: HE keys, nonces, Rubato counter and noise, and the masks come from crypto/rand each run. Symmetric key is fixed (1..blocksize). Synthetic data (if used) is seeded.",
		Provenance: map[string]string{"image_ref": os.Getenv("PPFDAAS_IMAGE_REF"), "repo_sha": os.Getenv("PPFDAAS_REPO_SHA"),
			"rtf_sha": os.Getenv("PPFDAAS_RTF_SHA"), "rtf_patch_sha256": os.Getenv("PPFDAAS_PATCH_SHA256"),
			"build_date": os.Getenv("PPFDAAS_BUILD_DATE")},
		OfflineNote: "Offline (keystream_crypt, stc_modswitch) depends only on nonces, not on client data; identical code path to the HalfBoot e2e rows (rtf_e2e_test.go). Online = client-dependent stages only.",
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

	// ---- setup (FV only) ----
	x := cnpSetup(t, cipher, logN)
	out.SetupMs, out.SetupHeapKB = x.setupMs, x.heapKB // maps: later steps (weights, HalfBoot) appear too
	cp("after_fv_setup")
	N, tmod := x.params.N(), x.params.PlainModulus()
	lanes := N / cnpFeatures
	out.ParamSet, out.LogN, out.N, out.Lanes, out.T = x.cfg.paramSet, x.params.LogN(), N, lanes, tmod
	out.Q0Bits = math.Log2(float64(x.params.Qi()[0]))
	out.RotKeys["stc_only"] = len(x.rotkeys.Keys)

	// ---- data ----
	synthRows := lanes * 3
	d := cnpLoadData(t, os.Getenv("CNP_DATA_DIR"), synthRows, tmod)
	out.DataSource, out.DataCheck = d.source, d.pyCheck

	// ---- server model: weights polynomial (offline, once) ----
	nonces, counter, ks := cnpClientKS(t, x)
	crypt, reset, outputs := cnpCrypt(t, x, nonces, counter)
	out.BlockOutputs = outputs
	cp("after_cipher_init_and_enckey")

	// ---- offline phase, REPS times (as rtf_e2e_test.go) ----
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
		out.Online["keystream_crypt (offline)"] = cnpSummarise(runs)
	}
	cp("after_crypt")
	lowest := stcMode != "keep"
	var ks0 *Ciphertext
	{
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			in := all[0].CopyNew().Ciphertext()
			t0 := time.Now()
			ks0 = cnpStCMode(x, in, lowest)
			runs = append(runs, cnpMs(time.Since(t0)))
		}
		out.Online["stc_modswitch (offline)"] = cnpSummarise(runs)
	}
	level := ks0.Level()
	out.Level = level
	cp("after_stc")
	{
		bpe := bits.Len64(tmod - 1) // bits per symmetric element
		limbs := level + 1
		out.Wire = map[string]interface{}{
			"upload":                  "RtF symmetric ciphertext: N elements mod t (client does NO scale-up; the server does, inside scaleup_sub)",
			"upload_bits_per_element": bpe, "upload_bytes_per_output_ct_bitpacked": (N*bpe + 7) / 8,
			"upload_bytes_per_output_ct_uint32_wire":   4 * N,
			"upload_bytes_per_transaction_bitpacked":   float64((N*bpe+7)/8) / float64(lanes),
			"upload_bytes_per_transaction_uint32_wire": float64(4*N) / float64(lanes),
			"download":       "FV ciphertext (2 polys x (level+1) limbs x N); no compression",
			"download_limbs": limbs, "download_bytes_per_output_ct_uint64_limbs": 2 * limbs * N * 8,
			"download_bytes_per_output_ct_60bit_packed":   2 * limbs * N * 60 / 8,
			"download_bytes_per_transaction_uint64_limbs": float64(2*limbs*N*8) / float64(lanes),
			"download_bytes_per_transaction_60bit_packed": float64(2*limbs*N*60/8) / float64(lanes),
			"note": "computed from the object layout (the fork has no ciphertext marshaller); transactions per ct = lanes",
		}
	}

	var ptMul *PlaintextMul
	x.timedMem("encode_weights_ptmul_signed_lift", func() { ptMul = cnpPtMulSigned(x, level, cnpWRev(d.wq, N)) })

	// ---- first output: noise headroom at each point + gate (BEFORE any more work) ----
	data0 := cnpDataVec(d, N, lanes, 0)
	sym0 := cnpClientSym(x, data0, ks, 0)
	noiseAt := func(point string, ct *Ciphertext) float64 {
		raw := x.fvDec.DecryptNew(ct)
		nb, hb := cnpNoise(x, raw)
		out.Noise = append(out.Noise, cnpNoisePoint{point, ct.Level(), nb, hb})
		return hb
	}
	noiseAt("before_sub (keystream ct after StC)", ks0)
	ctSub := cnpSubStage(x, sym0, ks0)
	noiseAt("after_sub", ctSub)
	ctMul := cnpPmulStage(x, ctSub, ptMul)
	hbMul := noiseAt("after_pmul", ctMul)
	ctMask := ctMul.CopyNew().Ciphertext()
	x.fvEval.Add(ctMask, cnpMaskFV(x, level, d.biasQ, lanes, true), ctMask)
	noiseAt("after_mask_add", ctMask)
	if hbMul < gateBits {
		out.Gate = fmt.Sprintf("STOPPED: headroom after pmul %.2f bits < %.0f (level %d, mode %s). Nothing beyond output 0 was run.", hbMul, gateBits, level, stcMode)
		write()
		t.Fatalf("%s", out.Gate)
	}
	out.Gate = fmt.Sprintf("passed: headroom after pmul %.2f bits >= %.0f", hbMul, gateBits)
	cp("after_noise_checks")

	// ---- online stages, REPS times ----
	var subRuns, mulRuns, maskGenRuns, maskAddRuns, totRuns, bankRuns, clientRuns []float64
	for r := 0; r < reps; r++ {
		t0 := time.Now()
		cl := cnpClientSym(x, data0, ks, 0) // client work, reported separately, not in the server total
		clientRuns = append(clientRuns, cnpMs(time.Since(t0)))

		ts := time.Now()
		ct := cnpSubStage(x, cl, ks0)
		tSub := time.Since(ts)
		ts = time.Now()
		ct = cnpPmulStage(x, ct, ptMul)
		tMul := time.Since(ts)
		ts = time.Now()
		mk := cnpMaskRingT(x, d.biasQ, lanes, true)
		mpt := NewPlaintextFVLvl(x.params, level)
		tGen := time.Since(ts)
		ts = time.Now()
		x.fvEncoder.FVScaleUp(mk, mpt)
		x.fvEval.Add(ct, mpt, ct)
		tAdd := time.Since(ts)
		subRuns = append(subRuns, cnpMs(tSub))
		mulRuns = append(mulRuns, cnpMs(tMul))
		maskGenRuns = append(maskGenRuns, cnpMs(tGen))
		maskAddRuns = append(maskAddRuns, cnpMs(tAdd))
		totRuns = append(totRuns, cnpMs(tSub+tMul+tGen+tAdd))
		ts = time.Now()
		_ = cnpBankRead(x, ct, lanes)
		bankRuns = append(bankRuns, cnpMs(time.Since(ts)))
	}
	out.Online["scaleup_sub (server scale-up + Sub)"] = cnpSummarise(subRuns)
	out.Online["pmul"] = cnpSummarise(mulRuns)
	out.Online["mask_generate (CSPRNG, rejection sampling)"] = cnpSummarise(maskGenRuns)
	out.Online["mask_fv_encode_and_add"] = cnpSummarise(maskAddRuns)
	out.Online["online_total (scaleup+sub+pmul+mask)"] = cnpSummarise(totRuns)
	out.Online["client_build_symmetric_ct (not server)"] = cnpSummarise(clientRuns)
	out.Online["bank_decrypt_and_read (not server)"] = cnpSummarise(bankRuns)
	med := func(n string) float64 { return out.Online[n].Median }
	out.PerOutputMs = map[string]float64{"scaleup_sub": med("scaleup_sub (server scale-up + Sub)"), "pmul": med("pmul"),
		"mask": med("mask_generate (CSPRNG, rejection sampling)") + med("mask_fv_encode_and_add"), "total": med("online_total (scaleup+sub+pmul+mask)")}
	out.PerTxnMs = map[string]float64{}
	for k, v := range out.PerOutputMs {
		out.PerTxnMs[k] = v / float64(lanes)
	}
	out.PerOutputMs["offline_keystream_crypt_per_batch"] = med("keystream_crypt (offline)")
	out.PerOutputMs["offline_stc_modswitch"] = med("stc_modswitch (offline)")
	cp("after_online_stages")

	// ---- mask tests (adapted to mod t) on output 0 ----
	mt := map[string]interface{}{}
	out.MaskTests = mt
	{
		zero := cnpMaskFV(x, level, 0, lanes, false) // all-zero mask: raw product, no bias (test-only)
		unm := ctMul.CopyNew().Ciphertext()
		x.fvEval.Add(unm, zero, unm)
		uc, _ := cnpDecryptRingT(x, unm)
		mc, _ := cnpDecryptRingT(x, ctMask)
		bad := 0
		for k := 0; k < lanes; k++ {
			c := cnpFeatures*k + cnpFeatures - 1
			want := cnpCentre(int64(uc[c])+d.biasQ, int64(tmod))
			if cnpCentre(int64(mc[c]), int64(tmod)) != want {
				bad++
			}
		}
		mt["test5_masked_equals_unmasked_plus_bias_mismatching_lanes"] = bad
		// negative control: a raw (non-FV-scaled) mask add must be CAUGHT by the same check
		raw := ctMul.CopyNew().Ciphertext()
		x.fvEval.Add(raw, cnpRawMaskPlaintext(x, level, d.biasQ, lanes), raw)
		rc, _ := cnpDecryptRingT(x, raw)
		badRaw := 0
		for k := 0; k < lanes; k++ {
			c := cnpFeatures*k + cnpFeatures - 1
			if cnpCentre(int64(rc[c]), int64(tmod)) != cnpCentre(int64(uc[c])+d.biasQ, int64(tmod)) {
				badRaw++
			}
		}
		mt["test5_negative_control_raw_unscaled_mask_mismatching_lanes"] = badRaw
		var non, nonU []uint64
		for c := range mc {
			if c%cnpFeatures != cnpFeatures-1 {
				non = append(non, mc[c])
				nonU = append(nonU, uc[c])
			}
		}
		st, df, p := cnpChiSquare(non, tmod, 64)
		mt["chi_square_masked_nonoutput"] = map[string]interface{}{"stat": st, "df": df, "p": p, "samples": len(non)}
		st, df, p = cnpChiSquare(nonU, tmod, 64)
		mt["chi_square_unmasked_nonoutput"] = map[string]interface{}{"stat": st, "df": df, "p": p, "samples": len(nonU)}
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
		// weight recovery: query x = 1 at (lane 0, feature 0). The product is then w_rev itself,
		// so coefficient 255-j holds wq_j: an attacker reads the weights straight off the unmasked output.
		q := make([]int64, N)
		q[0] = 1
		zeroQ := make([]int64, N)
		run := func(data []int64, masked bool) []uint64 {
			prod := cnpPmulStage(x, cnpSubStage(x, cnpClientSym(x, data, ks, 0), ks0), ptMul)
			if masked {
				x.fvEval.Add(prod, cnpMaskFV(x, level, d.biasQ, lanes, true), prod)
			}
			c, _ := cnpDecryptRingT(x, prod)
			return c
		}
		// Linear in the data, and the keystream (incl. Rubato's added noise) is the same in both queries,
		// so (query - zero query) = w_rev exactly. Masked: two independent fresh masks, so the difference is uniform.
		u1, u0 := run(q, false), run(zeroQ, false)
		m1, m0 := run(q, true), run(zeroQ, true)
		okU, okM := 0, 0
		T := int64(tmod)
		for j := 0; j < cnpFeatures; j++ {
			if cnpCentre(int64(u1[cnpFeatures-1-j])-int64(u0[cnpFeatures-1-j]), T) == d.wq[j] {
				okU++
			}
			// j=0 is the lane's OUTPUT coefficient (255): both masks hold the same bias there, so it cancels
			// in the difference. That is the legitimate output, w_0 * x_0, not a leak; count j>=1 only.
			if j >= 1 && cnpCentre(int64(m1[cnpFeatures-1-j])-int64(m0[cnpFeatures-1-j]), T) == d.wq[j] {
				okM++
			}
		}
		mt["weight_recovery_unmasked_exact_of_256"] = okU
		mt["weight_recovery_masked_exact_of_255_nonoutput (chance ~ 255/t)"] = okM
		if okU != cnpFeatures {
			write()
			t.Fatalf("unmasked weight recovery got %d/256, expected 256", okU)
		}
		if okM != 0 {
			write()
			t.Fatalf("masked output still leaks weights: %d/255 exact", okM)
		}
	}

	// ---- correctness over the selected rows ----
	nRowsAvail := len(d.rows)
	rowsSel := make([]int, 0, nRowsAvail)
	rowMode := cnpEnvStr("CNP_ROWS", "auto")
	perOutExtra := med("client_build_symmetric_ct (not server)") + med("bank_decrypt_and_read (not server)")
	perOutAll := med("stc_modswitch (offline)") + out.PerOutputMs["total"] + perOutExtra
	if d.source[:4] == "real" {
		nTot := len(d.xq)
		ncts := (nTot + lanes - 1) / lanes
		ncrypt := (ncts + outputs - 1) / outputs
		est := (float64(ncrypt)*med("keystream_crypt (offline)") + float64(ncts)*perOutAll) / 1000
		out.AUCEstimateS = est
		budget := float64(cnpEnvInt("CNP_BUDGET_S", 7200))
		switch {
		case rowMode == "all" || (rowMode == "auto" && est <= budget):
			out.AUCSet = fmt.Sprintf("full test set (%d rows, %d output cts, %d Crypt calls); estimate %.0f s <= budget %.0f s", nTot, ncts, ncrypt, est, budget)
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
			out.AUCSet = fmt.Sprintf("STRATIFIED subset (%d rows: all frauds + seeded random non-frauds); full-set estimate %.0f s > budget %.0f s", len(rowsSel), est, budget)
		default:
			var n int
			fmt.Sscanf(rowMode, "%d", &n)
			for r := 0; r < n && r < nTot; r++ {
				rowsSel = append(rowsSel, r)
			}
			out.AUCSet = fmt.Sprintf("first %d rows (CNP_ROWS=%s)", len(rowsSel), rowMode)
		}
	} else {
		for r := 0; r < nRowsAvail; r++ {
			rowsSel = append(rowsSel, r)
		}
		out.AUCSet = fmt.Sprintf("synthetic %d rows", len(rowsSel))
	}
	// remap d.rows to the selected global rows
	sel := make([]int, len(rowsSel))
	for i, r := range rowsSel {
		if d.source[:4] == "real" {
			sel[i] = r
		} else {
			sel[i] = d.rows[r]
		}
	}
	d.rows = sel
	nCts := (len(d.rows) + lanes - 1) / lanes
	out.OutputCts = nCts
	t.Logf("row set: %s", out.AUCSet)

	decRows := make([]int32, 0, len(d.rows))
	decVals := make([]int64, 0, len(d.rows))
	mismatches, maxAbsInt := 0, int64(0)
	var sumAbs float64
	cryptCalls := 1
	curBatchKS := ks
	curAll := all
	for o := 0; o < nCts; o++ {
		b, kk := o/outputs, o%outputs
		if b+1 > cryptCalls { // new nonces + a fresh homomorphic keystream evaluation for each batch of outputs
			cryptCalls = b + 1
			nn, cc, kss := cnpClientKS(t, x)
			cr, _, _ := cnpCrypt(t, x, nn, cc)
			curAll, curBatchKS = cr(), kss
			cp(fmt.Sprintf("after_crypt_batch_%d", b))
		}
		ks0o := cnpStCMode(x, curAll[kk].CopyNew().Ciphertext(), lowest)
		clo := cnpClientSym(x, cnpDataVec(d, N, lanes, o*lanes), curBatchKS, kk)
		ct := cnpPmulStage(x, cnpSubStage(x, clo, ks0o), ptMul)
		x.fvEval.Add(ct, cnpMaskFV(x, level, d.biasQ, lanes, true), ct)
		vals := cnpBankRead(x, ct, lanes)
		for k := 0; k < lanes; k++ {
			r := o*lanes + k
			if r >= len(d.rows) {
				break
			}
			want := cnpSim(d.xq[d.rows[r]], d.wq, d.biasQ, d.t)
			diff := vals[k] - want
			if diff != 0 {
				mismatches++
			}
			if a := diff; a < 0 {
				a = -a
				if a > maxAbsInt {
					maxAbsInt = a
				}
			} else if a > maxAbsInt {
				maxAbsInt = a
			}
			sumAbs += math.Abs(float64(diff))
			decRows = append(decRows, int32(d.rows[r]))
			decVals = append(decVals, vals[k])
		}
		if cipher == "hera" && mismatches > 0 {
			out.Correctness["status"] = "STOPPED: HERA must be bit-exact"
			out.Correctness["mismatching_rows_so_far"] = mismatches
			out.Correctness["first_bad_output_ct"] = o
			write()
			t.Fatalf("HERA bit-exactness violated: %d rows differ from the integer simulation (output ct %d)", mismatches, o)
		}
	}
	nRows := len(decVals)
	out.CryptCalls = cryptCalls
	out.Correctness["rows"] = nRows
	out.Correctness["output_cts"] = nCts
	out.Correctness["rows_not_equal_to_integer_simulation"] = mismatches
	out.Correctness["bit_exact"] = mismatches == 0
	out.Correctness["max_abs_integer_diff"] = maxAbsInt
	out.Correctness["mean_abs_integer_diff"] = sumAbs / math.Max(1, float64(nRows))
	out.Correctness["integer_to_logit"] = "logit = integer / 2^14 (bias included server-side)"
	if len(d.plain) > 0 {
		var maxE, sumE float64
		for i, v := range decVals {
			e := math.Abs(float64(v)/float64(int64(1)<<(cnpLogDx+cnpLogDw)) - d.plain[decRows[i]])
			sumE += e
			if e > maxE {
				maxE = e
			}
		}
		out.Correctness["max_abs_logit_error_vs_float_plaintext"] = maxE
		out.Correctness["mean_abs_logit_error_vs_float_plaintext"] = sumE / float64(nRows)
		// plaintext quantisation error alone, for context (no HE)
		var qmax float64
		for i := range decVals {
			w := cnpSim(d.xq[decRows[i]], d.wq, d.biasQ, d.t)
			if e := math.Abs(float64(w)/16384 - d.plain[decRows[i]]); e > qmax {
				qmax = e
			}
		}
		out.Correctness["max_abs_quantisation_error_integer_sim_vs_float (no HE)"] = qmax
	}
	// dump decrypted integers so scripts/cnp_b/score.py can compute AUC against the labels
	{
		var rb, vb []byte
		for i := range decVals {
			rb = append(rb, byte(decRows[i]), byte(decRows[i]>>8), byte(decRows[i]>>16), byte(decRows[i]>>24))
			vb = append(vb, make([]byte, 8)...)
			binary.LittleEndian.PutUint64(vb[8*i:], uint64(decVals[i]))
		}
		_ = ioutil.WriteFile(filepath.Join(resultsDir, "cnp_"+cipher+"_rows.i32"), rb, 0644)
		_ = ioutil.WriteFile(filepath.Join(resultsDir, "cnp_"+cipher+"_dec.i64"), vb, 0644)
	}
	cp("after_correctness")
	write() // CNP results are on disk before the (memory-hungry) baseline arm starts

	// ---- baseline arm: Sub + HalfBoot + 2x+1 (LOWER BOUND for the HalfBoot path) ----
	if wantBaseline {
		out.Baseline = map[string]*e2eStage{}
		h := cnpBuildHalfBoot(t, x)
		out.RotKeys["baseline_halfboot_extra"] = h.hbKeys
		cp("after_halfboot_setup")
		hp := x.cfg.hbtpParams
		messageScaling := float64(tmod) / hp.MessageRatio
		ks0b := cnpStCMode(x, all[0].CopyNew().Ciphertext(), true) // RtF's level-0 ks for the baseline
		coeffs := make([]float64, N)
		nB := N
		data := make([]float64, nB)
		for i := range data {
			data[i] = utils.RandFloat64(-1, 1)
		}
		for i := 0; i < nB/2; i++ {
			j := utils.BitReverse64(uint64(i), uint64(x.params.LogN()-1))
			coeffs[j] = data[i]
			coeffs[j+uint64(N/2)] = data[i+nB/2]
		}
		plainRingT := h.ckksEnc.EncodeCoeffsRingTNew(coeffs, messageScaling)
		poly := plainRingT.Value()[0]
		for i := 0; i < nB; i++ {
			j := utils.BitReverse64(uint64(i), uint64(x.params.LogN()))
			poly.Coeffs[0][j] = (poly.Coeffs[0][j] + ks[i][0]) % tmod
		}
		ptB := NewPlaintextFVLvl(x.params, 0)
		x.fvEncoder.FVScaleUp(plainRingT, ptB)
		stage := func(name string, f func()) {
			runs := make([]float64, 0, reps)
			for r := 0; r < reps; r++ {
				t0 := time.Now()
				f()
				runs = append(runs, cnpMs(time.Since(t0)))
			}
			out.Baseline[name] = cnpSummarise(runs)
		}
		var prepared *Ciphertext
		stage("sub_ntt_setscale", func() {
			ct := NewCiphertextFVLvl(x.params, 1, 0)
			ct.Value()[0] = ptB.Value()[0].CopyNew()
			x.fvEval.Sub(ct, ks0b, ct)
			x.fvEval.TransformToNTT(ct, ct)
			ct.SetScale(math.Exp2(math.Round(math.Log2(float64(x.params.Qi()[0]) / float64(tmod) * messageScaling))))
			prepared = ct
		})
		var b0, b1 *Ciphertext
		runs := make([]float64, 0, reps)
		for r := 0; r < reps; r++ {
			in := prepared.CopyNew().Ciphertext()
			t0 := time.Now()
			b0, b1 = h.hbtp.HalfBoot(in, false)
			runs = append(runs, cnpMs(time.Since(t0)))
		}
		out.Baseline["halfboot"] = cnpSummarise(runs)
		var res []*Ciphertext
		stage("ckks_eval_2x_plus_1", func() {
			ins := []*Ciphertext{b0}
			if b1 != nil {
				ins = append(ins, b1)
			}
			res = res[:0]
			for _, in := range ins {
				r := h.ckksEval.MultByConstNew(in, 2.0)
				if err := h.ckksEval.Rescale(r, x.params.Scale(), r); err != nil {
					t.Fatalf("Rescale: %v", err)
				}
				h.ckksEval.AddConst(r, 1.0, r)
				res = append(res, r)
			}
		})
		maxErr := 0.0
		for k, r := range res {
			got := h.ckksEnc.DecodeComplex(h.ckksDec.DecryptNew(r), x.params.LogSlots())
			for i := 0; i < x.params.Slots(); i++ {
				if e := math.Abs(real(got[i]) - (2*data[k*x.params.Slots()+i] + 1)); e > maxErr {
					maxErr = e
				}
			}
		}
		out.Baseline["_max_abs_error_check"] = &e2eStage{Median: maxErr}
		bm := func(n string) float64 { return out.Baseline[n].Median }
		lb := bm("sub_ntt_setscale") + bm("halfboot") + bm("ckks_eval_2x_plus_1")
		out.BaselineLB = map[string]float64{"sub_ntt_setscale": bm("sub_ntt_setscale"), "halfboot": bm("halfboot"), "ckks_eval_2x_plus_1": bm("ckks_eval_2x_plus_1"), "total": lb}
		out.BaselineLBTxn = map[string]float64{"total": lb / float64(lanes)}
		out.BaselineNote = "LOWER BOUND for the HalfBoot path: Sub + HalfBoot + 2x+1 only. A real LR after HalfBoot would also need slot rotations (inner sum over 256 features) and a bias add, none of which are included. Per-output-ct stages are medians for output 0; per-transaction = per-output-ct / lanes (256 at LogN 16). HalfBoot returns 1 ct for full-coefficient sets (b1==nil)."
		cp("after_baseline")
	}
	write()
	t.Logf("cnp %s LogN=%d lanes=%d: online total %.3f ms per output ct (%.5f ms/txn), headroom after pmul %.2f bits, %s",
		cipher, x.params.LogN(), lanes, out.PerOutputMs["total"], out.PerTxnMs["total"], hbMul, out.AUCSet)
}
