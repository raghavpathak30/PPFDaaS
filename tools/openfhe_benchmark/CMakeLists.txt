# tools/openfhe_benchmark/CMakeLists.txt — Phase 4, §4.3 cross-library study.
#
# This directory is a STANDALONE CMake project. It is NOT added as a
# subdirectory of the root or vendor_server CMake configurations, and is NOT
# part of the deployed PPFDaaS vendor server or its TCB (see docs/spec.md
# §6.9, §7.4). It exists purely to measure genuine Halevi-Shoup rotation
# hoisting (via OpenFHE's EvalFastRotationPrecompute/EvalFastRotation) against
# the two SEAL strategies implemented in vendor_server/src/rotation_hoisting.cpp
# (sequential fold and BSGS two-layer, Phase 4 §4.1/§4.2).
#
# OpenFHE is NOT installed in the environment this scaffold was written in
# (no OpenFHEConfig.cmake was found anywhere on the system). The FATAL_ERROR
# below is intentional: this project must fail closed with build instructions
# rather than silently skip the comparison. See README.md for the full
# install procedure and the SEAL-160-bit <-> OpenFHE parameter equivalence
# table.

cmake_minimum_required(VERSION 3.20)
project(ppfdaas_openfhe_benchmark LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(OpenFHE QUIET)
if(NOT OpenFHE_FOUND)
    message(FATAL_ERROR
"OpenFHE was not found (find_package(OpenFHE) failed).\n"
"\n"
"tools/openfhe_benchmark/ is a Phase 4 (docs/spec.md §4.3 / §7.4) "
"cross-library study comparing genuine Halevi-Shoup rotation hoisting "
"(OpenFHE EvalFastRotation) against the two SEAL strategies in "
"vendor_server/src/rotation_hoisting.cpp (sequential fold, BSGS two-layer). "
"It is intentionally a standalone CMake project, never built by the root or "
"vendor_server configurations.\n"
"\n"
"To build OpenFHE from source (>= v1.1.x):\n"
"    git clone https://github.com/openfheorg/openfhe-development.git\n"
"    cd openfhe-development && mkdir build && cd build\n"
"    cmake .. -DCMAKE_BUILD_TYPE=Release\n"
"    make -j$(nproc)\n"
"    sudo make install\n"
"\n"
"Then configure and build this directory directly:\n"
"    cmake -S tools/openfhe_benchmark -B tools/openfhe_benchmark/build -DCMAKE_BUILD_TYPE=Release\n"
"    cmake --build tools/openfhe_benchmark/build -j$(nproc)\n"
"    ./tools/openfhe_benchmark/build/openfhe_benchmark\n"
"\n"
"See tools/openfhe_benchmark/README.md for the full parameter equivalence "
"table (SEAL 160-bit vs OpenFHE) and notes on OpenFHE's automatic "
"ring-dimension selection.")
endif()

# ---------------------------------------------------------------------------
# Standard OpenFHE consumer setup, following the pattern documented by the
# OpenFHE project itself (openfhe-development/CMakeLists.User.txt and the
# example projects under src/pke/examples).
# ---------------------------------------------------------------------------
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${OpenFHE_CXX_FLAGS}")

find_package(OpenMP)

include_directories(${OPENMP_INCLUDES})
include_directories(${OpenFHE_INCLUDE})
include_directories(${OpenFHE_INCLUDE}/third-party/include)
include_directories(${OpenFHE_INCLUDE}/core)
include_directories(${OpenFHE_INCLUDE}/pke)

link_directories(${OpenFHE_LIBDIR})
link_directories(${OPENMP_LIBRARIES})

set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} ${OpenFHE_EXE_LINKER_FLAGS}")

add_library(openfhe_linear_eval STATIC
    openfhe_linear_eval.cpp
    openfhe_linear_eval.h
)
target_link_libraries(openfhe_linear_eval PUBLIC ${OpenFHE_SHARED_LIBRARIES})

add_executable(openfhe_benchmark openfhe_benchmark.cpp)
target_link_libraries(openfhe_benchmark PRIVATE openfhe_linear_eval ${OpenFHE_SHARED_LIBRARIES})
