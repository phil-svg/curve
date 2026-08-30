// replay_meta.cpp — STANDALONE event-replay driver for the metapool engines
// (kept separate so src/replay.cpp is untouched; the parent wires
// stable_meta.hpp into the main dispatcher later).
//
//   ./replay_meta <job.json> <out.json>
//
// Kinds:
//   stable_meta     classic factory metapool (embedded stable_v1/v2 base)
//   stable_meta_ng  stableswap-ng metapool  (embedded stable_ng base)
//
// Compile:
//   clang++ -std=c++17 -O2 -I$(brew --prefix boost)/include \
//       -I$(brew --prefix nlohmann-json)/include \
//       src/replay_meta.cpp -o build/replay_meta

#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

#include "stable_meta.hpp"

using json = nlohmann::json;

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: replay_meta <job.json> <out.json>\n";
        return 2;
    }
    json job;
    {
        std::ifstream f(argv[1]);
        if (!f) { std::cerr << "cannot read " << argv[1] << "\n"; return 2; }
        f >> job;
    }
    json out;
    const std::string kind = job.at("kind");
    try {
        if (kind == "stable_meta")
            out = run_stable_meta(job);
        else if (kind == "stable_meta_ng")
            out = run_stable_meta_ng(job);
        else
            out = { {"error", "unknown kind: " + kind} };
    } catch (const std::exception& e) {
        out = { {"error", std::string("engine threw: ") + e.what()} };
    }
    std::ofstream f(argv[2]);
    f << out.dump(1);
    return 0;
}
