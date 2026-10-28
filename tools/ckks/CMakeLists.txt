cmake_minimum_required(VERSION 3.20)
project(ckks_bench LANGUAGES CXX)

# Standalone build of the CKKS benchmark harness: SEAL + OpenMP only (no gRPC,
# protobuf or Python). The reductions are the production ones, compiled from
# vendor_server/src/rotation_hoisting.cpp. Configure with -S tools/ckks.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG")

list(PREPEND CMAKE_PREFIX_PATH "/usr/local")
find_package(SEAL 4.1.2 EXACT REQUIRED CONFIG)
find_package(OpenMP REQUIRED)
find_package(Threads REQUIRED)

set(VENDOR "${CMAKE_CURRENT_SOURCE_DIR}/../../vendor_server")
add_executable(ckks_bench
    ckks_bench.cpp
    "${VENDOR}/src/rotation_hoisting.cpp")
target_include_directories(ckks_bench PRIVATE "${VENDOR}/include" "${CMAKE_CURRENT_SOURCE_DIR}")
target_compile_options(ckks_bench PRIVATE -mavx2 -Wall -Wextra)
target_link_libraries(ckks_bench PRIVATE SEAL::seal OpenMP::OpenMP_CXX Threads::Threads)
