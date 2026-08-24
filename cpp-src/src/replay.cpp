// replay.cpp — main event-replay driver.
//
// Reads: snapshot.json (initial state at block N), events.json (chronological
// events in [N+1, M]).
// Emits: state.json (LLAMMA state at block M) for verification.
//
// Usage:
//   ./build/replay <snapshot.json> <events.json> <out_state.json>

#include "llama_amm.hpp"
#include <nlohmann/json.hpp>
#include <cstdio>
#include <fstream>
#include <string>
#include <vector>

using json = nlohmann::json;

static u256 U(const json& j) { return to_u256(j.get<std::string>()); }
static i256 I(const json& j) { return to_i256(j.get<std::string>()); }

static void load_snapshot(const std::string& path, LlammaImmutables& im, LlammaState& s) {
    std::ifstream f(path);
    json j; f >> j;

    const auto& imm = j["immutables"];
    im.A                    = U(imm["A"]);
    im.Aminus1              = U(imm["Aminus1"]);
    im.A2                   = U(imm["A2"]);
    im.Aminus12             = U(imm["Aminus12"]);
    im.BORROWED_PRECISION   = U(imm["BORROWED_PRECISION"]);
    im.COLLATERAL_PRECISION = U(imm["COLLATERAL_PRECISION"]);
    im.BASE_PRICE           = U(imm["BASE_PRICE"]);
    im.SQRT_BAND_RATIO      = U(imm["SQRT_BAND_RATIO"]);
    im.LOG_A_RATIO          = I(imm["LOG_A_RATIO"]);
    im.MAX_ORACLE_DN_POW    = U(imm["MAX_ORACLE_DN_POW"]);

    const auto& st = j["state"];
    s.fee            = U(st["fee"]);
    s.admin_fee      = U(st["admin_fee"]);
    s.rate           = U(st["rate"]);
    s.rate_time      = U(st["rate_time"]);
    s.rate_mul       = U(st["rate_mul"]);
    s.active_band    = I(st["active_band"]);
    s.min_band       = I(st["min_band"]);
    s.max_band       = I(st["max_band"]);
    s.admin_fees_x   = U(st["admin_fees_x"]);
    s.admin_fees_y   = U(st["admin_fees_y"]);
    s.old_p_o        = U(st["old_p_o"]);
    s.old_dfee       = U(st["old_dfee"]);
    s.prev_p_o_time  = U(st["prev_p_o_time"]);
    s.block_timestamp = U(j["timestamp"]);
    s.external_price  = U(j["external_oracle_price"]);

    for (auto it = j["bands"].begin(); it != j["bands"].end(); ++it) {
        int64_t b = std::stoll(it.key());
        BandState bs;
        bs.x      = U(it.value()["x"]);
        bs.y      = U(it.value()["y"]);
        bs.shares = U(it.value()["shares"]);
        s.bands[b] = bs;
    }
    for (auto it = j["users"].begin(); it != j["users"].end(); ++it) {
        UserTicks ut;
        ut.ns0 = i256(it.value()["ns0"].get<std::string>());
        ut.ns1 = i256(it.value()["ns1"].get<std::string>());
        if (it.value().contains("shares")) {
            for (auto sit = it.value()["shares"].begin(); sit != it.value()["shares"].end(); ++sit) {
                int64_t b = std::stoll(sit.key());
                ut.shares[b] = U(sit.value());
            }
        }
        s.users[it.key()] = ut;
    }
}

static void dump_state(const LlammaState& s, const std::string& out_path) {
    json j;
    j["state"] = {
        {"fee",           s.fee.str()},
        {"admin_fee",     s.admin_fee.str()},
        {"rate",          s.rate.str()},
        {"rate_time",     s.rate_time.str()},
        {"rate_mul",      s.rate_mul.str()},
        {"active_band",   s.active_band.str()},
        {"min_band",      s.min_band.str()},
        {"max_band",      s.max_band.str()},
        {"admin_fees_x",  s.admin_fees_x.str()},
        {"admin_fees_y",  s.admin_fees_y.str()},
        {"old_p_o",       s.old_p_o.str()},
        {"old_dfee",      s.old_dfee.str()},
        {"prev_p_o_time", s.prev_p_o_time.str()},
    };
    j["timestamp"] = s.block_timestamp.str();
    j["bands"] = json::object();
    for (auto& kv : s.bands) {
        // Skip all-zero bands — on-chain `bands_x/y/total_shares` return 0 for
        // any key not explicitly written, so these carry no information.
        if (kv.second.x == 0 && kv.second.y == 0 && kv.second.shares == 0) continue;
        json bj = {{"x", kv.second.x.str()}, {"y", kv.second.y.str()}, {"shares", kv.second.shares.str()}};
        j["bands"][std::to_string(kv.first)] = bj;
    }
    j["users"] = json::object();
    for (auto& kv : s.users) {
        json u;
        u["ns0"] = kv.second.ns0.str();
        u["ns1"] = kv.second.ns1.str();
        json sh = json::object();
        for (auto& shv : kv.second.shares) sh[std::to_string(shv.first)] = shv.second.str();
        u["shares"] = sh;
        j["users"][kv.first] = u;
    }
    std::ofstream o(out_path);
    o << j.dump(2);
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::fprintf(stderr, "usage: %s <snapshot.json> <events.json> <out_state.json>\n", argv[0]);
        return 1;
    }
    LlammaImmutables im;
    LlammaState s;
    load_snapshot(argv[1], im, s);
    std::fprintf(stderr, "[replay] loaded snapshot: %zu bands, %zu users\n",
        s.bands.size(), s.users.size());

    std::ifstream f(argv[2]);
    json events; f >> events;
    std::fprintf(stderr, "[replay] replaying %zu events\n", events.size());

    size_t stop_at = argc >= 5 ? std::strtoull(argv[4], nullptr, 10) : events.size();
    for (size_t k = 0; k < events.size() && k < stop_at; ++k) {
        auto& e = events[k];
        std::string kind = e["kind"].get<std::string>();
        s.block_timestamp = u256(e["ts"].get<uint64_t>());
        s.external_price  = U(e["external_price"]);

        try {
            if (kind == "TokenExchange") {
                uint64_t i = e["i"].get<uint64_t>();
                uint64_t j = e["j"].get<uint64_t>();
                u256 sold = U(e["sold"]);
                u256 bought = U(e["bought"]);
                // Snapshot pre-swap state we care about
                auto pre_active = s.active_band;
                auto pre_admx = s.admin_fees_x, pre_admy = s.admin_fees_y;
                apply_token_exchange(im, s, i, j, sold, bought);
                // Post-check: nothing to compare to for in/out unless we
                // instrument apply_token_exchange to return them, but we CAN
                // detect the pathology (no state change vs event says nonzero)
                if (s.active_band == pre_active
                        && s.admin_fees_x == pre_admx
                        && s.admin_fees_y == pre_admy
                        && (sold > 0 || bought > 0)) {
                    static int _n = 0;
                    if (_n < 20) {
                        std::fprintf(stderr,
                            "[divergent-swap #%d] event %zu block=%llu i=%llu j=%llu "
                            "sold=%s bought=%s pre_active=%s\n",
                            ++_n, k, (unsigned long long)e["block"].get<uint64_t>(),
                            (unsigned long long)i, (unsigned long long)j,
                            sold.str().c_str(), bought.str().c_str(),
                            pre_active.str().c_str());
                    }
                }
            } else if (kind == "Deposit") {
                std::string u = e["user"].get<std::string>();
                u256 amt = U(e["amount"]);
                i256 n1 = i256(e["n1"].get<int64_t>());
                i256 n2 = i256(e["n2"].get<int64_t>());
                apply_deposit(im, s, u, amt, n1, n2);
            } else if (kind == "Withdraw") {
                // Controller.repay/self_liquidate/liquidate_extended all invoke
                // AMM.withdraw(user, frac) where `frac` is:
                //   - 10**18 (full) for repay + repay_extended + self-repay paths
                //     (Controller then re-deposits via a Deposit event if debt remains)
                //   - _get_f_remove(f, h_limit) for hard liquidations (< 10**18)
                // We distinguish by checking whether the on-chain amounts match
                // what our apply_withdraw(frac=1e18) would produce. If yes,
                // use frac=1e18. Otherwise (hard-liq or partial), fall back to
                // an analytical frac derived from the event totals.
                std::string u = e["user"].get<std::string>();
                u256 amt_borrowed   = U(e["amount_borrowed"]);
                u256 amt_collateral = U(e["amount_collateral"]);

                LlammaState scopy = s;
                auto full = apply_withdraw(im, scopy, u, ONE_1E18());
                if (full.first == amt_borrowed && full.second == amt_collateral) {
                    apply_withdraw(im, s, u, ONE_1E18());
                } else {
                    // Partial (hard-liq case). Bisect frac in [0, 1e18] to find
                    // the value that matches BOTH amounts (Vyper's exact
                    // Controller-supplied frac). If no single frac matches both,
                    // fall back to analytical frac from amt_collateral only.
                    auto try_frac = [&](u256 frac) -> std::pair<u256, u256> {
                        LlammaState c = s;
                        return apply_withdraw(im, c, u, frac);
                    };
                    u256 lo = 0, hi = ONE_1E18();
                    u256 best_frac = 0;
                    bool matched_both = false;
                    for (int it = 0; it < 62 && lo <= hi; ++it) {
                        u256 mid = (lo + hi) / 2;
                        auto r = try_frac(mid);
                        if (r.first == amt_borrowed && r.second == amt_collateral) {
                            best_frac = mid;
                            matched_both = true;
                            break;
                        }
                        // amount_collateral is monotonic non-decreasing in frac
                        if (r.second < amt_collateral) {
                            lo = mid + 1;
                        } else if (r.second > amt_collateral) {
                            if (mid == 0) break;
                            hi = mid - 1;
                        } else {
                            // collateral matches; check which side of Δborrowed
                            if (r.first < amt_borrowed) lo = mid + 1;
                            else if (r.first > amt_borrowed) { if (mid == 0) break; hi = mid - 1; }
                            else break; // impossible — would have matched both
                        }
                    }
                    if (!matched_both) {
                        // Fallback: analytical frac (keeps compound drift bounded)
                        auto uit = s.users.find(u);
                        best_frac = ONE_1E18();
                        if (uit != s.users.end()) {
                            static const u256 DEAD = 1000;
                            u256 total_y = 0, total_x = 0;
                            for (auto& shv : uit->second.shares) {
                                auto bit = s.bands.find(shv.first);
                                if (bit == s.bands.end()) continue;
                                u256 tsh = bit->second.shares + DEAD;
                                if (tsh == 0) continue;
                                total_y += (bit->second.y + 1) * shv.second / tsh;
                                total_x += (bit->second.x + 1) * shv.second / tsh;
                            }
                            if      (total_y > 0) best_frac = amt_collateral * ONE_1E18() / total_y;
                            else if (total_x > 0) best_frac = amt_borrowed    * ONE_1E18() / total_x;
                            if (best_frac > ONE_1E18()) best_frac = ONE_1E18();
                        }
                    }
                    apply_withdraw(im, s, u, best_frac);
                }
                (void)amt_borrowed;
            } else if (kind == "SetRate") {
                s.rate     = U(e["rate"]);
                s.rate_mul = U(e["rate_mul"]);
                s.rate_time = U(e["time"]);
            } else if (kind == "SetFee") {
                s.fee = U(e["fee"]);
            } else if (kind == "SetAdminFee") {
                s.admin_fee = U(e["admin_fee"]);
            } else if (kind == "UserState") {
                std::string u = e["user"].get<std::string>();
                u256 collat = U(e["collateral"]);
                u256 debt   = U(e["debt"]);
                i256 n1 = e.contains("n1") ? i256(e["n1"].get<int64_t>()) : i256(0);
                i256 n2 = e.contains("n2") ? i256(e["n2"].get<int64_t>()) : i256(0);
                // A fully-closed loan (Repay-to-zero or Liquidate) fires
                // UserState with all-zero fields. Match Controller.n_loans()
                // by removing the user entry entirely — otherwise the map
                // accumulates every user who ever borrowed.
                if (collat == 0 && debt == 0 && n1 == 0 && n2 == 0) {
                    s.users.erase(u);
                } else {
                    UserTicks& ut = s.users[u];
                    ut.collateral = collat;
                    ut.debt       = debt;
                    ut.N          = u256(n2 - n1 + 1);
                }
            }
        } catch (std::exception& ex) {
            std::fprintf(stderr, "[replay] event block=%llu kind=%s error: %s\n",
                (unsigned long long)e["block"].get<uint64_t>(), kind.c_str(), ex.what());
        }
    }

    dump_state(s, argv[3]);
    std::fprintf(stderr, "[replay] wrote %s\n", argv[3]);
    return 0;
}
