#pragma once
// ============================================================================
// crypto2_classic.hpp — wei-exact replay state machine for the CLASSIC
// handwritten Curve CryptoSwap 2-coin vintages (2021 family).
//
// Covered deployments (all mainnet, sources fetched from Blockscout
// eth.blockscout.com getsourcecode on 2026-08-29 and ported line-by-line):
//
//   flavor "eursusd"     0x98a7f18d4e56cfe84e3d081b40001b3d5bd3eb8b
//     "Curve EURS-USDC" (eursusd vintage), vyper 0.3.0. coins =
//     [USDC (6 dec), EURS (2 dec)] -> PRECISIONS [1e12, 1e16]. Standalone
//     contract, coins/token compile-time constants, LP token external
//     (0x3D229E1B4faab62F621eF2F6A610961f7BD7b23B). EARLY tweak_price:
//     rebalance trigger has NO norm gate, adjustment_step is the plain
//     stored value (no norm/K floor), and there is NO trailing
//     _claim_admin_fees when a pending adjustment finds norm <= step
//     (not_adjusted simply stays set). price_oracle is a plain public
//     storage var (no smoothing on read).
//
//   flavor "cvxeth"      0xb576491f1e6e5e62f1d8f26062ee822b40b0e0d4
//     "Curve CVX-ETH" (cvxeth vintage), vyper 0.3.1. coins =
//     [WETH (18), CVX (18)] -> PRECISIONS [1, 1]; ETH_INDEX = 0 (pool holds
//     native ETH for coin 0 — transfers only, math unchanged). LP token
//     external (0x3A283D9c08E8b55966afb64C515f5143cf907611). LATER
//     tweak_price: trigger additionally requires norm > adjustment_step and
//     old_virtual_price > 0; adjustment_step = max(stored, norm/10); when a
//     pending adjustment fall-throughs (norm <= step) it clears not_adjusted
//     and runs _claim_admin_fees. price_oracle plain public storage.
//
//   flavor "factory_2eth" 0x47d5e1679fe5f0d9f0a657c6715924e33ce05093
//     "Curve.fi Factory Crypto Pool: frxETHCVX" (CurveCryptoSwap2ETH factory
//     template), vyper 0.3.1. coins = [frxETH (18), CVX (18)] -> PRECISIONS
//     [1, 1] (packed PRECISIONS storage = 0). LP token external
//     (0x6e52cce4eafdf77091dd1c82183b2d97b776b397), fee receiver from
//     Factory(self.factory).fee_receiver(). tweak_price identical to cvxeth
//     EXCEPT adjustment_step = max(stored, norm/5). _price_oracle storage is
//     PRIVATE and the price_oracle() view EMA-smooths to the query
//     timestamp -> initial state and final compares MUST use the raw
//     storage slot (slot 5), not the view.
//
// SHARED MATH (byte-identical in all three deployed sources; reused from
// crypto_math.hpp where the exact ports already exist, verified against each
// deployed source line-by-line):
//   newton_D  -> cm::old2_newton_D  (geometric-mean seed, old frac bounds)
//   newton_y  -> cm::old2_newton_y  (old fixed K0_i bounds, final frac assert)
//   geometric_mean -> cm::old_geometric_mean2 (+ local sort-desc wrapper for
//                     the sort=True call sites)
//   _fee      -> cm::two_fee
//   halfpow   -> ported below (iterative 0.5**x series, EXP_PRECISION 1e10)
// ANN/gamma range asserts (MIN_A=4000, MAX_A=4e9, MIN_GAMMA=1e10,
// MAX_GAMMA=2e16) are applied here before calling the cm kernels since the
// cm ports omit them.
//
// CRITICAL SEMANTICS (each verified against the deployed vyper):
//   1. block.timestamp := each event's "ts", set before applying the event.
//      tweak_price's EMA runs only when last_prices_timestamp < ts (once per
//      block), alpha = halfpow((ts - lpt) * 1e18 / ma_half_time).
//   2. last_prices is NOT taken from a get_p-style formula: exchange passes
//      p_i computed from the ACTUAL trade legs (dx*prec_i*1e18/(dy*prec_j),
//      inverted for i>0) when dx > 1e5 and dy > 1e5; single-sided
//      add_liquidity and remove_liquidity_one_coin pass their own p_i
//      formulas; otherwise (p_i == 0) tweak_price runs the newton_y probe:
//      dx_price = xp[0]/1e6, last_prices = price_scale * dx_price /
//      (xp[1] - newton_y(.., xp with xp[0]+dx_price, D_unadjusted, 1)).
//   3. not_adjusted (bool storage) drives a MULTI-TX rebalance state
//      machine: once the profit trigger fires, EVERY subsequent tweak_price
//      attempts a price_scale move until one either succeeds (commit p_new,
//      no claim) or fails its profit gate / norm gate, which clears the flag
//      and runs _claim_admin_fees INSIDE tweak_price (fall-through claim in
//      cvxeth/factory only; eursusd never claims on the norm<=step path).
//   4. _claim_admin_fees "gulps" real coin balances (balanceOf / self.balance
//      for the native-ETH leg) — a NO-OP in this replay because every
//      transfer settles before the claim runs and swap/withdraw fees stay in
//      self.balances; direct donations would break this (validated by the
//      final-state compare). The claim mints LP EXTERNALLY via
//      mint_relative(receiver, frac): frac = vprice*1e18/(vprice-fees)-1e18,
//      claimed = totalSupply * frac / 1e18 (CurveTokenV4/V5 semantics,
//      verified in all three deployed LP token sources); the ClaimAdminFee
//      log fires whenever fees > 0 and the receiver is set, even when the
//      minted amount rounds to 0. xcp_profit -= 2*fees; D is then re-derived
//      from newton_D(xp()) and virtual_price recomputed with the POST-mint
//      supply; xcp_profit_a := the DECREMENTED xcp_profit, but only if it is
//      still > the ORIGINAL xcp_profit_a (admin_fee = 100% would skip it).
//   5. future_A_gamma_time doubles as a flag: exchange/add set it to 1 when a
//      ramp has ended (ts >= t, t > 0); remove_liquidity_one_coin sets it to
//      1 UNCONDITIONALLY (block.timestamp >= fagt is true for fagt == 0!);
//      tweak_price's "Loss" assert only fires when the flag is 0, and the
//      flag is reset 1 -> 0 inside tweak_price (old_virtual_price > 0 gate).
//      So remove_one NEVER hits "Loss" and always leaves fagt == 0.
//   6. remove_liquidity burns FIRST (supply tracked pre-burn), amount =
//      _amount - 1 ("favor LPs", no special full-exit case), d_balance =
//      balances[i]*amount/pre_supply, D -= D*amount/pre_supply. No tweak, no
//      claim, no EMA.
//   7. add_liquidity mints BEFORE tweak_price (supply seen by tweak includes
//      the mint); the logged token_supply is pre_supply + d_token. d_token
//      fee = _calc_token_fee(amountsp, xp)*d_token/1e10 + 1.
//   8. Vyper `and` does NOT short-circuit: `virtual_price*2 - 10**18` and
//      `2*old_virtual_price - 10**18` are evaluated (and would
//      underflow-revert) regardless of the other operand — ported with csub
//      evaluated unconditionally.
//   9. apply_new_parameters (NewParameters event) claims FIRST (with the OLD
//      admin_fee) when the admin fee changes, then stores all new values.
//  10. Reverts: any vyper assert / checked under/overflow / div-by-zero
//      throws; the event is reported {"revert": msg}, pre-event state is
//      restored, replay continues (an on-chain-succeeded event reverting
//      here is a mismatch the harness flags).
//
// SEMANTIC UNCERTAINTIES:
//   a. The gulp reads real ERC20/ETH balances; untracked donations (direct
//      transfers, force-sent ETH — cvxeth's __default__ is payable) would be
//      absorbed on-chain but are invisible here. Final-state compare catches
//      any drift.
//   b. fee_receiver_set is read once at window start; a mid-window receiver
//      change to/from zero would flip claim behavior.
//   c. is_killed is read at window start (kill window expired 2022-era for
//      both standalone pools; factory template has no kill switch).
//
// ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md) — purely additive; with
// none of the new job fields set the event outputs and "final" are byte-for-
// byte what they were before.
//   job:  "probe_all" / "probe_last" / "cf" (bool), per-event "probe" (bool)
//         and, in cf mode, per-event "burn_frac" (1e18 fraction of the LIVE
//         totalSupply to burn) on "remove" / "remove_one".
//   out:  result["probes"] (only when a probe was requested) and
//         result["meter"] (always).
//  v2.1 "spot" is these vintages' OWN internal spot definition: the newton_y
//       probe tweak_price runs whenever the caller hands it p_i == 0, i.e.
//       dx_price = xp[0]/1e6, then price_scale * dx_price / (xp[1] -
//       newton_y(A, gamma, xp with xp[0]+dx_price, D, 1)) — evaluated against
//       the CURRENT post-event xp and self.D. There is no MATH.get_p in these
//       2021 sources, so this small fee-free numerical derivative IS the
//       pool's marginal price (and is the form the spec explicitly blesses).
//       self.xp()'s PRECISIONS leg folds the decimals in, so the value is the
//       REAL, fee-free, whole-token price of coin 1 in coin-0 units, 1e18
//       (n - 1 == 1 entry, e.g. CVX per WETH for cvxeth, EURS per USDC for
//       eursusd). NOTE for cvxeth the coin order is [WETH, CVX], so spot is
//       the price of CVX quoted in WETH.
//  v2.2 "ps_gap_bp" (in every probe) and meter "max_ps_gap_bp" (the run
//       maximum over every event, probe or not) are
//       |spot * 1e18 / price_scale - 1e18| in basis points. This is the input
//       to the project's price_scale-vs-spot freeze rule, so it is tracked
//       inline and never requires a probe.
//  v2.3 Meter fee accounting for this family:
//       - exchange: the fee is charged on the OUTPUT coin (dy is reduced) and
//         stays inside self.balances -> meter["fee"][j].
//       - remove_liquidity_one_coin: the fee is charged on D; the coin-i
//         equivalent N*D_fee*xx[i]/D (the quantity the later vintages log as
//         approx_fee; these log nothing) lands in meter["fee"][i].
//       - add_liquidity: the fee is an LP-TOKEN haircut (d_token_fee) with no
//         coin denomination -> meter["fee_lp"] (LP wei), not meter["fee"].
//       - the DAO slice is taken by MINTING LP to the fee receiver
//         (CurveToken mint_relative), never in coin units, so meter["admin"]
//         is all zeros and meter["admin_lp"] accumulates the LP minted. Note
//         the claim can fire from INSIDE tweak_price (the fall-through path),
//         which is why the accumulation lives in _claim_admin_fees itself.
//  v2.4 probes omit "adm": this family has no admin_balances bucket.
//  v2.5 "rebase_mul" does not apply to this family and is ignored.
//  v2.6 cf "burn_frac" is taken against the totalSupply as it stands at the
//       burn — these vintages do NOT claim inside remove / remove_one, so that
//       is simply the live supply when the event is reached.
//  v2.7 every probe also carries the meter AS OF that event, under the same
//       units and conventions as v2.3: "cfee"[2], "cfee_lp", "cadm"[2] (all
//       zeros, mirroring meter["admin"]), "cadm_lp" and "cvol"[2]. The last
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

namespace c2c {

using u = boost::multiprecision::cpp_int;   // uint256 role
using json = nlohmann::json;

inline const u E18 = cm::E18();
inline const u E10 = cm::pow_int(10, 10);

// classic-vintage parameter bounds (identical in all three sources)
inline const u MIN_A = 4000;                     // N**N * A_MULTIPLIER / 10
inline const u MAX_A("4000000000");              // N**N * A_MULTIPLIER * 1e5
inline const u MIN_GAMMA("10000000000");         // 1e10
inline const u MAX_GAMMA("20000000000000000");   // 2e16
inline const u NOISE_FEE = 100000;               // 1e5
inline const u EXP_PRECISION("10000000000");     // 1e10

enum class Flavor { EURSUSD, CVXETH, FACTORY_2ETH };

// vyper checked subtraction
inline u csub(const u& a, const u& b) {
    if (b > a) throw std::runtime_error("Integer underflow");
    return a - b;
}

// vyper division (revert on /0); unsigned args only in this contract family
inline u vdiv(const u& a, const u& b) {
    if (b == 0) throw std::runtime_error("Division by zero");
    return a / b;
}

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

// ---- math wrappers ----------------------------------------------------------

// geometric_mean(x, sort): sort=True call sites sort desc first
inline u geo_mean(const u x_in[2], bool sort) {
    u x[2] = { x_in[0], x_in[1] };
    if (sort && x[0] < x[1]) { x[0] = x_in[1]; x[1] = x_in[0]; }
    return cm::old_geometric_mean2(x);
}

inline void check_A_gamma_range(const u& ANN, const u& gamma) {
    cm::req(ANN > MIN_A - 1 && ANN < MAX_A + 1, "unsafe values A");
    cm::req(gamma > MIN_GAMMA - 1 && gamma < MAX_GAMMA + 1,
            "unsafe values gamma");
}

inline u newton_D(const u& ANN, const u& gamma, const u x[2]) {
    check_A_gamma_range(ANN, gamma);
    return cm::old2_newton_D(ANN, gamma, x);
}

inline u newton_y(const u& ANN, const u& gamma, const u x[2], const u& D,
                  int i) {
    check_A_gamma_range(ANN, gamma);
    return cm::old2_newton_y(ANN, gamma, x, D, i);
}

// halfpow(power) = 1e18 * 0.5 ** (power/1e18) — exact port
inline u halfpow(const u& power) {
    u intpow = power / E18;
    u otherpow = power - intpow * E18;
    if (intpow > 59) return 0;
    u result = E18 / (u(1) << intpow.convert_to<unsigned>());
    if (otherpow == 0) return result;

    u term = E18;
    u x("500000000000000000");   // 5e17
    u Ssum = E18;
    bool neg = false;

    for (int i = 1; i < 256; ++i) {
        u K = u(i) * E18;
        u c = csub(K, E18);
        if (otherpow > c) {
            c = otherpow - c;
            neg = !neg;
        } else {
            c = csub(c, otherpow);
        }
        term = term * (c * x / E18) / K;
        if (neg) Ssum = csub(Ssum, term);
        else Ssum += term;
        if (term < EXP_PRECISION) return result * Ssum / E18;
    }
    throw std::runtime_error("halfpow did not converge");
}

// ---- pool state -------------------------------------------------------------

// Engine-contract-v2 revenue meter. Lives INSIDE Pool on purpose: the driver
// snapshots/restores Pool around every event, so a reverted event accrues
// nothing. Never read by the consensus math.
struct Meters {
    u fee[2]{};        // gross fee charged, coin units (see header note v2.3)
    u fee_lp{0};       // gross add_liquidity d_token fee, LP-token units
    u admin_lp{0};     // LP minted to the fee receiver by _claim_admin_fees
    u vol[2]{};        // gross exchange input volume (dx), per coin
};

struct Pool {
    Flavor flavor = Flavor::CVXETH;

    // params (initial_/future_A_gamma kept unpacked)
    u initial_A = 0, initial_gamma = 0;
    u future_A = 0, future_gamma = 0;
    u initial_A_gamma_time = 0, future_A_gamma_time = 0;   // fagt is MUTABLE
    u allowed_extra_profit = 0, fee_gamma = 0, adjustment_step = 0;
    u ma_half_time = 0;
    u mid_fee = 0, out_fee = 0, admin_fee = 0;
    u prec[2];
    bool fee_receiver_set = true;
    bool is_killed = false;

    // state
    u bal[2];
    u D = 0;
    u price_scale = 0, price_oracle = 0, last_prices = 0;
    u last_prices_timestamp = 0;
    u xcp_profit = 0, xcp_profit_a = 0, virtual_price = 0;
    bool not_adjusted = false;
    u total_supply = 0;                // external LP token, tracked

    u ts = 0;                          // current block.timestamp

    // per-op ClaimAdminFee tracking (reset by the event loop per event)
    bool claim_fired = false;
    u claimed = 0;

    Meters m;                          // engine contract v2 meter

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
    void xp_now(u out[2]) const {
        out[0] = bal[0] * prec[0];
        out[1] = bal[1] * prec[1] * price_scale / E18;
    }

    // ---- self._fee(xp) ------------------------------------------------------
    u fee(const u xp[2]) const {
        return cm::two_fee(xp, mid_fee, out_fee, fee_gamma);
    }

    // ---- self.get_xcp(D) ----------------------------------------------------
    u get_xcp(const u& D_in) const {
        u x[2] = { D_in / 2, D_in * E18 / (price_scale * 2) };
        return geo_mean(x, true);
    }

    // ---- self._calc_token_fee(amounts, xp) ----------------------------------
    u calc_token_fee(const u amounts[2], const u xp[2]) const {
        u f = fee(xp) * 2 / 4;              // fee * N / (4 * (N-1))
        u Ssum = amounts[0] + amounts[1];
        u avg = Ssum / 2;
        u Sdiff = 0;
        for (int k = 0; k < 2; ++k)
            Sdiff += (amounts[k] > avg) ? u(amounts[k] - avg)
                                        : u(avg - amounts[k]);
        return vdiv(f * Sdiff, Ssum) + NOISE_FEE;
    }

    // ---- self._claim_admin_fees() -------------------------------------------
    // The coin gulp is a no-op here (header note 4). Sets claim_fired /
    // claimed when the ClaimAdminFee log would be emitted.
    void claim_admin_fees() {
        u A_g[2];
        A_gamma(A_g);

        u xcp = xcp_profit;
        u xcp_a = xcp_profit_a;

        // gulp: self.balances[i] = balanceOf/self.balance — no-op.

        u vprice = virtual_price;

        if (xcp > xcp_a) {
            u fees = (xcp - xcp_a) * admin_fee / (2 * E10);
            if (fees > 0 && fee_receiver_set) {
                u frac = csub(vdiv(vprice * E18, csub(vprice, fees)), E18);
                // CurveToken.mint_relative(receiver, frac):
                u d_supply = total_supply * frac / E18;
                if (d_supply > 0) {
                    total_supply += d_supply;
                    m.admin_lp += d_supply;      // meter only
                }
                xcp = csub(xcp, fees * 2);
                xcp_profit = xcp;
                claim_fired = true;         // log ClaimAdminFee(recv, claimed)
                claimed = d_supply;
            }
        }

        u supply = total_supply;            // read AFTER the mint

        // Recalculate D b/c we "gulped"
        u xp_c[2];
        xp_now(xp_c);
        u A_l = A_g[0], g_l = A_g[1];
        D = newton_D(A_l, g_l, xp_c);
        virtual_price = vdiv(E18 * get_xcp(D), supply);

        if (xcp > xcp_a) xcp_profit_a = xcp;   // decremented xcp vs OLD xcp_a
    }

    // ---- self.tweak_price(A_gamma, _xp, p_i, new_D) -------------------------
    void tweak_price(const u A_g[2], const u _xp[2], const u& p_i,
                     const u& new_D) {
        u price_oracle_l = price_oracle;
        u last_prices_l = last_prices;
        u price_scale_l = price_scale;
        u lpt = last_prices_timestamp;
        u p_new = 0;

        // ---- EMA (once per block) ----
        if (lpt < ts) {
            u alpha = halfpow(csub(ts, lpt) * E18 / ma_half_time);
            price_oracle_l = (last_prices_l * csub(E18, alpha)
                              + price_oracle_l * alpha) / E18;
            price_oracle = price_oracle_l;
            last_prices_timestamp = ts;
        }

        u D_unadjusted = new_D;
        if (new_D == 0)
            D_unadjusted = newton_D(A_g[0], A_g[1], _xp);

        if (p_i > 0) {
            last_prices_l = p_i;
        } else {
            // newton_y probe price
            u xp_probe[2] = { _xp[0], _xp[1] };
            u dx_price = xp_probe[0] / 1000000;
            xp_probe[0] += dx_price;
            last_prices_l = vdiv(
                price_scale_l * dx_price,
                csub(_xp[1], newton_y(A_g[0], A_g[1], xp_probe,
                                      D_unadjusted, 1)));
        }
        last_prices = last_prices_l;

        u supply = total_supply;
        u old_xcp_profit = xcp_profit;
        u old_virtual_price = virtual_price;

        // ---- profit numbers without price adjustment ----
        u xp_eq[2] = { D_unadjusted / 2,
                       D_unadjusted * E18 / (2 * price_scale_l) };
        u xcp_profit_l = E18;
        u virtual_price_l = E18;

        if (old_virtual_price > 0) {
            u xcp = geo_mean(xp_eq, true);
            virtual_price_l = vdiv(E18 * xcp, supply);
            xcp_profit_l = old_xcp_profit * virtual_price_l
                           / old_virtual_price;

            u t = future_A_gamma_time;
            if (virtual_price_l < old_virtual_price && t == 0)
                throw std::runtime_error("Loss");
            if (t == 1) future_A_gamma_time = 0;
        }
        xcp_profit = xcp_profit_l;

        bool needs_adjustment = not_adjusted;

        if (flavor == Flavor::EURSUSD) {
            // EARLY variant: no norm gate in the trigger, plain step, no
            // trailing claim. norm/step computed inside the branch.
            u vp2m1 = csub(virtual_price_l * 2, E18);   // non-short-circuit
            if (!needs_adjustment
                && vp2m1 > xcp_profit_l + 2 * allowed_extra_profit) {
                needs_adjustment = true;
                not_adjusted = true;
            }
            if (needs_adjustment) {
                u step = adjustment_step;
                u norm = price_oracle_l * E18 / price_scale_l;
                norm = (norm > E18) ? u(norm - E18) : u(E18 - norm);
                if (norm > step && old_virtual_price > 0) {
                    if (attempt_adjustment(A_g, _xp, price_oracle_l,
                                           price_scale_l, norm, step, supply,
                                           xcp_profit_l, virtual_price_l,
                                           D_unadjusted))
                        return;
                    return;   // failed attempt path already committed+claimed
                }
            }
            D = D_unadjusted;
            virtual_price = virtual_price_l;
            return;           // eursusd: not_adjusted may stay set, NO claim
        }

        // LATER variants (cvxeth: norm/10 floor, factory_2eth: norm/5)
        u norm = price_oracle_l * E18 / price_scale_l;
        norm = (norm > E18) ? u(norm - E18) : u(E18 - norm);
        u step = adjustment_step;
        {
            u nk = norm / ((flavor == Flavor::CVXETH) ? 10 : 5);
            if (nk > step) step = nk;
        }

        u vp2m1 = csub(virtual_price_l * 2, E18);       // non-short-circuit
        if (!needs_adjustment
            && vp2m1 > xcp_profit_l + 2 * allowed_extra_profit
            && norm > step && old_virtual_price > 0) {
            needs_adjustment = true;
            not_adjusted = true;
        }

        if (needs_adjustment) {
            if (norm > step && old_virtual_price > 0) {
                if (attempt_adjustment(A_g, _xp, price_oracle_l,
                                       price_scale_l, norm, step, supply,
                                       xcp_profit_l, virtual_price_l,
                                       D_unadjusted))
                    return;
                return;       // failed attempt path already committed+claimed
            }
        }

        // fall-through: adjustment did not happen
        D = D_unadjusted;
        virtual_price = virtual_price_l;

        // norm appeared < adjustment_step after (cvxeth/factory only)
        if (needs_adjustment) {
            not_adjusted = false;
            claim_admin_fees();
        }
    }

    // The shared price_scale adjustment attempt. Returns true when the new
    // scale was committed; on the failed-profit path it commits the
    // unadjusted values, clears not_adjusted and claims (all flavors share
    // this else-branch), and returns false.
    bool attempt_adjustment(const u A_g[2], const u _xp[2],
                            const u& price_oracle_l, const u& price_scale_l,
                            const u& norm, const u& step, const u& supply,
                            const u& xcp_profit_l, const u& virtual_price_l,
                            const u& D_unadjusted) {
        u p_new = (price_scale_l * csub(norm, step) + step * price_oracle_l)
                  / norm;

        u xp2[2] = { _xp[0], _xp[1] * p_new / price_scale_l };
        u D_new = newton_D(A_g[0], A_g[1], xp2);
        xp2[0] = D_new / 2;
        xp2[1] = D_new * E18 / (2 * p_new);
        u vp_new = vdiv(E18 * geo_mean(xp2, true), supply);

        u vp2m1 = csub(2 * vp_new, E18);                // non-short-circuit
        if (vp_new > E18 && vp2m1 > xcp_profit_l) {
            price_scale = p_new;
            D = D_new;
            virtual_price = vp_new;
            return true;
        }
        not_adjusted = false;
        D = D_unadjusted;
        virtual_price = virtual_price_l;
        claim_admin_fees();
        return false;
    }
};

// ---- engine contract v2: spot price, ps gap, probes -------------------------

// REAL, fee-free marginal price of coin 1 in coin-0 units, 1e18-scaled. These
// vintages have no MATH.get_p; their own internal spot definition is the
// newton_y probe tweak_price runs whenever the caller hands it p_i == 0:
// dx_price = xp[0]/1e6, then price_scale * dx_price / (xp[1] - newton_y(A,
// gamma, xp with xp[0]+dx_price, D, 1)) — reproduced verbatim against the
// CURRENT (post-event) xp and self.D. self.xp()'s PRECISIONS leg folds the
// decimals in, so the result is the whole-token price. Degenerate states
// (non-convergence, frac asserts, a zero denominator on an emptied pool)
// yield 0 rather than throwing.
inline u spot_of(const Pool& p) {
    try {
        u A_g[2];
        p.A_gamma(A_g);
        u xp[2];
        p.xp_now(xp);
        if (xp[0] == 0 || xp[1] == 0 || p.D == 0) return 0;
        u dx_price = xp[0] / 1000000;
        if (dx_price == 0) return 0;
        u xp_probe[2] = { xp[0] + dx_price, xp[1] };
        u y = newton_y(A_g[0], A_g[1], xp_probe, p.D, 1);
        if (y >= xp[1]) return 0;                  // no measurable move
        return p.price_scale * dx_price / (xp[1] - y);
    } catch (const std::exception&) {
        return 0;
    }
}

// |spot * 1e18 / price_scale - 1e18| expressed in basis points
inline u gap_bp_one(const u& spot, const u& ps) {
    if (ps == 0 || spot == 0) return 0;
    u r = spot * E18 / ps;
    u d = (r > E18) ? u(r - E18) : u(E18 - r);
    return d * 10000 / E18;
}

// get_virtual_price(): 1e18 * get_xcp(D) / totalSupply, recomputed live from
// the post-event state (self.virtual_price is stale after remove_liquidity).
inline u live_vp(const Pool& p) {
    if (p.total_supply == 0 || p.price_scale == 0) return 0;
    try {
        return E18 * p.get_xcp(p.D) / p.total_supply;
    } catch (const std::exception&) {
        return 0;
    }
}

// probe object; "adm" is omitted — this family has no admin_balances bucket
// (the DAO slice is minted as LP, see meter["admin_lp"]).
inline json make_probe(const Pool& p, const u& sp, int idx) {
    return json{
        {"i", idx},
        {"bal", json::array({S(p.bal[0]), S(p.bal[1])})},
        {"sup", S(p.total_supply)},
        {"D", S(p.D)},
        {"vp", S(live_vp(p))},
        {"xcp", S(p.xcp_profit)},
        {"ps", json::array({S(p.price_scale)})},
        {"spot", json::array({S(sp)})},
        {"ps_gap_bp", json::array({
            gap_bp_one(sp, p.price_scale).convert_to<long long>()})},
        // cumulative meter as of this event — mirrors result["meter"]'s
        // fee / fee_lp / admin / admin_lp / vol exactly (last probe == totals).
        // "cadm" is all zeros for the same reason "admin" is: the DAO slice is
        // minted as LP, which "cadm_lp" carries.
        {"cfee", json::array({S(p.m.fee[0]), S(p.m.fee[1])})},
        {"cfee_lp", S(p.m.fee_lp)},
        {"cadm", json::array({"0", "0"})},
        {"cadm_lp", S(p.m.admin_lp)},
        {"cvol", json::array({S(p.m.vol[0]), S(p.m.vol[1])})}};
}

// ---- event appliers (throw == revert; caller restores state) ----------------

// _exchange (transfers resolved by the feeder; use_eth affects transfers only)
inline json apply_exchange(Pool& p, int i, int j, const u& dx) {
    cm::req(!p.is_killed, "the pool is killed");
    cm::req(i != j, "coin index out of range");
    cm::req(i >= 0 && i < 2 && j >= 0 && j < 2, "coin index out of range");
    cm::req(dx > 0, "do not exchange 0 coins");

    u A_g[2];
    p.A_gamma(A_g);

    u xp[2] = { p.bal[0], p.bal[1] };
    u y = xp[j];
    u x0 = xp[i];
    xp[i] = x0 + dx;
    p.bal[i] = xp[i];

    u price_scale_l = p.price_scale;

    xp[0] = xp[0] * p.prec[0];
    xp[1] = xp[1] * price_scale_l * p.prec[1] / E18;

    u prec_i = p.prec[0], prec_j = p.prec[1];
    if (i == 1) { prec_i = p.prec[1]; prec_j = p.prec[0]; }

    // ramp in progress (or the fagt==1 flag): real D storage write
    u t = p.future_A_gamma_time;
    if (t > 0) {
        x0 *= prec_i;
        if (i > 0) x0 = x0 * price_scale_l / E18;
        u x1 = xp[i];
        xp[i] = x0;
        p.D = newton_D(A_g[0], A_g[1], xp);
        xp[i] = x1;
        if (p.ts >= t) p.future_A_gamma_time = 1;
    }

    u dy = csub(xp[j], newton_y(A_g[0], A_g[1], xp, p.D, j));
    xp[j] = csub(xp[j], dy);
    dy = csub(dy, 1);

    if (j > 0) dy = dy * E18 / price_scale_l;
    dy = dy / prec_j;

    u fee_amt = p.fee(xp) * dy / E10;
    dy = csub(dy, fee_amt);
    y = csub(y, dy);

    p.bal[j] = y;

    y *= prec_j;
    if (j > 0) y = y * price_scale_l / E18;
    xp[j] = y;

    // trade price for last_prices
    u pr = 0;
    if (dx > 100000 && dy > 100000) {
        u _dx = dx * prec_i;
        u _dy = dy * prec_j;
        if (i == 0) pr = vdiv(_dx * E18, _dy);
        else pr = vdiv(_dy * E18, _dx);
    }

    p.tweak_price(A_g, xp, pr, 0);

    p.m.vol[i] += dx;              // meter only
    p.m.fee[j] += fee_amt;         // meter only (fee lands on coin j)

    json out{{"dy", S(dy)}, {"price_scale", S(p.price_scale)}};
    if (p.claim_fired) out["claimed"] = S(p.claimed);
    return out;
}

// add_liquidity
inline json apply_add(Pool& p, const u amounts[2]) {
    cm::req(!p.is_killed, "the pool is killed");
    cm::req(amounts[0] > 0 || amounts[1] > 0, "no coins to add");

    u A_g[2];
    p.A_gamma(A_g);

    u xp[2] = { p.bal[0], p.bal[1] };
    u xp_old[2] = { xp[0], xp[1] };
    u amountsp[2] = { 0, 0 };

    for (int k = 0; k < 2; ++k) {
        u b = xp[k] + amounts[k];
        xp[k] = b;
        p.bal[k] = b;
    }
    u xx[2] = { xp[0], xp[1] };

    u price_scale_p = p.price_scale * p.prec[1];   // pre-multiplied
    xp[0] = xp[0] * p.prec[0];
    xp[1] = xp[1] * price_scale_p / E18;
    xp_old[0] = xp_old[0] * p.prec[0];
    xp_old[1] = xp_old[1] * price_scale_p / E18;

    for (int k = 0; k < 2; ++k)
        if (amounts[k] > 0) amountsp[k] = csub(xp[k], xp_old[k]);

    u old_D = 0;
    u t = p.future_A_gamma_time;
    if (t > 0) {
        old_D = newton_D(A_g[0], A_g[1], xp_old);
        if (p.ts >= t) p.future_A_gamma_time = 1;
    } else {
        old_D = p.D;
    }

    u D_new = newton_D(A_g[0], A_g[1], xp);

    u token_supply = p.total_supply;
    u d_token;
    if (old_D > 0)
        d_token = csub(token_supply * D_new / old_D, token_supply);
    else
        d_token = p.get_xcp(D_new);
    cm::req(d_token > 0, "nothing minted");

    u d_token_fee = 0;
    if (old_D > 0) {
        d_token_fee = p.calc_token_fee(amountsp, xp) * d_token / E10 + 1;
        d_token = csub(d_token, d_token_fee);
        token_supply += d_token;
        p.total_supply += d_token;                 // mint BEFORE tweak
        p.m.fee_lp += d_token_fee;                 // meter only

        // single-sided deposit price
        u pr = 0;
        if (d_token > 100000) {
            if (amounts[0] == 0 || amounts[1] == 0) {
                u Ssum = 0, precision = 0;
                int ix = 0;
                if (amounts[0] == 0) {
                    Ssum = xx[0] * p.prec[0];
                    precision = p.prec[1];
                    ix = 1;
                } else {
                    Ssum = xx[1] * p.prec[1];
                    precision = p.prec[0];
                }
                Ssum = Ssum * d_token / token_supply;
                pr = vdiv(Ssum * E18,
                          csub(amounts[ix] * precision,
                               d_token * xx[ix] * precision / token_supply));
                if (ix == 0) pr = vdiv(E18 * E18, pr);
            }
        }

        p.tweak_price(A_g, xp, pr, D_new);
    } else {
        p.D = D_new;
        p.virtual_price = E18;
        p.xcp_profit = E18;
        p.total_supply += d_token;
    }

    json out{{"minted", S(d_token)}, {"fee", S(d_token_fee)},
             {"supply", S(token_supply)},
             {"price_scale", S(p.price_scale)}};
    if (p.claim_fired) out["claimed"] = S(p.claimed);
    return out;
}

// remove_liquidity (proportional; supply_after == the event's token_supply).
// burn_frac (engine contract v2 cf mode, 1e18-scaled) replaces the historical
// supply_after: the burn becomes that fraction of the live totalSupply (these
// vintages do not claim inside remove). nullptr == today's behaviour.
inline json apply_remove(Pool& p, const u& supply_after,
                         const u* burn_frac = nullptr) {
    u total_supply = p.total_supply;               // pre-burn
    u _amount = burn_frac ? u(total_supply * (*burn_frac) / E18)
                          : csub(total_supply, supply_after);
    cm::req(_amount <= total_supply, "burn exceeds supply");
    p.total_supply = csub(p.total_supply, _amount); // burnFrom

    u balances0[2] = { p.bal[0], p.bal[1] };
    u amount = csub(_amount, 1);                   // favor LPs

    u d_bal[2];
    for (int k = 0; k < 2; ++k) {
        d_bal[k] = vdiv(balances0[k] * amount, total_supply);
        p.bal[k] = csub(balances0[k], d_bal[k]);
    }

    p.D = csub(p.D, p.D * amount / total_supply);

    return json{{"amounts", json::array({S(d_bal[0]), S(d_bal[1])})},
                {"supply", S(csub(total_supply, _amount))}};
}

// remove_liquidity_one_coin. burn_frac (engine contract v2 cf mode,
// 1e18-scaled) replaces token_amount with that fraction of the live
// totalSupply; nullptr == today's behaviour.
inline json apply_remove_one(Pool& p, const u& token_amount_in, int i,
                             const u* burn_frac = nullptr) {
    if (p.flavor != Flavor::FACTORY_2ETH)
        cm::req(!p.is_killed, "the pool is killed");

    u A_g[2];
    p.A_gamma(A_g);

    u fagt = p.future_A_gamma_time;

    // ---- _calc_withdraw_one_coin(A_g, token_amount, i, fagt>0, True) ----
    u token_supply = p.total_supply;
    const u token_amount = burn_frac
                               ? u(token_supply * (*burn_frac) / E18)
                               : token_amount_in;
    cm::req(token_amount <= token_supply, "token amount more than supply");
    cm::req(i >= 0 && i < 2, "coin out of range");

    u xx[2] = { p.bal[0], p.bal[1] };

    u price_scale_i = p.price_scale * p.prec[1];
    u xp[2] = { xx[0] * p.prec[0], xx[1] * price_scale_i / E18 };
    if (i == 0) price_scale_i = E18 * p.prec[0];

    u D0 = (fagt > 0) ? newton_D(A_g[0], A_g[1], xp) : p.D;
    u Dv = D0;

    u fee_rate = p.fee(xp);
    u dD = token_amount * Dv / token_supply;
    u D_fee = fee_rate * dD / (2 * E10) + 1;
    // coin-i-denominated withdrawal fee (these vintages do not log it; the
    // formula is the one the later vintages log as approx_fee). Meter only.
    p.m.fee[i] += 2 * D_fee * xx[i] / Dv;
    Dv = csub(Dv, csub(dD, D_fee));
    u y = newton_y(A_g[0], A_g[1], xp, Dv, i);
    u dy = csub(xp[i], y) * E18 / price_scale_i;
    xp[i] = y;

    u pr = 0;
    if (dy > 100000 && token_amount > 100000) {
        u Ssum, precision;
        if (i == 1) {
            Ssum = xx[0] * p.prec[0];
            precision = p.prec[1];
        } else {
            Ssum = xx[1] * p.prec[1];
            precision = p.prec[0];
        }
        Ssum = Ssum * dD / D0;
        pr = vdiv(Ssum * E18,
                  csub(dy * precision, dD * xx[i] * precision / D0));
        if (i == 0) pr = vdiv(E18 * E18, pr);
    }

    // ---- state updates + tweak ----
    if (p.ts >= fagt) p.future_A_gamma_time = 1;   // fagt==0 -> ALWAYS

    p.bal[i] = csub(p.bal[i], dy);
    p.total_supply = csub(p.total_supply, token_amount);   // burnFrom

    p.tweak_price(A_g, xp, pr, Dv);

    json out{{"dy", S(dy)}, {"supply", S(p.total_supply)},
             {"price_scale", S(p.price_scale)}};
    if (p.claim_fired) out["claimed"] = S(p.claimed);
    return out;
}

// standalone claim_admin_fees()
inline json apply_claim(Pool& p) {
    p.claim_admin_fees();
    json out{{"supply", S(p.total_supply)}};
    out["claimed"] = p.claim_fired ? S(p.claimed) : "none";
    return out;
}

// apply_new_parameters (NewParameters carries the applied values)
inline json apply_new_params(Pool& p, const json& ev) {
    u new_admin_fee = ju(ev.at("admin_fee"));
    if (p.admin_fee != new_admin_fee) {
        p.claim_admin_fees();                      // claim with OLD admin_fee
        p.admin_fee = new_admin_fee;
    }
    p.mid_fee = ju(ev.at("mid_fee"));
    p.out_fee = ju(ev.at("out_fee"));
    p.fee_gamma = ju(ev.at("fee_gamma"));
    p.allowed_extra_profit = ju(ev.at("allowed_extra_profit"));
    p.adjustment_step = ju(ev.at("adjustment_step"));
    p.ma_half_time = ju(ev.at("ma_half_time"));
    json out = json::object();
    if (p.claim_fired) out["claimed"] = S(p.claimed);
    return out;
}

} // namespace c2c

// ---- entry point ------------------------------------------------------------

inline nlohmann::json run_crypto2_classic(const nlohmann::json& job) {
    using c2c::u;
    using c2c::ju;
    using c2c::S;
    using json = nlohmann::json;

    cm::req(job.at("n").get<int>() == 2, "crypto2_classic needs n == 2");

    c2c::Pool p;

    const std::string fl = job.at("flavor").get<std::string>();
    if (fl == "eursusd") p.flavor = c2c::Flavor::EURSUSD;
    else if (fl == "cvxeth") p.flavor = c2c::Flavor::CVXETH;
    else if (fl == "factory_2eth") p.flavor = c2c::Flavor::FACTORY_2ETH;
    else cm::req(false, "unknown flavor");

    const json& decs = job.at("decimals");
    for (int k = 0; k < 2; ++k) {
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
    p.allowed_extra_profit = ju(prm.at("allowed_extra_profit"));
    p.fee_gamma = ju(prm.at("fee_gamma"));
    p.adjustment_step = ju(prm.at("adjustment_step"));
    p.ma_half_time = ju(prm.at("ma_half_time"));
    p.mid_fee = ju(prm.at("mid_fee"));
    p.out_fee = ju(prm.at("out_fee"));
    p.admin_fee = ju(prm.at("admin_fee"));
    if (prm.contains("fee_receiver_set"))
        p.fee_receiver_set = prm.at("fee_receiver_set").get<bool>();
    if (prm.contains("is_killed"))
        p.is_killed = prm.at("is_killed").get<bool>();

    const json& st = job.at("state");
    for (int k = 0; k < 2; ++k)
        p.bal[k] = ju(st.at("balances").at(static_cast<size_t>(k)));
    p.D = ju(st.at("D"));
    p.price_scale = ju(st.at("price_scale"));
    p.price_oracle = ju(st.at("price_oracle"));
    p.last_prices = ju(st.at("last_prices"));
    p.last_prices_timestamp = ju(st.at("last_prices_timestamp"));
    p.xcp_profit = ju(st.at("xcp_profit"));
    p.xcp_profit_a = ju(st.at("xcp_profit_a"));
    p.virtual_price = ju(st.at("virtual_price"));
    p.not_adjusted = st.at("not_adjusted").get<bool>();
    p.total_supply = ju(st.at("total_supply"));

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
        p.claim_fired = false;
        p.claimed = 0;

        // cf mode: burn amounts become fractions of the LIVE total supply (the
        // historical supply_after / burn describes a state path that no longer
        // exists). Outside cf mode burn_frac is ignored entirely.
        const bool has_bf = cf_mode && ev.contains("burn_frac");
        const u burn_frac_val = has_bf ? ju(ev.at("burn_frac")) : u(0);

        c2c::Pool snapshot = p;   // restored on revert
        bool reverted = false, skipped = false;

        try {
            json outputs;

            if (type == "exchange") {
                outputs = c2c::apply_exchange(p, ev.at("sold_id").get<int>(),
                                              ev.at("bought_id").get<int>(),
                                              ju(ev.at("dx")));
            } else if (type == "add") {
                u amts[2];
                for (int k = 0; k < 2; ++k)
                    amts[k] = ju(ev.at("amounts").at(static_cast<size_t>(k)));
                outputs = c2c::apply_add(p, amts);
            } else if (type == "remove") {
                outputs = c2c::apply_remove(
                    p, has_bf ? u(0) : ju(ev.at("supply_after")),
                    has_bf ? &burn_frac_val : nullptr);
            } else if (type == "remove_one") {
                outputs = c2c::apply_remove_one(
                    p, has_bf ? u(0) : ju(ev.at("burn")),
                    ev.at("i").get<int>(),
                    has_bf ? &burn_frac_val : nullptr);
            } else if (type == "claim_admin") {
                outputs = c2c::apply_claim(p);
            } else if (type == "ramp_ag") {
                // RampAgamma logs exactly what ramp_A_gamma stores
                p.initial_A = ju(ev.at("initial_A"));
                p.future_A = ju(ev.at("future_A"));
                p.initial_gamma = ju(ev.at("initial_gamma"));
                p.future_gamma = ju(ev.at("future_gamma"));
                p.initial_A_gamma_time = ju(ev.at("initial_time"));
                p.future_A_gamma_time = ju(ev.at("future_time"));
                outputs = json::object();
            } else if (type == "stop_ramp_ag") {
                u A = ju(ev.at("A")), g = ju(ev.at("gamma"));
                u t = ju(ev.at("time"));
                p.initial_A = A;
                p.future_A = A;
                p.initial_gamma = g;
                p.future_gamma = g;
                p.initial_A_gamma_time = t;
                p.future_A_gamma_time = t;
                outputs = json::object();
            } else if (type == "new_params") {
                outputs = c2c::apply_new_params(p, ev);
            } else {
                out_events.push_back(json{{"type", type}, {"skipped", true}});
                skipped = true;
            }

            if (!skipped) {
                outputs["claim_fired"] = p.claim_fired;
                out_events.push_back(json{{"type", type},
                                          {"outputs", outputs}});
            }
        } catch (const std::exception& e) {
            p = std::move(snapshot);   // restore pre-event state
            reverted = true;
            out_events.push_back(json{{"type", type},
                                      {"revert", std::string(e.what())}});
        }

        // ---- engine contract v2 bookkeeping (never touches pool state) ----
        ++n_events;
        if (reverted) ++n_reverts;
        u sp = c2c::spot_of(p);
        {
            u gap = c2c::gap_bp_one(sp, p.price_scale);
            if (gap > max_ps_gap) max_ps_gap = gap;
        }
        bool want_probe =
            probe_all || (probe_last && ev_idx + 1 == n_ev_total) ||
            (ev.contains("probe") && ev.at("probe").is_boolean() &&
             ev.at("probe").get<bool>());
        if (want_probe) {
            any_probe = true;
            probes.push_back(c2c::make_probe(p, sp,
                                             static_cast<int>(ev_idx)));
        }
        ++ev_idx;
    }

    json result;
    result["events"] = out_events;
    result["final"] = json{
        {"balances", json::array({S(p.bal[0]), S(p.bal[1])})},
        {"D", S(p.D)},
        {"price_scale", S(p.price_scale)},
        {"price_oracle", S(p.price_oracle)},
        {"last_prices", S(p.last_prices)},
        {"last_prices_timestamp", S(p.last_prices_timestamp)},
        {"virtual_price", S(p.virtual_price)},
        {"xcp_profit", S(p.xcp_profit)},
        {"xcp_profit_a", S(p.xcp_profit_a)},
        {"not_adjusted", p.not_adjusted},
        {"future_A_gamma_time", S(p.future_A_gamma_time)},
        {"total_supply", S(p.total_supply)}};

    // ---- engine contract v2 result additions -------------------------------
    // The DAO slice is taken by MINTING LP to the fee receiver (CurveToken
    // mint_relative), never in coin units, so "admin" is all zeros and
    // "admin_lp" carries it. "fee" is the gross coin-denominated fee (swap fee
    // on the output coin + the remove_one withdrawal fee); add_liquidity's fee
    // is an LP-token haircut with no coin denomination -> "fee_lp".
    result["meter"] = json{
        {"fee", json::array({S(p.m.fee[0]), S(p.m.fee[1])})},
        {"fee_lp", S(p.m.fee_lp)},
        {"admin", json::array({"0", "0"})},
        {"admin_lp", S(p.m.admin_lp)},
        {"vol", json::array({S(p.m.vol[0]), S(p.m.vol[1])})},
        {"n_events", n_events},
        {"n_reverts", n_reverts},
        {"max_ps_gap_bp", max_ps_gap.convert_to<long long>()}};
    if (any_probe) result["probes"] = probes;
    return result;
}
