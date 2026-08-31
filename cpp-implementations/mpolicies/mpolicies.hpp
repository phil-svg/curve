// mpolicies.hpp — wei-exact C++ ports of the LlamaLend monetary policies.
//
//   * SemilogMonetaryPolicy.vy: rate = exp(debt * (ln max - ln min) /
//     reserves + ln min), with the contract's exact solmate-style wad exp
//     and its log2-based ln
//   * SecondaryMonetaryPolicy.vy: rate = r0 * r_minf/1e18
//     + A * r0 / (u_inf - u) + shift, u = debt*1e18/reserves, r0 = the
//     crvUSD AMM base rate (chain input)
//   * EMAMonetaryPolicy: the deployed secondary variant — same hyperbola
//     with SIGNED r_minf over an EMA of an external rate calculator
//   * HyperbolicDynamicMP: the LLV2 dynamic policy — hyperbola over a
//     clamped external calculator rate, reserves net of admin fees, and
//     the stored curve derived from (target_utilization, low/high ratio)
//   * FlatTime-LinearMonetaryPolicy: rate = clamp(base + slope*dt)
//
// Validated by sampling every market's policy rate() across its on-chain
// history and matching wei-for-wei (see replay.cpp).
#pragma once
#include "u256.hpp"

namespace mpolicies {

inline const u256& E18() { return ONE_1E18(); }

// ---- SemilogMonetaryPolicy math -------------------------------------------

// the contract's exp(): solmate expWad on 2^96 fixed point, identical
// constants and operation order (intermediates fit int256 inside the
// guarded input range, so plain big-int arithmetic is semantically exact)
inline u256 semilog_exp(const z256& power) {
    static const u256 MAX_EXP =
        to_u256("115792089237316195423570985008687907853269984665640564039457584007913129639935");
    if (power <= z256("-41446531673892821376")) return 0;
    if (power >= z256("135305999368893231589"))
        return MAX_EXP;   // MAX_EXP constant in the contract

    const z256 P96 = z256(1) << 96;
    z256 x = (power * P96) / z256("1000000000000000000");

    z256 k = ((x * P96) / z256("54916777467707473351141471128")
              + (z256(1) << 95)) / P96;
    x = x - k * z256("54916777467707473351141471128");

    z256 y = x + z256("1346386616545796478920950773328");
    y = (y * x) / P96 + z256("57155421227552351082224309758442");
    z256 p = y + x - z256("94201549194550492254356042504812");
    p = (p * y) / P96 + z256("28719021644029726153956944680412240");
    p = p * x + (z256("4385272521454847904659076985693276") * P96);

    z256 q = x - z256("2855989394907223263936484059900");
    q = (q * x) / P96 + z256("50020603652535783019961831881945");
    q = (q * x) / P96 - z256("533845033583426703283633433725380");
    q = (q * x) / P96 + z256("3604857256930695427073651918091429");
    q = (q * x) / P96 - z256("14423608567350463180887372962807573");
    q = (q * x) / P96 + z256("26449188498355588339934803723976023");

    z256 r = (p / q)
        * z256("3822833074963236453042738258902158003155416615667");
    // shift(uint256(r), k - 195): k <= ~2 here, so this right-shifts
    u256 ru = to_uint256_mod(r);
    z256 sh = k - 195;
    if (sh >= 0) return ru << (unsigned)sh;
    return ru >> (unsigned)(-sh);
}

// the contract's ln_int()
inline z256 semilog_ln(const u256& _x) {
    u256 x = _x;
    if (_x < E18()) x = pow10(36) / _x;
    u256 res = 0;
    for (int i = 0; i < 8; i++) {
        unsigned t = 1u << (7 - i);
        u256 p = u256(1) << t;
        if (x >= p * E18()) {
            x /= p;
            res += u256(t) * E18();
        }
    }
    u256 d = E18();
    for (int i = 0; i < 59; i++) {
        if (x >= 2 * E18()) {
            res += d;
            x /= 2;
        }
        x = x * x / E18();
        d /= 2;
    }
    z256 result = as_z256(res * E18() / to_u256("1442695040888963328"));
    return _x >= E18() ? result : -result;
}

// calculate_rate(_for, 0, 0): inputs read from chain at the sample block
inline u256 semilog_rate(const u256& total_debt, const u256& balance,
                         const u256& min_rate, const z256& log_min_rate,
                         const z256& log_max_rate) {
    z256 debt = as_z256(total_debt);
    z256 reserves = as_z256(balance) + debt;
    if (debt == 0) return min_rate;
    return semilog_exp(debt * (log_max_rate - log_min_rate) / reserves
                       + log_min_rate);
}

// ---- SecondaryMonetaryPolicy ----------------------------------------------

inline u256 secondary_rate(const u256& total_debt, const u256& balance,
                           const u256& u_inf, const u256& A,
                           const u256& r_minf, const u256& shift,
                           const u256& r0) {
    u256 reserves = balance + total_debt;
    u256 u = 0;
    if (reserves > 0) u = total_debt * E18() / reserves;
    return r0 * r_minf / E18() + A * r0 / (u_inf - u) + shift;
}

// ---- EMAMonetaryPolicy (the deployed "secondary" for yield-stables) -------
// hyperbolic secondary curve over an EMA of an external rate calculator;
// r_minf is SIGNED here. ema uses the same solmate exp.

inline u256 ema_rate(const u256& prev_rate, const u256& prev_ma_rate,
                     const u256& last_timestamp, const u256& ts) {
    static const u256 MIN_EMA_RATE = to_u256("317097920");
    u256 ema = prev_ma_rate;
    if (last_timestamp != ts) {
        // alpha = exp(-(dt * (1e18 // TEXP))), TEXP = 40000
        z256 power = -as_z256((ts - last_timestamp)
                              * (E18() / u256(40000)));
        u256 alpha = semilog_exp(power);
        ema = (prev_rate * (E18() - alpha) + prev_ma_rate * alpha) / E18();
    }
    return ema > MIN_EMA_RATE ? ema : MIN_EMA_RATE;
}

inline u256 ema_secondary_rate(const u256& total_debt, const u256& balance,
                               const u256& u_inf, const u256& A,
                               const z256& r_minf, const u256& shift,
                               const u256& r0) {
    u256 reserves = balance + total_debt;
    u256 u = 0;
    if (reserves > 0) u = total_debt * E18() / reserves;
    z256 a = as_z256(r0) * r_minf / as_z256(E18());   // trunc, signed
    z256 b = as_z256(A * r0 / (u_inf - u));
    z256 rate = a + b + as_z256(shift);
    return to_uint256_mod(rate);   // contract asserts rate >= 0
}

// ---- HyperbolicDynamicMP (the LLV2 dynamic policy) ------------------------
// same hyperbola as the EMA secondary, but r0 comes from an external rate
// calculator clamped to [~1%, ~150%] APR, and utilization uses the
// controller's available_balance net of admin fees. The stored curve
// (u_inf, A, r_minf) is derived from the raw knobs at set_parameters time.

struct llv2_params { u256 u_inf, A; z256 r_minf; };

inline llv2_params llv2_get_params(const u256& u0, const u256& alpha,
                                   const u256& beta) {
    llv2_params p;
    u256 numerator = (beta - E18()) * u0;
    u256 subtrahend = (E18() - u0) * (E18() - alpha);
    p.u_inf = numerator * E18() / (numerator - subtrahend);
    p.A = (E18() - alpha) * p.u_inf / E18() * (p.u_inf - u0) / u0;
    p.r_minf = as_z256(alpha) - as_z256(p.A * E18() / p.u_inf);
    return p;
}

inline u256 llv2_dynamic_rate(const u256& total_debt,
                              const u256& available_balance,
                              const u256& admin_fees,
                              const u256& u_inf, const u256& A,
                              const z256& r_minf, const u256& rate_shift,
                              const u256& raw_calc_rate) {
    static const u256 MIN_TARGET_RATE = to_u256("317097920");
    static const u256 MAX_TARGET_RATE = to_u256("47564687975");
    u256 r0 = raw_calc_rate;
    if (r0 < MIN_TARGET_RATE) r0 = MIN_TARGET_RATE;
    if (r0 > MAX_TARGET_RATE) r0 = MAX_TARGET_RATE;

    z256 reserves = as_z256(available_balance) + as_z256(total_debt)
                    - as_z256(admin_fees);
    u256 u = 0;
    if (reserves > 0) {
        u = total_debt * E18() / to_uint256_mod(reserves);
        if (u > E18()) u = E18();
    }
    z256 a = as_z256(r0) * r_minf / as_z256(E18());   // trunc, signed
    z256 b = as_z256(A * r0 / (u_inf - u));
    z256 rate = a + b + as_z256(rate_shift);
    if (rate < 0) return 0;      // clamped, not reverted, in this policy
    return to_uint256_mod(rate);
}

// ---- FlatTime-LinearMonetaryPolicy ----------------------------------------
// rate(t) = clamp(base_rate + slope * min(t - snapshot_time, MAX_ELAPSED),
//                 min_rate, max_rate); single instance, multi-market,
// utilization-independent (verified deployed source, vyper 0.4.3)

inline u256 flat_linear_rate(const u256& base_rate, const u256& slope,
                             const u256& snapshot_time, const u256& ts,
                             const u256& min_rate, const u256& max_rate) {
    static const u256 MAX_ELAPSED = u256(100) * 365 * 86400;
    u256 elapsed = 0;
    if (ts > snapshot_time) {
        elapsed = ts - snapshot_time;
        if (elapsed > MAX_ELAPSED) elapsed = MAX_ELAPSED;
    }
    u256 r = base_rate + slope * elapsed;
    if (r < min_rate) r = min_rate;
    if (r > max_rate) r = max_rate;
    return r;
}

}  // namespace mpolicies
