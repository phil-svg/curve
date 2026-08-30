BOOST_INC := $(shell brew --prefix boost)/include
JSON_INC  := $(shell brew --prefix nlohmann-json)/include

CXX      := clang++
# -I src lets the Yield Basis engine (which lives one directory up, in
# ../yieldbasis, and is pulled in by replay.cpp through the include path)
# still find crypto_math.hpp here.
CXXFLAGS := -std=c++17 -O2 -Wall -Wextra -I src -I ../yieldbasis \
	-I $(BOOST_INC) -I $(JSON_INC)

BUILD := build

all: $(BUILD)/replay $(BUILD)/replay_meta

$(BUILD)/replay: src/replay.cpp src/stable_classic.hpp src/stable_ng.hpp \
	src/tricrypto_ng.hpp ../yieldbasis/twocrypto_yb.hpp src/crypto_math.hpp \
	src/tricrypto2.hpp src/crypto2_classic.hpp src/twocrypto_ng.hpp \
	src/stable_meta.hpp src/stable_lending.hpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/replay.cpp -o $@

$(BUILD)/replay_meta: src/replay_meta.cpp src/stable_meta.hpp \
	src/stable_classic.hpp src/stable_ng.hpp
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) src/replay_meta.cpp -o $@

clean:
	rm -rf $(BUILD)

.PHONY: all clean
