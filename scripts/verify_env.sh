#!/bin/bash
# scripts/verify_env.sh — Run BEFORE any cmake build. Exit code 0 = all clear.
set -e
grep -q 'avx2' /proc/cpuinfo && echo '[PASS] AVX2' || { echo '[FAIL] AVX2 missing — 10ms target unachievable'; exit 1; }
g++ -std=c++17 -x c++ - <<< 'int main(){}' -o /dev/null && echo '[PASS] C++17'
g++ -fopenmp -x c++ - <<< '#include<omp.h>
int main(){}' -o /tmp/_omp && echo '[PASS] OpenMP'
python3 -c 'import sys; assert sys.version_info>=(3,10); print("[PASS] Python",sys.version.split()[0])'
python3 -c 'import numpy,pybind11,grpc,xgboost,shap,imblearn,scipy; print("[PASS] Python deps")'
protoc --version | grep -E '[3-9]\.[2-9][0-9]' && echo '[PASS] protoc >= 3.21'
cmake --version | head -1
