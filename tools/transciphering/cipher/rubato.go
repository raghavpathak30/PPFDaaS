package cipher

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"sync"

	"github.com/ldsec/lattigo/v2/ring"
	"github.com/ldsec/lattigo/v2/utils"
)

// Rubato128L implements CipherBackend using the Rubato-128L stream cipher
// (Ha et al., "Rubato: Noisy Ciphers for Approximate Homomorphic Encryption",
// Eurocrypt 2022; eprint 2022/537), the noisy-keystream sibling of HERA used
// in the RtF transciphering framework.
//
// Parameters (third_party/RtF-Transciphering/ckks_fv/fv_rubato.go:48-53,
// RubatoParams[RUBATO128L], at the pinned SHA
// 105fc73115b56f1d6ff357029c7682b19a6d8510):
//   - State (block) size n = 64
//   - Rounds r = 2
//   - Plaintext modulus q = 0x1fc0001 (NOT a power of two -- an NTT-friendly
//     prime required by the FV/CKKS side of the RtF framework)
//   - Gaussian noise std dev sigma = 1.6356633496458739795537788457309656607510203877762320964302959
//   - Output length: n-4 = 60 elements (the last 4 state elements are
//     internal-diffusion-only and discarded)
//
// Variant choice: Grassi et al. (CRYPTO 2023, eprint 2023/822 §6.1/7.1, p.22)
// give a key-recovery attack with complexity below the claimed security
// level for five of the six Rubato variants. For Rubato-128L specifically,
// the paper states the attack's bound "cannot be established" -- i.e.
// Rubato-128L is NOT COVERED by this attack's established bound (not the
// same as proven secure). See cipher/backend.go's package doc.
//
// Round-key derivation matches ckks_fv exactly: SHAKE256(nonce || counter)
// seeds an XOF, and sampleZqx-drawn scalars are multiplied against each key
// element per round (shakeprf.go's roundKeys, shared with hera.go), ported
// from fv_rubato.go:165-179 and RtF_bench_test.go:615-634. This is the same
// general scheme HERA uses; the only derivation difference is that Rubato's
// XOF is also seeded with a counter (HERA's is nonce-only).
//
// Rubato's counter is a real part of its spec (unlike HERA's block index,
// which is this module's own extension -- see hera.go) and doubles as this
// module's multi-block mechanism: block index 0, 1, 2, ... is packed as an
// 8-byte big-endian counter for every block, including block 0.
//
// Gaussian noise: added once, in the finalization step, before the last
// AddRoundKey (RtF_bench_test.go:658-660, rubatoAddGaussianNoise:766-769).
// noiseAGN below calls ldsec/lattigo's ring.GaussianSampler.AGN directly --
// the exact primitive the reference uses, via a `replace` in go.mod pointing
// at the pinned third_party/RtF-Transciphering checkout (that fork's module
// path is github.com/ldsec/lattigo/v2; AGN is a KAIST-CryptLab addition not
// present in the real upstream github.com/ldsec/lattigo/v2, so this can't
// be satisfied by the public module alone -- third_party/fetch_rtf.sh must
// be run before this module builds). An earlier version of this file
// approximated the noise via Box-Muller over crypto/rand-drawn uniforms;
// that is a DIFFERENT distribution than AGN's discrete Gaussian and was
// never verified statistically equivalent to it -- Rubato's security rests
// on an LWE-style assumption stated over Gaussian error, so an unverified
// substitute distribution is a correctness bug, not a performance detail.
// Noise is still randomized by design (the reference's own plainRubato
// draws a freshly-seeded PRNG per call and is non-deterministic
// run-to-run), so Gate A's known-answer test compares with sigma=0 (the
// deterministic algebra only); see cipher/rubato_test.go for that gate's
// explicit statement that it cannot and does not validate the noise
// distribution, and TestRubatoNoiseStatistics for what does.
//
// mfvRubato, the homomorphic (server-side) evaluator in ckks_fv, does NOT
// add this noise (grepped fv_rubato.go: zero references to Sigma/Gaussian).
// This is intentional, not a gap: the client encrypts with the noisy
// keystream; the server homomorphically evaluates the noise-free keystream
// and subtracts it; the residual client-side noise becomes CKKS
// approximation error that HalfBoot's tolerance is designed to absorb.
const (
	RubatoBlockSize  = 64                // n
	RubatoRounds     = 2                 // r
	RubatoModulus    = uint64(0x1fc0001) // q (NOT a power of two)
	RubatoOutputSize = RubatoBlockSize - 4
	RubatoSigma      = 1.6356633496458739795537788457309656607510203877762320964302959

	rubatoKeyElems   = RubatoBlockSize
	rubatoNonceElems = 16
)

// Rubato128L is the Rubato stream cipher with n=64, r=2, q=0x1fc0001.
type Rubato128L struct{}

// NewRubato128L returns a new Rubato-128L backend.
func NewRubato128L() CipherBackend { return Rubato128L{} }

func (Rubato128L) Name() string    { return "Rubato-128L" }
func (Rubato128L) KeySize() int    { return rubatoKeyElems }
func (Rubato128L) NonceSize() int  { return rubatoNonceElems }
func (Rubato128L) Modulus() uint64 { return RubatoModulus }

// OnlineCiphertextBytes: n uint64 elements × 4 bytes each (q is a ~25-bit
// prime, fits in 4 bytes) + 28 bytes AEAD overhead (12 nonce + 16 tag).
func (Rubato128L) OnlineCiphertextBytes(n int) int {
	return n*4 + 28
}

// Encrypt encrypts plaintext (Z_q elements) using Rubato-128L + AES-GCM
// wrapper. key must be rubatoKeyElems elements; nonce must be
// rubatoNonceElems elements.
func (r Rubato128L) Encrypt(key, nonce, plaintext []uint64) ([]uint64, error) {
	if len(key) != rubatoKeyElems {
		return nil, fmt.Errorf("%w: want %d elements, got %d", ErrInvalidKey, rubatoKeyElems, len(key))
	}
	if len(nonce) != rubatoNonceElems {
		return nil, fmt.Errorf("%w: want %d elements, got %d", ErrInvalidNonce, rubatoNonceElems, len(nonce))
	}

	ks := r.keystream(key, nonce, RubatoSigma, len(plaintext))

	ct := make([]uint64, len(plaintext))
	for i := range plaintext {
		ct[i] = (plaintext[i] + ks[i]) % RubatoModulus
	}

	raw := make([]byte, len(ct)*4)
	for i, v := range ct {
		binary.LittleEndian.PutUint32(raw[i*4:], uint32(v))
	}

	aesKey := rubatoAEADKey(key)
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
	sealed := gcm.Seal(gcmNonce, gcmNonce, raw, nil)

	out := make([]uint64, len(sealed))
	for i, b := range sealed {
		out[i] = uint64(b)
	}
	return out, nil
}

// rubatoAEADKey derives the AES-128 key for the outer AEAD wrapper from the
// Rubato key material via SHA-256 -- this module's own application-level
// design, not part of Rubato's spec (mirrors heraAEADKey in hera.go).
func rubatoAEADKey(key []uint64) []byte {
	sum := sha256.Sum256(packElems(key))
	return sum[:16]
}

// EvalKeyExpansion returns the per-round key vectors (plaintext path) for
// block 0 (counter=0) of the given nonce. heCtx must be nil in the plaintext
// benchmark.
func (r Rubato128L) EvalKeyExpansion(key, nonce []uint64, heCtx interface{}) ([][]uint64, error) {
	if heCtx != nil {
		return nil, fmt.Errorf("PENDING: HE-path EvalKeyExpansion requires vendor_server integration; heCtx must be nil in the plaintext benchmark")
	}
	if len(key) != rubatoKeyElems {
		return nil, fmt.Errorf("%w: want %d, got %d", ErrInvalidKey, rubatoKeyElems, len(key))
	}
	if len(nonce) != rubatoNonceElems {
		return nil, fmt.Errorf("%w: want %d, got %d", ErrInvalidNonce, rubatoNonceElems, len(nonce))
	}
	xof := newShake256()
	xof.Write(packElems(nonce))
	xof.Write(blockCounterBytes(0))
	return roundKeys(xof, RubatoRounds, RubatoBlockSize, key, RubatoModulus), nil
}

// --- Internal helpers ---------------------------------------------------------

// blockCounterBytes packs a block index as Rubato's 8-byte big-endian
// counter input (fv_rubato.go's counter []byte, always present -- unlike
// HERA where a block index is this module's own extension).
func blockCounterBytes(blockIdx uint64) []byte {
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], blockIdx)
	return b[:]
}

// keystream generates len(pt) keystream elements (RubatoOutputSize per
// block), incrementing the counter per block.
func (r Rubato128L) keystream(key, nonce []uint64, sigma float64, n int) []uint64 {
	ks := make([]uint64, 0, n)
	blockIdx := uint64(0)
	for len(ks) < n {
		blk := r.encryptBlock(key, nonce, blockIdx, sigma)
		ks = append(ks, blk[:min(RubatoOutputSize, n-len(ks))]...)
		blockIdx++
	}
	return ks[:n]
}

// encryptBlock computes one Rubato128L keystream block (RubatoOutputSize
// elements) for the given nonce, block index (Rubato's counter), and noise
// std dev. sigma=0 skips the noise step entirely (matches plainRubato's own
// `if sigma > 0` guard, RtF_bench_test.go:658) and is what Gate A's
// known-answer test uses; Encrypt always calls this with sigma=RubatoSigma.
// Ported from plainRubato, RtF_bench_test.go:615-667.
func (r Rubato128L) encryptBlock(key, nonce []uint64, blockIdx uint64, sigma float64) [RubatoOutputSize]uint64 {
	xof := newShake256()
	xof.Write(packElems(nonce))
	xof.Write(blockCounterBytes(blockIdx))
	rks := roundKeys(xof, RubatoRounds, RubatoBlockSize, key, RubatoModulus)

	// Fixed public initial state (ic = 1..blocksize). RtF_bench_test.go:636-638.
	var state [RubatoBlockSize]uint64
	for i := 0; i < RubatoBlockSize; i++ {
		state[i] = uint64(i + 1)
	}

	// Initial AddRoundKey. RtF_bench_test.go:640-643.
	for i := 0; i < RubatoBlockSize; i++ {
		state[i] = (state[i] + rks[0][i]) % RubatoModulus
	}

	// Rounds 1..RubatoRounds-1: linLayer, feistel, AddRoundKey.
	// RtF_bench_test.go:645-652.
	for round := 1; round < RubatoRounds; round++ {
		state = rubatoLinLayer64(state)
		state = rubatoFeistel(state)
		for i := 0; i < RubatoBlockSize; i++ {
			state[i] = (state[i] + rks[round][i]) % RubatoModulus
		}
	}

	// Finalization: linLayer, feistel, linLayer, [noise], AddRoundKey.
	// RtF_bench_test.go:654-663.
	state = rubatoLinLayer64(state)
	state = rubatoFeistel(state)
	state = rubatoLinLayer64(state)
	if sigma > 0 {
		noiseAGN(state[:], RubatoModulus, sigma)
	}
	for i := 0; i < RubatoBlockSize; i++ {
		state[i] = (state[i] + rks[RubatoRounds][i]) % RubatoModulus
	}

	// Output truncation: drop the last 4 state elements. RtF_bench_test.go:664.
	var out [RubatoOutputSize]uint64
	copy(out[:], state[:RubatoOutputSize])
	return out
}

// rubatoLinLayer64 applies Rubato-128L's fixed linear layer (blocksize 64):
// MixColumns then MixRows, each an 8-tap [5,3,4,3,6,2,1,1] circulant over an
// 8x8 grid. Ported verbatim from RtF_bench_test.go:719-747.
func rubatoLinLayer64(state [RubatoBlockSize]uint64) [RubatoBlockSize]uint64 {
	var buf [RubatoBlockSize]uint64
	for col := 0; col < 8; col++ {
		for row := 0; row < 8; row++ {
			v := 5 * state[row*8+col]
			v += 3 * state[((row+1)%8)*8+col]
			v += 4 * state[((row+2)%8)*8+col]
			v += 3 * state[((row+3)%8)*8+col]
			v += 6 * state[((row+4)%8)*8+col]
			v += 2 * state[((row+5)%8)*8+col]
			v += state[((row+6)%8)*8+col]
			v += state[((row+7)%8)*8+col]
			buf[row*8+col] = v % RubatoModulus
		}
	}
	var out [RubatoBlockSize]uint64
	for row := 0; row < 8; row++ {
		for col := 0; col < 8; col++ {
			v := 5 * buf[row*8+col]
			v += 3 * buf[row*8+(col+1)%8]
			v += 4 * buf[row*8+(col+2)%8]
			v += 3 * buf[row*8+(col+3)%8]
			v += 6 * buf[row*8+(col+4)%8]
			v += 2 * buf[row*8+(col+5)%8]
			v += buf[row*8+(col+6)%8]
			v += buf[row*8+(col+7)%8]
			out[row*8+col] = v % RubatoModulus
		}
	}
	return out
}

// rubatoFeistel applies Rubato's sequential Feistel-square nonlinear layer:
// state[i] += state[i-1]^2 for i = 1..blocksize-1, using the pre-update
// value of state[i-1]. Ported verbatim from RtF_bench_test.go:753-764.
func rubatoFeistel(state [RubatoBlockSize]uint64) [RubatoBlockSize]uint64 {
	buf := state
	var out [RubatoBlockSize]uint64
	out[0] = buf[0]
	for i := 1; i < RubatoBlockSize; i++ {
		// buf[i-1] < RubatoModulus (~2^25); squared < 2^50, fits uint64 --
		// NOT hera.go's mulMod, which reduces mod HeraModulus (2^26), the
		// wrong modulus for Rubato (0x1fc0001).
		out[i] = (buf[i] + buf[i-1]*buf[i-1]) % RubatoModulus
	}
	return out
}

// rubatoGaussianSampler is the exact primitive ckks_fv's own plainRubato
// uses (RtF_bench_test.go:621-625:
//   prng, err := utils.NewPRNG()
//   gaussianSampler := ring.NewGaussianSampler(prng)
// ), not a re-implementation. An earlier version of this file approximated
// the noise via Box-Muller over crypto/rand-drawn uniforms; that was a
// DIFFERENT distribution than the reference's discrete Gaussian sampler and
// was never verified statistically equivalent -- Rubato's security rests on
// an LWE-style assumption stated over Gaussian error, so an unverified
// approximation is a correctness bug, not a performance detail (see
// toy_correctness/README.md and this package's tests for why neither
// correctness gate could have caught it). Allocated once, lazily, and
// reused across calls -- NOT per Encrypt()/per block -- matching the
// reference's own single-sampler-per-run usage and avoiding per-call
// allocation in the hot path. NOT goroutine-safe (the underlying sampler
// holds mutable internal PRNG buffer state, same as upstream); this
// module's callers (bench/main.go, the test suite) are single-threaded.
var (
	rubatoGaussianSamplerOnce sync.Once
	rubatoGaussianSampler     *ring.GaussianSampler
)

func getRubatoGaussianSampler() *ring.GaussianSampler {
	rubatoGaussianSamplerOnce.Do(func() {
		prng, err := utils.NewPRNG()
		if err != nil {
			panic(err)
		}
		rubatoGaussianSampler = ring.NewGaussianSampler(prng)
	})
	return rubatoGaussianSampler
}

// noiseAGN adds discrete Gaussian noise to state in place via ckks_fv's own
// ring.GaussianSampler.AGN -- ported to call the identical primitive, not
// reimplement it. Matches rubatoAddGaussianNoise exactly
// (RtF_bench_test.go:766-769: bound := int(6*sigma); gaussianSampler.AGN(
// state, plainModulus, sigma, bound)). AGN itself only touches
// state[:len(state)-4] internally (ring_sampler_gaussian.go's AGN:
// outputsize := len(state)-4) -- callers must pass the full
// RubatoBlockSize-length state, matching the reference's call convention
// (noise is added before the blocksize-4 output truncation, not after).
func noiseAGN(state []uint64, q uint64, sigma float64) {
	bound := int(6 * sigma)
	getRubatoGaussianSampler().AGN(state, q, sigma, bound)
}
