// Generator for the HERA known-answer vectors in cipher/hera_test.go.
//
// This file declares `package ckks_fv` and calls the UNEXPORTED reference
// plainHera (third_party/RtF-Transciphering/ckks_fv/RtF_bench_test.go:500),
// so it cannot build inside this module -- it lives under testdata/ so Go
// tooling ignores it, exactly like toy_correctness/testdata/ckks_fv_patch/.
//
// Pinned reference: KAIST RtF-Transciphering SHA
// 105fc73115b56f1d6ff357029c7682b19a6d8510.
//
// To regenerate:
//
//	cp tools/transciphering/cipher/testdata/hera_kat_gen_test.go \
//	   third_party/RtF-Transciphering/ckks_fv/
//	(cd third_party/RtF-Transciphering/ckks_fv && go test -run TestGenHeraKAT -v .)
//	rm third_party/RtF-Transciphering/ckks_fv/hera_kat_gen_test.go
//
// The modulus is read from RtFHeraParams[3] (128as), not hardcoded. The
// legacy 2^26 vector is also printed: it is the vector the previous (wrong)
// hera.go produced, kept in hera_test.go as a negative check.
package ckks_fv

import (
	"encoding/binary"
	"fmt"
	"testing"
)

func TestGenHeraKAT(t *testing.T) {
	key := make([]uint64, 16)
	nonce := make([]byte, 16*4) // 16 elements, 4 little-endian bytes each (cipher.packElems)
	for i := 0; i < 16; i++ {
		key[i] = uint64(1000 + i)
		binary.LittleEndian.PutUint32(nonce[i*4:], uint32(2000+i))
	}
	for _, c := range []struct {
		name string
		q    uint64
	}{
		{"RtFHeraParams[3].PlainModulus", RtFHeraParams[3].PlainModulus},
		{"legacy 1<<26", 1 << 26},
	} {
		fmt.Printf("%s (q=%d): %v\n", c.name, c.q, plainHera(5, nonce, key, c.q))
	}
}
