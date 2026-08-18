# Phase 7 toy correctness harness

Proves that the KAIST RtF-Transciphering bridge's full server-side path --
HERA-in-BFV or Rubato-in-BFV transcipher -> HalfBoot -> FV->CKKS repack -> a
CKKS eval -- runs correctly end to end, using a reduced ring so it fits
comfortably in this host. **This is a functional-correctness check only. It
is not a security or performance claim; `online_transcipher_ms` and
`repacking_ms` remain PENDING** until the real (~60 GB) run on cloud
hardware -- see `RESEARCH_FINDINGS_v3.md` §B and
`tools/transciphering/README.md`.

Three harnesses, all staged into the same vendored `ckks_fv` package by
`third_party/fetch_rtf.sh`:

| Test | Cipher config | Matches |
|---|---|---|
| `TestRtFHeraToyCorrectness` | HERA, numRound=4, `HeraModDownParams80`, radix=0 | `BenchmarkRtFHera80as` |
| `TestRtFHera128asToyCorrectness` | HERA, numRound=5, `HeraModDownParams128`, radix=2 | `BenchmarkRtFHera128as` -- exact parity with `cipher/hera.go`'s `HeraRounds=5` |
| `TestRtFRubato128LToyCorrectness` | Rubato128L, numRound=2, `RubatoModDownParams[RUBATO128L]`, radix=2, full-coefficient packing | `benchmarkRtFRubato(RUBATO128L)` -- matches `cipher/rubato.go` |

The first ("80as") predates the HERA→Rubato swap and is kept as-is; the
other two were added for this task, since neither an exact-round-count HERA
harness nor any Rubato harness previously existed here.

## What's here

- `testdata/ckks_fv_patch/*.go` -- the tracked source of all three
  harnesses. They live under a `testdata/` directory so Go tooling
  (`go build ./...`, `go vet ./...`) never tries to compile them as part of
  the `tools/transciphering` module -- each declares `package ckks_fv` and
  only becomes buildable once staged into the vendored checkout, where the
  package-internal symbols they use (`plainHera`, `plainRubato`,
  `HalfBootParameters`, ...) actually exist.
- `third_party/fetch_rtf.sh` clones the pinned upstream commit into
  `third_party/RtF-Transciphering/` and then copies every file in this
  directory to `third_party/RtF-Transciphering/ckks_fv/`. Running `go test`
  in that directory picks them up automatically.

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

`TestRtFHera128asToyCorrectness` and `TestRtFRubato128LToyCorrectness` follow
the same structure at their respective configs (see the table above);
Rubato's additionally exercises the Gaussian noise step (`sigma>0` in
`plainRubato`) end to end -- the noise is real, client-side-only entropy
(see `cipher/rubato.go`'s package doc), so this is the first check that it
doesn't blow the HalfBoot error tolerance.

## Result

```
=== RUN   TestRtFHeraToyCorrectness
    rtf_toy_correctness_test.go:193: toy RtF+HalfBoot+CKKS-eval correctness: LogN=10, slots=16, max|got-want|=2.124082e-05 (tolerance=5.000000e-02)
--- PASS: TestRtFHeraToyCorrectness (1.37s)
=== RUN   TestRtFHera128asToyCorrectness
    rtf_toy_correctness_hera128as_test.go:173: toy RtF+HalfBoot+CKKS-eval correctness (HERA-128as, r=5): LogN=10, slots=16, max|got-want|=1.390409e-05 (tolerance=5.000000e-02)
--- PASS: TestRtFHera128asToyCorrectness (1.14s)
=== RUN   TestRtFRubato128LToyCorrectness
    rtf_toy_correctness_rubato128l_test.go:204: toy RtF+HalfBoot+CKKS-eval correctness (Rubato-128L, r=2, sigma=1.635663349645874): LogN=10, slots=512, max|got-want|=3.419122e-05 (tolerance=5.000000e-02)
--- PASS: TestRtFRubato128LToyCorrectness (1.63s)
PASS
```

**PASS, all three.** Max absolute errors 1.4e-05 to 3.4e-05 (varies slightly
run-to-run: both harnesses use fresh random plaintext/nonces per run, and
Rubato's additionally includes real Gaussian noise draws), all four-plus
orders of magnitude inside the 5e-2 tolerance. Note Rubato's `slots=512`
vs HERA's `slots=16` -- Rubato's only CKKS/RtF param set ("128af") uses
full-coefficient packing, not HERA's sparse 4-slot ("as") packing; this
is a property of the reference's own param sets, not a change made here.

**Peak RSS (original "80as" run): 275,060 KB (~275 MB).** Measured via
`/proc/<pid>/status` `VmHWM` polling every 0.3s (GNU `time -v` is not
installed on this host and there is no root access to install it -- this is
a documented substitution, not a fabricated number; the polling wrapper is
disposable shell, not committed). 275 MB is roughly 1.8% of the 15 GB
budget, with no swap pressure observed. RSS was not re-measured for the two
new harnesses this pass -- both ran in low single-digit seconds on the same
host with no observed swap, consistent with the same order of magnitude,
but that's an observation, not a measured VmHWM figure; treat as
UNVERIFIED until polled the same way.

## Reproduce

```bash
third_party/fetch_rtf.sh
cd third_party/RtF-Transciphering/ckks_fv
go test -run 'TestRtFHeraToyCorrectness|TestRtFHera128asToyCorrectness|TestRtFRubato128LToyCorrectness' -v .
```
