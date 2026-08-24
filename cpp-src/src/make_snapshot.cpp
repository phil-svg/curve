// make_snapshot.cpp — synthesize a LLAMMA snapshot through the REAL code path.
//
// Hand-writing band/share state cannot reproduce a partially soft-liquidated
// position: the x/y split and the share bookkeeping are the product of a
// deposit PLUS a history of band crossings. So instead we:
//   1. start from an empty AMM at a pre-crash price,
//   2. deposit every position pure-y via apply_deposit (the real deposit path),
//   3. walk the oracle down a price path, arbing the AMM back to the oracle at
//      each step — soft-liquidation then generates x endogenously.
// Every emitted state is therefore reachable by real trading.
//
// Usage:
//   ./build/make_snapshot <ref_snapshot.json> <positions.json> <pricepath.json> <out.json>
//
// positions.json: [{"user":"0x..","y0":"<wei>","n1":<int>,"n2":<int>,"debt":"<wei>"}, ...]
// pricepath.json: ["<price_1e18>", ...]   descending; first entry = deposit-time price

#include "llama_amm.hpp"
#include <nlohmann/json.hpp>
#include <cstdio>
#include <fstream>
#include <string>
#include <cstdlib>

using json = nlohmann::json;
static u256 U(const json& j) { return to_u256(j.get<std::string>()); }
static i256 I(const json& j) { return to_i256(j.get<std::string>()); }

// Equilibrate the AMM's marginal price to target WITHOUT a profit gate — this
// reconstructs history, it is not an economic actor. Bisects on trade size.
static void arb_to(const LlammaImmutables& im, LlammaState& s, u256 target_p) {
    if (target_p == 0) return;
    static const u256 ONE = ONE_1E18();
    if (s.block_timestamp > u256(120)) {          // bypass the 2-min oracle clamp
        s.prev_p_o_time = s.block_timestamp - u256(120);
        s.old_p_o = s.external_price;
        s.old_dfee = 0;
    }
    for (int outer = 0; outer < 6; ++outer) {     // repeat: one pass may not reach
        u256 cur = get_p(im, s);
        u256 tol = ONE / 5000;
        u256 diff = (cur > target_p) ? (cur - target_p) : (target_p - cur);
        if (diff < tol) return;
        bool pump = (cur < target_p);             // crvUSD in raises marginal price
        uint64_t i = pump ? 0 : 1, j = pump ? 1 : 0;
        u256 total_other = 0;
        for (auto& kv : s.bands) total_other += pump ? kv.second.y : kv.second.x;
        if (total_other == 0) return;
        u256 upper = pump ? (target_p * total_other) / ONE * u256(4) / im.COLLATERAL_PRECISION
                          : (total_other * ONE) / target_p * u256(4) / im.BORROWED_PRECISION;
        if (upper == 0) return;
        u256 lo = 0, hi = upper, best = 0;
        for (int it = 0; it < 48; ++it) {
            u256 mid = (lo + hi) / 2;
            if (mid == lo) break;
            LlammaState c = s;
            try { apply_trade_dx(im, c, i, j, mid); } catch (...) { hi = mid; continue; }
            u256 pm = get_p(im, c);
            u256 d = (pm > target_p) ? (pm - target_p) : (target_p - pm);
            bool overshot = pump ? (pm > target_p) : (pm < target_p);
            if (d < tol || overshot) { hi = mid; best = mid; } else { lo = mid; }
        }
        if (best == 0) return;
        try { apply_trade_dx(im, s, i, j, best); } catch (...) { return; }
    }
}

int main(int argc, char** argv) {
    if (argc < 5) {
        std::fprintf(stderr, "usage: %s <ref_snapshot.json> <positions.json> <pricepath.json> <out.json> [active_band]\n", argv[0]);
        return 1;
    }
    std::ifstream rf(argv[1]); json ref; rf >> ref;
    LlammaImmutables im; LlammaState s;
    const auto& imm = ref["immutables"];
    im.A = U(imm["A"]); im.Aminus1 = U(imm["Aminus1"]); im.A2 = U(imm["A2"]);
    im.Aminus12 = U(imm["Aminus12"]);
    im.BORROWED_PRECISION = U(imm["BORROWED_PRECISION"]);
    im.COLLATERAL_PRECISION = U(imm["COLLATERAL_PRECISION"]);
    im.BASE_PRICE = U(imm["BASE_PRICE"]);
    im.SQRT_BAND_RATIO = U(imm["SQRT_BAND_RATIO"]);
    im.LOG_A_RATIO = I(imm["LOG_A_RATIO"]);
    im.MAX_ORACLE_DN_POW = U(imm["MAX_ORACLE_DN_POW"]);
    const auto& st = ref["state"];
    s.fee = U(st["fee"]); s.admin_fee = U(st["admin_fee"]);
    s.rate = U(st["rate"]); s.rate_time = U(st["rate_time"]); s.rate_mul = U(st["rate_mul"]);
    s.block_timestamp = U(ref["timestamp"]);

    std::ifstream pf(argv[3]); json path; pf >> path;
    u256 p_start = to_u256(path[0].get<std::string>());
    s.external_price = p_start; s.old_p_o = p_start; s.old_dfee = 0;
    s.prev_p_o_time = s.block_timestamp;

    // active_band is MARKET state, not per-user: band n spans
    // [p_oracle_up(n+1), p_oracle_up(n)], so the band holding p_start is the
    // first n with p_oracle_up(n+1) <= p_start.  An explicit 5th argument
    // overrides it — use that to pin an abstracted book to the real market's
    // active_band at the same block, since the real value carries trade
    // hysteresis that no price alone can reproduce.
    i256 ab = 0;
    for (i256 n = i256(-200); n <= i256(600); n += 1) {
        ab = n;
        if (p_oracle_up(im, s, n + 1) <= p_start) break;
    }
    if (argc >= 6) ab = i256(std::atoll(argv[5]));
    s.active_band = ab; s.min_band = ab; s.max_band = ab;
    std::fprintf(stderr, "[make] p_start=%s active_band=%s (p_up=%s .. %s)\n",
                 p_start.str().c_str(), ab.str().c_str(),
                 p_oracle_up(im, s, ab + 1).str().c_str(), p_oracle_up(im, s, ab).str().c_str());

    std::ifstream posf(argv[2]); json positions; posf >> positions;
    for (auto& p : positions) {
        apply_deposit(im, s, p["user"].get<std::string>(), U(p["y0"]),
                      i256(p["n1"].get<int64_t>()), i256(p["n2"].get<int64_t>()));
        s.users[p["user"].get<std::string>()].debt = U(p["debt"]);
        s.users[p["user"].get<std::string>()].N =
            u256((uint64_t)(p["n2"].get<int64_t>() - p["n1"].get<int64_t>() + 1));
    }
    std::fprintf(stderr, "[make] deposited %zu positions, %zu bands, active_band=%s\n",
                 positions.size(), s.bands.size(), s.active_band.str().c_str());

    // Walk the oracle down; arb back to it at each step -> soft-liq creates x.
    for (size_t k = 1; k < path.size(); ++k) {
        s.block_timestamp += u256(12);
        s.external_price = to_u256(path[k].get<std::string>());
        arb_to(im, s, s.external_price);
    }
    std::fprintf(stderr, "[make] after price walk: active_band=%s get_p=%s\n",
                 s.active_band.str().c_str(), get_p(im, s).str().c_str());

    // Emit in the exact format load_snapshot() expects.
    json out;
    out["market"] = ref["market"];
    out["block"] = ref["block"];
    out["timestamp"] = ref["timestamp"];
    out["immutables"] = ref["immutables"];
    json js;
    js["fee"] = s.fee.str(); js["admin_fee"] = s.admin_fee.str();
    js["rate"] = s.rate.str(); js["rate_time"] = s.rate_time.str();
    js["rate_mul"] = s.rate_mul.str();
    js["active_band"] = s.active_band.str();
    js["min_band"] = s.min_band.str(); js["max_band"] = s.max_band.str();
    js["admin_fees_x"] = s.admin_fees_x.str(); js["admin_fees_y"] = s.admin_fees_y.str();
    js["old_p_o"] = s.old_p_o.str(); js["old_dfee"] = s.old_dfee.str();
    js["prev_p_o_time"] = s.prev_p_o_time.str();
    out["state"] = js;
    json jb = json::object();
    for (auto& kv : s.bands) {
        if (kv.second.x == 0 && kv.second.y == 0 && kv.second.shares == 0) continue;
        jb[std::to_string(kv.first)] = { {"x", kv.second.x.str()}, {"y", kv.second.y.str()},
                                         {"shares", kv.second.shares.str()} };
    }
    out["bands"] = jb;
    json ju = json::object();
    for (auto& kv : s.users) {
        json sh = json::object();
        for (auto& q : kv.second.shares) sh[std::to_string(q.first)] = q.second.str();
        auto [sx, sy] = get_sum_xy(im, s, kv.first);
        ju[kv.first] = { {"ns0", kv.second.ns0.str()}, {"ns1", kv.second.ns1.str()},
                         {"shares", sh}, {"collateral", sy.str()},
                         {"stablecoin", sx.str()}, {"debt", kv.second.debt.str()},
                         {"N", kv.second.N.str()} };
    }
    out["users"] = ju;
    out["external_oracle_price"] = s.external_price.str();
    std::ofstream of(argv[4]); of << out.dump();
    std::fprintf(stderr, "[make] wrote %s (%zu bands, %zu users)\n", argv[4], jb.size(), ju.size());
    return 0;
}
