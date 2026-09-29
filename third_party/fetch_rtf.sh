#!/usr/bin/env bash
# third_party/fetch_rtf.sh
#
# Reproduces the exact pinned checkout of the RtF-Transciphering bridge library
# (github.com/KAIST-CryptLab/RtF-Transciphering, a Lattigo-v2-era fork,
# package ckks_fv) under third_party/RtF-Transciphering/.
#
# third_party/ is gitignored -- this script is the source of truth for
# reproducing the checkout, not the checked-out tree itself.
#
# Usage: third_party/fetch_rtf.sh

set -euo pipefail

REPO_URL="https://github.com/KAIST-CryptLab/RtF-Transciphering"
# Pinned commit on the default branch (master), recorded 2026-07-27.
PINNED_SHA="105fc73115b56f1d6ff357029c7682b19a6d8510"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLONE_DIR="${SCRIPT_DIR}/RtF-Transciphering"

if [[ -d "${CLONE_DIR}/.git" ]]; then
  echo "[INFO] ${CLONE_DIR} already exists; fetching and checking out pinned SHA"
  git -C "${CLONE_DIR}" fetch --depth=1 origin "${PINNED_SHA}"
  git -C "${CLONE_DIR}" checkout --detach FETCH_HEAD
else
  rm -rf "${CLONE_DIR}"
  echo "[INFO] Cloning ${REPO_URL} ..."
  git clone "${REPO_URL}" "${CLONE_DIR}"
  git -C "${CLONE_DIR}" checkout --detach "${PINNED_SHA}"
fi

ACTUAL_SHA="$(git -C "${CLONE_DIR}" rev-parse HEAD)"
if [[ "${ACTUAL_SHA}" != "${PINNED_SHA}" ]]; then
  echo "[ERROR] checked out ${ACTUAL_SHA}, expected ${PINNED_SHA}" >&2
  exit 1
fi

echo "[OK] third_party/RtF-Transciphering pinned at ${ACTUAL_SHA}"

# ---------------------------------------------------------------------------
# Stage the PPFDaaS toy correctness harnesses (tracked source, see
# tools/transciphering/toy_correctness/README.md) as additive files in the
# vendored ckks_fv/ package. They are kept under a testdata/ directory in the
# main repo so `go build ./...` / `go vet ./...` on the tools/transciphering
# module never tries to compile them standalone (testdata/ is ignored by Go
# tooling); they only become buildable once copied here, where the real
# ckks_fv package symbols they reference (plainHera, plainRubato,
# HalfBootParameters, ...) exist.
TOY_HARNESS_DIR="${SCRIPT_DIR}/../tools/transciphering/toy_correctness/testdata/ckks_fv_patch"
if [[ -d "${TOY_HARNESS_DIR}" ]]; then
  for f in "${TOY_HARNESS_DIR}"/*.go; do
    [[ -e "${f}" ]] || continue
    cp "${f}" "${CLONE_DIR}/ckks_fv/$(basename "${f}")"
    echo "[OK] staged toy correctness harness -> ckks_fv/$(basename "${f}")"
  done
else
  echo "[WARN] toy harness dir not found at ${TOY_HARNESS_DIR}; skipped staging" >&2
fi
