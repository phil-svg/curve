#pragma once
// ============================================================================
// stable_meta.hpp — wei-exact replay state machines for Curve METAPOOLS
// (coin 1 is a base pool's LP token; exchange_underlying routes through the
// base pool). Each engine embeds a validated base-pool machine and replays a
// MERGED event stream of the metapool AND its base pool.
//
// Kinds:
//   "stable_meta"     classic factory metapool. DEPLOYED SOURCE PORTED:
//                     0xed279fdd11ca84beef15af5d39bb4d4bee23f0ca (LUSD/3CRV)
//                     is a vyper forwarder proxy -> implementation
//                     0x5F890841f657d90E081bAbdB532A05996Af79Fe6, verified on
//                     Blockscout as "3pool metapool implementation contract",
//                     vyper 0.2.8 (the curve-factory MetaUSD template with
//                     BASE_POOL = 3pool hardcoded). Ported line-by-line from
//                     that VERIFIED DEPLOYED source (fetched 2026-08-29).
//                     A_PRECISION = 100, ADMIN_FEE constant 5000000000,
//                     N_COINS = 2, BASE_N_COINS = 3.
//   "stable_meta_ng"  stableswap-ng metapool. DEPLOYED SOURCE PORTED:
//                     0xc09e82f81cb811db0922dd48206fc2e212322caf
//                     ("World Liberty USD1 Pool") verified directly on
//                     Blockscout as CurveStableSwapMetaNG, vyper 0.3.10,
//                     version "v7.0.0". BASE_POOL (immutable) =
//                     0x4f493b7de8aac7d55f71853688b1f7c8f0243c85 ("Strategic
//                     Reserves" 2-coin NG pool, USDC/USDT); BASE_POOL_IS_NG
//                     is true for it, so _meta_add_liquidity uses the NG
//                     path (return value of base add_liquidity, not a
//                     balanceOf delta). admin_fee constant 5000000000.
//
// THE CORE MECHANIC (identical in both flavors):
//   rates[0] = rate_multiplier of coin 0 (10**(36 - decimals); the NG flavor
//              can additionally multiply an oracle rate for asset_type 1/3 —
//              USD1 is asset_type 0, so rates[0] is the constant 1e18; the
//              job may still override per event via "rate0").
//   rates[1] = BASE_POOL.get_virtual_price() — READ LIVE at the top of every
//              op that uses rates, BEFORE any internal base-pool call of that
//              op. The embedded base machine computes it wei-exactly:
//                classic 3pool: D = get_D(xp(RATES, balances), A(ts)) with
//                               A_PRECISION = 1 math; vp = D * 1e18 / supply
//                ng base:       D = get_D(xp(stored_rates, balances), A(ts))
//                               (A_PRECISION=100, NG rounding);
//                               vp = D * 1e18 / total_supply
//
// exchange_underlying(i, j, dx) — underlying index space: 0 = meta coin 0,
// 1..BASE_N = base coin (i-1):
//   i>0, j==0: base.add_liquidity(one-sided dx) -> minted LP = dx_w_fee;
//              meta-level swap 1 -> 0 with x = xp[1] + minted*rates[1]/1e18.
//   i==0, j>0: meta-level swap 0 -> 1 -> dy LP;
//              base.remove_liquidity_one_coin(dy, j-1) -> final dy.
//   i>0, j>0:  pure base.exchange(i-1, j-1, dx); metapool state UNTOUCHED.
//   The final event dy (TokenExchangeUnderlying tokens_bought) is the output
//   of the LAST leg. rates are computed BEFORE the internal base call, so the
//   base vp used is the PRE-call one (matches deployed order of operations).
//   Classic quirk kept: the "Handle potential Tether fees" branch measures
//   the input balanceOf delta only when j == 3 (deployed as-is; a fee-less
//   input token makes dx_w_fee == dx, which is what we use — a真 fee-on-
//   transfer input would break wei parity, documented in the spec).
//
// MERGED STREAM & THE SKIP RULE:
//   The job's "events" is the union of BOTH pools' decoded events over the
//   window, sorted by (blockNumber, logIndex), each tagged
//     "pool": "meta" | "base",  "tx": txhash,  "ts", "block".
//   When the metapool itself calls the base pool, the base pool emits its own
//   events IN THE SAME TX, at a logIndex BELOW the metapool's event (the meta
//   logs last). Those base events MUST NOT be applied from the stream —
//   the meta op performs them internally. Recognition (done by the builder,
//   asserted here): base event whose indexed provider/buyer (topics[1]) is
//   the METAPOOL address => "internal": true. The engine stashes internal
//   base events into a FIFO; the meta event that follows in the same tx
//   consumes them as it makes its internal base calls, and CROSS-CHECKS the
//   stashed event's ground-truth amounts against what the internal call
//   produced (free extra check; any diff is reported and fails validation).
//   Leftover stashed events (wrong tx / never consumed) are counted in
//   final.unconsumed_internal — nonzero fails validation.
//
// SEMANTIC NOTES:
//   * Classic meta get_D is the IN-LOOP form D_P = D_P * D / (x * N_COINS)
//     with the A_PRECISION=100 update and `raise` on non-convergence. This is
//     NOT stable_v1's A_PRECISION=1 form and NOT Plain2Basic's dp-outside
//     n==2 form — it gets its own kernel here. get_y / get_y_D are
//     bit-identical to the NG shapes (sng::get_y / sng::get_y_D reused).
//   * NG meta math == plain stableswap-ng math with n = 2 (same deployed Math
//     contract; verified formula-by-formula against the deployed
//     CurveStableSwapMetaNG source, incl. dynamic fee, ys = (D0+D1)/n for
//     add/imbalance and (D0+D1)/(2n) for remove_one). The sng:: op functions
//     are therefore reused verbatim for the meta level; only
//     exchange_underlying is custom.
//   * Classic meta AddLiquidity / RemoveLiquidityImbalance events emit the
//     PRE-fee D1; NG meta AddLiquidity emits the post-fee D (reused D1) —
//     both matched by the respective op outputs ("invariant").
//   * Classic meta RemoveLiquidityOne carries NO coin index -> inferred by
//     matching the computed dy against the event's coin_amount (i = -1 with
//     "dy_expected", same convention as stable_classic).
//   * NG meta remove_liquidity defaults _claim_admin_fees=True and the
//     factory has a fee_receiver -> meta admin_balances are zeroed on every
//     meta "remove" (same as stable_ng.hpp note 2). Base NG remove events do
//     the same for the base machine (sng::apply_remove).
//   * STANDALONE withdraw_admin_fees() calls on the NG metapool emit NO pool
//     event but zero admin_balances (live balances unaffected). The builder
//     detects them from the two coin Transfer logs out of the metapool
//     (from == metapool, to != 0x0, tx contains no metapool event) and
//     injects a synthetic meta event {"type": "claim_admin",
//     "claimed_expected": [c0, c1]}; the engine zeroes admin_balances and
//     reports the claimed amounts, which the harness compares wei-for-wei
//     against the Transfer values (validated 6/6 in the proof window).
//     The classic metapool needs no such handling: its admin fees accrue
//     OUTSIDE self.balances (balanceOf excess), so claims never touch the
//     replayed state. A standalone claim on the NG BASE pool would be the
//     same blind spot (stable_ng.hpp note 2 caveat), surfacing only in the
//     final admin_balances compare.
//   * Oracle upkeep (last_prices/D EMA, _update TWAP of the classic meta) is
//     not replayed: it never feeds back into the replayed op math and is not
//     part of the compared final state.
//   * A vyper assert / checked-op failure => {"revert": msg} for that event
//     and BOTH machines (meta + base) are restored atomically to the
//     pre-event snapshot.
//   * Base events may carry "rates" / any event may carry "base_rates" (the
//     base pool's stored_rates read from chain at that block): exogenous
//     state, applied before execution, never rolled back. Meta events may
//     carry "rate0" (oracle-scaled coin-0 rate) the same way.
//
// Job schema / result schema: see run_stable_meta() / run_stable_meta_ng()
// at the bottom, and specs/stable_meta.md.
//
// ---- ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md, added 2026-08-29) ----
// Purely additive; with none of the new job fields set the result is
// byte-identical to before EXCEPT the always-emitted result["meter"].
//
//   job:    "probe_all" | "probe_last" | "cf"      (all default false)
//   event:  "probe"                                (default false)
//   event:  "burn_frac"   cf only — burn = total_supply * frac / 1e18 (floor)
//                         for "remove"/"remove_one"; honoured on BOTH meta and
//                         base events, each against its own machine's supply
//   "rebase_mul" is NOT used by this family (neither metapool nor either base
//   pool holds a rebasing coin) and is ignored, as the contract permits.
//
// PROBES AND METER DESCRIBE THE METAPOOL.
//   result["meter"] = {fee[2], admin[2], vol[2], n_events, n_reverts} — the
//   META level only, in META coin units (coin 1 is base-LP tokens). Base-pool
//   fees, including those charged by the base legs of exchange_underlying, are
//   NOT counted: the base pool is a different LP set. n_events counts every
//   EXECUTED stream event (meta and base, excluding stashed internal ones);
//   n_reverts counts those that reverted.
//   For exchange_underlying: vol is credited to the META input coin with the
//   META-level input amount (dx for i == 0, the minted base LP for i > 0);
//   a base-to-base swap (i > 0 && j > 0) never touches the metapool and so
//   contributes nothing to the meter.
//
//   result["probes"][k] = {i, bal[2], sup, adm[2]?, D, vp, spot[1],
//                          cfee[2], cadm[2], cvol[2],
//                          "base": {bal[], sup, adm[]?, D, vp}}
//   "adm" appears for the NG flavor only (the classic metapool keeps no
//   admin_balances array). The nested "base" object carries the base
//   machine's state so the harness can value the metapool's LP leg.
//
//   cfee/cadm/cvol are the meter's fee/admin/vol accumulators AS OF that
//   event — META level only, exactly like result["meter"], with the same
//   units and exclusions (the base machine is never metered, so nothing
//   appears under "base"). The last probe's values equal result["meter"]
//   exactly; a stashed internal base event fires a probe without advancing
//   them. Metering commits only once an op can no longer revert, so a
//   reverted event contributes nothing and its probe shows the pre-event
//   totals. No "cfee_lp"/"cadm_lp" — neither metapool flavor has an
//   LP-denominated fee or mints LP for the DAO.
//
//   spot[0] = 1e18 * (real coin-1 out) / (real coin-0 in) for a zero-size
//   FEE-FREE meta-level trade. COIN 1 OF A METAPOOL IS THE BASE LP TOKEN, so
//   spot[0] reads "base LP tokens per coin 0" — that is the intended meaning,
//   not a USD price. Multiply by base.vp / 1e18 to get the coin-0 price in
//   base-pool virtual units. It is the meta-level marginal price, i.e. the
//   limit of get_dy(0, 1, dx)/dx (NOT get_dy_underlying).
// ============================================================================

#include <boost/multiprecision/cpp_int.hpp>
#include <nlohmann/json.hpp>

#include <deque>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "stable_classic.hpp"
#include "stable_ng.hpp"

namespace smeta {

using u = boost::multiprecision::cpp_int;
using json = nlohmann::json;
namespace SC = stable_classic_detail;

inline const u& P() { static const u v("1000000000000000000"); return v; }   // 1e18
inline const u& FD() { static const u v("10000000000"); return v; }          // 1e10
inline const u A_PRECISION = 100;

// checked ops in vyper-0.2.8 / safe paths of 0.3.10
inline u fsub(const u& a, const u& b) {
    if (b > a) throw std::runtime_error("uint256 underflow");
    return a - b;
}
inline u fdiv(const u& a, const u& b) {
    if (b == 0) throw std::runtime_error("division by zero");
    return a / b;
}
inline u ju(const json& v) { return sng::ju(v); }
inline std::string S(const u& v) { return v.str(); }

inline std::vector<u> jvec(const json& arr, int n, const char* what) {
    if (!arr.is_array() || static_cast<int>(arr.size()) != n)
        throw std::runtime_error(std::string("bad array length for ") + what);
    std::vector<u> r(static_cast<size_t>(n));
    for (int i = 0; i < n; ++i) r[static_cast<size_t>(i)] = ju(arr[static_cast<size_t>(i)]);
    return r;
}
inline json svec(const std::vector<u>& v) {
    json a = json::array();
    for (const auto& x : v) a.push_back(S(x));
    return a;
}

// _A() ramp interpolation on raw stored values (identical formula in both
// deployed metapool sources; values stored already * A_PRECISION)
inline u ramp_A(const u& iA, const u& fA, const u& iAt, const u& fAt, const u& ts) {
    if (ts < fAt) {
        if (fA > iA) return iA + (fA - iA) * fsub(ts, iAt) / fsub(fAt, iAt);
        return iA - (iA - fA) * fsub(ts, iAt) / fsub(fAt, iAt);
    }
    return fA;
}

// ---- classic-meta math kernel ----------------------------------------------
// MetaUSD.get_D (vyper 0.2.8): IN-LOOP D_P = D_P * D / (x * N_COINS),
// A_PRECISION = 100 update, raise on non-convergence.
inline u meta_get_D(const std::vector<u>& xp, const u& amp, int n) {
    u Ssum = 0;
    for (const auto& x : xp) Ssum += x;
    if (Ssum == 0) return 0;
    u D = Ssum;
    const u Ann = amp * n;
    for (int it = 0; it < 255; ++it) {
        u D_P = D;
        for (const auto& x : xp) D_P = fdiv(D_P * D, x * n);
        const u Dprev = D;
        D = fdiv((fdiv(Ann * Ssum, A_PRECISION) + D_P * n) * D,
                 fdiv(fsub(Ann, A_PRECISION) * D, A_PRECISION) + (n + 1) * D_P);
        if (D > Dprev ? D - Dprev <= 1 : Dprev - D <= 1) return D;
    }
    throw std::runtime_error("get_D did not converge");
}

inline std::vector<u> xp_mem2(const std::vector<u>& rates, const std::vector<u>& bal) {
    std::vector<u> r(bal.size());
    for (size_t k = 0; k < bal.size(); ++k) r[k] = rates[k] * bal[k] / P();
    return r;
}

// ---- base-pool virtual price (wei-exact, from the embedded machine) --------

// 3pool StableSwap3Pool.get_virtual_price():
//   D = get_D(_xp(), _A());  return D * PRECISION / token.totalSupply()
inline u base_vp_classic(const SC::Pool& b, const u& ts) {
    const u amp = SC::calc_A(b, ts);
    const u D = SC::get_D_mem(b, b.balances, amp);
    return fdiv(D * P(), b.total_supply);
}

// CurveStableSwapNG.get_virtual_price():
//   xp = _xp_mem(stored_rates, _balances()); D = get_D(xp, _A());
//   return D * PRECISION / total_supply
inline u base_vp_ng(const sng::Pool& b, const u& ts) {
    const std::vector<u> xp = sng::xp_mem(b.rates, b.bal);
    const u D = sng::get_D(xp, sng::A_now(b, ts), b.n);
    return fdiv(D * sng::PRECISION, b.total_supply);
}

// ---- classic metapool state ------------------------------------------------

struct MetaC {
    u rate_mult;                 // 10**(36 - decimals(coin0))
    std::vector<u> bal;          // balances[2] (the contract's storage array)
    u total_supply;
    u fee, admin_fee;            // admin_fee is the ADMIN_FEE constant (5e9)
    u iA, fA, iAt, fAt;          // stored * A_PRECISION
};

inline u meta_A(const MetaC& m, const u& ts) { return ramp_A(m.iA, m.fA, m.iAt, m.fAt, ts); }

// ---- classic metapool ops (ported from the deployed MetaUSD source) --------

// exchange(i, j, dx) — meta-level coins only
inline json c_exchange(MetaC& m, const SC::Pool& base, int i, int j,
                       const u& dx, const u& ts, SC::Meter* mt = nullptr) {
    if (i < 0 || i > 1 || j < 0 || j > 1) throw std::runtime_error("coin index out of range");
    const std::vector<u> rates = {m.rate_mult, base_vp_classic(base, ts)};
    const std::vector<u> old = m.bal;
    const std::vector<u> xp = xp_mem2(rates, old);

    const u x = xp[static_cast<size_t>(i)] + fdiv(dx * rates[static_cast<size_t>(i)], P());
    const u amp = meta_A(m, ts);
    const u D = meta_get_D(xp, amp, 2);
    const u y = sng::get_y(i, j, x, xp, amp, D, 2);   // identical iteration to MetaUSD.get_y

    const u dy_xp = fsub(fsub(xp[static_cast<size_t>(j)], y), 1);
    const u dy_fee = fdiv(dy_xp * m.fee, FD());
    const u dy = fdiv(fsub(dy_xp, dy_fee) * P(), rates[static_cast<size_t>(j)]);
    u dy_admin = fdiv(dy_fee * m.admin_fee, FD());
    dy_admin = fdiv(dy_admin * P(), rates[static_cast<size_t>(j)]);

    m.bal[static_cast<size_t>(i)] = old[static_cast<size_t>(i)] + dx;
    m.bal[static_cast<size_t>(j)] = fsub(old[static_cast<size_t>(j)], dy + dy_admin);
    if (mt) {
        mt->add_fee(j, fdiv(dy_fee * P(), rates[static_cast<size_t>(j)]), dy_admin);
        mt->add_vol(i, dx);
    }
    return json{{"dy", S(dy)}};
}

// add_liquidity(amounts) — event invariant is the PRE-fee D1
inline json c_add(MetaC& m, const SC::Pool& base, const std::vector<u>& amounts, const u& ts,
                  SC::Meter* mt = nullptr) {
    const u amp = meta_A(m, ts);
    const std::vector<u> rates = {m.rate_mult, base_vp_classic(base, ts)};
    const std::vector<u> old = m.bal;
    const u D0 = meta_get_D(xp_mem2(rates, old), amp, 2);

    const u total_supply = m.total_supply;
    std::vector<u> new_bal = old;
    for (int i = 0; i < 2; ++i) {
        if (total_supply == 0 && amounts[static_cast<size_t>(i)] == 0)
            throw std::runtime_error("initial deposit requires all coins");
        new_bal[static_cast<size_t>(i)] += amounts[static_cast<size_t>(i)];
    }
    const u D1 = meta_get_D(xp_mem2(rates, new_bal), amp, 2);
    if (!(D1 > D0)) throw std::runtime_error("D1 must be > D0");

    std::vector<u> fees(2, u(0));
    std::vector<u> admin_take(2, u(0));   // metering only (committed below)
    u mint_amount = 0;
    if (total_supply > 0) {
        const u base_fee = fdiv(m.fee * 2, u(4));    // fee * N / (4 * (N-1)), N = 2
        for (int i = 0; i < 2; ++i) {
            const size_t si = static_cast<size_t>(i);
            const u ideal = fdiv(D1 * old[si], D0);
            const u diff = ideal > new_bal[si] ? ideal - new_bal[si] : new_bal[si] - ideal;
            fees[si] = fdiv(base_fee * diff, FD());
            admin_take[si] = fdiv(fees[si] * m.admin_fee, FD());
            m.bal[si] = fsub(new_bal[si], admin_take[si]);
            new_bal[si] = fsub(new_bal[si], fees[si]);
        }
        const u D2 = meta_get_D(xp_mem2(rates, new_bal), amp, 2);
        mint_amount = fdiv(total_supply * fsub(D2, D0), D0);
    } else {
        m.bal = new_bal;
        mint_amount = D1;
    }
    m.total_supply = total_supply + mint_amount;
    // commit metering only once the op can no longer revert
    if (mt) for (int i = 0; i < 2; ++i)
        mt->add_fee(i, fees[static_cast<size_t>(i)], admin_take[static_cast<size_t>(i)]);
    return json{{"fees", svec(fees)}, {"invariant", S(D1)},
                {"supply", S(m.total_supply)}, {"minted", S(mint_amount)}};
}

// remove_liquidity — proportional; event gives token_supply AFTER the burn
// cf mode derives the burn from "burn_frac" instead of the historical
// supply_after; c_remove_burn is the shared body.
inline json c_remove_burn(MetaC& m, const u& burn) {
    const u total_supply = m.total_supply;
    json amounts = json::array();
    for (int i = 0; i < 2; ++i) {
        const size_t si = static_cast<size_t>(i);
        const u value = fdiv(m.bal[si] * burn, total_supply);
        m.bal[si] = fsub(m.bal[si], value);
        amounts.push_back(S(value));
    }
    m.total_supply = fsub(total_supply, burn);
    return json{{"amounts", amounts}, {"supply", S(m.total_supply)}};
}

inline json c_remove(MetaC& m, const u& supply_after) {
    return c_remove_burn(m, fsub(m.total_supply, supply_after));
}

// remove_liquidity_imbalance — event invariant is the PRE-fee D1, supply after
inline json c_remove_imb(MetaC& m, const SC::Pool& base, const std::vector<u>& amounts, const u& ts,
                         SC::Meter* mt = nullptr) {
    const u amp = meta_A(m, ts);
    const std::vector<u> rates = {m.rate_mult, base_vp_classic(base, ts)};
    const std::vector<u> old = m.bal;
    const u D0 = meta_get_D(xp_mem2(rates, old), amp, 2);

    std::vector<u> new_bal = old;
    for (int i = 0; i < 2; ++i)
        new_bal[static_cast<size_t>(i)] = fsub(new_bal[static_cast<size_t>(i)],
                                               amounts[static_cast<size_t>(i)]);
    const u D1 = meta_get_D(xp_mem2(rates, new_bal), amp, 2);

    std::vector<u> fees(2, u(0));
    std::vector<u> admin_take(2, u(0));   // metering only (committed below)
    const u base_fee = fdiv(m.fee * 2, u(4));
    for (int i = 0; i < 2; ++i) {
        const size_t si = static_cast<size_t>(i);
        const u ideal = fdiv(D1 * old[si], D0);
        const u diff = ideal > new_bal[si] ? ideal - new_bal[si] : new_bal[si] - ideal;
        fees[si] = fdiv(base_fee * diff, FD());
        admin_take[si] = fdiv(fees[si] * m.admin_fee, FD());
        m.bal[si] = fsub(new_bal[si], admin_take[si]);
        new_bal[si] = fsub(new_bal[si], fees[si]);
    }
    const u D2 = meta_get_D(xp_mem2(rates, new_bal), amp, 2);
    const u total_supply = m.total_supply;
    const u burn = fdiv(fsub(D0, D2) * total_supply, D0) + 1;
    if (!(burn > 1)) throw std::runtime_error("zero tokens burned");
    m.total_supply = fsub(total_supply, burn);
    // commit metering only once the op can no longer revert
    if (mt) for (int i = 0; i < 2; ++i)
        mt->add_fee(i, fees[static_cast<size_t>(i)], admin_take[static_cast<size_t>(i)]);
    return json{{"fees", svec(fees)}, {"invariant", S(D1)},
                {"supply", S(m.total_supply)}, {"burned", S(burn)}};
}

// _calc_withdraw_one_coin -> (dy, dy_fee); pure
inline std::pair<u, u> c_calc_one(const MetaC& m, const SC::Pool& base,
                                  const u& burn, int i, const u& ts) {
    if (i < 0 || i > 1) throw std::runtime_error("coin index out of range");
    const u amp = meta_A(m, ts);
    const std::vector<u> rates = {m.rate_mult, base_vp_classic(base, ts)};
    const std::vector<u> xp = xp_mem2(rates, m.bal);
    const u D0 = meta_get_D(xp, amp, 2);
    const u D1 = fsub(D0, fdiv(burn * D0, m.total_supply));
    const u new_y = sng::get_y_D(amp, i, xp, D1, 2);  // identical to MetaUSD.get_y_D

    const u base_fee = fdiv(m.fee * 2, u(4));
    std::vector<u> xp_reduced = xp;
    for (int j = 0; j < 2; ++j) {
        const size_t sj = static_cast<size_t>(j);
        u dx_expected;
        if (j == i) dx_expected = fsub(fdiv(xp[sj] * D1, D0), new_y);
        else        dx_expected = fsub(xp[sj], fdiv(xp[sj] * D1, D0));
        xp_reduced[sj] = fsub(xp[sj], fdiv(base_fee * dx_expected, FD()));
    }
    const size_t si = static_cast<size_t>(i);
    u dy = fsub(xp_reduced[si], sng::get_y_D(amp, i, xp_reduced, D1, 2));
    const u dy_0 = fdiv(fsub(xp[si], new_y) * P(), rates[si]);
    dy = fdiv(fsub(dy, 1) * P(), rates[si]);
    return {dy, fsub(dy_0, dy)};
}

inline u c_apply_one(MetaC& m, const SC::Pool& base, const u& burn, int i, const u& ts,
                     SC::Meter* mt = nullptr) {
    const auto [dy, dy_fee] = c_calc_one(m, base, burn, i, ts);
    const u adm_i = fdiv(dy_fee * m.admin_fee, FD());
    m.bal[static_cast<size_t>(i)] = fsub(m.bal[static_cast<size_t>(i)], dy + adm_i);
    m.total_supply = fsub(m.total_supply, burn);
    if (mt) mt->add_fee(i, dy_fee, adm_i);
    return dy;
}

// remove_liquidity_one_coin — the classic event has no coin index: infer by
// matching the computed dy against the event's coin_amount (dy_expected).
inline json c_remove_one(MetaC& m, const SC::Pool& base, const u& burn, int i,
                         const json& ev, const u& ts, SC::Meter* mt = nullptr) {
    if (i >= 0) {
        const u dy = c_apply_one(m, base, burn, i, ts, mt);
        return json{{"dy", S(dy)}, {"i", i}, {"supply", S(m.total_supply)}};
    }
    if (!ev.contains("dy_expected"))
        throw std::runtime_error("remove_one: i=-1 without dy_expected");
    const u dy_expected = ju(ev.at("dy_expected"));
    int best_i = -1;
    u best_diff = 0;
    bool exact = false;
    for (int cand = 0; cand < 2; ++cand) {
        u dy;
        try { dy = c_calc_one(m, base, burn, cand, ts).first; }
        catch (const std::exception&) { continue; }
        const u diff = dy > dy_expected ? dy - dy_expected : dy_expected - dy;
        if (diff == 0) { best_i = cand; exact = true; break; }
        if (best_i < 0 || diff < best_diff) { best_i = cand; best_diff = diff; }
    }
    if (best_i < 0) throw std::runtime_error("remove_one: all coin candidates revert");
    const u dy = c_apply_one(m, base, burn, best_i, ts, mt);
    json out{{"dy", S(dy)}, {"i", best_i}, {"supply", S(m.total_supply)}};
    if (exact) out["inferred"] = true; else out["matched"] = false;
    return out;
}

// ---- internal base-call bookkeeping ----------------------------------------
// FIFO of base events emitted by the metapool's own base call ("internal");
// consumed by the meta op that follows in the same tx, cross-checked.

struct Pending {
    std::deque<json> q;
    int unconsumed = 0;

    // Any event with a different tx arriving while entries are queued means
    // the plumbing failed (an internal base event was never consumed).
    void flush_other_tx(const std::string& tx, json& errors) {
        while (!q.empty() && q.front().value("tx", std::string()) != tx) {
            errors.push_back(json{{"unconsumed_internal", q.front().value("type", std::string())},
                                  {"tx", q.front().value("tx", std::string())}});
            q.pop_front();
            ++unconsumed;
        }
    }
    // Pop the next internal event; must match tx and type.
    json take(const std::string& tx, const std::string& type, json& checks) {
        if (q.empty()) {
            checks.push_back(json{{"ok", false}, {"error", "no internal base event queued"},
                                  {"want", type}});
            return json();
        }
        json ev = q.front();
        q.pop_front();
        if (ev.value("tx", std::string()) != tx || ev.value("type", std::string()) != type) {
            checks.push_back(json{{"ok", false}, {"error", "internal base event mismatch"},
                                  {"want", type}, {"got", ev.value("type", std::string())},
                                  {"want_tx", tx}, {"got_tx", ev.value("tx", std::string())}});
            return json();
        }
        return ev;
    }
};

// compare one ground-truth field of a stashed internal event vs the value the
// internal call produced (numeric compare; both are decimal strings)
inline void check_eq(json& checks, const std::string& what,
                     const json& expected, const json& got) {
    bool ok;
    if (expected.is_array() && got.is_array() && expected.size() == got.size()) {
        ok = true;
        for (size_t k = 0; k < expected.size(); ++k)
            if (ju(expected[k]) != ju(got[k])) { ok = false; break; }
    } else if (!expected.is_array() && !got.is_array()) {
        ok = ju(expected) == ju(got);
    } else {
        ok = false;
    }
    if (ok) checks.push_back(json{{"ok", true}, {"what", what}});
    else checks.push_back(json{{"ok", false}, {"what", what},
                               {"expected", expected}, {"got", got}});
}

// ---- exchange_underlying: CLASSIC flavor -----------------------------------
// MetaUSD.exchange_underlying ported 1:1. rates (incl. base vp) are computed
// BEFORE the internal base call. dx_w_fee == dx for all fee-less inputs (the
// deployed j==3 Tether-branch measures a balanceOf delta that equals dx).
inline json c_exchange_underlying(MetaC& m, SC::Pool& base, int i, int j,
                                  const u& dx, const u& ts,
                                  const std::string& tx, Pending& pend,
                                  SC::Meter* mt = nullptr) {
    const int base_n = base.n;
    if (i < 0 || i > base_n || j < 0 || j > base_n || i == j)
        throw std::runtime_error("coin index out of range");

    const std::vector<u> rates = {m.rate_mult, base_vp_classic(base, ts)};
    const std::vector<u> old = m.bal;
    const std::vector<u> xp = xp_mem2(rates, old);

    int base_i = 0, base_j = 0, meta_i = 0, meta_j = 0;
    if (i != 0) { base_i = i - 1; meta_i = 1; }
    if (j != 0) { base_j = j - 1; meta_j = 1; }

    json checks = json::array();
    // staged meter deltas — committed only when the whole (possibly two-leg)
    // op has completed without reverting
    bool meta_leg = false;
    int mfee_i = 0, mvol_i = 0;
    u mfee_gross = 0, mfee_admin = 0, mvol_amt = 0;
    u dy;
    if (i == 0 || j == 0) {
        u x;
        u dx_w_fee = dx;
        if (i == 0) {
            x = xp[0] + fdiv(dx * rates[0], P());
        } else {
            // internal: base.add_liquidity(one-sided dx) — consume+check the
            // stashed base AddLiquidity event, apply on the embedded machine
            std::vector<u> base_in(static_cast<size_t>(base_n), u(0));
            base_in[static_cast<size_t>(base_i)] = dx;
            const json bev = pend.take(tx, "add", checks);
            const json r = SC::do_add(base, base_in, ts);
            const u minted = ju(r.at("minted"));
            if (!bev.is_null()) {
                if (bev.contains("amounts")) {
                    json in = json::array();
                    for (const auto& a : base_in) in.push_back(S(a));
                    check_eq(checks, "base_add.amounts", bev.at("amounts"), in);
                }
                if (bev.contains("fees_expected"))
                    check_eq(checks, "base_add.fees", bev.at("fees_expected"), r.at("fees"));
                if (bev.contains("invariant_expected"))
                    check_eq(checks, "base_add.invariant", bev.at("invariant_expected"),
                             r.at("invariant"));
                if (bev.contains("supply_expected"))
                    check_eq(checks, "base_add.supply", bev.at("supply_expected"),
                             r.at("supply"));
            }
            dx_w_fee = minted;
            x = fdiv(dx_w_fee * rates[1], P());
            x += xp[1];
        }
        const u amp = meta_A(m, ts);
        const u D = meta_get_D(xp, amp, 2);
        const u y = sng::get_y(meta_i, meta_j, x, xp, amp, D, 2);
        const u dy_xp = fsub(fsub(xp[static_cast<size_t>(meta_j)], y), 1);
        const u dy_fee = fdiv(dy_xp * m.fee, FD());
        dy = fdiv(fsub(dy_xp, dy_fee) * P(), rates[static_cast<size_t>(meta_j)]);
        u dy_admin = fdiv(dy_fee * m.admin_fee, FD());
        dy_admin = fdiv(dy_admin * P(), rates[static_cast<size_t>(meta_j)]);

        m.bal[static_cast<size_t>(meta_i)] = old[static_cast<size_t>(meta_i)] + dx_w_fee;
        m.bal[static_cast<size_t>(meta_j)] =
            fsub(old[static_cast<size_t>(meta_j)], dy + dy_admin);

        meta_leg = true;
        mfee_i = meta_j;
        mfee_gross = fdiv(dy_fee * P(), rates[static_cast<size_t>(meta_j)]);
        mfee_admin = dy_admin;
        mvol_i = meta_i;
        mvol_amt = dx_w_fee;

        if (j > 0) {
            // internal: base.remove_liquidity_one_coin(dy, base_j)
            const json bev = pend.take(tx, "remove_one", checks);
            const u dy_lp = dy;
            dy = SC::apply_remove_one(base, dy_lp, base_j, ts);
            if (!bev.is_null()) {
                if (bev.contains("burn"))
                    check_eq(checks, "base_one.burn", bev.at("burn"), S(dy_lp));
                if (bev.contains("dy_expected"))
                    check_eq(checks, "base_one.dy", bev.at("dy_expected"), S(dy));
            }
        }
    } else {
        // pure base swap: metapool storage untouched
        const json bev = pend.take(tx, "exchange", checks);
        const json r = SC::do_exchange(base, base_i, base_j, dx, ts);
        dy = ju(r.at("dy"));
        if (!bev.is_null()) {
            if (bev.contains("sold_id"))
                check_eq(checks, "base_ex.i", bev.at("sold_id"), base_i);
            if (bev.contains("bought_id"))
                check_eq(checks, "base_ex.j", bev.at("bought_id"), base_j);
            if (bev.contains("dx"))
                check_eq(checks, "base_ex.dx", bev.at("dx"), S(dx));
            if (bev.contains("dy_expected"))
                check_eq(checks, "base_ex.dy", bev.at("dy_expected"), S(dy));
        }
    }
    if (mt && meta_leg) {
        mt->add_fee(mfee_i, mfee_gross, mfee_admin);
        mt->add_vol(mvol_i, mvol_amt);
    }
    json out{{"dy", S(dy)}};
    if (!checks.empty()) out["internal_checks"] = checks;
    return out;
}

// ---- exchange_underlying: NG flavor ----------------------------------------
// CurveStableSwapMetaNG.exchange_underlying ported 1:1 (BASE_POOL_IS_NG path:
// dx_w_fee for a base-coin deposit is the RETURN VALUE of the base pool's
// add_liquidity). The meta machine is an sng::Pool (n=2) whose working rates
// are set to [rate0, base_vp] before the op.
inline json ng_exchange_underlying(sng::Pool& m, sng::Pool& base, int i, int j,
                                   const u& dx, const u& ts,
                                   const std::string& tx, Pending& pend,
                                   sng::Meter* mt = nullptr) {
    const int base_n = base.n;
    if (i < 0 || i > base_n || j < 0 || j > base_n || i == j)
        throw std::runtime_error("coin index out of range");
    if (dx == 0) throw std::runtime_error("do not exchange 0 coins");

    // rates BEFORE any base interaction (deployed order: _stored_rates() is
    // the first thing exchange_underlying does)
    m.rates[1] = base_vp_ng(base, ts);
    const std::vector<u> rates = m.rates;
    const std::vector<u> old = m.bal;                 // _balances(): live
    const std::vector<u> xp = sng::xp_mem(rates, old);

    int base_i = 0, base_j = 0, meta_i = 0, meta_j = 0;
    if (i > 0) { base_i = i - 1; meta_i = 1; }
    if (j > 0) { base_j = j - 1; meta_j = 1; }

    json checks = json::array();
    // staged meter deltas — committed only when the whole (possibly two-leg)
    // op has completed without reverting
    bool meta_leg = false;
    int mfee_i = 0, mvol_i = 0;
    u mfee_gross = 0, mfee_admin = 0, mvol_amt = 0;

    // ---- _transfer_in ------------------------------------------------------
    u dx_w_fee = dx;
    if (i > 0 && j > 0) {
        // is_base_pool_swap: input goes straight to the base swap; the meta's
        // stored_balances are NOT touched.
    } else if (i > 0) {
        // _meta_add_liquidity (NG base): minted = base.add_liquidity(...)
        std::vector<u> base_in(static_cast<size_t>(base_n), u(0));
        base_in[static_cast<size_t>(base_i)] = dx;
        const json bev = pend.take(tx, "add", checks);
        const json r = sng::apply_add(base, base_in, ts);
        const u minted = ju(r.at("minted"));
        if (!bev.is_null()) {
            if (bev.contains("amounts")) {
                json in = json::array();
                for (const auto& a : base_in) in.push_back(S(a));
                check_eq(checks, "base_add.amounts", bev.at("amounts"), in);
            }
            if (bev.contains("fees_expected"))
                check_eq(checks, "base_add.fees", bev.at("fees_expected"), r.at("fees"));
            if (bev.contains("invariant_expected"))
                check_eq(checks, "base_add.invariant", bev.at("invariant_expected"),
                         r.at("invariant"));
            if (bev.contains("supply_expected"))
                check_eq(checks, "base_add.supply", bev.at("supply_expected"), r.at("supply"));
        }
        dx_w_fee = minted;
        m.bal[1] += dx_w_fee;         // stored_balances[1] += _dx (live +=, admin unchanged)
    } else {
        m.bal[0] += dx;               // stored_balances[0] += measured dx
    }

    u dy;
    if (i == 0 || j == 0) {
        // ---- meta-level __exchange (identical math to sng::apply_exchange
        // core; the balance credit was already done above) -------------------
        const u x = xp[static_cast<size_t>(meta_i)] +
                    dx_w_fee * rates[static_cast<size_t>(meta_i)] / sng::PRECISION;
        const u amp = sng::A_now(m, ts);
        const u D = sng::get_D(xp, amp, 2);
        const u y = sng::get_y(meta_i, meta_j, x, xp, amp, D, 2);

        const u dy_xp = sng::csub(sng::csub(xp[static_cast<size_t>(meta_j)], y), 1);
        const u dy_fee = dy_xp *
                         sng::dynamic_fee((xp[static_cast<size_t>(meta_i)] + x) / 2,
                                          (xp[static_cast<size_t>(meta_j)] + y) / 2,
                                          m.fee, m.offpeg) /
                         sng::FEE_DENOMINATOR;
        dy = (dy_xp - dy_fee) * sng::PRECISION / rates[static_cast<size_t>(meta_j)];
        const u admin_j = (dy_fee * m.admin_fee / sng::FEE_DENOMINATOR) * sng::PRECISION /
                          rates[static_cast<size_t>(meta_j)];
        m.adminb[static_cast<size_t>(meta_j)] += admin_j;
        // stored_balances[meta_j] -= dy; admin slice also leaves the live bal
        m.bal[static_cast<size_t>(meta_j)] =
            sng::csub(m.bal[static_cast<size_t>(meta_j)], dy + admin_j);

        meta_leg = true;
        mfee_i = meta_j;
        mfee_gross = dy_fee * sng::PRECISION / rates[static_cast<size_t>(meta_j)];
        mfee_admin = admin_j;
        mvol_i = meta_i;
        mvol_amt = dx_w_fee;

        if (j > 0) {
            // internal: base.remove_liquidity_one_coin(dy, base_j)
            const json bev = pend.take(tx, "remove_one", checks);
            const u dy_lp = dy;
            const json r = sng::apply_remove_one(base, dy_lp, base_j, ts);
            dy = ju(r.at("dy"));
            if (!bev.is_null()) {
                if (bev.contains("i"))
                    check_eq(checks, "base_one.i", bev.at("i"), base_j);
                if (bev.contains("burn"))
                    check_eq(checks, "base_one.burn", bev.at("burn"), S(dy_lp));
                if (bev.contains("dy_expected"))
                    check_eq(checks, "base_one.dy", bev.at("dy_expected"), S(dy));
            }
        }
    } else {
        // pure base swap; metapool storage untouched
        const json bev = pend.take(tx, "exchange", checks);
        const json r = sng::apply_exchange(base, base_i, base_j, dx_w_fee, ts);
        dy = ju(r.at("dy"));
        if (!bev.is_null()) {
            if (bev.contains("sold_id"))
                check_eq(checks, "base_ex.i", bev.at("sold_id"), base_i);
            if (bev.contains("bought_id"))
                check_eq(checks, "base_ex.j", bev.at("bought_id"), base_j);
            if (bev.contains("dx"))
                check_eq(checks, "base_ex.dx", bev.at("dx"), S(dx_w_fee));
            if (bev.contains("dy_expected"))
                check_eq(checks, "base_ex.dy", bev.at("dy_expected"), S(dy));
        }
    }
    if (mt && meta_leg) {
        mt->add_fee(mfee_i, mfee_gross, mfee_admin);
        mt->add_vol(mvol_i, mvol_amt);
    }
    json out{{"dy", S(dy)}};
    if (!checks.empty()) out["internal_checks"] = checks;
    return out;
}

// ---- base-event application (non-internal stream events) -------------------

// `cf` only changes how burns are derived (burn_frac against the BASE
// machine's own supply); the base pool is never metered or probed here.
inline json apply_base_classic(SC::Pool& b, const json& ev, const u& ts, bool cf = false) {
    const std::string type = ev.at("type").get<std::string>();
    if (type == "exchange")
        return SC::do_exchange(b, ev.at("sold_id").get<int>(),
                               ev.at("bought_id").get<int>(),
                               SC::parse_u(ev.at("dx")), ts);
    if (type == "add")
        return SC::do_add(b, SC::parse_u_array(ev.at("amounts"), b.n, "amounts"), ts);
    if (type == "remove") {
        if (cf && ev.contains("burn_frac"))
            return SC::do_remove_burn(
                b, fdiv(b.total_supply * ju(ev.at("burn_frac")), P()));
        return SC::do_remove(b, SC::parse_u(ev.at("supply_after")));
    }
    if (type == "remove_one") {
        const u burn = (cf && ev.contains("burn_frac"))
                           ? fdiv(b.total_supply * ju(ev.at("burn_frac")), P())
                           : SC::parse_u(ev.at("burn"));
        return SC::do_remove_one(b, burn, ev.value("i", -1), ev, ts);
    }
    if (type == "remove_imb")
        return SC::do_remove_imbalance(b, SC::parse_u_array(ev.at("amounts"), b.n, "amounts"), ts);
    if (type == "ramp_a") return SC::do_ramp_a(b, ev);
    if (type == "stop_ramp") return SC::do_stop_ramp(b, ev, ts);
    if (type == "new_fee") return SC::do_new_fee(b, ev);
    return json{{"skipped", true}};
}

inline json apply_base_ng(sng::Pool& b, const json& ev, const u& ts, bool cf = false) {
    const std::string type = ev.at("type").get<std::string>();
    if (type == "exchange")
        return sng::apply_exchange(b, ev.at("sold_id").get<int>(),
                                   ev.at("bought_id").get<int>(), ju(ev.at("dx")), ts);
    if (type == "add")
        return sng::apply_add(b, sng::jvec(ev.at("amounts"), b.n), ts);
    if (type == "remove") {
        if (cf && ev.contains("burn_frac"))
            return sng::apply_remove_burn(
                b, b.total_supply * ju(ev.at("burn_frac")) / sng::PRECISION);
        return sng::apply_remove(b, ju(ev.at("supply_after")), ts);
    }
    if (type == "remove_one") {
        const u burn = (cf && ev.contains("burn_frac"))
                           ? b.total_supply * ju(ev.at("burn_frac")) / sng::PRECISION
                           : ju(ev.at("burn"));
        return sng::apply_remove_one(b, burn, ev.at("i").get<int>(), ts);
    }
    if (type == "remove_imb")
        return sng::apply_remove_imb(b, sng::jvec(ev.at("amounts"), b.n), ts);
    if (type == "ramp_a") {
        b.initial_A = ju(ev.at("initial_A"));
        b.future_A = ju(ev.at("future_A"));
        b.initial_A_time = ju(ev.at("initial_time"));
        b.future_A_time = ju(ev.at("future_time"));
        return json::object();
    }
    if (type == "stop_ramp") {
        const u A = ju(ev.at("A"));
        b.initial_A = A; b.future_A = A;
        b.initial_A_time = ts; b.future_A_time = ts;
        return json::object();
    }
    if (type == "new_fee") {
        b.fee = ju(ev.at("fee"));
        b.offpeg = ju(ev.at("offpeg_fee_multiplier"));
        return json::object();
    }
    return json{{"skipped", true}};
}

// ---- probes (engine contract v2) -------------------------------------------
// The probe describes the METAPOOL; a nested "base" object carries the base
// machine's state (bal/sup/D/vp, plus adm for the NG base) so the harness can
// value the metapool's LP leg. Any revert inside a probe yields 0 for the
// affected number; a probe never aborts or mutates a replay.

inline json base_probe_classic(const SC::Pool& b, const u& ts) {
    u D = 0, vp = 0;
    try {
        D = SC::get_D_mem(b, b.balances, SC::calc_A(b, ts));
        if (b.total_supply > 0) vp = fdiv(D * P(), b.total_supply);
    } catch (const std::exception&) {
        D = 0;
        vp = 0;
    }
    return json{{"bal", svec(b.balances)}, {"sup", S(b.total_supply)},
                {"D", S(D)}, {"vp", S(vp)}};
}

inline json base_probe_ng(const sng::Pool& b, const u& ts) {
    u D = 0, vp = 0;
    try {
        D = sng::get_D(sng::xp_mem(b.rates, b.bal), sng::A_now(b, ts), b.n);
        if (b.total_supply > 0) vp = D * sng::PRECISION / b.total_supply;
    } catch (const std::exception&) {
        D = 0;
        vp = 0;
    }
    return json{{"bal", svec(b.bal)}, {"sup", S(b.total_supply)},
                {"adm", svec(b.adminb)}, {"D", S(D)}, {"vp", S(vp)}};
}

// meta-level marginal price, coin 0 -> coin 1 (coin 1 IS the base LP token):
// spot[0] = 1e18 * dyp * rates[0] / (dxp * rates[1]) with the spec's
// dxp = max(1, xp[0]/1e6) numerical derivative through get_y.
inline void meta_spot(const std::vector<u>& rates, const std::vector<u>& bal,
                      const u& amp, bool classic_kernel, u& spot, u& spot_xp) {
    spot = 0;
    spot_xp = 0;
    try {
        const std::vector<u> xp = xp_mem2(rates, bal);
        const u D = classic_kernel ? meta_get_D(xp, amp, 2) : sng::get_D(xp, amp, 2);
        if (D == 0) return;
        u dxp = xp[0] / 1000000;
        if (dxp == 0) dxp = 1;
        const u y = sng::get_y(0, 1, xp[0] + dxp, xp, amp, D, 2);
        if (xp[1] <= y) return;
        const u dyp = xp[1] - y;
        spot = fdiv(dyp * rates[0] * P(), dxp * rates[1]);
        spot_xp = fdiv(dyp * P(), dxp);
    } catch (const std::exception&) {
        spot = 0;
        spot_xp = 0;
    }
}

inline json probe_classic(const MetaC& m, const SC::Pool& base, int idx, const u& ts,
                          const SC::Meter& mt) {
    u bvp = 0, D = 0, vp = 0, sp = 0, spx = 0;
    try {
        bvp = base_vp_classic(base, ts);
        const std::vector<u> rates = {m.rate_mult, bvp};
        D = meta_get_D(xp_mem2(rates, m.bal), meta_A(m, ts), 2);
        if (m.total_supply > 0) vp = fdiv(D * P(), m.total_supply);
        meta_spot(rates, m.bal, meta_A(m, ts), true, sp, spx);
    } catch (const std::exception&) {
        // leave whatever was computed; missing pieces stay 0
    }
    json spot = json::array(), spot_xp = json::array();
    spot.push_back(S(sp));
    spot_xp.push_back(S(spx));
    json pr{{"i", idx},
            {"bal", svec(m.bal)},
            {"sup", S(m.total_supply)},
            {"D", S(D)},
            {"vp", S(vp)},
            {"spot", spot},
            {"spot_xp", spot_xp},
            {"base", base_probe_classic(base, ts)}};
    mt.cum_into(pr);   // METApool-coin cumulative meter (base is never metered)
    return pr;
}

inline json probe_ng(const sng::Pool& m, const sng::Pool& base, int idx, const u& ts,
                     const sng::Meter& mt) {
    u bvp = 0, D = 0, vp = 0, sp = 0, spx = 0;
    try {
        bvp = base_vp_ng(base, ts);
        const std::vector<u> rates = {m.rates[0], bvp};
        D = sng::get_D(xp_mem2(rates, m.bal), sng::A_now(m, ts), 2);
        if (m.total_supply > 0) vp = D * sng::PRECISION / m.total_supply;
        meta_spot(rates, m.bal, sng::A_now(m, ts), false, sp, spx);
    } catch (const std::exception&) {
        // leave whatever was computed; missing pieces stay 0
    }
    json spot = json::array(), spot_xp = json::array();
    spot.push_back(S(sp));
    spot_xp.push_back(S(spx));
    json pr{{"i", idx},
            {"bal", svec(m.bal)},
            {"sup", S(m.total_supply)},
            {"adm", svec(m.adminb)},
            {"D", S(D)},
            {"vp", S(vp)},
            {"spot", spot},
            {"spot_xp", spot_xp},
            {"base", base_probe_ng(base, ts)}};
    mt.cum_into(pr);   // METApool-coin cumulative meter (base is never metered)
    return pr;
}

// ---- job parsing helpers ---------------------------------------------------

inline SC::Pool parse_base_classic(const json& jb) {
    SC::Pool b;
    const std::string k = jb.at("kind").get<std::string>();
    if (k == "stable_v1") b.v2 = false;
    else if (k == "stable_v2") b.v2 = true;
    else throw std::runtime_error("stable_meta: unsupported classic base kind " + k);
    b.n = jb.at("n").get<int>();
    b.rates = SC::parse_u_array(jb.at("rates"), b.n, "base rates");
    const json& prm = jb.at("params");
    b.initial_A = SC::parse_u(prm.at("initial_A"));
    b.future_A = SC::parse_u(prm.at("future_A"));
    b.initial_A_time = SC::parse_u(prm.at("initial_A_time"));
    b.future_A_time = SC::parse_u(prm.at("future_A_time"));
    b.fee = SC::parse_u(prm.at("fee"));
    b.admin_fee = SC::parse_u(prm.at("admin_fee"));
    const json& st = jb.at("state");
    b.balances = SC::parse_u_array(st.at("balances"), b.n, "base balances");
    b.total_supply = SC::parse_u(st.at("total_supply"));
    return b;
}

inline sng::Pool parse_base_ng(const json& jb) {
    sng::Pool b;
    b.n = jb.at("n").get<int>();
    b.rates = sng::jvec(jb.at("rates"), b.n);
    const json& prm = jb.at("params");
    b.initial_A = ju(prm.at("initial_A"));
    b.future_A = ju(prm.at("future_A"));
    b.initial_A_time = ju(prm.at("initial_A_time"));
    b.future_A_time = ju(prm.at("future_A_time"));
    b.fee = ju(prm.at("fee"));
    b.admin_fee = ju(prm.at("admin_fee"));
    b.offpeg = ju(prm.at("offpeg_fee_multiplier"));
    const json& st = jb.at("state");
    b.bal = sng::jvec(st.at("balances"), b.n);
    b.total_supply = ju(st.at("total_supply"));
    b.adminb = sng::jvec(st.at("admin_balances"), b.n);
    return b;
}

// ---- run: classic ----------------------------------------------------------
//
// Job: { "kind": "stable_meta", "n": 2, "rate_multiplier": dec,
//        "params": {initial_A, future_A, initial_A_time, future_A_time,
//                   fee, admin_fee},
//        "state": {balances[2], total_supply},
//        "base": {"kind": "stable_v1"|"stable_v2", "n", "rates",
//                 "params": {...}, "state": {balances, total_supply}},
//        "events": [ {pool, type, tx, ts, block, ...fields...,
//                     internal?, base_rates?}... ] }
// Result: { "events": [...], "final": {"meta": {balances, total_supply},
//           "base": {balances, total_supply}, "unconsumed_internal": n} }

inline json run_classic(const json& job) {
    MetaC m;
    m.rate_mult = ju(job.at("rate_multiplier"));
    const json& prm = job.at("params");
    m.iA = ju(prm.at("initial_A"));
    m.fA = ju(prm.at("future_A"));
    m.iAt = ju(prm.at("initial_A_time"));
    m.fAt = ju(prm.at("future_A_time"));
    m.fee = ju(prm.at("fee"));
    m.admin_fee = ju(prm.at("admin_fee"));
    const json& st = job.at("state");
    m.bal = jvec(st.at("balances"), 2, "meta balances");
    m.total_supply = ju(st.at("total_supply"));

    SC::Pool base = parse_base_classic(job.at("base"));

    // ---- engine contract v2 job flags (all default OFF) --------------------
    const bool cf = job.value("cf", false);
    const bool probe_all = job.value("probe_all", false);
    const bool probe_last = job.value("probe_last", false);
    SC::Meter mt;
    mt.init(2);                       // METApool coins only
    json probes = json::array();
    bool any_probe = false;

    Pending pend;
    json errors = json::array();
    json out_events = json::array();

    const json& jevents = job.at("events");
    const int n_events = static_cast<int>(jevents.size());
    int idx = -1;

    for (const json& ev : jevents) {
        ++idx;
        const std::string pool = ev.value("pool", std::string("meta"));
        const std::string type = ev.at("type").get<std::string>();
        const std::string tx = ev.value("tx", std::string());
        const u ts = ev.contains("ts") ? ju(ev.at("ts")) : u(0);

        pend.flush_other_tx(tx, errors);

        if (pool == "base" && ev.value("internal", false)) {
            pend.q.push_back(ev);
            out_events.push_back(json{{"pool", "base"}, {"type", type},
                                      {"internal", true}, {"stashed", true}});
            // stashed, not executed -> not counted in n_events; a probe
            // requested on it still fires (state is simply unchanged) so that
            // probe_all/probe_last stay one-per-event
            if (probe_all || ev.value("probe", false) ||
                (probe_last && idx == n_events - 1)) {
                probes.push_back(probe_classic(m, base, idx, ts, mt));
                any_probe = true;
            }
            continue;
        }

        const MetaC msnap = m;
        const SC::Pool bsnap = base;
        json rec;
        ++mt.n_events;
        try {
            json outputs;
            if (pool == "base") {
                outputs = apply_base_classic(base, ev, ts, cf);
            } else if (type == "exchange") {
                outputs = c_exchange(m, base, ev.at("sold_id").get<int>(),
                                     ev.at("bought_id").get<int>(), ju(ev.at("dx")), ts, &mt);
            } else if (type == "exchange_underlying") {
                outputs = c_exchange_underlying(m, base, ev.at("sold_id").get<int>(),
                                                ev.at("bought_id").get<int>(),
                                                ju(ev.at("dx")), ts, tx, pend, &mt);
            } else if (type == "add") {
                outputs = c_add(m, base, jvec(ev.at("amounts"), 2, "amounts"), ts, &mt);
            } else if (type == "remove") {
                if (cf && ev.contains("burn_frac"))
                    outputs = c_remove_burn(
                        m, fdiv(m.total_supply * ju(ev.at("burn_frac")), P()));
                else
                    outputs = c_remove(m, ju(ev.at("supply_after")));
            } else if (type == "remove_one") {
                const u burn = (cf && ev.contains("burn_frac"))
                                   ? fdiv(m.total_supply * ju(ev.at("burn_frac")), P())
                                   : ju(ev.at("burn"));
                outputs = c_remove_one(m, base, burn, ev.value("i", -1), ev, ts, &mt);
            } else if (type == "remove_imb") {
                outputs = c_remove_imb(m, base, jvec(ev.at("amounts"), 2, "amounts"), ts, &mt);
            } else if (type == "ramp_a") {
                m.iA = ju(ev.at("initial_A"));
                m.fA = ju(ev.at("future_A"));
                m.iAt = ju(ev.at("initial_time"));
                m.fAt = ju(ev.at("future_time"));
                outputs = json{{"applied", true}};
            } else if (type == "stop_ramp") {
                const u A = ju(ev.at("A"));
                m.iA = A; m.fA = A; m.iAt = ts; m.fAt = ts;
                outputs = json{{"applied", true}};
            } else if (type == "new_fee") {
                if (ev.contains("fee")) m.fee = ju(ev.at("fee"));
                outputs = json{{"applied", true}};
            } else {
                outputs = json{{"skipped", true}};
            }
            rec = json{{"pool", pool}, {"type", type}, {"outputs", outputs}};
        } catch (const std::exception& e) {
            m = msnap;
            base = bsnap;
            ++mt.n_reverts;
            rec = json{{"pool", pool}, {"type", type}, {"revert", e.what()}};
        }
        out_events.push_back(std::move(rec));

        if (probe_all || ev.value("probe", false) ||
            (probe_last && idx == n_events - 1)) {
            probes.push_back(probe_classic(m, base, idx, ts, mt));
            any_probe = true;
        }
    }
    pend.flush_other_tx("\x01", errors);  // drain any leftovers

    json result;
    result["events"] = std::move(out_events);
    result["final"] = json{
        {"meta", json{{"balances", svec(m.bal)}, {"total_supply", S(m.total_supply)}}},
        {"base", json{{"balances", svec(base.balances)},
                      {"total_supply", S(base.total_supply)}}},
        {"unconsumed_internal", pend.unconsumed}};
    if (!errors.empty()) result["errors"] = errors;
    if (any_probe) result["probes"] = std::move(probes);
    result["meter"] = mt.to_json();
    return result;
}

// ---- run: NG ---------------------------------------------------------------
//
// Job: { "kind": "stable_meta_ng", "n": 2, "rate_multiplier": dec (rate0),
//        "params": {initial_A, future_A, initial_A_time, future_A_time,
//                   fee, admin_fee, offpeg_fee_multiplier},
//        "state": {balances[2], total_supply, admin_balances[2]},
//        "base": {"kind": "stable_ng", "n", "rates", "params" (incl. offpeg),
//                 "state" (incl. admin_balances)},
//        "events": [...] }
// Per-event exogenous updates (applied before execution, not rolled back):
//   base events: "rates" (base stored_rates at that block)
//   any event:   "base_rates" (same), meta events: "rate0"
// Result final adds admin_balances for both machines.

inline json run_ng(const json& job) {
    sng::Pool m;                     // the metapool as an sng machine (n = 2)
    m.n = 2;
    const u rate0 = ju(job.at("rate_multiplier"));
    m.rates = {rate0, u(0)};         // rates[1] set per-op from the base vp
    const json& prm = job.at("params");
    m.initial_A = ju(prm.at("initial_A"));
    m.future_A = ju(prm.at("future_A"));
    m.initial_A_time = ju(prm.at("initial_A_time"));
    m.future_A_time = ju(prm.at("future_A_time"));
    m.fee = ju(prm.at("fee"));
    m.admin_fee = ju(prm.at("admin_fee"));
    m.offpeg = ju(prm.at("offpeg_fee_multiplier"));
    const json& st = job.at("state");
    m.bal = sng::jvec(st.at("balances"), 2);
    m.total_supply = ju(st.at("total_supply"));
    m.adminb = sng::jvec(st.at("admin_balances"), 2);

    sng::Pool base = parse_base_ng(job.at("base"));

    // ---- engine contract v2 job flags (all default OFF) --------------------
    const bool cf = job.value("cf", false);
    const bool probe_all = job.value("probe_all", false);
    const bool probe_last = job.value("probe_last", false);
    sng::Meter mt;
    mt.init(2);                       // METApool coins only
    json probes = json::array();
    bool any_probe = false;

    Pending pend;
    json errors = json::array();
    json out_events = json::array();

    const json& jevents = job.at("events");
    const int n_events = static_cast<int>(jevents.size());
    int idx = -1;

    for (const json& ev : jevents) {
        ++idx;
        const std::string pool = ev.value("pool", std::string("meta"));
        const std::string type = ev.at("type").get<std::string>();
        const std::string tx = ev.value("tx", std::string());
        const u ts = ev.contains("ts") ? ju(ev.at("ts")) : u(0);

        pend.flush_other_tx(tx, errors);

        // exogenous chain state (never rolled back)
        if (ev.contains("base_rates") && !ev.at("base_rates").is_null())
            base.rates = sng::jvec(ev.at("base_rates"), base.n);
        if (pool == "base" && ev.contains("rates") && !ev.at("rates").is_null())
            base.rates = sng::jvec(ev.at("rates"), base.n);
        if (pool == "meta" && ev.contains("rate0"))
            m.rates[0] = ju(ev.at("rate0"));

        if (pool == "base" && ev.value("internal", false)) {
            pend.q.push_back(ev);
            out_events.push_back(json{{"pool", "base"}, {"type", type},
                                      {"internal", true}, {"stashed", true}});
            // stashed, not executed -> not counted in n_events; a probe
            // requested on it still fires (state is simply unchanged) so that
            // probe_all/probe_last stay one-per-event
            if (probe_all || ev.value("probe", false) ||
                (probe_last && idx == n_events - 1)) {
                probes.push_back(probe_ng(m, base, idx, ts, mt));
                any_probe = true;
            }
            continue;
        }

        const sng::Pool msnap = m;
        const sng::Pool bsnap = base;
        json rec;
        ++mt.n_events;
        try {
            json outputs;
            if (pool == "base") {
                outputs = apply_base_ng(base, ev, ts, cf);
            } else if (type == "exchange") {
                m.rates[1] = base_vp_ng(base, ts);   // _stored_rates() live
                outputs = sng::apply_exchange(m, ev.at("sold_id").get<int>(),
                                              ev.at("bought_id").get<int>(),
                                              ju(ev.at("dx")), ts, nullptr, &mt);
            } else if (type == "exchange_underlying") {
                outputs = ng_exchange_underlying(m, base, ev.at("sold_id").get<int>(),
                                                 ev.at("bought_id").get<int>(),
                                                 ju(ev.at("dx")), ts, tx, pend, &mt);
            } else if (type == "add") {
                m.rates[1] = base_vp_ng(base, ts);
                outputs = sng::apply_add(m, sng::jvec(ev.at("amounts"), 2), ts, &mt);
            } else if (type == "remove") {
                if (cf && ev.contains("burn_frac"))
                    outputs = sng::apply_remove_burn(
                        m, m.total_supply * ju(ev.at("burn_frac")) / sng::PRECISION);
                else
                    outputs = sng::apply_remove(m, ju(ev.at("supply_after")), ts);
            } else if (type == "remove_one") {
                m.rates[1] = base_vp_ng(base, ts);
                const u burn = (cf && ev.contains("burn_frac"))
                                   ? m.total_supply * ju(ev.at("burn_frac")) / sng::PRECISION
                                   : ju(ev.at("burn"));
                outputs = sng::apply_remove_one(m, burn,
                                                ev.at("i").get<int>(), ts, nullptr, &mt);
            } else if (type == "remove_imb") {
                m.rates[1] = base_vp_ng(base, ts);
                outputs = sng::apply_remove_imb(m, sng::jvec(ev.at("amounts"), 2), ts,
                                                nullptr, &mt);
            } else if (type == "ramp_a") {
                m.initial_A = ju(ev.at("initial_A"));
                m.future_A = ju(ev.at("future_A"));
                m.initial_A_time = ju(ev.at("initial_time"));
                m.future_A_time = ju(ev.at("future_time"));
                outputs = json::object();
            } else if (type == "stop_ramp") {
                const u A = ju(ev.at("A"));
                m.initial_A = A; m.future_A = A;
                m.initial_A_time = ts; m.future_A_time = ts;
                outputs = json::object();
            } else if (type == "new_fee") {
                m.fee = ju(ev.at("fee"));
                m.offpeg = ju(ev.at("offpeg_fee_multiplier"));
                outputs = json::object();
            } else if (type == "claim_admin") {
                // synthetic event: a standalone withdraw_admin_fees() call
                // (emits NO pool event; the builder detects it from the two
                // coin Transfer logs out of the metapool). Mirrors
                // _withdraw_admin_fees: admin balances transferred out and
                // zeroed; live balances (stored - admin) unaffected.
                outputs = json{{"claimed", sng::svec(m.adminb)}};
                for (auto& a : m.adminb) a = 0;
            } else {
                outputs = json{{"skipped", true}};
            }
            rec = json{{"pool", pool}, {"type", type}, {"outputs", outputs}};
        } catch (const std::exception& e) {
            m = msnap;
            base = bsnap;
            ++mt.n_reverts;
            rec = json{{"pool", pool}, {"type", type}, {"revert", e.what()}};
        }
        out_events.push_back(std::move(rec));

        if (probe_all || ev.value("probe", false) ||
            (probe_last && idx == n_events - 1)) {
            probes.push_back(probe_ng(m, base, idx, ts, mt));
            any_probe = true;
        }
    }
    pend.flush_other_tx("\x01", errors);

    json result;
    result["events"] = std::move(out_events);
    result["final"] = json{
        {"meta", json{{"balances", sng::svec(m.bal)},
                      {"total_supply", S(m.total_supply)},
                      {"admin_balances", sng::svec(m.adminb)}}},
        {"base", json{{"balances", sng::svec(base.bal)},
                      {"total_supply", S(base.total_supply)},
                      {"admin_balances", sng::svec(base.adminb)}}},
        {"unconsumed_internal", pend.unconsumed}};
    if (!errors.empty()) result["errors"] = errors;
    if (any_probe) result["probes"] = std::move(probes);
    result["meter"] = mt.to_json();
    return result;
}

}  // namespace smeta

// ---- entry points (replay.cpp conventions) ---------------------------------

inline nlohmann::json run_stable_meta(const nlohmann::json& job) {
    return smeta::run_classic(job);
}

inline nlohmann::json run_stable_meta_ng(const nlohmann::json& job) {
    return smeta::run_ng(job);
}
