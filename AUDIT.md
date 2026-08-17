# Phase 0 Audit — PPFDaaS measurement integrity (2026-06-19)

This audit confirms three suspected problems before any fix is attempted, per
the governing rule: no number may be reported unless it was produced by
executing the thing it describes.

## 1. CPU governor

- `grep -rl cpu_governor artifacts/*.json` → only `artifacts/comparison_results.json`
  records a `hardware_manifest.cpu_governor` field at all. Its value:
  `"powersave"` (`artifacts/comparison_results.json` → `hardware_manifest.cpu_governor`).
- No other timing-bearing artifact (`throughput_results.json`,
  `privacy_cost_analysis.json`, `rotation_strategy_comparison.json`,
  `execution_matrix.json`, `wire_sizes.json`, `amortization_table.json`) records
  the governor at collection time at all — so even retroactively we cannot
  confirm what governor those numbers were collected under. This is itself a
  hygiene gap, not just the powersave finding.
- `tests/benchmark_comparison.py:487-491` reads
  `/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` into the manifest.
  `tests/benchmark_comparison.py:587-590` prints a WARNING if governor !=
  `"performance"`. `tests/benchmark_comparison.py:714-720` confirms: SLA gates
  are only treated as fatal when `hardware_manifest["cpu_governor"] ==
  "performance"` — i.e. under the current `powersave` collection, gate
  failures are explicitly downgraded to non-fatal NOTEs. **Confirmed.**
- `tests/benchmark_throughput.py` and `scripts/privacy_cost_analysis.py`,
  `scripts/rotation_strategy_comparison.py`, `scripts/build_execution_matrix.py`,
  `scripts/measure_wire_size.py`, `scripts/generate_amortization_table.py` contain
  **no** `governor` string at all — they never check or record it.
- Live check in this environment: `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor`
  → `powersave`. `sudo -n true` → `sudo: a password is required` (no
  passwordless sudo, no interactive TTY for a password prompt in this tool
  environment). **Governor cannot be changed here — confirmed blocker for
  Phase 1, see below.**

## 2. Privacy-cost conflation (§5.8)

`scripts/privacy_cost_analysis.py`:
- `BASELINE_BIN = vendor_server/build/vendor_server_main` (200-bit chain,
  line 50) and `REDUCED_BIN = vendor_server/build/vendor_server_160` (160-bit
  chain, line 51).
- `vendor_server_main` is built from `inference_service.cpp`, which constructs
  a `CKKSContext` (`vendor_server/include/ckks_context.h:11-26`). `CKKSContext`
  holds a live `seal::SecretKey`, `seal::PublicKey`, and `seal::Decryptor` —
  the full decrypt capability — inside the server process.
- `vendor_server_160` is built from `inference_service_160.cpp`, which
  constructs an `EvalContext160` (`vendor_server/include/eval_context_160.h:38-71`).
  Per its own header comment (lines 13-30): `EvalContext160` has **no**
  `SecretKey`, **no** `Decryptor`, **no** `KeyGenerator` anywhere — it is the
  Phase 1 eval-only, fail-closed rewrite. It also carries the Phase 1
  provisioning state machine, the Phase 2 `thread_local` concurrency fix, and
  a structurally different RPC service implementation from
  `inference_service.cpp`.
- The latency numbers `privacy_cost_analysis.py:181-183` consumes come from
  `artifacts/comparison_results.json["summary"]["baseline_200bit"/"reduced_160bit"]`,
  which `tests/benchmark_comparison.py` produces by launching exactly these
  two binaries (`tests/benchmark_comparison.py:49-50, 565-566, 592-596`) over
  gRPC.
- **Confirmed**: the §5.8 latency/bandwidth delta currently attributed to "one
  additional 40-bit RNS prime" is actually the sum of (a) the modulus-chain
  difference, (b) an entirely different server architecture (decrypt-capable
  legacy service vs. eval-only Phase 1 service), and (c) different RPC service
  code paths (`inference_service.cpp` vs `inference_service_160.cpp`).
- A more architecture-matched pair exists locally: `vendor_server/src/benchmark.cpp`
  uses `CKKSContext` (200-bit, `#include "ckks_context.h"`) and
  `vendor_server/src/benchmark_160.cpp` uses `CKKSContext160`
  (`#include "ckks_context_160.h"`, **not** `EvalContext160`) — both retain a
  live secret key for self-timing and both share `rotation_hoisting.h/.cpp`
  (there is no `rotation_hoisting_160` variant). This pair differs by modulus
  chain only, not by service architecture. This is the basis for the Phase 3
  fix.

## 3. OpenFHE arm status

- `tools/openfhe_benchmark/results/openfhe_results.json` → `"status": "PENDING"`,
  with reason: "OpenFHE is not installed in this environment (no
  OpenFHEConfig.cmake or pkg-config found anywhere on the system)."
- Live check: `find / -iname OpenFHEConfig.cmake` → no results.
  `pkg-config --exists openfhe` → exit 1 (not found).
- `tools/openfhe_benchmark/` is a complete, compile-ready scaffold
  (`CMakeLists.txt`, `openfhe_linear_eval.{h,cpp}`, `openfhe_benchmark.cpp`)
  that fails closed (`FATAL_ERROR`) if `find_package(OpenFHE)` does not
  succeed. It has never been built or run in this checkout. **Confirmed
  PENDING**, scaffold-only.

## Pre-existing uncommitted work found in the working tree

Before starting remediation, this checkout already contained uncommitted
work (not yet in any AUDIT/PROJECT_STATE entry) that appears to be a prior,
unfinished attempt at this same task:
- `docs/spec.md` §8 (Transciphering threat model), `proto/inference.proto`
  `CanaryCheckTranscipher` RPC, `vendor_server/src/inference_service_160.cpp`
  PENDING stub for it, `tools/transciphering/` (Go HERA-16 cipher + bench
  results), `artifacts/hhe_breakeven.json`, `scripts/hhe_breakeven.py`.
- This work follows the same honesty discipline (explicit `PENDING` /
  `pending_impl` markers, cited blocking reason: standard Lattigo v6.2.0 lacks
  the KAIST `ckks_fv` FV→CKKS bridge). It is being kept and extended rather
  than redone, per Phase 5 below.
- `lab/mock_server.py` and `server_stub.py` are unrelated Docker-exercise
  scratch files (Module 2 PID-1/signal-handling practice, not PPFDaaS code).
  Left untouched — out of scope for this task.
- `tools/local_benchmark/precision_probe` is a stale build binary
  (`precision_probe.cpp` compiled output); harmless, not referenced by any
  script, left as-is.

## Phase 1 result: CPU governor — PENDING (no root in this environment)

- `sudo -n true` → `sudo: a password is required`; this tool environment has
  no interactive TTY for a password prompt and no NOPASSWD sudo rule. The
  governor genuinely cannot be changed here.
- **Marking Phase 1 PENDING.** Exact command sequence to run on a machine
  where I (or the user) have root, before any further latency numbers are
  collected:

  ```bash
  # 1. Set all cores to performance
  for i in $(seq 0 $(($(nproc) - 1))); do
    echo performance | sudo tee /sys/devices/system/cpu/cpu$i/cpufreq/scaling_governor
  done

  # 2. Verify
  cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor   # all "performance"

  # 3. (optional, reduces frequency variance) pin min==max frequency
  for i in $(seq 0 $(($(nproc) - 1))); do
    maxf=$(cat /sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_max_freq)
    echo $maxf | sudo tee /sys/devices/system/cpu/cpu$i/cpufreq/scaling_min_freq
  done

  # 4. (optional, Intel only) disable turbo boost variability
  echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo   # if intel_pstate
  # or: echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost     # generic

  # 5. Re-run the full timing pipeline
  python3 scripts/reproduce_all.py --from <comparison_step>
  ```

- **Consequence for this session**: all *latency* numbers regenerated below
  remain under `powersave` and are explicitly labeled as such — they are not
  promoted to final/publication numbers. Correctness, bandwidth, and
  precision results (which do not depend on clock frequency) are unaffected
  and proceed normally.

## Phase 5 addendum: the KAIST ckks_fv bridge exists and is runnable — RAM, not code, is the blocker

Re-verifying the pre-existing transciphering work (HERA-16 client-side bench,
`artifacts/hhe_breakeven.json`) before trusting it: re-ran
`tools/transciphering/bench` (Go) and `scripts/hhe_breakeven.py` myself,
both reproduced fresh numbers in the same range as the committed ones, and
the bandwidth figures cross-check exactly against `artifacts/wire_sizes.json`
(`262257` bytes for 160-bit standard). `cipher/hera.go` intentionally has no
`Decrypt` (only `Encrypt` is needed for the measured client-side columns) —
not a bug.

While verifying, found that the documented blocker ("Standard Lattigo v6.2.0
does not include the `ckks_fv` module, PENDING `github.com/B-R-P/lattigo-
ckks-fv`") undersold what's actually available: KAIST-CryptLab's own
reference implementation, `github.com/KAIST-CryptLab/RtF-Transciphering`
(Lattigo v2 fork, package `ckks_fv`, `fv_hera.go` +
`RtF_bench_test.go`/`BenchmarkRtFHera80s` etc.), is real and clonable. Cloned
it and ran its lightest configuration
(`go test ./ckks_fv/ -bench BenchmarkRtFHera80s -benchtime=1x`, 4 slots,
80-bit security, no full-slot bootstrap). It drove this 15GB-RAM host to
<200MB free + heavy swap during the "RtF HERA Offline Latency" sub-benchmark
and was OOM-killed (exit 137) before producing a single number. Did not
retry with a larger/heavier config, per the "stop and mark PENDING" rule, to
avoid further destabilizing the host (briefly affected an unrelated
monitoring command in this session). Clone removed
(`third_party/RtF-Transciphering`, never committed).

**Updated PENDING reason for §8 steps 6-7 / `online_transcipher_ms` /
`repacking_ms`: insufficient RAM in this environment, not unavailable code.**
Re-attempt on a machine with substantially more RAM than 15GB; the 4-slot
80-bit variant already failed, so full-slot/128-bit variants will need
considerably more.
