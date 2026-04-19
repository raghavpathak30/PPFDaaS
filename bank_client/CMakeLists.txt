find_package(Python3 REQUIRED COMPONENTS Interpreter Development.Module)
execute_process(
    COMMAND "${Python3_EXECUTABLE}" -m pybind11 --cmakedir
    OUTPUT_VARIABLE pybind11_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
)

list(PREPEND CMAKE_PREFIX_PATH "/usr/local")
find_package(SEAL 4.1 REQUIRED CONFIG)
find_package(pybind11 CONFIG REQUIRED PATHS "${pybind11_DIR}" NO_DEFAULT_PATH)
find_package(OpenMP REQUIRED)

pybind11_add_module(seal_wrapper MODULE he_wrapper/seal_wrapper.cpp)
target_link_libraries(seal_wrapper PRIVATE SEAL::seal OpenMP::OpenMP_CXX)
set_target_properties(seal_wrapper PROPERTIES
    PREFIX ""
    OUTPUT_NAME "seal_wrapper"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
)

pybind11_add_module(seal_wrapper_160 MODULE he_wrapper/seal_wrapper_160.cpp)
target_link_libraries(seal_wrapper_160 PRIVATE SEAL::seal OpenMP::OpenMP_CXX)
set_target_properties(seal_wrapper_160 PROPERTIES
    PREFIX ""
    OUTPUT_NAME "seal_wrapper_160"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
)

add_executable(generate_seal_keys tools/generate_seal_keys.cpp)
target_include_directories(generate_seal_keys PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../vendor_server/include"
)
target_link_libraries(generate_seal_keys PRIVATE he_core OpenMP::OpenMP_CXX)
target_compile_definitions(generate_seal_keys PRIVATE
    PPFDAAS_REPO_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/.."
)
