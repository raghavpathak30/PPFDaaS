# CMake generated Testfile for 
# Source directory: /home/raghavp/BTP/tests
# Build directory: /home/raghavp/BTP/build/tests
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test([=[verify_all]=] "/usr/bin/python3" "/home/raghavp/BTP/tests/verify_all.py")
set_tests_properties([=[verify_all]=] PROPERTIES  WORKING_DIRECTORY "/home/raghavp/BTP" _BACKTRACE_TRIPLES "/home/raghavp/BTP/tests/CMakeLists.txt;3;add_test;/home/raghavp/BTP/tests/CMakeLists.txt;0;")
add_test([=[catch2_he_core]=] "/home/raghavp/BTP/build/vendor_server/test_he_core")
set_tests_properties([=[catch2_he_core]=] PROPERTIES  _BACKTRACE_TRIPLES "/home/raghavp/BTP/tests/CMakeLists.txt;18;add_test;/home/raghavp/BTP/tests/CMakeLists.txt;0;")
