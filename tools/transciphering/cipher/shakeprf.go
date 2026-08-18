package cipher

import (
	"io"
	"math/bits"

	"golang.org/x/crypto/sha3"
)

// sampleZqx returns a uniform random value in [0, q) by rejection sampling
// from rand. Ported verbatim from the pinned ckks_fv reference
// (SampleZqx, third_party/RtF-Transciphering/ckks_fv/utils.go:13-38 at
// commit 105fc73115b56f1d6ff357029c7682b19a6d8510), which both HERA's and
// Rubato's round-key derivation call directly.
func sampleZqx(rand io.Reader, q uint64) (res uint64) {
	bitLen := bits.Len64(q - 2)
	byteLen := (bitLen + 7) / 8
	b := bitLen % 8
	if b == 0 {
		b = 8
	}

	bytes := make([]byte, byteLen)
	for {
		if _, err := io.ReadFull(rand, bytes); err != nil {
			panic(err)
		}
		bytes[byteLen-1] &= uint8((1 << b) - 1)

		res = 0
		for i := 0; i < byteLen; i++ {
			res += uint64(bytes[i]) << (8 * i)
		}

		if res < q {
			return
		}
	}
}

// newShake256 returns a fresh SHAKE256 XOF, matching sha3.NewShake256() as
// used by ckks_fv's round-constant derivation (fv_hera.go / fv_rubato.go).
func newShake256() sha3.ShakeHash {
	return sha3.NewShake256()
}

// roundKeys derives the per-round key material for a SHAKE256-seeded HHE
// stream cipher: rks[r][i] = sampleZqx(xof, plainModulus) * key[i] % plainModulus,
// for r = 0..numRound inclusive. This is the exact scheme both HERA and
// Rubato use in the pinned ckks_fv reference:
//   - HERA:   fv_hera.go:107-113 (homomorphic) / RtF_bench_test.go:508-513 (plaintext)
//   - Rubato: fv_rubato.go:173-179 (homomorphic) / RtF_bench_test.go:629-634 (plaintext)
// The two ciphers differ only in how xof is seeded before this is called:
// HERA writes nonce only; Rubato writes nonce then counter (see hera.go /
// rubato.go). blockSize is the cipher's state size (16 for HERA, 64 for
// Rubato128L); key must have length blockSize.
func roundKeys(xof io.Reader, numRound, blockSize int, key []uint64, plainModulus uint64) [][]uint64 {
	rks := make([][]uint64, numRound+1)
	for r := 0; r <= numRound; r++ {
		rks[r] = make([]uint64, blockSize)
		for i := 0; i < blockSize; i++ {
			rks[r][i] = sampleZqx(xof, plainModulus) * key[i] % plainModulus
		}
	}
	return rks
}
