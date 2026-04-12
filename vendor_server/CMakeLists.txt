cmake_minimum_required(VERSION 3.20)
project(vendor_server LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

list(PREPEND CMAKE_PREFIX_PATH "/usr/local")
find_package(SEAL 4.1 REQUIRED CONFIG)

find_package(OpenMP REQUIRED)

add_library(he_core STATIC
    src/ckks_context.cpp
    src/weight_loader.cpp
    src/rotation_hoisting.cpp
    src/he_inference.cpp
)
target_include_directories(he_core PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(he_core PUBLIC SEAL::seal OpenMP::OpenMP_CXX)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(he_core PUBLIC -mavx2)
elseif(MSVC)
    target_compile_options(he_core PUBLIC /arch:AVX2)
endif()

add_executable(ckks_smoke_test src/smoke_test.cpp)
target_link_libraries(ckks_smoke_test PRIVATE he_core)

add_executable(benchmark src/benchmark.cpp)
target_link_libraries(benchmark PRIVATE he_core)

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
