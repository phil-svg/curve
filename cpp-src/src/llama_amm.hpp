// llama_amm.hpp — LLAMMA AMM state + math port.
// Line-for-line port of Curve LLAMMA amm.py (Vyper 0.3.10) using u256/i256.
#pragma once
#include "u256.hpp"
#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

// ---------------------------------------------------------------------------
// Storage state (mirrors amm.py declaration order)
// ---------------------------------------------------------------------------
struct LlammaImmutables {
    u256 A;
    u256 Aminus1;
    u256 A2;
    u256 Aminus12;
    u256 BORROWED_PRECISION;
    u256 COLLATERAL_PRECISION;
    u256 BASE_PRICE;
    u256 SQRT_BAND_RATIO;
    i256 LOG_A_RATIO;
    u256 MAX_ORACLE_DN_POW;
};

struct BandState {
    u256 x = 0;
    u256 y = 0;
    u256 shares = 0;
};

struct UserTicks {
    i256 ns0 = 0;
    i256 ns1 = 0;
    // per-band share allocation (in on-chain UserTicks these are 128-bit
    // fractions packed 2-per-slot; for V1 we store full shares per band).
    std::unordered_map<int64_t, u256> shares;
    // Controller-side per-user snapshot (from Controller.user_state).
    // These aren't updated by AMM math; they're overwritten on Controller
    // events (Borrow/Repay/UserState). If we don't replay Controller events
    // they stay at snapshot value — good enough for short block windows.
    u256 collateral = 0;
    u256 stablecoin = 0;
    u256 debt = 0;
    u256 N = 0;
};

struct LlammaState {
    // mutables
    u256 fee = 0;
    u256 admin_fee = 0;
    u256 rate = 0;
    u256 rate_time = 0;
    u256 rate_mul = pow10(18);
    i256 active_band = 0;
    i256 min_band = 0;
    i256 max_band = 0;
    u256 admin_fees_x = 0;
    u256 admin_fees_y = 0;
    u256 old_p_o = 0;
    u256 old_dfee = 0;
    u256 prev_p_o_time = 0;

    // hashmaps
    std::unordered_map<int64_t, BandState> bands;   // key = band index
    std::unordered_map<std::string, UserTicks> users;  // key = lowercased addr

    // exogenous "current block" context (set by replay driver on each event)
    u256 block_timestamp = 0;
    // exogenous external oracle price at current block (the AMM otherwise
    // reads price_oracle_contract.price() — we inject it here from a fetched
    // per-block table).
    u256 external_price = 0;
};

// ---------------------------------------------------------------------------
// LLAMMA math — read-only (do not mutate state)
// ---------------------------------------------------------------------------
// Corresponds to `sqrt_int`. Uses Newton's method on u256.
u256 sqrt_int(const u256& x);

// _rate_mul(): rate_mul * (1e18 + rate * (block.timestamp - rate_time)) / 1e18
u256 rate_mul_current(const LlammaState& s);

// _base_price(): BASE_PRICE * _rate_mul() / 1e18
u256 base_price(const LlammaImmutables& im, const LlammaState& s);

// Solmate-style exp polynomial with LLAMMA-specific power range.
// Returns exp(power / 2^96) approximated at ~1e-16 precision.
u256 solmate_exp(i256 power);

// _p_oracle_up(n): base_price × ((A-1)/A)^n via exp(-n·LOG_A_RATIO).
u256 p_oracle_up(const LlammaImmutables& im, const LlammaState& s, i256 n);

// limit_p_o(p): apply price-oracle guardrail and dynamic-fee decay.
// Returns [limited_p, dfee] as a 2-vector.
struct LimitedP { u256 p; u256 dfee; };
LimitedP limit_p_o(const LlammaImmutables& im, const LlammaState& s, u256 p);

// _price_oracle_ro(): limit_p_o(external_price).
LimitedP price_oracle_ro(const LlammaImmutables& im, const LlammaState& s);

// _get_y0(x, y, p_o, p_o_up): band invariant helper.
u256 get_y0(const LlammaImmutables& im, u256 x, u256 y, u256 p_o, u256 p_o_up);

// _get_p(n, x, y): AMM's current price in band n.
u256 get_p_in_band(const LlammaImmutables& im, const LlammaState& s, i256 n, u256 x, u256 y);

// public get_p()
inline u256 get_p(const LlammaImmutables& im, const LlammaState& s) {
    auto it = s.bands.find(s.active_band.convert_to<int64_t>());
    u256 x = it == s.bands.end() ? u256(0) : it->second.x;
    u256 y = it == s.bands.end() ? u256(0) : it->second.y;
    return get_p_in_band(im, s, s.active_band, x, y);
}

// get_dynamic_fee(p_o, p_o_up)
u256 get_dynamic_fee(const LlammaImmutables& im, u256 p_o, u256 p_o_up);

// ---------------------------------------------------------------------------
// LLAMMA math — read-only user summaries (see get_xy / get_xy_up)
// ---------------------------------------------------------------------------
// _read_user_tick_numbers is stored directly in the LlammaState via
// user.ns0/ns1 (unpacked at snapshot time). For _read_user_ticks we return
// the per-band shares vector.
std::vector<u256> read_user_ticks(const LlammaState& s, const std::string& user);

// _get_xy(user, is_sum): returns (Σx, Σy) or per-band vectors.
struct XY { u256 sumX = 0; u256 sumY = 0; std::vector<u256> xs; std::vector<u256> ys; };
XY get_xy_impl(const LlammaImmutables& im, const LlammaState& s, const std::string& user, bool is_sum);

// public get_sum_xy(user) — returns (x_sum, y_sum) already divided by BORROWED_PRECISION / COLLATERAL_PRECISION.
inline std::pair<u256, u256> get_sum_xy(const LlammaImmutables& im, const LlammaState& s, const std::string& user) {
    XY r = get_xy_impl(im, s, user, true);
    return {r.sumX, r.sumY};
}

// get_xy_up(user, use_y): adiabatic value of position. Backs `get_y_up` and `get_x_down`.
u256 get_xy_up(const LlammaImmutables& im, const LlammaState& s, const std::string& user, bool use_y);
inline u256 get_x_down(const LlammaImmutables& im, const LlammaState& s, const std::string& user) {
    return get_xy_up(im, s, user, false);
}
inline u256 get_y_up(const LlammaImmutables& im, const LlammaState& s, const std::string& user) {
    return get_xy_up(im, s, user, true);
}

// ---------------------------------------------------------------------------
// LLAMMA math — state-mutating
// ---------------------------------------------------------------------------
// DetailedTrade result of calc_swap_out / calc_swap_in
struct DetailedTrade {
    u256 in_amount = 0;
    u256 out_amount = 0;
    i256 n1 = 0;
    i256 n2 = 0;
    std::vector<u256> ticks_in;
    u256 last_tick_j = 0;
    u256 admin_fee = 0;
};

// calc_swap_out(pump, in_amount, [p_o, dfee], in_precision, out_precision)
DetailedTrade calc_swap_out(const LlammaImmutables& im, const LlammaState& s,
                            bool pump, u256 in_amount, u256 p_o, u256 p_dfee,
                            u256 in_precision, u256 out_precision);

// calc_swap_in — same shape as calc_swap_out but solves for input given desired output.
// Used on-chain when the tx is `exchange_dy`. Byte-exact port of amm.py:1161.
DetailedTrade calc_swap_in(const LlammaImmutables& im, const LlammaState& s,
                           bool pump, u256 out_amount, u256 p_o, u256 p_dfee,
                           u256 in_precision, u256 out_precision);

// Apply a TokenExchange event to state (mutates bands, active_band, admin_fees_*).
// Reads external_price from state so the caller must set it to price_oracle_contract.price()
// at that block, then also calls limit_p_o to update old_p_o/old_dfee/prev_p_o_time.
// i, j: 0=borrowed, 1=collateral.
void apply_token_exchange(const LlammaImmutables& im, LlammaState& s,
                          uint64_t i, uint64_t j, u256 tokens_sold, u256 tokens_bought);

// Apply a Deposit event (user, amount, n1, n2). Mirrors deposit_range().
void apply_deposit(const LlammaImmutables& im, LlammaState& s,
                   const std::string& user, u256 amount, i256 n1, i256 n2);

// Apply a Withdraw event by user + frac. Mirrors withdraw(). Emits (dx, dy).
std::pair<u256, u256> apply_withdraw(const LlammaImmutables& im, LlammaState& s,
                                     const std::string& user, u256 frac);

// Update oracle state at start of a block — mirrors _price_oracle_w().
LimitedP tick_oracle(const LlammaImmutables& im, LlammaState& s);

// Apply a trade specified as (i, j, dx) — computes dy internally via
// calc_swap_out and writes bands. Use for synthesizing arb trades where
// the caller doesn't know dy in advance.
void apply_trade_dx(const LlammaImmutables& im, LlammaState& s,
                    uint64_t i, uint64_t j, u256 dx);

// Simulate an arb that brings the AMM's marginal price to `target_p` (1e18).
// Bisects on trade size using calc_swap_out. No-op if already within tolerance.
// Returns the (i, j, dx) of the injected trade, or (0,0,0) if none.
struct SynthTrade { uint64_t i = 0; uint64_t j = 0; u256 dx = 0; u256 dy = 0; };
SynthTrade arb_to_target_price(const LlammaImmutables& im, LlammaState& s, u256 target_p);

// ---------------------------------------------------------------------------
// Health (Controller-side)
// ---------------------------------------------------------------------------
// Port of `computeHealthManual` in the smol TS project. All inputs come from
// LLAMMA state + per-user record + priceOracle read + liquidation_discount.
//   health = 1e18 - liquidation_discount
//   health = (xDown * health) / debt - 1e18
//   if full && ns0 > active_band && priceOracle > pUp:
//     health += ((priceOracle - pUp) * sumY * COLL_PREC) / (debt * BORR_PREC)
// Returns signed value scaled to 1e18. Negative means underwater.
i256 compute_health(const LlammaImmutables& im, const LlammaState& s,
                    const std::string& user, u256 liquidation_discount,
                    bool full = true,
                    const u256* p_oracle_override = nullptr);
