package cipher

import "testing"

// These isolate pure keystream-generation cost (encryptBlock only, no AEAD
// wrap, no batching over multiple blocks) for exactly one block per cipher,
// so cost-per-keystream-element can be compared fairly: HERA yields
// HeraStateSize=16 usable elements per block; Rubato-128L yields
// RubatoOutputSize=60 (RubatoBlockSize=64 minus the 4-element truncation).
// Unlike online_encrypt_ms in results/*_bench.json (which includes AES-GCM
// AEAD overhead and, for n not a multiple of the block size, some wasted
// tail keystream material), these report exactly one block's cost with
// nothing else mixed in. Run all three together, in one invocation, for a
// same-session comparison (this host's absolute timings are noisy enough
// between separate invocations -- see PROJECT_STATE.md -- that comparing
// numbers from different `go test -bench` runs is not defensible even for
// the same binary/code):
//   go test ./cipher/... -bench . -benchtime=100000x -count=5 -run '^$'
// BenchmarkRubatoBlockNoNoise exists specifically so the with/without-noise
// comparison is measured in the same invocation as BenchmarkHERABlock and
// BenchmarkRubatoBlock, not reconstructed from separate sessions.

var sinkState [HeraStateSize]uint64
var sinkOut [RubatoOutputSize]uint64

func BenchmarkHERABlock(b *testing.B) {
	key := make([]uint64, HeraStateSize)
	nonce := make([]uint64, heraNonceElems)
	for i := 0; i < HeraStateSize; i++ {
		key[i] = uint64(1000 + i)
	}
	for i := 0; i < heraNonceElems; i++ {
		nonce[i] = uint64(2000 + i)
	}
	h := HERA16{}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		sinkState = h.encryptBlock(key, nonce, 0)
	}
}

func BenchmarkRubatoBlock(b *testing.B) {
	key := make([]uint64, RubatoBlockSize)
	nonce := make([]uint64, rubatoNonceElems)
	for i := 0; i < RubatoBlockSize; i++ {
		key[i] = uint64(1000 + i)
	}
	for i := 0; i < rubatoNonceElems; i++ {
		nonce[i] = uint64(2000 + i)
	}
	r := Rubato128L{}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		sinkOut = r.encryptBlock(key, nonce, 0, RubatoSigma)
	}
}

// BenchmarkRubatoBlockNoNoise is identical to BenchmarkRubatoBlock except
// sigma=0, which skips the noiseAGN call entirely (encryptBlock's
// `if sigma > 0` guard) -- this path never touches the Gaussian sampler,
// with either the old approximation or the current reference primitive.
func BenchmarkRubatoBlockNoNoise(b *testing.B) {
	key := make([]uint64, RubatoBlockSize)
	nonce := make([]uint64, rubatoNonceElems)
	for i := 0; i < RubatoBlockSize; i++ {
		key[i] = uint64(1000 + i)
	}
	for i := 0; i < rubatoNonceElems; i++ {
		nonce[i] = uint64(2000 + i)
	}
	r := Rubato128L{}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		sinkOut = r.encryptBlock(key, nonce, 0, 0)
	}
}
