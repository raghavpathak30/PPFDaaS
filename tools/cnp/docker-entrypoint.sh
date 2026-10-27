#!/usr/bin/env bash
# Entrypoint for the CNP benchmark image (Dockerfile.cnpbench).
#
#   smoke   10 requests / 2 warmup per configuration, all strategies and lanes; prints the table
#   cnp     the real run: strategy x lanes x requests, JSON to /results/cnp_bench.json
#   shell   bash (debugging)
#
# Mount: host artifacts/ at /app/artifacts (read-only), a writable dir at /results.
# Environment (all optional):
#   STRATEGIES=fold,bsgs,naive,cnp   LANES=1,4,8,16,32   REQUESTS=1000   WARMUP=20
#   OMP_THREADS=1   SERVER_CORES=0-7   CLIENT_CORES=10-11   PORT=50052
set -euo pipefail

OUT="${RESULTS_DIR:-/results}"
mkdir -p "${OUT}"

for f in model_weights.bin weights.npy X_test.npy y_test.npy public_key_160.bin secret_key_160.bin galois_keys_160.bin; do
  [[ -r "/app/artifacts/${f}" ]] || { echo "missing /app/artifacts/${f} -- mount the host artifacts/ dir read-only at /app/artifacts" >&2; exit 2; }
done

flags=(--strategies "${STRATEGIES:-fold,bsgs,naive,cnp}" --lanes "${LANES:-1,4,8,16,32}")
[[ -n "${OMP_THREADS:-}" ]]   && flags+=(--omp-threads "${OMP_THREADS}")
[[ -n "${SERVER_CORES:-}" ]]  && flags+=(--server-cores "${SERVER_CORES}")
[[ -n "${CLIENT_CORES:-}" ]]  && flags+=(--client-cores "${CLIENT_CORES}")
[[ -n "${PORT:-}" ]]          && flags+=(--port "${PORT}")

mode="${1:-smoke}"; shift || true
case "${mode}" in
  smoke) exec python3 /app/scripts/cnp_bench.py "${flags[@]}" --smoke --out "${OUT}/cnp_bench_smoke.json" "$@" ;;
  cnp)   exec python3 /app/scripts/cnp_bench.py "${flags[@]}" --requests "${REQUESTS:-1000}" --warmup "${WARMUP:-20}" \
              --out "${OUT}/cnp_bench.json" "$@" ;;
  shell) exec bash "$@" ;;
  *)     sed -n '2,13p' "$0" >&2; exit 2 ;;
esac
