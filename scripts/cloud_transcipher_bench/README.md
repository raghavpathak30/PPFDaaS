# Cloud RtF-Transciphering Benchmark Runbook

**Status: SCAFFOLDING ONLY — do NOT execute without Raghav's explicit go-ahead.**

Purpose: measure `online_transcipher_ms` and `repacking_ms` for the
`hhe_breakeven.json` PENDING cells. See `tools/transciphering/README.md` and
`RESEARCH_FINDINGS_v3.md §B` for why this requires a cloud machine.

---

## Why this machine cannot run it

`BenchmarkRtFHera80s` (lightest config: 4 slots, 80-bit cipher security)
OOM-killed this 15 GB-RAM host (exit 137) during the offline phase. RAM anchor:
~60 GB for HERA 80-bit cipher security [CONFIRMED-SOURCE: arXiv:2409.06422v1 §II].

---

## Prerequisites

- AWS account with EC2 permissions in us-east-1 or eu-west-1
- Key pair and security group allowing SSH inbound
- Go 1.21+ (installed via `apt install golang-go` or `snap install go --classic`)
- `/usr/bin/time` (GNU time for `-v` RSS output; ships with `time` package)

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

Results and the hardware/governor manifest must match the format of existing
`artifacts/performance_revalidation/performance/*.governor_manifest.json`:

```json
{
  "governor_at_run_start": "<cpufreq governor or 'cloud-fixed-freq'>",
  "taskset_cpus": "<cpu list or 'all'>",
  "run_started_at_utc": "<ISO8601>",
  "allow_non_performance_override": false,
  "cpu_governor": "<value>",
  "turbo_disabled": <bool>,
  "stamped_at_utc": "<ISO8601>",
  "wall_seconds": <float>,
  "instance_type": "r7i.4xlarge",
  "peak_rss_kb": <int>,
  "benchmark_name": "BenchmarkRtFHera80as",
  "go_version": "<go version string>"
}
```

Write sidecar as `artifacts/cloud_rtf_bench_YYYYMMDD.governor_manifest.json`
alongside `artifacts/cloud_rtf_bench_YYYYMMDD.json` (raw benchmark output).

---

## Launch script

See `run_benchmark.sh` in this directory. Review carefully before running.
The script is scaffolding — it has not been tested end-to-end.

---

## After the run

1. `scp` or `aws s3 cp` the two artifact files back to this repo's `artifacts/`
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
