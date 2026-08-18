package cipher

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
)

// HERA16 implements CipherBackend using the HERA stream cipher (Cho et al.,
// "Transciphering Framework for Approximate Homomorphic Encryption", ASIACRYPT
// 2021) with post-analysis parameters.
//
// Post-attack parameter set (safe as of 2026):
//   - State size m = 16
//   - Rounds r = 5 (matches RtFHeraParams[3] "128as", the HE-side 128-bit
//     target in third_party/RtF-Transciphering/ckks_fv/rtf_params.go. The
//     original HERA-16 spec used r=4; this module matches the server.)
//   - Plaintext modulus t = 2^26 (26-bit; chosen conservatively to stay within
//     the safe range identified by the algebraic cryptanalysis)
//   - Key: HeraStateSize (16) field elements in [0, t), matching ckks_fv's
//     own `key []uint64` (NOT an AES-shaped byte key -- see below).
//   - Nonce: 16 field elements in [0, t), packed into bytes to seed a
//     SHAKE256 XOF (see roundKeys in shakeprf.go).
//
// AEAD wrapper: AES-128-GCM is applied over the online ciphertext so that
// additive malleability of the stream cipher is neutralised (docs/spec.md §8).
// This wrapper -- and its key derivation via SHA-256 of the HERA key
// material -- is this module's own application-level framing, not part of
// HERA's spec.
//
// IMPORTANT: This is the PLAINTEXT path. The HE-evaluation path (evaluate
// HERA inside BFV, then convert to CKKS via StC + modular reduction) is
// PENDING vendor_server integration. See README.md.
//
// Round-key derivation now matches ckks_fv exactly: SHAKE256(nonce) seeds an
// XOF, and sampleZqx-drawn scalars are multiplied against each key element
// per round (shakeprf.go's roundKeys, ported from
// third_party/RtF-Transciphering/ckks_fv/fv_hera.go:100-113 and
// RtF_bench_test.go:500-513 at the pinned SHA
// 105fc73115b56f1d6ff357029c7682b19a6d8510). The previous fixed-table
// (`heraRC`) + AES-ECB-PRF scheme is gone -- it never matched the reference
// and its 5th round-constant row was an admitted, unsourced placeholder.
// Gate A (a known-answer test against ckks_fv's own plainHera) is the
// correctness check for this derivation; see cipher/hera_test.go.
//
// HERA's reference defines no multi-block extension: one nonce deterministically
// yields exactly one HeraStateSize-element block (fv_hera.go's `ic = 1..16`
// fixed initial state; all nonce-dependence flows through the round keys).
// Plaintexts longer than one block are this module's own extension: block 0
// matches the reference exactly (XOF seeded with nonce alone); block index
// >0 appends a big-endian counter to the XOF seed, reusing the SHAKE256
// primitive rather than inventing a new formula. See encryptBlock.
const (
	HeraStateSize = 16 // m
	HeraRounds    = 5  // r
	HeraModBits   = 26 // log2(t)
	HeraModulus   = uint64(1) << HeraModBits // t = 2^26

	heraKeyElems   = HeraStateSize // key is HeraStateSize field elements, matching ckks_fv's key []uint64
	heraNonceElems = 16            // nonce is 16 field elements, packed to bytes to seed the XOF
)

// HERA16 is the HERA stream cipher with m=16, r=5, t=2^26.
type HERA16 struct{}

// NewHERA16 returns a new HERA-16 backend.
func NewHERA16() CipherBackend { return HERA16{} }

func (HERA16) Name() string    { return "HERA-16" }
func (HERA16) KeySize() int    { return heraKeyElems }
func (HERA16) NonceSize() int  { return heraNonceElems }
func (HERA16) Modulus() uint64 { return HeraModulus }

// OnlineCiphertextBytes: n uint64 elements × 4 bytes each (packed at 26 bits,
// rounded up to 4 bytes) + 28 bytes AEAD overhead (12 nonce + 16 tag).
func (HERA16) OnlineCiphertextBytes(n int) int {
	return n*4 + 28
}

// Encrypt encrypts plaintext (Z_t elements) using HERA-16 + AES-GCM wrapper.
// The output is: [12-byte GCM nonce || GCM-authenticated ciphertext+tag].
// key must be heraKeyElems elements; nonce must be heraNonceElems elements.
func (h HERA16) Encrypt(key, nonce, plaintext []uint64) ([]uint64, error) {
	if len(key) != heraKeyElems {
		return nil, fmt.Errorf("%w: want %d elements, got %d", ErrInvalidKey, heraKeyElems, len(key))
	}
	if len(nonce) != heraNonceElems {
		return nil, fmt.Errorf("%w: want %d elements, got %d", ErrInvalidNonce, heraNonceElems, len(nonce))
	}

	// Generate keystream via HERA rounds.
	ks := h.keystream(key, nonce, len(plaintext))

	// XOR plaintext with keystream (mod t).
	ct := make([]uint64, len(plaintext))
	for i := range plaintext {
		ct[i] = (plaintext[i] + ks[i]) % HeraModulus
	}

	// Serialize ct as packed uint32 (26 bits fit in 32 bits).
	raw := make([]byte, len(ct)*4)
	for i, v := range ct {
		binary.LittleEndian.PutUint32(raw[i*4:], uint32(v))
	}

	// AES-128-GCM over the serialized ciphertext output.
	aesKey := heraAEADKey(key)
	noncePacked := packElems(nonce)

	block, err := aes.NewCipher(aesKey)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCMWithNonceSize(block, 12)
	if err != nil {
		return nil, err
	}
	gcmNonce := noncePacked[:12]
	sealed := gcm.Seal(gcmNonce, gcmNonce, raw, nil) // prepend nonce

	// Re-encode as []uint64 for interface consistency (1 byte per element).
	out := make([]uint64, len(sealed))
	for i, b := range sealed {
		out[i] = uint64(b)
	}
	return out, nil
}

// heraAEADKey derives the AES-128 key for the outer AEAD wrapper from the
// HERA key material via SHA-256. This wrapper is this module's own
// application-level design (docs/spec.md §8), not part of HERA's spec --
// HERA's own key is heraKeyElems field elements, not AES-key-shaped bytes.
func heraAEADKey(key []uint64) []byte {
	sum := sha256.Sum256(packElems(key))
	return sum[:16]
}

// packElems packs each field element into 4 little-endian bytes (matches the
// ciphertext serialization above), so the full element -- not just its low
// byte -- contributes entropy downstream (XOF seeding, AEAD key/nonce).
func packElems(elems []uint64) []byte {
	buf := make([]byte, len(elems)*4)
	for i, v := range elems {
		binary.LittleEndian.PutUint32(buf[i*4:], uint32(v))
	}
	return buf
}

// EvalKeyExpansion returns the per-round key vectors (plaintext path) for
// block 0 of the given nonce (i.e. the same round keys Encrypt would use for
// the first HeraStateSize-element block). heCtx must be nil in the plaintext
// benchmark.
func (h HERA16) EvalKeyExpansion(key, nonce []uint64, heCtx interface{}) ([][]uint64, error) {
	if heCtx != nil {
		return nil, fmt.Errorf("PENDING: HE-path EvalKeyExpansion requires vendor_server integration; heCtx must be nil in the plaintext benchmark")
	}
	if len(key) != heraKeyElems {
		return nil, fmt.Errorf("%w: want %d, got %d", ErrInvalidKey, heraKeyElems, len(key))
	}
	if len(nonce) != heraNonceElems {
		return nil, fmt.Errorf("%w: want %d, got %d", ErrInvalidNonce, heraNonceElems, len(nonce))
	}
	xof := newShake256()
	xof.Write(packElems(nonce))
	return roundKeys(xof, HeraRounds, HeraStateSize, key, HeraModulus), nil
}

// --- Internal helpers ---------------------------------------------------------

// keystream generates len(pt) keystream elements.
func (h HERA16) keystream(key, nonce []uint64, n int) []uint64 {
	ks := make([]uint64, 0, n)
	blockIdx := uint64(0)
	for len(ks) < n {
		blk := h.encryptBlock(key, nonce, blockIdx)
		ks = append(ks, blk[:min(HeraStateSize, n-len(ks))]...)
		blockIdx++
	}
	return ks[:n]
}

// encryptBlock computes one HERA keystream block for the given nonce and
// block index. blockIdx=0 matches ckks_fv's plainHera/mfvHera exactly (XOF
// seeded with nonce alone: RtF_bench_test.go:502-503 / fv_hera.go:100-105)
// -- this is what Gate A's known-answer test validates. blockIdx>0 is this
// module's own extension for plaintexts longer than one block (see the
// package doc comment above): the XOF seed gets an appended big-endian block
// index.
func (h HERA16) encryptBlock(key, nonce []uint64, blockIdx uint64) [HeraStateSize]uint64 {
	xof := newShake256()
	xof.Write(packElems(nonce))
	if blockIdx != 0 {
		var idxBytes [8]byte
		binary.BigEndian.PutUint64(idxBytes[:], blockIdx)
		xof.Write(idxBytes[:])
	}
	rks := roundKeys(xof, HeraRounds, HeraStateSize, key, HeraModulus)

	// Fixed public initial state (ic = 1..16). HERA has no nonce-dependent
	// state init -- all nonce dependence flows through rks above. Matches
	// fv_hera.go:65-68 (NewMFVHera) / RtF_bench_test.go:515-517 (plainHera).
	var state [HeraStateSize]uint64
	for i := 0; i < HeraStateSize; i++ {
		state[i] = uint64(i + 1)
	}

	// Round 0: initial AddRoundKey (whitening). RtF_bench_test.go:520-522.
	for st := 0; st < HeraStateSize; st++ {
		state[st] = (state[st] + rks[0][st]) % HeraModulus
	}

	// Rounds 1..HeraRounds-1: linLayer, cube S-box, AddRoundKey.
	// RtF_bench_test.go:524-556.
	for r := 1; r < HeraRounds; r++ {
		state = heraLinLayer(state)
		for st := 0; st < HeraStateSize; st++ {
			state[st] = mulMod(mulMod(state[st], state[st]), state[st])
		}
		for st := 0; st < HeraStateSize; st++ {
			state[st] = (state[st] + rks[r][st]) % HeraModulus
		}
	}

	// Finalization: linLayer, cube, linLayer (twice), AddRoundKey.
	// RtF_bench_test.go:557-611 / fv_hera.go's Crypt: linLayer, cube,
	// linLayer, addRoundKey(numRound, reduce=true).
	state = heraLinLayer(state)
	for st := 0; st < HeraStateSize; st++ {
		state[st] = mulMod(mulMod(state[st], state[st]), state[st])
	}
	state = heraLinLayer(state)
	for st := 0; st < HeraStateSize; st++ {
		state[st] = (state[st] + rks[HeraRounds][st]) % HeraModulus
	}
	return state
}

// heraLinLayer applies HERA's fixed linear layer: MixColumns then MixRows,
// each a [2,3,1,1] circulant over a 4x4 grid. Ported verbatim from
// RtF_bench_test.go:525-547 (plaintext form); verified identical to the
// homomorphic form's coefficients (fv_hera.go:297-353).
func heraLinLayer(state [HeraStateSize]uint64) [HeraStateSize]uint64 {
	var buf [HeraStateSize]uint64
	for col := 0; col < 4; col++ {
		y0 := 2*state[col] + 3*state[col+4] + state[col+8] + state[col+12]
		y1 := 2*state[col+4] + 3*state[col+8] + state[col+12] + state[col]
		y2 := 2*state[col+8] + 3*state[col+12] + state[col] + state[col+4]
		y3 := 2*state[col+12] + 3*state[col] + state[col+4] + state[col+8]
		buf[col] = y0 % HeraModulus
		buf[col+4] = y1 % HeraModulus
		buf[col+8] = y2 % HeraModulus
		buf[col+12] = y3 % HeraModulus
	}
	var out [HeraStateSize]uint64
	for row := 0; row < 4; row++ {
		y0 := 2*buf[4*row] + 3*buf[4*row+1] + buf[4*row+2] + buf[4*row+3]
		y1 := 2*buf[4*row+1] + 3*buf[4*row+2] + buf[4*row+3] + buf[4*row]
		y2 := 2*buf[4*row+2] + 3*buf[4*row+3] + buf[4*row] + buf[4*row+1]
		y3 := 2*buf[4*row+3] + 3*buf[4*row] + buf[4*row+1] + buf[4*row+2]
		out[4*row] = y0 % HeraModulus
		out[4*row+1] = y1 % HeraModulus
		out[4*row+2] = y2 % HeraModulus
		out[4*row+3] = y3 % HeraModulus
	}
	return out
}

// mulMod computes (a * b) mod HeraModulus without overflow.
func mulMod(a, b uint64) uint64 {
	// Both a, b < 2^26; product < 2^52 — fits in uint64.
	return (a * b) % HeraModulus
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
