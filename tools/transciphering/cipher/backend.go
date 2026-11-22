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
// Cipher choice (§7.2): HERA-16 and Rubato128L, both benchmarked.
//   - Rubato: Grassi et al. (CRYPTO 2023, eprint 2023/822) give a key
//     recovery attack below the claimed security level for five of the six
//     Rubato variants (80S/M/L, 128S/M). It works only for composite q with
//     suitable small factors (§7.1 Assumptions 1-2; Table 4: 25%-58.47% of q
//     per variant). They report no attack on Rubato-128L: m_min > m_max, and
//     0% of q is vulnerable (§7.1, p.26; Table 4, p.28). Prime q is immune to
//     the attack (§8.1, p.29-30), and q = 0x1fc0001 is prime. This is the
//     absence of an attack, not a proof of security. It is the basis for
//     benchmarking Rubato-128L here rather than the other five variants.
//   - Elisabeth-4: broken (Cosseron et al.).
//   - HERA-16 (m=16, r=5, t=0x1fc0001): the designers recommend r=5 for
//     128-bit cipher security, assuming t > 2^16 (Cho et al., ASIACRYPT 2021,
//     eprint 2020/1335 full version, §4.3 Table 3; Table 4 Par-128a and
//     Par-128as both have r=5). The original HERA spec has r=4 only for its
//     80-bit sets (Table 4, Par-80*), not for 128-bit. RtFHeraParams[3]
//     ("128as") holds HE parameters only. The paper says all its sets use
//     128-bit HE parameters (§5.1), and ckks_fv runs this one entry with r=4
//     in BenchmarkRtFHera80as and r=5 in BenchmarkRtFHera128as
//     (RtF_bench_test.go:31,51). No third-party analysis of HERA is cited in
//     this repo.
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

	// KeySize returns the required key length in Z_t elements.
	KeySize() int

	// NonceSize returns the required nonce length in Z_t elements.
	NonceSize() int

	// Modulus returns the plaintext modulus t (all operations are mod t).
	Modulus() uint64

	// Encrypt applies the stream cipher to plaintext, producing a
	// ciphertext of identical length. Both slices must be over Z_t.
	// Nonce must be NonceSize() elements; key must be KeySize() elements.
	Encrypt(key, nonce, plaintext []uint64) ([]uint64, error)

	// EvalKeyExpansion returns the per-round key schedule expanded from
	// the key material, for block 0 of the given nonce (i.e. the same round
	// keys Encrypt would use for the first block). In the plaintext path
	// this is a direct evaluation; in the HE path the key is an encrypted
	// BFV ciphertext and the round keys are evaluated homomorphically.
	// nonce seeds the round-key derivation (both HERA and Rubato derive
	// round keys from a SHAKE256-based XOF — see cipher/shakeprf.go) and
	// must be NonceSize() elements.
	//
	// PENDING (RtF FV->CKKS path): the HE context parameter is nil in
	// the plaintext benchmark; a non-nil value would carry the BFV
	// evaluator once vendor_server integration lands.
	// See README.md "PENDING: RtF Scheme Bridge".
	EvalKeyExpansion(key, nonce []uint64, heCtx interface{}) ([][]uint64, error)

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
