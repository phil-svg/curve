// replay.cpp — historical-sample validation driver for the monetary-policy
// engines (mpolicies.hpp).
//
//   replay --job job.json
//
// Job JSON: { "kind": "semilog"|"secondary", "samples": [ {
//   "block": n, "chain_rate": "<dec>",       // policy.rate(controller)
//   "total_debt": ..., "balance": ...,       // controller inputs
//   // semilog: "min_rate", "log_min_rate", "log_max_rate" (signed str)
//   // secondary: "u_inf","A","r_minf","shift","r0"
// } ] }
#include "mpolicies.hpp"

#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

using nlohmann::json;
using namespace mpolicies;

static u256 U(const json& j) { return to_u256(j.get<std::string>()); }
static z256 Z(const json& j) { return z256(j.get<std::string>()); }

int main(int argc, char** argv) {
    std::string job_path;
    for (int i = 1; i + 1 < argc; i += 2)
        if (std::string(argv[i]) == "--job") job_path = argv[i + 1];
    if (job_path.empty()) {
        std::cerr << "usage: replay --job job.json\n";
        return 2;
    }
    json job;
    std::ifstream(job_path) >> job;
    const std::string kind = job.at("kind").get<std::string>();
    const bool semilog = kind == "semilog";
    const bool flat = kind == "flat_linear";
    const bool sec_ema = kind == "secondary_ema";
    const bool llv2 = kind == "llv2_dynamic";

    size_t n = 0, ok = 0, mism = 0;
    for (const auto& s : job.at("samples")) {
        n++;
        u256 got;
        if (flat) {
            got = flat_linear_rate(U(s.at("base_rate")), U(s.at("slope")),
                                   U(s.at("snapshot_time")), U(s.at("ts")),
                                   U(s.at("min_rate")), U(s.at("max_rate")));
        } else if (llv2) {
            // two-layer: the stored curve must equal what we derive from
            // the raw knobs, then the rate itself
            llv2_params p = llv2_get_params(U(s.at("target_utilization")),
                                            U(s.at("low_ratio")),
                                            U(s.at("high_ratio")));
            if (p.u_inf != U(s.at("u_inf")) || p.A != U(s.at("A"))
                    || p.r_minf != Z(s.at("r_minf"))) {
                mism++;
                if (mism <= 10)
                    std::cout << json{{"block", s.at("block")},
                                      {"params_got",
                                       {p.u_inf.str(), p.A.str(),
                                        p.r_minf.str()}},
                                      {"params_want",
                                       {s.at("u_inf"), s.at("A"),
                                        s.at("r_minf")}}}.dump() << "\n";
                continue;
            }
            got = llv2_dynamic_rate(U(s.at("total_debt")),
                                    U(s.at("available_balance")),
                                    U(s.at("admin_fees")), p.u_inf, p.A,
                                    p.r_minf, U(s.at("rate_shift")),
                                    U(s.at("raw_calc_rate")));
        } else if (sec_ema) {
            // two-layer: our EMA from raw state, then the signed hyperbola
            u256 r0 = ema_rate(U(s.at("prev_rate")),
                               U(s.at("prev_ma_rate")),
                               U(s.at("last_timestamp")), U(s.at("ts")));
            if (r0 != U(s.at("chain_ma_rate"))) {
                mism++;
                if (mism <= 10)
                    std::cout << json{{"block", s.at("block")},
                                      {"ema_got", r0.str()},
                                      {"ema_want",
                                       s.at("chain_ma_rate")}}.dump()
                              << "\n";
                continue;
            }
            got = ema_secondary_rate(U(s.at("total_debt")),
                                     U(s.at("balance")), U(s.at("u_inf")),
                                     U(s.at("A")), Z(s.at("r_minf")),
                                     U(s.at("shift")), r0);
        } else if (semilog) {
            // logs recomputed with our ln port (the contracts store
            // ln_int(min/max) at deploy; older revisions don't expose them)
            const u256 mn = U(s.at("min_rate"));
            const u256 mx = U(s.at("max_rate"));
            got = semilog_rate(U(s.at("total_debt")), U(s.at("balance")),
                               mn, semilog_ln(mn), semilog_ln(mx));
        }
        else
            got = secondary_rate(U(s.at("total_debt")), U(s.at("balance")),
                                 U(s.at("u_inf")), U(s.at("A")),
                                 U(s.at("r_minf")), U(s.at("shift")),
                                 U(s.at("r0")));
        u256 want = U(s.at("chain_rate"));
        if (got == want) {
            ok++;
        } else {
            mism++;
            if (mism <= 10)
                std::cout << json{{"block", s.at("block")},
                                  {"got", got.str()},
                                  {"want", want.str()}}.dump() << "\n";
        }
    }
    std::cout << json{{"samples", n}, {"ok", ok},
                      {"mismatches", mism}}.dump() << std::endl;
    return mism == 0 ? 0 : 1;
}
