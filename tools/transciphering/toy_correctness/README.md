# Phase 7 toy correctness harness

Proves that the KAIST RtF-Transciphering bridge's full server-side path --
HERA-in-BFV transcipher -> HalfBoot -> FV->CKKS repack -> a CKKS eval -- runs
correctly end to end, using a reduced ring so it fits comfortably in this
15 GB-RAM host. **This is a functional-correctness check only. It is not a
security or performance claim; `online_transcipher_ms` and `repacking_ms`
remain PENDING** until the real (~60 GB) run on cloud hardware -- see
`RESEARCH_FINDINGS_v3.md` §B and `tools/transciphering/README.md`.

## What's here

- `testdata/ckks_fv_patch/rtf_toy_correctness_test.go` -- the tracked source
  of the harness. It lives under a `testdata/` directory so Go tooling
  (`go build ./...`, `go vet ./...`) never tries to compile it as part of
  the `tools/transciphering` module -- it declares `package ckks_fv` and
  only becomes buildable once staged into the vendored checkout, where the
  package-internal symbols it uses (`plainHera`, `HalfBootParameters`, ...)
  actually exist.
- `third_party/fetch_rtf.sh` clones the pinned upstream commit into
  `third_party/RtF-Transciphering/` and then copies this file to
  `third_party/RtF-Transciphering/ckks_fv/rtf_toy_correctness_test.go`.
  Running `go test` in that directory picks it up automatically.

## Toy params settled on

A deep copy of `RtFHeraParams[3]` ("128as": arcsine SineEval variant, 4
sparse slots) with only `LogN` changed:

| Field | Upstream 128as | Toy |
|---|---|---|
| `LogN` (ring degree N) | 16 (N=65,536) | **10 (N=1,024)** |
| `LogSlots` | 4 (16 slots) | 4 (16 slots) -- unchanged, matches HERA-16's m=16 state |
| ResidualModuli / KeySwitchModuli / SineEvalModuli / CoeffsToSlotsModuli / DiffScaleModulus | as in `rtf_params.go` | **byte-for-byte identical** -- reused verbatim |
| H (sparse secret Hamming weight) | 192 | 192 -- unchanged |

**Why reusing the same moduli is valid:** `checkModuli` (`ckks_fv/params.go`)
requires each `Qi`/`Pi` to satisfy `qi mod 2N == 1` (NTT-friendliness). Since
1024 divides 65536, `2*1024` divides `2*65536`, so every prime already
NTT-valid for N=65,536 is automatically NTT-valid for N=1,024 too. This
means the toy ring preserves the exact 15-level HalfBoot depth (4 CtS + 11
SineEval) with zero changes to the modulus chain -- only the ring dimension
(and therefore the security level and BSGS Galois-key cost) shrinks.

**Why LogN=10, not smaller:** `GenKeyPairSparse(H=192)` requires N > H (it
samples a ternary secret with exactly 192 non-zero coefficients out of N).
2^10=1024 was the first power-of-two comfortably above 192 with headroom for
the BSGS rotation-key machinery, and it worked immediately at negligible RAM
(see below), so no further reduction was pursued -- the goal was a value
that clears the 15 GB ceiling, not the theoretical minimum.

**This parameter set is toy-only and NOT secure.** `LogN=10` is far below
any real security target; it exists solely to exercise the code path.

## Correctness assertion

`TestRtFHeraToyCorrectness`:
1. Generates 16 known plaintext lanes of random floats in [-1, 1] (slot 0 is
   the one carried through).
2. Computes the HERA-80 (4-round) keystream in the clear via the package's
   own reference implementation (`plainHera`, `RtF_bench_test.go`) and XORs
   it into the plaintext, exactly mirroring `BenchmarkRtFHera80as`'s setup
   (paramIndex=3, numRound=4, radix=0, fullCoeffs=false).
3. Server-side: homomorphically evaluates HERA inside BFV
   (`MFVHera.Crypt`), `SlotsToCoeffs`, subtracts the FV keystream from the
   uploaded ciphertext to strip the stream cipher, `HalfBoot`s it into the
   CKKS domain with repacking.
4. Runs a trivial CKKS eval on the repacked ciphertext: `2*x + 1` (via
   `MultByConstNew` + `Rescale` + `AddConst`), to exercise "the existing
   CKKS circuit unchanged" step from `docs/spec.md` §8, not just decode the
   raw repack.
5. Decrypts, decodes, and asserts `max(|got[i] - (2*data[0][i]+1)|)` across
   all 16 slots is within a **tolerance of 5e-2**.

## Result

```
=== RUN   TestRtFHeraToyCorrectness
    rtf_toy_correctness_test.go:193: toy RtF+HalfBoot+CKKS-eval correctness: LogN=10, slots=16, max|got-want|=2.124082e-05 (tolerance=5.000000e-02)
--- PASS: TestRtFHeraToyCorrectness (1.37s)
PASS
```

**PASS.** Max absolute error 2.124082e-05, four orders of magnitude inside
the 5e-2 tolerance.

**Peak RSS: 275,060 KB (~275 MB).** Measured via `/proc/<pid>/status`
`VmHWM` polling every 0.3s (GNU `time -v` is not installed on this host and
there is no root access to install it -- this is a documented substitution,
not a fabricated number; the polling wrapper is disposable shell, not
committed). 275 MB is roughly 1.8% of the 15 GB budget, with no swap
pressure observed.

## Reproduce

```bash
third_party/fetch_rtf.sh
cd third_party/RtF-Transciphering/ckks_fv
go test -run TestRtFHeraToyCorrectness -v .
```
