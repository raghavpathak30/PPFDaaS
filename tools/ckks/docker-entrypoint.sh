#!/usr/bin/env bash
# Entrypoint for the CKKS benchmark image (Dockerfile.bench). Every target
# writes its JSON under /results (mount a host dir there).
#
#   parity   plaintext parity gate, both param sets, fold and bsgs
#   a1       per-request stage breakdown + wire bytes (deployed fold)
#   a2       fold vs BSGS in one invocation
#   a3       thread scaling (fold, bsgs) over THREADS
#   a4       concurrent-request throughput over CONCURRENCY (runs with OMP_PROC_BIND=false)
#   all      parity, a1, a2, a3, a4 in order; exit code is the worst of them
#
# Environment (all optional; unset = the harness default):
#   PARAMS=160|200|both  REPS  WARMUP  SEED  STRATEGIES=fold,bsgs
#   OMP_THREADS (a1, a2)  THREADS="1,2,4,..." (a3)  CONCURRENCY="1,2,4,..." (a4)
#   DURATION=<seconds per a4 point>
set -euo pipefail

BIN=/opt/bench/ckks_bench
OUT="${RESULTS_DIR:-/results}"
mkdir -p "${OUT}"

flags=("--out=${OUT}")
[[ -n "${PARAMS:-}" ]] && flags+=("--params=${PARAMS}")
[[ -n "${REPS:-}" ]] && flags+=("--reps=${REPS}")
[[ -n "${WARMUP:-}" ]] && flags+=("--warmup=${WARMUP}")
[[ -n "${SEED:-}" ]] && flags+=("--seed=${SEED}")
[[ -n "${STRATEGIES:-}" ]] && flags+=("--strategies=${STRATEGIES}")

run() {  # run <target> [extra flags]
  local t="$1"; shift
  # a4 spawns its own worker threads, which inherit the main thread's affinity; OMP_PROC_BIND would
  # pin the main thread to one core and silently put every worker on it. The cpuset still applies.
  if [[ "${t}" == a4 ]]; then
    env -u OMP_PLACES OMP_PROC_BIND=false "${BIN}" "${t}" "${flags[@]}" "$@"
  else
    "${BIN}" "${t}" "${flags[@]}" "$@"
  fi
}

target_flags() {
  case "$1" in
    a1|a2) [[ -n "${OMP_THREADS:-}" ]] && echo "--omp-threads=${OMP_THREADS}" || true ;;
    a3)    [[ -n "${THREADS:-}" ]] && echo "--threads=${THREADS// /,}" || true ;;
    a4)    [[ -n "${CONCURRENCY:-}" ]] && echo "--concurrency=${CONCURRENCY// /,}"
           [[ -n "${DURATION:-}" ]] && echo "--duration=${DURATION}" || true ;;
  esac
}

mode="${1:-}"; shift || true
case "${mode}" in
  parity|a1|a2|a3|a4)
    mapfile -t extra < <(target_flags "${mode}")
    run "${mode}" "${extra[@]}" "$@" ;;
  all)
    rc=0
    for t in parity a1 a2 a3 a4; do
      mapfile -t extra < <(target_flags "${t}")
      run "${t}" "${extra[@]}" || rc=$?
    done
    exit "${rc}" ;;
  *)
    sed -n '2,13p' "$0" >&2; exit 2 ;;
esac
