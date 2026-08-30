BOOST_INC   := $(shell brew --prefix boost)/include
JSON_INC    := $(shell brew --prefix nlohmann-json)/include

CXX      := clang++
CXXFLAGS := -std=c++17 -O2 -Wall -Wextra -I src -I $(BOOST_INC) -I $(JSON_INC)

SRC := src/llama_amm.cpp
HDR := src/llama_amm.hpp src/u256.hpp

BUILD := build

all: $(BUILD)/replay

$(BUILD)/replay: $(SRC) $(HDR) src/replay.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/replay.cpp $(SRC) -o $@

clean:
	rm -rf $(BUILD)

.PHONY: all clean
