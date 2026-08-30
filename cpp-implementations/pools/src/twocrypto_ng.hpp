#pragma once
// ============================================================================
// twocrypto_ng.hpp — wei-exact replay state machine for the "Twocrypto"
// version "v2.1.0d" pools on ethereum mainnet (vyper 0.4.3, Yield Basis /
// iREET family, deployed through the standard Twocrypto-NG factory
// 0x98EE851a00abeE0d95D08cF4CA2BdCE32aeaAF7F with a custom blueprint).
//
// TARGET POOLS (verified 2026-08-29):
//   0x83f24023d15d835a213df24fd309c47dab5beb32  "YB cbBTC"   flavor "yb"
//   0xf1f435b05d255a5dbde37333c0f61da6f69c6127  "YB tBTC"    flavor "yb"
//   0xd9ff8396554a0d18b2cfbec53e1979b7ecce8373  "YB WBTC"    flavor "yb"
//   0x57129759d0e23116c1e7402dbc084e53d2e209a2  "iREET/pmUSD" flavor "ireet"
//
// SOURCES FOLLOWED (authoritative, fetched 2026-08-29):
//   Pool:  Blockscout verified source of 0x83f240... (identical bytes for all
//          three YB pools) and Sourcify verified source of 0x571297...
//          (iREET). Both declare version = "v2.1.0d", pragma vyper 0.4.3.
//          The two sources differ ONLY in (a) tweak_price's adjustment_step
//          (yb: max(param, norm/5); ireet: min(param, norm/5)), (b) the
//          _calc_token_fee lp_spam_penalty formula (yb: pf*fee/1e18;
//          ireet: min(fee, pf*fee*donation_shares/totalSupply/max_ratio)),
//          (c) constructor defaults (state — read from chain anyway), and
//          (d) checked-vs-unsafe div/sub spellings that are value-identical
//          on the reachable domain. => one engine, two flavors.
//   Math:  MATH() = 0x79839c2D74531A8222C0F555865aAc1834e82e51 on ALL FOUR
//          pools (unverified on explorers; version() = "v0.1.0"; 2515 bytes
//          of runtime code). Behaviorally verified by fuzzing eth_call
//          against local ports (60/60 newton_D, 60/60 get_y, 60/60 get_p,
//          9/9 wad_exp exact): it is the StableswapMath shell — CLASSIC
//          STABLESWAP Newton with A_MULTIPLIER = 10000, Ann = _amp *
//          N_COINS; gamma and K0_prev are accepted and IGNORED (multiplying
//          gamma by 7 changes nothing); get_y returns [y, 0]; get_p is the
//          stableswap spot formula; wad_exp is snekmate's. It is the direct
//          predecessor of the v0.1.1 math used by the v3.0.0 YB pools,
//          WITHOUT v0.1.1's "!balance" (ratio < 10_000) guard — an extreme
//          imbalance runs the full 255 Newton iterations and reverts with
//          "Did not converge" instead. Hence cm::ss_newton_D(...,
//          check_balance=false) / cm::ss_get_y are the exact kernels.
//          NOTE: despite the task-level expectation of "real cryptoswap
//          math with active gamma", the DEPLOYED math ignores gamma; this
//          engine ports what is deployed.
//
// STORAGE LAYOUT (vyper 0.4.3, @nonreentrant uses transient storage, so
// user vars start at slot 0; verified slot-by-slot against getters):
//   0 MATH, 1 VIEW, 2 cached_price_scale, 3 cached_price_oracle,
//   4 last_prices, 5 last_timestamp, 6 initial_A_gamma,
//   7 initial_A_gamma_time, 8 future_A_gamma, 9 future_A_gamma_time,
//   10 donation_shares, 11 donation_shares_max_ratio, 12 donation_duration,
//   13 last_donation_release_ts, 14 donation_protection_expiry_ts,
//   15 donation_protection_period, 16 donation_protection_lp_threshold,
//   17..18 balances[2], 19 D, 20 xcp_profit, 21 xcp_profit_a,
//   22 virtual_price, 23 packed_rebalancing_params, 24 packed_fee_params,
//   25 admin_fee, 26 last_admin_fee_claim_timestamp, 27 balanceOf,
//   28 allowance, 29 totalSupply.
//
// SEMANTIC DECISIONS / NOTES (each checked against the deployed source):
//  1. block.timestamp := each event's "ts". The EMA runs once per block
//     (last_timestamp < ts); the REBALANCE gate also requires the pre-EMA
//     last_timestamp < ts, i.e. only the FIRST pool op in a block can move
//     price_scale.
//  2. _is_ramping() compares future_A_gamma_time > last_timestamp (the EMA
//     timestamp!), not block.timestamp — ported as such everywhere
//     (_exchange D refresh, _get_D, tweak_price's "virtual price decreased"
//     allowance, _claim_admin_fees gate, ramp_A_gamma's "!ramp" gate).
//  3. xcp_profit tracking is ADDITIVE: xcp_profit += vp - old_vp (goes down
//     under ramping losses); rebalance threshold_vp = max(1e18,
//     (xcp_profit + 1e18)/2); the trigger compares vp_boosted = 1e18*xcp /
//     (total_supply - donation_shares_after_protection).
//  4. _claim_admin_fees runs ONLY at the top of _remove_liquidity_fixed_out
//     (remove_liquidity_one_coin / remove_liquidity_fixed_out) — NOT in
//     exchange/add/remove_liquidity, and there is no external claim. Even
//     with admin_fee == 0 (all three YB pools) it mutates state once per
//     86400s: virtual_price is recomputed from _xcp (a DOWNWARD correction
//     of the cached value is possible), xcp_profit_a ratchets up to
//     xcp_profit, last_admin_fee_claim_timestamp := ts. With fees > 0 it
//     additionally transfers admin_tokens out of balances (totalSupply is
//     NOT minted/changed) and adjusts D down; ClaimAdminFee logs only when
//     admin_share > 0. The "vprice < 1e18" bail leaves ALL state untouched.
//  5. Exchange fees stay in the pool balances (no per-swap admin skim; no
//     admin_balances bucket exists in this version).
//  6. add_liquidity logs token_supply = (pre-op totalSupply) + d_token;
//     a donation-share burn inside tweak_price can make the ACTUAL post-op
//     totalSupply smaller than the logged value. Outputs report the logged
//     value as "supply" and the true value as "total_supply".
//  7. A donation add (add_liquidity(..., donation=True)) asserts receiver
//     == 0x0 and emits BOTH Donation and AddLiquidity(receiver=0x0). The
//     job feeds it ONCE as type "add" with donation=true.
//  8. remove_liquidity burns first, pays balances[i]*amount/total_supply
//     (NO -1 decrement in this version), scales D down, and does NOT call
//     tweak_price (no EMA, no price action).
//  9. _withdraw_leftover_donations() runs at the end of remove_liquidity
//     and _remove_liquidity_fixed_out: when donation_shares == totalSupply
//     (including 0 == 0 after a pool-emptying removal) the pool sweeps ALL
//     balances to the fee receiver, zeroes donation_shares/totalSupply/D/
//     donation_protection_expiry_ts and emits a second RemoveLiquidity log
//     (token_supply = 0). Outputs report "sweep_amounts" when it fires; the
//     job builder must drop/skip that second log (type "sweep_log").
// 10. _calc_withdraw_fixed_out: amountsp[i] = amount_i*price_scales[i]/1e18
//     FLOORS (no ceil), get_y has NO +1 nudge (both differ from v3.0.0),
//     fee xp is xp_new (post-first-get_y), dD -= dD*approx_fee/1e10 + 1.
// 11. _fee() has NO min/max clamp (MIN_FEE only gates apply_new_parameters).
// 12. _calc_token_fee (from_view=False): balances_ratio = (balances[0] -
//     amounts[0])*prec0*1e18 / ((balances[1] - amounts[1])*prec1) where
//     balances are the CURRENT stored balances (post-transfer-in for adds,
//     pre-withdrawal for removes) and amounts are the op amounts; the
//     amounts are then scaled by _xp(amounts, balances_ratio).
// 13. Vyper checked ops -> csub()/throw; unsafe_mul/unsafe_div wrap mod
//     2^256 where noted (mask256). A throw == revert: the event is reported
//     {"revert": msg}, the pre-event snapshot is restored, replay continues
//     (an engine revert of an event that succeeded on-chain is a mismatch
//     the harness flags).
// 14. Slippage guards (min_dy/min_mint_amount/min_amounts/min_amount_j) and
//     admin auth are not replayed — the feeder only supplies events that
//     succeeded on-chain.
// 15. ramp_A_gamma / stop_ramp_A_gamma / apply_new_parameters /
//     set_donation_duration / set_donation_protection_params /
//     set_admin_fee apply exactly what their logs carry (final values;
//     apply_new_parameters' sentinel resolution already happened on-chain).
//     set_periphery (MATH/VIEW swap) is reported {"skipped": true} — a math
//     swap would need a new flavor and must be surfaced, not silently eaten.
//
// JOB SCHEMA ("kind":"twocrypto_ng","n":2):
//   flavor: "yb" (default) | "ireet"
//   decimals: [d0, d1]  (fallback for precisions)
//   precisions: [p0, p1]  (PRECISIONS immutable, from precisions() view;
//                          preferred over decimals)
//   params: A, gamma, future_A, future_gamma (unpacked; on-chain packed as
//     (A << 128) | gamma), initial_A_gamma_time, future_A_gamma_time,
//     mid_fee, out_fee, fee_gamma (packed_fee_params, 1e10/1e18 scales),
//     allowed_extra_profit, adjustment_step, ma_time (packed_rebalancing_
//     params; ma_time RAW, seconds/ln2), admin_fee (1e10),
//     donation_duration, donation_shares_max_ratio,
//     donation_protection_period, donation_protection_lp_threshold,
//     fee_receiver_set (bool, default true)
//   state: balances[2], price_scale, price_oracle, last_prices (scalars or
//     1-element arrays), last_timestamp, D, xcp_profit, xcp_profit_a,
//     virtual_price, total_supply, donation_shares,
//     last_donation_release_ts, donation_protection_expiry_ts,
//     last_admin_fee_claim_timestamp
//   events (each carries "ts"):
//     "exchange"    {sold_id, bought_id, dx}
//                   -> {dy, fee, price_scale, total_supply}
//     "add"         {amounts:[2], donation?:bool}
//                   -> {minted, fee, supply (as logged), price_scale,
//                       total_supply, sweep_amounts?}
//     "remove"      {supply_after} or {burn}
//                   -> {amounts:[2], supply, sweep_amounts?}
//     "remove_one"  {burn, i (coin withdrawn, == log coin_index)}
//                   -> {dy, approx_fee (LP units, as logged), price_scale,
//                       total_supply, claim?:{tokens:[2]}, sweep_amounts?}
//     "remove_imb"  {burn, amounts_expected:[2]} (fixed leg inferred) or
//                   {burn, i, amount_i}
//                   -> like remove_one plus {amounts:[2], inferred_i?}
//     "ramp_ag"     {future_A, future_gamma, future_time}
//     "stop_ramp_ag" {}                     (uses A/gamma at ts, like chain)
//     "new_params"  {mid_fee, out_fee, fee_gamma, allowed_extra_profit,
//                    adjustment_step, ma_time}          (final logged values)
//     "set_donation_duration"  {duration}
//     "set_donation_protection" {period, lp_threshold, max_ratio}
//     "set_admin_fee" {admin_fee}
//     "sweep_log"   -> {"skipped": true}  (the second RemoveLiquidity log
//                    emitted by _withdraw_leftover_donations, note 9)
//     anything else -> {"skipped": true}
//   Result: {"events": [...], "final": {balances[2], D, price_scale,
//     price_oracle, last_prices, last_timestamp, xcp_profit, xcp_profit_a,
//     virtual_price, total_supply, donation_shares, donation_duration,
//     donation_shares_max_ratio, last_donation_release_ts,
//     donation_protection_expiry_ts, donation_protection_period,
//     donation_protection_lp_threshold, last_admin_fee_claim_timestamp,
//     initial_A_gamma, initial_A_gamma_time, future_A_gamma,
//     future_A_gamma_time, packed_rebalancing_params, packed_fee_params,
//     admin_fee}} — every mutable storage slot, comparable 1:1 with
//     eth_getStorageAt at the end block.
//
// ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md) — purely additive; with
// none of the new job fields set the event outputs and "final" are byte-for-
// byte what they were before.
//   job:   "probe_all" / "probe_last" / "cf" (bool), per-event "probe" (bool)
//          and, in cf mode, per-event "burn_frac" (1e18 fraction of the LIVE
//          totalSupply to burn).
//   out:   result["probes"] (only when a probe was requested) and
//          result["meter"] (always).
//  * "spot" is the REAL, fee-free marginal price of coin 1 in coin 0 units,
//    1e18-scaled: get_p(_xp(balances, price_scale), D, A) * price_scale /
//    1e18 — the same quantity the pool stores in last_prices, recomputed from
//    the post-event state. PRECISIONS folds the decimals in, so it is a
//    whole-token price.
//  * "ps_gap_bp" / meter "max_ps_gap_bp": |spot*1e18/price_scale - 1e18| in
//    basis points, tracked after EVERY event (no probe required).
//  * Meter fee accounting for this family:
//      - exchange fees are coin-denominated and land on the output coin ->
//        meter["fee"][j]. They stay in the pool (no per-swap admin skim).
//      - add_liquidity / remove_liquidity_one_coin / remove_liquidity_fixed_
//        out charge an LP-TOKEN fee with no per-coin denomination; it is
//        reported separately as meter["fee_lp"] (LP wei).
//      - the DAO slice is NOT minted as LP either: _claim_admin_fees derives
//        a virtual admin_share and then transfers COIN amounts out of
//        balances, so meter["admin"] is per-coin and meter["admin_lp"] is
//        always "0". All three YB pools run admin_fee = 0, so admin is
//        typically all zeros.
//  * probes omit "adm": v2.1.0d has no admin_balances bucket at all.
//  * every probe also carries the meter AS OF that event, under exactly the
//    units and conventions just described: "cfee"[2], "cfee_lp", "cadm"[2],
//    "cadm_lp" (always "0") and "cvol"[2]. The last probe's values equal
//    result["meter"] exactly. The meter lives inside Pool, so a reverted
//    event's state restore rolls it back and that event's probe shows the
//    not-counted (pre-event) totals.
//  * "rebase_mul" does not apply to this family and is ignored.
//
// VALIDATION (2026-08-29, mainnet replay; see specs/twocrypto_ng.md for the
// exact windows and per-event tallies): all four pools replay 100% of their
// window events wei-exact and match the full end-of-window storage state.
// ============================================================================

#include "crypto_math.hpp"

#include <nlohmann/json.hpp>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace tng {

using I = cm::I;
using json = nlohmann::json;

// ---- constants (Twocrypto v2.1.0d) -----------------------------------------
inline const I& E18() { static const I v = cm::E18(); return v; }
inline I FEE_PRECISION() { static I v("10000000000"); return v; }        // 1e10
inline I NOISE_FEE() { static I v("100000"); return v; }               // 0.1bps
inline I MIN_FEE() { static I v("500000"); return v; }    // 0.5bps (params only)
inline I MAX_FEE() { static I v("10000000000"); return v; }
inline const I A_MULTIPLIER = 10000;
inline const I MIN_A = 2 * 10000;               // N_COINS * A_MULTIPLIER
inline I MAX_A() { static I v("100000000"); return v; }      // 10_000 * 10000
inline I MIN_GAMMA() { static I v("10000000000"); return v; }            // 1e10
inline I MAX_GAMMA() { static I v("199000000000000000"); return v; } // 1.99e17
inline const I MAX_PARAM_CHANGE = 10;
inline const I MIN_RAMP_TIME = 86400;
inline const I MIN_ADMIN_FEE_CLAIM_INTERVAL = 86400;
inline I MAX_ADMIN_FEE() { static I v("10000000000"); return v; }        // 1e10
inline const I TWO256_M1 = (I(1) << 256) - 1;

// ---- helpers ----------------------------------------------------------------

// vyper checked subtraction (uint256): revert on underflow
inline I csub(const I& a, const I& b) {
    if (b > a) throw std::runtime_error("Integer underflow");
    return a - b;
}

inline I maxI(const I& a, const I& b) { return a > b ? a : b; }
inline I minI(const I& a, const I& b) { return a < b ? a : b; }
inline I mask256(const I& a) { return a & TWO256_M1; }

// EVM SAR by 96 on int256: floor division by 2^96
inline I sar96(const I& v) {
    static const I d = I(1) << 96;
    if (v >= 0) return v >> 96;
    I m = -v;
    I q = m >> 96;
    if ((m & (d - 1)) != 0) q += 1;
    return -q;
}

// parse job uint (decimal string or JSON number)
inline I ju(const json& v) {
    if (v.is_string()) return I(v.get<std::string>());
    if (v.is_number_unsigned()) return I(v.get<std::uint64_t>());
    if (v.is_number_integer()) {
        long long x = v.get<long long>();
        if (x < 0) throw std::runtime_error("negative uint");
        return I(x);
    }
    throw std::runtime_error("bad uint json");
}

inline I jfield(const json& obj, const char* key, const I& dflt) {
    if (obj.contains(key) && !obj.at(key).is_null()) return ju(obj.at(key));
    return dflt;
}

// price fields arrive as scalars or 1-element arrays
inline I jprice(const json& v) {
    if (v.is_array()) {
        if (v.size() != 1) throw std::runtime_error("price array must have 1 element");
        return ju(v.at(0));
    }
    return ju(v);
}

inline std::string S(const I& v) { return v.str(); }

// ---- MATH v0.1.0 wad_exp — snekmate math._wad_exp, exact int256 port -------
// (identical polynomial to the v0.1.1 math; validated vs chain 9/9)
inline I wad_exp(const I& x_in) {
    static const I LOWCUT("-41446531673892822313");
    static const I HIGHCUT("135305999368893231589");
    static const I LOG2_96("54916777467707473351141471128");     // ln2 * 2^96
    static const I FIVE18 = cm::pow_int(5, 18);
    static const I TWO256 = I(1) << 256;

    if (x_in <= LOWCUT) return 0;
    if (!(x_in < HIGHCUT)) throw std::runtime_error("math: wad_exp overflow");

    I x = (x_in * (I(1) << 78)) / FIVE18;               // SDIV trunc toward 0

    I k = ((x * (I(1) << 96)) / LOG2_96 + (I(1) << 95));
    k = sar96(k);
    x = x - k * LOG2_96;

    I y = sar96((x + I("1346386616545796478920950773328")) * x)
          + I("57155421227552351082224309758442");
    I p = sar96((y + x - I("94201549194550492254356042504812")) * y)
          + I("28719021644029726153956944680412240");
    p = p * x + (I("4385272521454847904659076985693276") << 96);

    I q = sar96((x - I("2855989394907223263936484059900")) * x)
          + I("50020603652535783019961831881945");
    q = sar96(q * x) - I("533845033583426703283633433725380");
    q = sar96(q * x) + I("3604857256930695427073651918091429");
    q = sar96(q * x) - I("14423608567350463180887372962807573");
    q = sar96(q * x) + I("26449188498355588339934803723976023");

    I r = p / q;                                        // SDIV trunc toward 0

    I ur = r;
    if (ur < 0) ur += TWO256;
    I prod = (ur * I("3822833074963236453042738258902158003155416615667"))
             % TWO256;

    I shift = I(195) - k;
    if (shift < 0) throw std::runtime_error("wad_exp shift underflow");
    I res;
    if (shift >= 256) res = 0;
    else res = prod >> shift.convert_to<unsigned>();
    if (res >= (I(1) << 255)) throw std::runtime_error("wad_exp int256 overflow");
    return res;
}

// ---- MATH v0.1.0 get_p — stableswap spot price (gamma ignored) --------------
// Validated vs chain 60/60. Interface takes A_gamma; only A is used.
inline I get_p(const I xp[2], const I& D, const I& A) {
    I ANN = A * 2;
    I Dr = D / 4;                                       // D / N**N
    Dr = Dr * D / xp[0];
    Dr = Dr * D / xp[1];
    I xp0_A = ANN * xp[0] / A_MULTIPLIER;
    return E18() * (xp0_A + Dr * xp[0] / xp[1]) / (xp0_A + Dr);
}

// ---- pool state -------------------------------------------------------------

enum class Flavor { YB, IREET };

// Engine-contract-v2 revenue meter. Lives INSIDE Pool on purpose: the driver
// snapshots/restores Pool around every event, so a reverted event accrues
// nothing (and the remove_imbalance "try i=0 then i=1" probe cannot double
// count). Never read by the consensus math.
struct Meters {
    I fee[2]{};        // gross fee charged, coin units (swap fees only)
    I fee_lp{0};       // gross d_token fee, LP-token units (adds/withdrawals)
    I admin[2]{};      // coins transferred to the fee receiver by the claim
    I vol[2]{};        // gross exchange input volume, per coin
};

struct Pool {
    Flavor flavor = Flavor::YB;

    I prec[2];                                  // PRECISIONS immutable
    I balances[2]{};

    I price_scale{0}, price_oracle{0}, last_prices{0}, last_timestamp{0};

    // A/gamma ramp (unpacked; on-chain packed (A << 128) | gamma)
    I initial_A{0}, initial_gamma{0}, initial_A_gamma_time{0};
    I future_A{0}, future_gamma{0}, future_A_gamma_time{0};

    I mid_fee{0}, out_fee{0}, fee_gamma{0};          // packed_fee_params
    I allowed_extra_profit{0}, adjustment_step{0}, ma_time{0};  // rebalancing

    I admin_fee{0};                                  // 1e10

    I D{0}, xcp_profit{0}, xcp_profit_a{0}, virtual_price{0}, total_supply{0};

    I donation_shares{0}, donation_shares_max_ratio{0};
    I donation_duration{0}, last_donation_release_ts{0};
    I dp_expiry_ts{0}, dp_period{0}, dp_lp_threshold{0};

    I last_admin_fee_claim_ts{0};
    bool fee_receiver_set = true;

    Meters m;                                        // contract-v2 meter
};

// Twocrypto._A_gamma() at timestamp ts
inline void A_gamma_now(const Pool& p, const I& ts, I out[2]) {
    I t1 = p.future_A_gamma_time;
    I A1 = p.future_A, g1 = p.future_gamma;
    if (ts < t1) {
        const I& A0 = p.initial_A;
        const I& g0 = p.initial_gamma;
        I t0 = p.initial_A_gamma_time;
        t1 = csub(t1, t0);
        I t0e = csub(ts, t0);
        I t2 = csub(t1, t0e);
        A1 = (A0 * t2 + A1 * t0e) / t1;
        g1 = (g0 * t2 + g1 * t0e) / t1;
    }
    out[0] = A1;
    out[1] = g1;
}

// Twocrypto._is_ramping(): compares against last_timestamp, NOT block time
inline bool is_ramping(const Pool& p) {
    return p.future_A_gamma_time > p.last_timestamp;
}

// Twocrypto._xp
inline void xp_of(const Pool& p, const I bal[2], const I& price_scale, I out[2]) {
    out[0] = bal[0] * p.prec[0];
    out[1] = bal[1] * p.prec[1] * price_scale / E18();
}

// Twocrypto._xcp: D * 1e18 // 2 // isqrt(1e18 * price_scale)
inline I xcp_of(const I& D, const I& price_scale) {
    return D * E18() / 2 / cm::isqrt_u(E18() * price_scale);
}

// Twocrypto._fee — NO clamp in this version
inline I fee_of(const Pool& p, const I xp[2]) {
    I B = xp[0] + xp[1];
    B = E18() * 4 * xp[0] / B * xp[1] / B;
    B = p.fee_gamma * B / (p.fee_gamma * B / E18() + E18() - B);
    return (p.mid_fee * B + p.out_fee * (E18() - B)) / E18();
}

// Twocrypto._get_D
inline I get_D_of(const Pool& p, const I A_gamma[2], const I xp[2]) {
    if (is_ramping(p)) {
        I x[2] = {xp[0], xp[1]};
        return cm::ss_newton_D(A_gamma[0], x, /*check_balance=*/false);
    }
    return p.D;
}

// MATH.newton_D shim: v0.1.0 has no "!balance" guard
inline I math_newton_D(const I& A, const I xp[2], const I& /*K0_prev*/) {
    I x[2] = {xp[0], xp[1]};
    return cm::ss_newton_D(A, x, /*check_balance=*/false);
}

// Twocrypto._donation_shares(_donation_protection)
inline I donation_shares_of(const Pool& p, const I& ts, bool protection) {
    const I& shares = p.donation_shares;
    if (shares == 0) return 0;

    I elapsed = csub(ts, p.last_donation_release_ts);
    // ireet spells this unsafe_div — donation_duration > 0 always (asserted
    // in set_donation_duration), so values agree; a 0 divisor throws here.
    I unlocked = minI(shares, shares * elapsed / p.donation_duration);
    if (!protection) return unlocked;

    I protection_factor = 0;
    if (p.dp_expiry_ts > ts)
        protection_factor = minI((p.dp_expiry_ts - ts) * E18() / p.dp_period,
                                 E18());
    return unlocked * (E18() - protection_factor) / E18();
}

// Twocrypto._calc_token_fee(amounts, xp, donation, deposit), from_view=False
inline I calc_token_fee(const Pool& p, const I& ts, const I amounts_in[2],
                        const I xp[2], bool donation, bool deposit) {
    if (donation) return NOISE_FEE();

    I balances_ratio =
        csub(p.balances[0], amounts_in[0]) * p.prec[0] * E18() /
        (csub(p.balances[1], amounts_in[1]) * p.prec[1]);

    I amounts[2];
    amounts[0] = amounts_in[0] * p.prec[0];
    amounts[1] = amounts_in[1] * p.prec[1] * balances_ratio / E18();

    I fee = fee_of(p, xp) * 2 / 4;      // fee * N // (4 * (N - 1))

    I Ssum = amounts[0] + amounts[1];
    I avg = Ssum / 2;
    I Sdiff = 0;
    for (int k = 0; k < 2; ++k)
        Sdiff += amounts[k] > avg ? I(amounts[k] - avg) : I(avg - amounts[k]);

    I lp_spam_penalty_fee = 0;
    if (deposit && p.dp_expiry_ts > ts) {
        I protection_factor =
            minI((p.dp_expiry_ts - ts) * E18() / p.dp_period, E18());
        if (p.flavor == Flavor::YB) {
            lp_spam_penalty_fee = protection_factor * fee / E18();
        } else {
            // ireet: min(fee, pf*fee*donation_shares // totalSupply
            //                  // donation_shares_max_ratio)
            lp_spam_penalty_fee = minI(
                fee,
                protection_factor * fee * p.donation_shares / p.total_supply /
                    p.donation_shares_max_ratio);
        }
    }
    return fee * Sdiff / Ssum + NOISE_FEE() + lp_spam_penalty_fee;
}

// Twocrypto.tweak_price — mutates p; returns the resulting price_scale
inline I tweak_price(Pool& p, const I& ts, const I A_gamma[2], const I _xp[2],
                     const I& D_in) {
    I price_oracle = p.price_oracle;
    I last_prices = p.last_prices;
    I price_scale = p.price_scale;
    bool ramping = is_ramping(p);   // read BEFORE last_timestamp is bumped

    // ------------------ EMA oracle (once per block) --------------------------
    I last_timestamp = p.last_timestamp;   // kept: also gates the rebalance
    if (last_timestamp < ts) {
        I alpha = wad_exp(-I(csub(ts, last_timestamp) * E18() / p.ma_time));
        price_oracle =
            (minI(last_prices, 2 * price_scale) * (E18() - alpha) +
             price_oracle * alpha) /
            E18();
        p.price_oracle = price_oracle;
        p.last_timestamp = ts;
    }

    // spot price after this op
    p.last_prices = get_p(_xp, D_in, A_gamma[0]) * price_scale / E18();

    // ------------------ profit numbers ---------------------------------------
    I total_supply = p.total_supply;
    I donation_shares = donation_shares_of(p, ts, true);
    I locked_supply = csub(total_supply, donation_shares);

    I old_virtual_price = p.virtual_price;
    I xcp = xcp_of(D_in, price_scale);
    if (total_supply == 0) throw std::runtime_error("division by zero");
    I virtual_price = E18() * xcp / total_supply;

    if (virtual_price < old_virtual_price) {
        if (!ramping) throw std::runtime_error("virtual price decreased");
    }

    I xcp_profit = csub(p.xcp_profit + virtual_price, old_virtual_price);
    p.xcp_profit = xcp_profit;

    // ------------------ rebalance attempt ------------------------------------
    I threshold_vp = maxI(E18(), (xcp_profit + E18()) / 2);
    if (locked_supply == 0) throw std::runtime_error("division by zero");
    I vp_boosted = E18() * xcp / locked_supply;
    if (!(vp_boosted >= virtual_price))
        throw std::runtime_error("negative donation");

    if (vp_boosted > threshold_vp + p.allowed_extra_profit &&
        last_timestamp < ts) {
        I norm = mask256(price_oracle * E18()) / price_scale;
        norm = norm > E18() ? I(norm - E18()) : I(E18() - norm);

        // yb: max(param, norm/5)   ireet: min(param, norm/5)
        I step = (p.flavor == Flavor::YB)
                     ? maxI(p.adjustment_step, norm / 5)
                     : minI(p.adjustment_step, norm / 5);

        if (norm > step) {
            I p_new = (price_scale * (norm - step) + step * price_oracle) /
                      norm;

            I xp2[2] = {_xp[0], _xp[1] * p_new / price_scale};
            I new_D = math_newton_D(A_gamma[0], xp2, 0);
            I new_xcp = xcp_of(new_D, p_new);
            I new_virtual_price = E18() * new_xcp / total_supply;

            I donation_shares_to_burn = 0;
            I goal_vp = maxI(threshold_vp, virtual_price);
            if (new_virtual_price < goal_vp) {
                I tweaked_supply = E18() * new_xcp / goal_vp;
                if (!(tweaked_supply < total_supply))
                    throw std::runtime_error("tweaked supply must shrink");
                donation_shares_to_burn =
                    minI(total_supply - tweaked_supply, donation_shares);
                new_virtual_price =
                    E18() * new_xcp / (total_supply - donation_shares_to_burn);
            }

            if (new_virtual_price > E18() &&
                new_virtual_price >= threshold_vp) {
                p.D = new_D;
                p.virtual_price = new_virtual_price;
                p.price_scale = p_new;

                if (donation_shares_to_burn > 0) {
                    I shares_unlocked = donation_shares_of(p, ts, false);
                    const I& shares_available = donation_shares;
                    I shares_unlocked_new = csub(
                        shares_unlocked, donation_shares_to_burn *
                                             shares_unlocked /
                                             shares_available);
                    I new_total =
                        csub(p.donation_shares, donation_shares_to_burn);
                    I new_elapsed = 0;
                    if (new_total > 0 && shares_unlocked_new > 0)
                        new_elapsed = shares_unlocked_new *
                                      p.donation_duration / new_total;
                    p.donation_shares = new_total;
                    p.total_supply =
                        csub(p.total_supply, donation_shares_to_burn);
                    p.last_donation_release_ts = csub(ts, new_elapsed);
                }
                return p_new;
            }
        }
    }

    // no price adjustment
    p.D = D_in;
    p.virtual_price = virtual_price;
    return price_scale;
}

// Twocrypto._claim_admin_fees (only from _remove_liquidity_fixed_out).
// Returns {claimed?, tokens[2]} info via out params; mutates p.
inline bool claim_admin_fees(Pool& p, const I& ts, I admin_tokens[2]) {
    admin_tokens[0] = 0;
    admin_tokens[1] = 0;

    // unsafe_sub wrap: last_claim > ts would wrap huge -> gate passes; keep
    // the wrapping semantics (never triggers on real histories).
    I diff = (ts >= p.last_admin_fee_claim_ts)
                 ? I(ts - p.last_admin_fee_claim_ts)
                 : I(ts - p.last_admin_fee_claim_ts + (I(1) << 256));
    if (diff < MIN_ADMIN_FEE_CLAIM_INTERVAL || is_ramping(p)) return false;

    I xcp_profit = p.xcp_profit;
    I xcp_profit_a = p.xcp_profit_a;
    I current_lp_token_supply = p.total_supply;
    if (xcp_profit <= xcp_profit_a || current_lp_token_supply < E18())
        return false;

    I D = p.D;
    I vprice = p.virtual_price;
    I price_scale = p.price_scale;

    I fees = csub(xcp_profit, xcp_profit_a) * p.admin_fee /
             (2 * FEE_PRECISION());

    I admin_share = 0;
    if (p.fee_receiver_set && fees > 0) {
        I frac = csub(vprice * E18() / csub(vprice, fees), E18());
        admin_share += current_lp_token_supply * frac / E18();
        xcp_profit = csub(xcp_profit, fees * 2);
    }

    I total_supply_including_admin_share = current_lp_token_supply + admin_share;
    vprice = E18() * xcp_of(D, price_scale) / total_supply_including_admin_share;

    if (vprice < E18()) return false;      // bail: NO state was written

    p.xcp_profit = xcp_profit;
    p.last_admin_fee_claim_ts = ts;
    p.virtual_price = vprice;
    p.D = csub(D, D * admin_share / total_supply_including_admin_share);

    if (xcp_profit > xcp_profit_a) p.xcp_profit_a = xcp_profit;

    if (admin_share > 0) {
        for (int i = 0; i < 2; ++i) {
            admin_tokens[i] = p.balances[i] * admin_share /
                              total_supply_including_admin_share;
            p.balances[i] = csub(p.balances[i], admin_tokens[i]);
            p.m.admin[i] += admin_tokens[i];   // meter only
        }
        return true;   // ClaimAdminFee logged on-chain
    }
    return false;
}

// Twocrypto._withdraw_leftover_donations — returns true when the sweep fired
inline bool withdraw_leftover_donations(Pool& p, I swept[2]) {
    swept[0] = 0;
    swept[1] = 0;
    if (p.donation_shares != p.total_supply) return false;
    swept[0] = p.balances[0];
    swept[1] = p.balances[1];
    p.balances[0] = 0;
    p.balances[1] = 0;
    p.donation_shares = 0;
    p.total_supply = 0;
    p.D = 0;
    p.dp_expiry_ts = 0;
    return true;   // emits RemoveLiquidity(receiver, swept, 0) on-chain
}

// ---- event applications -----------------------------------------------------

// exchange(i, j, dx) — _transfer_in + _exchange + _transfer_out
inline json apply_exchange(Pool& p, int i, int j, const I& dx, const I& ts) {
    if (i < 0 || i > 1 || j < 0 || j > 1)
        throw std::runtime_error("coin index out of range");
    if (i == j) throw std::runtime_error("same coin");
    if (!(dx > 0)) throw std::runtime_error("zero dx");

    p.balances[i] += dx;                       // _transfer_in

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);

    I balances[2] = {p.balances[0], p.balances[1]};
    I y = balances[j];
    I x0 = csub(balances[i], dx);

    I price_scale = p.price_scale;
    I xp[2];
    xp_of(p, balances, price_scale, xp);

    if (is_ramping(p)) {
        x0 *= p.prec[i];
        if (i > 0) x0 = x0 * price_scale / E18();
        I x1 = xp[i];
        xp[i] = x0;
        p.D = math_newton_D(A_gamma[0], xp, 0);
        xp[i] = x1;
    }

    I D = p.D;
    I y_out0 = cm::ss_get_y(A_gamma[0], xp, D, j);   // MATH returns [y, 0]
    I dy = csub(xp[j], y_out0);
    xp[j] = csub(xp[j], dy);
    dy = csub(dy, 1);

    if (j > 0) dy = dy * E18() / price_scale;
    dy /= p.prec[j];

    I fee = fee_of(p, xp) * dy / FEE_PRECISION();
    dy = csub(dy, fee);
    y = csub(y, dy);

    p.m.vol[i] += dx;          // meter only
    p.m.fee[j] += fee;         // gross swap fee, output-coin units

    y *= p.prec[j];
    if (j > 0) y = y * price_scale / E18();
    xp[j] = y;

    D = math_newton_D(A_gamma[0], xp, /*K0_prev=*/0);   // K0_prev ignored

    I price_scale_new = tweak_price(p, ts, A_gamma, xp, D);

    p.balances[j] = csub(p.balances[j], dy);   // _transfer_out

    return json{{"dy", S(dy)},
                {"fee", S(fee)},
                {"price_scale", S(price_scale_new)},
                {"total_supply", S(p.total_supply)}};
}

// add_liquidity(amounts, donation)
inline json apply_add(Pool& p, const I amounts_in[2], bool donation, const I& ts) {
    if (!(amounts_in[0] + amounts_in[1] > 0)) throw std::runtime_error("!amounts");

    I old_balances[2] = {p.balances[0], p.balances[1]};

    I amounts_received[2] = {amounts_in[0], amounts_in[1]};
    for (int i = 0; i < 2; ++i) p.balances[i] += amounts_received[i];
    I balances[2] = {p.balances[0], p.balances[1]};

    I price_scale = p.price_scale;
    I xp[2], old_xp[2];
    xp_of(p, balances, price_scale, xp);
    xp_of(p, old_balances, price_scale, old_xp);

    // finalize ramping of empty pool
    if (p.D == 0) p.future_A_gamma_time = ts;

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);
    I old_D = get_D_of(p, A_gamma, old_xp);
    I D = math_newton_D(A_gamma[0], xp, 0);

    I token_supply = p.total_supply;
    I d_token;
    if (old_D > 0)
        d_token = csub(token_supply * D / old_D, token_supply);
    else
        d_token = xcp_of(D, price_scale);   // initial virtual price = 1

    if (!(d_token > 0)) throw std::runtime_error("nothing minted");

    I d_token_fee = 0;
    I price_scale_out = price_scale;

    if (old_D > 0) {
        d_token_fee =
            calc_token_fee(p, ts, amounts_received, xp, donation, true) *
                d_token / FEE_PRECISION() +
            1;
        d_token = csub(d_token, d_token_fee);
        p.m.fee_lp += d_token_fee;   // meter only (LP-token-denominated fee)

        if (donation) {
            I new_donation_shares = p.donation_shares + d_token;
            if (!(new_donation_shares * E18() / (token_supply + d_token) <=
                  p.donation_shares_max_ratio))
                throw std::runtime_error("donation above cap!");
            I new_elapsed = donation_shares_of(p, ts, false) *
                            p.donation_duration / new_donation_shares;
            p.last_donation_release_ts = csub(ts, new_elapsed);
            p.donation_shares = new_donation_shares;
            p.total_supply += d_token;
        } else {
            // donation protection extension (no remainder in this version)
            I relative_lp_add = d_token * E18() / (token_supply + d_token);
            if (relative_lp_add > 0 && p.donation_shares > 0) {
                I extension_seconds =
                    minI(relative_lp_add * p.dp_period / p.dp_lp_threshold,
                         p.dp_period);
                I current_expiry = maxI(p.dp_expiry_ts, ts);
                p.dp_expiry_ts =
                    minI(current_expiry + extension_seconds, ts + p.dp_period);
            }
            p.total_supply += d_token;             // mint(receiver, d_token)
        }

        price_scale_out = tweak_price(p, ts, A_gamma, xp, D);
    } else {
        // (re)instantiating an empty pool (no MINIMUM_LIQUIDITY in v2.1.0d)
        p.D = D;
        p.virtual_price = E18();
        p.xcp_profit = E18();
        p.xcp_profit_a = E18();
        p.total_supply += d_token;                 // mint(receiver, d_token)
    }

    // AddLiquidity logs token_supply = pre-op supply + d_token (note 6)
    return json{{"minted", S(d_token)},
                {"fee", S(d_token_fee)},
                {"supply", S(token_supply + d_token)},
                {"price_scale", S(price_scale_out)},
                {"total_supply", S(p.total_supply)}};
}

// remove_liquidity(amount) — balanced, no fees, D scaled down, no tweak
inline json apply_remove(Pool& p, const I& amount, const I& /*ts*/) {
    I total_supply = p.total_supply;
    p.total_supply = csub(p.total_supply, amount);   // burnFrom

    I withdraw_amounts[2];
    I D = p.D;

    if (amount == total_supply) {                    // Case 2: empty pool
        for (int i = 0; i < 2; ++i) withdraw_amounts[i] = p.balances[i];
    } else {                                         // Case 1
        for (int i = 0; i < 2; ++i)
            withdraw_amounts[i] = p.balances[i] * amount / total_supply;
    }

    if (total_supply == 0) throw std::runtime_error("division by zero");
    p.D = csub(D, D * amount / total_supply);        // unsafe_div, in-range

    for (int i = 0; i < 2; ++i)
        p.balances[i] = csub(p.balances[i], withdraw_amounts[i]);

    json out{{"amounts", json::array({S(withdraw_amounts[0]),
                                      S(withdraw_amounts[1])})},
             {"supply", S(csub(total_supply, amount))}};

    I swept[2];
    if (withdraw_leftover_donations(p, swept))
        out["sweep_amounts"] = json::array({S(swept[0]), S(swept[1])});
    return out;
}

// _calc_withdraw_fixed_out — pure computation, no state writes
inline void calc_withdraw_fixed_out(const Pool& p, const I A_gamma[2],
                                    const I& lp_token_amount, int i,
                                    const I& amount_i, I& dy_out, I& D_out,
                                    I xp_out[2], I& approx_fee_out) {
    I token_supply = p.total_supply;
    if (!(lp_token_amount <= token_supply)) throw std::runtime_error("!amount");
    if (i < 0 || i > 1) throw std::runtime_error("coin index out of range");
    int j = 1 - i;

    I balances[2] = {p.balances[0], p.balances[1]};
    I price_scale = p.price_scale;
    I xp[2];
    xp_of(p, balances, price_scale, xp);
    I D = get_D_of(p, A_gamma, xp);

    if (token_supply == 0) throw std::runtime_error("division by zero");
    I dD = lp_token_amount * D / token_supply;
    I xp_new[2] = {xp[0], xp[1]};

    I price_scales[2] = {E18() * p.prec[0], price_scale * p.prec[1]};

    I amountsp[2] = {I(0), I(0)};
    amountsp[i] = amount_i * price_scales[i] / E18();   // FLOOR (no ceil)
    xp_new[i] = csub(xp_new[i], amountsp[i]);

    I y = cm::ss_get_y(A_gamma[0], xp_new, csub(D, dD), j);   // no +1
    amountsp[j] = csub(xp[j], y);
    xp_new[j] = y;

    I amounts[2] = {I(0), I(0)};
    amounts[i] = amount_i;
    if (i == 0)
        amounts[1] = amountsp[1] * E18() / p.prec[1] / price_scale;
    else
        amounts[0] = amountsp[0] / p.prec[0];

    if (!(amounts[0] + amounts[1] > 0)) throw std::runtime_error("!tokens");

    // donation=False, deposit=False -> ts-dependent penalty never triggers
    I approx_fee = calc_token_fee(p, /*ts*/ 0, amounts, xp_new, false, false);

    dD = csub(dD, dD * approx_fee / FEE_PRECISION() + 1);

    y = cm::ss_get_y(A_gamma[0], xp_new, csub(D, dD), j);     // no +1
    I dy = csub(xp[j], y) * E18() / price_scales[j];
    xp_new[j] = y;

    dy_out = dy;
    D_out = csub(D, dD);
    xp_out[0] = xp_new[0];
    xp_out[1] = xp_new[1];
    approx_fee_out = approx_fee;
}

// remove_liquidity_fixed_out / remove_liquidity_one_coin.
// `i` is the INTERNAL index (the coin with the fixed amount_i); one-coin
// withdrawals of user coin u call with i = 1 - u, amount_i = 0.
inline json apply_remove_fixed_out(Pool& p, const I& token_amount, int i,
                                   const I& amount_i, const I& ts) {
    I admin_tokens[2];
    bool claimed = claim_admin_fees(p, ts, admin_tokens);

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);

    I dy, D, xp[2], approx_fee;
    calc_withdraw_fixed_out(p, A_gamma, token_amount, i, amount_i, dy, D, xp,
                            approx_fee);

    p.m.fee_lp += approx_fee * token_amount / FEE_PRECISION() + 1;  // meter

    p.total_supply = csub(p.total_supply, token_amount);   // burnFrom

    I price_scale_new = tweak_price(p, ts, A_gamma, xp, D);

    int j = 1 - i;
    if (amount_i != 0) p.balances[i] = csub(p.balances[i], amount_i);
    p.balances[j] = csub(p.balances[j], dy);

    I amounts_out[2] = {I(0), I(0)};
    amounts_out[i] = amount_i;
    amounts_out[j] = dy;

    json out{{"dy", S(dy)},
             {"amounts", json::array({S(amounts_out[0]), S(amounts_out[1])})},
             // approx_fee as logged: LP-token units
             {"approx_fee", S(approx_fee * token_amount / FEE_PRECISION() + 1)},
             {"price_scale", S(price_scale_new)},
             {"total_supply", S(p.total_supply)}};
    if (claimed)
        out["claim"] = json{{"tokens", json::array({S(admin_tokens[0]),
                                                    S(admin_tokens[1])})}};

    I swept[2];
    if (withdraw_leftover_donations(p, swept))
        out["sweep_amounts"] = json::array({S(swept[0]), S(swept[1])});
    return out;
}

// ramp_A_gamma(future_A, future_gamma, future_time)
inline json apply_ramp_ag(Pool& p, const I& future_A, const I& future_gamma,
                          const I& future_time, const I& ts) {
    if (is_ramping(p)) throw std::runtime_error("!ramp");
    if (!(future_time > ts + MIN_RAMP_TIME - 1))
        throw std::runtime_error("ramp time<min");

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);

    if (!(future_A > MIN_A - 1)) throw std::runtime_error("A<min");
    if (!(future_A < MAX_A() + 1)) throw std::runtime_error("A>max");
    if (!(future_gamma > MIN_GAMMA() - 1)) throw std::runtime_error("gamma<min");
    if (!(future_gamma < MAX_GAMMA() + 1)) throw std::runtime_error("gamma>max");

    I ratio = E18() * future_A / A_gamma[0];
    if (!(ratio < E18() * MAX_PARAM_CHANGE + 1))
        throw std::runtime_error("A too high");
    if (!(ratio > E18() / MAX_PARAM_CHANGE - 1))
        throw std::runtime_error("A too low");
    ratio = E18() * future_gamma / A_gamma[1];
    if (!(ratio < E18() * MAX_PARAM_CHANGE + 1))
        throw std::runtime_error("gamma too high");
    if (!(ratio > E18() / MAX_PARAM_CHANGE - 1))
        throw std::runtime_error("gamma too low");

    p.initial_A = A_gamma[0];
    p.initial_gamma = A_gamma[1];
    p.initial_A_gamma_time = ts;
    p.future_A = future_A;
    p.future_gamma = future_gamma;
    p.future_A_gamma_time = future_time;
    return json::object();
}

// stop_ramp_A_gamma()
inline json apply_stop_ramp_ag(Pool& p, const I& ts) {
    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);
    p.initial_A = A_gamma[0];
    p.initial_gamma = A_gamma[1];
    p.future_A = A_gamma[0];
    p.future_gamma = A_gamma[1];
    p.initial_A_gamma_time = ts;
    p.future_A_gamma_time = ts;
    return json::object();
}

// ---- engine contract v2: spot price, ps gap, probes -------------------------

// REAL, fee-free marginal price of coin 1 in coin 0 units, 1e18-scaled.
// _xp() folds PRECISIONS and price_scale in and get_p returns d(xp0)/d(xp1);
// undoing the price_scale leg turns that into the whole-token price — exactly
// how the pool derives last_prices. 0 for a degenerate/empty pool.
// Never throws: the meter must not be able to fail a replay that would
// otherwise succeed (e.g. an event with no "ts" against a ramping pool).
inline I spot_of(const Pool& p, const I& ts) {
    try {
        if (p.D == 0 || p.price_scale == 0) return 0;
        I A_gamma[2];
        A_gamma_now(p, ts, A_gamma);
        if (A_gamma[0] <= 0) return 0;
        I xp[2];
        xp_of(p, p.balances, p.price_scale, xp);
        if (xp[0] <= 0 || xp[1] <= 0) return 0;
        return get_p(xp, p.D, A_gamma[0]) * p.price_scale / E18();
    } catch (const std::exception&) {
        return 0;
    }
}

// |spot * 1e18 / price_scale - 1e18| expressed in basis points
inline I ps_gap_bp(const I& spot, const I& price_scale) {
    if (price_scale == 0 || spot == 0) return 0;
    I r = spot * E18() / price_scale;
    I d = r > E18() ? I(r - E18()) : I(E18() - r);
    return d * 10000 / E18();
}

// live virtual price (== get_virtual_price()), 1e18-scaled
inline I live_vp(const Pool& p) {
    if (p.total_supply == 0 || p.price_scale == 0) return 0;
    return E18() * xcp_of(p.D, p.price_scale) / p.total_supply;
}

// no "adm": v2.1.0d has no admin_balances bucket
inline json make_probe(const Pool& p, const I& ts, int idx) {
    I sp = spot_of(p, ts);
    return json{
        {"i", idx},
        {"bal", json::array({S(p.balances[0]), S(p.balances[1])})},
        {"sup", S(p.total_supply)},
        {"D", S(p.D)},
        {"vp", S(live_vp(p))},
        {"xcp", S(p.xcp_profit)},
        {"ps", json::array({S(p.price_scale)})},
        {"spot", json::array({S(sp)})},
        {"ps_gap_bp", ps_gap_bp(sp, p.price_scale).convert_to<long long>()},
        // cumulative meter as of this event — mirrors result["meter"]'s
        // fee / fee_lp / admin / admin_lp / vol exactly (last probe == totals).
        // "cadm_lp" is always "0": this vintage never mints LP for the DAO.
        {"cfee", json::array({S(p.m.fee[0]), S(p.m.fee[1])})},
        {"cfee_lp", S(p.m.fee_lp)},
        {"cadm", json::array({S(p.m.admin[0]), S(p.m.admin[1])})},
        {"cadm_lp", "0"},
        {"cvol", json::array({S(p.m.vol[0]), S(p.m.vol[1])})},
        // extra (not in the contract, but this fork needs it to make sense of
        // "sup"/"vp"): LP supply held by the donation buffer at this point.
        {"don", S(p.donation_shares)}};
}

}  // namespace tng

// ---- entry point ------------------------------------------------------------

inline nlohmann::json run_twocrypto_ng(const nlohmann::json& job) {
    using tng::I;
    using tng::ju;
    using tng::jfield;
    using tng::S;
    using json = nlohmann::json;

    cm::req(job.at("n").get<int>() == 2, "twocrypto_ng needs n == 2");

    tng::Pool p;

    if (job.contains("flavor")) {
        const std::string f = job.at("flavor").get<std::string>();
        if (f == "yb") p.flavor = tng::Flavor::YB;
        else if (f == "ireet") p.flavor = tng::Flavor::IREET;
        else throw std::runtime_error("unknown flavor: " + f);
    }

    if (job.contains("precisions")) {
        const json& pr = job.at("precisions");
        p.prec[0] = ju(pr.at(0));
        p.prec[1] = ju(pr.at(1));
    } else {
        const json& dec = job.at("decimals");
        for (int i = 0; i < 2; ++i) {
            int d = dec.at(static_cast<size_t>(i)).get<int>();
            if (d < 0 || d > 18) throw std::runtime_error("bad decimals");
            p.prec[i] = cm::pow_int(10, 18 - d);
        }
    }

    const json& prm = job.at("params");
    p.initial_A = ju(prm.at("A"));
    p.initial_gamma = ju(prm.at("gamma"));
    p.future_A = prm.contains("future_A") ? ju(prm.at("future_A")) : p.initial_A;
    p.future_gamma = prm.contains("future_gamma") ? ju(prm.at("future_gamma"))
                                                  : p.initial_gamma;
    p.initial_A_gamma_time = jfield(prm, "initial_A_gamma_time", 0);
    p.future_A_gamma_time = jfield(prm, "future_A_gamma_time", 0);
    p.mid_fee = ju(prm.at("mid_fee"));
    p.out_fee = ju(prm.at("out_fee"));
    p.fee_gamma = ju(prm.at("fee_gamma"));
    p.allowed_extra_profit = ju(prm.at("allowed_extra_profit"));
    p.adjustment_step = ju(prm.at("adjustment_step"));
    p.ma_time = ju(prm.at("ma_time"));       // raw stored value (seconds/ln2)
    p.admin_fee = jfield(prm, "admin_fee", 0);
    p.donation_duration = jfield(prm, "donation_duration", I(604800));
    p.donation_shares_max_ratio =
        jfield(prm, "donation_shares_max_ratio", I("100000000000000000"));
    p.dp_period = jfield(prm, "donation_protection_period", I(600));
    p.dp_lp_threshold =
        jfield(prm, "donation_protection_lp_threshold", I("200000000000000000"));
    if (prm.contains("fee_receiver_set"))
        p.fee_receiver_set = prm.at("fee_receiver_set").get<bool>();

    const json& st = job.at("state");
    {
        const json& b = st.at("balances");
        p.balances[0] = ju(b.at(0));
        p.balances[1] = ju(b.at(1));
    }
    p.price_scale = tng::jprice(st.at("price_scale"));
    p.price_oracle = tng::jprice(st.at("price_oracle"));
    p.last_prices = tng::jprice(st.at("last_prices"));
    p.last_timestamp = ju(st.at("last_timestamp"));
    p.D = ju(st.at("D"));
    p.xcp_profit = ju(st.at("xcp_profit"));
    p.xcp_profit_a = jfield(st, "xcp_profit_a", tng::E18());
    p.virtual_price = ju(st.at("virtual_price"));
    p.total_supply = ju(st.at("total_supply"));
    p.donation_shares = jfield(st, "donation_shares", 0);
    p.last_donation_release_ts = jfield(st, "last_donation_release_ts", 0);
    p.dp_expiry_ts = jfield(st, "donation_protection_expiry_ts", 0);
    p.last_admin_fee_claim_ts = jfield(st, "last_admin_fee_claim_timestamp", 0);

    // ---- engine contract v2 job flags (all default OFF) --------------------
    auto truthy = [](const json& v) {
        if (v.is_boolean()) return v.get<bool>();
        if (v.is_number_unsigned()) return v.get<std::uint64_t>() != 0;
        if (v.is_number_integer()) return v.get<long long>() != 0;
        return false;
    };
    auto jbool = [&](const char* k) {
        return job.contains(k) && truthy(job.at(k));
    };
    const bool probe_all = jbool("probe_all");
    const bool probe_last = jbool("probe_last");
    const bool cf_mode = jbool("cf");

    json out_events = json::array();
    json probes = json::array();
    bool any_probe = false;
    long long n_events = 0, n_reverts = 0;
    I max_ps_gap = 0;

    const auto& events = job.at("events");
    const std::size_t n_ev_total = events.size();
    std::size_t ev_idx = 0;

    for (const auto& ev : events) {
        const std::string type = ev.at("type").get<std::string>();
        I ts = ev.contains("ts") ? ju(ev.at("ts")) : I(0);

        // cf mode: burn amounts become fractions of the LIVE supply (the
        // historical absolute burn / supply_after describes a state path that
        // no longer exists). Outside cf mode burn_frac is ignored entirely.
        auto burn_of = [&](const char* abs_key) {
            if (cf_mode && ev.contains("burn_frac"))
                return I(p.total_supply * ju(ev.at("burn_frac")) / tng::E18());
            return ju(ev.at(abs_key));
        };

        tng::Pool snapshot = p;   // restored on revert
        bool reverted = false, skipped = false;

        try {
            json outputs;

            if (type == "exchange") {
                outputs = tng::apply_exchange(p, ev.at("sold_id").get<int>(),
                                              ev.at("bought_id").get<int>(),
                                              ju(ev.at("dx")), ts);
            } else if (type == "add" || type == "donation") {
                I amounts[2];
                const json& am = ev.at("amounts");
                amounts[0] = ju(am.at(0));
                amounts[1] = ju(am.at(1));
                bool donation = (type == "donation") ||
                                (ev.contains("donation") &&
                                 ev.at("donation").get<bool>());
                outputs = tng::apply_add(p, amounts, donation, ts);
            } else if (type == "remove") {
                I amount;
                if (cf_mode && ev.contains("burn_frac"))
                    amount = burn_of("burn");
                else if (ev.contains("burn")) amount = ju(ev.at("burn"));
                else amount = tng::csub(p.total_supply, ju(ev.at("supply_after")));
                outputs = tng::apply_remove(p, amount, ts);
            } else if (type == "remove_one") {
                // i = the coin the user withdraws (log coin_index);
                // internal index is flipped, amount_i = 0
                int ui = ev.at("i").get<int>();
                if (ui < 0 || ui > 1)
                    throw std::runtime_error("coin index out of range");
                outputs = tng::apply_remove_fixed_out(p, burn_of("burn"),
                                                      1 - ui, I(0), ts);
            } else if (type == "remove_imb" || type == "remove_fixed_out") {
                // cf mode: burn_frac may re-scale the LP leg, but amount_i
                // stays an absolute coin amount (the path is defined by its
                // fixed output, not by a supply share) — if it no longer fits
                // the event reverts, which is the intended signal.
                if (ev.contains("i") && ev.contains("amount_i")) {
                    outputs = tng::apply_remove_fixed_out(
                        p, burn_of("burn"), ev.at("i").get<int>(),
                        ju(ev.at("amount_i")), ts);
                } else {
                    // RemoveLiquidityImbalance logs BOTH final amounts but
                    // not which leg the caller fixed — try i=0, accept if
                    // the derived other leg matches, else i=1.
                    const json& am = ev.at("amounts_expected");
                    I a0 = ju(am.at(0)), a1 = ju(am.at(1));
                    tng::Pool snap2 = p;
                    I bn = burn_of("burn");
                    bool ok0 = false;
                    try {
                        outputs = tng::apply_remove_fixed_out(p, bn, 0, a0, ts);
                        ok0 = outputs.contains("amounts") &&
                              ju(outputs.at("amounts").at(1)) == a1;
                    } catch (const std::exception&) {
                        ok0 = false;
                    }
                    if (!ok0) {
                        p = snap2;
                        outputs = tng::apply_remove_fixed_out(p, bn, 1, a1, ts);
                        outputs["inferred_i"] = 1;
                    } else {
                        outputs["inferred_i"] = 0;
                    }
                }
            } else if (type == "ramp_ag") {
                outputs = tng::apply_ramp_ag(p, ju(ev.at("future_A")),
                                             ju(ev.at("future_gamma")),
                                             ju(ev.at("future_time")), ts);
            } else if (type == "stop_ramp_ag") {
                outputs = tng::apply_stop_ramp_ag(p, ts);
            } else if (type == "new_params") {
                // NewParameters logs FINAL values (sentinels resolved)
                p.mid_fee = ju(ev.at("mid_fee"));
                p.out_fee = ju(ev.at("out_fee"));
                p.fee_gamma = ju(ev.at("fee_gamma"));
                p.allowed_extra_profit = ju(ev.at("allowed_extra_profit"));
                p.adjustment_step = ju(ev.at("adjustment_step"));
                p.ma_time = ju(ev.at("ma_time"));
                outputs = json::object();
            } else if (type == "set_donation_duration") {
                I d = ju(ev.at("duration"));
                if (!(d > 0)) throw std::runtime_error("!duration");
                p.donation_duration = d;
                outputs = json::object();
            } else if (type == "set_donation_protection") {
                I per = ju(ev.at("period"));
                I thr = ju(ev.at("lp_threshold"));
                I mx = ju(ev.at("max_ratio"));
                if (!(per > 0)) throw std::runtime_error("!period");
                if (!(thr > 0)) throw std::runtime_error("!threshold");
                if (!(mx > 0)) throw std::runtime_error("!max_shares");
                p.dp_period = per;
                p.dp_lp_threshold = thr;
                p.donation_shares_max_ratio = mx;
                outputs = json::object();
            } else if (type == "set_admin_fee") {
                I af = ju(ev.at("admin_fee"));
                if (!(af <= tng::MAX_ADMIN_FEE()))
                    throw std::runtime_error("admin_fee>MAX");
                p.admin_fee = af;
                outputs = json::object();
            } else {
                // incl. "sweep_log" (note 9) and "set_periphery" (note 15)
                out_events.push_back(json{{"type", type}, {"skipped", true}});
                skipped = true;
            }

            if (!skipped)
                out_events.push_back(json{{"type", type}, {"outputs", outputs}});
        } catch (const std::exception& e) {
            p = snapshot;   // restore pre-event state
            reverted = true;
            out_events.push_back(
                json{{"type", type}, {"revert", std::string(e.what())}});
        }

        // ---- engine contract v2 bookkeeping (never touches pool state) ----
        ++n_events;
        if (reverted) ++n_reverts;
        {
            I gap = tng::ps_gap_bp(tng::spot_of(p, ts), p.price_scale);
            if (gap > max_ps_gap) max_ps_gap = gap;
        }
        bool want_probe =
            probe_all || (probe_last && ev_idx + 1 == n_ev_total) ||
            (ev.contains("probe") && truthy(ev.at("probe")));
        if (want_probe) {
            any_probe = true;
            probes.push_back(tng::make_probe(p, ts, static_cast<int>(ev_idx)));
        }
        ++ev_idx;
    }

    // packed views of the param slots, for raw-storage comparison
    I packed_reb = (p.allowed_extra_profit << 128) |
                   (p.adjustment_step << 64) | p.ma_time;
    I packed_fee = (p.mid_fee << 128) | (p.out_fee << 64) | p.fee_gamma;
    I initial_ag = (p.initial_A << 128) | p.initial_gamma;
    I future_ag = (p.future_A << 128) | p.future_gamma;

    nlohmann::json result;
    result["events"] = out_events;
    result["final"] = json{
        {"balances", json::array({S(p.balances[0]), S(p.balances[1])})},
        {"D", S(p.D)},
        {"price_scale", S(p.price_scale)},
        {"price_oracle", S(p.price_oracle)},
        {"last_prices", S(p.last_prices)},
        {"last_timestamp", S(p.last_timestamp)},
        {"xcp_profit", S(p.xcp_profit)},
        {"xcp_profit_a", S(p.xcp_profit_a)},
        {"virtual_price", S(p.virtual_price)},
        {"total_supply", S(p.total_supply)},
        {"donation_shares", S(p.donation_shares)},
        {"donation_shares_max_ratio", S(p.donation_shares_max_ratio)},
        {"donation_duration", S(p.donation_duration)},
        {"last_donation_release_ts", S(p.last_donation_release_ts)},
        {"donation_protection_expiry_ts", S(p.dp_expiry_ts)},
        {"donation_protection_period", S(p.dp_period)},
        {"donation_protection_lp_threshold", S(p.dp_lp_threshold)},
        {"last_admin_fee_claim_timestamp", S(p.last_admin_fee_claim_ts)},
        {"initial_A_gamma", S(initial_ag)},
        {"initial_A_gamma_time", S(p.initial_A_gamma_time)},
        {"future_A_gamma", S(future_ag)},
        {"future_A_gamma_time", S(p.future_A_gamma_time)},
        {"packed_rebalancing_params", S(packed_reb)},
        {"packed_fee_params", S(packed_fee)},
        {"admin_fee", S(p.admin_fee)}};

    // ---- engine contract v2 result additions -------------------------------
    // "admin" is coin-denominated (the DAO slice is transferred out of
    // balances, never minted as LP), so admin_lp is always 0; "fee_lp" carries
    // the LP-token-denominated add / withdraw fee (see header).
    result["meter"] = json{
        {"fee", json::array({S(p.m.fee[0]), S(p.m.fee[1])})},
        {"fee_lp", S(p.m.fee_lp)},
        {"admin", json::array({S(p.m.admin[0]), S(p.m.admin[1])})},
        {"admin_lp", "0"},
        {"vol", json::array({S(p.m.vol[0]), S(p.m.vol[1])})},
        {"n_events", n_events},
        {"n_reverts", n_reverts},
        {"max_ps_gap_bp", max_ps_gap.convert_to<long long>()}};
    if (any_probe) result["probes"] = probes;
    return result;
}
