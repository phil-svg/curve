// routed_engine.cpp — C++ port of the case study's routed_sim.py: the venue-
// routed bad-debt sim at arbitrary step count (one step per 12 s over 7 days =
// 50,400 steps, which Python cannot do interactively).
//
// Semantics are a line-for-line port:
//   per step: 1) external arb bot walks the venue to the schedule spot
//             2) oracle = EMA (half-life ma_time) of the venue's marginal
//             3) soft-liq arb LLAMMA <-> venue, ternary on round-trip pnl
//                including venue slippage and gas
//             4) profit-tested partial hard liquidation (real withdraw)
//             4b) bot re-pegs the venue after liquidation dumps
//             5) bad debt = debt_left - x - venue_spot * y, floored at 0
//
// Gas: FIXED base fee (default 100 gwei) and a fixed ETH price — no per-block
// history, which is what freed the engine from the 110 real blocks.
//
// Output rows are decimated to ~--chart-rows buckets: flow fields (hard-liq,
// ext-arb $) are SUMS over the bucket, state fields are sampled at the bucket's
// last step. --chart-rows 0 emits every step (parity testing).
#include "llama_amm.hpp"
#include "venue.hpp"
#include <nlohmann/json.hpp>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <vector>

using json = nlohmann::json;
using vint = venue::cpp_int;

static u256 U(const json& j) { return to_u256(j.get<std::string>()); }
static i256 I(const json& j) { return to_i256(j.get<std::string>()); }

static const vint ONE_18 = venue::E18;

// ---------------------------------------------------------------- profiling
// Per-substep wall time + call counts. Timer reads cost ~20 ns against
// substeps measuring microseconds, so the instrumentation cannot distort the
// picture it is drawing. Emitted per output bucket (--timing-out) so the cost
// can be read as a time series, not just a total.
namespace prof {
using clk = std::chrono::steady_clock;
struct Acc {
    double ext_arb1 = 0, oracle = 0, softliq = 0, hardliq = 0, ext_arb2 = 0, emit = 0;
    // work counters — what the time is actually made of
    long venue_quotes = 0;    // get_dy/exec: newton_D / newton_y solves
    long venue_execs = 0;
    long venue_clones = 0;
    long book_copies = 0;     // LlammaState copy: the arb's hypothetical trades
    long book_trades = 0;     // apply_trade_dx: band walks
    long arb_evals = 0;       // pnl_liq/pnl_deliq evaluations (ternary probes)
    long spot_probes = 0;     // venue_spot marginal quotes
    long arb_rounds = 0;      // rounds that got past the pruning guard
    long ternaries = 0;       // ternary searches launched
    long ternary_iters = 0;   // total bisection iterations inside them
    long steps_searched = 0;  // steps where >=1 ternary ran
    long ceiling_tests = 0;   // cheap profitability ceilings evaluated
    long ceiling_pruned = 0;  // ... that proved no size can clear gas
    void operator+=(const Acc& o) {
        ext_arb1 += o.ext_arb1; oracle += o.oracle; softliq += o.softliq;
        hardliq += o.hardliq; ext_arb2 += o.ext_arb2; emit += o.emit;
        venue_quotes += o.venue_quotes; venue_execs += o.venue_execs;
        venue_clones += o.venue_clones; book_copies += o.book_copies;
        book_trades += o.book_trades; arb_evals += o.arb_evals;
        spot_probes += o.spot_probes; arb_rounds += o.arb_rounds;
        ternaries += o.ternaries; ternary_iters += o.ternary_iters;
        steps_searched += o.steps_searched;
        ceiling_tests += o.ceiling_tests; ceiling_pruned += o.ceiling_pruned;
    }
};
static Acc cur, total;                       // cur = this bucket, total = run
struct T {                                   // scoped timer
    clk::time_point t0; double* slot;
    explicit T(double* s) : t0(clk::now()), slot(s) {}
    ~T() { *slot += std::chrono::duration<double>(clk::now() - t0).count(); }
};
} // namespace prof

// ---------------------------------------------------------------- bridging
static vint to_cpp(const u256& v) { return v.convert_to<vint>(); }
static u256 to_u(const vint& v) { return v.convert_to<u256>(); }
static double to_d(const vint& v) { return v.convert_to<double>(); }
static vint cpp_from_double(double v) { return vint(v); } // truncates

// ---------------------------------------------------------------- snapshot
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

// ---------------------------------------------------------------- book helpers
static vint book_sum_x(const LlammaState& s) {
    vint t = 0;
    for (auto& kv : s.bands) t += to_cpp(kv.second.x);
    return t;
}
static vint book_sum_y(const LlammaState& s) {
    vint t = 0;
    for (auto& kv : s.bands) t += to_cpp(kv.second.y);
    return t;
}

// apply_trade_dx returning the out amount (band-total delta, like the Python
// Book's return value).
static vint trade_dx_out(const LlammaImmutables& im, LlammaState& s,
                            int i, int j, const vint& dx) {
    ++prof::cur.book_trades;
    vint before = (i == 0) ? book_sum_y(s) : book_sum_x(s);
    apply_trade_dx(im, s, i, j, to_u(dx));
    vint after = (i == 0) ? book_sum_y(s) : book_sum_x(s);
    return before - after;
}

// Quote-only band walk: `calc_swap_out` is const on the state and reads only
// bands/active_band/fee — none of the oracle fields tick_oracle mutates — so a
// hypothetical trade needs no book copy at all. This is the same math
// apply_trade_dx runs internally; `po` is hoisted out of the search loop
// because it is identical for every probe at a fixed book + timestamp.
static vint quote_trade_out(const LlammaImmutables& im, const LlammaState& s,
                            const LimitedP& po, int i, int j, const vint& dx) {
    if (dx <= 0) return 0;
    bool pump = (i == 0 && j == 1);
    u256 in_prec = pump ? im.BORROWED_PRECISION : im.COLLATERAL_PRECISION;
    u256 out_prec = pump ? im.COLLATERAL_PRECISION : im.BORROWED_PRECISION;
    DetailedTrade tr = calc_swap_out(im, s, pump, to_u(dx) * in_prec,
                                     po.p, po.dfee, in_prec, out_prec);
    u256 in_done = tr.in_amount / in_prec;
    u256 out_done = tr.out_amount / out_prec;
    if (in_done == 0 || out_done == 0) return 0;
    return to_cpp(out_done);
}

// ---------------------------------------------------------------- ext arb bot
struct ExtArb { std::string dir; double usd = 0.0; };

static bool ext_arb_step(venue::Venue& pool, double target_spot, ExtArb& out,
                         double tol = 5e-4, int max_chunks = 64) {
    double total_usd = 0.0;
    std::set<std::string> dirs;
    for (int c = 0; c < max_chunks; ++c) {
        ++prof::cur.spot_probes;
        double cur = venue::venue_spot(pool);
        if (std::fabs(cur - target_spot) <= tol * target_spot) break;
        bool sell = cur > target_spot;    // venue dear -> sell collateral into it
        int i = sell ? 1 : 0, j = sell ? 0 : 1;
        vint chunk = vint(pool.pair_balances()[sell ? 1 : 0] / 20);
        if (chunk <= 0) break;
        auto crossed_after = [&](const vint& dx) -> int {
            // 1 crossed, 0 not crossed, -1 reverted
            try {
                ++prof::cur.venue_clones; ++prof::cur.venue_execs;
                ++prof::cur.spot_probes;
                auto cl = pool.clone();
                cl->exec(i, j, dx);
                return ((venue::venue_spot(*cl) < target_spot) == sell) ? 1 : 0;
            } catch (...) { return -1; }
        };
        int cr = crossed_after(chunk);
        if (cr < 0) {
            chunk /= 8;
            if (chunk <= 0) break;
        } else if (cr == 1) {
            vint lo = 1, hi = chunk;
            for (int it = 0; it < 40; ++it) {
                if (hi - lo <= std::max(vint(chunk / 1000), vint(1))) break;
                vint mid = (lo + hi) / 2;
                int c2 = crossed_after(mid);
                if (c2 != 0) hi = mid; else lo = mid;
            }
            chunk = hi;
        }
        double prev = cur;
        ++prof::cur.venue_execs;
        try { pool.exec(i, j, chunk); } catch (...) { break; }
        total_usd += to_d(chunk) / 1e18 * (sell ? target_spot : 1.0);
        dirs.insert(sell ? "SELL" : "BUY");
        ++prof::cur.spot_probes;
        if (std::fabs(venue::venue_spot(pool) - prev) < 1e-12) break;
    }
    if (total_usd <= 0) return false;
    std::string d;
    for (auto& x : dirs) d += (d.empty() ? "" : "/") + x;
    out.dir = d; out.usd = total_usd;
    return true;
}

// ---------------------------------------------------------------- soft-liq arb
struct ArbTrade { int i, j; vint dx, dy; };

// Cheap EXACT ceiling on what an arb round trip could gross, computed from the
// band map alone — no band walk, no newton solve, O(#bands).
//
// LIQ: the arb buys collateral out of LLAMMA and sells it at the venue. Grant
// it every impossible favour: it pays each band's CURRENT marginal price for
// every token in that band (buying only pushes that price up), sells every
// token at the venue's marginal as if its own selling never moved it, and pays
// no fee on either leg. Real profit is strictly below this. Bands already
// priced above the venue cannot contribute and are excluded.
//
// The band's marginal is get_p_in_band, NOT its static price range: LLAMMA
// reprices a band as the oracle moves (the p_o^3/p_up^2 amplification), and
// that repricing is precisely what makes collateral cheap in a crash. Using
// the static range here silently excluded every profitable band.
//
// If this ceiling does not clear gas, NO trade size is profitable and the
// ternary that would have proved it is provably wasted work. Skipping it
// cannot change any result.
static double max_liq_gross(const LlammaImmutables& im, const LlammaState& s,
                            double p_venue) {
    double g = 0;
    for (const auto& kv : s.bands) {
        if (kv.second.y == 0) continue;
        double p_band = to_d(to_cpp(get_p_in_band(im, s, i256(kv.first),
                                                  kv.second.x, kv.second.y))) / 1e18;
        if (p_band >= p_venue) continue;
        g += to_d(to_cpp(kv.second.y)) / 1e18 * (p_venue - p_band);
    }
    return g;
}

// DELIQ is the mirror: buy collateral at the venue and sell it into bands whose
// marginal sits ABOVE the venue. A band can pay out at most the x it holds, and
// earning x costs at least x*p_venue/p_band, so its profit cannot exceed
// x*(1 - p_venue/p_band).
static double max_deliq_gross(const LlammaImmutables& im, const LlammaState& s,
                              double p_venue) {
    double g = 0;
    for (const auto& kv : s.bands) {
        if (kv.second.x == 0) continue;
        double p_band = to_d(to_cpp(get_p_in_band(im, s, i256(kv.first),
                                                  kv.second.x, kv.second.y))) / 1e18;
        if (p_band <= p_venue || p_band <= 0) continue;
        g += to_d(to_cpp(kv.second.x)) / 1e18 * (1.0 - p_venue / p_band);
    }
    return g;
}

static std::vector<ArbTrade> soft_liq_arb(
    const LlammaImmutables& im, LlammaState& book, venue::Venue& pool,
    double target_spot, double gas_usd, int max_rounds = 4) {
    std::vector<ArbTrade> trades;
    for (int round = 0; round < max_rounds; ++round) {
        vint by = book_sum_y(book);
        vint bx = book_sum_x(book);

        // Pruning guard (exact): the FIRST marginal unit trades at the best
        // price either curve will ever give — LLAMMA's get_p buying up, the
        // venue's marginal selling in — and fees/gas only subtract from that.
        // So a direction whose marginal price gap is <= 0 cannot contain a
        // profitable size anywhere, and its ternary (~50 full evaluations of
        // clone + band-walk + newton) is provably a no-op. This is what makes
        // quiescent steps cheap; it can never change results.
        double p_llamma = to_d(to_cpp(get_p(im, book))) / 1e18;
        ++prof::cur.spot_probes;
        double p_venue = venue::venue_spot(pool);
        LimitedP po;
        { LlammaState probe = book; ++prof::cur.book_copies;
          po = tick_oracle(im, probe); }   // one copy per round, not per probe
        bool try_liq = (by > 0) && (p_venue > p_llamma);
        bool try_deliq = (bx > 0) && (p_llamma > p_venue);
        // Step 1: can ANY size clear gas? (exact ceiling, no solves)
        if (try_liq) {
            ++prof::cur.ceiling_tests;
            if (max_liq_gross(im, book, p_venue) <= gas_usd) {
                try_liq = false; ++prof::cur.ceiling_pruned;
            }
        }
        if (try_deliq) {
            ++prof::cur.ceiling_tests;
            if (max_deliq_gross(im, book, p_venue) <= gas_usd) {
                try_deliq = false; ++prof::cur.ceiling_pruned;
            }
        }
        // Step 2 (only if step 1 says a profit is possible): find the peak.
        if (!try_liq && !try_deliq) break;
        ++prof::cur.arb_rounds;

        auto pnl_liq = [&](const vint& dx) -> double {
            if (dx <= 0) return 0.0;
            ++prof::cur.arb_evals; ++prof::cur.venue_quotes; ++prof::cur.book_trades;
            vint coll_out;
            try { coll_out = quote_trade_out(im, book, po, 0, 1, dx); }
            catch (...) { return -1e30; }
            if (coll_out <= 0) return -1e30;
            vint dz;
            try { dz = pool.get_dy(1, 0, coll_out); }
            catch (...) { return -1e30; }
            return to_d(dz - dx) / 1e18 - gas_usd;
        };
        auto pnl_deliq = [&](const vint& dx) -> double {
            if (dx <= 0) return 0.0;
            ++prof::cur.arb_evals; ++prof::cur.venue_quotes;
            vint coll;
            try { coll = pool.get_dy(0, 1, dx); }
            catch (...) { return -1e30; }
            if (coll <= 0) return -1e30;
            vint dz;
            ++prof::cur.book_trades;
            try { dz = quote_trade_out(im, book, po, 1, 0, coll); }
            catch (...) { return -1e30; }
            if (dz <= 0) return -1e30;
            return to_d(dz - dx) / 1e18 - gas_usd;
        };
        // Golden-section search for the profit-maximising trade size.
        //
        // Same bracket-shrinking idea as ternary search, but the two interior
        // probes sit at the golden ratio, which makes ONE of them land exactly
        // where the next iteration needs a probe. Ternary throws both probes
        // away every round and pays 2 evaluations per shrink; this pays 1, and
        // the bracket also shrinks slightly faster (x0.618 vs x0.667). Same
        // tolerance, ~23 evaluations instead of 47.
        //
        // The reused point is CARRIED (value and its f), never recomputed, so
        // integer truncation cannot make it drift off the previous probe.
        auto golden = [&](auto fn, const vint& hi) -> std::pair<double, vint> {
            static const vint MIN_STEP = venue::pow10_i(15);
            static const vint K = 618034, D = 1000000;   // 1/phi, fixed point
            vint a = 0, b = std::max(hi, vint(1));
            vint tol = std::max(vint(hi / 10000), MIN_STEP);
            ++prof::cur.ternaries;
            vint c = b - (b - a) * K / D;
            vint d = a + (b - a) * K / D;
            double fc = fn(c), fd = fn(d);
            for (int it = 0; it < 64 && (b - a) > tol; ++it) {
                ++prof::cur.ternary_iters;
                if (fc > fd) {          // max lies in [a, d]
                    b = d; d = c; fd = fc;
                    c = b - (b - a) * K / D;
                    fc = fn(c);
                } else {                // max lies in [c, b]
                    a = c; c = d; fc = fd;
                    d = a + (b - a) * K / D;
                    fd = fn(d);
                }
            }
            vint best_dx = (a + b) / 2;
            return {fn(best_dx), best_dx};
        };

        vint hi_liq = (by > 0) ? cpp_from_double(to_d(by) * target_spot) * 2 : vint(0);
        vint hi_deliq = (bx > 0) ? bx * 2 : vint(0);

        bool have = false; double best_p = 0; int best_kind = 0; vint best_dx = 0;
        if (try_liq && hi_liq > 0) {
            auto [p, dx] = golden(pnl_liq, hi_liq);
            if (p > 0) { have = true; best_p = p; best_kind = 0; best_dx = dx; }
        }
        if (try_deliq && hi_deliq > 0) {
            auto [p, dx] = golden(pnl_deliq, hi_deliq);
            if (p > 0 && (!have || p > best_p)) { have = true; best_p = p; best_kind = 1; best_dx = dx; }
        }
        if (!have) break;

        if (best_kind == 0) {
            vint coll_out;
            try { coll_out = trade_dx_out(im, book, 0, 1, best_dx); }
            catch (...) { break; }
            if (coll_out <= 0) break;
            ++prof::cur.venue_execs;
            try { pool.exec(1, 0, coll_out); } catch (...) { break; }
            trades.push_back({0, 1, best_dx, coll_out});
        } else {
            vint coll;
            ++prof::cur.venue_execs;
            try { coll = pool.exec(0, 1, best_dx); } catch (...) { break; }
            if (coll <= 0) break;
            vint dz;
            try { dz = trade_dx_out(im, book, 1, 0, coll); }
            catch (...) { break; }
            if (dz <= 0) break;
            trades.push_back({1, 0, coll, dz});
        }
    }
    return trades;
}

// ---------------------------------------------------------------- hard liq
// Controller._get_f_remove (exact port via synth_bad_debt.get_f_remove).
static vint get_f_remove(const vint& frac, const vint& health_limit) {
    if (frac >= ONE_18) return ONE_18;
    vint f = ((ONE_18 + health_limit / 2) * (ONE_18 - frac)) / (ONE_18 + health_limit);
    return ((f + frac) * frac) / ONE_18;
}

struct HardLiq { double profit; vint frac, f_remove, dy; };

static HardLiq best_partial_liquidation(
    const venue::Venue& pool, double gas_usd,
    const vint& collat, const vint& debt, const vint& soft_x,
    const vint& disc) {
    auto profit_at = [&](const vint& frac) -> HardLiq {
        if (frac <= 0) return {-1.0, frac, 0, 0};
        vint fr = get_f_remove(frac, disc);
        vint y_take = collat * fr / ONE_18;
        vint x_take = soft_x * fr / ONE_18;
        vint d_repay = debt * frac / ONE_18;
        if (y_take <= 0 || d_repay <= 0) return {-1.0, frac, fr, 0};
        vint dy;
        ++prof::cur.venue_quotes;
        try { dy = pool.get_dy(1, 0, y_take); }
        catch (...) { return {-1.0, frac, fr, 0}; }
        double gain = to_d(dy + x_take - d_repay) / 1e18;
        return {gain - gas_usd, frac, fr, dy};
    };
    vint lo = 0, hi = ONE_18;
    for (int it = 0; it < 50; ++it) {
        if (hi - lo < ONE_18 / 10000) break;
        vint m1 = lo + (hi - lo) / 3;
        vint m2 = hi - (hi - lo) / 3;
        if (profit_at(m1).profit < profit_at(m2).profit) lo = m1; else hi = m2;
    }
    HardLiq a = profit_at((lo + hi) / 2);
    HardLiq b = profit_at(ONE_18);          // always also test the full liq
    return (b.profit > a.profit) ? b : a;
}

// ---------------------------------------------------------------- main
int main(int argc, char** argv) {
    std::map<std::string, std::string> A;
    for (int i = 1; i + 1 < argc; i += 2) A[argv[i]] = argv[i + 1];
    auto S_ = [&](const char* k, const char* d = nullptr) -> std::string {
        auto it = A.find(k);
        if (it != A.end()) return it->second;
        if (d) return d;
        std::fprintf(stderr, "missing arg %s\n", k); exit(2);
    };
    auto D_ = [&](const char* k, double d = NAN) -> double {
        auto it = A.find(k);
        if (it != A.end()) return std::stod(it->second);
        if (!std::isnan(d)) return d;
        std::fprintf(stderr, "missing arg %s\n", k); exit(2);
    };

    const std::string snapshot = S_("--snapshot");
    const double discount = D_("--discount");
    const double tvl_usd = D_("--tvl-usd");
    const double A_raw = D_("--A-raw", 67.5);
    const std::string pool_type = S_("--pool-type", "cryptoswap");
    const int n_coins = (int)D_("--n-coins", 2);
    const double ss_A = D_("--ss-A", 500);
    const double cs = D_("--crash-start-spot");
    const double ce = D_("--crash-end-spot", 0);
    const double off_s = D_("--crash-start-offset-s", 0);
    const double dur_s = D_("--crash-duration-s", 1);
    const std::string path_file = S_("--price-path", "");
    const double ma_time = D_("--ma-time-s");
    const double oracle_seed = D_("--oracle-seed", cs);
    // Gas is a FLAT DOLLAR COST per transaction (user decision) — the old
    // gas-units x base-fee x ETH-price model is gone; --arb-gas /
    // --base-fee-gwei / --eth-price are still parsed so old invocations do not
    // break, but they no longer affect anything.
    const double gas_usd_flat = D_("--gas-usd", 10.0);
    // AMM.rate(), wei per second per 1e18. 0 = no accrual (the parity
    // gate and every pre-existing invocation keep their old results).
    const double rate_per_s = D_("--amm-rate-wei", 0.0) / 1e18;
    // --hard-liq 0 disables the hard-liquidation pass. Only the S.L./D.L.
    // parameter study uses it (llamma-simulator has no Controller either, so
    // a soft-liq loss table needs the borrower to survive the whole path);
    // every production run keeps the default 1.
    const bool hard_liq_on = D_("--hard-liq", 1) != 0;
    // --deliq-protect 1: protocol-patch study (self-cure). Band crvUSD from
    // soft liquidation is counted at (1 - liq_discount) inside health but
    // repays debt at par, so while 0 > health > -liq_discount a position can
    // ALWAYS lift itself back to health >= 0 by repaying R = -h*debt/disc out
    // of its own band x — no external funds, no discount, no seizure. The
    // patch semantics modeled here: Controller.liquidate reverts while a
    // position is curable (inside the buffer AND enough band x); anyone may
    // call cure(user) instead for a small tip. The keeper fires once the tip
    // clears gas (R >= --cure-min-usd); below that the position just sits
    // protected. Deep breaches (h <= -disc) or x-poor books are NOT curable
    // and fall through to the classic liquidation path unchanged.
    const bool deliq_protect = D_("--deliq-protect", 0) != 0;
    const double cure_tip = D_("--cure-tip-bps", 10.0) / 1e4;
    const double cure_min_usd = D_("--cure-min-usd", 10000.0);
    // Cure target: restore health to this level, not just to 0. Headroom
    // costs a bigger par-repay (R = D*(ht-h)/(disc+ht)) but survives the next
    // wiggle. If band x can't fund the full target the cure downsizes, as
    // long as it at least restores h >= 0.
    const double cure_target_h = D_("--cure-target-health", 0.0);
    const bool cure_debug = D_("--cure-debug", 0) != 0;
    // --xpar-health 1: liquidation trigger counts band crvUSD at PAR.
    // The liquidation discount pays for SELLING collateral; band x is the
    // debt asset itself, repays 1:1 and needs no execution incentive, so
    // discounting it in the health gate marks solvent de-liquidating
    // positions liquidatable for no economic reason. Trigger becomes
    // h_liq = h + x*disc/debt (equivalently: discount only the y-side).
    // The reported per-row "health" stays the unmodified on-chain formula.
    const bool xpar_health = D_("--xpar-health", 0) != 0;
    // Synthetic-venue fee override in 1e10 units (5bp = 50'000'000). 0 keeps
    // the type default. Used by the S.L./D.L. "their conditions" arb mode to
    // replicate llamma-simulator's flat 5bp external fee.
    const long venue_fee_1e10 = (long)D_("--venue-fee-1e10", 0);
    const double arb_gas = D_("--arb-gas", 850'000);
    const double base_fee = D_("--base-fee-gwei", 100) * 1e9;   // wei (unused)
    const double eth_price = D_("--eth-price", 2000);           // (unused)
    (void)base_fee; (void)eth_price; (void)arb_gas;
    const long n_steps = (long)D_("--steps", 110);
    const double dt = D_("--dt-s", 12);
    // Sim clock start. MUST default to the snapshot's own timestamp: the AMM
    // accrues interest via rate_mul from state.rate_time, so starting the
    // clock at wall-clock "now" against a snapshot anchored months earlier
    // inflates BASE_PRICE and shifts the whole band ladder, handing the arb a
    // free double-digit mispricing at t=0. Set below, after the snapshot loads.
    long t0_epoch = (long)D_("--t0-epoch", 0);
    long chart_rows = (long)D_("--chart-rows", 1100);
    const std::string out_file = S_("--out");
    const std::string oracle_file = S_("--oracle-out", "");
    const std::string arb_file = S_("--arb-log-out", "");
    const std::string timing_file = S_("--timing-out", "");
    // Progress heartbeat for the UI: a run is a blocking subprocess, so the
    // only way the browser can show anything but a spinner is if the engine
    // says how far along it is.
    const std::string prog_file = S_("--progress-out", "");

    auto t_start = std::chrono::steady_clock::now();

    // schedule -------------------------------------------------------------
    std::vector<std::pair<double, double>> path;
    if (!path_file.empty()) {
        std::ifstream f(path_file); json pj; f >> pj;
        for (auto& e : pj) path.push_back({e[0].get<double>(), e[1].get<double>()});
        std::sort(path.begin(), path.end());
    }
    // --price-paths: MANY price paths in one invocation (JSON array of
    // [[t,p],...] arrays). The snapshot and venue state are parsed once;
    // every piece of mutable sim state is rebuilt from scratch per path, so
    // results are bit-identical to N separate single-path invocations. The
    // output file becomes an array of per-path row-arrays.
    std::vector<std::vector<std::pair<double, double>>> paths_list;
    // Per-path oracle seed (warm pre-window EMA, llamma-simulator style). An
    // entry may be a bare [[t,p],...] array (seed = --oracle-seed, unchanged
    // behaviour) or {"seed": s, "path": [[t,p],...]}.
    std::vector<double> path_seeds;
    const std::string paths_file = S_("--price-paths", "");
    if (!paths_file.empty()) {
        std::ifstream f(paths_file); json pj; f >> pj;
        for (auto& pl : pj) {
            const json& arr = pl.is_object() ? pl.at("path") : pl;
            double seed = pl.is_object() && pl.contains("seed")
                ? pl["seed"].get<double>() : oracle_seed;
            std::vector<std::pair<double, double>> p1;
            for (auto& e : arr) p1.push_back({e[0].get<double>(), e[1].get<double>()});
            std::sort(p1.begin(), p1.end());
            paths_list.push_back(std::move(p1));
            path_seeds.push_back(seed);
        }
    }
    const bool batch = !paths_list.empty();
    if (!batch) {                              // single mode: exactly one entry
        paths_list.push_back(path);
        path_seeds.push_back(oracle_seed);
    }
    auto schedule = [&](double elapsed) -> double {
        if (!path.empty()) {
            if (elapsed <= path.front().first) return path.front().second;
            if (elapsed >= path.back().first) return path.back().second;
            size_t lo = 0;
            // binary search for the segment (paths can be 10k+ points)
            size_t a = 0, b = path.size() - 1;
            while (b - a > 1) { size_t m = (a + b) / 2;
                if (path[m].first >= elapsed) b = m; else a = m; }
            lo = a;
            auto [tA, pA] = path[lo]; auto [tB, pB] = path[lo + 1];
            return (tB == tA) ? pA : pA + (pB - pA) * (elapsed - tA) / (tB - tA);
        }
        if (elapsed <= off_s) return cs;
        if (elapsed >= off_s + dur_s) return ce;
        return cs - (cs - ce) * (elapsed - off_s) / dur_s;
    };

    // venue ------------------------------------------------------------------
    // --venue-state: the real pool's live balances/rates/fees, resolved by
    // venues.venue_from_state in Python so both engines seed identically. The
    // balanced reconstruction from pair TVL stays the fallback. The file is
    // parsed ONCE; the pool object is rebuilt fresh for every path.
    const std::string vstate_file = S_("--venue-state", "");
    json vs; bool have_vs = false;
    if (!vstate_file.empty()) {
        std::ifstream f(vstate_file);
        if (f) { f >> vs; have_vs = true; }
    }
    auto build_pool = [&]() {
        std::unique_ptr<venue::Venue> pool;
        bool seeded = false;
        if (have_vs) {
            auto V = [&](const char* k) {
                return venue::cpp_int(vs[k].get<std::string>()); };
            auto VEC = [&](const char* k) {
                std::vector<venue::cpp_int> o;
                for (auto& e : vs[k]) o.push_back(venue::cpp_int(e.get<std::string>()));
                return o; };
            const std::string vk = vs.value("venue_kind", "stableswap");
            if (vk == "crypto2") {
                auto v = std::make_unique<venue::Crypto2StateVenue>();
                v->balances = VEC("balances");
                v->price_scale = V("price_scale");
                v->ANN = V("A"); v->gamma = V("gamma");
                v->mid_fee = V("mid_fee"); v->out_fee = V("out_fee");
                v->fee_gamma = V("fee_gamma");
                v->math = vs["math"].get<std::string>();
                v->D = V("D");
                pool = std::move(v);
            } else if (vk == "tri3") {
                auto v = std::make_unique<venue::Tri3StateVenue>();
                v->balances = VEC("balances");
                v->price_scale = VEC("price_scale");
                v->i_q = vs["i_q"].get<int>(); v->i_b = vs["i_b"].get<int>();
                v->q_usd = V("q_usd");
                v->ANN = V("A"); v->gamma = V("gamma");
                v->mid_fee = V("mid_fee"); v->out_fee = V("out_fee");
                v->fee_gamma = V("fee_gamma");
                v->D = V("D");
                pool = std::move(v);
            } else {
                pool = venue::stableswap_from_state(
                    vs["ng"].get<bool>(), V("amp"), V("fee"), V("offpeg"),
                    V("bal0"), V("bal1"), V("rate1"));
            }
            seeded = true;
        }
        if (!pool) {
            double A_ui = (pool_type.rfind("stableswap", 0) == 0) ? ss_A : A_raw;
            pool = venue::make_venue(pool_type, n_coins,
                                     cpp_from_double(tvl_usd * 1e18),
                                     cpp_from_double(cs * 1e18), A_ui,
                                     venue_fee_1e10);
        }
        return std::make_pair(std::move(pool), seeded);
    };

    // book -------------------------------------------------------------------
    // Parsed once; copied fresh per path (LlammaState is a value type — the
    // engine already copies it constantly inside the arb search).
    LlammaImmutables im0; LlammaState book0;
    load_snapshot(snapshot, im0, book0);
    if (t0_epoch <= 0) t0_epoch = book0.block_timestamp.convert_to<long>();

    json all_out = json::array();
    long n_hard_grand = 0, n_trades_grand = 0, n_cure_grand = 0;
    double cure_usd_grand = 0;

    // ---- per-path simulation: EVERYTHING mutable is (re)built inside ------
    for (size_t path_i = 0; path_i < paths_list.size(); ++path_i) {
    path = paths_list[path_i];

    auto [pool, seeded] = build_pool();
    double spot_seed = venue::venue_spot(*pool);
    venue::push_pool_to_spot(*pool, cs);
    if (seeded && path_i == 0)
        std::fprintf(stderr, "[routed-cpp] venue seeded from real state: "
                     "spot %.8f vs schedule %.8f (%.3f%%)\n",
                     spot_seed, cs, 100.0 * (spot_seed / cs - 1.0));

    LlammaImmutables im = im0; LlammaState book = book0;
    std::string user = book.users.begin()->first;
    vint debt0 = to_cpp(book.users[user].debt);
    vint debt_left = debt0;
    vint disc_wei = cpp_from_double(discount / 100.0 * 1e18);
    double y0_tokens = to_d(book_sum_y(book)) / 1e18;
    double coll_scale = 1.0;

    // loop -------------------------------------------------------------------
    if (chart_rows <= 0) chart_rows = n_steps;
    long stride = (n_steps + chart_rows - 1) / chart_rows;
    if (stride < 1) stride = 1;

    double ema = path_seeds[path_i];
    bool have_prev_ts = false;
    long prev_ts = 0;
    bool settled = false;
    long n_hard_total = 0, n_trades_total = 0;
    long n_cure_total = 0; double cure_usd_path = 0;

    json rows = json::array(), arb_log = json::array(), oracle_out = json::object();
    json timing = json::array();
    double agg_hard_usd = 0, agg_hard_profit = 0, agg_ext_usd = 0;
    double agg_hard_coll = 0, agg_hard_tok = 0, agg_hard_tok_usd = 0;
    double last_h = -1e9, h_ema_at_check = 1e18;   // health cache (substep 4)
    long last_h_step = -1'000'000;
    long agg_n_hard = 0;
    double agg_cure_usd = 0; long agg_n_cure = 0;
    std::string agg_ext_dir;

    for (long i = 0; i < n_steps; ++i) {
        if (!prog_file.empty() && (i % 256 == 0 || i == n_steps - 1)) {
            if (FILE* pf = std::fopen(prog_file.c_str(), "w")) {
                std::fprintf(pf, "{\"done\":%ld,\"total\":%ld}", i + 1, n_steps);
                std::fclose(pf);
            }
        }
        long ts = t0_epoch + llround(i * dt);
        double elapsed = ts - t0_epoch;
        double target = schedule(elapsed);
        // Interest. LLAMMA accrues it into the band ladder via rate_mul, so the
        // borrower's debt has to grow by the same factor or the two sides of
        // the loan drift apart over a multi-day horizon. AMM._rate_mul() is
        // linear in elapsed time: rate_mul * (1 + rate * dt).
        const double accr = 1.0 + rate_per_s * elapsed;
        vint debt_now = (accr > 1.0)
            ? vint(debt_left * cpp_from_double(accr * 1e18) / ONE_18) : debt_left;

        // 1. external arb bot
        ExtArb ext; bool ext_any;
        { prof::T _t(&prof::cur.ext_arb1); ext_any = ext_arb_step(*pool, target, ext); }

        // 2. oracle EMA of the venue's marginal
        prof::T* _to = new prof::T(&prof::cur.oracle);
        ++prof::cur.spot_probes;
        double spot_v = venue::venue_spot(*pool);
        if (have_prev_ts && ma_time > 0) {
            double alpha = std::exp(-std::log(2.0) * (ts - prev_ts) / ma_time);
            ema = ema * alpha + spot_v * (1.0 - alpha);
        } else if (ma_time <= 0) {
            ema = spot_v;
        }
        have_prev_ts = true;
        prev_ts = ts;

        book.block_timestamp = u256(ts);
        book.external_price = to_u(cpp_from_double(ema * 1e18));
        // On-chain semantics, always: limit_p_o's ±25%/120s clamp and
        // anti-sandwich fee run faithfully. old_p_o/old_dfee/prev_p_o_time
        // are ONLY written by tick_oracle inside actual trades
        // (apply_trade_dx), the way _price_oracle_w only runs inside
        // exchange txs — an AMM untouched >120s takes the next jump
        // unclamped, exactly like the WFRAX frozen-pool case. (A pre-2026-08
        // "bypass" neutered this each step to match the clampless TS
        // reference; it is gone, so those historical numbers need the old
        // binary to reproduce.)

        delete _to;   // end of substep 2

        // 3. soft-liq arb through the venue
        bool traded_this_step = false;
        double gas_usd = gas_usd_flat;   // flat per round trip
        if (!settled) {
            prof::T _t(&prof::cur.softliq);
            long tern0 = prof::cur.ternaries;
            auto trades = soft_liq_arb(im, book, *pool, target, gas_usd);
            if (prof::cur.ternaries > tern0) ++prof::cur.steps_searched;
            n_trades_total += (long)trades.size();
            traded_this_step = !trades.empty();
            if (!arb_file.empty())
                for (auto& tr : trades)
                    arb_log.push_back({{"block", i}, {"i", tr.i}, {"j", tr.j},
                                       {"dx", tr.dx.str()}, {"dy", tr.dy.str()},
                                       {"p_before_arb", "0"}, {"p_after_arb", "0"},
                                       {"target_p", cpp_from_double(target * 1e18).str()}});
        }

        // 4. hard liquidation
        double hard_usd = 0, hard_profit = 0, hard_coll_usd = 0, hard_coll_tok = 0;
        double hard_tok_usd = 0;   // collateral leg only, at market
        int n_hard = 0;
        double cure_usd_step = 0; int n_cure_step = 0;
        if (hard_liq_on && !settled && debt_left > 0) {
            prof::T _t(&prof::cur.hardliq);
            book.users[user].debt = to_u(debt_now);   // health at accrued debt
            // The profitability search was ALREADY gated on h < 0 — on a run
            // with no liquidations it never executes, and the whole cost is
            // compute_health itself on every step. Health only falls when the
            // oracle drops, the book is traded, or interest accrues, so skip
            // the recompute when none of those can have pushed a comfortably
            // positive health through zero. Rechecked unconditionally near the
            // boundary and every RECHECK_EVERY steps, so drift cannot hide.
            static const double SAFE_H = 0.02;      // 2% clear of liquidation
            static const long RECHECK_EVERY = 32;
            bool must = traded_this_step || (ema <= h_ema_at_check)
                        || (last_h < SAFE_H) || (i - last_h_step >= RECHECK_EVERY);
            i256 h;
            if (must) {
                h = compute_health(im, book, user, to_u(disc_wei), true);
                last_h = h.convert_to<double>() / 1e18;
                h_ema_at_check = ema;
                last_h_step = i;
                ++prof::cur.ceiling_tests;
            } else {
                h = i256(1);                        // known positive, not liquidatable
                ++prof::cur.ceiling_pruned;
            }
            // 4a-pre. x-par liquidation gate (--xpar-health): a position
            // whose par-valued crvUSD closes the health gap is NOT
            // liquidatable; only the discounted y-side counts against it.
            if (xpar_health && h < 0) {
                auto [px_u, py_u] = get_sum_xy(im, book, user);
                (void)py_u;
                double x_par = to_d(to_cpp(px_u)) / 1e18;
                double h_adj = h.convert_to<double>() / 1e18
                               + x_par * (discount / 100.0)
                                 / (to_d(debt_now) / 1e18);
                if (h_adj >= 0) h = i256(1);   // solvent at par: no liq, no cure
            }
            // 4a. self-cure (--deliq-protect): repay from the position's own
            // band crvUSD while still inside the liquidation-discount buffer.
            // R = -h*debt/disc restores health to exactly 0; 2% margin covers
            // rounding. The keeper tip comes out of band x on top of R. While
            // curable but below the keeper's minimum, liquidate() is treated
            // as reverting (liq_blocked), matching the patch semantics.
            bool liq_blocked = false;
            if (deliq_protect && h < 0) {
                double h_d = h.convert_to<double>() / 1e18;
                double f_d = discount / 100.0;
                auto [cx_u, cy_u] = get_sum_xy(im, book, user);
                (void)cy_u;
                double x_d = to_d(to_cpp(cx_u)) / 1e18;
                double D_d = to_d(debt_now) / 1e18;
                // R to reach the target health; R to merely restore 0.
                double R_tgt = D_d * (cure_target_h - h_d) / (f_d + cure_target_h) * 1.02;
                double R_zero = (-h_d) * D_d / f_d * 1.02;
                // Downsize toward what band x can actually fund.
                double R_d = std::min(R_tgt, x_d * 0.999 / (1.0 + cure_tip));
                double tip_d = R_d * cure_tip;
                bool curable = (h_d > -f_d) && (R_d > 0) && (R_d >= R_zero)
                               && (x_d >= R_d + tip_d);
                if (cure_debug && h_d < 0 && !curable)
                    std::fprintf(stderr, "[cure-skip] t=%.2fh h=%.4f%% x=$%.0f "
                                 "R_zero=$%.0f R_tgt=$%.0f\n", elapsed / 3600,
                                 h_d * 100, x_d, R_zero, R_tgt);
                if (curable && R_d >= cure_min_usd) {
                    // withdraw-frac + repay-x + redeposit-y nets out to an
                    // x-only pro-rata removal across the user's bands.
                    double frac = (R_d + tip_d) / x_d;
                    vint keep = cpp_from_double((1.0 - frac) * 1e18);
                    for (auto& kv : book.bands)
                        if (kv.second.x != 0)
                            kv.second.x = to_u(to_cpp(kv.second.x) * keep / ONE_18);
                    vint repay_prin = cpp_from_double(R_d / accr * 1e18);
                    if (repay_prin > debt_left) repay_prin = debt_left;
                    debt_left -= repay_prin;
                    debt_now = (accr > 1.0)
                        ? vint(debt_left * cpp_from_double(accr * 1e18) / ONE_18)
                        : debt_left;
                    book.users[user].debt = to_u(debt_now);
                    cure_usd_step += R_d;
                    ++n_cure_step; ++n_cure_total;
                    if (debt_left <= venue::pow10_i(9)) { settled = true; debt_left = 0; }
                    h = (debt_left > 0)
                        ? compute_health(im, book, user, to_u(disc_wei), true)
                        : i256(1);
                    last_h = h.convert_to<double>() / 1e18;
                    last_h_step = i;
                } else if (curable) {
                    liq_blocked = true;   // liquidate() reverts; cure pending
                }
                // Final gate, matching the patch's revert semantics: even
                // right after a cure that undershot (active-band x is not
                // valued exactly at par), liquidate() keeps reverting as long
                // as the position can still fund a restore-to-0 from band x.
                if (h < 0 && !liq_blocked) {
                    double h2 = h.convert_to<double>() / 1e18;
                    auto [gx_u, gy_u] = get_sum_xy(im, book, user);
                    (void)gy_u;
                    double x2 = to_d(to_cpp(gx_u)) / 1e18;
                    double D2 = to_d(debt_now) / 1e18;
                    double Rz2 = (-h2) * D2 / f_d * 1.02;
                    liq_blocked = (h2 > -f_d)
                                  && (x2 * 0.999 / (1.0 + cure_tip) >= Rz2);
                }
            }
            if (h < 0 && !liq_blocked) {
                auto [x_u, y_u] = get_sum_xy(im, book, user);
                vint x_wei = to_cpp(x_u), y_wei = to_cpp(y_u);
                if (to_d(y_wei) / 1e18 > 10 && to_d(debt_now) / 1e18 > 10) {
                    HardLiq hl = best_partial_liquidation(
                        *pool, gas_usd_flat, y_wei, debt_now, x_wei, disc_wei);
                    if (hl.profit > 0) {
                        auto [dx_rm, dy_rm] = apply_withdraw(im, book, user, to_u(hl.f_remove));
                        hard_coll_usd = to_d(to_cpp(dx_rm)) / 1e18;   // crvUSD leg
                        if (dy_rm > 0) {
                            // Market value of what left the position. The
                            // liquidator's NET profit (hl.profit) is this minus
                            // the debt repaid MINUS their venue slippage and
                            // gas, so reporting profit as "user loss" hides
                            // everything the venue ate.
                            hard_coll_tok = to_d(to_cpp(dy_rm)) / 1e18;
                            hard_tok_usd = hard_coll_tok * venue::venue_spot(*pool);
                            hard_coll_usd += hard_tok_usd;
                            try { pool->exec(1, 0, to_cpp(dy_rm)); } catch (...) {}
                        }
                        // frac is proportional, so the principal shrinks by
                        // the same fraction; the dollars repaid are accrued.
                        vint repay = debt_left * hl.frac / ONE_18;
                        debt_left -= repay;
                        hard_usd = to_d(repay) / 1e18 * accr;
                        hard_profit = hl.profit;
                        n_hard = 1;
                        ++n_hard_total;
                        coll_scale *= 1.0 - to_d(hl.f_remove) / 1e18;
                        if (debt_left <= venue::pow10_i(9)) { settled = true; debt_left = 0; }
                    }
                }
            }
        }

        // 4b. re-peg after the liquidation dumps
        ExtArb ext2;
        prof::T* _t2 = new prof::T(&prof::cur.ext_arb2);
        bool re_peg = ext_arb_step(*pool, target, ext2);
        delete _t2;
        if (re_peg) {
            if (ext_any) { ext.usd += ext2.usd; ext.dir = ext2.dir; }
            else { ext = ext2; ext_any = true; }
        }

        // aggregate flows for the decimated row
        agg_cure_usd += cure_usd_step; agg_n_cure += n_cure_step;
        cure_usd_path += cure_usd_step;
        agg_hard_usd += hard_usd; agg_hard_profit += hard_profit;
        agg_hard_coll += hard_coll_usd; agg_hard_tok += hard_coll_tok;
        agg_hard_tok_usd += hard_tok_usd;
        agg_n_hard += n_hard;
        if (ext_any) { agg_ext_usd += ext.usd; agg_ext_dir = ext.dir; }

        // i == 0 is emitted unconditionally: the decomposition below
        // differences the FIRST row against the last, and without a true
        // t=0 row that baseline was the first bucket boundary instead.
        bool emit = (i == 0) || ((i + 1) % stride == 0) || (i == n_steps - 1);
        if (emit) {
            prof::T _t(&prof::cur.emit);
            ++prof::cur.spot_probes;
            spot_v = venue::venue_spot(*pool);
            // debt_now was computed at the TOP of the step. When the bucket's
            // last step executes a hard liq, the row would otherwise pair the
            // post-liq book (x/y already withdrawn) with pre-liq debt —
            // phantom bad debt equal to the just-repaid slice (seen on the
            // sfrxUSD default run). Recompute from debt_left so every emitted
            // row is one consistent post-step snapshot. Flow fields
            // (hardLiqUsd etc.) are unaffected.
            debt_now = (accr > 1.0)
                ? vint(debt_left * cpp_from_double(accr * 1e18) / ONE_18)
                : debt_left;
            book.users[user].debt = to_u(debt_now);
            auto [x_u, y_u] = get_sum_xy(im, book, user);
            double x_usd = to_d(to_cpp(x_u)) / 1e18;
            double y_tok = to_d(to_cpp(y_u)) / 1e18;
            double bad_debt = std::max(0.0, to_d(debt_now) / 1e18 - x_usd - spot_v * y_tok);
            // Controller.health(user, full=true): the number the protocol
            // itself watches. < 0 is liquidatable. Computed once per emitted
            // row, so it costs nothing next to the per-step hard-liq probe.
            double health = (debt_left > 0)
                ? compute_health(im, book, user, to_u(disc_wei), true)
                      .convert_to<double>() / 1e18
                : 0.0;
            double sl_user_loss = coll_scale * y0_tokens * spot_v - (y_tok * spot_v + x_usd);

            json row_bands = json::array();
            for (auto& kv : book.bands) {
                if (kv.second.x == 0 && kv.second.y == 0) continue;
                double bx = to_d(to_cpp(kv.second.x)) / 1e18;
                double by = to_d(to_cpp(kv.second.y)) / 1e18;
                double pu = to_d(to_cpp(p_oracle_up(im, book, i256(kv.first)))) / 1e18;
                row_bands.push_back({kv.first,
                                     std::round(bx * 100) / 100,
                                     std::round(by * 10000) / 10000,
                                     std::round(pu * 1e6) / 1e6});
            }

            char datebuf[8] = "";
            {
                time_t tt = (time_t)ts;
                struct tm g; gmtime_r(&tt, &g);
                std::snprintf(datebuf, sizeof datebuf, "%02d:%02d", g.tm_hour, g.tm_min);
            }
            rows.push_back({
                {"blockNumber", i},
                {"LiquidationDiscount", discount},
                {"timestamp", ts},
                {"date", datebuf},
                {"elapsed_s", (long)elapsed},
                {"badDebt", llround(bad_debt)},
                {"health", std::round(health * 1e6) / 1e6},
                {"comp_lend_usd", llround(x_usd)},
                {"comp_coll_tokens", std::round(y_tok * 100) / 100},
                {"comp_coll_usd", llround(y_tok * spot_v)},
                {"hardLiqUsd", llround(agg_hard_usd)},
                {"hardLiqProfit", llround(agg_hard_profit)},
                {"hardLiqCollUsd", llround(agg_hard_coll)},
                {"hardLiqCollTok", std::round(agg_hard_tok * 100) / 100},
                {"hardLiqTokUsd", llround(agg_hard_tok_usd)},
                {"slUserLoss", llround(sl_user_loss)},
                {"debtFrac", debt0 > 0 ? to_d(debt_left) / to_d(debt0) : 1.0},
                {"debtUsd", llround(to_d(debt_now) / 1e18)},
                {"accrual", std::round(accr * 1e9) / 1e9},
                {"bands", row_bands},
                {"real_curve_spot", std::round(spot_v * 1e6) / 1e6},
                {"target_spot", std::round(target * 1e6) / 1e6},
                {"pool_crv_spot", std::round(spot_v * 1e6) / 1e6},
                {"pool_crvusd_bal", std::round(to_d(pool->pair_balances()[0]) / 1e18)},
                {"pool_crv_bal", std::round(to_d(pool->pair_balances()[1]) / 1e18)},
                {"profitable_this_block", agg_n_hard},
                {"settled_total", settled ? 1 : 0},
                {"extArbUsd", llround(agg_ext_usd)},
                {"extArbDir", agg_ext_dir.empty() ? json(nullptr) : json(agg_ext_dir)},
            });
            if (deliq_protect) {
                rows.back()["cureUsd"] = llround(agg_cure_usd);
                rows.back()["cureN"] = agg_n_cure;
            }
            oracle_out[std::to_string(i)] = to_u(cpp_from_double(ema * 1e18)).str();
            if (!timing_file.empty()) {
                const auto& c = prof::cur;
                timing.push_back({{"step", i}, {"elapsed_s", (long)elapsed},
                    {"steps_in_bucket", stride},
                    {"ext_arb1_ms", c.ext_arb1 * 1e3}, {"oracle_ms", c.oracle * 1e3},
                    {"softliq_ms", c.softliq * 1e3}, {"hardliq_ms", c.hardliq * 1e3},
                    {"ext_arb2_ms", c.ext_arb2 * 1e3}, {"emit_ms", c.emit * 1e3},
                    {"venue_quotes", c.venue_quotes}, {"venue_execs", c.venue_execs},
                    {"venue_clones", c.venue_clones}, {"book_copies", c.book_copies},
                    {"book_trades", c.book_trades}, {"arb_evals", c.arb_evals},
                    {"spot_probes", c.spot_probes}, {"ternaries", c.ternaries},
                    {"steps_searched", c.steps_searched}});
            }
            prof::total += prof::cur;
            prof::cur = prof::Acc{};
            agg_hard_usd = agg_hard_profit = agg_ext_usd = agg_hard_coll = 0;
            agg_hard_tok = agg_hard_tok_usd = 0;
            agg_n_hard = 0;
            agg_cure_usd = 0; agg_n_cure = 0;
            agg_ext_dir.clear();
        }
    }

    prof::total += prof::cur;       // trailing partial bucket
    n_hard_grand += n_hard_total;
    n_cure_grand += n_cure_total; cure_usd_grand += cure_usd_path;
    n_trades_grand += n_trades_total;
    // oracle/arb/timing sidecars describe ONE run; only single mode has one.
    if (!batch) {
        if (!timing_file.empty()) std::ofstream(timing_file) << timing.dump();
        if (!oracle_file.empty()) std::ofstream(oracle_file) << oracle_out.dump();
        if (!arb_file.empty()) std::ofstream(arb_file) << arb_log.dump();
    }
    all_out.push_back(std::move(rows));
    }   // ---- end per-path loop -------------------------------------------

    std::ofstream(out_file) << (batch ? all_out : all_out[0]).dump();

    double secs = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t_start).count();
    std::fprintf(stderr,
                 "[routed-cpp] %zu path(s) x %ld steps (dt=%.0fs) in %.2fs  "
                 "soft-liq trades=%ld  hard liqs=%ld  cures=%ld ($%.0f)\n",
                 paths_list.size(), n_steps, dt, secs,
                 n_trades_grand, n_hard_grand, n_cure_grand, cure_usd_grand);
    {
        const auto& T = prof::total;
        double acc = T.ext_arb1 + T.oracle + T.softliq + T.hardliq + T.ext_arb2 + T.emit;
        auto pc = [&](double v) { return 100.0 * v / std::max(secs, 1e-9); };
        std::fprintf(stderr,
            "[profile] ext_arb1 %.2fs (%.1f%%) | oracle %.2fs (%.1f%%) | "
            "softliq %.2fs (%.1f%%) | hardliq %.2fs (%.1f%%) | ext_arb2 %.2fs (%.1f%%) | "
            "emit %.2fs (%.1f%%) | accounted %.1f%%\n",
            T.ext_arb1, pc(T.ext_arb1), T.oracle, pc(T.oracle), T.softliq, pc(T.softliq),
            T.hardliq, pc(T.hardliq), T.ext_arb2, pc(T.ext_arb2), T.emit, pc(T.emit),
            pc(acc));
        std::fprintf(stderr,
            "[profile] venue: %ld quotes, %ld execs, %ld clones, %ld spot probes | "
            "book: %ld copies, %ld band-walks | arb pnl evals: %ld\n",
            T.venue_quotes, T.venue_execs, T.venue_clones, T.spot_probes,
            T.book_copies, T.book_trades, T.arb_evals);
        std::fprintf(stderr,
            "[profile] search: %ld steps searched (%.1f%% of steps), %ld rounds, "
            "%ld ternaries (%.2f per searched step), %ld iters "
            "(%.1f per ternary) -> %.1f evals per ternary\n",
            T.steps_searched,
            100.0 * T.steps_searched
                / std::max(n_steps * (long)paths_list.size(), 1L),
            T.arb_rounds, T.ternaries,
            (double)T.ternaries / std::max(T.steps_searched, 1L),
            T.ternary_iters, (double)T.ternary_iters / std::max(T.ternaries, 1L),
            (double)T.arb_evals / std::max(T.ternaries, 1L));
    }
    return 0;
}
