#pragma once
// ============================================================================
// tricrypto2.hpp — wei-exact replay state machine for the ORIGINAL Curve
// tricrypto2 pool (USDT/WBTC/WETH):
//   pool  0xd51a44d3fae010294c616388b506acda1bfaae46  (vyper 0.2.12)
//   math  0x8F68f4810CcE3194B6cB6F3d50fa58c2c9bDD1d5  (vyper 0.2.12)
//   token 0xc4AD29ba4B3c580e6D59105FFf484999997675Ff  (vyper 0.2.12, external
//         LP token: mint / mint_relative / burnFrom driven by the pool)
//   views 0x40745803C2faA8E8402E2Ae935933D07cA8f355c  (get_dy / calc_token_-
//         amount only — never touches state, not needed for replay)
//
// SOURCES FOLLOWED (authoritative): the VERIFIED DEPLOYED sources fetched
// from Blockscout (eth.blockscout.com getsourcecode) on 2026-08-29 for all
// three contracts above; ported line-by-line. This is the 2021 vintage —
// NOT tricrypto-NG. Differences from tricrypto_ng.hpp that this port
// reproduces exactly:
//   1. External math contract: geometric_mean is ITERATIVE (sorted Newton,
//      "diff <= 1 or diff*1e18 < D"), newton_D is seeded with
//      3*geometric_mean (no cbrt / K0_prev), newton_y is the plain Newton
//      solver (no analytic cubic), EMA alpha comes from halfpow
//      (0.5**(dt/ma_half_time) with 1e10 precision Taylor series — NOT
//      snekmate wad_exp), and tweak_price's norm uses sqrt_int
//      (Babylonian on x*1e18, raises if it never lands exactly).
//   2. tweak_price(A_gamma, _xp, i, p_i, new_D): last_prices comes from the
//      spot price p_i handed in by the caller (exchange / add_liquidity /
//      remove_one price formulas ported below). p_i == 0 (multi-coin or
//      dust ops) triggers the "calculate real prices" branch: bump
//      __xp[0] by __xp[0]/1e6 and probe with newton_y per quote coin.
//   3. Repeg gating via the persistent storage bool not_adjusted (slot 29):
//      once profit allows, it stays set across calls until a repeg attempt
//      fails the profit check (then cleared). adjustment_step is used
//      directly (no norm/5 lower bound); trigger is norm > step**2 checked
//      in 1e36 units BEFORE sqrt_int(norm/1e18).
//   4. future_A_gamma_time doubles as a flag: exchange/add set it to 1 when
//      a ramp just finished; remove_liquidity_one_coin sets it to 1
//      UNCONDITIONALLY (block.timestamp >= t always holds for t in {0,1});
//      tweak_price skips the "Loss" revert when t != 0 and resets 1 -> 0.
//      The "Loss" check itself is virtual_price < old (strict), only at
//      t == 0.
//   5. _claim_admin_fees runs ONLY from the standalone claim_admin_fees()
//      external and from apply_new_parameters() when admin_fee changes —
//      NOT inside add/remove (no dedup against those needed). It has no
//      early return: it ALWAYS re-derives D = newton_D(xp()) and
//      virtual_price, even with nothing to claim. ClaimAdminFee is logged
//      (possibly with 0 tokens) only when xcp_profit > xcp_profit_a AND
//      fees > 0. mint_relative: d_supply = supply*frac/1e18.
//   6. remove_liquidity: no claim, no tweak_price, no EMA. amount =
//      _amount - 1 always (no empty-pool branch); d_balances and the D
//      reduction use the PRE-BURN total supply.
//   7. remove_liquidity_one_coin: fee = _fee(xp) on the CURRENT xp (no
//      imprecise-withdrawal correction, no out_fee fallback);
//      D -= (dD - (fee*dD/(2*1e10) + 1)); update_D (newton_D instead of
//      self.D) only while future_A_gamma_time > 0.
//   8. Packed prices: 2 per word, 128 bits (PRICE_SIZE = 256/(N-1) = 128,
//      PRICE_MASK = 2**128-1, k=0 in the LOW half). Kept unpacked here;
//      every store site enforces the vyper `assert p < PRICE_MASK`.
//   9. Events (deployed shapes, all data uint256 words):
//        TokenExchange(address idx, sold_id, dx, bought_id, dy)   dy POST-fee
//        AddLiquidity(address idx, uint256[3], fee, token_supply) supply POST-mint
//        RemoveLiquidity(address idx, uint256[3], token_supply)   supply POST-burn
//        RemoveLiquidityOne(address idx, token_amount, i, dy)
//        ClaimAdminFee(address idx, tokens)
//        RampAgamma / StopRampA(A,gamma,time) / NewParameters(7 words incl.
//        admin_fee first) / CommitNewParameters (ignored, future_* only).
//  10. Vyper 0.2.12: all arithmetic checked (sub underflow -> revert,
//      ported as csub -> throw), uint division floors, no unsafe_* ops.
//      Non-short-circuit `and`: profit-gate subtractions (virtual_price*2 -
//      1e18, 2*vp_new - 1e18) are evaluated unconditionally, so they are
//      csub'ed before their sibling condition is looked at. Checked mul
//      overflow is unreachable for physical pool states (max intermediate
//      ~1e58 << 2**256) and is not modelled.
//  11. Reverts: any assert / checked-arith underflow / div-by-zero throws;
//      the runner reports {"revert": msg}, restores the pre-event state
//      snapshot, and continues (an on-chain event that we revert on is a
//      mismatch the validator will flag).
//
// SEMANTIC UNCERTAINTIES (documented, all verified benign for the
// validation window):
//   a. The gulp in _claim_admin_fees (balances[i] = ERC20.balanceOf(self))
//      is a no-op here: tracked storage balances equal real ERC20 balances
//      unless someone donates tokens directly (verified equal at window
//      start; swaps/withdrawals in this contract keep fees inside
//      self.balances).
//   b. A claim_admin_fees() call with xcp_profit <= xcp_profit_a (or
//      fees == 0) emits NO event yet still rewrites D and virtual_price
//      on-chain — invisible to a log-driven replay. Final-state compare
//      would catch it; none occurred in the validation window.
//   c. is_killed is permanently false (kill_deadline expired 2021), so the
//      assert-not-killed guards are omitted.
//
// ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md) — purely additive; with
// none of the new job fields set the event outputs and "final" are byte-for-
// byte what they were before.
//   job:  "probe_all" / "probe_last" / "cf" (bool), per-event "probe" (bool)
//         and, in cf mode, per-event "burn_frac" (1e18 fraction of the LIVE
//         totalSupply to burn) on "remove" / "remove_one".
//   out:  result["probes"] (only when a probe was requested) and
//         result["meter"] (always).
//  v2.1 "spot" is this vintage's OWN internal spot definition: the newton_y
//       probe tweak_price runs whenever the caller hands it p_i == 0 ("calc-
//       ulate real prices"), i.e. dx_price = xp[0]/1e6, then
//       price_scale[k] * dx_price / (xp[k+1] - newton_y(A, gamma, xp with
//       xp[0]+dx_price, D, k+1)) — evaluated against the CURRENT post-event
//       xp and self.D. There is no MATH.get_p in the 2021 math contract, so
//       this small fee-free numerical derivative IS the pool's marginal price
//       (and is the form the spec explicitly blesses). self.xp()'s PRECISIONS
//       leg folds the decimals in, so the value is the REAL, fee-free,
//       whole-token price of coin k+1 in coin-0 units, 1e18-scaled.
//  v2.2 "ps_gap_bp" (per j, in every probe) and meter "max_ps_gap_bp" (the run
//       maximum over j AND over every event, probe or not) are
//       |spot_j * 1e18 / price_scale_j - 1e18| in basis points. This is the
//       input to the project's price_scale-vs-spot freeze rule, so it is
//       tracked inline and never requires a probe.
//  v2.3 Meter fee accounting for this family:
//       - exchange: the fee is charged on the OUTPUT coin (dy is reduced) and
//         stays inside self.balances -> meter["fee"][j].
//       - remove_liquidity_one_coin: the fee is charged on D; the coin-i
//         equivalent N*D_fee*xx[i]/D (the quantity the later vintages log as
//         approx_fee; this one logs nothing) lands in meter["fee"][i].
//       - add_liquidity: the fee is an LP-TOKEN haircut (d_token_fee) with no
//         coin denomination -> meter["fee_lp"] (LP wei), not meter["fee"].
//       - the DAO slice is taken by MINTING LP to the fee receiver
//         (CurveToken.mint_relative), never in coin units, so meter["admin"]
//         is all zeros and meter["admin_lp"] accumulates the LP minted.
//  v2.4 probes omit "adm": this family has no admin_balances bucket.
//  v2.5 "rebase_mul" does not apply to this family and is ignored.
//  v2.6 cf "burn_frac" is taken against the totalSupply as it stands at the
//       burn — this vintage does NOT claim inside remove / remove_one, so that
//       is simply the live supply when the event is reached.
//  v2.7 every probe also carries the meter AS OF that event, under the same
//       units and conventions as v2.3: "cfee"[3], "cfee_lp", "cadm"[3] (all
//       zeros, mirroring meter["admin"]), "cadm_lp" and "cvol"[3]. The last
//       probe's values equal result["meter"] exactly. The meter lives inside
//       Pool, so a reverted event's state restore rolls it back and that
//       event's probe shows the not-counted (pre-event) totals.
// ============================================================================

#include "crypto_math.hpp"

#include <nlohmann/json.hpp>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>

namespace tc2 {

using u = boost::multiprecision::cpp_int;   // uint256 role
using json = nlohmann::json;

// ---- constants --------------------------------------------------------------
inline const u PRECISION = cm::E18();                 // 10**18
inline const u A_MULTIPLIER = 10000;
inline const u NOISE_FEE = 100000;                    // 10**5
inline const u PRICE_MASK = (u(1) << 128) - 1;        // 2**128 - 1
// math-contract bounds (N_COINS = 3)
inline const u MATH_MIN_A = 2700;                     // 27*10000/100
inline const u MATH_MAX_A = 270000000;                // 27*10000*1000
inline const u MATH_MIN_GAMMA = 10000000000;          // 10**10
inline const u MATH_MAX_GAMMA("50000000000000000");   // 5*10**16

// vyper checked subtraction: revert on underflow
inline u csub(const u& a, const u& b) {
    if (b > a) throw std::runtime_error("Integer underflow");
    return a - b;
}

// parse a job uint (decimal string or JSON number)
inline u ju(const json& v) {
    if (v.is_string()) return u(v.get<std::string>());
    if (v.is_number_unsigned()) return u(v.get<std::uint64_t>());
    if (v.is_number_integer()) {
        long long x = v.get<long long>();
        if (x < 0) throw std::runtime_error("negative uint");
        return u(x);
    }
    throw std::runtime_error("bad uint json");
}

inline std::string S(const u& v) { return v.str(); }

// _pack_prices per-element bound: assert p < PRICE_MASK
inline void check_packable(const u p[2]) {
    cm::req(p[0] < PRICE_MASK && p[1] < PRICE_MASK, "price pack overflow");
}

// ---- MATH contract (0x8F68f481...), exact ports -----------------------------

// insertion sort high-to-low (3 elements; result identical to any desc sort)
inline void sort_desc(u x[3]) {
    u t;
    if (x[0] < x[1]) { t = x[0]; x[0] = x[1]; x[1] = t; }
    if (x[0] < x[2]) { t = x[0]; x[0] = x[2]; x[2] = t; }
    if (x[1] < x[2]) { t = x[1]; x[1] = x[2]; x[2] = t; }
}

// geometric_mean(unsorted_x, sort): iterative Newton on the product
inline u geometric_mean(const u x_in[3], bool do_sort) {
    u x[3] = { x_in[0], x_in[1], x_in[2] };
    if (do_sort) sort_desc(x);
    u D = x[0];
    for (int it = 0; it < 255; ++it) {
        u D_prev = D;
        u tmp = PRECISION;
        for (int k = 0; k < 3; ++k) tmp = tmp * x[k] / D;   // div0 -> throw
        D = D * (2 * PRECISION + tmp) / (3 * PRECISION);
        u diff = (D > D_prev) ? u(D - D_prev) : u(D_prev - D);
        if (diff <= 1 || diff * PRECISION < D) return D;
    }
    throw std::runtime_error("Did not converge");
}

// reduction_coefficient(x, fee_gamma)
inline u reduction_coefficient(const u x[3], const u& fee_gamma) {
    u K = PRECISION;
    u Ssum = x[0] + x[1] + x[2];
    for (int k = 0; k < 3; ++k) K = K * 3 * x[k] / Ssum;
    if (fee_gamma > 0)
        K = fee_gamma * PRECISION / csub(fee_gamma + PRECISION, K);
    return K;
}

// newton_D(ANN, gamma, x_unsorted) — 3*geometric_mean seed, old asserts
inline u newton_D(const u& ANN, const u& gamma, const u x_unsorted[3]) {
    cm::req(ANN > MATH_MIN_A - 1 && ANN < MATH_MAX_A + 1, "unsafe values A");
    cm::req(gamma > MATH_MIN_GAMMA - 1 && gamma < MATH_MAX_GAMMA + 1,
            "unsafe values gamma");
    u x[3] = { x_unsorted[0], x_unsorted[1], x_unsorted[2] };
    sort_desc(x);
    cm::req(x[0] > cm::pow_int(10, 9) - 1 &&
            x[0] < cm::pow_int(10, 15) * PRECISION + 1, "unsafe values x[0]");
    for (int k = 1; k < 3; ++k)
        cm::req(x[k] * PRECISION / x[0] > cm::pow_int(10, 11) - 1,
                "unsafe values x[i]");

    u D = 3 * geometric_mean(x, false);
    u Ssum = x[0] + x[1] + x[2];

    for (int it = 0; it < 255; ++it) {
        u D_prev = D;
        u K0 = PRECISION;
        for (int k = 0; k < 3; ++k) K0 = K0 * x[k] * 3 / D;
        u g1k0 = gamma + PRECISION;
        g1k0 = (g1k0 > K0) ? u(g1k0 - K0 + 1) : u(K0 - g1k0 + 1);
        u mul1 = PRECISION * D / gamma * g1k0 / gamma * g1k0
                 * A_MULTIPLIER / ANN;
        u mul2 = (2 * PRECISION) * 3 * K0 / g1k0;
        u neg_fprime = csub(Ssum + Ssum * mul2 / PRECISION + mul1 * 3 / K0,
                            mul2 * D / PRECISION);
        u D_plus = D * (neg_fprime + Ssum) / neg_fprime;
        u D_minus = D * D / neg_fprime;
        if (PRECISION > K0)
            D_minus += D * (mul1 / neg_fprime) / PRECISION
                       * (PRECISION - K0) / K0;
        else
            D_minus = csub(D_minus, D * (mul1 / neg_fprime) / PRECISION
                                    * (K0 - PRECISION) / K0);
        if (D_plus > D_minus) D = D_plus - D_minus;
        else D = (D_minus - D_plus) / 2;
        u diff = (D > D_prev) ? u(D - D_prev) : u(D_prev - D);
        u lim = D; if (lim < cm::pow_int(10, 16)) lim = cm::pow_int(10, 16);
        if (diff * cm::pow_int(10, 14) < lim) {
            for (int k = 0; k < 3; ++k) {
                u frac = x[k] * PRECISION / D;
                cm::req(frac > cm::pow_int(10, 16) - 1 &&
                        frac < cm::pow_int(10, 20) + 1, "unsafe values x[i]");
            }
            return D;
        }
    }
    throw std::runtime_error("Did not converge");
}

// newton_y(ANN, gamma, x, D, i)
inline u newton_y(const u& ANN, const u& gamma, const u x[3], const u& D,
                  int i) {
    cm::req(ANN > MATH_MIN_A - 1 && ANN < MATH_MAX_A + 1, "unsafe values A");
    cm::req(gamma > MATH_MIN_GAMMA - 1 && gamma < MATH_MAX_GAMMA + 1,
            "unsafe values gamma");
    cm::req(D > cm::pow_int(10, 17) - 1 &&
            D < cm::pow_int(10, 15) * PRECISION + 1, "unsafe values D");
    for (int k = 0; k < 3; ++k) {
        if (k == i) continue;
        u frac = x[k] * PRECISION / D;
        cm::req(frac > cm::pow_int(10, 16) - 1 &&
                frac < cm::pow_int(10, 20) + 1, "unsafe values x[i]");
    }

    u y = D / 3;
    u K0_i = PRECISION;
    u S_i = 0;
    u xs[3] = { x[0], x[1], x[2] };
    xs[i] = 0;
    sort_desc(xs);
    u convergence_limit = xs[0] / cm::pow_int(10, 14);
    { u t = D / cm::pow_int(10, 14); if (t > convergence_limit) convergence_limit = t; }
    if (convergence_limit < 100) convergence_limit = 100;

    for (int j = 2; j <= 3; ++j) {          // small x first
        u _x = xs[3 - j];
        y = y * D / (_x * 3);
        S_i += _x;
    }
    for (int j = 0; j < 2; ++j)             // large x first
        K0_i = K0_i * xs[j] * 3 / D;

    for (int it = 0; it < 255; ++it) {
        u y_prev = y;
        u K0 = K0_i * y * 3 / D;
        u Ssum = S_i + y;
        u g1k0 = gamma + PRECISION;
        g1k0 = (g1k0 > K0) ? u(g1k0 - K0 + 1) : u(K0 - g1k0 + 1);
        u mul1 = PRECISION * D / gamma * g1k0 / gamma * g1k0
                 * A_MULTIPLIER / ANN;
        u mul2 = PRECISION + (2 * PRECISION) * K0 / g1k0;
        u yfprime = PRECISION * y + Ssum * mul2 + mul1;
        u dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        u fprime = yfprime / y;
        u y_minus = mul1 / fprime;
        u y_plus = (yfprime + PRECISION * D) / fprime
                   + y_minus * PRECISION / K0;
        y_minus += PRECISION * Ssum / fprime;
        if (y_plus < y_minus) y = y_prev / 2;
        else y = y_plus - y_minus;
        u diff = (y > y_prev) ? u(y - y_prev) : u(y_prev - y);
        u lim = y / cm::pow_int(10, 14);
        if (lim < convergence_limit) lim = convergence_limit;
        if (diff < lim) {
            u frac = y * PRECISION / D;
            cm::req(frac > cm::pow_int(10, 16) - 1 &&
                    frac < cm::pow_int(10, 20) + 1, "unsafe value for y");
            return y;
        }
    }
    throw std::runtime_error("Did not converge");
}

// halfpow(power, precision): 1e18 * 0.5**(power/1e18), Taylor series
inline u halfpow(const u& power, const u& precision) {
    u intpow = power / PRECISION;
    u otherpow = power - intpow * PRECISION;
    if (intpow > 59) return 0;
    u result = PRECISION / (u(1) << intpow.convert_to<unsigned>());
    if (otherpow == 0) return result;

    u term = PRECISION;
    u x = u("500000000000000000");          // 5 * 10**17
    u Ssum = PRECISION;
    bool neg = false;

    for (int i = 1; i < 256; ++i) {
        u K = u(i) * PRECISION;
        u c = K - PRECISION;
        if (otherpow > c) {
            c = otherpow - c;
            neg = !neg;
        } else {
            c = csub(c, otherpow);
        }
        term = term * (c * x / PRECISION) / K;
        if (neg) Ssum = csub(Ssum, term);
        else Ssum += term;
        if (term < precision) return result * Ssum / PRECISION;
    }
    throw std::runtime_error("Did not converge");
}

// sqrt_int(x): Babylonian for sqrt(x * 1e18); raises without exact landing
inline u sqrt_int(const u& x) {
    if (x == 0) return 0;
    u z = (x + PRECISION) / 2;
    u y = x;
    for (int i = 0; i < 256; ++i) {
        if (z == y) return y;
        y = z;
        z = (x * PRECISION / z + z) / 2;
    }
    throw std::runtime_error("Did not converge");
}

// ---- pool state -------------------------------------------------------------

// Engine-contract-v2 revenue meter. Lives INSIDE Pool on purpose: the driver
// snapshots/restores Pool around every event, so a reverted event accrues
// nothing. Never read by the consensus math.
struct Meters {
    u fee[3]{};        // gross fee charged, coin units (see header note v2.3)
    u fee_lp{0};       // gross add_liquidity d_token fee, LP-token units
    u admin_lp{0};     // LP minted to the fee receiver by _claim_admin_fees
    u vol[3]{};        // gross exchange input volume (dx), per coin
};

struct Pool {
    // params (unpacked A_gamma; job supplies from packed storage reads)
    u initial_A = 0, initial_gamma = 0;
    u future_A = 0, future_gamma = 0;
    u initial_A_gamma_time = 0, future_A_gamma_time = 0;   // 0/1 flag values!
    u mid_fee = 0, out_fee = 0, admin_fee = 0;
    u fee_gamma = 0, allowed_extra_profit = 0, adjustment_step = 0;
    u ma_half_time = 0;                                    // HALF-LIFE seconds
    u prec[3];                                             // 10**(18-dec)

    // state
    u bal[3];                                              // raw token units
    u D = 0;
    u price_scale[2], price_oracle[2], last_prices[2];     // unpacked, k=0/k=1
    u last_prices_timestamp = 0;
    u virtual_price = 0, xcp_profit = 0, xcp_profit_a = 0;
    u total_supply = 0;                                    // external LP token
    bool not_adjusted = false;                             // slot 29

    u ts = 0;                                              // block.timestamp

    Meters m;                                              // contract-v2 meter

    // ---- _A_gamma() ---------------------------------------------------------
    void A_gamma(u out[2]) const {
        u t1 = future_A_gamma_time;
        u A1 = future_A, g1 = future_gamma;
        if (ts < t1) {
            u t0 = initial_A_gamma_time;
            t1 = csub(t1, t0);
            t0 = csub(ts, t0);
            u t2 = csub(t1, t0);
            A1 = (initial_A * t2 + A1 * t0) / t1;
            g1 = (initial_gamma * t2 + g1 * t0) / t1;
        }
        out[0] = A1;
        out[1] = g1;
    }

    // ---- self.xp() ----------------------------------------------------------
    void xp_now(u out[3]) const {
        out[0] = bal[0] * prec[0];
        out[1] = bal[1] * price_scale[0] * prec[1] / PRECISION;
        out[2] = bal[2] * price_scale[1] * prec[2] / PRECISION;
    }

    // ---- self._fee(xp) ------------------------------------------------------
    u fee(const u xp[3]) const {
        u f = reduction_coefficient(xp, fee_gamma);
        return (mid_fee * f + out_fee * csub(PRECISION, f)) / PRECISION;
    }

    // ---- self.get_xcp(D) ----------------------------------------------------
    u get_xcp(const u& D_in) const {
        u x[3];
        x[0] = D_in / 3;
        x[1] = D_in * PRECISION / (3 * price_scale[0]);
        x[2] = D_in * PRECISION / (3 * price_scale[1]);
        return geometric_mean(x, true);
    }

    // ---- self._calc_token_fee(amounts, xp) ----------------------------------
    u calc_token_fee(const u amounts[3], const u xp[3]) const {
        u f = fee(xp) * 3 / 8;                  // fee * N / (4 * (N-1))
        u Ssum = amounts[0] + amounts[1] + amounts[2];
        u avg = Ssum / 3;
        u Sdiff = 0;
        for (int k = 0; k < 3; ++k)
            Sdiff += (amounts[k] > avg) ? u(amounts[k] - avg)
                                        : u(avg - amounts[k]);
        return f * Sdiff / Ssum + NOISE_FEE;    // div0 -> revert, as chain
    }

    // ---- self.tweak_price(A_gamma, _xp, i, p_i, new_D) ----------------------
    void tweak_price(const u A_g[2], const u _xp[3], const u& ix,
                     const u& p_i, const u& new_D) {
        u po[2] = { price_oracle[0], price_oracle[1] };
        u lp[2] = { last_prices[0], last_prices[1] };

        // ---- MA update (once per block) ----
        if (last_prices_timestamp < ts) {
            u alpha = halfpow(csub(ts, last_prices_timestamp) * PRECISION
                              / ma_half_time, cm::pow_int(10, 10));
            for (int k = 0; k < 2; ++k)
                po[k] = (lp[k] * csub(PRECISION, alpha) + po[k] * alpha)
                        / PRECISION;
            check_packable(po);                 // _pack assert
            price_oracle[0] = po[0];
            price_oracle[1] = po[1];
            last_prices_timestamp = ts;
        }

        // ---- D_unadjusted ----
        u D_unadjusted = new_D;
        if (new_D == 0)
            D_unadjusted = newton_D(A_g[0], A_g[1], _xp);
        u ps[2] = { price_scale[0], price_scale[1] };

        // ---- last_prices from spot price p_i (or newton_y probe) ----
        if (p_i > 0) {
            if (ix > 0) {
                lp[ix.convert_to<int>() - 1] = p_i;
            } else {
                for (int k = 0; k < 2; ++k)
                    lp[k] = lp[k] * PRECISION / p_i;
            }
        } else {
            u __xp[3] = { _xp[0], _xp[1], _xp[2] };
            u dx_price = __xp[0] / cm::pow_int(10, 6);
            __xp[0] += dx_price;
            for (int k = 0; k < 2; ++k)
                lp[k] = ps[k] * dx_price
                        / csub(_xp[k + 1],
                               newton_y(A_g[0], A_g[1], __xp,
                                        D_unadjusted, k + 1));
        }
        check_packable(lp);                     // _pack assert
        last_prices[0] = lp[0];
        last_prices[1] = lp[1];

        u total_supply_l = total_supply;        // CurveToken.totalSupply()
        u old_xcp_profit = xcp_profit;
        u old_virtual_price = virtual_price;

        // ---- profit numbers without price adjustment ----
        u xp_eq[3];
        xp_eq[0] = D_unadjusted / 3;
        xp_eq[1] = D_unadjusted * PRECISION / (3 * ps[0]);
        xp_eq[2] = D_unadjusted * PRECISION / (3 * ps[1]);

        u xcp_profit_l = PRECISION;
        u virtual_price_l = PRECISION;
        if (old_virtual_price > 0) {
            u xcp = geometric_mean(xp_eq, true);
            virtual_price_l = PRECISION * xcp / total_supply_l;
            xcp_profit_l = old_xcp_profit * virtual_price_l
                           / old_virtual_price;
            u t = future_A_gamma_time;
            if (virtual_price_l < old_virtual_price && t == 0)
                throw std::runtime_error("Loss");
            if (t == 1) future_A_gamma_time = 0;
        }
        xcp_profit = xcp_profit_l;

        // ---- repeg gate (vyper `and` evaluates both sides: csub first) ----
        bool needs_adjustment = not_adjusted;
        u vp2m1 = csub(virtual_price_l * 2, PRECISION);
        if (!needs_adjustment &&
            vp2m1 > xcp_profit_l + 2 * allowed_extra_profit) {
            needs_adjustment = true;
            not_adjusted = true;
        }

        if (needs_adjustment) {
            u step = adjustment_step;
            u norm = 0;
            for (int k = 0; k < 2; ++k) {
                u ratio = po[k] * PRECISION / ps[k];
                ratio = (ratio > PRECISION) ? u(ratio - PRECISION)
                                            : u(PRECISION - ratio);
                norm += ratio * ratio;
            }
            if (norm > step * step && old_virtual_price > 0) {
                norm = sqrt_int(norm / PRECISION);   // to 1e18 units

                u p_new[2];
                for (int k = 0; k < 2; ++k)
                    p_new[k] = (ps[k] * csub(norm, step) + step * po[k])
                               / norm;

                // xp at new prices
                u xp2[3] = { _xp[0],
                             _xp[1] * p_new[0] / ps[0],
                             _xp[2] * p_new[1] / ps[1] };

                u D_new = newton_D(A_g[0], A_g[1], xp2);
                xp2[0] = D_new / 3;
                xp2[1] = D_new * PRECISION / (3 * p_new[0]);
                xp2[2] = D_new * PRECISION / (3 * p_new[1]);
                u vp_new = PRECISION * geometric_mean(xp2, true)
                           / total_supply_l;

                // non-short-circuit `and`: evaluate 2*vp - 1e18 regardless
                u vpn2m1 = csub(2 * vp_new, PRECISION);
                if (vp_new > PRECISION && vpn2m1 > xcp_profit_l) {
                    check_packable(p_new);      // _pack assert
                    price_scale[0] = p_new[0];
                    price_scale[1] = p_new[1];
                    D = D_new;
                    virtual_price = vp_new;
                    return;
                } else {
                    not_adjusted = false;
                }
            }
        }

        // no adjustment: commit unadjusted values
        D = D_unadjusted;
        virtual_price = virtual_price_l;
    }

    // ---- self._claim_admin_fees() -------------------------------------------
    // Returns (claimed, event_logged). NO early return in this vintage: D and
    // virtual_price are re-derived on every call. Gulp is a no-op (header a.).
    std::pair<u, bool> claim_admin_fees() {
        u A_g[2];
        A_gamma(A_g);

        u xcp = xcp_profit;
        u xcp_a = xcp_profit_a;

        // gulp: self.balances[i] = ERC20(coins[i]).balanceOf(self) — no-op.

        u vprice = virtual_price;
        u claimed = 0;
        bool logged = false;

        if (xcp > xcp_a) {
            u fees = csub(xcp, xcp_a) * admin_fee / (2 * cm::pow_int(10, 10));
            if (fees > 0) {
                u frac = csub(vprice * PRECISION / csub(vprice, fees),
                              PRECISION);
                // CurveToken.mint_relative(receiver, frac):
                u d_supply = total_supply * frac / PRECISION;
                if (d_supply > 0) {
                    total_supply += d_supply;
                    m.admin_lp += d_supply;                 // meter only
                }
                claimed = d_supply;
                logged = true;                  // ClaimAdminFee always logged
                xcp = csub(xcp, fees * 2);
                xcp_profit = xcp;
            }
        }

        u total_supply_l = total_supply;        // post-mint

        // recalculate D b/c we gulped
        u xp_c[3];
        xp_now(xp_c);
        D = newton_D(A_g[0], A_g[1], xp_c);
        virtual_price = PRECISION * get_xcp(D) / total_supply_l;

        if (xcp > xcp_a) xcp_profit_a = xcp;
        return { claimed, logged };
    }
};

// ---- engine contract v2: spot price, ps gap, probes -------------------------

inline json ps_json(const Pool& p) {
    return json::array({S(p.price_scale[0]), S(p.price_scale[1])});
}

// REAL, fee-free marginal price of coin j (j = 1, 2) in coin-0 units,
// 1e18-scaled. This vintage has no MATH.get_p; its own internal spot
// definition is the newton_y probe tweak_price runs on the p_i == 0 path
// ("calculate real prices"): bump xp[0] by xp[0]/1e6 and measure how far
// newton_y moves xp[j], then multiply by price_scale[j-1] to undo the scaling.
// self.xp()'s PRECISIONS leg folds the decimals in, so the result is the
// whole-token price. Reproduced here verbatim against the CURRENT (post-event)
// xp and self.D. Degenerate states (non-convergence, frac asserts, a zero
// denominator on an emptied pool) yield zeros rather than throwing.
inline void spot_of(const Pool& p, u out[2]) {
    out[0] = 0;
    out[1] = 0;
    try {
        u A_g[2];
        p.A_gamma(A_g);
        u xp[3];
        p.xp_now(xp);
        if (xp[0] == 0 || xp[1] == 0 || xp[2] == 0 || p.D == 0) return;
        u dx_price = xp[0] / cm::pow_int(10, 6);
        if (dx_price == 0) return;
        u __xp[3] = { xp[0] + dx_price, xp[1], xp[2] };
        u got[2];
        for (int k = 0; k < 2; ++k) {
            u y = newton_y(A_g[0], A_g[1], __xp, p.D, k + 1);
            if (y >= xp[k + 1]) return;             // no measurable move
            got[k] = p.price_scale[k] * dx_price / (xp[k + 1] - y);
        }
        out[0] = got[0];
        out[1] = got[1];
    } catch (const std::exception&) {
        out[0] = 0;
        out[1] = 0;
    }
}

// |spot * 1e18 / price_scale - 1e18| expressed in basis points
inline u gap_bp_one(const u& spot, const u& ps) {
    if (ps == 0 || spot == 0) return 0;
    u r = spot * PRECISION / ps;
    u d = (r > PRECISION) ? u(r - PRECISION) : u(PRECISION - r);
    return d * 10000 / PRECISION;
}

inline u max_gap_bp(const Pool& p, const u sp[2]) {
    u g0 = gap_bp_one(sp[0], p.price_scale[0]);
    u g1 = gap_bp_one(sp[1], p.price_scale[1]);
    return g0 > g1 ? g0 : g1;
}

// get_virtual_price(): 1e18 * get_xcp(D) / totalSupply, recomputed live from
// the post-event state (self.virtual_price is stale after remove_liquidity).
inline u live_vp(const Pool& p) {
    if (p.total_supply == 0 || p.price_scale[0] == 0 || p.price_scale[1] == 0)
        return 0;
    try {
        return PRECISION * p.get_xcp(p.D) / p.total_supply;
    } catch (const std::exception&) {
        return 0;
    }
}

// probe object; "adm" is omitted — this family has no admin_balances bucket
// (the DAO slice is minted as LP, see meter["admin_lp"]).
inline json make_probe(const Pool& p, const u sp[2], int idx) {
    return json{
        {"i", idx},
        {"bal", json::array({S(p.bal[0]), S(p.bal[1]), S(p.bal[2])})},
        {"sup", S(p.total_supply)},
        {"D", S(p.D)},
        {"vp", S(live_vp(p))},
        {"xcp", S(p.xcp_profit)},
        {"ps", ps_json(p)},
        {"spot", json::array({S(sp[0]), S(sp[1])})},
        {"ps_gap_bp", json::array({
            gap_bp_one(sp[0], p.price_scale[0]).convert_to<long long>(),
            gap_bp_one(sp[1], p.price_scale[1]).convert_to<long long>()})},
        // cumulative meter as of this event — mirrors result["meter"]'s
        // fee / fee_lp / admin / admin_lp / vol exactly (last probe == totals).
        // "cadm" is all zeros for the same reason "admin" is: the DAO slice is
        // minted as LP, which "cadm_lp" carries.
        {"cfee", json::array({S(p.m.fee[0]), S(p.m.fee[1]), S(p.m.fee[2])})},
        {"cfee_lp", S(p.m.fee_lp)},
        {"cadm", json::array({"0", "0", "0"})},
        {"cadm_lp", S(p.m.admin_lp)},
        {"cvol", json::array({S(p.m.vol[0]), S(p.m.vol[1]), S(p.m.vol[2])})}};
}

// ---- event appliers (throw == revert; runner restores state) ----------------

// exchange(i, j, dx, ...) — transfer legs resolved by the feeder
inline json apply_exchange(Pool& p, int i, int j, const u& dx) {
    cm::req(i != j, "same coin");
    cm::req(i >= 0 && i < 3 && j >= 0 && j < 3, "coin index out of range");
    cm::req(dx > 0, "do not exchange 0 coins");

    u A_g[2];
    p.A_gamma(A_g);

    u xp[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u ix = j;
    u pp = 0;

    u y = xp[j];
    u x0 = xp[i];
    xp[i] = x0 + dx;
    p.bal[i] = xp[i];

    u ps[2] = { p.price_scale[0], p.price_scale[1] };

    xp[0] *= p.prec[0];
    for (int k = 1; k < 3; ++k)
        xp[k] = xp[k] * ps[k - 1] * p.prec[k] / PRECISION;

    u prec_i = p.prec[i];

    // in case ramp is happening (t is also the 0/1 flag)
    {
        u t = p.future_A_gamma_time;
        if (t > 0) {
            x0 *= prec_i;
            if (i > 0) x0 = x0 * ps[i - 1] / PRECISION;
            u x1 = xp[i];
            xp[i] = x0;
            p.D = newton_D(A_g[0], A_g[1], xp);
            xp[i] = x1;
            if (p.ts >= t) p.future_A_gamma_time = 1;
        }
    }

    u prec_j = p.prec[j];

    u dy = csub(xp[j], newton_y(A_g[0], A_g[1], xp, p.D, j));
    xp[j] = csub(xp[j], dy);
    dy = csub(dy, 1);

    if (j > 0) dy = dy * PRECISION / ps[j - 1];
    dy /= prec_j;

    u fee_amt = p.fee(xp) * dy / cm::pow_int(10, 10);
    dy = csub(dy, fee_amt);
    y = csub(y, dy);

    p.bal[j] = y;                  // fee remains inside the pool balance

    y *= prec_j;
    if (j > 0) y = y * ps[j - 1] / PRECISION;
    xp[j] = y;

    // spot price for tweak_price
    if (dx > cm::pow_int(10, 5) && dy > cm::pow_int(10, 5)) {
        u _dx = dx * prec_i;
        u _dy = dy * prec_j;
        if (i != 0 && j != 0)
            pp = p.last_prices[i - 1] * _dx / _dy;
        else if (i == 0)
            pp = _dx * PRECISION / _dy;
        else {                     // j == 0
            pp = _dy * PRECISION / _dx;
            ix = i;
        }
    }

    p.tweak_price(A_g, xp, ix, pp, 0);

    p.m.vol[i] += dx;              // meter only
    p.m.fee[j] += fee_amt;         // meter only (fee lands on coin j)

    return json{{"dy", S(dy)}};
}

// add_liquidity(amounts, ...)
inline json apply_add(Pool& p, const u amounts[3]) {
    u A_g[2];
    p.A_gamma(A_g);

    u xp[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u amountsp[3] = { 0, 0, 0 };
    u xx[3];
    u d_token = 0, d_token_fee = 0, old_D = 0;
    const int INF_COINS = 15;
    int ix = INF_COINS;

    u xp_old[3] = { xp[0], xp[1], xp[2] };

    for (int k = 0; k < 3; ++k) {
        u b = xp[k] + amounts[k];
        xp[k] = b;
        p.bal[k] = b;
        xx[k] = b;
    }

    u ps[2] = { p.price_scale[0], p.price_scale[1] };
    xp[0] *= p.prec[0];
    xp_old[0] *= p.prec[0];
    for (int k = 1; k < 3; ++k) {
        u price_scale_k = ps[k - 1] * p.prec[k];
        xp[k] = xp[k] * price_scale_k / PRECISION;
        xp_old[k] = xp_old[k] * price_scale_k / PRECISION;
    }

    for (int k = 0; k < 3; ++k)
        if (amounts[k] > 0) {
            amountsp[k] = csub(xp[k], xp_old[k]);
            if (ix == INF_COINS) ix = k;
            else ix = INF_COINS - 1;
        }
    cm::req(ix != INF_COINS, "no coins to add");

    {
        u t = p.future_A_gamma_time;
        if (t > 0) {
            old_D = newton_D(A_g[0], A_g[1], xp_old);
            if (p.ts >= t) p.future_A_gamma_time = 1;
        } else {
            old_D = p.D;
        }
    }

    u Dv = newton_D(A_g[0], A_g[1], xp);

    u token_supply = p.total_supply;
    if (old_D > 0)
        d_token = csub(token_supply * Dv / old_D, token_supply);
    else
        d_token = p.get_xcp(Dv);
    cm::req(d_token > 0, "nothing minted");

    if (old_D > 0) {
        d_token_fee = p.calc_token_fee(amountsp, xp) * d_token
                      / cm::pow_int(10, 10) + 1;
        d_token = csub(d_token, d_token_fee);
        token_supply += d_token;
        p.total_supply += d_token;             // CurveToken.mint
        p.m.fee_lp += d_token_fee;             // meter only

        // spot price of the single deposited coin (0 for multi-coin adds)
        u pp = 0;
        if (d_token > cm::pow_int(10, 5) && ix < 3) {
            u Ssum = 0;
            for (int k = 0; k < 3; ++k) {
                if (k == ix) continue;
                if (k == 0)
                    Ssum += xx[0] * p.prec[0];
                else
                    Ssum += xx[k] * p.last_prices[k - 1] * p.prec[k]
                            / PRECISION;
            }
            Ssum = Ssum * d_token / token_supply;
            pp = Ssum * PRECISION
                 / csub(amounts[ix] * p.prec[ix],
                        d_token * xx[ix] * p.prec[ix] / token_supply);
        }

        p.tweak_price(A_g, xp, u(ix), pp, Dv);
    } else {
        // unreachable for an initialized pool; token_supply (logged) stays
        // the PRE-mint totalSupply() read, exactly as on-chain
        p.D = Dv;
        p.virtual_price = PRECISION;
        p.xcp_profit = PRECISION;
        p.total_supply += d_token;
    }

    return json{{"minted", S(d_token)}, {"fee", S(d_token_fee)},
                {"supply", S(token_supply)}};
}

// remove_liquidity(_amount, ...) — supply_after == the event's token_supply.
// burn_frac (engine contract v2 cf mode, 1e18-scaled) replaces the historical
// supply_after: the burn becomes that fraction of the live totalSupply (this
// vintage does not claim inside remove). nullptr == today's behaviour.
inline json apply_remove(Pool& p, const u& supply_after,
                         const u* burn_frac = nullptr) {
    u total_supply = p.total_supply;           // pre-burn
    u _amount = burn_frac ? u(total_supply * (*burn_frac) / PRECISION)
                          : csub(total_supply, supply_after);
    cm::req(_amount <= total_supply, "burn exceeds supply");
    p.total_supply = csub(p.total_supply, _amount);    // burnFrom
    u balances0[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u amount = csub(_amount, 1);               // favor other LPs (always)

    u d_balances[3];
    for (int k = 0; k < 3; ++k) {
        d_balances[k] = balances0[k] * amount / total_supply;
        p.bal[k] = csub(balances0[k], d_balances[k]);
    }

    u Dv = p.D;
    p.D = csub(Dv, Dv * amount / total_supply);

    json amts = json::array({S(d_balances[0]), S(d_balances[1]),
                             S(d_balances[2])});
    return json{{"amounts", amts}, {"supply", S(csub(total_supply, _amount))}};
}

// remove_liquidity_one_coin(token_amount, i, ...). burn_frac (engine contract
// v2 cf mode, 1e18-scaled) replaces token_amount with that fraction of the
// live totalSupply; nullptr == today's behaviour.
inline json apply_remove_one(Pool& p, const u& token_amount_in, int i,
                             const u* burn_frac = nullptr) {
    u A_g[2];
    p.A_gamma(A_g);

    u fagt = p.future_A_gamma_time;
    bool update_D = fagt > 0;

    // ---- _calc_withdraw_one_coin(A_gamma, token_amount, i, update_D, True) --
    u token_supply = p.total_supply;
    const u token_amount = burn_frac
                               ? u(token_supply * (*burn_frac) / PRECISION)
                               : token_amount_in;
    cm::req(token_amount <= token_supply, "token amount more than supply");
    cm::req(i >= 0 && i < 3, "coin out of range");

    u xx[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u xp[3] = { p.prec[0], p.prec[1], p.prec[2] };

    u price_scale_i = PRECISION * p.prec[0];
    xp[0] *= xx[0];
    for (int k = 1; k < 3; ++k) {
        u pk = p.price_scale[k - 1];
        if (i == k) price_scale_i = pk * xp[i];    // xp[i] still == prec[i]
        xp[k] = xp[k] * xx[k] * pk / PRECISION;
    }

    u D0 = update_D ? newton_D(A_g[0], A_g[1], xp) : p.D;
    u Dv = D0;

    // fee on D (charged on the CURRENT xp in this vintage)
    u fee_rate = p.fee(xp);
    u dD = token_amount * Dv / token_supply;
    u D_fee = fee_rate * dD / (2 * cm::pow_int(10, 10)) + 1;
    // coin-i-denominated withdrawal fee (this vintage does not log it; the
    // formula is the one the later vintages log as approx_fee). Meter only.
    p.m.fee[i] += 3 * D_fee * xx[i] / Dv;
    Dv = csub(Dv, csub(dD, D_fee));
    u y = newton_y(A_g[0], A_g[1], xp, Dv, i);
    u dy = csub(xp[i], y) * PRECISION / price_scale_i;
    xp[i] = y;

    // spot price (calc_price=True path)
    u pp = 0;
    if (dy > cm::pow_int(10, 5) && token_amount > cm::pow_int(10, 5)) {
        u Ssum = 0;
        for (int k = 0; k < 3; ++k) {
            if (k == i) continue;
            if (k == 0)
                Ssum += xx[0] * p.prec[0];
            else
                Ssum += xx[k] * p.last_prices[k - 1] * p.prec[k] / PRECISION;
        }
        Ssum = Ssum * dD / D0;
        pp = Ssum * PRECISION
             / csub(dy * p.prec[i], dD * xx[i] * p.prec[i] / D0);
    }
    // ---- end _calc_withdraw_one_coin ----

    if (p.ts >= fagt) p.future_A_gamma_time = 1;   // ALWAYS at rest (fagt<=1)

    p.bal[i] = csub(p.bal[i], dy);
    p.total_supply = csub(p.total_supply, token_amount);   // burnFrom
    p.tweak_price(A_g, xp, u(i), pp, Dv);

    return json{{"dy", S(dy)}, {"supply", S(p.total_supply)}};
}

// standalone claim_admin_fees()
inline json apply_claim(Pool& p) {
    auto r = p.claim_admin_fees();
    return json{{"claimed", S(r.first)}, {"minted_branch", r.second},
                {"supply", S(p.total_supply)}};
}

} // namespace tc2

// ---- entry point ------------------------------------------------------------

inline nlohmann::json run_tricrypto2(const nlohmann::json& job) {
    using tc2::u;
    using tc2::ju;
    using tc2::S;
    using json = nlohmann::json;

    cm::req(job.at("n").get<int>() == 3, "tricrypto2 needs n == 3");

    tc2::Pool p;

    const json& decs = job.at("decimals");
    for (int k = 0; k < 3; ++k) {
        int d = decs.at(static_cast<size_t>(k)).get<int>();
        cm::req(d >= 0 && d <= 18, "bad decimals");
        p.prec[k] = cm::pow_int(10, 18 - d);
    }

    const json& prm = job.at("params");
    p.initial_A = ju(prm.at("initial_A"));
    p.initial_gamma = ju(prm.at("initial_gamma"));
    p.future_A = ju(prm.at("future_A"));
    p.future_gamma = ju(prm.at("future_gamma"));
    p.initial_A_gamma_time = ju(prm.at("initial_A_gamma_time"));
    p.future_A_gamma_time = ju(prm.at("future_A_gamma_time"));
    p.mid_fee = ju(prm.at("mid_fee"));
    p.out_fee = ju(prm.at("out_fee"));
    p.admin_fee = ju(prm.at("admin_fee"));
    p.fee_gamma = ju(prm.at("fee_gamma"));
    p.allowed_extra_profit = ju(prm.at("allowed_extra_profit"));
    p.adjustment_step = ju(prm.at("adjustment_step"));
    p.ma_half_time = ju(prm.at("ma_half_time"));

    const json& st = job.at("state");
    for (int k = 0; k < 3; ++k)
        p.bal[k] = ju(st.at("balances").at(static_cast<size_t>(k)));
    for (int k = 0; k < 2; ++k) {
        p.price_scale[k] = ju(st.at("price_scale").at(static_cast<size_t>(k)));
        p.price_oracle[k] = ju(st.at("price_oracle").at(static_cast<size_t>(k)));
        p.last_prices[k] = ju(st.at("last_prices").at(static_cast<size_t>(k)));
    }
    p.last_prices_timestamp = ju(st.at("last_prices_timestamp"));
    p.D = ju(st.at("D"));
    p.virtual_price = ju(st.at("virtual_price"));
    p.xcp_profit = ju(st.at("xcp_profit"));
    p.xcp_profit_a = ju(st.at("xcp_profit_a"));
    p.total_supply = ju(st.at("total_supply"));
    p.not_adjusted = st.at("not_adjusted").get<bool>();

    // ---- engine contract v2 job flags (all default OFF) --------------------
    auto jbool = [&](const char* k) {
        return job.contains(k) && job.at(k).is_boolean() &&
               job.at(k).get<bool>();
    };
    const bool probe_all = jbool("probe_all");
    const bool probe_last = jbool("probe_last");
    const bool cf_mode = jbool("cf");

    json out_events = json::array();
    json probes = json::array();
    bool any_probe = false;
    long long n_events = 0, n_reverts = 0;
    u max_ps_gap = 0;

    const auto& events = job.at("events");
    const std::size_t n_ev_total = events.size();
    std::size_t ev_idx = 0;

    for (const auto& ev : events) {
        const std::string type = ev.at("type").get<std::string>();
        if (ev.contains("ts")) p.ts = ju(ev.at("ts"));

        // cf mode: burn amounts become fractions of the LIVE total supply (the
        // historical supply_after / burn describes a state path that no longer
        // exists). Outside cf mode burn_frac is ignored entirely.
        const bool has_bf = cf_mode && ev.contains("burn_frac");
        const u burn_frac_val = has_bf ? ju(ev.at("burn_frac")) : u(0);

        tc2::Pool snapshot = p;    // restored on revert
        bool reverted = false, skipped = false;

        try {
            json outputs;

            if (type == "exchange") {
                outputs = tc2::apply_exchange(p,
                                              ev.at("sold_id").get<int>(),
                                              ev.at("bought_id").get<int>(),
                                              ju(ev.at("dx")));
            } else if (type == "add") {
                u amts[3];
                for (int k = 0; k < 3; ++k)
                    amts[k] = ju(ev.at("amounts").at(static_cast<size_t>(k)));
                outputs = tc2::apply_add(p, amts);
            } else if (type == "remove") {
                outputs = tc2::apply_remove(
                    p, has_bf ? u(0) : ju(ev.at("supply_after")),
                    has_bf ? &burn_frac_val : nullptr);
            } else if (type == "remove_one") {
                outputs = tc2::apply_remove_one(
                    p, has_bf ? u(0) : ju(ev.at("burn")),
                    ev.at("i").get<int>(),
                    has_bf ? &burn_frac_val : nullptr);
            } else if (type == "claim_admin") {
                outputs = tc2::apply_claim(p);
            } else if (type == "ramp_ag") {
                // RampAgamma logs exactly what ramp_A_gamma stores.
                p.initial_A = ju(ev.at("initial_A"));
                p.future_A = ju(ev.at("future_A"));
                p.initial_gamma = ju(ev.at("initial_gamma"));
                p.future_gamma = ju(ev.at("future_gamma"));
                p.initial_A_gamma_time = ju(ev.at("initial_time"));
                p.future_A_gamma_time = ju(ev.at("future_time"));
                outputs = json::object();
            } else if (type == "stop_ramp_ag") {
                // StopRampA(current_A, current_gamma, time)
                u A = ju(ev.at("A")), g = ju(ev.at("gamma"));
                p.initial_A = A;
                p.future_A = A;
                p.initial_gamma = g;
                p.future_gamma = g;
                p.initial_A_gamma_time = p.ts;
                p.future_A_gamma_time = p.ts;
                outputs = json::object();
            } else if (type == "new_params") {
                // apply_new_parameters: claim (with the OLD admin_fee) first
                // iff admin_fee changes, then store all 7 logged values.
                u claimed = 0;
                bool claimed_ran = false;
                u new_admin_fee = ju(ev.at("admin_fee"));
                if (new_admin_fee != p.admin_fee) {
                    claimed = p.claim_admin_fees().first;
                    claimed_ran = true;
                }
                p.admin_fee = new_admin_fee;
                p.mid_fee = ju(ev.at("mid_fee"));
                p.out_fee = ju(ev.at("out_fee"));
                p.fee_gamma = ju(ev.at("fee_gamma"));
                p.allowed_extra_profit = ju(ev.at("allowed_extra_profit"));
                p.adjustment_step = ju(ev.at("adjustment_step"));
                p.ma_half_time = ju(ev.at("ma_half_time"));
                outputs = claimed_ran ? json{{"claimed", S(claimed)}}
                                      : json::object();
            } else {
                out_events.push_back(json{{"type", type}, {"skipped", true}});
                skipped = true;
            }

            if (!skipped)
                out_events.push_back(json{{"type", type},
                                          {"outputs", outputs}});
        } catch (const std::exception& e) {
            p = std::move(snapshot);   // restore pre-event state
            reverted = true;
            out_events.push_back(json{{"type", type},
                                      {"revert", std::string(e.what())}});
        }

        // ---- engine contract v2 bookkeeping (never touches pool state) ----
        ++n_events;
        if (reverted) ++n_reverts;
        u sp[2];
        tc2::spot_of(p, sp);
        {
            u gap = tc2::max_gap_bp(p, sp);
            if (gap > max_ps_gap) max_ps_gap = gap;
        }
        bool want_probe =
            probe_all || (probe_last && ev_idx + 1 == n_ev_total) ||
            (ev.contains("probe") && ev.at("probe").is_boolean() &&
             ev.at("probe").get<bool>());
        if (want_probe) {
            any_probe = true;
            probes.push_back(tc2::make_probe(p, sp,
                                             static_cast<int>(ev_idx)));
        }
        ++ev_idx;
    }

    json result;
    result["events"] = out_events;
    result["final"] = json{
        {"balances", json::array({S(p.bal[0]), S(p.bal[1]), S(p.bal[2])})},
        {"D", S(p.D)},
        {"price_scale", json::array({S(p.price_scale[0]), S(p.price_scale[1])})},
        {"price_oracle", json::array({S(p.price_oracle[0]), S(p.price_oracle[1])})},
        {"last_prices", json::array({S(p.last_prices[0]), S(p.last_prices[1])})},
        {"last_prices_timestamp", S(p.last_prices_timestamp)},
        {"virtual_price", S(p.virtual_price)},
        {"xcp_profit", S(p.xcp_profit)},
        {"xcp_profit_a", S(p.xcp_profit_a)},
        {"total_supply", S(p.total_supply)},
        {"not_adjusted", p.not_adjusted},
        {"initial_A", S(p.initial_A)},
        {"initial_gamma", S(p.initial_gamma)},
        {"future_A", S(p.future_A)},
        {"future_gamma", S(p.future_gamma)},
        {"initial_A_gamma_time", S(p.initial_A_gamma_time)},
        {"future_A_gamma_time", S(p.future_A_gamma_time)},
        {"admin_fee", S(p.admin_fee)},
        {"mid_fee", S(p.mid_fee)},
        {"out_fee", S(p.out_fee)}};

    // ---- engine contract v2 result additions -------------------------------
    // The DAO slice is taken by MINTING LP to the fee receiver (CurveToken
    // mint_relative), never in coin units, so "admin" is all zeros and
    // "admin_lp" carries it. "fee" is the gross coin-denominated fee (swap fee
    // on the output coin + the remove_one withdrawal fee); add_liquidity's fee
    // is an LP-token haircut with no coin denomination -> "fee_lp".
    result["meter"] = json{
        {"fee", json::array({S(p.m.fee[0]), S(p.m.fee[1]), S(p.m.fee[2])})},
        {"fee_lp", S(p.m.fee_lp)},
        {"admin", json::array({"0", "0", "0"})},
        {"admin_lp", S(p.m.admin_lp)},
        {"vol", json::array({S(p.m.vol[0]), S(p.m.vol[1]), S(p.m.vol[2])})},
        {"n_events", n_events},
        {"n_reverts", n_reverts},
        {"max_ps_gap_bp", max_ps_gap.convert_to<long long>()}};
    if (any_probe) result["probes"] = probes;
    return result;
}
