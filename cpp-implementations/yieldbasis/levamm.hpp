// levamm.hpp — wei-exact C++ port of the Yield Basis leverage AMM (AMM.vy,
// "LEVAMM", vyper 0.4.3, yield-basis/yb-core).
//
// Ports the full state machine on unchecked 256-bit integers: get_x0 (the
// constant-leverage invariant), interest accrual (rate_mul), exchange in
// both directions with the final-state check, _deposit / _withdraw (the LT
// legs), and _collect_fees. The LP-price oracle and the cryptopool belong
// to other (validated) engines; their observable values are inputs.
//
// Validated by replaying every TokenExchange / AddLiquidityRaw /
// RemoveLiquidityRaw / CollectFees event of every deployed YB AMM against
// mainnet (see replay_yb.cpp).
#pragma once
#include "u256.hpp"

#include <optional>
#include <stdexcept>

namespace levamm {

inline const u256& E18() { return ONE_1E18(); }

inline u256 isqrt(const u256& x) {          // vyper isqrt: floor sqrt
    if (x == 0) return 0;
    z256 z = as_z256(x);
    z256 r = z, y = (z + 1) / 2;
    while (y < r) { r = y; y = (z / y + y) / 2; }
    return u256(r);
}

inline u256 ceil_div(const u256& a, const u256& b) {  // snekmate _ceil_div
    if (a == 0) return 0;
    return (a - 1) / b + 1;
}

struct Params {                 // immutables (leverage fixed at 2e18)
    u256 leverage = u256("2000000000000000000");
    u256 collateral_precision = 1;   // 10**(18 - LP decimals) — LP has 18
    u256 lev_ratio() const {
        u256 den = 2 * leverage - E18();
        return leverage * leverage * E18() / (den * den);
    }
    u256 min_safe_debt() const {     // 1 / (4 L^2), 1e18-based
        return pow10(54) / (4 * leverage * leverage);
    }
    u256 max_safe_debt() const {
        u256 den = 2 * leverage - E18();
        return den * den * E18() / (4 * leverage * leverage)
             - pow10(54) / (8 * leverage * leverage);
    }
};

struct State {
    u256 collateral;            // collateral_amount
    u256 debt;                  // CURRENT debt (self.debt scaled by rate),
                                // i.e. get_debt() at execution time
    u256 fee;                   // exchange fee, 1e18-based
    u256 minted;
    u256 redeemed;
    u256 stables_balance;       // STABLECOIN.balanceOf(amm)
};

// get_x0(p_oracle, collateral, debt, safe_limits) — throws on safe-limit
// violation exactly where the contract asserts
inline u256 get_x0(const Params& P, const u256& p_o, const u256& coll,
                   const u256& debt, bool safe_limits) {
    u256 coll_value = p_o * coll * P.collateral_precision / E18();
    if (safe_limits) {
        if (!(debt >= coll_value * P.min_safe_debt() / E18()))
            throw std::runtime_error("Unsafe min");
        if (!(debt <= coll_value * P.max_safe_debt() / E18()))
            throw std::runtime_error("Unsafe max");
    }
    u256 D = coll_value * coll_value
           - 4 * coll_value * P.lev_ratio() / E18() * debt;
    return (coll_value + isqrt(D)) * E18() / (2 * P.lev_ratio());
}

struct ExchangeResult {
    u256 out_amount;
    u256 collateral_after;
    u256 debt_after;
    bool ok = true;
    std::string err;
};

// exchange(i, j, in_amount) at oracle price p_o (the event's price_oracle)
inline ExchangeResult exchange(const Params& P, const State& s, unsigned i,
                               const u256& in_amount, const u256& p_o) {
    ExchangeResult r;
    try {
        u256 collateral = s.collateral;
        u256 debt = s.debt;
        u256 x0 = get_x0(P, p_o, collateral, debt, false);
        u256 x_initial = x0 - debt;
        u256 fee = s.fee;

        u256 cvb_before = debt == 0 ? ~u256(0)
            : unsafe_div(p_o * collateral * P.collateral_precision, debt);

        if (i == 0) {           // trader buys collateral
            u256 x = x_initial + in_amount;
            u256 y = ceil_div(x_initial * collateral, x);
            r.out_amount = (collateral - y) * (E18() - fee) / E18();
            debt -= in_amount;
            collateral -= r.out_amount;
        } else {                // trader sells collateral
            u256 y = collateral + in_amount;
            u256 x = ceil_div(x_initial * collateral, y);
            r.out_amount = (x_initial - x) * (E18() - fee) / E18();
            debt += r.out_amount;
            collateral = y;
        }

        u256 cvb_after = debt == 0 ? ~u256(0)
            : unsafe_div(p_o * collateral * P.collateral_precision, debt);

        bool check_state = true;
        if (cvb_after > 2 * E18()) {
            if (cvb_before > cvb_after) check_state = false;
        } else {
            if (cvb_before < cvb_after) check_state = false;
        }
        if (!(get_x0(P, p_o, collateral, debt, check_state) >= x0))
            throw std::runtime_error("Bad final state");

        r.collateral_after = collateral;
        r.debt_after = debt;
    } catch (const std::exception& e) {
        r.ok = false;
        r.err = e.what();
    }
    return r;
}

struct DepositResult { u256 value; u256 collateral_after; u256 debt_after; };

// _deposit(d_collateral, d_debt) at oracle price p_o — returns the logged
// invariant (value_after); throws where the contract would revert
inline DepositResult amm_deposit(const Params& P, const State& s,
                                 const u256& d_coll, const u256& d_debt,
                                 const u256& p_o) {
    DepositResult r;
    r.debt_after = s.debt + d_debt;
    r.collateral_after = s.collateral + d_coll;
    r.value = get_x0(P, p_o, r.collateral_after, r.debt_after, true)
            * E18() / (2 * P.leverage - E18());
    return r;
}

struct WithdrawResult { u256 d_collateral; u256 d_debt; };

// _withdraw(frac)
inline WithdrawResult amm_withdraw(const State& s, const u256& frac) {
    WithdrawResult r;
    r.d_collateral = s.collateral * frac / E18();
    r.d_debt = ceil_div(s.debt * frac, E18());
    return r;
}

struct FeesResult { u256 amount; u256 new_supply; u256 minted_after; };

// _collect_fees()
inline FeesResult collect_fees(const State& s) {
    FeesResult r;
    r.new_supply = s.debt;
    u256 minted = s.minted;
    u256 to_be_redeemed = s.debt + s.redeemed;
    if (to_be_redeemed > minted) {
        r.minted_after = to_be_redeemed;
        to_be_redeemed = unsafe_sub(to_be_redeemed, minted);
        if (s.stables_balance < to_be_redeemed) {
            r.minted_after -= (to_be_redeemed - s.stables_balance);
            to_be_redeemed = s.stables_balance;
        }
        r.amount = to_be_redeemed;
    } else {
        r.amount = 0;
        r.minted_after = minted;
    }
    return r;
}

// the short-lived November-2025 "adjustment" vintage of the LT measures
// AMM value with cryptopool collateral scaled to exclude rebalancing
// reserves: coll * (xcp_profit + 1e18) / (2 * virtual_price)
inline u256 adjust_collateral(const u256& coll, const u256& xcp_profit,
                              const u256& virtual_price) {
    return coll * (xcp_profit + E18()) / (2 * virtual_price);
}

// value_oracle().value for a given state (used by the LT engine)
inline u256 value_oracle(const Params& P, const u256& p_lp,
                         const u256& coll, const u256& debt) {
    return get_x0(P, p_lp, coll, debt, false) * E18()
         / (2 * P.leverage - E18());
}

}  // namespace levamm
