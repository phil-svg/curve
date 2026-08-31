// lt.hpp — wei-exact C++ port of the Yield Basis LT vault (LT.vy, vyper
// 0.4.3, yield-basis/yb-core): the leveraged-liquidity share accounting.
//
// Ports _calculate_values (the staker/admin value split with loss
// recovery), deposit share minting, withdraw fraction math, admin-fee
// minting and borrower-fee distribution, on exact signed 256-bit
// arithmetic (vyper signed division truncates toward zero; cpp_int does
// too). The cryptopool legs and the AMM belong to their own validated
// engines (twocrypto_yb.hpp, levamm.hpp); observable values are inputs.
#pragma once
#include "levamm.hpp"

namespace ltvault {

using levamm::E18;
using levamm::isqrt;

inline const z256& Z18() { static const z256 v = z256(ONE_1E18()); return v; }
inline const z256& Z36() { static const z256 v = Z18() * Z18(); return v; }

// vyper signed // compiles to SDIV: truncation toward zero (validated
// empirically — gen-1 YB deposits are exact under trunc, break under
// Python-style floor)
inline z256 floordiv(const z256& a, const z256& b) {
    return a / b;                         // cpp_int also truncates
}

// vyper: math._mul_div magnitudes floor + sign parity (trunc semantics —
// this helper is explicit magnitude math in the source)
inline z256 mul_div_signed(const z256& x, const z256& y, const z256& den) {
    if (den == 0) return 0;
    z256 v = (abs(x) * abs(y)) / abs(den);
    if (((x < 0) != (y < 0)) != (den < 0)) v = -v;
    return v;
}

struct LiquidityValues {        // self.liquidity
    z256 admin;                 // int256, can be negative
    u256 total;
    u256 ideal_staked;
    u256 staked;
};

struct LtState {
    LiquidityValues liquidity;
    u256 total_supply;
    u256 staked_tokens;         // balanceOf[staker] (0 if staker unset)
    bool has_staker = false;
    u256 min_admin_fee;         // Factory.min_admin_fee() (0 if admin EOA)
};

struct ValuesOut {
    z256 admin;
    u256 total;
    u256 ideal_staked;
    u256 staked;
    u256 staked_tokens;
    u256 supply_tokens;
    z256 token_reduction;
};

inline const z256 SQRT_MIN_UNSTAKED_FRACTION = as_z256(pow10(14));
inline const z256 MIN_STAKED_FOR_FEES = as_z256(pow10(16));

// LT._calculate_values(p_o, amm_value)
inline ValuesOut calculate_values(const LtState& s, const u256& p_o,
                                  const u256& amm_value) {
    const LiquidityValues& prev = s.liquidity;
    z256 staked = s.has_staker ? as_z256(s.staked_tokens) : z256(0);
    z256 supply = as_z256(s.total_supply);

    z256 fa_inner = Z36() - floordiv(staked * Z36(), supply);
    z256 f_a = Z18()
        - (Z18() - as_z256(s.min_admin_fee))
          * as_z256(isqrt(u256(fa_inner))) / Z18();

    z256 cur_value = as_z256(amm_value * E18() / p_o);
    z256 prev_value = as_z256(prev.total);
    z256 value_change = cur_value - (prev_value + prev.admin);

    z256 v_st = as_z256(prev.staked);
    z256 v_st_ideal = as_z256(prev.ideal_staked);

    z256 dv_use_36 = 0;
    z256 v_st_loss = v_st_ideal - v_st;
    if (v_st_loss < 0) v_st_loss = 0;
    if (staked >= MIN_STAKED_FOR_FEES) {
        if (value_change > 0) {
            z256 cap = floordiv(v_st_loss * supply, staked);
            z256 v_loss = value_change < cap ? value_change : cap;
            dv_use_36 = v_loss * Z18()
                      + (value_change - v_loss) * (Z18() - f_a);
        } else {
            dv_use_36 = value_change * Z18();
        }
    } else {
        dv_use_36 = value_change * (Z18() - f_a);
    }

    z256 admin = prev.admin + (value_change - floordiv(dv_use_36, Z18()));

    z256 dv_s_36 = mul_div_signed(dv_use_36, staked, supply);
    if (dv_use_36 > 0) {
        z256 cap = v_st_loss * Z18();
        if (dv_s_36 > cap) dv_s_36 = cap;
    }

    z256 new_total_36 = prev_value * Z18() + dv_use_36;
    if (new_total_36 < 0) new_total_36 = 0;
    z256 new_staked_36 = v_st * Z18() + dv_s_36;
    if (new_staked_36 < 0) new_staked_36 = 0;

    z256 den = new_total_36 - new_staked_36;
    z256 token_reduction = mul_div_signed(new_total_36, staked, den)
                         - mul_div_signed(new_staked_36, supply, den);

    z256 max_red = floordiv(value_change * supply,
                            prev_value + value_change + 1);
    max_red = max_red * (Z18() - f_a);
    max_red = abs(floordiv(max_red, SQRT_MIN_UNSTAKED_FRACTION));

    if (staked > 0 && token_reduction > staked - 1)
        token_reduction = staked - 1;
    if (supply > 0 && token_reduction > supply - 1)
        token_reduction = supply - 1;
    if (token_reduction >= 0) {
        if (token_reduction > max_red) token_reduction = max_red;
    } else {
        if (token_reduction < -max_red) token_reduction = -max_red;
    }
    if (den < as_z256(pow10(22)) && token_reduction < 0)
        token_reduction = 0;

    ValuesOut o;
    o.admin = admin;
    o.total = to_uint256_mod(floordiv(new_total_36, Z18()));
    o.ideal_staked = prev.ideal_staked;
    o.staked = to_uint256_mod(floordiv(new_staked_36, Z18()));
    o.staked_tokens = to_uint256_mod(staked - token_reduction);
    o.supply_tokens = to_uint256_mod(supply - token_reduction);
    o.token_reduction = token_reduction;
    return o;
}

// deposit(): shares minted for an existing vault (supply > 0, total > 0).
// value_after_fiat = AMM value after _deposit (the AddLiquidityRaw
// invariant); p_o = the LT price oracle (price_scale * agg price / 1e18).
inline u256 deposit_shares(const ValuesOut& lv, const u256& value_after_amm,
                           const u256& p_o) {
    u256 value_after = to_uint256_mod(
        as_z256(value_after_amm * E18() / p_o) - lv.admin);
    u256 supply = lv.supply_tokens;
    return supply * value_after / lv.total - supply;
}

// deposit() when the vault is empty: shares = value in crypto units
inline u256 deposit_shares_initial(const u256& value_after_amm,
                                   const u256& p_o) {
    return value_after_amm * E18() / p_o;
}

// withdraw(): the fraction handed to AMM._withdraw
inline u256 withdraw_frac(const ValuesOut& lv, const u256& shares) {
    u256 admin_pos = lv.admin > 0 ? u256(lv.admin) : u256(0);
    return E18() * lv.total / (lv.total + admin_pos) * shares
         / lv.supply_tokens;
}

// withdraw_admin_fees(): tokens minted to the fee receiver
inline u256 admin_fee_mint(const ValuesOut& lv) {
    u256 new_total = lv.total + (lv.admin > 0 ? u256(lv.admin) : u256(0));
    return lv.supply_tokens * new_total / lv.total - lv.supply_tokens;
}

// _distribute_borrower_fees(): the add_liquidity min_amount guard
inline u256 bfees_min_amount(const u256& amount, const u256& discount,
                             const u256& lp_price) {
    return (E18() - discount) * amount / lp_price;
}

}  // namespace ltvault
