#!/bin/zsh
# WASM builds of the S.L./D.L. engines for the browser runner.
# Flags mirror the validated dev/wasm-poc recipe (512 MB initial memory =
# the value the shipped, parity-checked PoC artifacts were built with).
# Rebuild after any cpp-src engine change, then re-run wasm/test/ parity.
set -e
cd "$(dirname "$0")"
JSON_INC="$(brew --prefix nlohmann-json)/include"

COMMON=(-std=c++17 -O3 -DNDEBUG -fno-math-errno -flto -msimd128
  -I "$JSON_INC"
  -pthread -sPROXY_TO_PTHREAD
  "-sPTHREAD_POOL_SIZE=((globalThis.navigator&&globalThis.navigator.hardwareConcurrency)||8)+1"
  -sMODULARIZE=1
  -sINITIAL_MEMORY=536870912 -sALLOW_MEMORY_GROWTH=1
  -sEXIT_RUNTIME=1 -sINVOKE_RUN=0 -sFORCE_FILESYSTEM=1
  -sEXPORTED_RUNTIME_METHODS=callMain,FS
  -sENVIRONMENT=web,worker,node)

em++ "${COMMON[@]}" ../cpp-src/src/ref_model_v2.cpp \
  -o ref_model_v2.js -sEXPORT_NAME=RefModelV2
em++ "${COMMON[@]}" ../cpp-src/src/ref_model.cpp \
  -o ref_model_v1.js -sEXPORT_NAME=RefModelV1

em++ --version | head -1 > BUILD_INFO.txt
ls -la ref_model_v1.js ref_model_v1.wasm ref_model_v2.js ref_model_v2.wasm
