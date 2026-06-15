cmake_minimum_required(VERSION 3.20)
project(PPFDaaS LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# ---- Phase 4 pre-gate ---------------------------------------------------
# This file used to be a stale, full duplicate of vendor_server/CMakeLists.txt
# (same target definitions, but with source paths resolved relative to the
# REPO ROOT instead of vendor_server/, e.g. "src/ckks_context.cpp" -- a path
# that has never existed at the repo root, and "src/ckks_context_160.cpp",
# which moved to tools/local_benchmark/ckks_context_160.cpp in the Phase 2
# pre-gate). Configuring this file directly therefore failed.
#
# vendor_server/CMakeLists.txt is the single source of truth for all C++
# targets (he_core, vendor_server_160, benchmark_160, etc.) and already
# references tools/local_benchmark/ckks_context_160.cpp correctly. This root
# file is now a thin wrapper that pulls vendor_server in as a subdirectory
# and registers the repo-level test suite (tests/verify_all.py,
# tests/test_he_core.cpp via CTest).
enable_testing()

add_subdirectory(vendor_server)
add_subdirectory(tests)
