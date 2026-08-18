# PPFDaaS — Phase 7 Transciphering Scaffold

Standalone Go module for the HHE (Homomorphic Hybrid Encryption / "transciphering")
arm of PPFDaaS. This is NOT part of the TCB — it is a research-tool, like
`tools/openfhe_benchmark/`.

## What is here (MEASURED)

| Component | Status |
|---|---|
| `cipher/backend.go` — CipherBackend interface | COMPLETE |
| `cipher/shakeprf.go` — shared SHAKE256/`sampleZqx` round-key derivation (HERA + Rubato) | COMPLETE |
| `cipher/hera.go` — HERA-16, m=16, r=5, t=2^26 | COMPLETE |
| `cipher/rubato.go` — Rubato-128L, n=64, r=2, q=0x1fc0001 | COMPLETE |
| `bench/main.go` — client CPU + upload-size benchmark, `--cipher=hera\|rubato` | COMPLETE |
| `cipher/hera_test.go`, `cipher/rubato_test.go` — known-answer tests vs `ckks_fv`'s own reference | COMPLETE |
| `results/{hera,rubato}_bench_lane{1,4,8,16}.json` — per-lane benchmarks | MEASURED |
| `artifacts/hera_vs_rubato_transciphering.json` — cross-cipher comparison | MEASURED |

## What is PENDING

The RtF (Real-to-Finish) FV→CKKS scheme bridge.

**Standard Lattigo v6.2.0 does NOT include the `ckks_fv` module.** This is
correct. **Correction (this session, 2026-06-20): the bridge DOES exist and
IS importable/runnable in principle** — it is KAIST-CryptLab's own reference
implementation, `github.com/KAIST-CryptLab/RtF-Transciphering` (a fork of
Lattigo v2, module path `github.com/ldsec/lattigo/v2`, package `ckks_fv`,
containing `fv_hera.go` and `RtF_bench_test.go` with `BenchmarkRtFHera80s` /
`...80f` / `...128s` / `...128f` etc.). This was NOT found by the earlier
scaffolding session and is a better, primary-source bridge than the
`github.com/B-R-P/lattigo-ckks-fv` placeholder previously named here.

**It was attempted in this session and is PENDING for a real but different
reason: insufficient RAM in this environment, not unavailability of the
code.** `BenchmarkRtFHera80s` (the LIGHTEST configuration: 4 slots, 80-bit
security, no full-slot bootstrapping) was cloned and run with
`go test ./ckks_fv/ -bench BenchmarkRtFHera80s -benchtime=1x`. It drove this
15GB-RAM host to under 200MB free + heavy swapping mid-run (the "RtF HERA
Offline Latency" sub-benchmark, i.e. before even reaching the online
transcipher timing) and was OOM-killed before producing a number. Per this
session's instructions ("if a step is genuinely not implementable, STOP and
mark PENDING with a concrete reason"), this was not retried with a larger
config or repeated, to avoid further destabilizing the shared host. The
clone was removed (`third_party/RtF-Transciphering`, not committed).

**2026-07-27 update — build and code-path correctness now confirmed; only RAM
remains PENDING:**

This session vendored a pinned, reproducible checkout
(`third_party/fetch_rtf.sh`, commit `105fc73115b56f1d6ff357029c7682b19a6d8510`)
and ran two diagnostics that were not previously separated:

1. **Build/link: CLEAN.** `go vet ./...` and `go test -c -run '^$' .` in
   `ckks_fv/` both succeeded with **zero source changes**, on Go 1.25.0
   against the fork's `go 1.13` directive (no `replace` lines, no toolchain
   pin needed). The "dependency/API breakage" framing does not apply — see
   `third_party/BUILD_NOTES.md`.
2. **Functional correctness: PASS at toy scale.** A reduced, deliberately
   insecure toy parameter set (`LogN=10` instead of 16; every modulus reused
   verbatim from `RtFHeraParams[3]` since 1024 divides 65536 and NTT-validity
   is preserved) was run end-to-end — HERA-in-BFV transcipher → HalfBoot →
   FV→CKKS repack → a CKKS eval — and **passed** (max abs error 2.1e-05,
   peak RSS ~275 MB). This is a correctness proof only, not a security or
   performance claim. See `tools/transciphering/toy_correctness/README.md`.

**Concrete reason it remains PENDING:**

1. **RAM (sole remaining blocker):** the *real* (secure, LogN=16) parameters
   OOM-killed (exit 137) running the lightest available RtF+HERA benchmark
   in a 15 GB-RAM environment; the online phase was never reached. RAM
   anchor: ~60 GB for HERA at 80-bit cipher security
   [CONFIRMED-SOURCE: arXiv:2409.06422v1 §II]. Minimum safe tier: r7i.4xlarge
   (128 GiB) — r7i.2xlarge (64 GiB) has no margin. Cloud harness scaffolded
   at `scripts/cloud_transcipher_bench/` — not yet executed.

2. **No literature fallback:** RtF Table 5 (eprint 2020/1335) returns HTTP 403
   on all accessible mirrors (eprint, Springer, ASIACRYPT slides) — a dead end,
   not a future fetch task. Presto (arXiv 2507.00367) §V measures CLIENT-SIDE
   HERA stream-key generation on bank edge device/FPGA — not server-side HE
   evaluation or HalfBoot latency. There is no accessible literature source for
   `online_transcipher_ms` or `repacking_ms`; cloud execution is the only path.

**Naming note:** HERA-80as/128as — "80"/"128" = symmetric cipher security
level; HE ring LogN=16 (N=65,536) is the same for every HERA config
[CONFIRMED-SOURCE: ckks_fv/rtf_params.go]. See `RESEARCH_FINDINGS_v3.md` §B.

Until re-attempted successfully, the following columns in
`artifacts/hhe_breakeven.json` remain `"status": "PENDING"`:
- `online_transcipher_time_ms` (BFV eval of HERA inside vendor_server)
- `repacking_time_ms` (StC + CKKS modular reduction)
- `full_rtf_latency_ms` (end-to-end)

## Ciphers: HERA-16 and Rubato-128L

Both are now implemented and benchmarked; either can be selected via
`bench --cipher=hera|rubato`. Round-key derivation is shared
(`cipher/shakeprf.go`: SHAKE256-seeded XOF + `sampleZqx` rejection sampling,
ported verbatim from the pinned `ckks_fv` reference at
`105fc73115b56f1d6ff357029c7682b19a6d8510`) — this replaces HERA's previous
fixed round-constant table and AES-ECB-PRF key schedule, neither of which
matched the reference or ckks_fv's own SHAKE256-nonce-derived scheme. See
`cipher/hera.go` and `cipher/rubato.go`'s package doc comments for exact
file/line citations of every constant and matrix.

| Parameter | HERA-16 | Rubato-128L |
|---|---|---|
| State size | 16 | 64 |
| Rounds | 5 (matches `RtFHeraParams[3]` "128as") | 2 |
| Plaintext modulus | 2^26 | 0x1fc0001 |
| Round-key seed | SHAKE256(nonce) | SHAKE256(nonce‖counter) |
| Nonlinear layer | cube S-box | sequential Feistel-square |
| Client-side noise | none | Gaussian, σ≈1.6357 |
| Key/nonce size | 16 / 16 elements | 64 / 16 elements |
| Security | 128-bit, under current algebraic analysis | NOT covered by Grassi et al.'s (CRYPTO 2023) attack bound — see below |

**Attack record note (§7.2):**
- Rubato: Grassi et al. (CRYPTO 2023, eprint 2023/822 §6.1/7.1) give a key
  recovery attack with complexity below the claimed security level for five
  of the six Rubato variants. For Rubato-128L specifically, the paper states
  the attack's bound "cannot be established" (p.22) — i.e. Rubato-128L is
  NOT COVERED by this attack's established bound, which is the basis for
  benchmarking it here rather than the other five variants. This is not the
  same as proven secure.
- Elisabeth-4: broken (Cosseron et al.). Not used.
- HERA: round-key collision weaknesses found; current parameters (m=16, r=5,
  t=2^26) remain secure.

## Correctness gates (both required before any timing number is trusted)

- **Gate A — known-answer test**: `cipher/hera_test.go` / `rubato_test.go`
  compare this module's keystream against `ckks_fv`'s own
  `plainHera`/`plainRubato` reference for fixed inputs. **PASS**, both
  ciphers. This gate caught a real bug during implementation (Rubato's
  Feistel layer reducing mod `HeraModulus` instead of `RubatoModulus` via an
  accidentally-shared helper) before any number was measured.
- **Gate B — toy-scale HE harness**: `toy_correctness/` runs the full
  server-side path (cipher-in-BFV → HalfBoot → FV→CKKS repack → CKKS eval →
  decrypt) at `LogN 16→10`, all moduli reused. **PASS**, all three configs
  (HERA r=4/"80as", HERA r=5/"128as" — exact parity with this module's
  `HeraRounds=5` — and Rubato-128L). See `toy_correctness/README.md`.

**LIMITATION, found by review and worth stating explicitly: neither gate
validates the noise distribution.** Gate A runs Rubato at `sigma=0`
(deterministic algebra only), so a wrong noise sampler is invisible to it.
Gate B only checks that CKKS's tolerance absorbs the residual error's
*magnitude* — a wrong-but-similarly-small distribution would also pass. This
is exactly what happened during implementation: `noiseAGN` initially used a
Box-Muller approximation over `crypto/rand` instead of the reference's
`ring.GaussianSampler.AGN`, and it was never verified statistically
equivalent — invisible to both gates, caught only by review. Since Rubato's
security rests on an LWE-style assumption stated over Gaussian error, this
was a correctness bug, not a performance detail. Fixed: `noiseAGN` now calls
the reference sampler directly (see `cipher/rubato.go`'s package doc for the
`go.mod replace` mechanics), and `TestRubatoNoiseStatistics`
(`cipher/rubato_test.go`) checks empirical mean/std-dev against `sigma`
specifically to catch this class of bug in the future. **This also means
the per-element and per-record numbers below were re-measured after the
fix — they differ substantially, including in direction, from what was
first reported.** See `artifacts/hera_vs_rubato_transciphering.json`'s
`correction_note` for the full account.

## Key measured numbers (from `results/` and `artifacts/hera_vs_rubato_transciphering.json`)

Same-session, back-to-back HERA-vs-Rubato client-cipher benchmark, measured
**after** the noise-sampler fix above (AC power, powersave governor — see
the artifact for full machine state; absolute values are visibly different
from an earlier, now-superseded run at similar load average, consistent
with this host's documented volatility — ratios, not absolute values, are
the defensible output). n=200 measured + 10 warmup per point, 256
features/transaction.

| Lanes | HERA mean | Rubato mean | Ratio (Rubato/HERA) | Upload bytes (both, identical) |
|---|---|---|---|---|
| 1  | 0.164 ms | 0.079 ms | 0.48x | 1,052 (249x smaller than plain CKKS) |
| 4  | 0.388 ms | 0.280 ms | 0.72x | 4,124 |
| 8  | 0.956 ms | 0.783 ms | 0.82x | 8,220 |
| 16 | 1.641 ms | 1.150 ms | 0.70x | 16,412 (16x smaller than plain CKKS) |

**Headline: Rubato-128L client encrypt now costs ~0.48-0.82x HERA-16 —
Rubato is CHEAPER, not more expensive.** This reverses what was first
reported (~1.75-2.25x, Rubato more expensive), because that number was
measured with the wrong noise sampler (see the LIMITATION note above);
with the reference sampler, the wrong sampler's own overhead — not
Rubato's algebra — turns out to have been the dominant cost. **Upload size
is identical between the two ciphers at every lane count** — the cipher
swap only changes client CPU cost, not the wire-format size reduction vs
plain CKKS. Full per-lane JSON, correctness-gate results, and the
plain-CKKS-vs-RtF axis (still PENDING, unrelated to this cipher swap — see
`PROJECT_STATE.md`) are in `artifacts/hera_vs_rubato_transciphering.json`,
including its superseded prior version
(`hera_vs_rubato_transciphering_PRIOR_wrong_noise_sampler.json`), kept for
the record per this project's artifact rules.

These r=5/Rubato-128L numbers supersede any earlier r=4 HERA numbers
(`results/hera_bench_lane{1,4,8,16}.json`, kept for the historical record)
and any prose-only r=5 figures that previously appeared here without a
backing artifact file.

**Per-keystream-element, not per-record.** HERA-16 yields 16 usable
keystream elements per block; Rubato-128L yields 60 (64 minus the 4-element
truncation). The per-record ratio above is invariant to this (dividing both
ciphers' time by the same record size cancels it out), so it doesn't answer
"which cipher does more useful work per unit of keystream generated."
A dedicated single-block micro-benchmark (`cipher/block_bench_test.go`:
`BenchmarkHERABlock`, `BenchmarkRubatoBlock`, `BenchmarkRubatoBlockNoNoise`,
all three run together in one invocation —
`go test ./cipher/... -bench . -benchtime=100000x -count=7 -run '^$'`,
median of 7 repetitions, low/clean load — gives:

| | ns/block | ns/keystream element |
|---|---|---|
| HERA-16 | 4,028 | 251.75 |
| Rubato-128L, with noise (what `Encrypt()` does) | 11,624 | 193.73 |
| Rubato-128L, without noise (algebra only) | 8,629 | 143.82 |

Per element, Rubato costs **~0.77x HERA with noise** and **~0.57x without
it** — cheaper both ways. **A discrepancy check on the without-noise
figure, since a review pass flagged it as suspicious:** `encryptBlock`'s
`if sigma > 0` guard means the Gaussian sampler is never invoked on the
sigma=0 path, in the old buggy code or the new fixed code — that path
should have been unaffected by the sampler fix, yet an earlier report of
this table showed it moving from 248.1 to 146.1 ns/element. Investigated:
the 248.1 figure came from a single `-benchtime=2000x` run with no
repetition (low statistical power); two independent, more rigorous
re-measurements since (a 5-rep run and this 7-rep run) both converge to
~144-146 ns/element — that convergence, not 248.1, is the number to trust.
The with/without-noise gap is consistently ~35-40% across the rigorous
passes (not the ~158% the original comparison implied) — noise overhead is
real but smaller than first reported. The reversal itself (Rubato cheaper
than HERA, both metrics) is confirmed across multiple independent
methodologically-sound re-measurements and is not an artifact of this
specific discrepancy. Plausible driver of Rubato being cheaper overall, not
independently isolated this session: HERA needs `HeraRounds+1=6` round-key
rows (96 `sampleZqx` calls per 16-element block = 6/element) vs Rubato's
`RubatoRounds+1=3` rows (192 calls per 60-element block = 3.2/element) —
HERA does relatively more round-key-derivation work per unit of useful
output. See `artifacts/hera_vs_rubato_transciphering.json`'s
`per_element_normalization` for the full breakdown.

**Gate B toy-harness resource usage (the only server-side evidence available
while `vendor_server`'s BFV eval is stub-only — TOY-SCALE, LogN=10, not a
full-scale measurement).** Re-verified after the noise-sampler fix, since
the Rubato figure is the basis for a full-scale memory-risk estimate: this
harness calls `ckks_fv`'s own `plainRubato`/`ring.GaussianSampler` directly
(no import of the client `cipher` package — confirmed) so it was never
exposed to the client-side bug, and the re-run confirms this empirically,
not just by code-path argument:

| | Wall time (original / re-verified) | Peak RSS (original / re-verified range) |
|---|---|---|
| HERA r=5/"128as" | 1.26s / 2.2-2.7s (elevated load) | 293,332 / 283,028-293,332 KB |
| Rubato-128L | 1.79s / 3.9-3.9s (elevated load) | 610,808 / 610,808-654,328 KB |

The re-verification ran under an unrelated ~560%-CPU background process on
this shared host (confirmed via `ps aux`), inflating wall time — peak RSS
is much less sensitive to CPU contention and lands within a ~7% band across
4 total runs (1 original + 3 re-verification) for Rubato, ~3.5% for HERA:
**the original 610,808 KB Rubato figure is confirmed, not inflated by the
old client-side sampler** (had it been, RSS would have dropped after the
fix, not stayed flat or risen slightly). Rubato's toy-scale HalfBoot run
takes ~1.4x the wall time and ~2.1x the peak RSS of HERA's in the original,
uncontended measurement — plausibly its larger block size (64 vs 16
ciphertext states carried through the FV evaluator), not its Gaussian
noise (the homomorphic evaluator `mfvRubato` doesn't add noise at all).
Toy-scale numbers at LogN=10 don't necessarily scale linearly to the real
LogN=16 — treat as directional, not predictive. See
`multiplicative_depth_argument` below for why toy-scale wall time isn't the
deciding number either.

## Multiplicative depth (CONFIRMED-SOURCE — the argument the client-side numbers above do NOT test)

Everything measured above is **plaintext** CPU cost. FHE (homomorphic)
evaluation cost is dominated by a different thing: **multiplicative
depth** — the number of *sequentially-dependent* ciphertext-ciphertext
multiplications (each needs relinearization and consumes noise budget), not
the total volume of arithmetic a plaintext CPU performs. The two ciphers'
depth profiles, read directly from the pinned `ckks_fv` source:

- **HERA-16: 5 rounds × degree-3 cube S-box** (`x → x³ = x² · x`). Each cube
  is 2 *sequential* ciphertext multiplications
  (`fv_hera.go:355-363`: `x2 = Mul(ct,ct); Relin; x3 = Mul(x2,ct); Relin`).
  5 `cube()` calls total (4 in the round loop + 1 in finalization) × 2 mults
  each = **10 multiplicative levels**.
- **Rubato-128L: 2 rounds × degree-2 Feistel-square**
  (`state[i] += state[i-1]²`). Each `feistel()` call squares 63 *different,
  mutually-independent* state elements (`fv_rubato.go:734-741`) — these are
  logically *parallel*, not sequentially dependent, so one `feistel()` call
  costs exactly 1 multiplicative level regardless of state size. 2
  `feistel()` calls total (1 in the round loop + 1 in finalization) × 1
  level each = **2 multiplicative levels**.

By this count, Rubato-128L's homomorphic evaluation should need **~5x fewer
sequential ciphertext multiplications** than HERA-16's — this is the entire
design rationale behind Rubato's noise mechanism (Ha et al., Eurocrypt
2022): trade a few extra rounds and client-side noise sampling for a much
shallower homomorphic circuit.

**The client-side measurements in this file do not test this at all.** They
measure plaintext CPU cost — an entirely different cost model than
ciphertext multiplicative depth, dominated by things like `sampleZqx`
call volume per round (see above), not by sequential-multiplication count.
The corrected client numbers now happen to point the same direction as this
depth argument (Rubato cheaper), but that agreement should not be read as
confirming it — it could as easily be coincidental given how different the
two cost models are. **The deciding comparison — `mfvHera.Crypt` vs
`mfvRubato.Crypt` wall time, homomorphically, at full or even
properly-instrumented toy scale — is blocked on the same thing the rest of
Phase 7 is: `vendor_server`'s BFV evaluation is stub-only.** The Gate B
wall-time/RSS numbers above measure the whole HalfBoot pipeline, not
isolated `Crypt()`-call timing per cipher, so they don't isolate this
either. This remains the single most important open measurement for this
task.

## Build / run

**Prerequisite, since the Rubato noise-sampler fix above: `third_party/`
must be vendored first.** `cipher/rubato.go`'s Gaussian noise sampler
depends on `github.com/ldsec/lattigo/v2`'s `ring.GaussianSampler.AGN` — a
KAIST-CryptLab addition absent from the real public
`github.com/ldsec/lattigo/v2` module, so `go.mod` `replace`s it with a
relative path to the pinned, gitignored `third_party/RtF-Transciphering`
checkout (`replace github.com/ldsec/lattigo/v2 =>
../../third_party/RtF-Transciphering`). This means **the whole module now
fails to build without it** — previously only `toy_correctness/`'s
staged tests needed `third_party/` present; now `go build ./...` itself
does, because `cipher/rubato.go` is part of the main build, not a
test-only file.

```bash
# Requires Go 1.25+
cd ../..              # repo root
third_party/fetch_rtf.sh   # vendors third_party/RtF-Transciphering — required, see above
cd tools/transciphering
go build ./...
go run ./bench --cipher=hera --features=256 --lanes=16 --rounds=100
go run ./bench --cipher=rubato --features=256 --lanes=16 --rounds=100
go test ./cipher/...   # known-answer + statistical tests (run in-process, no server required)
```

Nothing in this repo's CI or Docker builds currently invokes `go build` on
this module (checked: neither `Dockerfile.client` nor `Dockerfile.server`
runs any Go toolchain command, and no CI workflow exists at all) — so this
prerequisite doesn't break an existing automated pipeline. It would matter
if either is ever added later.

## Threat model summary (full text: docs/spec.md §8)

1. Bank holds symmetric key `k`; provisions vendor with `Enc_BFV(k)` once
   (amortized offline cost, same provisioning protocol as Phase 1 Galois keys).
2. Online: bank encrypts 256-feature vector under `k` → ~1 KB upload.
   Vendor evaluates the chosen cipher (HERA or Rubato) homomorphically inside
   BFV, converts to CKKS via StC + modular reduction, runs the existing CKKS
   circuit unchanged.
3. AEAD (AES-128-GCM) wraps every online ciphertext — additive malleability
   of the stream cipher is neutralised.
4. Nonces are monotonic per session; replay rejection is the bank's obligation.
5. Symmetric key rotation: session-bound (new `Enc_BFV(k')` on re-provisioning).
