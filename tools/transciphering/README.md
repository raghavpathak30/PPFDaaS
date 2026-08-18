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

## Key measured numbers (from `results/` and `artifacts/hera_vs_rubato_transciphering.json`)

Same-session, back-to-back HERA-vs-Rubato client-cipher benchmark (AC power,
powersave governor, load avg ~2.5 — see the artifact for full machine
state). n=200 measured + 10 warmup per point, 256 features/transaction.

| Lanes | HERA mean | Rubato mean | Ratio (Rubato/HERA) | Upload bytes (both, identical) |
|---|---|---|---|---|
| 1  | 0.082 ms | 0.172 ms | 2.10x | 1,052 (249x smaller than plain CKKS) |
| 4  | 0.390 ms | 0.877 ms | 2.25x | 4,124 |
| 8  | 0.693 ms | 1.231 ms | 1.78x | 8,220 |
| 16 | 1.213 ms | 2.494 ms | 2.06x | 16,412 (16x smaller than plain CKKS) |

**Headline: Rubato-128L client encrypt costs ~1.75-2.25x HERA-16**, fairly
stable across the lane sweep. **Upload size is identical between the two
ciphers at every lane count** — the cipher swap only changes client CPU
cost, not the wire-format size reduction vs plain CKKS. Absolute values
carry the powersave/AC caveat per `PROJECT_STATE.md`; the ratio is the
defensible number. Full per-lane JSON, correctness-gate results, and the
plain-CKKS-vs-RtF axis (still PENDING, unrelated to this cipher swap — see
`PROJECT_STATE.md`) are in `artifacts/hera_vs_rubato_transciphering.json`.

These r=5/Rubato-128L numbers supersede any earlier r=4 HERA numbers
(`results/hera_bench_lane{1,4,8,16}.json`, kept for the historical record)
and any prose-only r=5 figures that previously appeared here without a
backing artifact file.

## Build / run

```bash
# Requires Go 1.25+
cd tools/transciphering
go build ./...
go run ./bench --cipher=hera --features=256 --lanes=16 --rounds=100
go run ./bench --cipher=rubato --features=256 --lanes=16 --rounds=100
go test ./cipher/...   # known-answer tests (run in-process, no server required)
```

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
