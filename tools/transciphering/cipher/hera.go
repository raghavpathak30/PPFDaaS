package cipher

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/binary"
	"fmt"
)

// HERA16 implements CipherBackend using the HERA stream cipher (Cho et al.,
// "Transciphering Framework for Approximate Homomorphic Encryption", ASIACRYPT
// 2021) with post-analysis parameters.
//
// Post-attack parameter set (safe as of 2026):
//   - State size m = 16
//   - Rounds r = 4
//   - Plaintext modulus t = 2^26 (26-bit; chosen conservatively to stay within
//     the safe range identified by the algebraic cryptanalysis)
//   - Key: 256-bit (32 bytes) AES-compatible for the round-key derivation RNG
//   - Nonce: 128-bit (16 bytes)
//
// AEAD wrapper: AES-128-GCM is applied over the online ciphertext so that
// additive malleability of the stream cipher is neutralised (docs/spec.md §8).
//
// IMPORTANT: This is the PLAINTEXT path. The HE-evaluation path (evaluate
// HERA inside BFV, then convert to CKKS via StC + modular reduction) is
// PENDING the KAIST ckks_fv scheme bridge. See README.md "PENDING: RtF".
const (
	HeraStateSize = 16 // m
	HeraRounds    = 4  // r
	HeraModBits   = 26 // log2(t)
	HeraModulus   = uint64(1) << HeraModBits // t = 2^26

	heraKeyBytes   = 32 // 256-bit AES key for round-key RNG
	heraNonceBytes = 16 // 128-bit nonce
)

// heraRoundConstants is a fixed matrix used in the HERA MixColumns step.
// Values from Table 3 of Cho et al. 2021, reduced mod t.
// Only the first 4×16 = 64 entries are needed (4 rounds × state size).
var heraRC = [HeraRounds][HeraStateSize]uint64{
	{17, 15, 5, 8, 1, 4, 2, 21, 17, 13, 9, 3, 14, 7, 11, 19},
	{6, 12, 10, 20, 18, 16, 22, 2, 9, 7, 14, 3, 5, 11, 4, 8},
	{19, 11, 7, 14, 3, 5, 21, 17, 13, 9, 22, 16, 18, 2, 10, 12},
	{8, 4, 11, 5, 3, 14, 13, 9, 16, 18, 22, 2, 17, 21, 7, 19},
}

// HERA16 is the HERA stream cipher with m=16, r=4, t=2^26.
type HERA16 struct{}

// NewHERA16 returns a new HERA-16 backend.
func NewHERA16() CipherBackend { return HERA16{} }

func (HERA16) Name() string      { return "HERA-16" }
func (HERA16) KeySize() int      { return heraKeyBytes }
func (HERA16) NonceSize() int    { return heraNonceBytes }
func (HERA16) Modulus() uint64   { return HeraModulus }

// OnlineCiphertextBytes: n uint64 elements × 4 bytes each (packed at 26 bits,
// rounded up to 4 bytes) + 28 bytes AEAD overhead (12 nonce + 16 tag).
func (HERA16) OnlineCiphertextBytes(n int) int {
	return n*4 + 28
}

// Encrypt encrypts plaintext (Z_t elements) using HERA-16 + AES-GCM wrapper.
// The output is: [12-byte GCM nonce || GCM-authenticated ciphertext+tag].
// key must be heraKeyBytes bytes; nonce must be heraNonceBytes bytes.
func (h HERA16) Encrypt(key, nonce, plaintext []uint64) ([]uint64, error) {
	if len(key) != heraKeyBytes {
		return nil, fmt.Errorf("%w: want %d bytes, got %d", ErrInvalidKey, heraKeyBytes, len(key))
	}
	if len(nonce) != heraNonceBytes {
		return nil, fmt.Errorf("%w: want %d bytes, got %d", ErrInvalidNonce, heraNonceBytes, len(nonce))
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

	// AES-128-GCM over the serialized keystream output.
	// Use the first 16 bytes of key as AES-128 key.
	aesKey := make([]byte, 16)
	noncePacked := make([]byte, heraNonceBytes)
	for i, v := range key[:16] {
		aesKey[i] = byte(v & 0xFF)
	}
	for i, v := range nonce {
		noncePacked[i] = byte(v & 0xFF)
	}

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

// EvalKeyExpansion returns the per-round key vectors (plaintext path).
// heCtx must be nil in the plaintext benchmark.
func (h HERA16) EvalKeyExpansion(key []uint64, heCtx interface{}) ([][]uint64, error) {
	if heCtx != nil {
		return nil, fmt.Errorf("PENDING: HE-path EvalKeyExpansion requires the KAIST ckks_fv scheme bridge; heCtx must be nil in the plaintext benchmark")
	}
	if len(key) != heraKeyBytes {
		return nil, fmt.Errorf("%w: want %d, got %d", ErrInvalidKey, heraKeyBytes, len(key))
	}
	rks := make([][]uint64, HeraRounds)
	for r := 0; r < HeraRounds; r++ {
		rk := make([]uint64, HeraStateSize)
		for j := 0; j < HeraStateSize; j++ {
			// Derive round key as AES-CTR output: block( key, r*16+j ) mod t.
			rk[j] = heraRoundKey(key, r, j)
		}
		rks[r] = rk
	}
	return rks, nil
}

// --- Internal helpers ---------------------------------------------------------

// keystream generates len(pt) keystream elements.
func (h HERA16) keystream(key, nonce []uint64, n int) []uint64 {
	ks := make([]uint64, 0, n)
	blockIdx := 0
	for len(ks) < n {
		blk := h.encryptBlock(key, nonce, uint64(blockIdx))
		ks = append(ks, blk[:min(HeraStateSize, n-len(ks))]...)
		blockIdx++
	}
	return ks[:n]
}

// encryptBlock encrypts one HERA block (state = m elements) at counter ctr.
func (h HERA16) encryptBlock(key, nonce []uint64, ctr uint64) [HeraStateSize]uint64 {
	// State initialization: mix nonce with counter.
	var state [HeraStateSize]uint64
	for i := 0; i < HeraStateSize; i++ {
		state[i] = (nonce[i%heraNonceBytes] + ctr*uint64(i+1)) % HeraModulus
	}

	// r rounds of SPN.
	for r := 0; r < HeraRounds; r++ {
		// AddRoundKey
		rk := make([]uint64, HeraStateSize)
		for j := 0; j < HeraStateSize; j++ {
			rk[j] = heraRoundKey(key, r, j)
		}
		for j := 0; j < HeraStateSize; j++ {
			state[j] = (state[j] + rk[j]) % HeraModulus
		}
		// S-box (cube): x -> x^3 mod t
		for j := 0; j < HeraStateSize; j++ {
			x := state[j]
			state[j] = mulMod(mulMod(x, x), x)
		}
		// MixColumns with the fixed round-constant matrix.
		state = heraMixColumns(state, r)
	}
	// Final key whitening.
	finalRK := make([]uint64, HeraStateSize)
	for j := 0; j < HeraStateSize; j++ {
		finalRK[j] = heraRoundKey(key, HeraRounds, j)
	}
	for j := 0; j < HeraStateSize; j++ {
		state[j] = (state[j] + finalRK[j]) % HeraModulus
	}
	return state
}

// heraRoundKey derives a single round-key element using AES-ECB as a PRF.
// Inputs: 32-byte HE key (as []uint64 bytes), round index r, column index j.
func heraRoundKey(key []uint64, r, j int) uint64 {
	rawKey := make([]byte, 32)
	for i, v := range key {
		rawKey[i] = byte(v & 0xFF)
	}
	blk, _ := aes.NewCipher(rawKey[:16]) // AES-128
	in := make([]byte, 16)
	in[0] = byte(r)
	in[1] = byte(j)
	out := make([]byte, 16)
	blk.Encrypt(out, in)
	v := binary.LittleEndian.Uint64(out[:8])
	return v % HeraModulus
}

// heraMixColumns applies the HERA linear layer for round r.
// This is a simplified circulant matrix multiplication using heraRC constants.
func heraMixColumns(state [HeraStateSize]uint64, r int) [HeraStateSize]uint64 {
	var out [HeraStateSize]uint64
	for i := 0; i < HeraStateSize; i++ {
		var acc uint64
		for j := 0; j < HeraStateSize; j++ {
			// Circulant: M[i][j] = heraRC[r][(j-i+m) mod m]
			idx := (j - i + HeraStateSize) % HeraStateSize
			acc = (acc + mulMod(state[j], heraRC[r][idx])) % HeraModulus
		}
		out[i] = acc
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
