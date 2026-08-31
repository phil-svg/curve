// pegkeeper.hpp — wei-exact C++ port of the crvUSD PegKeepers.
//
// Ports, line for line on unchecked 256-bit integers:
//   * PegKeeper.vy   (V1, curve-stablecoin) — update() amount selection
//   * PegKeeperV2.vy (V2)                   — update() amount selection
//   * PegKeeperRegulator.vy                 — provide_allowed / withdraw_allowed
//
// The pool legs (add_liquidity / remove_liquidity_imbalance effects,
// virtual_price) belong to the separately-validated stableswap engines; this
// engine takes their observable values as inputs and reproduces every value
// the keeper itself computes: the provide/withdraw amount, the debt
// trajectory, and the regulator's allowance.
//
// Validated by replaying every Provide/Withdraw event of every deployed
// keeper against the chain (see replay.cpp; harness fetches per-event
// pre-state from an archive node).
#pragma once
#include "u256.hpp"

#include <optional>
#include <vector>

namespace pegkeeper {

inline const u256& ONE() { return ONE_1E18(); }

// vyper isqrt(): floor integer square root (Babylonian, as in vyper's
// builtin — result r with r*r <= x < (r+1)*(r+1))
inline u256 isqrt(const u256& x) {
    if (x == 0) return 0;
    z256 z = as_z256(x);
    z256 r = z, y = (z + 1) / 2;
    while (y < r) { r = y; y = (z / y + y) / 2; }
    return u256(r);
}

// ---- PegKeeperRegulator ---------------------------------------------------

struct RegKeeperInfo {           // one entry of regulator.peg_keepers
    bool is_self = false;        // this entry IS the acting keeper
    u256 price_oracle;           // pool.price_oracle([0]) raw
    u256 get_p;                  // pool.get_p([0]) raw (self entry only)
    bool is_inverse = false;
    u256 debt;                   // other keepers: debt()
    u256 stable_balance;         // other keepers: STABLECOIN.balanceOf(pk)
};

struct RegState {
    bool killed_provide = false;
    bool killed_withdraw = false;
    u256 agg_price;              // aggregator.price()
    u256 worst_price_threshold;
    u256 price_deviation;
    u256 alpha, beta;
    std::vector<RegKeeperInfo> infos;
    u256 pk_debt;                // acting keeper debt()
    u256 pk_stable_balance;      // STABLECOIN.balanceOf(acting keeper)
};

inline u256 reg_price(const RegKeeperInfo& i, const u256& raw) {
    // _get_price / _get_price_oracle inversion leg
    return i.is_inverse ? ONE_1E36() / raw : raw;
}

inline bool price_in_range(const u256& p0, const u256& p1,
                           const u256& deviation) {
    // unsafe wrap semantics preserved by unchecked u256
    return unsafe_sub(unsafe_add(deviation, p0), p1) < (deviation << 1);
}

inline u256 get_max_ratio(const RegState& s,
                          const std::vector<u256>& ratios) {
    u256 rsum = 0;
    for (const u256& r : ratios) rsum += isqrt(r * ONE());
    u256 base = s.alpha + s.beta * rsum / ONE();
    return base * base / ONE();
}

// PegKeeperRegulator.provide_allowed(_pk)
inline u256 provide_allowed(const RegState& s) {
    if (s.killed_provide) return 0;
    if (s.agg_price < ONE()) return 0;
    u256 price = ~u256(0);       // max_value — fails if self not present
    u256 largest_price = 0;
    std::vector<u256> ratios;
    for (const auto& i : s.infos) {
        u256 po = reg_price(i, i.price_oracle);
        if (i.is_self) {
            price = po;
            if (!price_in_range(price, reg_price(i, i.get_p),
                                s.price_deviation))
                return 0;
            continue;
        }
        if (largest_price < po) largest_price = po;
        ratios.push_back(i.debt * ONE()
                         / (1 + i.debt + i.stable_balance));
    }
    if (largest_price < unsafe_sub(price, s.worst_price_threshold)) return 0;
    u256 total = s.pk_debt + s.pk_stable_balance;
    u256 cap = get_max_ratio(s, ratios) * total / ONE();
    if (cap < s.pk_debt) return 0;   // vyper would revert on underflow
    return cap - s.pk_debt;
}

// PegKeeperRegulator.withdraw_allowed(_pk)
inline u256 withdraw_allowed(const RegState& s) {
    if (s.killed_withdraw) return 0;
    if (s.agg_price > ONE()) return 0;
    for (const auto& i : s.infos)
        if (i.is_self)
            return price_in_range(reg_price(i, i.get_p),
                                  reg_price(i, i.price_oracle),
                                  s.price_deviation)
                ? ~u256(0) : 0;
    return 0;
}

// ---- the keepers ----------------------------------------------------------

struct PreState {
    u256 ts;                     // block.timestamp
    u256 last_change;
    u256 action_delay;           // V1: constant 900; V2: state
    u256 bal_pegged;             // POOL.balances(I)
    u256 bal_peg_raw;            // POOL.balances(1 - I), before PEG_MUL
    u256 debt;                   // keeper debt()
    u256 pegged_balance;         // PEGGED.balanceOf(keeper)  (V2 cap)
    u256 agg_price;              // V1 price gate
    std::optional<RegState> reg; // V2 only
};

struct Action {
    enum Kind { NONE, PROVIDE, WITHDRAW, BLOCKED } kind = NONE;
    u256 amount = 0;
    u256 debt_after = 0;
    u256 allowed = 0;            // V2: the regulator allowance used
};

// PegKeeper.vy (V1) update() — amount + debt trajectory
inline Action update_v1(const PreState& p, const u256& peg_mul) {
    Action a;
    a.debt_after = p.debt;
    if (p.last_change + p.action_delay > p.ts) return a;
    u256 bal_pegged = p.bal_pegged;
    u256 bal_peg = p.bal_peg_raw * peg_mul;
    if (bal_peg > bal_pegged) {
        if (p.agg_price < ONE()) { a.kind = Action::BLOCKED; return a; }
        a.kind = Action::PROVIDE;
        a.amount = (bal_peg - bal_pegged) / 5;   // no balance cap in V1
        a.debt_after = p.debt + a.amount;
    } else {
        if (p.agg_price > ONE()) { a.kind = Action::BLOCKED; return a; }
        a.kind = Action::WITHDRAW;
        u256 amt = (bal_pegged - bal_peg) / 5;
        a.amount = amt < p.debt ? amt : p.debt;
        a.debt_after = p.debt - a.amount;
    }
    return a;
}

// PegKeeperV2.vy update() — amount + debt trajectory
inline Action update_v2(const PreState& p, const u256& peg_mul) {
    Action a;
    a.debt_after = p.debt;
    if (p.last_change + p.action_delay > p.ts) return a;
    u256 bal_pegged = p.bal_pegged;
    u256 bal_peg = p.bal_peg_raw * peg_mul;
    if (bal_peg > bal_pegged) {
        a.allowed = provide_allowed(*p.reg);
        if (a.allowed == 0) { a.kind = Action::BLOCKED; return a; }
        u256 amt = unsafe_sub(bal_peg, bal_pegged) / 5;
        if (a.allowed < amt) amt = a.allowed;
        // _provide: amount = min(_amount, PEGGED.balanceOf(self))
        if (p.pegged_balance < amt) amt = p.pegged_balance;
        a.kind = Action::PROVIDE;
        a.amount = amt;
        a.debt_after = p.debt + amt;
    } else {
        a.allowed = withdraw_allowed(*p.reg);
        if (a.allowed == 0) { a.kind = Action::BLOCKED; return a; }
        u256 amt = unsafe_sub(bal_pegged, bal_peg) / 5;
        if (a.allowed < amt) amt = a.allowed;
        // _withdraw: amount = min(_amount, debt)
        if (p.debt < amt) amt = p.debt;
        a.kind = Action::WITHDRAW;
        a.amount = amt;
        a.debt_after = p.debt - amt;
    }
    return a;
}

}  // namespace pegkeeper
