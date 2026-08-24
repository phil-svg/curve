// sweep_precompute.cpp — for a discount % and a block range, replay LLAMMA
// events per block and emit per-block candidate lists.
//
// Output JSON:
//   [ {"block": N, "candidates": [{"user":"0x…","x":"…","y":"…","debt":"…"}, …]}, … ]
//
// Usage:
//   ./build/sweep_precompute <snapshot.json> <events.json> <from_block> <to_block>
//                            <discount_pct> <out_candidates.json>
//
// discount_pct: e.g. 12 for 12%.

#include "llama_amm.hpp"
#include <nlohmann/json.hpp>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>

using json = nlohmann::json;

static u256 U(const json& j) { return to_u256(j.get<std::string>()); }
static i256 I(const json& j) { return to_i256(j.get<std::string>()); }

static void load_snapshot(const std::string& path, LlammaImmutables& im, LlammaState& s) {
    std::ifstream f(path); json j; f >> j;
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
        bs.x = U(it.value()["x"]); bs.y = U(it.value()["y"]); bs.shares = U(it.value()["shares"]);
        s.bands[b] = bs;
    }
    for (auto it = j["users"].begin(); it != j["users"].end(); ++it) {
        UserTicks ut;
        ut.ns0 = i256(it.value()["ns0"].get<std::string>());
        ut.ns1 = i256(it.value()["ns1"].get<std::string>());
        if (it.value().contains("shares")) {
            for (auto sit = it.value()["shares"].begin(); sit != it.value()["shares"].end(); ++sit)
                ut.shares[std::stoll(sit.key())] = U(sit.value());
        }
        if (it.value().contains("collateral")) ut.collateral = U(it.value()["collateral"]);
        if (it.value().contains("stablecoin")) ut.stablecoin = U(it.value()["stablecoin"]);
        if (it.value().contains("debt"))       ut.debt       = U(it.value()["debt"]);
        if (it.value().contains("N"))          ut.N          = U(it.value()["N"]);
        s.users[it.key()] = ut;
    }
}

int main(int argc, char** argv) {
    if (argc < 7) {
        std::fprintf(stderr, "usage: %s <snap.json> <events.json> <from_block> <to_block> <discount_pct> <out.json> [<oracle_file> [<target_spot_file>]]\n", argv[0]);
        std::fprintf(stderr, "  oracle_file: optional JSON {\"<block>\": \"<price_1e18>\"} overrides p_oracle used for health\n");
        std::fprintf(stderr, "  target_spot_file: optional JSON {\"<block>\": \"<price_1e18>\"}.\n");
        std::fprintf(stderr, "    When present: real TokenExchange events in-window are IGNORED and replaced\n");
        std::fprintf(stderr, "    each block by an arb trade that brings AMM marginal price ≈ target. Use for\n");
        std::fprintf(stderr, "    counterfactual sims where soft-liq must respond to the imposed schedule\n");
        std::fprintf(stderr, "    instead of playing back real on-chain arb activity.\n");
        return 1;
    }
    LlammaImmutables im; LlammaState s;
    load_snapshot(argv[1], im, s);
    std::fprintf(stderr, "[precompute] snapshot: %zu bands, %zu users\n", s.bands.size(), s.users.size());

    std::ifstream ef(argv[2]); json events; ef >> events;
    uint64_t from_block = std::strtoull(argv[3], nullptr, 10);
    uint64_t to_block   = std::strtoull(argv[4], nullptr, 10);
    double disc = std::strtod(argv[5], nullptr);

    // Optional per-block oracle overrides (chainlink-mode). Missing blocks =
    // fall back to internal AMM p_oracle_ro.
    std::unordered_map<uint64_t, u256> oracle_override;
    if (argc >= 8) {
        std::ifstream of(argv[7]); json j; of >> j;
        for (auto it = j.begin(); it != j.end(); ++it)
            oracle_override[std::stoull(it.key())] = U(it.value());
        std::fprintf(stderr, "[precompute] oracle-override: %zu blocks loaded from %s\n",
                     oracle_override.size(), argv[7]);
    }

    // Optional per-block target spot for the counterfactual arb sim.
    std::unordered_map<uint64_t, u256> target_spot;
    bool auto_arb = false;
    if (argc >= 9) {
        std::ifstream of(argv[8]); json j; of >> j;
        for (auto it = j.begin(); it != j.end(); ++it)
            target_spot[std::stoull(it.key())] = U(it.value());
        auto_arb = true;
        std::fprintf(stderr, "[precompute] auto-arb mode: %zu target-spot blocks loaded from %s\n",
                     target_spot.size(), argv[8]);
        std::fprintf(stderr, "[precompute]   → real TokenExchange events in-window will be SKIPPED\n");
    }

    // Optional per-block arb-trade log — records (block, i, j, dx, dy, p_before, p_after).
    // Emitted only when auto_arb is on. Used by the arb-bot analyzer.
    json arb_log = json::array();
    const char* arb_log_path = nullptr;
    if (argc >= 10) arb_log_path = argv[9];
    u256 liquidation_discount = u256((uint64_t)(disc * 1e16));
    // Actually for exact match we need explicit precision — multiply as int
    // (e.g. 12 → 12e16). Redo:
    liquidation_discount = 0;
    {
        // reconstruct = round(disc_pct * 1e16)
        double scaled = disc * 1e16;
        uint64_t as64 = (uint64_t)(scaled + 0.5);
        liquidation_discount = u256(as64);
    }

    // Group events by block for efficient stepping
    std::unordered_map<uint64_t, std::vector<size_t>> by_block;
    for (size_t k = 0; k < events.size(); ++k)
        by_block[events[k]["block"].get<uint64_t>()].push_back(k);

    // Prepare output
    json out = json::array();
    long long total_candidates = 0;

    for (uint64_t b = from_block; b <= to_block; ++b) {
        // Apply any events at exactly this block (in log_index order — events
        // are already sorted by (block, log_index) from fetch_events.py).
        auto it = by_block.find(b);
        if (it != by_block.end()) {
            for (size_t k : it->second) {
                auto& e = events[k];
                s.block_timestamp = u256(e["ts"].get<uint64_t>());
                s.external_price  = U(e["external_price"]);
                std::string kind = e["kind"].get<std::string>();
                try {
                    if (kind == "BlockTick") {
                        // Synthetic per-block tick — external_price + timestamp
                        // are already updated above; nothing else to do.
                    } else if (kind == "TokenExchange") {
                        if (auto_arb) {
                            // Auto-arb mode: drop real TokenExchange events.
                            // The per-block arb-to-target call after this loop
                            // synthesizes the trade instead.
                        } else {
                            apply_token_exchange(im, s,
                                e["i"].get<uint64_t>(), e["j"].get<uint64_t>(),
                                U(e["sold"]), U(e["bought"]));
                        }
                    } else if (kind == "Deposit") {
                        apply_deposit(im, s, e["user"].get<std::string>(), U(e["amount"]),
                            i256(e["n1"].get<int64_t>()), i256(e["n2"].get<int64_t>()));
                    } else if (kind == "Withdraw") {
                        // Same "try full first, else bisect for exact match" as replay.cpp
                        std::string uu = e["user"].get<std::string>();
                        u256 amt_borrowed  = U(e["amount_borrowed"]);
                        u256 amt_collateral = U(e["amount_collateral"]);
                        LlammaState scopy = s;
                        auto full = apply_withdraw(im, scopy, uu, ONE_1E18());
                        if (full.first == amt_borrowed && full.second == amt_collateral) {
                            apply_withdraw(im, s, uu, ONE_1E18());
                        } else {
                            auto try_frac = [&](u256 frac) -> std::pair<u256, u256> {
                                LlammaState c = s;
                                return apply_withdraw(im, c, uu, frac);
                            };
                            u256 lo = 0, hi = ONE_1E18();
                            u256 best_frac = 0; bool matched_both = false;
                            for (int it = 0; it < 62 && lo <= hi; ++it) {
                                u256 mid = (lo + hi) / 2;
                                auto r = try_frac(mid);
                                if (r.first == amt_borrowed && r.second == amt_collateral) {
                                    best_frac = mid; matched_both = true; break;
                                }
                                if (r.second < amt_collateral) lo = mid + 1;
                                else if (r.second > amt_collateral) { if (mid == 0) break; hi = mid - 1; }
                                else { if (r.first < amt_borrowed) lo = mid + 1;
                                       else if (r.first > amt_borrowed) { if (mid == 0) break; hi = mid - 1; }
                                       else break; }
                            }
                            if (!matched_both) {
                                auto uit = s.users.find(uu);
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
                            apply_withdraw(im, s, uu, best_frac);
                        }
                    } else if (kind == "SetRate") {
                        s.rate = U(e["rate"]); s.rate_mul = U(e["rate_mul"]); s.rate_time = U(e["time"]);
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
                    std::fprintf(stderr, "[precompute] block %llu %s err: %s\n",
                        (unsigned long long)b, kind.c_str(), ex.what());
                }
            }
        }

        // Auto-arb: synthesize a soft-liq trade this block to bring the AMM's
        // marginal price ≈ target_spot[b]. Runs after other events so oracle
        // and rate updates already applied. Skipped for blocks not in the map.
        if (auto_arb) {
            auto tit = target_spot.find(b);
            if (tit != target_spot.end()) {
                // Set external_price from the oracle-file when provided (that's
                // what the AMM's own price_oracle contract would return under
                // this scenario). If no oracle-file, keep whatever BlockTick
                // already set (the real on-chain oracle). Arb then equilibrates
                // the AMM's marginal price to the target_spot (market price) —
                // which can differ from external_price, that's the WHOLE POINT
                // of soft-liq: oracle stays smoothed while market moves.
                auto oit = oracle_override.find(b);
                if (oit != oracle_override.end()) {
                    s.external_price = oit->second;
                }
                u256 p_before = get_p(im, s);
                SynthTrade tr;
                try {
                    tr = arb_to_target_price(im, s, tit->second);
                } catch (std::exception& ex) {
                    std::fprintf(stderr, "[precompute] block %llu arb err: %s\n",
                        (unsigned long long)b, ex.what());
                }
                u256 p_after = get_p(im, s);
                if (arb_log_path) {
                    arb_log.push_back({
                        {"block", b},
                        {"i", tr.i}, {"j", tr.j},
                        {"dx", tr.dx.str()}, {"dy", tr.dy.str()},
                        {"p_before_arb", p_before.str()},
                        {"p_after_arb",  p_after.str()},
                        {"target_p",     tit->second.str()},
                    });
                }
            }
        }

        // Compute health for every user; collect candidates.
        // The x/y values we emit MUST reflect the current LLAMMA state — i.e.
        // the user's post-soft-liquidation position — not their Controller
        // snapshot amounts. get_sum_xy walks the user's bands and multiplies
        // share fractions by bands_x/y, which have been mutated in place by
        // apply_token_exchange as the active_band moved through the user's range.
        json cand_arr = json::array();
        // Pick this-block override once (avoid map lookup per user).
        const u256* p_override = nullptr;
        u256 p_ov_val;
        if (!oracle_override.empty()) {
            auto oit = oracle_override.find(b);
            if (oit != oracle_override.end()) { p_ov_val = oit->second; p_override = &p_ov_val; }
        }
        // Whole-book composition totals — every user with debt, healthy or not.
        // Candidates only cover flagged users, so a composition chart built from
        // them would have gaps in the blocks where the borrower is healthy.
        u256 book_x = 0, book_y = 0;
        for (auto& kv : s.users) {
            const UserTicks& ut = kv.second;
            if (ut.debt == 0) continue;
            try {
                auto [sumX, sumY] = get_sum_xy(im, s, kv.first);
                book_x = book_x + sumX;
                book_y = book_y + sumY;
                i256 h = compute_health(im, s, kv.first, liquidation_discount, true, p_override);
                if (h <= 0) {
                    cand_arr.push_back({
                        {"user", kv.first},
                        {"x",    sumX.str()},   // live soft-liquidated stablecoin balance
                        {"y",    sumY.str()},   // live remaining collateral
                        {"debt", ut.debt.str()},
                    });
                }
            } catch (std::exception&) {}
        }
        total_candidates += cand_arr.size();
        // Per-band liquidity snapshot (non-empty bands only) for the UI's
        // 3D bands view: [n, x, y, p_oracle_up(n)] per band, post-arb state.
        json bands_arr = json::array();
        {
            std::vector<int64_t> ns;
            for (auto& kv : s.bands)
                if (kv.second.x != 0 || kv.second.y != 0) ns.push_back(kv.first);
            std::sort(ns.begin(), ns.end());
            for (int64_t n : ns) {
                const BandState& bs = s.bands.at(n);
                bands_arr.push_back({n, bs.x.str(), bs.y.str(),
                                     p_oracle_up(im, s, i256(n)).str()});
            }
        }
        out.push_back({{"block", b}, {"candidates", cand_arr},
                       {"book_x", book_x.str()}, {"book_y", book_y.str()},
                       {"bands", bands_arr}});
    }

    std::ofstream of(argv[6]); of << out.dump();
    std::fprintf(stderr, "[precompute] wrote %s  total_candidates=%lld across %llu blocks\n",
        argv[6], total_candidates, (unsigned long long)(to_block - from_block + 1));
    if (arb_log_path) {
        std::ofstream al(arb_log_path); al << arb_log.dump();
        std::fprintf(stderr, "[precompute] wrote arb-log %s  (%zu trades)\n",
            arb_log_path, arb_log.size());
    }
    return 0;
}
