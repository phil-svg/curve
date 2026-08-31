# Monetary-policy engines. deps: brew boost + nlohmann-json; u256.hpp
# shared with the llamalend ports.
CXX ?= clang++
BOOST_INC ?= $(shell brew --prefix boost 2>/dev/null)/include
JSON_INC ?= $(shell brew --prefix nlohmann-json 2>/dev/null)/include
CXXFLAGS = -std=c++17 -O2 -DNDEBUG -Wall -Wextra \
	-I . -I ../llamalend/llv1/src -I $(BOOST_INC) -I $(JSON_INC)

build/replay: replay.cpp mpolicies.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) replay.cpp -o $@

clean:
	rm -rf build
