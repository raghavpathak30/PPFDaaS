cmake_minimum_required(VERSION 3.20)
project(vendor_server LANGUAGES CXX)

option(PPFDAAS_ENABLE_DEGREE2 "Build the Degree-2 fallback path" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

list(PREPEND CMAKE_PREFIX_PATH "/usr/local")
find_package(SEAL 4.1 REQUIRED CONFIG)

find_package(Protobuf REQUIRED)
find_package(gRPC REQUIRED CONFIG)

find_package(OpenMP REQUIRED)

find_program(PROTOC_EXECUTABLE protoc REQUIRED)
if(NOT PROTOC_EXECUTABLE)
    message(FATAL_ERROR "protoc not found: required to generate vendor_server/generated/inference.pb.cc and inference.grpc.pb.cc")
endif()

find_program(GRPC_CPP_PLUGIN_EXECUTABLE grpc_cpp_plugin REQUIRED)
if(NOT GRPC_CPP_PLUGIN_EXECUTABLE)
    message(FATAL_ERROR "grpc_cpp_plugin not found: required to generate vendor_server/generated/inference.grpc.pb.cc")
endif()

set(PROTO_FILE "${CMAKE_CURRENT_SOURCE_DIR}/../proto/inference.proto")
set(GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/generated")

set(PROTO_GENERATED_SOURCES
    "${GENERATED_DIR}/inference.pb.cc"
    "${GENERATED_DIR}/inference.grpc.pb.cc"
)

add_custom_command(
    OUTPUT
        "${GENERATED_DIR}/inference.pb.cc"
        "${GENERATED_DIR}/inference.pb.h"
        "${GENERATED_DIR}/inference.grpc.pb.cc"
        "${GENERATED_DIR}/inference.grpc.pb.h"
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${GENERATED_DIR}"
    COMMAND "${PROTOC_EXECUTABLE}"
            --proto_path "${CMAKE_CURRENT_SOURCE_DIR}/../proto"
            --cpp_out "${GENERATED_DIR}"
            --grpc_out "${GENERATED_DIR}"
            --plugin=protoc-gen-grpc=${GRPC_CPP_PLUGIN_EXECUTABLE}
            "${PROTO_FILE}"
    DEPENDS "${PROTO_FILE}"
    VERBATIM
)

add_custom_target(generate_inference_proto DEPENDS ${PROTO_GENERATED_SOURCES})

add_library(he_core STATIC
    ${PROTO_GENERATED_SOURCES}
    src/ckks_context.cpp
    src/inference_service.cpp
    src/weight_loader.cpp
    src/rotation_hoisting.cpp
    src/he_inference.cpp
    $<$<BOOL:${PPFDAAS_ENABLE_DEGREE2}>:src/ckks_context_depth2.cpp>
    $<$<BOOL:${PPFDAAS_ENABLE_DEGREE2}>:src/weight_loader_degree2.cpp>
    $<$<BOOL:${PPFDAAS_ENABLE_DEGREE2}>:src/rotation_hoisting_degree2.cpp>
)
add_dependencies(he_core generate_inference_proto)
target_include_directories(he_core PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
    "${GENERATED_DIR}"
)
# he_inference.h declares depth1_he_inference_160(CKKSContext160&, ...) (used
# only by benchmark_160.cpp) and therefore needs the CKKSContext160 type
# declaration. Header-only include, PUBLIC because he_inference.h is included
# directly by other targets (e.g. benchmark.cpp): ckks_context_160.cpp (the
# file holding the live SecretKey/Decryptor/KeyGenerator, OUT OF TCB) is
# compiled ONLY into the benchmark_160 target below, never into he_core or any
# binary that links it.
target_include_directories(he_core PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/../tools/local_benchmark"
)
target_link_libraries(he_core PUBLIC
    SEAL::seal
    gRPC::grpc++
    protobuf::libprotobuf
    OpenMP::OpenMP_CXX
)
target_compile_definitions(he_core PUBLIC
    PPFDAAS_REPO_ROOT="${CMAKE_SOURCE_DIR}"
)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(he_core PUBLIC -mavx2 -O3)
    include(CheckCXXCompilerFlag)
    check_cxx_compiler_flag("-mavx512f" COMPILER_HAS_AVX512F)
    execute_process(
        COMMAND sh -c "grep -qm1 avx512f /proc/cpuinfo"
        RESULT_VARIABLE CPU_HAS_AVX512F
    )
    if(COMPILER_HAS_AVX512F AND CPU_HAS_AVX512F EQUAL 0)
        target_compile_options(he_core PUBLIC -mavx512f -mavx512dq)
        message(STATUS "[PPFDaaS] AVX-512 enabled")
    else()
        message(STATUS "[PPFDaaS] AVX-512 not available, using AVX2")
    endif()
elseif(MSVC)
    target_compile_options(he_core PUBLIC /arch:AVX2)
endif()

add_executable(ckks_smoke_test src/smoke_test.cpp)
target_link_libraries(ckks_smoke_test PRIVATE he_core)

add_executable(benchmark src/benchmark.cpp)
target_link_libraries(benchmark PRIVATE he_core)

add_executable(benchmark_160
    src/benchmark_160.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../tools/local_benchmark/ckks_context_160.cpp
    src/he_inference.cpp
    src/rotation_hoisting.cpp
)
target_link_libraries(benchmark_160 PRIVATE
    SEAL::seal
    OpenMP::OpenMP_CXX
)
target_include_directories(benchmark_160 PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/../tools/local_benchmark"
)
target_compile_definitions(benchmark_160 PRIVATE
    PPFDAAS_REPO_ROOT="${CMAKE_SOURCE_DIR}"
)

add_executable(vendor_server_main src/server_main.cpp)
target_link_libraries(vendor_server_main PRIVATE he_core)
target_compile_definitions(vendor_server_main PRIVATE
    PPFDAAS_REPO_ROOT="${CMAKE_SOURCE_DIR}"
)

add_executable(vendor_server_160
    src/vendor_server_160.cpp
    src/inference_service_160.cpp
    src/eval_context_160.cpp
)
target_link_libraries(vendor_server_160 PRIVATE
    he_core
    SEAL::seal
    gRPC::grpc++
    protobuf::libprotobuf
    OpenMP::OpenMP_CXX
)
target_include_directories(vendor_server_160 PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
    "${GENERATED_DIR}"
)
target_compile_definitions(vendor_server_160 PRIVATE
    PPFDAAS_REPO_ROOT="${CMAKE_SOURCE_DIR}"
)

include(FetchContent)
FetchContent_Declare(
    Catch2
    GIT_REPOSITORY https://github.com/catchorg/Catch2.git
    GIT_TAG v3.5.4
)
FetchContent_MakeAvailable(Catch2)

add_executable(test_he_core tests/test_he_core.cpp)
target_link_libraries(test_he_core PRIVATE he_core Catch2::Catch2WithMain)

enable_testing()
add_test(NAME he_core COMMAND test_he_core)
