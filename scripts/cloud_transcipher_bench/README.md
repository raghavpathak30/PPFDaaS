# Cloud RtF-Transciphering Benchmark Runbook

**Status: SCAFFOLDING — do NOT execute without Raghav's explicit go-ahead.**

Purpose: measure `online_transcipher_ms` and `repacking_ms` for the
`hhe_breakeven.json` PENDING cells. See `tools/transciphering/README.md` and
`RESEARCH_FINDINGS_v3.md §B` for why this requires a large-RAM machine.

**2026-07-27 update:** this session confirmed the bridge **builds cleanly**
(`go vet` + `go test -c`, zero source changes, Go 1.25.0 — see
`third_party/BUILD_NOTES.md`) and that the **code path is functionally
correct** at toy scale (`tools/transciphering/toy_correctness/README.md`,
LogN=10, PASS, max abs error 2.1e-05). The only remaining blocker for a real
timing number is RAM for the secure (LogN=16) parameters. This runbook and
`run_benchmark.sh` are now finalized end to end — see "The one command"
below — pending only the go-ahead to spend cloud budget.

---

## Why this machine cannot run it

`BenchmarkRtFHera80s` (lightest config: 4 slots, 80-bit cipher security)
OOM-killed this 15 GB-RAM host (exit 137) during the offline phase. RAM anchor:
~60 GB for HERA 80-bit cipher security [CONFIRMED-SOURCE: arXiv:2409.06422v1 §II].

---

## Prerequisites

- AWS account with EC2 permissions in us-east-1 or eu-west-1 (or the IIT-K
  box, if using that instead of AWS)
- Key pair and security group allowing SSH inbound (AWS only)
- git
- `/usr/bin/time` (GNU time for `-v` RSS output; ships with the `time`
  apt/yum package — **not installed on the 15GB dev host**, which is why
  Step 0-2 of this session's work used `/proc`-based RSS polling instead;
  the cloud box must have it since `run_benchmark.sh` hard-requires it)
- Go: `run_benchmark.sh` installs a pinned toolchain (`go1.25.0`, via
  `golang.org/dl`) automatically if the system `go` doesn't already match;
  it only needs *some* `go` on PATH to bootstrap that install

---

## Instance selection

| Tier | Instance | RAM | On-demand price | Use case |
|------|----------|-----|-----------------|----------|
| **Minimum starting point** | r7i.4xlarge | 128 GiB | NOT YET PULLED — fetch from Vantage before quoting | HERA 80-bit configs |
| Fallback (128-bit configs) | r7i.8xlarge | 256 GiB | NOT YET PULLED | HERA 128-bit full-slot |

Rationale: ~60 GB anchor + OS overhead + Go allocator headroom. r7i.2xlarge
(64 GiB) has no margin and is NOT the correct starting tier (updated 2026-07-01
from v2's recommendation).

AMI: Amazon Linux 2023 or Ubuntu 22.04 LTS (amd64). Storage: gp3, 30 GB root.

`run_benchmark.sh` preflight-refuses to start below ~90 GB `MemAvailable`
(fail fast, not OOM) — see "Preflight" below.

---

## The one command

Once this repo is checked out on the r7i.4xlarge instance (or the IIT-K box):

```bash
scripts/cloud_transcipher_bench/run_benchmark.sh
```

This single command:
1. Preflight-checks `/proc/meminfo` `MemAvailable` >= ~90 GB and that
   `/usr/bin/time` is present; exits 1 immediately if either fails.
2. Installs/uses the pinned Go toolchain (`go1.25.0`).
3. Runs `third_party/fetch_rtf.sh` to clone and pin
   `github.com/KAIST-CryptLab/RtF-Transciphering` at commit
   `105fc73115b56f1d6ff357029c7682b19a6d8510`.
4. Runs `BenchmarkRtFHera80as` (paramIndex=3, the paper's 128-bit HE
   parameter target) with `-benchtime=1x`, wrapped in `/usr/bin/time -v`.
5. Writes `artifacts/cloud_rtf_bench_<TIMESTAMP>.txt` (raw benchmark output),
   `artifacts/cloud_rtf_bench_<TIMESTAMP>.time_v.txt` (RSS log), and
   `artifacts/cloud_rtf_bench_<TIMESTAMP>.governor_manifest.json` (sidecar).

No manual clone, toolchain install, or scp step is required if run directly
on a checkout of this repo on the cloud instance.

---

## Benchmark to run

```
BenchmarkRtFHera80as  (paramIndex=3 in RtFHeraParams)
```

**Not** BenchmarkRtFHera80s (that was the OOM config on the 15GB host).
"80as" = 80-bit symmetric cipher security, arcsine variant, 4 sparse slots.
HE ring is LogN=16 (N=65,536) — same for all HERA configs regardless of name.

---

## Output format

The governor/host manifest (`*.governor_manifest.json`) extends the format
used by `artifacts/performance_revalidation/*/*.governor_manifest.json` with
cloud-specific fields:

```json
{
  "governor_at_run_start": "<cpufreq governor or 'unknown'>",
  "taskset_cpus": "all",
  "run_started_at_utc": "<ISO8601>",
  "allow_non_performance_override": true,
  "cpu_governor": "<value>",
  "turbo_disabled": false,
  "stamped_at_utc": "<ISO8601>",
  "wall_seconds": null,
  "instance_type": "r7i.4xlarge",
  "cpu_model": "<from /proc/cpuinfo>",
  "nproc": "<core count>",
  "total_ram_kb": <int>,
  "peak_rss_kb": <int>,
  "benchmark_name": "BenchmarkRtFHera80as",
  "go_version": "<go version string>",
  "fork_repo": "github.com/KAIST-CryptLab/RtF-Transciphering",
  "fork_sha": "105fc73115b56f1d6ff357029c7682b19a6d8510"
}
```

Written as `artifacts/cloud_rtf_bench_<TIMESTAMP>.governor_manifest.json`
alongside `artifacts/cloud_rtf_bench_<TIMESTAMP>.txt` (raw benchmark output)
and `artifacts/cloud_rtf_bench_<TIMESTAMP>.time_v.txt` (full `time -v` log).

---

## Preflight

`run_benchmark.sh` refuses to start (exit 1, no partial run) if:
- `/proc/meminfo` `MemAvailable` < ~90 GB (90 * 1024 * 1024 KB), or
- `/usr/bin/time` is not on PATH.

This was dry-run-verified on the 15 GB dev host: `MemAvailable` reads
~6.5 GB there, so the preflight correctly refuses before touching the
network or spending any compute.

---

## After the run

1. If on a rented cloud instance (not the IIT-K box), `scp` or
   `aws s3 cp` the three artifact files back if they weren't already
   produced directly inside this repo checkout
2. Fill `online_transcipher_ms` and `repacking_ms` in `artifacts/hhe_breakeven.json`
3. Update `tools/transciphering/README.md` PENDING status
4. Update `docs/spec.md` §8.3 PENDING block
5. Add PROJECT_STATE.md session entry per existing convention
6. Tag commit as Phase 7 completion

---

## Cost estimate (rough)

At r7i.4xlarge ~$1–2/hr on-demand (price NOT confirmed — fetch before running),
a benchmark run + setup + teardown should be under 2 hours → under $4. Spot
pricing (typically 60–70% off) is available but requires interruption handling.
Terminate the instance immediately after copying artifacts.
