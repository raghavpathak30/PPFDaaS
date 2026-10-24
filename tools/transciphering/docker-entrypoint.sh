#!/usr/bin/env bash
# Entrypoint for the transciphering benchmark image (Dockerfile.bench).
# Every mode writes its output under /results (mount a host dir there).
#
#   kat                     HERA + Rubato known-answer tests and the modulus test
#   toy                     LogN=10 toy harness, HERA-128as and Rubato-128L, once
#   toy-repeat              toy harness N times per cipher (TOY_REPEAT_N, default 30)
#   bench [bench flags]     client-side plaintext benchmark (cipher/bench)
#   test <bin> <filter>     run a test binary (cipher | ckks_fv) with -test.run <filter>;
#                           extra args are passed through (e.g. -test.timeout 30m)
set -euo pipefail

BIN=/opt/bench
export RESULTS_DIR="${RESULTS_DIR:-/results}"
export RSS_CHECKPOINT_PATH="${RSS_CHECKPOINT_PATH:-${RESULTS_DIR}/rss_checkpoints.jsonl}"
mkdir -p "${RESULTS_DIR}"

mode="${1:-}"; shift || true
case "${mode}" in
  kat)
    "${BIN}/cipher.test" -test.count=1 -test.v \
      -test.run '^(TestHERAKnownAnswer|TestHeraModulusMatchesCkksFV|TestRubatoKnownAnswer)$' "$@" ;;
  toy)
    "${BIN}/ckks_fv.test" -test.count=1 -test.v \
      -test.run '^(TestRtFHera128asToyCorrectness|TestRtFRubato128LToyCorrectness)$' "$@" ;;
  toy-repeat)
    exec "${BIN}/toyrep" ;;
  bench)
    exec "${BIN}/bench" -outdir "${RESULTS_DIR}" "$@" ;;
  test)
    bin="${1:?usage: test <cipher|ckks_fv> <filter> [extra test flags]}"; filter="${2:?missing filter}"; shift 2
    case "${bin}" in cipher|ckks_fv) ;; *) echo "unknown test binary: ${bin}" >&2; exit 2 ;; esac
    "${BIN}/${bin}.test" -test.count=1 -test.v -test.run "${filter}" "$@" ;;
  *)
    sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
