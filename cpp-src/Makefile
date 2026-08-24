# macOS: resolved via Homebrew. Linux: brew is absent, so override on the
# command line if headers are not on the default include path, e.g.
#   make BOOST_INC=/usr/include JSON_INC=/usr/include NATIVE=-march=native
BOOST_INC   ?= $(shell brew --prefix boost 2>/dev/null)/include
JSON_INC    ?= $(shell brew --prefix nlohmann-json 2>/dev/null)/include
NATIVE      ?= -mcpu=native

CXX      := clang++
# $(NATIVE): schedule for this machine's cores; -DNDEBUG: strip boost
# asserts from the bigint hot path; -flto: cross-TU inlining. None of these
# can change integer results — verified bit-identical via the parity gate and
# a full-sweep diff (2026-08-15).
CXXFLAGS := -std=c++17 -O3 -mcpu=native -DNDEBUG -flto -fno-math-errno -Wall -Wextra \
            -I src -I $(BOOST_INC) -I $(JSON_INC)

SRC := src/llama_amm.cpp
HDR := src/llama_amm.hpp src/u256.hpp

BUILD := ../bin

all: $(BUILD)/replay $(BUILD)/sweep_precompute $(BUILD)/make_snapshot $(BUILD)/book_test $(BUILD)/routed_engine $(BUILD)/ref_model $(BUILD)/ref_model_v2

# probe_poracle / probe_price_oracle were dev utilities used to verify the C++
# LLAMMA port against on-chain math. Sources + built binaries live in
# `new code/debugging-artifacts/cpp/` — rebuild from there if ever needed.

$(BUILD)/replay: $(SRC) $(HDR) src/replay.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/replay.cpp $(SRC) -o $@

$(BUILD)/sweep_precompute: $(SRC) $(HDR) src/sweep_precompute.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/sweep_precompute.cpp $(SRC) -o $@

$(BUILD)/make_snapshot: $(SRC) $(HDR) src/make_snapshot.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/make_snapshot.cpp $(SRC) -o $@

$(BUILD)/book_test: $(SRC) $(HDR) src/book_test.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/book_test.cpp $(SRC) -o $@

$(BUILD)/ref_model_v2: src/ref_model_v2.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/ref_model_v2.cpp -o $@

$(BUILD)/ref_model: src/ref_model.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/ref_model.cpp -o $@

$(BUILD)/routed_engine: $(SRC) $(HDR) src/venue.hpp src/routed_engine.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/routed_engine.cpp $(SRC) -o $@

clean:
	rm -rf $(BUILD)

.PHONY: all clean
