#!/usr/bin/env bash
# scripts/cloud_transcipher_bench/run_benchmark.sh
#
# SCAFFOLDING — do NOT run without Raghav's explicit go-ahead.
#
# Purpose: run BenchmarkRtFHera80as (the paper's 128-bit target HE parameter
# set, HERA-80 cipher security -- see docs/spec.md §8.3 for the naming note)
# on an r7i.4xlarge (>=128 GiB) EC2 instance or the IIT-K box, capture peak
# RSS, and write a governor_manifest.json sidecar.
#
# This is the ONE COMMAND referenced by tools/transciphering/README.md and
# RESEARCH_FINDINGS_v3.md §B once run on adequate hardware:
#
#   scripts/cloud_transcipher_bench/run_benchmark.sh
#
# Prerequisites (on the cloud instance):
#   - git
#   - Go toolchain (see step 1 below -- installs a pinned version if the
#     system Go is missing or too old; this repo's Step 0 diagnosis found
#     go1.25.0 builds the fork cleanly with zero source changes, so any
#     Go >= 1.21 is expected to work, but we pin explicitly for reproducibility)
#   - /usr/bin/time (GNU time, for the -v flag; ships with the 'time' apt
#     package -- NOT installed on the 15GB dev host this was scaffolded on,
#     which is exactly why this step never ran there)
#   - ~80 GB free disk (Go module cache + build artifacts)
#   - >=90 GB free RAM (preflight-checked below; fails fast rather than OOM)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FETCH_SCRIPT="${REPO_ROOT}/third_party/fetch_rtf.sh"
CLONE_DIR="${REPO_ROOT}/third_party/RtF-Transciphering"

PINNED_GO_VERSION="go1.25.0"
BENCH_NAME="BenchmarkRtFHera80as"
BENCH_PKG="./ckks_fv/"
BENCH_TIME="1x"           # one iteration; add -count=3 for stability if RAM allows
OUTPUT_DIR="${REPO_ROOT}/artifacts"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
MIN_FREE_RAM_KB=$((90 * 1024 * 1024)) # ~90 GB, fail fast rather than OOM

# ---------------------------------------------------------------------------
# 0. Preflight: refuse to run under ~90 GB free RAM
# ---------------------------------------------------------------------------
FREE_RAM_KB=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
if [[ -z "${FREE_RAM_KB}" || "${FREE_RAM_KB}" -lt "${MIN_FREE_RAM_KB}" ]]; then
  echo "[FAIL] preflight: MemAvailable=${FREE_RAM_KB:-unknown} KB, need >= ${MIN_FREE_RAM_KB} KB (~90 GB)." >&2
  echo "[FAIL] RAM anchor for HERA 80-bit cipher security is ~60 GB [CONFIRMED-SOURCE:" >&2
  echo "[FAIL] arXiv:2409.06422v1 §II]; 90 GB leaves margin for OS + Go allocator overhead." >&2
  echo "[FAIL] Minimum instance: r7i.4xlarge (128 GiB). Refusing to start (fail fast, not OOM)." >&2
  exit 1
fi
echo "[OK] preflight: MemAvailable=${FREE_RAM_KB} KB >= ${MIN_FREE_RAM_KB} KB required"

if ! command -v /usr/bin/time >/dev/null 2>&1; then
  echo "[FAIL] preflight: /usr/bin/time not found. Install GNU time (e.g. 'apt-get install -y time')." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Set the pinned Go toolchain
# ---------------------------------------------------------------------------
GO_BIN="go"
if command -v go >/dev/null 2>&1 && [[ "$(go version)" == *"${PINNED_GO_VERSION}"* ]]; then
  echo "[OK] system go already matches pinned toolchain ${PINNED_GO_VERSION}"
else
  echo "[INFO] system go is not ${PINNED_GO_VERSION}; installing pinned toolchain via golang.org/dl"
  go install "golang.org/dl/${PINNED_GO_VERSION}@latest"
  "$(go env GOPATH)/bin/${PINNED_GO_VERSION}" download
  GO_BIN="$(go env GOPATH)/bin/${PINNED_GO_VERSION}"
fi
GO_VERSION="$(${GO_BIN} version)"
echo "[INFO] go_version=${GO_VERSION}"

# ---------------------------------------------------------------------------
# 2. Fetch the pinned RtF-Transciphering checkout
# ---------------------------------------------------------------------------
"${FETCH_SCRIPT}"
FORK_SHA="$(git -C "${CLONE_DIR}" rev-parse HEAD)"

mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# 3. Record host/instance metadata
# ---------------------------------------------------------------------------
INSTANCE_TYPE=$(curl -sf --max-time 2 http://169.254.169.254/latest/meta-data/instance-type \
  2>/dev/null || echo "unknown")
CPU_MODEL=$(awk -F': ' '/model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null || echo "unknown")
NPROC=$(nproc 2>/dev/null || echo "unknown")
TOTAL_RAM_KB=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo "unknown")
CPU_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
RUN_START=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

echo "[INFO] instance_type=${INSTANCE_TYPE}"
echo "[INFO] fork_sha=${FORK_SHA}"
echo "[INFO] bench=${BENCH_NAME}"
echo "[INFO] run_started_at=${RUN_START}"

# ---------------------------------------------------------------------------
# 4. Run the benchmark with peak-RSS monitoring via GNU time -v
# ---------------------------------------------------------------------------
RSS_LOG="${OUTPUT_DIR}/cloud_rtf_bench_${TIMESTAMP}.time_v.txt"
BENCH_LOG="${OUTPUT_DIR}/cloud_rtf_bench_${TIMESTAMP}.txt"

echo "[INFO] Starting benchmark (this may take 30-90 minutes and use ~60 GB RAM) ..."
echo "[INFO] RSS log -> ${RSS_LOG}"
echo "[INFO] Bench log -> ${BENCH_LOG}"

cd "${CLONE_DIR}"

/usr/bin/time -v \
  "${GO_BIN}" test "${BENCH_PKG}" \
    -bench "${BENCH_NAME}" \
    -run '^$' \
    -benchtime="${BENCH_TIME}" \
    -v \
    -timeout 180m \
  2>"${RSS_LOG}" \
  | tee "${BENCH_LOG}"

WALL_END=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

# ---------------------------------------------------------------------------
# 5. Extract peak RSS from /usr/bin/time -v output
# ---------------------------------------------------------------------------
PEAK_RSS_KB=$(grep "Maximum resident set size" "${RSS_LOG}" | awk '{print $NF}')
PEAK_RSS_KB="${PEAK_RSS_KB:-0}"
echo "[INFO] peak_rss_kb=${PEAK_RSS_KB}"

# ---------------------------------------------------------------------------
# 6. Write governor_manifest.json sidecar
# ---------------------------------------------------------------------------
MANIFEST="${OUTPUT_DIR}/cloud_rtf_bench_${TIMESTAMP}.governor_manifest.json"

cat > "${MANIFEST}" <<EOF
{
  "governor_at_run_start": "${CPU_GOV}",
  "taskset_cpus": "all",
  "run_started_at_utc": "${RUN_START}",
  "allow_non_performance_override": true,
  "cpu_governor": "${CPU_GOV}",
  "turbo_disabled": false,
  "stamped_at_utc": "${WALL_END}",
  "wall_seconds": null,
  "instance_type": "${INSTANCE_TYPE}",
  "cpu_model": "${CPU_MODEL}",
  "nproc": "${NPROC}",
  "total_ram_kb": ${TOTAL_RAM_KB},
  "peak_rss_kb": ${PEAK_RSS_KB},
  "benchmark_name": "${BENCH_NAME}",
  "go_version": "${GO_VERSION}",
  "fork_repo": "github.com/KAIST-CryptLab/RtF-Transciphering",
  "fork_sha": "${FORK_SHA}",
  "note": "cloud/large-RAM host; wall_seconds not auto-calculated; see bench_raw for Go benchmark ns/op"
}
EOF

echo "[INFO] Manifest written -> ${MANIFEST}"
echo "[INFO] Done. Artifacts are in ${OUTPUT_DIR}/ -- this repo's artifacts/ directory directly,"
echo "[INFO] no scp/copy step needed if run_benchmark.sh executes on this checkout in-place."
echo ""
echo "[NEXT STEPS] Per scripts/cloud_transcipher_bench/README.md:"
echo "  1. Fill online_transcipher_ms / repacking_ms in artifacts/hhe_breakeven.json"
echo "  2. Update tools/transciphering/README.md PENDING status"
echo "  3. Update docs/spec.md §8.3 PENDING block"
echo "  4. Add a PROJECT_STATE.md session entry"
echo "[IMPORTANT] If on a rented cloud instance, terminate it after copying artifacts"
echo "            to avoid ongoing charges."
