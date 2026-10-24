package cipher

import (
	"math/big"
	"math/bits"
	"testing"

	"github.com/ldsec/lattigo/v2/ckks_fv"
)

// heraLegacyKAT2p26 is the keystream the previous hera.go produced at the
// wrong modulus t = 2^26 (same key/nonce as TestHERAKnownAnswer). It is kept
// only as a negative check: the current implementation must NOT reproduce it.
var heraLegacyKAT2p26 = [HeraStateSize]uint64{
	25659216, 62785430, 61075836, 56672174, 55041421, 6788142, 25451291, 34234690,
	6798004, 35538661, 55398152, 12001395, 42711758, 31894937, 47448404, 43070509,
}

// TestHeraModulusMatchesCkksFV pins HeraModulus (a compile-time const, like
// RubatoModulus, so `% HeraModulus` is strength-reduced) to the reference's
// PlainModulus for the HERA 128af (index 2) and 128as (index 3) sets, and
// checks it is an odd prime, not a power of two.
func TestHeraModulusMatchesCkksFV(t *testing.T) {
	for _, idx := range []int{2, 3} {
		if got := ckks_fv.RtFHeraParams[idx].PlainModulus; got != HeraModulus {
			t.Errorf("RtFHeraParams[%d].PlainModulus = %d, HeraModulus = %d", idx, got, HeraModulus)
		}
	}
	if bits.OnesCount64(HeraModulus) == 1 {
		t.Errorf("HeraModulus %d is a power of two; HERA is defined over a prime field", HeraModulus)
	}
	if !new(big.Int).SetUint64(HeraModulus).ProbablyPrime(32) {
		t.Errorf("HeraModulus %d is not prime", HeraModulus)
	}
}

// TestHERAKnownAnswer is Gate A: it compares this module's keystream against
// ckks_fv's own plainHera reference (RtF_bench_test.go:500-613) for a fixed
// (key, nonce) pair, at the pinned SHA
// 105fc73115b56f1d6ff357029c7682b19a6d8510, with the modulus taken from
// RtFHeraParams[3].PlainModulus (33292289). No HERA timing number is
// meaningful until this passes.
//
// The vector is produced by the committed generator
// cipher/testdata/hera_kat_gen_test.go (see its header for how to run it):
//
//	heraKey[i]   = 1000+i, i = 0..15
//	heraNonce[i] = 2000+i, i = 0..15
//	plainHera(5, packElems(heraNonce), heraKey, RtFHeraParams[3].PlainModulus)
//
// It also asserts the legacy t=2^26 vector no longer matches, so a regression
// to a power-of-two modulus fails this test.
func TestHERAKnownAnswer(t *testing.T) {
	key := make([]uint64, HeraStateSize)
	nonce := make([]uint64, heraNonceElems)
	for i := 0; i < HeraStateSize; i++ {
		key[i] = uint64(1000 + i)
	}
	for i := 0; i < heraNonceElems; i++ {
		nonce[i] = uint64(2000 + i)
	}

	want := [HeraStateSize]uint64{
		11681254, 104368, 32708696, 6168483, 20289095, 13937265, 1381117, 23770418,
		6472581, 9397252, 18033278, 3833657, 32904005, 12083593, 27572718, 11685833,
	}

	h := HERA16{}
	got := h.encryptBlock(key, nonce, 0)

	if got != want {
		t.Fatalf("HERA keystream block 0 mismatch (does not match ckks_fv's plainHera):\n got  %v\n want %v", got, want)
	}
	if got == heraLegacyKAT2p26 {
		t.Fatalf("HERA keystream equals the legacy t=2^26 vector; modulus regressed")
	}
}

// TestHERAEvalKeyExpansionMatchesEncrypt checks that EvalKeyExpansion returns
// the same round keys Encrypt uses for block 0 (both derive from
// SHAKE256(nonce) via roundKeys).
func TestHERAEvalKeyExpansionMatchesEncrypt(t *testing.T) {
	key := make([]uint64, HeraStateSize)
	nonce := make([]uint64, heraNonceElems)
	for i := 0; i < HeraStateSize; i++ {
		key[i] = uint64(1000 + i)
	}
	for i := 0; i < heraNonceElems; i++ {
		nonce[i] = uint64(2000 + i)
	}

	h := HERA16{}
	rks, err := h.EvalKeyExpansion(key, nonce, nil)
	if err != nil {
		t.Fatalf("EvalKeyExpansion: %v", err)
	}
	if len(rks) != HeraRounds+1 {
		t.Fatalf("want %d round-key rows, got %d", HeraRounds+1, len(rks))
	}

	xof := newShake256()
	xof.Write(packElems(nonce))
	want := roundKeys(xof, HeraRounds, HeraStateSize, key, HeraModulus)
	for r := range want {
		for i := range want[r] {
			if rks[r][i] != want[r][i] {
				t.Fatalf("round %d elem %d: got %d want %d", r, i, rks[r][i], want[r][i])
			}
		}
	}
}
