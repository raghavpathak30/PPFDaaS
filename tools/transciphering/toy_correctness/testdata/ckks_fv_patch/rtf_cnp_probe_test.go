package ckks_fv

// CNP-B Step 0 probes (a) index mapping and noise at the pmul input.
//
//	E2E_RUN=1 E2E_CIPHER=hera|rubato E2E_LOGN=10 go test -run '^TestCNPProbeIndexMapping$' -v
//
// Gated like the e2e harness. Staged into ckks_fv/ by third_party/fetch_rtf.sh.

import (
	"crypto/rand"
	"encoding/json"
	"math"
	"math/big"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/ldsec/lattigo/v2/utils"
)

// cnpClientKS is the client side of the offline phase: one nonce per coefficient of an output
// ciphertext and the plaintext keystream for every nonce. ks[i][k] is element k of block i.
func cnpClientKS(t *testing.T, x *cnpCtx) (nonces [][]byte, counter []byte, ks [][]uint64) {
	nonceLen := 64
	if x.cfg.cipher == "rubato" {
		nonceLen = 8
		counter = make([]byte, 8)
		if _, err := rand.Read(counter); err != nil {
			t.Fatalf("rand: %v", err)
		}
	}
	nonces = make([][]byte, x.nBlocks)
	ks = make([][]uint64, x.nBlocks)
	for i := range nonces {
		nonces[i] = make([]byte, nonceLen)
		if _, err := rand.Read(nonces[i]); err != nil {
			t.Fatalf("rand: %v", err)
		}
		if x.cfg.cipher == "hera" {
			ks[i] = plainHera(x.cfg.numRound, nonces[i], x.key, x.params.PlainModulus())
		} else {
			ks[i] = plainRubato(x.cfg.blocksize, x.cfg.numRound, nonces[i], counter, x.key, x.params.PlainModulus(), RubatoParams[x.cfg.rubatoPar].Sigma)
		}
	}
	return
}

// cnpCrypt returns the homomorphic keystream evaluation (server offline phase) for the given
// nonces, a reset between repetitions, and the number of usable output ciphertexts.
func cnpCrypt(t *testing.T, x *cnpCtx, nonces [][]byte, counter []byte) (crypt func() []*Ciphertext, reset func(), outputs int) {
	if x.cfg.cipher == "hera" {
		hera := NewMFVHera(x.cfg.numRound, x.params, x.fvEncoder, x.fvEnc, x.fvEval, x.cfg.modDown[0])
		kCt := hera.EncKey(x.key)
		return func() []*Ciphertext { return hera.Crypt(nonces, kCt, x.cfg.modDown) },
			func() { hera.Reset(x.cfg.modDown[0]) }, 16
	}
	rubato := NewMFVRubato(x.cfg.rubatoPar, x.params, x.fvEncoder, x.fvEnc, x.fvEval, x.cfg.modDown[0])
	kCt := rubato.EncKey(x.key)
	// Rubato: Crypt returns blocksize ciphertexts but only the first blocksize-4 carry the
	// final round key (fv_rubato.go finAddRoundKey(blocksize-4)).
	return func() []*Ciphertext { return rubato.Crypt(nonces, counter, kCt, x.cfg.modDown) },
		func() { rubato.Reset(x.cfg.modDown[0]) }, x.cfg.blocksize - 4
}

// cnpStC is the benchmarks' per-ciphertext StC step (RtF_bench_test.go:196-197).
func cnpStC(x *cnpCtx, ct *Ciphertext) *Ciphertext {
	out := x.fvEval.SlotsToCoeffs(ct, x.cfg.stcModDown)
	x.fvEval.ModSwitchMany(out, out, out.Level())
	return out
}

// cnpDecryptRingT decrypts an FV ciphertext to its R_t coefficients in [0,t) (natural coefficient order).
func cnpDecryptRingT(x *cnpCtx, ct *Ciphertext) (coeffs []uint64, raw *Plaintext) {
	raw = x.fvDec.DecryptNew(ct)
	// DecodeRingT -> DivByQOverTRounded scales its input IN PLACE (ring_scaling.go: MulScalar(p1Q,T,p1Q)),
	// so keep an untouched copy for the noise measurement.
	rawCopy := raw.CopyNew().Plaintext()
	ptRt := NewPlaintextRingT(x.params)
	x.fvEncoder.DecodeRingT(raw, ptRt)
	raw = rawCopy
	return append([]uint64(nil), ptRt.Value()[0].Coeffs[0]...), raw
}

// cnpNoise returns log2 of the max |FV decryption noise| and the headroom to the
// decryption bound q0/(2t), both in bits. Level-0 ciphertexts only (single prime).
func cnpNoise(x *cnpCtx, raw *Plaintext) (noiseBits, headroomBits float64) {
	q0 := new(big.Int).SetUint64(x.params.Qi()[0])
	tt := new(big.Int).SetUint64(x.params.PlainModulus())
	maxNoise := new(big.Float)
	half := new(big.Int).Rsh(q0, 1)
	for _, p := range raw.Value()[0].Coeffs[0] {
		P := new(big.Int).SetUint64(p)
		tp := new(big.Int).Mul(tt, P)
		// m = round(t*p/q0); v = t*p - m*q0, |v| <= q0/2, noise = |v|/t
		m := new(big.Int).Div(new(big.Int).Add(tp, half), q0)
		v := new(big.Int).Sub(tp, new(big.Int).Mul(m, q0))
		v.Abs(v)
		if f := new(big.Float).SetInt(v); f.Cmp(maxNoise) > 0 {
			maxNoise = f
		}
	}
	maxNoise.Quo(maxNoise, new(big.Float).SetInt(tt))
	nf, _ := maxNoise.Float64()
	bound := float64(x.params.Qi()[0]) / (2 * float64(x.params.PlainModulus()))
	if nf < 1 {
		nf = 1
	}
	return math.Log2(nf), math.Log2(bound / nf)
}

type cnpProbeOut struct {
	Cipher           string         `json:"cipher"`
	LogN             int            `json:"log_n"`
	N                int            `json:"n"`
	T                uint64         `json:"plain_modulus"`
	Q0Bits           float64        `json:"q0_bits"`
	OutputsChecked   int            `json:"output_ciphertexts_checked"`
	MapMatchesBitrev bool           `json:"decrypted_keystream_index_equals_bitrev_logN"`
	MapIsNatural     bool           `json:"decrypted_keystream_index_equals_natural"`
	Unmatched        int            `json:"elements_with_no_unique_match"`
	SampleIdxToCoeff map[int]int    `json:"sample_symmetric_index_to_fv_coefficient"`
	OneHot           []cnpOneHot    `json:"one_hot_probe"`
	NoiseBits        float64        `json:"ks_ct_noise_bits_log2_max"`
	HeadroomBits     float64        `json:"ks_ct_headroom_bits_to_q0_over_2t"`
}

type cnpOneHot struct {
	Index        int  `json:"symmetric_index"`
	NonzeroCount int  `json:"nonzero_coefficients_after_decrypt"`
	NonzeroAt    int  `json:"nonzero_coefficient"`
	Value        int  `json:"value_mod_t"`
	AtBitrev     bool `json:"at_bitrev_logN_of_index"`
}

func TestCNPProbeIndexMapping(t *testing.T) {
	if os.Getenv("E2E_RUN") != "1" {
		t.Skip("set E2E_RUN=1 to run")
	}
	cipher := os.Getenv("E2E_CIPHER")
	if cipher == "" {
		cipher = "hera"
	}
	logN := cnpEnvInt("E2E_LOGN", 10)
	x := cnpSetup(t, cipher, logN)
	N, logn, tmod := x.params.N(), x.params.LogN(), x.params.PlainModulus()
	nonces, counter, ks := cnpClientKS(t, x)
	crypt, _, outputs := cnpCrypt(t, x, nonces, counter)
	all := crypt()
	checkOutputs := []int{0, outputs - 1}
	out := &cnpProbeOut{Cipher: cipher, LogN: logn, N: N, T: tmod, Q0Bits: math.Log2(float64(x.params.Qi()[0])),
		OutputsChecked: len(checkOutputs), MapMatchesBitrev: true, MapIsNatural: true,
		SampleIdxToCoeff: map[int]int{}}

	for _, k := range checkOutputs {
		ks0 := cnpStC(x, all[k].CopyNew().Ciphertext())
		dec, raw := cnpDecryptRingT(x, ks0)
		nb, hb := cnpNoise(x, raw)
		out.NoiseBits, out.HeadroomBits = nb, hb

		// (a) which coefficient holds keystream element ks[i][k]? Match by value; accept only unique matches.
		// Rubato's client keystream carries the cipher's added noise (|d| <= 9, rubato.go:345) that the
		// server's homomorphic keystream does not, so Rubato matches within +-tol.
		tol := 0
		if cipher == "rubato" {
			tol = 9
		}
		byVal := map[uint64][]int{}
		for c, v := range dec {
			byVal[v] = append(byVal[v], c)
		}
		for i := 0; i < N; i++ {
			cs := cnpCandidates(byVal, ks[i][k], tol, tmod)
			if len(cs) != 1 {
				out.Unmatched++
				continue
			}
			c := cs[0]
			if c != int(utils.BitReverse64(uint64(i), uint64(logn))) {
				out.MapMatchesBitrev = false
			}
			if c != i {
				out.MapIsNatural = false
			}
			if k == 0 && (i < 6 || i == N/2 || i == N-1 || i == 100%N) {
				out.SampleIdxToCoeff[i] = c
			}
		}
		if out.Unmatched > 0 {
			t.Logf("output %d: %d of %d elements had no unique value match (collisions mod t)", k, out.Unmatched, N)
		}
	}

	// One-hot probe: client symmetric ciphertext = keystream at every position except element i, which
	// carries ks_i + 1 (one-hot message e_i). The client places element j at coefficient pi(j) (the
	// empirical mapping just found); after Sub, decrypt must show exactly one nonzero coefficient.
	ks0 := cnpStC(x, all[0].CopyNew().Ciphertext())
	dec0, _ := cnpDecryptRingT(x, ks0)
	pi := make([]int, N)
	tol0 := 0
	if cipher == "rubato" {
		tol0 = 9
	}
	byVal := map[uint64][]int{}
	for c, v := range dec0 {
		byVal[v] = append(byVal[v], c)
	}
	for i := 0; i < N; i++ {
		if cs := cnpCandidates(byVal, ks[i][0], tol0, tmod); len(cs) == 1 {
			pi[i] = cs[0]
		} else {
			pi[i] = int(utils.BitReverse64(uint64(i), uint64(logn))) // collision: fall back to the verified bitrev
		}
	}
	probeIdx := []int{0, 1, 2, 5, 100 % N, N / 2, N - 1}
	sort.Ints(probeIdx)
	// Residual of Sub with NO one-hot: all zeros for HERA; Rubato's per-element noise d_j otherwise.
	// The one-hot result is judged against it, so exactly one coefficient may differ, by +1.
	subResidual := func(onehot int) []uint64 {
		i := onehot
		ptRt := NewPlaintextRingT(x.params)
		poly := ptRt.Value()[0]
		for j := 0; j < N; j++ {
			v := ks[j][0]
			if j == i {
				v = (v + 1) % tmod
			}
			poly.Coeffs[0][pi[j]] = v
		}
		pt := NewPlaintextFVLvl(x.params, 0)
		x.fvEncoder.FVScaleUp(ptRt, pt)
		ct := NewCiphertextFVLvl(x.params, 1, 0)
		ct.Value()[0] = pt.Value()[0].CopyNew()
		x.fvEval.Sub(ct, cnpStC(x, all[0].CopyNew().Ciphertext()), ct)
		d, _ := cnpDecryptRingT(x, ct)
		return d
	}
	base := subResidual(-1)
	if cipher == "hera" {
		for c, v := range base {
			if v != 0 {
				t.Fatalf("HERA: Sub residual without one-hot is nonzero at coefficient %d (%d)", c, v)
			}
		}
	}
	for _, i := range probeIdx {
		d := subResidual(i)
		oh := cnpOneHot{Index: i, NonzeroAt: -1}
		for c := range d {
			if v := (d[c] + tmod - base[c]) % tmod; v != 0 {
				oh.NonzeroCount++
				oh.NonzeroAt, oh.Value = c, int(v)
			}
		}
		oh.AtBitrev = oh.NonzeroAt == int(utils.BitReverse64(uint64(i), uint64(logn)))
		out.OneHot = append(out.OneHot, oh)
		if oh.NonzeroCount != 1 || oh.Value != 1 {
			t.Errorf("one-hot e_%d: %d nonzero coefficients (at %d, value %d), want exactly one with value 1", i, oh.NonzeroCount, oh.NonzeroAt, oh.Value)
		}
	}
	if out.Unmatched > N/50 {
		t.Errorf("too many elements without a unique value match: %d of %d per output", out.Unmatched, N)
	}
	b, _ := json.MarshalIndent(out, "", "  ")
	t.Logf("probe result:\n%s", b)
	if dir := os.Getenv("RESULTS_DIR"); dir != "" {
		_ = os.MkdirAll(dir, 0755)
		_ = os.WriteFile(filepath.Join(dir, "cnp_probe_"+cipher+".json"), b, 0644)
	}
}

// cnpCandidates returns the coefficients whose decrypted value is within +-tol (mod t) of want.
func cnpCandidates(byVal map[uint64][]int, want uint64, tol int, t uint64) []int {
	var out []int
	for d := -tol; d <= tol; d++ {
		v := uint64((int64(want) + int64(d) + int64(t)) % int64(t))
		out = append(out, byVal[v]...)
	}
	return out
}
