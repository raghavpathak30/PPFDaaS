find_package(Python3 REQUIRED COMPONENTS Interpreter)

add_test(
    NAME verify_all
    COMMAND Python3::Interpreter ${CMAKE_SOURCE_DIR}/tests/verify_all.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
)

find_program(
    CATCH2_TEST_BIN
    NAMES test_he_core
    HINTS
        ${CMAKE_BINARY_DIR}
        ${CMAKE_BINARY_DIR}/vendor_server
)

if(CATCH2_TEST_BIN)
    add_test(NAME catch2_he_core COMMAND ${CATCH2_TEST_BIN})
endif()
