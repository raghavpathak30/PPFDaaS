// Package cipher defines the CipherBackend interface for HHE stream ciphers
// used in the transciphering arm of PPFDaaS (Phase 7).
//
// Threat model context (docs/spec.md §8):
//   - Bank holds symmetric key k; provisions vendor with Enc_BFV(k) once at
//     session start (amortized offline cost).
//   - Online: bank encrypts feature vector under k, uploads ~1 KB ciphertext
//     (vs ~256 KB CKKS standard). Vendor evaluates the cipher homomorphically
//     inside BFV, converts to CKKS via StC+modular-reduction (PENDING), then
//     runs the existing SEAL CKKS inference circuit unchanged.
//   - AEAD (AES-128-GCM) wraps every online ciphertext to defeat the additive
//     malleability of stream ciphers. Nonces are monotonic per session.
//
// Cipher choice (§7.2): HERA-16, post-attack parameters.
//   - Rubato: broken (Grassi et al. CRYPTO 2023, 5/6 family members).
//   - Elisabeth-4: broken (Cosseron et al.).
//   - HERA-16 (m=16, r=4, t=2^26): algebraic analysis found round-key
//     collisions but current parameters remain secure. Best-studied
//     HHE cipher for CKKS-adjacent workflows.
package cipher

import "errors"

// ErrInvalidKey is returned when a key has wrong length for the backend.
var ErrInvalidKey = errors.New("cipher: invalid key length")

// ErrInvalidNonce is returned when a nonce has wrong length.
var ErrInvalidNonce = errors.New("cipher: invalid nonce length")

// CipherBackend is the pluggable symmetric cipher interface.
// Both Encrypt and EvalKeyExpansion operate on uint64 vectors whose elements
// are in [0, Modulus). The domain matches the BFV/BGV plaintext space so that
// cipher evaluation inside HE requires no re-encoding.
type CipherBackend interface {
	// Name returns the cipher identifier (e.g. "HERA-16").
	Name() string

	// KeySize returns the required key length in bytes.
	KeySize() int

	// NonceSize returns the required nonce length in bytes.
	NonceSize() int

	// Modulus returns the plaintext modulus t (all operations are mod t).
	Modulus() uint64

	// Encrypt applies the stream cipher to plaintext, producing a
	// ciphertext of identical length. Both slices must be over Z_t.
	// Nonce must be NonceSize() bytes; key must be KeySize() bytes.
	Encrypt(key, nonce, plaintext []uint64) ([]uint64, error)

	// EvalKeyExpansion returns the per-round key schedule expanded from
	// the key material. In the plaintext path this is a direct evaluation;
	// in the HE path the key is an encrypted BFV ciphertext and the round
	// keys are evaluated homomorphically.
	//
	// PENDING (RtF FV->CKKS path): the HE context parameter is nil in
	// the plaintext benchmark; a non-nil value would carry the BFV
	// evaluator once the KAIST ckks_fv scheme bridge is available.
	// See README.md "PENDING: RtF Scheme Bridge".
	EvalKeyExpansion(key []uint64, heCtx interface{}) ([][]uint64, error)

	// OnlineCiphertextBytes returns the byte length of one online
	// ciphertext for n uint64 plaintext elements (upload-size formula).
	OnlineCiphertextBytes(n int) int
}

// QuantizeFeatures converts float64 features to uint64 mod t using
// scale = 2^scaleBits. Elements are clamped to [0, t).
// This is the bank-side quantization step before Encrypt().
func QuantizeFeatures(features []float64, scaleBits uint, t uint64) []uint64 {
	scale := float64(uint64(1) << scaleBits)
	out := make([]uint64, len(features))
	for i, f := range features {
		v := int64(f * scale)
		// two's complement mod t: map negatives to [t/2, t)
		out[i] = uint64((v%int64(t)+int64(t))) % t
	}
	return out
}

// DequantizeFeatures is the inverse: converts uint64 mod t back to float64.
// Values in (t/2, t) are treated as negative (two's complement).
func DequantizeFeatures(vals []uint64, scaleBits uint, t uint64) []float64 {
	scale := float64(uint64(1) << scaleBits)
	half := t / 2
	out := make([]float64, len(vals))
	for i, v := range vals {
		s := int64(v)
		if v > half {
			s = int64(v) - int64(t)
		}
		out[i] = float64(s) / scale
	}
	return out
}
