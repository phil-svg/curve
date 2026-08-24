// book_test.cpp — scripted-trade replay used to verify the Python port of the
// LLAMMA engine (llamma_book.py) against this C++ implementation, wei-exact.
//
// Usage: ./build/book_test <snapshot.json> <script.json>
//
// script.json: [ {"ts": N, "oracle": "<wei>", "bypass": true|false,
//                 "i": 0|1, "j": 1|0, "dx": "<wei>"}, ... ]
//   dx == "0" means: no trade, just set context and dump state.
//
// Emits one JSON object per step on stdout with the full observable state:
// active_band, per-band x/y/shares, get_p, and per-user x_down/sum_xy/health.
#include "llama_amm.hpp"
#include <nlohmann/json.hpp>
#include <fstream>
#include <iostream>

using json = nlohmann::json;

static u256 U(const json& j) { return to_u256(j.get<std::string>()); }
static i256 I(const json& j) { return to_i256(j.get<std::string>()); }

// Same loader as sweep_precompute.cpp.
static void load_snapshot(const std::string& path, LlammaImmutables& im, LlammaState& s) {
    std::ifstream f(path); json j; f >> j;
    const auto& imm = j["immutables"];
    im.A = U(imm["A"]); im.Aminus1 = U(imm["Aminus1"]);
    im.A2 = U(imm["A2"]); im.Aminus12 = U(imm["Aminus12"]);
    im.BORROWED_PRECISION = U(imm["BORROWED_PRECISION"]);
    im.COLLATERAL_PRECISION = U(imm["COLLATERAL_PRECISION"]);
    im.BASE_PRICE = U(imm["BASE_PRICE"]);
    im.SQRT_BAND_RATIO = U(imm["SQRT_BAND_RATIO"]);
    im.LOG_A_RATIO = I(imm["LOG_A_RATIO"]);
    im.MAX_ORACLE_DN_POW = U(imm["MAX_ORACLE_DN_POW"]);
    const auto& st = j["state"];
    s.fee = U(st["fee"]); s.admin_fee = U(st["admin_fee"]);
    s.rate = U(st["rate"]); s.rate_time = U(st["rate_time"]);
    s.rate_mul = U(st["rate_mul"]);
    s.active_band = I(st["active_band"]);
    s.min_band = I(st["min_band"]); s.max_band = I(st["max_band"]);
    s.admin_fees_x = U(st["admin_fees_x"]); s.admin_fees_y = U(st["admin_fees_y"]);
    s.old_p_o = U(st["old_p_o"]); s.old_dfee = U(st["old_dfee"]);
    s.prev_p_o_time = U(st["prev_p_o_time"]);
    s.block_timestamp = U(j["timestamp"]);
    s.external_price = U(j["external_oracle_price"]);
    for (auto it = j["bands"].begin(); it != j["bands"].end(); ++it) {
        int64_t b = std::stoll(it.key());
        BandState bs;
        bs.x = U(it.value()["x"]); bs.y = U(it.value()["y"]); bs.shares = U(it.value()["shares"]);
        s.bands[b] = bs;
    }
    for (auto it = j["users"].begin(); it != j["users"].end(); ++it) {
        UserTicks ut;
        ut.ns0 = i256(it.value()["ns0"].get<std::string>());
        ut.ns1 = i256(it.value()["ns1"].get<std::string>());
        if (it.value().contains("shares"))
            for (auto sit = it.value()["shares"].begin(); sit != it.value()["shares"].end(); ++sit)
                ut.shares[std::stoll(sit.key())] = U(sit.value());
        if (it.value().contains("debt")) ut.debt = U(it.value()["debt"]);
        s.users[it.key()] = ut;
    }
}

int main(int argc, char** argv) {
    if (argc < 3) { std::fprintf(stderr, "usage: %s <snap.json> <script.json>\n", argv[0]); return 1; }
    LlammaImmutables im; LlammaState s;
    load_snapshot(argv[1], im, s);
    std::ifstream sf(argv[2]); json script; sf >> script;

    static const u256 DISC = u256("28000000000000000");   // 2.8% — arbitrary fixed probe

    json out = json::array();
    for (const auto& step : script) {
        s.block_timestamp = u256(step["ts"].get<uint64_t>());
        s.external_price = U(step["oracle"]);
        if (step.value("bypass", false) && s.block_timestamp > u256(120)) {
            s.prev_p_o_time = s.block_timestamp - u256(120);
            s.old_p_o = s.external_price;
            s.old_dfee = 0;
        }
        u256 dx = U(step["dx"]);
        u256 in_done = 0, out_done = 0;
        if (dx > 0) {
            LlammaState before = s;
            apply_trade_dx(im, s, step["i"].get<uint64_t>(), step["j"].get<uint64_t>(), dx);
            // infer (in,out) from band deltas like arb_to_target_price does
            bool pump = step["i"].get<uint64_t>() == 0;
            for (auto& kv : before.bands) {
                auto it2 = s.bands.find(kv.first);
                if (it2 == s.bands.end()) continue;
                if (pump) { if (kv.second.y > it2->second.y) out_done += kv.second.y - it2->second.y; }
                else      { if (kv.second.x > it2->second.x) out_done += kv.second.x - it2->second.x; }
            }
            in_done = dx;
        }
        json bands = json::object();
        for (auto& kv : s.bands)
            if (kv.second.x != 0 || kv.second.y != 0 || kv.second.shares != 0)
                bands[std::to_string(kv.first)] = { {"x", kv.second.x.str()},
                    {"y", kv.second.y.str()}, {"shares", kv.second.shares.str()} };
        json users = json::object();
        for (auto& kv : s.users) {
            auto [sx, sy] = get_sum_xy(im, s, kv.first);
            users[kv.first] = {
                {"x_down", get_x_down(im, s, kv.first).str()},
                {"sum_x", sx.str()}, {"sum_y", sy.str()},
                {"health", compute_health(im, s, kv.first, DISC, true).str()},
            };
        }
        out.push_back({ {"active_band", s.active_band.str()},
                        {"get_p", get_p(im, s).str()},
                        {"in_done", in_done.str()}, {"out_done", out_done.str()},
                        {"bands", bands}, {"users", users} });
    }
    std::cout << out.dump() << std::endl;
    return 0;
}
