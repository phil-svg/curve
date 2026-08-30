#pragma once
// ============================================================================
// twocrypto_yb.hpp — wei-exact replay state machine for the Yield Basis
// Twocrypto pools on ethereum mainnet (Curve "Twocrypto" v3.0.0, vyper 0.4.3).
//
// SOURCE FOLLOWED (authoritative, fetched 2026-08-28 from sourcify.dev,
// full verified match for 0x862Cb4..., chain 1):
//   Pool:  "Twocrypto" version = "v3.0.0", pragma vyper 0.4.3
//          0x862cb4e988fb66e72f128d1183829f8c05b6c6a0  (YB cbBTC)
//          0x656341ef90b622c6634e0573772ffb7f3669b9f3  (YB WETH)
//          0x313698667d7fdd6789a9bc70821309ff891e729a  (YB WBTC, POLICY set)
//          0x4f52c3a81e33521e5a9a47fd9d3be475d2279c2e  (YB tBTC)
//   Math:  "StableswapMath" v0.1.1 at 0xBfDdF58Cb6ef84e115fF47c10e49A80B2653EA13
//          (same MATH() on all four pools, verified via RPC 2026-08-28).
//          NOTE: despite the pools carrying gamma in storage, the deployed
//          math is STABLESWAP math — gamma and K0_prev are accepted and
//          IGNORED. newton_D / get_y are classic stableswap Newton with
//          A_MULTIPLIER = 10000, Ann = _amp * N_COINS, plus a "!balance"
//          guard (xp ratio < 10_000). get_p is the stableswap spot formula.
//          wad_exp is snekmate math._wad_exp (Remco/solmate polynomial).
//   Policy: "YBTwocryptoPolicy" at 0xE050e36847df1aEEC5C57414A6370DdDf2Ab7532
//          (dual-EMA price-scale controller; only the WBTC pool has it set).
//          get_fee() always returns 0 => pools always use the native dynamic
//          fee. get_price_scale()/update_pool_state() are modeled exactly.
//
// The older "YB *" family (0x83f24023d15d835a213df24fd309c47dab5beb32 etc.)
// is version "v2.1.0d" with a DIFFERENT tweak_price (no vp_preop) and a
// different, unverified math contract — it is NOT covered by this engine.
//
// Math kernels reused from crypto_math.hpp: cm::ss_newton_D (check_balance =
// true, the v0.1.1 guard), cm::ss_get_y. get_p and wad_exp are implemented
// here (crypto_math.hpp has neither).
//
// SEMANTIC DECISIONS / UNCERTAINTIES:
//  1. balances[] here is self.balances — the AMM balance. admin_balances are
//     a separate bucket already excluded from it (unlike stableswap-ng there
//     is no live/stored split; pool.balances(i) returns exactly this).
//  2. Slippage guards (min_dy / min_mint_amount / min_amounts) are not
//     replayed — the harness feeds events that succeeded on-chain.
//     The LP allowlist and deploy_eoa init gate are likewise skipped.
//  3. _fee(): when a policy is attached the pool staticcalls
//     POLICY.get_fee(xp); the only deployed policy is @pure `return 0`,
//     which makes the pool fall back to its native fee curve. We hardcode
//     get_fee = 0. If a future policy returns a nonzero fee this engine
//     will diverge (visible in the harness).
//  4. _claim_admin_fees() (called at the top of remove_one / fixed-out
//     withdrawals) zeroes admin_balances when >= 86400s elapsed since the
//     last claim and factory.fee_receiver() != 0. fee_receiver is set on
//     mainnet (0xa2Bcd1a4Efbd04B63cd03f5aFf2561106ebCCE00, checked
//     2026-08-28); job field "fee_receiver_set" (default true) can disable.
//  5. Vyper checked ops -> csub()/throw; a revert restores the pre-event
//     snapshot (pool AND policy state) and reports {"revert": msg}.
//     uint256 overflow of checked ops is not modeled (cpp_int is unbounded);
//     unreachable on real histories. Division by zero throws == revert.
//  6. wad_exp follows snekmate math._wad_exp bit-for-bit: SDIV truncates
//     toward zero, SAR (>>) floors, the final mul is mod 2^256 with a
//     logical shift. Inputs here are always <= 0.
//  7. ramp_ag recomputes current A/gamma at ts and stores it as the initial
//     point (identical to what the RampAgamma log carries); new_params /
//     set_donation_params / set_fee_params apply the FINAL values their logs
//     emit (sentinel resolution already happened on-chain).
//  8. Donation adds ("add" with donation:true, or type "donation"): supply
//     grows without a holder; donation_shares tracks the buffer. A donation
//     add also emits AddLiquidity on-chain (receiver 0x0) — feed it ONCE.
//  9. tweak_price may burn donation shares (totalSupply shrinks inside an
//     exchange/add/remove_one) — outputs report the post-tweak supply, which
//     matches the token_supply field of the on-chain logs.
//
// JOB SCHEMA ("kind":"twocrypto_yb","n":2,"decimals":[d0,d1]):
//  params: A, gamma (initial, unpacked), future_A, future_gamma,
//    initial_A_gamma_time, future_A_gamma_time,
//    mid_fee, out_fee, fee_gamma            (1e10 / 1e18 scales, as stored),
//    adjustment_step_min (alias allowed_extra_profit),
//    adjustment_step_max (alias adjustment_step), ma_time (raw, sec/ln2),
//    admin_fee, reserved_profit_fraction    (1e10),
//    donation_duration, donation_protection_period,
//    donation_protection_lp_threshold, donation_shares_max_ratio,
//    policy: null | {fast_half_life, slow_half_life, kappa, deadband,
//                    min_cap, max_cap}      (YBTwocryptoPolicy immutables)
//  state: balances[2], admin_balances[2] (default 0),
//    price_scale[1], price_oracle[1], last_prices[1], last_timestamp,
//    D, virtual_price, xcp_profit, lp_xcp_profit (fallback: xcp_profit_a,
//    else 1e18), total_supply,
//    donation_shares, last_donation_release_ts,
//    donation_protection_expiry_ts, donation_protection_extension_remainder,
//    last_admin_fee_claim_timestamp (default 0),
//    policy_state: {last_update_ts, last_prices, fast_ema, slow_ema,
//                   price_scale}            (default all-zero = fresh),
//    fee_receiver_set (bool, default true)
//  events (every event carries "ts" = block timestamp, applied before the
//  event since the EMA depends on it):
//    "exchange"     {sold_id, bought_id, dx} -> {dy, fee, price_scale:[1]}
//    "add"          {amounts:[2], donation?:bool}
//                   -> {minted, fee, supply, price_scale:[1]}
//    "donation"     alias of add with donation = true
//    "remove"       {supply_after} or {burn} -> {amounts:[2], supply}
//    "remove_one"   {burn, i (coin withdrawn)}
//                   -> {dy, fee, supply, price_scale:[1]}
//    "remove_fixed_out" / "remove_imb" {burn, i, amount_i}
//                   -> {dy, amounts:[2], fee, supply, price_scale:[1]}
//    "ramp_ag"      {future_A, future_gamma, future_time}
//    "stop_ramp_ag" {}
//    "new_params"   {mid_fee, out_fee, fee_gamma, adjustment_step_min,
//                    adjustment_step_max, ma_time}   (final logged values)
//    "set_donation_params" {duration, donation_protection_period,
//                    donation_protection_lp_threshold,
//                    donation_shares_max_ratio}
//    "set_fee_params" {reserved_profit_fraction, admin_fee}
//    "set_policy"   {policy: null|{...}, policy_state?: {...}}
//    anything else  -> {"skipped": true}
//  Result: {"events":[...], "final":{balances[2], D, price_scale:[1],
//    price_oracle:[1], last_prices:[1], virtual_price, xcp_profit,
//    lp_xcp_profit, total_supply, admin_balances[2], donation state...,
//    policy_state?}}.
//
// ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md) — purely additive; with
// none of the new job fields set the event outputs and "final" are byte-for-
// byte what they were before.
//   job:   "probe_all" / "probe_last" / "cf" (bool), per-event "probe" (bool)
//          and, in cf mode, per-event "burn_frac" (1e18 fraction of the LIVE
//          total supply to burn).
//   out:   result["probes"] (only when a probe was requested) and
//          result["meter"] (always).
//  * "spot" is the REAL, fee-free marginal price of coin 1 in coin 0 units,
//    1e18-scaled: get_p(_xp(balances, price_scale), D, A) * price_scale /
//    1e18 — i.e. exactly the number the pool itself stores in last_prices,
//    recomputed from the post-event state. Decimals are folded in through
//    PRECISIONS, so it is a whole-token price.
//  * "ps_gap_bp" / meter "max_ps_gap_bp": |spot*1e18/price_scale - 1e18| in
//    basis points; tracked after EVERY event, no probe needed.
//  * Meter fee accounting for this family (differs from the generic text in
//    the spec, which assumes a stableswap-style coin-denominated fee):
//      - exchange fees ARE coin-denominated (they land on the output coin)
//        -> meter["fee"][j].
//      - add_liquidity / remove_liquidity_one_coin / remove_liquidity_fixed_
//        _out charge an LP-TOKEN fee (d_token_fee), which has no per-coin
//        denomination at all. It is reported separately as meter["fee_lp"]
//        (LP wei) instead of being smeared over the coins.
//      - this fork does NOT mint LP for the DAO: the admin slice of both the
//        swap fee and the d_token fee is moved into admin_balances in COIN
//        units, so meter["admin"] carries real per-coin numbers and
//        meter["admin_lp"] is always "0".  meter["admin"] is a cumulative
//        ACCRUAL: _claim_admin_fees zeroing admin_balances does not reduce
//        it.
//  * every probe also carries the meter AS OF that event, under exactly the
//    units and conventions just described: "cfee"[2], "cfee_lp", "cadm"[2],
//    "cadm_lp" (always "0") and "cvol"[2]. The last probe's values equal
//    result["meter"] exactly. The meter lives inside Pool, so a reverted
//    event's state restore rolls it back and that event's probe shows the
//    not-counted (pre-event) totals.
//  * "rebase_mul" is not applicable to this family (no rebasing coin, no
//    absolute balance syncs in the job) and is ignored.
//
// VALIDATION (2026-08-28, mainnet replay, blocks 25847001..25856246):
//   cbBTC pool 0x862cb4... (no policy): 116/116 events wei-exact (84 swaps
//     incl. dy/fee/price_scale with one repeg, 32 adds incl. 15 donations)
//     and 18/18 final storage slots exact (incl. donation_shares release,
//     protection expiry and the non-public extension remainder).
//   WBTC pool 0x313698... (policy 0xE050e3...): 47/47 events (25 swaps,
//     21 adds, 1 remove_one) and 23/23 final fields exact, including all
//     five YBTwocryptoPolicy state fields (dual-EMA settle validated).
// ============================================================================

#include "crypto_math.hpp"

#include <nlohmann/json.hpp>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace ytc {

using I = cm::I;
using json = nlohmann::json;

// ---- constants (Twocrypto v3.0.0) ------------------------------------------
inline const I& E18() { static const I v = cm::E18(); return v; }
inline I FEE_PRECISION() { static I v("10000000000"); return v; }          // 1e10
inline I NOISE_FEE() { static I v("100000"); return v; }   // 1e10/10/10000, 0.1bp
inline I MIN_FEE() { static I v("100000"); return v; }                     // 0.1bp
inline I MAX_FEE() { static I v("10000000000"); return v; }
inline I MINIMUM_LIQUIDITY() { static I v(10000); return v; }
inline const I A_MULTIPLIER = 10000;
inline const I MIN_A = 2 * 10000;          // N_COINS * A_MULTIPLIER
inline I MAX_A() { static I v("100000000"); return v; }        // 10_000 * 10000
inline I MIN_GAMMA() { static I v("10000000000"); return v; }              // 1e10
inline I MAX_GAMMA() { static I v("199000000000000000"); return v; }   // 1.99e17
inline const I MAX_PARAM_CHANGE = 10;
inline const I MIN_RAMP_TIME = 86400;
inline const I MIN_ADMIN_FEE_CLAIM_INTERVAL = 86400;
inline I MAX_ADMIN_FEE() { static I v("9000000000"); return v; }  // 90% of 1e10
inline I LN2() { static I v("693147180559945309"); return v; }  // policy LN2

// ---- helpers ----------------------------------------------------------------

// vyper checked subtraction (uint256): revert on underflow
inline I csub(const I& a, const I& b) {
    if (b > a) throw std::runtime_error("Integer underflow");
    return a - b;
}

inline I maxI(const I& a, const I& b) { return a > b ? a : b; }
inline I minI(const I& a, const I& b) { return a < b ? a : b; }

// arithmetic shift right by 96 (EVM SAR): floor division by 2^96
inline I sar96(const I& v) {
    static const I d = I(1) << 96;
    if (v >= 0) return v >> 96;
    I m = -v;
    I q = m >> 96;
    if ((m & (d - 1)) != 0) q += 1;   // ceil of magnitude => floor of value
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

// price fields arrive as 1-element arrays (schema) or scalars
inline I jprice(const json& v) {
    if (v.is_array()) {
        if (v.size() != 1) throw std::runtime_error("price array must have 1 element");
        return ju(v.at(0));
    }
    return ju(v);
}

inline std::string S(const I& v) { return v.str(); }
inline json parr(const I& v) { return json::array({S(v)}); }

// ---- snekmate math._wad_exp (exact) -----------------------------------------
// SDIV truncates toward zero (cpp_int '/' matches); '>>' in the vyper source
// is SAR (floor) on int256 -> sar96(); the final step is uint256 arithmetic
// mod 2^256 with a logical right shift.
inline I wad_exp(const I& x_in) {
    static const I LOWCUT("-41446531673892822313");
    static const I HIGHCUT("135305999368893231589");
    static const I LOG2_96("54916777467707473351141471128");   // ln2 * 2^96
    static const I FIVE18 = cm::pow_int(5, 18);
    static const I TWO256 = I(1) << 256;

    if (x_in <= LOWCUT) return 0;
    if (!(x_in < HIGHCUT)) throw std::runtime_error("math: wad_exp overflow");

    I x = (x_in * (I(1) << 78)) / FIVE18;                      // trunc toward 0

    // k = ((x << 96) sdiv LOG2_96 + 2^95) sar 96
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

    I r = p / q;                                               // trunc toward 0

    // convert(convert(r, bytes32), uint256): two's complement reinterpret
    I ur = r;
    if (ur < 0) ur += TWO256;
    I prod = (ur * I("3822833074963236453042738258902158003155416615667")) % TWO256;

    // shift = 195 - k; convert(negative, uint256) would revert (k <= 195 in
    // domain); SHR by >= 256 yields 0.
    I shift = I(195) - k;
    if (shift < 0) throw std::runtime_error("wad_exp shift underflow");
    I res;
    if (shift >= 256) res = 0;
    else res = prod >> shift.convert_to<unsigned>();
    if (res >= (I(1) << 255)) throw std::runtime_error("wad_exp int256 overflow");
    return res;   // always >= 0; math contract converts to uint256
}

// ---- StableswapMath.get_p (exact) -------------------------------------------
inline I get_p(const I xp[2], const I& D, const I& A) {
    I ANN = A * 2;
    I Dr = D / 4;                                    // D / N**N
    Dr = Dr * D / xp[0];
    Dr = Dr * D / xp[1];
    I xp0_A = ANN * xp[0] / A_MULTIPLIER;
    return E18() * (xp0_A + Dr * xp[0] / xp[1]) / (xp0_A + Dr);
}

// ---- YBTwocryptoPolicy (dual-EMA price-scale controller) --------------------

struct Policy {
    bool present = false;
    // immutables
    I fast_half_life{0}, slow_half_life{0}, kappa{0};
    I deadband{0}, min_cap{0}, max_cap{0};
    // state (price_scale == 0 => uninitialized)
    I last_update_ts{0}, last_prices{0};
    I fast_ema{0}, slow_ema{0}, price_scale{0};
};

inline const I POLICY_CAP_RAMP_SECONDS = 3600;
inline const I POLICY_TWEAK_MULT = 5;

// YBTwocryptoPolicy._ema
inline I policy_ema(const I& ema, const I& last_prices, const I& price_scale,
                    const I& dt, const I& half_life) {
    if (dt == 0) return ema;
    I price = minI(maxI(last_prices, price_scale / 2), 2 * price_scale);
    I alpha = wad_exp(-I(dt * LN2() / half_life));
    return (price * (E18() - alpha) + ema * alpha) / E18();
}

inline void policy_project(const Policy& pol, const I& dt, I& fast, I& slow) {
    fast = policy_ema(pol.fast_ema, pol.last_prices, pol.price_scale, dt,
                      pol.fast_half_life);
    slow = policy_ema(pol.slow_ema, pol.last_prices, pol.price_scale, dt,
                      pol.slow_half_life);
}

// YBTwocryptoPolicy.get_price_scale (view, evaluated at ts)
inline I policy_get_price_scale(const Policy& pol, const I& ts) {
    const I& current = pol.price_scale;
    if (current == 0) return 0;

    I elapsed = csub(ts, pol.last_update_ts);
    I ramp_time = minI(elapsed, POLICY_CAP_RAMP_SECONDS);
    I current_cap = pol.min_cap +
                    (pol.max_cap - pol.min_cap) * ramp_time / POLICY_CAP_RAMP_SECONDS;

    I fast, slow;
    policy_project(pol, elapsed, fast, slow);

    I raw_target;
    if (fast >= slow) {
        raw_target = slow + pol.kappa * (fast - slow) / E18();
    } else {
        I step = pol.kappa * (slow - fast) / E18();
        raw_target = slow - minI(step, slow);
    }

    I ema_gap = raw_target >= current ? I(raw_target - current)
                                      : I(current - raw_target);
    if (ema_gap * E18() <= current * pol.deadband) return current;

    I max_move = current * current_cap / E18();
    I desired_move = minI(ema_gap, max_move);
    I target_gap = POLICY_TWEAK_MULT * desired_move;
    return raw_target >= current ? I(current + target_gap)
                                 : I(csub(current, target_gap));
}

// YBTwocryptoPolicy.update_pool_state (pool-authenticated write)
inline void policy_update_pool_state(Policy& pol, const I& ts,
                                     const I& price_scale, const I& price_oracle,
                                     const I& last_prices) {
    I fast = price_oracle, slow = price_oracle;
    if (pol.price_scale != 0) {
        policy_project(pol, csub(ts, pol.last_update_ts), fast, slow);
    }
    pol.last_update_ts = ts;
    pol.last_prices = last_prices;
    pol.fast_ema = fast;
    pol.slow_ema = slow;
    pol.price_scale = price_scale;
}

// ---- pool state -------------------------------------------------------------

// Engine-contract-v2 revenue meter. Lives INSIDE Pool on purpose: the driver
// snapshots/restores Pool around every event, so a reverted event accrues
// nothing (and the remove_imbalance "try i=0 then i=1" probe cannot double
// count). Never read by the consensus math.
struct Meters {
    I fee[2]{};        // gross fee charged, coin units (swap fees only)
    I fee_lp{0};       // gross d_token fee, LP-token units (adds/withdrawals)
    I admin[2]{};      // DAO slice moved into admin_balances, coin units
    I vol[2]{};        // gross exchange input volume, per coin
};

struct Pool {
    I prec[2];                                 // 10**(18 - decimals[i])
    I balances[2]{};
    I admin_balances[2]{};

    I price_scale{0}, price_oracle{0}, last_prices{0}, last_timestamp{0};

    // A/gamma ramp (unpacked; packed layout on-chain is (A << 128) | gamma)
    I initial_A{0}, initial_gamma{0}, initial_A_gamma_time{0};
    I future_A{0}, future_gamma{0}, future_A_gamma_time{0};

    I mid_fee{0}, out_fee{0}, fee_gamma{0};          // packed_fee_params
    I step_min{0}, step_max{0}, ma_time{0};          // packed_rebalancing_params

    I reserved_profit_fraction{0}, admin_fee{0};     // 1e10

    I D{0}, xcp_profit{0}, lp_xcp_profit{0}, virtual_price{0}, total_supply{0};

    I donation_shares{0}, donation_shares_max_ratio{0};
    I donation_duration{0}, last_donation_release_ts{0};
    I dp_expiry_ts{0}, dp_period{0}, dp_lp_threshold{0}, dp_ext_remainder{0};

    I last_admin_fee_claim_ts{0};
    bool fee_receiver_set = true;

    Policy pol;
    Meters m;                                  // contract-v2 meter (not state)
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

// Twocrypto._assert_balance
inline void assert_balance(const I xp[2]) {
    if (!(xp[0] > 0 && xp[1] > 0 &&
          maxI(xp[0], xp[1]) / minI(xp[0], xp[1]) < 1000))
        throw std::runtime_error("!balance");
}

// Twocrypto._fee (policy get_fee is always 0 on the deployed policy ->
// native path unconditionally; see header note 3)
inline I fee_of(const Pool& p, const I xp[2]) {
    I B = xp[0] + xp[1];
    // PRECISION * N**N * xp[0] // B * xp[1] // B
    B = E18() * 4 * xp[0] / B * xp[1] / B;
    // fee_gamma * B // (fee_gamma * B // 1e18 + 1e18 - B)
    B = p.fee_gamma * B / (p.fee_gamma * B / E18() + E18() - B);
    I fee = (p.mid_fee * B + p.out_fee * (E18() - B)) / E18();
    return minI(MAX_FEE(), maxI(MIN_FEE(), fee));
}

// Twocrypto._get_D
inline I get_D_of(const Pool& p, const I A_gamma[2], const I xp[2]) {
    if (is_ramping(p)) {
        I x[2] = {xp[0], xp[1]};
        return cm::ss_newton_D(A_gamma[0], x, true);
    }
    return p.D;
}

// Twocrypto._donation_shares(_donation_protection)
inline I donation_shares_of(const Pool& p, const I& ts, bool protection) {
    const I& shares = p.donation_shares;
    if (shares == 0) return 0;

    I elapsed = csub(ts, p.last_donation_release_ts);
    I unlocked = minI(shares, shares * elapsed / p.donation_duration);
    if (!protection) return unlocked;

    I protection_factor = 0;
    if (p.dp_expiry_ts > ts)
        protection_factor = minI((p.dp_expiry_ts - ts) * E18() / p.dp_period, E18());
    return unlocked * (E18() - protection_factor) / E18();
}

// Twocrypto._calc_token_fee(amounts, xp, donation, deposit) with
// from_view = False (state-changing paths only)
inline I calc_token_fee(const Pool& p, const I& ts, const I amounts_in[2],
                        const I xp[2], bool donation, bool deposit) {
    if (donation) return NOISE_FEE();

    // surplus_amounts = amounts (from_view = False)
    I balances_ratio =
        csub(p.balances[0], amounts_in[0]) * p.prec[0] * E18() /
        (csub(p.balances[1], amounts_in[1]) * p.prec[1]);

    I amounts[2];
    // _xp(amounts, balances_ratio)
    amounts[0] = amounts_in[0] * p.prec[0];
    amounts[1] = amounts_in[1] * p.prec[1] * balances_ratio / E18();

    I fee = fee_of(p, xp) * 2 / 4;   // fee * N // (4 * (N - 1))

    I Ssum = amounts[0] + amounts[1];
    I avg = Ssum / 2;
    I Sdiff = 0;
    for (int k = 0; k < 2; ++k)
        Sdiff += amounts[k] > avg ? I(amounts[k] - avg) : I(avg - amounts[k]);

    I lp_spam_penalty_fee = 0;
    if (deposit && p.dp_expiry_ts > ts) {
        I protection_factor =
            minI((p.dp_expiry_ts - ts) * E18() / p.dp_period, E18());
        lp_spam_penalty_fee = minI(
            fee,
            protection_factor * fee * p.donation_shares / p.total_supply /
                p.donation_shares_max_ratio);
    }
    return fee * Sdiff / Ssum + NOISE_FEE() + lp_spam_penalty_fee;
}

// Twocrypto._apply_admin_d_token_fee — mutates admin_balances/balances,
// returns adjusted local_balances
inline void apply_admin_d_token_fee(Pool& p, I local_balances[2],
                                    const I& d_token_fee, const I& fee_supply) {
    I admin_d = d_token_fee * p.reserved_profit_fraction * p.admin_fee /
                (FEE_PRECISION() * FEE_PRECISION());
    if (admin_d > 0) {
        for (int i = 0; i < 2; ++i) {
            I admin_amount = local_balances[i] * admin_d / fee_supply;
            p.admin_balances[i] += admin_amount;
            p.balances[i] = csub(p.balances[i], admin_amount);
            local_balances[i] = csub(local_balances[i], admin_amount);
            p.m.admin[i] += admin_amount;   // meter only
        }
    }
}

// Twocrypto._update_policy_state — deterministic policy write
inline void update_policy_state(Pool& p, const I& ts, const I& price_scale,
                                const I& price_oracle, const I& last_prices) {
    if (p.pol.present)
        policy_update_pool_state(p.pol, ts, price_scale, price_oracle, last_prices);
}

// Twocrypto.tweak_price — mutates p; returns the resulting price_scale
inline I tweak_price(Pool& p, const I& ts, const I A_gamma[2], const I _xp[2],
                     I D, const I& vp_preop) {
    I price_oracle = p.price_oracle;
    I last_prices = p.last_prices;
    I price_scale = p.price_scale;
    bool ramping = is_ramping(p);     // read before last_timestamp is bumped

    // ------------------ EMA oracle (once per block) --------------------------
    I last_timestamp = p.last_timestamp;
    if (last_timestamp < ts) {
        I alpha = wad_exp(-I((ts - last_timestamp) * E18() / p.ma_time));
        price_oracle =
            (minI(maxI(last_prices, price_scale / 2), 2 * price_scale) *
                 (E18() - alpha) +
             price_oracle * alpha) /
            E18();
        p.price_oracle = price_oracle;
        p.last_timestamp = ts;
    }

    // spot price after this op
    last_prices = get_p(_xp, D, A_gamma[0]) * price_scale / E18();
    p.last_prices = last_prices;

    I total_supply = p.total_supply;
    I donation_shares = donation_shares_of(p, ts, true);
    I locked_supply = csub(total_supply, donation_shares);

    I old_virtual_price = p.virtual_price;
    I xcp = xcp_of(D, price_scale);
    I virtual_price = E18() * xcp / total_supply;

    if (!(virtual_price >= vp_preop &&
          (ramping || virtual_price >= old_virtual_price)))
        throw std::runtime_error("virtual price decreased");

    // ------------------ xcp_profit / lp_xcp_profit ratchet -------------------
    I old_xcp_profit = p.xcp_profit;
    I xcp_profit = old_xcp_profit;
    I lp_xcp_profit = p.lp_xcp_profit;

    if (virtual_price > old_virtual_price) {
        xcp_profit += virtual_price - old_virtual_price;
        if (xcp_profit > E18()) {
            I d_profit = xcp_profit - maxI(old_xcp_profit, E18());
            const I& rf = p.reserved_profit_fraction;
            const I& af = p.admin_fee;
            lp_xcp_profit += d_profit * rf * (FEE_PRECISION() - af) /
                             (FEE_PRECISION() * FEE_PRECISION() - rf * af);
        }
    } else {
        I vp_delta = old_virtual_price - virtual_price;
        xcp_profit = csub(xcp_profit, vp_delta);
        if (lp_xcp_profit > E18() && vp_delta <= lp_xcp_profit - E18())
            lp_xcp_profit = lp_xcp_profit - vp_delta;
        else
            lp_xcp_profit = E18();
    }
    p.lp_xcp_profit = lp_xcp_profit;
    p.xcp_profit = xcp_profit;

    // ------------------ rebalance attempt ------------------------------------
    I vp_boosted = E18() * xcp / locked_supply;
    if (!(vp_boosted >= virtual_price))
        throw std::runtime_error("negative donation");

    if (vp_boosted > lp_xcp_profit && ts > last_timestamp) {
        I p_policy = 0;
        if (p.pol.present) p_policy = policy_get_price_scale(p.pol, ts);

        I target_price = price_oracle;
        if (p_policy > 0) {
            I policy_bound = price_oracle / 5;
            target_price = minI(maxI(p_policy, price_oracle - policy_bound),
                                price_oracle + policy_bound);
        }

        I norm = target_price * E18() / price_scale;
        norm = norm > E18() ? I(norm - E18()) : I(E18() - norm);

        I adjustment_step = minI(norm / 5, p.step_max);

        I p_new = price_scale;
        if (adjustment_step > p.step_min) {
            p_new = (price_scale * (norm - adjustment_step) +
                     adjustment_step * target_price) /
                    norm;
        }

        if (p_new != price_scale) {
            I xp2[2] = {_xp[0], _xp[1] * p_new / price_scale};
            I new_D = cm::ss_newton_D(A_gamma[0], xp2, true);
            I new_xcp = xcp_of(new_D, p_new);
            I new_virtual_price = E18() * new_xcp / total_supply;

            I donation_shares_to_burn = 0;
            I goal_vp = maxI(lp_xcp_profit, virtual_price);
            if (new_virtual_price < goal_vp) {
                I tweaked_supply = E18() * new_xcp / goal_vp;
                if (!(tweaked_supply < total_supply))
                    throw std::runtime_error("tweaked supply must shrink");
                donation_shares_to_burn =
                    minI(total_supply - tweaked_supply, donation_shares);
                new_virtual_price =
                    E18() * new_xcp / (total_supply - donation_shares_to_burn);
            }

            if (new_virtual_price > E18() && new_virtual_price >= lp_xcp_profit) {
                p.D = new_D;
                p.virtual_price = new_virtual_price;
                p.price_scale = p_new;

                if (donation_shares_to_burn > 0) {
                    I shares_unlocked = donation_shares_of(p, ts, false);
                    const I& shares_available = donation_shares;
                    I shares_unlocked_new = csub(
                        shares_unlocked,
                        donation_shares_to_burn * shares_unlocked / shares_available);
                    I new_total = csub(p.donation_shares, donation_shares_to_burn);
                    I new_elapsed = 0;
                    if (new_total > 0 && shares_unlocked_new > 0)
                        new_elapsed =
                            shares_unlocked_new * p.donation_duration / new_total;
                    p.donation_shares = new_total;
                    p.total_supply = csub(p.total_supply, donation_shares_to_burn);
                    p.last_donation_release_ts = csub(ts, new_elapsed);
                }

                assert_balance(xp2);
                update_policy_state(p, ts, p_new, price_oracle, last_prices);
                return p_new;
            }
        }
    }

    // no price adjustment
    p.D = D;
    p.virtual_price = virtual_price;
    assert_balance(_xp);
    update_policy_state(p, ts, price_scale, price_oracle, last_prices);
    return price_scale;
}

// Twocrypto._claim_admin_fees (only invoked from fixed-out withdrawals)
inline void claim_admin_fees(Pool& p, const I& ts) {
    if (ts - p.last_admin_fee_claim_ts < MIN_ADMIN_FEE_CLAIM_INTERVAL ||
        !p.fee_receiver_set)
        return;
    if (p.admin_balances[0] == 0 && p.admin_balances[1] == 0) return;
    p.last_admin_fee_claim_ts = ts;
    p.admin_balances[0] = 0;
    p.admin_balances[1] = 0;
}

// ---- event applications -----------------------------------------------------
// Each mutates `p` exactly as the contract's storage writes do and returns
// the outputs object; any throw == vyper revert (caller restores state).

// exchange(i, j, dx) — _transfer_in + _exchange + _transfer_out
inline json apply_exchange(Pool& p, int i, int j, const I& dx, const I& ts) {
    if (i < 0 || i > 1 || j < 0 || j > 1)
        throw std::runtime_error("coin index out of range");
    if (i == j) throw std::runtime_error("same coin");
    if (!(dx > 0)) throw std::runtime_error("zero dx");

    // _transfer_in
    p.balances[i] += dx;

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);

    I balances[2] = {p.balances[0], p.balances[1]};
    I y = balances[j];
    I x0 = csub(balances[i], dx);   // old balance of coin i

    I price_scale = p.price_scale;
    I xp[2];
    xp_of(p, balances, price_scale, xp);

    if (is_ramping(p)) {
        x0 *= p.prec[i];
        if (i > 0) x0 = x0 * price_scale / E18();
        I x1 = xp[i];
        xp[i] = x0;
        p.D = cm::ss_newton_D(A_gamma[0], xp, true);
        xp[i] = x1;
    }

    I D = p.D;
    I vp_preop = E18() * xcp_of(D, price_scale) / p.total_supply;

    I y_out0 = cm::ss_get_y(A_gamma[0], xp, D, j);   // math returns [y, 0]
    I dy = csub(xp[j], y_out0);
    xp[j] = csub(xp[j], dy);
    dy = csub(dy, 1);

    if (j > 0) dy = dy * E18() / price_scale;
    dy /= p.prec[j];

    I fee = fee_of(p, xp) * dy / FEE_PRECISION();
    dy = csub(dy, fee);
    y = csub(y, dy);

    I admin_fee_amount = fee * p.reserved_profit_fraction * p.admin_fee /
                         (FEE_PRECISION() * FEE_PRECISION());
    if (admin_fee_amount > 0) {
        p.admin_balances[j] += admin_fee_amount;
        p.balances[j] = csub(p.balances[j], admin_fee_amount);
        y = csub(y, admin_fee_amount);
    }

    p.m.vol[i] += dx;                  // meter only
    p.m.fee[j] += fee;                 // gross swap fee, output-coin units
    p.m.admin[j] += admin_fee_amount;

    y *= p.prec[j];
    if (j > 0) y = y * price_scale / E18();
    xp[j] = y;

    D = cm::ss_newton_D(A_gamma[0], xp, true);   // K0_prev (y_out[1]) unused

    I price_scale_new = tweak_price(p, ts, A_gamma, xp, D, vp_preop);

    // _transfer_out
    p.balances[j] = csub(p.balances[j], dy);

    return json{{"dy", S(dy)},
                {"fee", S(fee)},
                {"price_scale", parr(price_scale_new)}};
}

// add_liquidity(amounts, donation)
inline json apply_add(Pool& p, const I amounts_in[2], bool donation, const I& ts) {
    if (!(amounts_in[0] + amounts_in[1] > 0)) throw std::runtime_error("!amounts");

    I old_balances[2] = {p.balances[0], p.balances[1]};

    // _transfer_in loop
    I amounts_received[2] = {amounts_in[0], amounts_in[1]};
    for (int i = 0; i < 2; ++i) p.balances[i] += amounts_received[i];
    I balances[2] = {p.balances[0], p.balances[1]};

    I price_scale = p.price_scale;
    I xp[2], old_xp[2];
    xp_of(p, balances, price_scale, xp);
    xp_of(p, old_balances, price_scale, old_xp);

    if (p.D == 0) {
        if (donation)
            throw std::runtime_error("donation not allowed on empty pool");
        // deploy_eoa init gate skipped (all target pools are initialized)
    }

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);
    I old_D = get_D_of(p, A_gamma, old_xp);   // uses stored D unless ramping
    I D = cm::ss_newton_D(A_gamma[0], xp, true);

    I token_supply = p.total_supply;
    I vp_preop = p.virtual_price;
    if (old_D > 0)
        vp_preop = E18() * xcp_of(old_D, price_scale) / token_supply;

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

        if (!donation) {
            if (d_token_fee > 0 && p.reserved_profit_fraction > 0 &&
                p.admin_fee > 0) {
                I fee_supply = token_supply + d_token + d_token_fee;
                I local_balances[2] = {balances[0], balances[1]};
                apply_admin_d_token_fee(p, local_balances, d_token_fee, fee_supply);
                xp_of(p, local_balances, price_scale, xp);
                D = cm::ss_newton_D(A_gamma[0], xp, true);
            }
        }

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
            // donation protection extension + spam-penalty remainder
            I relative_lp_add = d_token * E18() / (token_supply + d_token);
            if (relative_lp_add > 0 && p.donation_shares > 0) {
                I raw_extension =
                    relative_lp_add * p.dp_period + p.dp_ext_remainder;
                I extension_seconds = raw_extension / p.dp_lp_threshold;
                I current_expiry = maxI(p.dp_expiry_ts, ts);
                I max_expiry = ts + p.dp_period;
                I uncapped_expiry = current_expiry + extension_seconds;
                if (uncapped_expiry >= max_expiry) {
                    p.dp_expiry_ts = max_expiry;
                    p.dp_ext_remainder = 0;
                } else {
                    p.dp_expiry_ts = uncapped_expiry;
                    p.dp_ext_remainder = raw_extension % p.dp_lp_threshold;
                }
            }
            p.total_supply += d_token;   // mint(receiver, d_token)
        }

        price_scale_out = tweak_price(p, ts, A_gamma, xp, D, vp_preop);
    } else {
        // instantiating an empty pool
        if (!(d_token > MINIMUM_LIQUIDITY()))
            throw std::runtime_error("initial liquidity too low");
        p.D = D;
        p.virtual_price = E18();
        p.xcp_profit = E18();
        p.lp_xcp_profit = E18();
        p.total_supply += MINIMUM_LIQUIDITY();   // mint(self, MINIMUM_LIQUIDITY)
        d_token = csub(d_token, MINIMUM_LIQUIDITY());
        p.total_supply += d_token;               // mint(receiver, d_token)
        update_policy_state(p, ts, price_scale, p.price_oracle, p.last_prices);
    }

    return json{{"minted", S(d_token)},
                {"fee", S(d_token_fee)},
                {"supply", S(p.total_supply)},
                {"price_scale", parr(price_scale_out)}};
}

// remove_liquidity(amount) — balanced, no fees, D scaled down
inline json apply_remove(Pool& p, const I& amount, const I& ts) {
    I total_supply = p.total_supply;
    p.total_supply = csub(p.total_supply, amount);   // burnFrom

    I withdraw_amounts[2];
    I D = p.D;
    for (int i = 0; i < 2; ++i)
        withdraw_amounts[i] = p.balances[i] * amount / total_supply;

    p.D = csub(D, D * amount / total_supply);

    for (int i = 0; i < 2; ++i)
        p.balances[i] = csub(p.balances[i], withdraw_amounts[i]);

    if (amount > 0 && p.pol.present) {
        // best-effort policy update with current (post-withdraw) snapshot
        update_policy_state(p, ts, p.price_scale, p.price_oracle, p.last_prices);
    }

    return json{{"amounts", json::array({S(withdraw_amounts[0]),
                                         S(withdraw_amounts[1])})},
                {"supply", S(p.total_supply)}};
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

    I dD = lp_token_amount * D / token_supply;
    I xp_new[2] = {xp[0], xp[1]};

    I price_scales[2] = {E18() * p.prec[0], price_scale * p.prec[1]};

    I amountsp[2] = {I(0), I(0)};
    amountsp[i] = (amount_i * price_scales[i] + E18() - 1) / E18();
    xp_new[i] = csub(xp_new[i], amountsp[i]);

    I y = cm::ss_get_y(A_gamma[0], xp_new, csub(D, dD), j) + 1;
    amountsp[j] = csub(xp[j], y);
    xp_new[j] = y;

    I amounts[2] = {I(0), I(0)};
    amounts[i] = amount_i;
    if (i == 0)
        amounts[1] = amountsp[1] * E18() / p.prec[1] / price_scale;
    else
        amounts[0] = amountsp[0] / p.prec[0];

    if (!(amounts[0] + amounts[1] > 0)) throw std::runtime_error("!tokens");

    I approx_fee = calc_token_fee(p, /*ts*/ 0, amounts, xp_new, false, false);
    // note: deposit=false => the ts-dependent spam penalty never triggers,
    // so passing ts=0 is safe here.

    dD = csub(dD, dD * approx_fee / FEE_PRECISION() + 1);

    y = cm::ss_get_y(A_gamma[0], xp_new, csub(D, dD), j) + 1;
    I dy = csub(xp[j], y) * E18() / price_scales[j];
    xp_new[j] = y;

    dy_out = dy;
    D_out = csub(D, dD);
    xp_out[0] = xp_new[0];
    xp_out[1] = xp_new[1];
    approx_fee_out = approx_fee;
}

// remove_liquidity_fixed_out / remove_liquidity_one_coin.
// `i` here is the INTERNAL index (the coin with the fixed amount_i); for
// one-coin withdrawals of user coin u, call with i = 1 - u, amount_i = 0.
inline json apply_remove_fixed_out(Pool& p, const I& token_amount, int i,
                                   const I& amount_i, const I& ts) {
    claim_admin_fees(p, ts);

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);

    I dy, D, xp[2], approx_fee;
    calc_withdraw_fixed_out(p, A_gamma, token_amount, i, amount_i, dy, D, xp,
                            approx_fee);

    I price_scale_preop = p.price_scale;
    I xp_pre[2];
    xp_of(p, p.balances, price_scale_preop, xp_pre);
    I D_preop = get_D_of(p, A_gamma, xp_pre);
    I vp_preop = E18() * xcp_of(D_preop, price_scale_preop) / p.total_supply;

    int j = 1 - i;
    I d_token_fee = approx_fee * token_amount / FEE_PRECISION() + 1;
    p.m.fee_lp += d_token_fee;   // meter only (LP-token-denominated fee)

    if (d_token_fee > 0 && p.reserved_profit_fraction > 0 && p.admin_fee > 0) {
        I fee_supply = csub(p.total_supply, token_amount) + d_token_fee;
        I local_balances[2] = {p.balances[0], p.balances[1]};
        local_balances[i] = csub(local_balances[i], amount_i);
        local_balances[j] = csub(local_balances[j], dy);
        apply_admin_d_token_fee(p, local_balances, d_token_fee, fee_supply);
        xp_of(p, local_balances, price_scale_preop, xp);
        D = cm::ss_newton_D(A_gamma[0], xp, true);
    }

    p.total_supply = csub(p.total_supply, token_amount);   // burnFrom

    I price_scale_new = tweak_price(p, ts, A_gamma, xp, D, vp_preop);

    if (amount_i != 0) p.balances[i] = csub(p.balances[i], amount_i);
    p.balances[j] = csub(p.balances[j], dy);

    I amounts_out[2] = {I(0), I(0)};
    amounts_out[i] = amount_i;
    amounts_out[j] = dy;

    return json{{"dy", S(dy)},
                {"amounts", json::array({S(amounts_out[0]), S(amounts_out[1])})},
                {"fee", S(d_token_fee)},   // LP-token units, as logged
                {"supply", S(p.total_supply)},
                {"price_scale", parr(price_scale_new)}};
}

// ramp_A_gamma(future_A, future_gamma, future_time)
inline json apply_ramp_ag(Pool& p, const I& future_A, const I& future_gamma,
                          const I& future_time, const I& ts) {
    if (!(p.D > 0)) throw std::runtime_error("pool has no liquidity");
    if (p.future_A_gamma_time > p.last_timestamp)
        throw std::runtime_error("ramp active");
    if (!(future_time > ts + MIN_RAMP_TIME - 1))
        throw std::runtime_error("ramp time below minimum");

    I A_gamma[2];
    A_gamma_now(p, ts, A_gamma);

    if (!(future_A > MIN_A - 1)) throw std::runtime_error("A below minimum");
    if (!(future_A < MAX_A() + 1)) throw std::runtime_error("A above maximum");
    if (!(future_gamma > MIN_GAMMA() - 1))
        throw std::runtime_error("gamma below minimum");
    if (!(future_gamma < MAX_GAMMA() + 1))
        throw std::runtime_error("gamma above maximum");

    I ratio = E18() * future_A / A_gamma[0];
    if (!(ratio < E18() * MAX_PARAM_CHANGE + 1))
        throw std::runtime_error("A change too high");
    if (!(ratio > E18() / MAX_PARAM_CHANGE - 1))
        throw std::runtime_error("A change too low");
    ratio = E18() * future_gamma / A_gamma[1];
    if (!(ratio < E18() * MAX_PARAM_CHANGE + 1))
        throw std::runtime_error("gamma change too high");
    if (!(ratio > E18() / MAX_PARAM_CHANGE - 1))
        throw std::runtime_error("gamma change too low");

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
// _xp() folds the coin decimals (PRECISIONS) and price_scale in, and get_p
// returns d(xp0)/d(xp1); undoing the price_scale leg of _xp turns that into
// the whole-token price — which is exactly how the pool derives last_prices.
// Returns 0 for a degenerate (empty / uninitialized) pool.
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

// live virtual price (== the pool's get_virtual_price view), 1e18-scaled
inline I live_vp(const Pool& p) {
    if (p.total_supply == 0 || p.price_scale == 0) return 0;
    return E18() * xcp_of(p.D, p.price_scale) / p.total_supply;
}

inline json make_probe(const Pool& p, const I& ts, int idx) {
    I sp = spot_of(p, ts);
    return json{
        {"i", idx},
        {"bal", json::array({S(p.balances[0]), S(p.balances[1])})},
        {"sup", S(p.total_supply)},
        {"adm", json::array({S(p.admin_balances[0]), S(p.admin_balances[1])})},
        {"D", S(p.D)},
        {"vp", S(live_vp(p))},
        {"xcp", S(p.xcp_profit)},
        {"ps", parr(p.price_scale)},
        {"spot", parr(sp)},
        {"ps_gap_bp", ps_gap_bp(sp, p.price_scale).convert_to<long long>()},
        // cumulative meter as of this event — mirrors result["meter"]'s
        // fee / fee_lp / admin / admin_lp / vol exactly (last probe == totals).
        // "cadm_lp" is always "0": this fork never mints LP for the DAO.
        {"cfee", json::array({S(p.m.fee[0]), S(p.m.fee[1])})},
        {"cfee_lp", S(p.m.fee_lp)},
        {"cadm", json::array({S(p.m.admin[0]), S(p.m.admin[1])})},
        {"cadm_lp", "0"},
        {"cvol", json::array({S(p.m.vol[0]), S(p.m.vol[1])})},
        // extra (not in the contract, but this fork needs it to make sense of
        // "sup"/"vp"): LP supply held by the donation buffer at this point.
        {"don", S(p.donation_shares)}};
}

// ---- policy json parsing ----------------------------------------------------

inline void parse_policy_params(Policy& pol, const json& jp) {
    pol.present = true;
    pol.fast_half_life = ju(jp.at("fast_half_life"));
    pol.slow_half_life = ju(jp.at("slow_half_life"));
    pol.kappa = ju(jp.at("kappa"));
    pol.deadband = ju(jp.at("deadband"));
    pol.min_cap = ju(jp.at("min_cap"));
    pol.max_cap = ju(jp.at("max_cap"));
}

inline void parse_policy_state(Policy& pol, const json& js) {
    pol.last_update_ts = jfield(js, "last_update_ts", 0);
    pol.last_prices = jfield(js, "last_prices", 0);
    pol.fast_ema = jfield(js, "fast_ema", 0);
    pol.slow_ema = jfield(js, "slow_ema", 0);
    pol.price_scale = jfield(js, "price_scale", 0);
}

}  // namespace ytc

// ---- entry point ------------------------------------------------------------

inline nlohmann::json run_twocrypto_yb(const nlohmann::json& job) {
    using ytc::I;
    using ytc::ju;
    using ytc::jfield;
    using ytc::S;
    using json = nlohmann::json;

    ytc::Pool p;

    {
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
    p.future_gamma =
        prm.contains("future_gamma") ? ju(prm.at("future_gamma")) : p.initial_gamma;
    p.initial_A_gamma_time = jfield(prm, "initial_A_gamma_time", 0);
    p.future_A_gamma_time = jfield(prm, "future_A_gamma_time", 0);
    p.mid_fee = ju(prm.at("mid_fee"));
    p.out_fee = ju(prm.at("out_fee"));
    p.fee_gamma = ju(prm.at("fee_gamma"));
    // fork names, with tricrypto-schema aliases
    p.step_min = prm.contains("adjustment_step_min")
                     ? ju(prm.at("adjustment_step_min"))
                     : ju(prm.at("allowed_extra_profit"));
    p.step_max = prm.contains("adjustment_step_max")
                     ? ju(prm.at("adjustment_step_max"))
                     : ju(prm.at("adjustment_step"));
    p.ma_time = ju(prm.at("ma_time"));   // raw stored value (seconds / ln2)
    p.admin_fee = jfield(prm, "admin_fee", I("5000000000"));
    p.reserved_profit_fraction =
        jfield(prm, "reserved_profit_fraction", I("5000000000"));
    p.donation_duration = jfield(prm, "donation_duration", I(604800));
    p.donation_shares_max_ratio =
        jfield(prm, "donation_shares_max_ratio", I("100000000000000000"));
    p.dp_period = jfield(prm, "donation_protection_period", I(600));
    p.dp_lp_threshold =
        jfield(prm, "donation_protection_lp_threshold", I("200000000000000000"));

    if (prm.contains("policy") && !prm.at("policy").is_null())
        ytc::parse_policy_params(p.pol, prm.at("policy"));

    const json& st = job.at("state");
    {
        const json& b = st.at("balances");
        p.balances[0] = ju(b.at(0));
        p.balances[1] = ju(b.at(1));
    }
    if (st.contains("admin_balances")) {
        const json& b = st.at("admin_balances");
        p.admin_balances[0] = ju(b.at(0));
        p.admin_balances[1] = ju(b.at(1));
    }
    p.price_scale = ytc::jprice(st.at("price_scale"));
    p.price_oracle = ytc::jprice(st.at("price_oracle"));
    p.last_prices = ytc::jprice(st.at("last_prices"));
    p.last_timestamp = ju(st.at("last_timestamp"));
    p.D = ju(st.at("D"));
    p.virtual_price = ju(st.at("virtual_price"));
    p.xcp_profit = ju(st.at("xcp_profit"));
    // fork field; xcp_profit_a accepted as a legacy alias of the schema
    p.lp_xcp_profit = st.contains("lp_xcp_profit")
                          ? ju(st.at("lp_xcp_profit"))
                          : jfield(st, "xcp_profit_a", ytc::E18());
    p.total_supply = ju(st.at("total_supply"));
    p.donation_shares = jfield(st, "donation_shares", 0);
    p.last_donation_release_ts = jfield(st, "last_donation_release_ts", 0);
    p.dp_expiry_ts = jfield(st, "donation_protection_expiry_ts", 0);
    p.dp_ext_remainder = jfield(st, "donation_protection_extension_remainder", 0);
    p.last_admin_fee_claim_ts = jfield(st, "last_admin_fee_claim_timestamp", 0);
    if (st.contains("fee_receiver_set"))
        p.fee_receiver_set = st.at("fee_receiver_set").get<bool>();
    if (p.pol.present && st.contains("policy_state") &&
        !st.at("policy_state").is_null())
        ytc::parse_policy_state(p.pol, st.at("policy_state"));

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

        // cf mode: burn amounts are fractions of the LIVE supply, because the
        // historical absolute burn / supply_after no longer describes this
        // state path. Outside cf mode burn_frac is ignored entirely.
        auto burn_of = [&](const char* abs_key) {
            if (cf_mode && ev.contains("burn_frac"))
                return I(p.total_supply * ju(ev.at("burn_frac")) / ytc::E18());
            return ju(ev.at(abs_key));
        };

        ytc::Pool snapshot = p;   // restored on revert (includes policy state)
        bool reverted = false, skipped = false;

        try {
            json outputs;

            if (type == "exchange") {
                outputs = ytc::apply_exchange(p, ev.at("sold_id").get<int>(),
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
                outputs = ytc::apply_add(p, amounts, donation, ts);
            } else if (type == "remove") {
                I amount;
                if (cf_mode && ev.contains("burn_frac"))
                    amount = burn_of("burn");
                else if (ev.contains("burn")) amount = ju(ev.at("burn"));
                else amount = ytc::csub(p.total_supply, ju(ev.at("supply_after")));
                outputs = ytc::apply_remove(p, amount, ts);
            } else if (type == "remove_one") {
                // i = the coin the user withdraws; internal index is flipped
                int ui = ev.at("i").get<int>();
                if (ui < 0 || ui > 1)
                    throw std::runtime_error("coin index out of range");
                outputs =
                    ytc::apply_remove_fixed_out(p, burn_of("burn"), 1 - ui,
                                                I(0), ts);
            } else if (type == "remove_fixed_out" || type == "remove_imb") {
                // cf mode: burn_frac may re-scale the LP leg, but amount_i
                // stays an absolute coin amount (this path is defined by the
                // fixed output, not by a supply share) — if it no longer fits
                // the event reverts, which is the intended signal.
                if (ev.contains("i") && ev.contains("amount_i")) {
                    outputs = ytc::apply_remove_fixed_out(
                        p, burn_of("burn"), ev.at("i").get<int>(),
                        ju(ev.at("amount_i")), ts);
                } else {
                    // the fork's RemoveLiquidityImbalance log carries BOTH
                    // final amounts but not which one the caller fixed —
                    // try i=0, accept if the derived other leg matches the
                    // log, else i=1 (the harness flags a double miss)
                    const json& am = ev.at("amounts_expected");
                    I a0 = ju(am.at(0)), a1 = ju(am.at(1));
                    ytc::Pool snap2 = p;
                    I bn = burn_of("burn");
                    outputs = ytc::apply_remove_fixed_out(p, bn, 0, a0, ts);
                    bool ok0 = outputs.contains("amounts")
                        && ju(outputs.at("amounts").at(1)) == a1;
                    if (!ok0) {
                        p = snap2;
                        outputs = ytc::apply_remove_fixed_out(p, bn, 1, a1, ts);
                        outputs["inferred_i"] = 1;
                    } else {
                        outputs["inferred_i"] = 0;
                    }
                }
            } else if (type == "ramp_ag") {
                outputs = ytc::apply_ramp_ag(p, ju(ev.at("future_A")),
                                             ju(ev.at("future_gamma")),
                                             ju(ev.at("future_time")), ts);
            } else if (type == "stop_ramp_ag") {
                outputs = ytc::apply_stop_ramp_ag(p, ts);
            } else if (type == "new_params") {
                // NewParameters logs FINAL values (sentinels already resolved)
                p.mid_fee = ju(ev.at("mid_fee"));
                p.out_fee = ju(ev.at("out_fee"));
                p.fee_gamma = ju(ev.at("fee_gamma"));
                p.step_min = ev.contains("adjustment_step_min")
                                 ? ju(ev.at("adjustment_step_min"))
                                 : ju(ev.at("allowed_extra_profit"));
                p.step_max = ev.contains("adjustment_step_max")
                                 ? ju(ev.at("adjustment_step_max"))
                                 : ju(ev.at("adjustment_step"));
                p.ma_time = ju(ev.at("ma_time"));
                outputs = json::object();
            } else if (type == "set_donation_params") {
                p.donation_duration = ju(ev.at("duration"));
                p.dp_period = ju(ev.at("donation_protection_period"));
                p.dp_lp_threshold = ju(ev.at("donation_protection_lp_threshold"));
                p.dp_ext_remainder = 0;
                p.donation_shares_max_ratio = ju(ev.at("donation_shares_max_ratio"));
                outputs = json::object();
            } else if (type == "set_fee_params") {
                I rpf = ju(ev.at("reserved_profit_fraction"));
                I af = ju(ev.at("admin_fee"));
                if (!(rpf <= ytc::FEE_PRECISION()))
                    throw std::runtime_error("reserved profit fraction above 1e10");
                if (!(af <= ytc::MAX_ADMIN_FEE()))
                    throw std::runtime_error("admin fee above max");
                p.reserved_profit_fraction = rpf;
                p.admin_fee = af;
                outputs = json::object();
            } else if (type == "set_policy") {
                ytc::Policy np;   // absent by default
                if (ev.contains("policy") && !ev.at("policy").is_null() &&
                    !(ev.at("policy").is_string() &&
                      ev.at("policy").get<std::string>() == "none"))
                    ytc::parse_policy_params(np, ev.at("policy"));
                if (np.present && ev.contains("policy_state") &&
                    !ev.at("policy_state").is_null())
                    ytc::parse_policy_state(np, ev.at("policy_state"));
                p.pol = np;
                if (np.present && p.D > 0) {
                    // set_policy_contract pushes current state (must succeed)
                    ytc::update_policy_state(p, ts, p.price_scale,
                                             p.price_oracle, p.last_prices);
                }
                outputs = json::object();
            } else {
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
            I gap = ytc::ps_gap_bp(ytc::spot_of(p, ts), p.price_scale);
            if (gap > max_ps_gap) max_ps_gap = gap;
        }
        bool want_probe =
            probe_all || (probe_last && ev_idx + 1 == n_ev_total) ||
            (ev.contains("probe") && truthy(ev.at("probe")));
        if (want_probe) {
            any_probe = true;
            probes.push_back(ytc::make_probe(p, ts, static_cast<int>(ev_idx)));
        }
        ++ev_idx;
    }

    json final_state = json{
        {"balances", json::array({S(p.balances[0]), S(p.balances[1])})},
        {"D", S(p.D)},
        {"price_scale", ytc::parr(p.price_scale)},
        {"price_oracle", ytc::parr(p.price_oracle)},
        {"last_prices", ytc::parr(p.last_prices)},
        {"last_timestamp", S(p.last_timestamp)},
        {"virtual_price", S(p.virtual_price)},
        {"xcp_profit", S(p.xcp_profit)},
        {"lp_xcp_profit", S(p.lp_xcp_profit)},
        {"total_supply", S(p.total_supply)},
        {"admin_balances",
         json::array({S(p.admin_balances[0]), S(p.admin_balances[1])})},
        {"donation_shares", S(p.donation_shares)},
        {"last_donation_release_ts", S(p.last_donation_release_ts)},
        {"donation_protection_expiry_ts", S(p.dp_expiry_ts)},
        {"donation_protection_extension_remainder", S(p.dp_ext_remainder)},
        {"last_admin_fee_claim_timestamp", S(p.last_admin_fee_claim_ts)}};

    if (p.pol.present) {
        final_state["policy_state"] =
            json{{"last_update_ts", S(p.pol.last_update_ts)},
                 {"last_prices", S(p.pol.last_prices)},
                 {"fast_ema", S(p.pol.fast_ema)},
                 {"slow_ema", S(p.pol.slow_ema)},
                 {"price_scale", S(p.pol.price_scale)}};
    }

    nlohmann::json result;
    result["events"] = out_events;
    result["final"] = final_state;

    // ---- engine contract v2 result additions -------------------------------
    // "admin" is coin-denominated (this fork never mints LP for the DAO), so
    // admin_lp is always 0; "fee_lp" carries the LP-token-denominated add /
    // withdraw fee, which has no per-coin denomination (see header).
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
