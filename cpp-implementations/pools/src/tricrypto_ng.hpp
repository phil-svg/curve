#pragma once
// ============================================================================
// tricrypto_ng.hpp — wei-exact replay state machine for Curve Tricrypto-NG
// (the deployed CurveTricryptoOptimizedWETH.vy family; target pool:
//  TricryptoUSDT 0xf5f5b97624542d72a9e06f04804bf81baa15e2b4, version "v2.0.0").
//
// SOURCES FOLLOWED (authoritative, fetched 2026-08-28):
//   pool: https://raw.githubusercontent.com/curvefi/tricrypto-ng/main/
//         contracts/main/CurveTricryptoOptimizedWETH.vy
//         (pragma 0.3.10, version = "v2.0.0" — matches the deployed
//          TricryptoUSDT; ported line-by-line: _exchange, add_liquidity,
//          remove_liquidity, remove_liquidity_one_coin/_calc_withdraw_one_coin,
//          _claim_admin_fees, tweak_price, _A_gamma, _fee, _calc_token_fee,
//          get_xcp, ramp_A_gamma / stop_ramp_A_gamma / apply_new_parameters
//          storage effects).
//   math: global-sim-ui/engine-cpp/reference/tricrypto_math.vy (the verified
//         deployed CurveCryptoMathOptimized3) — get_p and _snekmate_wad_exp
//         ported here 1:1 with int256 SAR/SDIV semantics; newton_D (incl.
//         K0_prev warm-start seed), get_y (analytic cubic + _newton_y
//         fallback), geometric_mean and reduction_coefficient/_fee are reused
//         from crypto_math.hpp (cm::tri_* — the exact ports validated by the
//         457/483 wei-exact quote engine; cross-checked against the vyper).
//   sim_crypto/pool_ld.hpp was used for control-flow reference only: it is a
//   long-double (floating point) mirror, so ALL of its math was re-derived
//   here in exact cpp_int; nothing numeric was lifted from it.
//
// CRITICAL SEMANTICS (each verified against the vyper source):
//   1. block.timestamp := each event's "ts", set before applying the event.
//      tweak_price updates the EMA only when last_prices_timestamp <
//      block.timestamp, then stores last_prices_timestamp = ts — so for
//      consecutive events in one block only the first runs the EMA step
//      (with the PREVIOUS block's last_prices), exactly like the chain.
//   2. Packed storage (price_scale/_oracle/last_prices 2-per-word, fee and
//      rebalancing params 3-per-word) is kept UNPACKED here; the job supplies
//      unpacked decimal strings. _pack_prices' `assert p < PRICE_MASK`
//      (2**128-1) is enforced at every store site (EMA store, last_prices
//      store, price_scale commit) so packing-overflow reverts are reproduced.
//      last_prices_timestamp is a PLAIN uint256 in this contract (slot 6) —
//      NOT packed with any other timestamp (that packing is twocrypto-ng).
//   3. _claim_admin_fees() runs at the END of add_liquidity and at the START
//      of remove_liquidity and remove_liquidity_one_coin (plus the standalone
//      claim_admin_fees() external). It early-returns unless
//      xcp_profit > xcp_profit_a and totalSupply >= 10**18. Its coin "gulp"
//      (balances[i] = ERC20.balanceOf(self)) is a NO-OP here: in this
//      contract swap/withdraw fees stay inside self.balances, so tracked ==
//      actual unless someone donated tokens directly (see uncertainties).
//      When factory.fee_receiver() != 0 and fees > 0 it mints LP via
//      mint_relative (supply grows), xcp_profit -= 2*fees; in ALL non-early-
//      return cases D is re-derived via newton_D(xp) and virtual_price is
//      recomputed WITHOUT the "Loss" check, then xcp_profit_a := xcp_profit.
//      We assume fee_receiver is set (true on mainnet factory).
//   4. exchange: dy = xp[j] - get_y(..)[0]; xp[j] = y; dy -= 1 (checked, so
//      dy==0 reverts); fee on the post-trade xp with xp[j]=y; balances[j] -=
//      dy AFTER fee (fee stays in the pool); xp[j] is then rebuilt from the
//      final raw balance before tweak_price(A_gamma, xp, 0, K0_prev=get_y[1]).
//      Under an active ramp, self.D is first re-written from newton_D on the
//      PRE-trade xp (a real storage write, rolled back on revert).
//   5. add_liquidity: old_D = self.D, or newton_D(xp_old) under a ramp;
//      D = newton_D(xp_new); d_token = supply*D/old_D - supply;
//      d_token_fee = _calc_token_fee(amountsp, xp)*d_token/1e10 + 1;
//      mint happens BEFORE tweak_price(A_gamma, xp, D) (supply includes the
//      mint inside tweak); AddLiquidity's logged token_supply is the supply
//      BEFORE the trailing admin-fee claim — we report that logged value as
//      "supply" so outputs compare 1:1 against the on-chain event.
//   6. remove_liquidity (proportional): claim first, then burn; amount -= 1
//      ("favor LPs") in the non-emptying case — the D reduction
//      D -= D*amount/total_supply uses the DECREMENTED amount and the
//      PRE-BURN total_supply; no tweak_price, no EMA, no price action.
//      The job's "supply_after" (== the event's token_supply) determines
//      _amount = totalSupply_after_claim - supply_after.
//   7. remove_liquidity_one_coin: claim first; _calc_withdraw_one_coin
//      charges the fee on D (D_fee = fee*dD/(2*1e10) + 1, fee = out_fee if
//      the imprecise-xp correction underflows); price_scale_i for i>0 uses
//      price_scale[i-1] * precisions[i]; burn + balance update happen BEFORE
//      tweak_price(A_gamma, xp, D_reduced, 0).
//   8. A/gamma ramp: _A_gamma() interpolates between initial_A_gamma and
//      future_A_gamma while ts < future_A_gamma_time. ramp_ag events store
//      the logged values directly (RampAgamma logs exactly what ramp_A_gamma
//      writes: initial = current _A_gamma() at that block). ANN is stored
//      raw on-chain (already * N**N * A_MULTIPLIER) and passed to cm:: as-is.
//   9. EMA alpha = wad_exp(-int256((ts - last_ts) * 10**18 // ma_time)) with
//      ma_time = RAW stored value (seconds/ln2); snekmate wad_exp is ported
//      with exact int256 semantics (SAR '>>', SDIV, two's-complement
//      reinterpret + mod-2**256 wrap of the final multiply, >>256 -> 0).
//  10. tweak_price: EMA cap min(last_prices, 2*price_scale); "Loss" assert
//      (virtual_price > old strictly) only when future_A_gamma_time < ts;
//      rebalance gate 2*vp - 1e18 > xcp_profit + 2*allowed_extra_profit;
//      norm = isqrt(sum((p_o*1e18/p_s - 1e18)^2)) (NOT 1e18-based);
//      step = max(adjustment_step, norm/5); commit requires the frac bounds
//      asserts to PASS (they revert the whole tx otherwise) and
//      vp_new > 1e18 and 2*vp_new - 1e18 > xcp_profit.
//  11. donation/donation_shares: not present in this contract (YB fork only).
//  12. Reverts: any vyper assert / checked-arithmetic underflow / div-by-zero
//      throws; the event is reported {"revert": msg}, pre-event state is
//      restored, replay continues.
//
// SEMANTIC UNCERTAINTIES (see also final report):
//   a. The coin gulp in _claim_admin_fees reads real ERC20 balances; direct
//      token donations to the pool (no pool event) would be gulped on-chain
//      but are invisible to this replay — balances would drift from that
//      point. No known donations for TricryptoUSDT.
//   b. fee_receiver is assumed non-zero for the whole replay window (checked:
//      mainnet factory 0x0c0E5f2f... has one). If it were unset, claims
//      would skip the mint but still refresh D/virtual_price/xcp_profit_a.
//   c. remove_liquidity's claim_admin_fees=False variant (rare caller opt-out)
//      is not distinguishable from the event alone; we always claim, like the
//      default. A replayed opt-out call would drift supply slightly.
//   d. GitHub main-branch pool source was used; it declares version "v2.0.0"
//      which matches the deployed TricryptoUSDT, but the byte-level identity
//      with the verified Etherscan source was not re-diffed here.
//
// ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md) — purely additive; with
// none of the new job fields set the event outputs and "final" are byte-for-
// byte what they were before.
//   job:  "probe_all" / "probe_last" / "cf" (bool), per-event "probe" (bool)
//         and, in cf mode, per-event "burn_frac" (1e18 fraction of the LIVE
//         totalSupply to burn) on "remove" / "remove_one".
//   out:  result["probes"] (only when a probe was requested) and
//         result["meter"] (always).
//  v2.1 "spot" is MATH.get_p(self.xp(), self.D, A, gamma)[k] * price_scale[k]
//       / 1e18 — the pool's OWN internal spot definition, i.e. exactly the
//       quantity tweak_price stores into last_prices, recomputed from the
//       post-event state. get_p returns d(xp0)/d(xp_k+1) in the scaled
//       coordinate system; the price_scale multiply undoes the scaling and
//       self.xp()'s PRECISIONS leg folds the decimals in, so the result is the
//       REAL, fee-free, whole-token price of coin k+1 in coin-0 units, 1e18.
//  v2.2 "ps_gap_bp" (per j, in every probe) and meter "max_ps_gap_bp" (the run
//       maximum over j AND over every event, probe or not) are
//       |spot_j * 1e18 / price_scale_j - 1e18| in basis points. This is the
//       input to the project's price_scale-vs-spot freeze rule, so it is
//       tracked inline and never requires a probe.
//  v2.3 Meter fee accounting for this family:
//       - exchange: the fee is charged on the OUTPUT coin (dy is reduced) and
//         stays inside self.balances -> meter["fee"][j].
//       - remove_liquidity_one_coin: the fee is charged on D; the contract
//         logs the coin-i-denominated approx_fee = N*D_fee*xx[i]/D, which is
//         what lands in meter["fee"][i].
//       - add_liquidity: the fee is an LP-TOKEN haircut (d_token_fee) with no
//         coin denomination -> meter["fee_lp"] (LP wei), not meter["fee"].
//       - the DAO slice is taken by MINTING LP to the factory fee receiver
//         (mint_relative), never in coin units, so meter["admin"] is all
//         zeros and meter["admin_lp"] accumulates the LP minted over the run.
//  v2.4 probes omit "adm": this family has no admin_balances bucket.
//  v2.5 "rebase_mul" does not apply to this family and is ignored.
//  v2.7 every probe also carries the meter AS OF that event, under the same
//       units and conventions as v2.3: "cfee"[3], "cfee_lp", "cadm"[3] (all
//       zeros, mirroring meter["admin"]), "cadm_lp" and "cvol"[3]. The last
//       probe's values equal result["meter"] exactly. The meter lives inside
//       Pool, so a reverted event's state restore rolls it back and that
//       event's probe shows the not-counted (pre-event) totals.
//  v2.6 cf "burn_frac" is taken against the total supply as it stands AT THE
//       BURN, i.e. AFTER remove / remove_liquidity_one_coin's leading
//       _claim_admin_fees mint — the same totalSupply the contract itself
//       divides by. (Taking it pre-claim would under-burn by exactly the
//       claim's mint and make a historical burn un-reconstructable.)
// ============================================================================

#include "crypto_math.hpp"

#include <nlohmann/json.hpp>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace tcng {

using u = boost::multiprecision::cpp_int;   // uint256 role (and int256 where noted)
using json = nlohmann::json;

// ---- constants (CurveTricryptoOptimizedWETH.vy) -----------------------------
inline const u PRECISION = cm::E18();                 // 10**18
inline const u A_MULTIPLIER = 10000;
inline const u NOISE_FEE = 100000;                    // 10**5, 0.1 bps
inline const u ADMIN_FEE_DEFAULT("5000000000");       // 5 * 10**9
inline const u PRICE_MASK = (u(1) << 128) - 1;        // 2**128 - 1
inline const u TWO256 = u(1) << 256;

// ---- small helpers ----------------------------------------------------------

// vyper checked subtraction: revert on underflow
inline u csub(const u& a, const u& b) {
    if (b > a) throw std::runtime_error("Integer underflow");
    return a - b;
}

// parse a job uint (decimal string, or a JSON number e.g. for timestamps)
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

// EVM SAR: arithmetic shift right == floor division by 2**n (int256 '>>')
inline u sar(const u& a, unsigned n) {
    if (a >= 0) return a >> n;
    u d = u(1) << n;
    u q = a / d;                 // trunc toward zero
    if (a % d != 0) q -= 1;      // floor for negatives
    return q;
}

// wrap to uint256 (for unsafe_mul / pow_mod256 semantics)
inline u mask256(const u& a) { return a & (TWO256 - 1); }

// _pack_prices' per-price bound (assert p < PRICE_MASK)
inline void check_packable(const u p[2]) {
    cm::req(p[0] < PRICE_MASK && p[1] < PRICE_MASK, "price pack overflow");
}

// ---- MATH.wad_exp — snekmate _snekmate_wad_exp, exact int256 port ----------
// Vyper '>>' on int256 is SAR (floor); unsafe_div is SDIV (trunc toward 0,
// == cpp_int '/'); no intermediate wraps int256 for the reachable domain.
inline u wad_exp(const u& x /* int256 */) {
    if (x <= u("-42139678854452767551")) return 0;
    cm::req(x < u("135305999368893231589"), "wad_exp overflow");

    const u C_LN2("54916777467707473351141471128");           // ln2 * 2**96
    // value = unsafe_div(x << 78, 5**18): shl (no wrap in range) then SDIV
    u value = (x * (u(1) << 78)) / u("3814697265625");        // 5**18

    u k = sar((value * (u(1) << 96)) / C_LN2 + (u(1) << 95), 96);
    value = value - k * C_LN2;

    u y = sar((value + u("1346386616545796478920950773328")) * value, 96)
          + u("57155421227552351082224309758442");
    u p = (sar((y + value - u("94201549194550492254356042504812")) * y, 96)
           + u("28719021644029726153956944680412240")) * value
          + (u("4385272521454847904659076985693276") << 96);

    u q = sar((value - u("2855989394907223263936484059900")) * value, 96)
          + u("50020603652535783019961831881945");
    q = sar(q * value, 96) - u("533845033583426703283633433725380");
    q = sar(q * value, 96) + u("3604857256930695427073651918091429");
    q = sar(q * value, 96) - u("14423608567350463180887372962807573");
    q = sar(q * value, 96) + u("26449188498355588339934803723976023");

    u r = p / q;   // SDIV trunc toward zero

    // convert(convert(r, bytes32), uint256): two's-complement reinterpret
    u r_u = (r < 0) ? u(r + TWO256) : r;
    u prod = mask256(r_u * u("3822833074963236453042738258902158003155416615667"));
    u shift = u(195) - k;               // k in [-61, 195] here
    if (shift >= 256) return 0;         // EVM SHR by >=256 yields 0
    if (shift <= 0) return prod;        // unreachable (k <= 0 for our inputs)
    return prod >> shift.convert_to<unsigned>();
}

// ---- MATH.get_p — exact port (all-uint path; floor divisions) ---------------
// p[k] is dy(coin0)/dy(coin k+1) in the SCALED coordinate system, 1e18 fixed.
inline void get_p(const u xp[3], const u& D, const u& ANN, const u& gamma,
                  u out[2]) {
    cm::req(D > cm::pow_int(10, 17) - 1 &&
            D < cm::pow_int(10, 15) * PRECISION + 1, "unsafe D values");

    // K0 in 1e36 precision
    u K0 = ((27 * xp[0] * xp[1] / D) * xp[2] / D) * cm::pow_int(10, 36) / D;

    // GK0 = 2*K0^3/1e72 + (gamma+1e18)^2 - K0^2/1e36 * (2*gamma + 3e18)/1e18
    u g1 = gamma + PRECISION;
    u GK0 = (2 * K0 * K0 / cm::pow_int(10, 36)) * K0 / cm::pow_int(10, 36)
            + mask256(g1 * g1);                              // pow_mod256
    GK0 = csub(GK0, mask256(K0 * K0) / cm::pow_int(10, 36)
                        * (2 * gamma + 3 * PRECISION) / PRECISION);

    u NNAG2 = mask256(ANN * mask256(gamma * gamma)) / A_MULTIPLIER;

    u denominator = GK0 + (NNAG2 * xp[0] / D) * K0 / cm::pow_int(10, 36);

    out[0] = xp[0] * (GK0 + (NNAG2 * xp[1] / D) * K0 / cm::pow_int(10, 36))
             / xp[1] * PRECISION / denominator;
    out[1] = xp[0] * (GK0 + (NNAG2 * xp[2] / D) * K0 / cm::pow_int(10, 36))
             / xp[2] * PRECISION / denominator;
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
    // params
    u initial_A = 0, initial_gamma = 0;        // unpacked initial_A_gamma
    u future_A = 0, future_gamma = 0;          // unpacked future_A_gamma
    u initial_A_gamma_time = 0, future_A_gamma_time = 0;
    u mid_fee = 0, out_fee = 0, fee_gamma = 0;
    u allowed_extra_profit = 0, adjustment_step = 0, ma_time = 0;  // raw ma
    u admin_fee = ADMIN_FEE_DEFAULT;
    u prec[3];                                 // 10**(18 - decimals[i])

    // state
    u bal[3];                                  // raw token units (fees incl.)
    u D = 0;
    u price_scale[2], price_oracle[2], last_prices[2];
    u last_prices_timestamp = 0;
    u virtual_price = 0, xcp_profit = 0, xcp_profit_a = 0;
    u total_supply = 0;

    u ts = 0;                                  // current block.timestamp

    Meters m;                                  // engine contract v2 meter

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
        return cm::tri_fee(xp, mid_fee, out_fee, fee_gamma);
    }

    // ---- self.get_xcp(D) ----------------------------------------------------
    u get_xcp(const u& D_in) const {
        u x[3];
        x[0] = D_in / 3;
        x[1] = D_in * PRECISION / (3 * price_scale[0]);
        x[2] = D_in * PRECISION / (3 * price_scale[1]);
        return cm::tri_geometric_mean(x);
    }

    // ---- self._calc_token_fee(amounts, xp) ----------------------------------
    u calc_token_fee(const u amounts[3], const u xp[3]) const {
        u f = fee(xp) * 3 / 8;                  // fee * N / (4 * (N-1))
        u Ssum = amounts[0] + amounts[1] + amounts[2];
        u avg = Ssum / 3;
        u Sdiff = 0;
        for (int k = 0; k < 3; ++k)
            Sdiff += (amounts[k] > avg) ? u(amounts[k] - avg) : u(avg - amounts[k]);
        return f * Sdiff / Ssum + NOISE_FEE;    // div-by-zero -> revert, as chain
    }

    // ---- self.tweak_price(A_gamma, _xp, new_D, K0_prev) ---------------------
    // Mutates price_oracle / last_prices / last_prices_timestamp / xcp_profit /
    // D / virtual_price / price_scale exactly as the contract does.
    void tweak_price(const u A_g[2], const u _xp[3], const u& new_D,
                     const u& K0_prev) {
        u old_xcp_profit = xcp_profit;
        u old_virtual_price = virtual_price;

        // ---- EMA update (once per block) ----
        if (last_prices_timestamp < ts) {
            u alpha = wad_exp(-u(csub(ts, last_prices_timestamp) * PRECISION
                                 / ma_time));
            for (int k = 0; k < 2; ++k) {
                u cap = 2 * price_scale[k];
                u lp = last_prices[k] < cap ? last_prices[k] : cap;
                price_oracle[k] =
                    (lp * (PRECISION - alpha) + price_oracle[k] * alpha)
                    / PRECISION;
            }
            check_packable(price_oracle);       // _pack_prices assert
            last_prices_timestamp = ts;
        }

        // ---- D_unadjusted ----
        u D_unadjusted = new_D;
        if (new_D == 0)
            D_unadjusted = cm::tri_newton_D(A_g[0], A_g[1], _xp, K0_prev);

        // ---- last_prices = get_p * price_scale ----
        u gp[2];
        get_p(_xp, D_unadjusted, A_g[0], A_g[1], gp);
        for (int k = 0; k < 2; ++k)
            last_prices[k] = gp[k] * price_scale[k] / PRECISION;
        check_packable(last_prices);            // _pack_prices assert

        // ---- profit numbers without price adjustment ----
        u xp_eq[3];
        xp_eq[0] = D_unadjusted / 3;
        xp_eq[1] = D_unadjusted * PRECISION / (3 * price_scale[0]);
        xp_eq[2] = D_unadjusted * PRECISION / (3 * price_scale[1]);

        u new_xcp_profit = PRECISION;
        u new_virtual_price = PRECISION;
        if (old_virtual_price > 0) {
            u xcp = cm::tri_geometric_mean(xp_eq);
            new_virtual_price = PRECISION * xcp / total_supply;
            new_xcp_profit = old_xcp_profit * new_virtual_price
                             / old_virtual_price;
            if (future_A_gamma_time < ts)
                cm::req(new_virtual_price > old_virtual_price, "Loss");
        }
        xcp_profit = new_xcp_profit;

        // ---- rebalance price_scale if enough profit ----
        // (checked sub: vp*2 < 1e18 would revert on-chain -> csub throws)
        if (csub(new_virtual_price * 2, PRECISION)
                > new_xcp_profit + 2 * allowed_extra_profit) {
            u norm = 0;
            for (int k = 0; k < 2; ++k) {
                u ratio = price_oracle[k] * PRECISION / price_scale[k];
                ratio = (ratio > PRECISION) ? u(ratio - PRECISION)
                                            : u(PRECISION - ratio);
                norm += ratio * ratio;
            }
            norm = cm::isqrt_u(norm);           // NOT in 1e18 base
            u step = adjustment_step;
            { u n5 = norm / 5; if (n5 > step) step = n5; }

            if (norm > step) {
                u p_new[2];
                for (int k = 0; k < 2; ++k)
                    p_new[k] = (price_scale[k] * (norm - step)
                                + step * price_oracle[k]) / norm;

                // xp at new prices
                u xp2[3] = { _xp[0],
                             _xp[1] * p_new[0] / price_scale[0],
                             _xp[2] * p_new[1] / price_scale[1] };

                u D_new = cm::tri_newton_D(A_g[0], A_g[1], xp2, 0);

                for (int k = 0; k < 3; ++k) {
                    u frac = xp2[k] * PRECISION / D_new;
                    cm::req(frac > cm::pow_int(10, 16) - 1 &&
                            frac < cm::pow_int(10, 20) + 1,
                            "unsafe p_new");    // reverts whole tx, as chain
                }

                xp2[0] = D_new / 3;
                xp2[1] = D_new * PRECISION / (3 * p_new[0]);
                xp2[2] = D_new * PRECISION / (3 * p_new[1]);

                u vp_new = PRECISION * cm::tri_geometric_mean(xp2)
                           / total_supply;

                // vyper `and` is non-short-circuit: 2*vp - 1e18 is evaluated
                // (and would underflow-revert) even when vp <= 1e18.
                u vp2m1 = csub(2 * vp_new, PRECISION);
                if (vp_new > PRECISION && vp2m1 > new_xcp_profit) {
                    check_packable(p_new);      // _pack_prices assert
                    D = D_new;
                    virtual_price = vp_new;
                    price_scale[0] = p_new[0];
                    price_scale[1] = p_new[1];
                    return;
                }
            }
        }

        // no adjustment: commit unadjusted values
        D = D_unadjusted;
        virtual_price = new_virtual_price;
    }

    // ---- self._claim_admin_fees() -------------------------------------------
    // Returns LP minted to the fee receiver (0 if the early-return fired or
    // fees rounded to zero). The coin gulp is a no-op here (header note 3).
    u claim_admin_fees() {
        u A_g[2];
        A_gamma(A_g);

        u xcp = xcp_profit;
        u xcp_a = xcp_profit_a;
        if (xcp <= xcp_a || total_supply < PRECISION) return 0;

        // gulp: self.balances[i] = ERC20(coins[i]).balanceOf(self) — no-op.

        u vprice = virtual_price;
        u fees = csub(xcp, xcp_a) * admin_fee / (2 * cm::pow_int(10, 10));

        u claimed = 0;
        if (fees > 0) {   // fee_receiver assumed set (mainnet factory has one)
            u frac = csub(vprice * PRECISION / csub(vprice, fees), PRECISION);
            u d_supply = total_supply * frac / PRECISION;   // mint_relative
            if (d_supply > 0) {
                total_supply += d_supply;
                claimed = d_supply;
                m.admin_lp += d_supply;                    // meter only
            }
            xcp = csub(xcp, fees * 2);
            xcp_profit = xcp;
        }

        // recalculate D (we "gulped"), then virtual_price (no Loss check)
        u xp_c[3];
        xp_now(xp_c);
        D = cm::tri_newton_D(A_g[0], A_g[1], xp_c, 0);
        virtual_price = PRECISION * get_xcp(D) / total_supply;
        xcp_profit_a = xcp;
        return claimed;
    }
};

// ---- json helpers -----------------------------------------------------------

inline json ps_json(const Pool& p) {
    return json::array({S(p.price_scale[0]), S(p.price_scale[1])});
}

// ---- engine contract v2: spot price, ps gap, probes -------------------------

// REAL, fee-free marginal price of coin j (j = 1, 2) in coin-0 units,
// 1e18-scaled. This is EXACTLY the quantity the pool itself stores in
// last_prices: MATH.get_p(xp, D, A, gamma) returns d(xp0)/d(xp_j) in the
// SCALED coordinate system, and multiplying by price_scale[j-1] undoes the
// scaling to give the whole-token price (token decimals are already folded in
// by self.xp()'s PRECISIONS leg). Zero-size, zero-fee by construction.
// Degenerate states (get_p's D / xp asserts, division by zero on an emptied
// pool) yield zeros rather than propagating a throw into the driver.
inline void spot_of(const Pool& p, u out[2]) {
    out[0] = 0;
    out[1] = 0;
    try {
        u A_g[2];
        p.A_gamma(A_g);
        u xp[3];
        p.xp_now(xp);
        if (xp[0] == 0 || xp[1] == 0 || xp[2] == 0) return;
        u gp[2];
        get_p(xp, p.D, A_g[0], A_g[1], gp);
        for (int k = 0; k < 2; ++k)
            out[k] = gp[k] * p.price_scale[k] / PRECISION;
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

// ---- event appliers (throw == revert; caller restores state) ----------------

// _exchange (post-transfer math path; dx already resolved by the feeder)
inline json apply_exchange(Pool& p, int i, int j, const u& dx) {
    cm::req(i != j, "same coin");
    cm::req(i >= 0 && i < 3 && j >= 0 && j < 3, "coin index out of range");
    cm::req(dx > 0, "do not exchange 0 coins");

    u A_g[2];
    p.A_gamma(A_g);

    u xp[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u y = xp[j];
    u x0 = xp[i];
    xp[i] = x0 + dx;
    p.bal[i] = xp[i];

    xp[0] *= p.prec[0];
    xp[1] = xp[1] * p.price_scale[0] * p.prec[1] / PRECISION;
    xp[2] = xp[2] * p.price_scale[1] * p.prec[2] / PRECISION;

    // ---- update invariant if A/gamma are ramping (real storage write) ----
    if (p.future_A_gamma_time > p.ts) {
        x0 *= p.prec[i];
        if (i > 0) x0 = x0 * p.price_scale[i - 1] / PRECISION;
        u x1 = xp[i];
        xp[i] = x0;
        p.D = cm::tri_newton_D(A_g[0], A_g[1], xp, 0);
        xp[i] = x1;
    }

    // ---- dy and fee ----
    u y_out[2];
    cm::tri_get_y(A_g[0], A_g[1], xp, p.D, j, y_out);
    u dy = csub(xp[j], y_out[0]);
    xp[j] = y_out[0];
    dy = csub(dy, 1);

    if (j > 0) dy = dy * PRECISION / p.price_scale[j - 1];
    dy /= p.prec[j];

    u fee_amt = p.fee(xp) * dy / cm::pow_int(10, 10);
    dy = csub(dy, fee_amt);

    y = csub(y, dy);
    p.bal[j] = y;              // fee remains in the pool balance

    y *= p.prec[j];
    if (j > 0) y = y * p.price_scale[j - 1] / PRECISION;
    xp[j] = y;

    p.tweak_price(A_g, xp, 0, y_out[1]);

    p.m.vol[i] += dx;                  // meter only
    p.m.fee[j] += fee_amt;             // meter only (fee lands on coin j)

    return json{{"dy", S(dy)}, {"fee", S(fee_amt)}, {"price_scale", ps_json(p)}};
}

// add_liquidity
inline json apply_add(Pool& p, const u amounts[3]) {
    cm::req(amounts[0] + amounts[1] + amounts[2] > 0, "no coins to add");

    u A_g[2];
    p.A_gamma(A_g);

    u xp[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u xp_old[3] = { xp[0], xp[1], xp[2] };
    for (int k = 0; k < 3; ++k) {
        xp[k] += amounts[k];
        p.bal[k] = xp[k];
    }

    xp[0] *= p.prec[0];
    xp_old[0] *= p.prec[0];
    for (int k = 1; k < 3; ++k) {
        xp[k] = xp[k] * p.price_scale[k - 1] * p.prec[k] / PRECISION;
        xp_old[k] = xp_old[k] * p.price_scale[k - 1] * p.prec[k] / PRECISION;
    }

    u amountsp[3] = { 0, 0, 0 };
    for (int k = 0; k < 3; ++k)
        if (amounts[k] > 0) amountsp[k] = csub(xp[k], xp_old[k]);

    u old_D;
    if (p.future_A_gamma_time > p.ts)
        old_D = cm::tri_newton_D(A_g[0], A_g[1], xp_old, 0);
    else
        old_D = p.D;

    u D_new = cm::tri_newton_D(A_g[0], A_g[1], xp, 0);

    u token_supply = p.total_supply;
    u d_token;
    if (old_D > 0)
        d_token = csub(token_supply * D_new / old_D, token_supply);
    else
        d_token = p.get_xcp(D_new);
    cm::req(d_token > 0, "nothing minted");

    u d_token_fee = 0;
    if (old_D > 0) {
        d_token_fee = p.calc_token_fee(amountsp, xp) * d_token
                      / cm::pow_int(10, 10) + 1;
        d_token = csub(d_token, d_token_fee);
        token_supply += d_token;
        p.total_supply += d_token;                     // mint BEFORE tweak
        p.m.fee_lp += d_token_fee;                     // meter only
        p.tweak_price(A_g, xp, D_new, 0);
    } else {
        p.D = D_new;
        p.virtual_price = PRECISION;
        p.xcp_profit = PRECISION;
        p.xcp_profit_a = PRECISION;
        p.total_supply += d_token;
    }

    // AddLiquidity's token_supply is logged BEFORE the trailing claim:
    u logged_supply = token_supply;
    json out{{"minted", S(d_token)},
             {"supply", S(logged_supply)},
             {"fee", S(d_token_fee)},
             {"price_scale", ps_json(p)}};

    p.claim_admin_fees();                              // trailing claim
    return out;
}

// remove_liquidity (proportional; supply_after == the event's token_supply).
// burn_frac (engine contract v2 cf mode, 1e18-scaled) replaces the historical
// supply_after: the burn becomes that fraction of the supply as it stands at
// the burn, i.e. AFTER the leading claim. nullptr == today's behaviour.
inline json apply_remove(Pool& p, const u& supply_after,
                         const u* burn_frac = nullptr) {
    p.claim_admin_fees();                              // leading claim

    u total_supply = p.total_supply;                   // pre-burn
    u _amount = burn_frac ? u(total_supply * (*burn_frac) / PRECISION)
                          : csub(total_supply, supply_after);
    cm::req(_amount <= total_supply, "burn exceeds supply");
    u amount = _amount;

    u balances0[3] = { p.bal[0], p.bal[1], p.bal[2] };
    u d_balances[3] = { 0, 0, 0 };

    p.total_supply = csub(p.total_supply, _amount);    // burnFrom

    if (amount == total_supply) {                      // Case 2: empty pool
        for (int k = 0; k < 3; ++k) {
            d_balances[k] = balances0[k];
            p.bal[k] = 0;
        }
    } else {                                           // Case 1
        amount = csub(amount, 1);                      // favor LPs
        for (int k = 0; k < 3; ++k) {
            d_balances[k] = balances0[k] * amount / total_supply;
            p.bal[k] = csub(balances0[k], d_balances[k]);
        }
    }

    // D reduced with the (possibly decremented) amount, pre-burn supply:
    p.D = csub(p.D, p.D * amount / total_supply);

    json amts = json::array({S(d_balances[0]), S(d_balances[1]), S(d_balances[2])});
    return json{{"amounts", amts}, {"supply", S(csub(total_supply, _amount))}};
}

// remove_liquidity_one_coin. burn_frac (engine contract v2 cf mode,
// 1e18-scaled) replaces token_amount with that fraction of the supply as it
// stands at the burn, i.e. AFTER the leading claim. nullptr == today.
inline json apply_remove_one(Pool& p, const u& token_amount_in, int i,
                             const u* burn_frac = nullptr) {
    u A_g[2];
    p.A_gamma(A_g);

    p.claim_admin_fees();                              // leading claim

    // ---- _calc_withdraw_one_coin ----
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
        if (i == k) price_scale_i = pk * xp[i];        // xp[i] still == prec[i]
        xp[k] = xp[k] * xx[k] * pk / PRECISION;
    }

    u D0;
    if (p.future_A_gamma_time > p.ts)                  // update_D during ramp
        D0 = cm::tri_newton_D(A_g[0], A_g[1], xp, 0);
    else
        D0 = p.D;
    u Dv = D0;

    // fee on D via imprecise post-withdrawal xp
    u xp_imprecise[3] = { xp[0], xp[1], xp[2] };
    u xp_correction = xp[i] * 3 * token_amount / token_supply;
    u fee_rate = p.out_fee;
    if (xp_correction < xp_imprecise[i]) {
        xp_imprecise[i] -= xp_correction;
        fee_rate = p.fee(xp_imprecise);
    }

    u dD = token_amount * Dv / token_supply;
    u D_fee = fee_rate * dD / (2 * cm::pow_int(10, 10)) + 1;
    // approx_fee: the coin-i-denominated fee the contract logs for this
    // withdrawal (the fee itself is charged on D). Meter only.
    u approx_fee = 3 * D_fee * xx[i] / Dv;
    p.m.fee[i] += approx_fee;

    Dv = csub(Dv, csub(dD, D_fee));

    u y_out[2];
    cm::tri_get_y(A_g[0], A_g[1], xp, Dv, i, y_out);
    u dy = csub(xp[i], y_out[0]) * PRECISION / price_scale_i;
    xp[i] = y_out[0];

    // ---- state updates + tweak ----
    p.bal[i] = csub(p.bal[i], dy);
    p.total_supply = csub(p.total_supply, token_amount);   // burnFrom
    p.tweak_price(A_g, xp, Dv, 0);

    return json{{"dy", S(dy)}, {"supply", S(p.total_supply)},
                {"price_scale", ps_json(p)}};
}

// standalone claim_admin_fees()
inline json apply_claim(Pool& p) {
    u claimed = p.claim_admin_fees();
    return json{{"claimed", S(claimed)}, {"supply", S(p.total_supply)}};
}

} // namespace tcng

// ---- entry point ------------------------------------------------------------

inline nlohmann::json run_tricrypto_ng(const nlohmann::json& job) {
    using tcng::u;
    using tcng::ju;
    using tcng::S;
    using json = nlohmann::json;

    cm::req(job.at("n").get<int>() == 3, "tricrypto_ng needs n == 3");

    tcng::Pool p;

    const json& decs = job.at("decimals");
    for (int k = 0; k < 3; ++k) {
        int d = decs.at(static_cast<size_t>(k)).get<int>();
        cm::req(d >= 0 && d <= 18, "bad decimals");
        p.prec[k] = cm::pow_int(10, 18 - d);
    }

    const json& prm = job.at("params");
    p.initial_A = ju(prm.at("A"));                 // unpacked initial_A_gamma
    p.initial_gamma = ju(prm.at("gamma"));
    p.future_A = ju(prm.at("future_A"));           // unpacked future_A_gamma
    p.future_gamma = ju(prm.at("future_gamma"));
    p.initial_A_gamma_time = ju(prm.at("initial_A_gamma_time"));
    p.future_A_gamma_time = ju(prm.at("future_A_gamma_time"));
    p.mid_fee = ju(prm.at("mid_fee"));
    p.out_fee = ju(prm.at("out_fee"));
    p.fee_gamma = ju(prm.at("fee_gamma"));
    p.allowed_extra_profit = ju(prm.at("allowed_extra_profit"));
    p.adjustment_step = ju(prm.at("adjustment_step"));
    p.ma_time = ju(prm.at("ma_time"));             // RAW stored value
    if (prm.contains("admin_fee")) p.admin_fee = ju(prm.at("admin_fee"));

    const json& st = job.at("state");
    for (int k = 0; k < 3; ++k) p.bal[k] = ju(st.at("balances").at(static_cast<size_t>(k)));
    for (int k = 0; k < 2; ++k) {
        p.price_scale[k] = ju(st.at("price_scale").at(static_cast<size_t>(k)));
        p.price_oracle[k] = ju(st.at("price_oracle").at(static_cast<size_t>(k)));
        p.last_prices[k] = ju(st.at("last_prices").at(static_cast<size_t>(k)));
    }
    p.last_prices_timestamp = ju(st.at("last_prices_timestamp"));
    p.D = ju(st.at("D"));
    p.virtual_price = ju(st.at("virtual_price"));
    p.xcp_profit = ju(st.at("xcp_profit"));
    p.xcp_profit_a = st.contains("xcp_profit_a") ? ju(st.at("xcp_profit_a"))
                                                 : p.xcp_profit;
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

        // cf mode: burn amounts become fractions of the LIVE total supply (the
        // historical supply_after / burn describes a state path that no longer
        // exists). Outside cf mode burn_frac is ignored entirely.
        const bool has_bf = cf_mode && ev.contains("burn_frac");
        const u burn_frac_val = has_bf ? ju(ev.at("burn_frac")) : u(0);

        tcng::Pool snapshot = p;   // restored on revert
        bool reverted = false, skipped = false;

        try {
            json outputs;

            if (type == "exchange") {
                outputs = tcng::apply_exchange(p,
                                               ev.at("sold_id").get<int>(),
                                               ev.at("bought_id").get<int>(),
                                               ju(ev.at("dx")));
            } else if (type == "add") {
                u amts[3];
                for (int k = 0; k < 3; ++k)
                    amts[k] = ju(ev.at("amounts").at(static_cast<size_t>(k)));
                outputs = tcng::apply_add(p, amts);
            } else if (type == "remove") {
                outputs = tcng::apply_remove(
                    p, has_bf ? u(0) : ju(ev.at("supply_after")),
                    has_bf ? &burn_frac_val : nullptr);
            } else if (type == "remove_one") {
                outputs = tcng::apply_remove_one(
                    p, has_bf ? u(0) : ju(ev.at("burn")),
                    ev.at("i").get<int>(),
                    has_bf ? &burn_frac_val : nullptr);
            } else if (type == "claim_admin") {
                outputs = tcng::apply_claim(p);
            } else if (type == "ramp_ag") {
                // RampAgamma logs exactly what ramp_A_gamma stores:
                // initial_* = current _A_gamma() at that block, initial_time =
                // block.timestamp, future_* = requested — store directly.
                p.initial_A = ju(ev.at("initial_A"));
                p.future_A = ju(ev.at("future_A"));
                p.initial_gamma = ju(ev.at("initial_gamma"));
                p.future_gamma = ju(ev.at("future_gamma"));
                p.initial_A_gamma_time = ju(ev.at("initial_time"));
                p.future_A_gamma_time = ju(ev.at("future_time"));
                outputs = json::object();
            } else if (type == "stop_ramp_ag") {
                u A = ju(ev.at("A")), g = ju(ev.at("gamma"));
                p.initial_A = A;
                p.future_A = A;
                p.initial_gamma = g;
                p.future_gamma = g;
                p.initial_A_gamma_time = p.ts;
                p.future_A_gamma_time = p.ts;
                outputs = json::object();
            } else if (type == "new_params") {
                // NewParameters logs the final applied values.
                if (ev.contains("mid_fee")) p.mid_fee = ju(ev.at("mid_fee"));
                if (ev.contains("out_fee")) p.out_fee = ju(ev.at("out_fee"));
                if (ev.contains("fee_gamma")) p.fee_gamma = ju(ev.at("fee_gamma"));
                if (ev.contains("allowed_extra_profit"))
                    p.allowed_extra_profit = ju(ev.at("allowed_extra_profit"));
                if (ev.contains("adjustment_step"))
                    p.adjustment_step = ju(ev.at("adjustment_step"));
                if (ev.contains("ma_time")) p.ma_time = ju(ev.at("ma_time"));
                outputs = json::object();
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
        tcng::spot_of(p, sp);
        {
            u gap = tcng::max_gap_bp(p, sp);
            if (gap > max_ps_gap) max_ps_gap = gap;
        }
        bool want_probe =
            probe_all || (probe_last && ev_idx + 1 == n_ev_total) ||
            (ev.contains("probe") && ev.at("probe").is_boolean() &&
             ev.at("probe").get<bool>());
        if (want_probe) {
            any_probe = true;
            probes.push_back(tcng::make_probe(p, sp,
                                              static_cast<int>(ev_idx)));
        }
        ++ev_idx;
    }

    json result;
    result["events"] = out_events;
    result["final"] = json{
        {"balances", json::array({S(p.bal[0]), S(p.bal[1]), S(p.bal[2])})},
        {"D", S(p.D)},
        {"price_scale", tcng::ps_json(p)},
        {"price_oracle", json::array({S(p.price_oracle[0]), S(p.price_oracle[1])})},
        {"last_prices", json::array({S(p.last_prices[0]), S(p.last_prices[1])})},
        {"last_prices_timestamp", S(p.last_prices_timestamp)},
        {"virtual_price", S(p.virtual_price)},
        {"xcp_profit", S(p.xcp_profit)},
        {"xcp_profit_a", S(p.xcp_profit_a)},
        {"total_supply", S(p.total_supply)}};

    // ---- engine contract v2 result additions -------------------------------
    // The DAO slice is taken by MINTING LP to the fee receiver, never in coin
    // units, so "admin" is all zeros and "admin_lp" carries it. "fee" is the
    // gross coin-denominated fee (swap fee on the output coin + the
    // remove_one approx_fee); add_liquidity's fee has no coin denomination at
    // all (it is an LP-token haircut) and is reported as "fee_lp".
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
