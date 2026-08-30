// replay.cpp — event-replay validation driver.
//
//   ./replay <job.json> <out.json>
//
// The job carries one pool: engine kind, static config, initial on-chain
// state at the window-start block, and the decoded event list (with
// per-event ground-truth args and, for NG oracle pools, the stored_rates
// read from chain at each event's block). Each engine applies the events
// in order using ONLY the input legs (dx, amounts, burn...), returns its
// computed outputs per event plus the final state; the python harness
// compares computed vs expected and vs the on-chain end-block state.
//
// Engine kinds:
//   stable_v1        classic A_PRECISION=1  (3pool vintage)
//   stable_v2        classic factory plain, A_PRECISION=100
//   stable_ng        stableswap-ng (dynamic fee, admin_balances, stored_rates)
//   tricrypto_ng     tricrypto-NG (crypto3)
//   twocrypto_yb     Yield Basis Twocrypto v3.0.0 fork
//   tricrypto2       original tricrypto2 vintage (USD-BTC-ETH)
//   crypto2_classic  handwritten cryptoswap-2 vintages (cvxeth/eursusd/
//                    factory CurveCryptoSwap2ETH), flavor by job field
//   twocrypto_ng     Twocrypto v2.1.0d vintages (YB old family + iREET)
//   stable_meta      classic metapool on 3pool (embedded base machine)
//   stable_meta_ng   stableswap-ng metapool (embedded NG base machine)
//   stable_lending   IronBank cyToken pool (Compound accrual rates)
//
// All integer math is exact uint256 semantics (boost cpp_int, floor
// division); a vyper assert becomes {"revert": "<msg>"} for that event
// and the replay CONTINUES from the pre-event state (on-chain the tx
// still happened — a revert here is a mismatch the harness will flag).

#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

#include "crypto2_classic.hpp"
#include "stable_classic.hpp"
#include "stable_lending.hpp"
#include "stable_meta.hpp"
#include "stable_ng.hpp"
#include "tricrypto2.hpp"
#include "tricrypto_ng.hpp"
#include "twocrypto_ng.hpp"
#include "twocrypto_yb.hpp"

using json = nlohmann::json;

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: replay <job.json> <out.json>\n";
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
        if (kind == "stable_ng")
            out = run_stable_ng(job);
        else if (kind == "stable_v1" || kind == "stable_v2")
            out = run_stable_classic(job);
        else if (kind == "tricrypto_ng")
            out = run_tricrypto_ng(job);
        else if (kind == "twocrypto_yb")
            out = run_twocrypto_yb(job);
        else if (kind == "tricrypto2")
            out = run_tricrypto2(job);
        else if (kind == "crypto2_classic")
            out = run_crypto2_classic(job);
        else if (kind == "twocrypto_ng")
            out = run_twocrypto_ng(job);
        else if (kind == "stable_meta")
            out = run_stable_meta(job);
        else if (kind == "stable_meta_ng")
            out = run_stable_meta_ng(job);
        else if (kind == "stable_lending")
            out = run_stable_lending(job);
        else {
            out = { {"error", "unknown kind: " + kind} };
        }
    } catch (const std::exception& e) {
        out = { {"error", std::string("engine threw: ") + e.what()} };
    }
    std::ofstream f(argv[2]);
    f << out.dump(1);
    return 0;
}
