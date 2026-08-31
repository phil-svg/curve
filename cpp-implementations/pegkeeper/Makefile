# PegKeeper replay engine. deps: brew boost + nlohmann-json (same as the
# other families). u256.hpp is shared with the llamalend ports.
CXX ?= clang++
BOOST_INC ?= $(shell brew --prefix boost 2>/dev/null)/include
JSON_INC ?= $(shell brew --prefix nlohmann-json 2>/dev/null)/include
CXXFLAGS = -std=c++17 -O2 -DNDEBUG -Wall -Wextra \
	-I src -I ../llamalend/llv1/src -I $(BOOST_INC) -I $(JSON_INC)

build/replay: src/replay.cpp src/pegkeeper.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) src/replay.cpp -o $@

clean:
	rm -rf build
