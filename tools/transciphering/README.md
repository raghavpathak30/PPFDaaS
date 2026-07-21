# PPFDaaS — Phase 7 Transciphering Scaffold

Standalone Go module for the HHE (Homomorphic Hybrid Encryption / "transciphering")
arm of PPFDaaS. This is NOT part of the TCB — it is a research-tool, like
`tools/openfhe_benchmark/`.

## What is here (MEASURED)

| Component | Status |
|---|---|
| `cipher/backend.go` — CipherBackend interface | COMPLETE |
| `cipher/hera.go` — HERA-16, m=16, r=4, t=2^26 | COMPLETE |
| `bench/main.go` — client CPU + upload-size benchmark | COMPLETE |
| `results/hera_bench_lane{1,4,8,16}.json` — per-lane benchmarks (n=100) | MEASURED |

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

**Concrete reason it remains PENDING:**

1. **RAM (primary blocker):** OOM-killed (exit 137) running the lightest
   available RtF+HERA benchmark in a 15 GB-RAM environment; the online phase
   was never reached. RAM anchor: ~60 GB for HERA at 80-bit cipher security
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

## Cipher: HERA-16 (post-attack parameters)

| Parameter | Value | Rationale |
|---|---|---|
| State size m | 16 | Original HERA-16 spec |
| Rounds r | 4 | Original; no attack reduces this |
| Plaintext modulus t | 2^26 | Conservative post-analysis choice |
| Key size | 256 bits (32 bytes) | AES-256 compatible RNG |
| Nonce size | 128 bits (16 bytes) | |
| Security | 128-bit | Under current algebraic analysis |

**Attack record note (§7.2):**
- Rubato: broken for ≥25% of modulus choices (Grassi et al., CRYPTO 2023) — 5/6 family members below claimed security. Not used.
- Elisabeth-4: broken (Cosseron et al.). Not used.
- HERA: round-key collision weaknesses found; current parameters (m=16, r=4, t=2^26) remain secure.

## Key measured numbers (from `results/`)

For a realistic batch (lanes=16, features=256 per transaction):
- Online upload size: **16,412 bytes** (vs 262,257 bytes CKKS standard = **16× smaller**)
- Client encrypt time: **~7 ms** (mean, plaintext path, n=100)
- Key expansion: **~0.05 ms** (amortized offline, plaintext path)

For a single transaction (lanes=1, features=256):
- Online upload size: **1,052 bytes** (vs 262,257 bytes = **249× smaller**)
- Client encrypt time: **~0.53 ms** (mean, n=100)

## Build / run

```bash
# Requires Go 1.25+
cd tools/transciphering
go build ./...
go run ./bench --features=256 --lanes=16 --rounds=100
go test ./cipher/...   # unit tests (run in-process, no server required)
```

## Threat model summary (full text: docs/spec.md §8)

1. Bank holds symmetric key `k`; provisions vendor with `Enc_BFV(k)` once
   (amortized offline cost, same provisioning protocol as Phase 1 Galois keys).
2. Online: bank encrypts 256-feature vector under `k` → ~1 KB upload.
   Vendor evaluates HERA homomorphically inside BFV, converts to CKKS via
   StC + modular reduction, runs the existing CKKS circuit unchanged.
3. AEAD (AES-128-GCM) wraps every online ciphertext — additive malleability
   of the stream cipher is neutralised.
4. Nonces are monotonic per session; replay rejection is the bank's obligation.
5. Symmetric key rotation: session-bound (new `Enc_BFV(k')` on re-provisioning).
