package ckks_fv

// Fast, ungated unit tests for the FV-slot baseline (rtf_slot_test.go): layout arithmetic and the exact
// scale-up. No keys, no ciphertexts. Staged into ckks_fv/ by third_party/fetch_rtf.sh.

import (
	"math/big"
	mrand "math/rand"
	"testing"

	"github.com/ldsec/lattigo/v2/ring"
)

func TestSlotLayoutCapacityMatchesCNP(t *testing.T) {
	for _, logN := range []int{10, 12, 14, 16} { // arithmetic only; nothing is allocated at this N
		N := 1 << uint(logN)
		for _, c := range []struct {
			cipher  string
			outputs int
		}{{"hera", 16}, {"rubato", 60}} {
			groups, err := slotLayout(c.cipher, N)
			if err != nil {
				t.Fatalf("%s logN=%d: %v", c.cipher, logN, err)
			}
			lanes, cts, base := 0, 0, 0
			for _, g := range groups {
				if g.G*g.S != cnpFeatures {
					t.Errorf("%s: G*S = %d, want %d", c.cipher, g.G*g.S, cnpFeatures)
				}
				if g.Base != base {
					t.Errorf("%s: group base %d, want %d", c.cipher, g.Base, base)
				}
				base += g.G
				cts += g.G
				lanes += g.Lanes
				if g.Lanes != N/g.S {
					t.Errorf("%s: group lanes %d, want %d", c.cipher, g.Lanes, N/g.S)
				}
			}
			if cts != c.outputs {
				t.Errorf("%s: layout uses %d ciphertexts, want %d (every usable element, no waste)", c.cipher, cts, c.outputs)
			}
			// Same transactions per Crypt as CNP-B's outputs * N/256: this is what keeps client cost equal.
			if want := c.outputs * N / cnpFeatures; lanes != want {
				t.Errorf("%s logN=%d: %d lanes per Crypt, CNP-B has %d", c.cipher, logN, lanes, want)
			}
		}
	}
}

func TestSlotLayoutRotationsAndKeys(t *testing.T) {
	cases := []struct {
		cipher    string
		rotations int
		keys      []int
		outputs   int
	}{
		{"hera", 4, []int{1, 2, 4, 8}, 1},
		{"rubato", 18, []int{1, 2, 4, 8, 16, 32}, 4},
	}
	for _, c := range cases {
		groups, err := slotLayout(c.cipher, 1024)
		if err != nil {
			t.Fatal(err)
		}
		rots := 0
		for _, g := range groups {
			rots += len(g.steps())
		}
		if rots != c.rotations {
			t.Errorf("%s: %d rotations per Crypt, want %d", c.cipher, rots, c.rotations)
		}
		if len(groups) != c.outputs {
			t.Errorf("%s: %d output ciphertexts, want %d", c.cipher, len(groups), c.outputs)
		}
		got := slotRotationKeys(groups)
		if len(got) != len(c.keys) {
			t.Fatalf("%s: keys %v, want %v", c.cipher, got, c.keys)
		}
		for i := range got {
			if got[i] != c.keys[i] {
				t.Errorf("%s: keys %v, want %v", c.cipher, got, c.keys)
			}
		}
	}
	if _, err := slotLayout("aes", 1024); err == nil {
		t.Errorf("unknown cipher must be rejected")
	}
}

// Every (lane, feature) maps to a distinct (slot, ciphertext), and the map fills the grid exactly.
func TestSlotPositionsAreABijection(t *testing.T) {
	const N = 1024
	for _, cipher := range []string{"hera", "rubato"} {
		groups, err := slotLayout(cipher, N)
		if err != nil {
			t.Fatal(err)
		}
		seen := map[[2]int]bool{}
		for _, g := range groups {
			for l := 0; l < g.Lanes; l++ {
				if p := g.outputSlot(l, N); p != g.slot(l, 0, N) {
					t.Errorf("%s: lane %d output slot %d is not its first slot %d", cipher, l, p, g.slot(l, 0, N))
				}
				for j := 0; j < cnpFeatures; j++ {
					k := [2]int{g.slot(l, j/g.G, N), g.Base + j%g.G}
					if k[0] < 0 || k[0] >= N {
						t.Fatalf("%s: slot %d out of range", cipher, k[0])
					}
					if seen[k] {
						t.Fatalf("%s: position %v used twice", cipher, k)
					}
					seen[k] = true
				}
			}
		}
		cts := 0
		for _, g := range groups {
			cts += g.G
		}
		if len(seen) != cts*N {
			t.Errorf("%s: %d positions filled, want %d", cipher, len(seen), cts*N)
		}
	}
}

// The doubling sequence over S slots must leave the sum of acc[p..p+S-1] at slot p, within a row.
func TestSlotRotateSumAlgebra(t *testing.T) {
	const N = 1024
	for _, cipher := range []string{"hera", "rubato"} {
		groups, err := slotLayout(cipher, N)
		if err != nil {
			t.Fatal(err)
		}
		for _, g := range groups {
			row := make([]int64, N/2)
			for i := range row {
				row[i] = int64(i*i + 7)
			}
			out := append([]int64(nil), row...)
			for _, step := range g.steps() {
				next := make([]int64, N/2)
				for i := range out {
					next[i] = out[i] + out[(i+step)%(N/2)]
				}
				out = next
			}
			for p := range row {
				var want int64
				for i := 0; i < g.S; i++ {
					want += row[(p+i)%(N/2)]
				}
				if out[p] != want {
					t.Fatalf("%s S=%d: slot %d = %d, want %d", cipher, g.S, p, out[p], want)
				}
			}
		}
	}
}

// slotExactBig is the definition: round(c*Q_l/t), computed with big integers.
func slotExactBig(c uint64, Q *big.Int, tt uint64) *big.Int {
	T := new(big.Int).SetUint64(tt)
	n := new(big.Int).Mul(new(big.Int).SetUint64(c), Q)
	n.Add(n, new(big.Int).Rsh(T, 1))
	return n.Div(n, T)
}

func slotUnitCtx(t *testing.T) *cnpCtx {
	cfg := cnpConfig(t, "hera", 10)
	params, err := cfg.hbtpParams.Params()
	if err != nil {
		t.Fatal(err)
	}
	params.SetLogFVSlots(params.LogN())
	return &cnpCtx{cfg: cfg, params: params, ringLvl: map[int]*ring.Ring{}, fvEncoder: NewMFVEncoder(params)}
}

func TestSlotScaleUpExact(t *testing.T) {
	t.Setenv("CNP_SCALEUP", "exact") // cnpScaleUp's default, stated explicitly
	x := slotUnitCtx(t)
	N, tt := x.params.N(), x.params.PlainModulus()
	rng := mrand.New(mrand.NewSource(20260617))
	sym := make([]uint64, N)
	for i := range sym {
		sym[i] = uint64(rng.Int63n(int64(tt)))
	}
	sym[0], sym[1], sym[2], sym[3] = 0, 1, tt/2, tt-1

	// Level 0: bit-identical to CNP-B's scale-up, the brief's round(c*q0/t).
	got0 := newSlotScaler(x, 0).scale(sym).Value()[0].Coeffs[0]
	want0 := cnpScaleUp(x, 0, sym).Value()[0].Coeffs[0]
	for i := range got0 {
		if got0[i] != want0[i] {
			t.Fatalf("level 0, coefficient %d (c=%d): got %d, cnpScaleUp %d", i, sym[i], got0[i], want0[i])
		}
	}

	// Every level: each limb equals round(c*Q_l/t) mod q_i, by big-integer definition.
	for _, level := range []int{0, 1, 2, 8, 23} {
		rq := x.ringAt(level)
		pt := newSlotScaler(x, level).scale(sym)
		if pt.Level() != level {
			t.Fatalf("plaintext level %d, want %d", pt.Level(), level)
		}
		for i, c := range sym {
			e := slotExactBig(c, rq.ModulusBigint, tt)
			for li, q := range rq.Modulus {
				want := new(big.Int).Mod(e, new(big.Int).SetUint64(q)).Uint64()
				if pt.Value()[0].Coeffs[li][i] != want {
					t.Fatalf("level %d limb %d coefficient %d (c=%d): got %d, want %d", level, li, i, c, pt.Value()[0].Coeffs[li][i], want)
				}
			}
		}
	}

	// Negative control: the fork's floor-based FVScaleUp is a different function, so this test can fail.
	ptRt := NewPlaintextRingT(x.params)
	copy(ptRt.Value()[0].Coeffs[0], sym)
	ptF := NewPlaintextFVLvl(x.params, 1)
	x.fvEncoder.FVScaleUp(ptRt, ptF)
	ptE := newSlotScaler(x, 1).scale(sym)
	diff := 0
	for i := range sym {
		if ptF.Value()[0].Coeffs[0][i] != ptE.Value()[0].Coeffs[0][i] {
			diff++
		}
	}
	if diff < N/2 {
		t.Errorf("FVScaleUp agrees with the exact scale-up on %d of %d coefficients: the check has no power", N-diff, N)
	}
}

// The weight plaintext in the slot domain: ~t-sized coefficients, and sparse because the vector is periodic.
// This is the structural fact behind evaluating at level 1 (docs/bfv_slot_baseline_design.md section 4.4).
func TestSlotWeightPolyIsSparseAndLarge(t *testing.T) {
	x := slotUnitCtx(t)
	N, tt := x.params.N(), int64(x.params.PlainModulus())
	rng := mrand.New(mrand.NewSource(20260617))
	wq := make([]int64, cnpFeatures)
	for j := range wq {
		wq[j] = int64(rng.Intn(129) - 64)
	}
	groups, err := slotLayout("rubato", N)
	if err != nil {
		t.Fatal(err)
	}
	for _, g := range groups {
		poly := slotWeightPoly(x, g, 0, wq)
		nz, largest := 0, int64(0)
		for _, v := range poly {
			if v != 0 {
				nz++
			}
			if v < 0 {
				v = -v
			}
			if v > largest {
				largest = v
			}
			if v > tt/2 {
				t.Fatalf("signed lift out of range: |%d| > t/2", v)
			}
		}
		if nz > 2*g.S {
			t.Errorf("S=%d: %d nonzero coefficients, want <= %d", g.S, nz, 2*g.S)
		}
		if largest < tt/1024 {
			t.Errorf("S=%d: largest coefficient %d, expected ~t: the level-1 rationale would be wrong", g.S, largest)
		}
	}
}
