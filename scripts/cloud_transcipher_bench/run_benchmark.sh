#!/usr/bin/env bash
# scripts/cloud_transcipher_bench/run_benchmark.sh
#
# SCAFFOLDING ONLY — do NOT run without Raghav's explicit go-ahead.
# This script has not been tested end-to-end. Review every step before executing.
#
# Purpose: run BenchmarkRtFHera80as on an r7i.4xlarge (>=128 GiB) EC2 instance,
# capture peak RSS, and write artifacts in governor_manifest.json sidecar format.
#
# Prerequisites (on the cloud instance):
#   - Go 1.21+  (apt install golang-go  OR  snap install go --classic)
#   - git
#   - /usr/bin/time  (GNU time, for -v flag; ships with 'time' apt package)
#   - ~80 GB free disk (Go module cache + build artifacts)

set -euo pipefail

REPO_URL="https://github.com/KAIST-CryptLab/RtF-Transciphering"
CLONE_DIR="${HOME}/RtF-Transciphering"
BENCH_NAME="BenchmarkRtFHera80as"
BENCH_PKG="./ckks_fv/"
BENCH_TIME="1x"           # one iteration; add -count=3 for stability if RAM allows
OUTPUT_DIR="${HOME}/rtf_artifacts"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)

# ---------------------------------------------------------------------------
# 0. Record instance metadata
# ---------------------------------------------------------------------------
INSTANCE_TYPE=$(curl -sf http://169.254.169.254/latest/meta-data/instance-type \
  2>/dev/null || echo "unknown")
GO_VERSION=$(go version 2>/dev/null || echo "unknown")
RUN_START=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

echo "[INFO] instance_type=${INSTANCE_TYPE}"
echo "[INFO] go_version=${GO_VERSION}"
echo "[INFO] bench=${BENCH_NAME}"
echo "[INFO] run_started_at=${RUN_START}"

# ---------------------------------------------------------------------------
# 1. Clone (shallow) the RtF-Transciphering repo
# ---------------------------------------------------------------------------
if [[ ! -d "${CLONE_DIR}" ]]; then
  echo "[INFO] Cloning ${REPO_URL} ..."
  git clone --depth=1 "${REPO_URL}" "${CLONE_DIR}"
else
  echo "[INFO] Using existing clone at ${CLONE_DIR}"
fi

mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# 2. Run the benchmark with peak-RSS monitoring via GNU time -v
#    /usr/bin/time -v writes RSS to stderr; redirect to a capture file.
# ---------------------------------------------------------------------------
RSS_LOG="${OUTPUT_DIR}/time_v_${TIMESTAMP}.txt"
BENCH_LOG="${OUTPUT_DIR}/bench_raw_${TIMESTAMP}.txt"

echo "[INFO] Starting benchmark (this may take 30–90 minutes and use ~60 GB RAM) ..."
echo "[INFO] RSS log -> ${RSS_LOG}"
echo "[INFO] Bench log -> ${BENCH_LOG}"

cd "${CLONE_DIR}"

/usr/bin/time -v \
  go test "${BENCH_PKG}" \
    -bench "${BENCH_NAME}" \
    -benchtime="${BENCH_TIME}" \
    -v \
    -timeout 180m \
  2>"${RSS_LOG}" \
  | tee "${BENCH_LOG}"

WALL_END=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

# ---------------------------------------------------------------------------
# 3. Extract peak RSS from /usr/bin/time -v output
# ---------------------------------------------------------------------------
PEAK_RSS_KB=$(grep "Maximum resident set size" "${RSS_LOG}" \
  | awk '{print $NF}' || echo "0")

echo "[INFO] peak_rss_kb=${PEAK_RSS_KB}"

# ---------------------------------------------------------------------------
# 4. Write governor_manifest.json sidecar
# ---------------------------------------------------------------------------
MANIFEST="${OUTPUT_DIR}/cloud_rtf_bench_${TIMESTAMP}.governor_manifest.json"

# CPU governor (cloud instances typically show 'performance' or 'powersave')
CPU_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null \
  || echo "unknown")

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
  "peak_rss_kb": ${PEAK_RSS_KB},
  "benchmark_name": "${BENCH_NAME}",
  "go_version": "${GO_VERSION}",
  "note": "cloud instance; wall_seconds not auto-calculated; see bench_raw for Go benchmark ns/op"
}
EOF

echo "[INFO] Manifest written -> ${MANIFEST}"

# ---------------------------------------------------------------------------
# 5. Copy artifacts to a local path for scp retrieval
# ---------------------------------------------------------------------------
cp "${BENCH_LOG}" "${OUTPUT_DIR}/cloud_rtf_bench_${TIMESTAMP}.txt"
echo "[INFO] Done. Retrieve files from ${OUTPUT_DIR}/ before terminating instance."
echo ""
echo "  scp -r ec2-user@<IP>:${OUTPUT_DIR}/ ./artifacts/cloud_rtf_bench_${TIMESTAMP}/"
echo ""
echo "[IMPORTANT] Terminate the instance after copying artifacts to avoid ongoing charges."
