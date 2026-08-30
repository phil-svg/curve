#pragma once
// ============================================================================
// stable_ng.hpp — wei-exact replay state machine for Curve stableswap-ng
// (plain pools, the deployed CurveStableSwapNG.vy — NOT the metapool).
//
// SOURCE FOLLOWED (authoritative, fetched 2026-08-28):
//   https://raw.githubusercontent.com/curvefi/stableswap-ng/main/contracts/main/CurveStableSwapNG.vy
//   pragma version 0.3.10, contract `version = "v7.0.0"` (main branch).
//   Functions ported line-by-line: _A(), _xp_mem, get_D (NG rounding: D_P
//   divided by N**N AFTER the product loop), get_y, get_y_D, _dynamic_fee,
//   __exchange/_exchange, add_liquidity, remove_liquidity,
//   _calc_withdraw_one_coin/remove_liquidity_one_coin,
//   remove_liquidity_imbalance, ramp_A, stop_ramp_A, set_new_fee.
//   Math kernels (get_D / get_y loop shapes) lifted from the validated
//   global-sim-ui/engine-cpp/src/engine.cpp NG path (get_D_prec dp_outside,
//   get_y with a_prec=100), then aligned 1:1 with the vyper above.
//
// VERSION NOTES (deployed 1.0.0 / 1.0.1 / 1.1.0 / 1.2.0):
//   Differences between deployed NG plain-pool versions are in transfer
//   handling (exchange_received gating for rebasing tokens), oracle upkeep
//   and view helpers — the swap/add/remove math and _dynamic_fee above are
//   identical across them. exchange_received uses the same __exchange kernel;
//   its TokenExchangeUnderlying/TokenExchange dx is the resolved balance
//   delta, which is what the feeder supplies as "dx", so no branch is needed.
//
// SEMANTIC DECISIONS / UNCERTAINTIES:
//   1. Balance representation: `bal[]` here is the LIVE math balance
//      (stored_balances - admin_balances), which is exactly what NG's
//      pool.balances(i) / _balances() return and what the job's
//      state.balances carries. Therefore:
//        exchange:  bal[i] += dx;  bal[j] -= (dy + admin_fee_j)
//        add:       bal[i] += amounts[i] - admin_fee_i
//        remove_one/remove_imb: bal[i] -= (out_i + admin_fee_i)
//      because admin_balances[i] += admin_fee_i moves that slice out of the
//      live balance while stored balances only move by actual transfers.
//   2. remove_liquidity: the external call defaults _claim_admin_fees=True
//      and mainnet factories have a fee_receiver set, so we replicate
//      _withdraw_admin_fees(): admin_balances are zeroed on every "remove"
//      event (live balances unaffected). If a replayed tx passed
//      _claim_admin_fees=False (rare) the final admin_balances comparison —
//      not any event output — would drift. Standalone withdraw_admin_fees()
//      calls emit no pool event and cannot be replayed; same caveat.
//   3. ramp_a event: the on-chain RampA log emits old_A = _A() at that block
//      and new_A = _future_A * A_PRECISION, i.e. BOTH already scaled — and
//      those emitted values are bit-identical to what ramp_A stores into
//      initial_A / future_A. So we store the event fields directly; no extra
//      A_PRECISION multiply (multiplying again would double-scale).
//      stop_ramp pins initial_A = future_A = A, both times = ts.
//   4. Vyper checked ops -> csub()/throw here (assert/underflow => the event
//      is reported as {"revert": msg} and the pre-event state is restored).
//      unsafe_div/unsafe_sub/unsafe_mul in ranges the contract guarantees are
//      plain floor ops on cpp_int (values are non-negative, so C++ integer
//      division == floor division). Division by zero throws (boost), which
//      maps to a vyper revert — same observable behavior.
//   5. Revert-detection granularity: _transfer_out underflow is checked on
//      the live balance rather than stored_balances (stored = live + admin,
//      unknown split per coin during the window start). Only distinguishable
//      in a pathological near-empty pool; never hit on healthy replays.
//   6. Event "rates" (pool.stored_rates() at that block) replace the working
//      rates from that event onward, BEFORE execution, and are NOT rolled
//      back on revert — they are exogenous chain state, not pool state.
//   7. Unknown event types are echoed as {"type": t, "skipped": true} with
//      no state change.
//
// ---- ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md, added 2026-08-29) ----
// Purely additive; with none of the new job fields set the result is
// byte-identical to before EXCEPT the always-emitted result["meter"].
//
//   job:    "probe_all" | "probe_last" | "cf"      (all default false)
//   event:  "probe"                                (default false)
//   event:  "burn_frac"   cf only — burn = total_supply * frac / 1e18 (floor)
//                         for "remove" and "remove_one"
//   event:  "rebase_mul"  cf only — [[num,den]|null, ...] per coin, applied to
//                         the LIVE balances BEFORE the event, never rolled back
//
//   result["meter"]  ALWAYS: {fee[], admin[], vol[], n_events, n_reverts},
//                    per coin in COIN units. fee = gross (LP + admin) fee;
//                    admin = the slice added to admin_balances[i] by that op.
//                    vol[i] = sum of exchange dx in.
//   result["probes"] only when a probe was requested:
//                    {i, bal[], sup, adm[], D, vp, spot[n-1],
//                     cfee[], cadm[], cvol[]}. bal is the LIVE
//                    balance (stored - admin), the same convention as
//                    final.balances.
//
//   cfee/cadm/cvol are the meter's fee/admin/vol accumulators AS OF that
//   event (same units, same conventions); the last probe's values equal
//   result["meter"] exactly. Metering commits only once an op can no longer
//   revert, so a reverted event contributes nothing and its probe shows the
//   pre-event totals. No "cfee_lp"/"cadm_lp" — this family has neither an
//   LP-denominated fee nor LP minted for the DAO. Note cadm is a cumulative
//   ACCRUAL: an admin withdrawal zeroing adm[] does not reduce it.
//
//   spot[j-1] = 1e18 * (coin-j out) / (coin-0 in) for a zero-size FEE-FREE
//   trade == lim dx->0 of get_dy(0, j, dx)/dx, in RAW TOKEN units (xp
//   converted back with the working `rates`). "spot_xp[j-1]" is the same
//   number with `rates` folded in (1e18 * dxp_j / dxp_0); it always lands near
//   1e18 and so keeps full precision where `spot` cannot (a 6-decimal /
//   18-decimal leg leaves `spot` near 1e6, i.e. ~6 significant digits).
//       spot = spot_xp * rates[0] / rates[j]
//   NOTE for NG: stored_rates fold in the ORACLE rate as well as decimals, so
//   spot_xp is the rate-ADJUSTED price (≈1e18 for a healthy sDAI/USDC pool),
//   not the market price of the yield-bearing token; `spot` is the raw
//   get_dy ratio and does carry the oracle premium.
//
// cf-mode notes for this family:
//   * "balances_sync", "admin_sync" and "live_pre" are absolute snapshots of
//     the REAL history and are IGNORED in cf mode ("rebase_mul" replaces the
//     first two; without live_pre the rebasing pools fall back to the pure
//     arithmetic decrement, which is the correct counterfactual behaviour);
//   * the rebasing-coin add-amount deduction is driven by the event's
//     "invariant_expected" ground truth, so it is SKIPPED in cf mode (§3.4:
//     *_expected fields are ignored) — the logged amounts are used verbatim;
//   * "rates" stays live in cf mode: it is exogenous truth (§3.3).
// ============================================================================

#include <boost/multiprecision/cpp_int.hpp>
#include <nlohmann/json.hpp>

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace sng {

using u = boost::multiprecision::cpp_int;
using json = nlohmann::json;

// ---- constants (CurveStableSwapNG.vy) --------------------------------------
inline const u PRECISION = u("1000000000000000000"); // 10**18
inline const u FEE_DENOMINATOR = u("10000000000");   // 10**10
inline const u A_PRECISION = 100;

// ---- small helpers ---------------------------------------------------------

// vyper checked subtraction: revert on underflow
inline u csub(const u& a, const u& b) {
    if (b > a) throw std::runtime_error("Integer underflow");
    return a - b;
}

// parse a job uint (decimal string, or a JSON number for e.g. timestamps)
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

inline std::vector<u> jvec(const json& arr, int n) {
    std::vector<u> out;
    out.reserve(static_cast<size_t>(n));
    for (const auto& e : arr) out.push_back(ju(e));
    if (static_cast<int>(out.size()) != n) throw std::runtime_error("bad array length");
    return out;
}

inline json svec(const std::vector<u>& v) {
    json a = json::array();
    for (const auto& x : v) a.push_back(S(x));
    return a;
}

inline u pow_int(const u& b, int e) {
    u r = 1;
    for (int k = 0; k < e; ++k) r *= b;
    return r;
}

// ---- math kernels (lifted from engine.cpp NG path, aligned with vyper) -----

// CurveStableSwapNG.get_D: A_PRECISION=100, D_P divided by N**N AFTER the
// product loop (the NG rounding — this is the ordering validated wei-exact
// on 266 NG pools in engine.cpp's get_D_prec with dp_outside=true).
inline u get_D(const std::vector<u>& xp, const u& amp, int n) {
    u Ssum = 0;
    for (const auto& x : xp) Ssum += x;
    if (Ssum == 0) return 0;

    u D = Ssum;
    u Ann = amp * n;
    for (int it = 0; it < 255; ++it) {
        u D_P = D;
        for (const auto& x : xp) D_P = D_P * D / x; // throws on x==0 == vyper revert
        D_P /= pow_int(u(n), n);
        u Dprev = D;
        D = (Ann * Ssum / A_PRECISION + D_P * n) * D /
            ((Ann - A_PRECISION) * D / A_PRECISION + (n + 1) * D_P);
        if (D > Dprev ? D - Dprev <= 1 : Dprev - D <= 1) return D;
    }
    throw std::runtime_error("get_D did not converge");
}

// CurveStableSwapNG.get_y (calculate xp[j] if xp[i] becomes x)
inline u get_y(int i, int j, const u& x, const std::vector<u>& xp,
               const u& amp, const u& D, int n) {
    if (i == j) throw std::runtime_error("same coin");
    if (j < 0 || j >= n || i < 0 || i >= n) throw std::runtime_error("coin index out of range");

    u S_ = 0;
    u c = D;
    u Ann = amp * n;
    for (int k = 0; k < n; ++k) {
        u xk;
        if (k == i) xk = x;
        else if (k != j) xk = xp[static_cast<size_t>(k)];
        else continue;
        S_ += xk;
        c = c * D / (xk * n);
    }
    c = c * D * A_PRECISION / (Ann * n);
    u b = S_ + D * A_PRECISION / Ann;
    u y = D;
    for (int it = 0; it < 255; ++it) {
        u y_prev = y;
        y = (y * y + c) / (2 * y + b - D);
        if (y > y_prev ? y - y_prev <= 1 : y_prev - y <= 1) return y;
    }
    throw std::runtime_error("get_y did not converge");
}

// CurveStableSwapNG.get_y_D (calculate xp[i] for reduced invariant D)
inline u get_y_D(const u& A, int i, const std::vector<u>& xp, const u& D, int n) {
    if (i < 0 || i >= n) throw std::runtime_error("coin index out of range");

    u S_ = 0;
    u c = D;
    u Ann = A * n;
    for (int k = 0; k < n; ++k) {
        if (k == i) continue;
        const u& xk = xp[static_cast<size_t>(k)];
        S_ += xk;
        c = c * D / (xk * n);
    }
    c = c * D * A_PRECISION / (Ann * n);
    u b = S_ + D * A_PRECISION / Ann;
    u y = D;
    for (int it = 0; it < 255; ++it) {
        u y_prev = y;
        y = (y * y + c) / (2 * y + b - D);
        if (y > y_prev ? y - y_prev <= 1 : y_prev - y <= 1) return y;
    }
    throw std::runtime_error("get_y_D did not converge");
}

// ---- pool state ------------------------------------------------------------

struct Pool {
    int n{0};
    std::vector<u> rates;   // working rate multipliers (updated by event "rates")
    std::vector<u> bal;     // LIVE balances = stored_balances - admin_balances
    std::vector<u> adminb;  // admin_balances
    u total_supply{0};
    // ramp state (A values already * A_PRECISION, exactly as stored on-chain)
    u initial_A{0}, future_A{0}, initial_A_time{0}, future_A_time{0};
    // fee params, 1e10-scaled as on-chain
    u fee{0}, admin_fee{0}, offpeg{0};
};

// ---- revenue meter (engine contract v2) ------------------------------------
// Running accumulators over the whole replay, in COIN units. Every op takes an
// optional `Meter*`; nullptr (the default) means "don't meter", which is what
// stable_meta.hpp passes when it drives this machine as an embedded BASE pool.

struct Meter {
    std::vector<u> fee, admin, vol;
    long long n_events = 0, n_reverts = 0;

    void init(int n) {
        fee.assign(static_cast<size_t>(n), u(0));
        admin.assign(static_cast<size_t>(n), u(0));
        vol.assign(static_cast<size_t>(n), u(0));
    }
    void add_fee(int i, const u& gross, const u& adm) {
        fee[static_cast<size_t>(i)] += gross;
        admin[static_cast<size_t>(i)] += adm;
    }
    void add_vol(int i, const u& dx) { vol[static_cast<size_t>(i)] += dx; }

    json to_json() const {
        return json{{"fee", svec(fee)}, {"admin", svec(admin)}, {"vol", svec(vol)},
                    {"n_events", n_events}, {"n_reverts", n_reverts}};
    }

    // Cumulative-as-of-now snapshot for a probe: the same accumulators
    // to_json() reports at the end of the run, read mid-run. The last probe's
    // values therefore equal result["meter"] exactly. No "cfee_lp"/"cadm_lp":
    // this family has no LP-denominated fee and mints no LP for the DAO.
    void cum_into(json& pr) const {
        pr["cfee"] = svec(fee);
        pr["cadm"] = svec(admin);
        pr["cvol"] = svec(vol);
    }
};

// CurveStableSwapNG._A() — ramp interpolation at timestamp ts
inline u A_now(const Pool& p, const u& ts) {
    if (ts < p.future_A_time) {
        const u& A0 = p.initial_A;
        const u& A1 = p.future_A;
        u dt = csub(ts, p.initial_A_time);          // checked (block.timestamp - t0)
        u span = csub(p.future_A_time, p.initial_A_time);
        if (A1 > A0) return A0 + (A1 - A0) * dt / span;
        return A0 - (A0 - A1) * dt / span;
    }
    return p.future_A; // t1 == 0 or ts >= t1
}

// CurveStableSwapNG._dynamic_fee(xpi, xpj, _fee)
inline u dynamic_fee(const u& xpi, const u& xpj, const u& _fee, const u& offpeg) {
    if (offpeg <= FEE_DENOMINATOR) return _fee;
    u xps2 = xpi + xpj;
    xps2 *= xps2; // (xpi + xpj) ** 2
    return (offpeg * _fee) /
           ((offpeg - FEE_DENOMINATOR) * 4 * xpi * xpj / xps2 + FEE_DENOMINATOR);
}

// CurveStableSwapNG._xp_mem
inline std::vector<u> xp_mem(const std::vector<u>& rates, const std::vector<u>& balances) {
    std::vector<u> out;
    out.reserve(balances.size());
    for (size_t k = 0; k < balances.size(); ++k)
        out.push_back(rates[k] * balances[k] / PRECISION);
    return out;
}

// base_fee = fee * N / (4 * (N - 1))   (unsafe ops, all in range)
inline u base_fee_of(const Pool& p) {
    return p.fee * p.n / (4 * (p.n - 1));
}

// ---- event applications ----------------------------------------------------
// Each mutates `p` exactly as the contract's storage writes do, and returns
// the "outputs" object. Any throw == vyper revert (caller restores state).

// Rebasing-token pools (factory asset_type 2, pool_contains_rebasing_tokens
// in the deployed source): _transfer_out RE-SYNCS stored_balances[i] to
// balanceOf(self) - amount, so the stored balance silently absorbs any
// rebase drift at every outbound transfer. The optional live_pre argument
// carries that pre-transfer balanceOf (harness-attached, read at block-1)
// per coin; when present for an outbound coin the new live balance is
// (balanceOf_pre - amount_out) - admin_balances[coin] instead of the pure
// arithmetic decrement.
using LivePre = std::vector<std::optional<u>>;

// exchange(i, j, dx) — _exchange + __exchange. dx is the event's tokens_sold
// (fee-on-transfer already resolved on-chain).
inline json apply_exchange(Pool& p, int i, int j, const u& dx, const u& ts,
                           const LivePre* live_pre = nullptr, Meter* mt = nullptr) {
    if (i == j) throw std::runtime_error("coin index out of range (i==j)");
    if (i < 0 || i >= p.n || j < 0 || j >= p.n)
        throw std::runtime_error("coin index out of range");
    if (dx == 0) throw std::runtime_error("do not exchange 0 coins");

    std::vector<u> xp = xp_mem(p.rates, p.bal); // from old (live) balances

    // _transfer_in: stored_balances[i] += dx  -> live bal[i] += dx
    p.bal[static_cast<size_t>(i)] += dx;

    u x = xp[static_cast<size_t>(i)] + dx * p.rates[static_cast<size_t>(i)] / PRECISION;

    // __exchange
    u amp = A_now(p, ts);
    u D = get_D(xp, amp, p.n);
    u y = get_y(i, j, x, xp, amp, D, p.n);

    u dy_xp = csub(csub(xp[static_cast<size_t>(j)], y), 1); // xp[j] - y - 1, checked
    u dy_fee = dy_xp *
               dynamic_fee((xp[static_cast<size_t>(i)] + x) / 2,
                           (xp[static_cast<size_t>(j)] + y) / 2,
                           p.fee, p.offpeg) /
               FEE_DENOMINATOR;

    u dy = (dy_xp - dy_fee) * PRECISION / p.rates[static_cast<size_t>(j)];

    u admin_j = (dy_fee * p.admin_fee / FEE_DENOMINATOR) * PRECISION /
                p.rates[static_cast<size_t>(j)];
    p.adminb[static_cast<size_t>(j)] += admin_j;

    // _transfer_out: stored[j] -= dy; live = stored - admin also loses admin_j
    size_t sj = static_cast<size_t>(j);
    if (live_pre && (*live_pre)[sj])
        p.bal[sj] = csub(csub(*(*live_pre)[sj], dy), p.adminb[sj]);
    else
        p.bal[sj] = csub(p.bal[sj], dy + admin_j);

    if (mt) {
        // gross fee lands on the OUTPUT coin; dy_fee is in xp units, converted
        // to coin units exactly as dy itself is
        mt->add_fee(j, dy_fee * PRECISION / p.rates[sj], admin_j);
        mt->add_vol(i, dx);
    }

    return json{{"dy", S(dy)}};
}

// add_liquidity(_amounts)
inline json apply_add(Pool& p, const std::vector<u>& amounts, const u& ts,
                      Meter* mt = nullptr) {
    const int n = p.n;
    u amp = A_now(p, ts);
    std::vector<u> old_balances = p.bal; // _balances()
    u D0 = get_D(xp_mem(p.rates, old_balances), amp, n);

    u total_supply = p.total_supply;
    std::vector<u> new_balances = old_balances;

    for (int i = 0; i < n; ++i) {
        if (amounts[static_cast<size_t>(i)] > 0)
            new_balances[static_cast<size_t>(i)] += amounts[static_cast<size_t>(i)];
        else if (total_supply == 0)
            throw std::runtime_error("initial deposit requires all coins");
    }

    u D1 = get_D(xp_mem(p.rates, new_balances), amp, n);
    if (!(D1 > D0)) throw std::runtime_error("D1 must be > D0");

    std::vector<u> fees(static_cast<size_t>(n), u(0));
    std::vector<u> admin_take(static_cast<size_t>(n), u(0));
    u mint_amount = 0;

    if (total_supply > 0) {
        u ys = (D0 + D1) / n;
        u base_fee = base_fee_of(p);

        for (int i = 0; i < n; ++i) {
            size_t si = static_cast<size_t>(i);
            u ideal_balance = D1 * old_balances[si] / D0;
            u new_balance = new_balances[si];
            u difference = ideal_balance > new_balance ? ideal_balance - new_balance
                                                       : new_balance - ideal_balance;
            u xs = p.rates[si] * (old_balances[si] + new_balance) / PRECISION;
            u dfee = dynamic_fee(xs, ys, base_fee, p.offpeg);
            fees[si] = dfee * difference / FEE_DENOMINATOR;
            admin_take[si] = fees[si] * p.admin_fee / FEE_DENOMINATOR;
            p.adminb[si] += admin_take[si];
            new_balances[si] = csub(new_balances[si], fees[si]);
        }

        std::vector<u> xp = xp_mem(p.rates, new_balances);
        D1 = get_D(xp, amp, n); // reuse D1 for new D (post-fee) — this is what AddLiquidity emits
        mint_amount = total_supply * csub(D1, D0) / D0;
    } else {
        mint_amount = D1;
    }

    // live balances: stored[i] += amounts[i]; admin[i] += admin_take[i]
    for (int i = 0; i < n; ++i) {
        size_t si = static_cast<size_t>(i);
        p.bal[si] = csub(p.bal[si] + amounts[si], admin_take[si]);
    }
    p.total_supply = total_supply + mint_amount;
    // commit metering only once the op can no longer revert
    if (mt) for (int i = 0; i < n; ++i)
        mt->add_fee(i, fees[static_cast<size_t>(i)], admin_take[static_cast<size_t>(i)]);

    return json{{"fees", svec(fees)},
                {"invariant", S(D1)},
                {"supply", S(p.total_supply)},
                {"minted", S(mint_amount)}};
}

// remove_liquidity — proportional; event gives supply_after, burn = supply - supply_after
// cf mode derives the burn from "burn_frac" instead of the historical
// supply_after; apply_remove_burn is the shared body.
inline json apply_remove_burn(Pool& p, const u& burn,
                              const LivePre* live_pre = nullptr) {
    u total_supply = p.total_supply;
    if (burn == 0) throw std::runtime_error("invalid burn amount");
    if (burn > total_supply) throw std::runtime_error("Integer underflow");

    std::vector<u> amounts;
    amounts.reserve(static_cast<size_t>(p.n));
    for (int i = 0; i < p.n; ++i) {
        size_t si = static_cast<size_t>(i);
        u value = p.bal[si] * burn / total_supply; // unsafe_div
        amounts.push_back(value);
        if (live_pre && (*live_pre)[si])
            p.bal[si] = csub(csub(*(*live_pre)[si], value), p.adminb[si]);
        else
            p.bal[si] = csub(p.bal[si], value); // _transfer_out
    }
    p.total_supply = csub(total_supply, burn); // == supply_after

    // _claim_admin_fees defaults True -> _withdraw_admin_fees(): stored[i] -=
    // adminb[i], adminb[i] = 0; live balances unchanged. See header note 2.
    for (auto& a : p.adminb) a = 0;

    return json{{"amounts", svec(amounts)}, {"supply", S(p.total_supply)}};
}

inline json apply_remove(Pool& p, const u& supply_after, const u& /*ts*/,
                         const LivePre* live_pre = nullptr) {
    return apply_remove_burn(p, csub(p.total_supply, supply_after), live_pre);
}

// remove_liquidity_one_coin(_burn_amount, i) via _calc_withdraw_one_coin
inline json apply_remove_one(Pool& p, const u& burn, int i, const u& ts,
                             const LivePre* live_pre = nullptr, Meter* mt = nullptr) {
    if (burn == 0) throw std::runtime_error("do not remove 0 LP tokens");
    if (i < 0 || i >= p.n) throw std::runtime_error("coin index out of range");
    const int n = p.n;
    size_t si = static_cast<size_t>(i);

    u amp = A_now(p, ts);
    std::vector<u> xp = xp_mem(p.rates, p.bal);
    u D0 = get_D(xp, amp, n);

    u total_supply = p.total_supply;
    u D1 = csub(D0, burn * D0 / total_supply);
    u new_y = get_y_D(amp, i, xp, D1, n);

    u base_fee = base_fee_of(p);
    std::vector<u> xp_reduced = xp;
    u ys = (D0 + D1) / (2 * n);

    for (int j = 0; j < n; ++j) {
        size_t sj = static_cast<size_t>(j);
        u xp_j = xp[sj];
        u dx_expected, xavg;
        if (j == i) {
            dx_expected = csub(xp_j * D1 / D0, new_y);
            xavg = (xp_j + new_y) / 2;
        } else {
            dx_expected = csub(xp_j, xp_j * D1 / D0);
            xavg = xp_j;
        }
        u dfee = dynamic_fee(xavg, ys, base_fee, p.offpeg);
        xp_reduced[sj] = csub(xp_j, dfee * dx_expected / FEE_DENOMINATOR);
    }

    u dy = csub(xp_reduced[si], get_y_D(amp, i, xp_reduced, D1, n));
    u dy_0 = csub(xp[si], new_y) * PRECISION / p.rates[si]; // w/o fees
    dy = csub(dy, 1) * PRECISION / p.rates[si];             // withdraw less for rounding

    u fee_amt = csub(dy_0, dy);
    u admin_i = fee_amt * p.admin_fee / FEE_DENOMINATOR;
    p.adminb[si] += admin_i;

    p.total_supply = csub(total_supply, burn);
    // stored[i] -= dy; admin[i] += admin_i -> live loses dy + admin_i
    if (live_pre && (*live_pre)[si])
        p.bal[si] = csub(csub(*(*live_pre)[si], dy), p.adminb[si]);
    else
        p.bal[si] = csub(p.bal[si], dy + admin_i);

    if (mt) mt->add_fee(i, fee_amt, admin_i);

    return json{{"dy", S(dy)}, {"supply", S(p.total_supply)}};
}

// remove_liquidity_imbalance(_amounts) — exactly these coin amounts out
inline json apply_remove_imb(Pool& p, const std::vector<u>& amounts, const u& ts,
                             const LivePre* live_pre = nullptr, Meter* mt = nullptr) {
    const int n = p.n;
    u amp = A_now(p, ts);
    std::vector<u> old_balances = p.bal;
    u D0 = get_D(xp_mem(p.rates, old_balances), amp, n);
    std::vector<u> new_balances = old_balances;

    for (int i = 0; i < n; ++i) {
        size_t si = static_cast<size_t>(i);
        if (amounts[si] != 0)
            new_balances[si] = csub(new_balances[si], amounts[si]); // checked; + _transfer_out
    }

    u D1 = get_D(xp_mem(p.rates, new_balances), amp, n);
    u base_fee = base_fee_of(p);
    u ys = (D0 + D1) / n;

    std::vector<u> fees(static_cast<size_t>(n), u(0));
    std::vector<u> admin_take(static_cast<size_t>(n), u(0));

    for (int i = 0; i < n; ++i) {
        size_t si = static_cast<size_t>(i);
        u ideal_balance = D1 * old_balances[si] / D0;
        u new_balance = new_balances[si];
        u difference = ideal_balance > new_balance ? ideal_balance - new_balance
                                                   : new_balance - ideal_balance;
        u xs = p.rates[si] * (old_balances[si] + new_balance) / PRECISION;
        u dfee = dynamic_fee(xs, ys, base_fee, p.offpeg);
        fees[si] = dfee * difference / FEE_DENOMINATOR;
        admin_take[si] = fees[si] * p.admin_fee / FEE_DENOMINATOR;
        p.adminb[si] += admin_take[si];
        new_balances[si] = csub(new_balances[si], fees[si]);
    }

    D1 = get_D(xp_mem(p.rates, new_balances), amp, n); // reuse D1 for new D

    u total_supply = p.total_supply;
    u burn_amount = csub(D0, D1) * total_supply / D0 + 1;
    if (!(burn_amount > 1)) throw std::runtime_error("zero tokens burned");

    p.total_supply = csub(total_supply, burn_amount);
    // live: transfers out amounts[i]; admin slice of the fee also leaves live
    for (int i = 0; i < n; ++i) {
        size_t si = static_cast<size_t>(i);
        if (live_pre && (*live_pre)[si] && amounts[si] != 0)
            p.bal[si] = csub(csub(*(*live_pre)[si], amounts[si]), p.adminb[si]);
        else
            p.bal[si] = csub(p.bal[si], amounts[si] + admin_take[si]);
    }
    // commit metering only once the op can no longer revert
    if (mt) for (int i = 0; i < n; ++i)
        mt->add_fee(i, fees[static_cast<size_t>(i)], admin_take[static_cast<size_t>(i)]);

    return json{{"fees", svec(fees)},
                {"supply", S(p.total_supply)},
                {"burned", S(burn_amount)}};
}

// ---- probes (engine contract v2) -------------------------------------------
// spot[j-1] = 1e18 * (real coin-j out) / (real coin-0 in) for a zero-size
// FEE-FREE trade == lim dx->0 of get_dy(0, j, dx)/dx, in REAL units.
// Numerical derivative through the engine's own get_y, per the spec:
//   dxp = max(1, xp[0] / 1e6);  dyp = xp[j] - get_y(0, j, xp[0]+dxp, ...)
//   spot = 1e18 * dyp * rates[0] / (dxp * rates[j])
// Any revert inside the probe yields "0" for that leg; a probe never aborts or
// mutates a replay.
inline void spot_prices(const Pool& p, const u& ts, json& spot, json& spot_xp) {
    std::vector<u> xp;
    u amp = 0, D = 0, dxp = 1;
    bool ok = true;
    try {
        xp = xp_mem(p.rates, p.bal);
        amp = A_now(p, ts);
        D = get_D(xp, amp, p.n);
        dxp = xp[0] / 1000000;
        if (dxp == 0) dxp = 1;
    } catch (const std::exception&) {
        ok = false;
    }
    for (int j = 1; j < p.n; ++j) {
        u s = 0, sx = 0;
        if (ok && D > 0) {
            try {
                const u y = get_y(0, j, xp[0] + dxp, xp, amp, D, p.n);
                const size_t sj = static_cast<size_t>(j);
                if (xp[sj] > y) {
                    const u dyp = xp[sj] - y;
                    s = dyp * p.rates[0] * PRECISION / (dxp * p.rates[sj]);
                    sx = dyp * PRECISION / dxp;
                }
            } catch (const std::exception&) {
                s = 0;
                sx = 0;
            }
        }
        spot.push_back(S(s));
        spot_xp.push_back(S(sx));
    }
}

// One probe object:
// {i, bal[], sup, adm[], D, vp, spot[n-1], cfee[], cadm[], cvol[]}.
inline json make_probe(const Pool& p, int idx, const u& ts, const Meter& mt) {
    u D = 0, vp = 0;
    try {
        D = get_D(xp_mem(p.rates, p.bal), A_now(p, ts), p.n);
        if (p.total_supply > 0) vp = D * PRECISION / p.total_supply;
    } catch (const std::exception&) {
        D = 0;
        vp = 0;
    }
    json spot = json::array(), spot_xp = json::array();
    spot_prices(p, ts, spot, spot_xp);
    json pr{{"i", idx},
            {"bal", svec(p.bal)},
            {"sup", S(p.total_supply)},
            {"adm", svec(p.adminb)},
            {"D", S(D)},
            {"vp", S(vp)},
            {"spot", spot},
            {"spot_xp", spot_xp}};
    mt.cum_into(pr);
    return pr;
}

} // namespace sng

// ---- entry point -----------------------------------------------------------

inline nlohmann::json run_stable_ng(const nlohmann::json& job) {
    using sng::u;
    using sng::ju;
    using sng::S;
    using json = nlohmann::json;

    sng::Pool p;
    p.n = job.at("n").get<int>();
    p.rates = sng::jvec(job.at("rates"), p.n);

    const json& prm = job.at("params");
    p.initial_A = ju(prm.at("initial_A")); // already A * A_PRECISION as stored on-chain
    p.future_A = ju(prm.at("future_A"));
    p.initial_A_time = ju(prm.at("initial_A_time"));
    p.future_A_time = ju(prm.at("future_A_time"));
    p.fee = ju(prm.at("fee"));
    p.admin_fee = ju(prm.at("admin_fee"));
    p.offpeg = ju(prm.at("offpeg_fee_multiplier"));

    const json& st = job.at("state");
    p.bal = sng::jvec(st.at("balances"), p.n); // live balances (stored - admin)
    p.total_supply = ju(st.at("total_supply"));
    p.adminb = sng::jvec(st.at("admin_balances"), p.n);

    json out_events = json::array();

    // Rebasing-coin indices (factory asset_type 2) — enables the deduction
    // of measured transfer-in amounts on adds (see below).
    std::vector<int> rebasing;
    if (job.contains("rebasing"))
        for (const auto& r : job.at("rebasing")) rebasing.push_back(r.get<int>());

    // ---- engine contract v2 job flags (all default OFF) --------------------
    const bool cf = job.value("cf", false);
    const bool probe_all = job.value("probe_all", false);
    const bool probe_last = job.value("probe_last", false);
    sng::Meter mt;
    mt.init(p.n);
    json probes = json::array();
    bool any_probe = false;

    const json& jevents = job.at("events");
    const int n_events = static_cast<int>(jevents.size());
    int idx = -1;

    for (const auto& ev : jevents) {
        ++idx;
        const std::string type = ev.at("type").get<std::string>();
        u ts = ev.contains("ts") ? ju(ev.at("ts")) : u(0);

        // Oracle rates read from chain at this event's block: exogenous state,
        // applied before execution and never rolled back (header note 6).
        // Still exogenous truth in cf mode (contract §3.3).
        if (ev.contains("rates") && !ev.at("rates").is_null())
            p.rates = sng::jvec(ev.at("rates"), p.n);

        sng::LivePre lp(static_cast<size_t>(p.n));
        const sng::LivePre* lpp = nullptr;

        if (!cf) {
            // Rebasing pools: stored balances drift with the token's rebase (no
            // pool event); the harness attaches getter reads at (block-1) to the
            // first event of each block. Exogenous truth — applied before the
            // snapshot, never rolled back. Absolute -> ignored in cf mode.
            if (ev.contains("balances_sync"))
                p.bal = sng::jvec(ev.at("balances_sync"), p.n);
            if (ev.contains("admin_sync"))
                p.adminb = sng::jvec(ev.at("admin_sync"), p.n);

            // Pre-transfer balanceOf(self) for outbound rebasing legs (absorbed
            // into stored balances by the deployed _transfer_out).
            if (ev.contains("live_pre")) {
                const auto& arr = ev.at("live_pre");
                for (int i = 0; i < p.n; ++i)
                    if (!arr.at(static_cast<size_t>(i)).is_null())
                        lp[static_cast<size_t>(i)] =
                            ju(arr.at(static_cast<size_t>(i)));
                lpp = &lp;
            }
        } else if (ev.contains("rebase_mul") && !ev.at("rebase_mul").is_null()) {
            // cf replacement for balances_sync: bal[i] = bal[i]*num/den.
            // Exogenous -> applied before the snapshot, never rolled back.
            const auto& rm = ev.at("rebase_mul");
            for (int i = 0; i < p.n && i < static_cast<int>(rm.size()); ++i) {
                if (rm[static_cast<size_t>(i)].is_null()) continue;
                const u num = ju(rm[static_cast<size_t>(i)][0]);
                const u den = ju(rm[static_cast<size_t>(i)][1]);
                if (den == 0) continue;
                p.bal[static_cast<size_t>(i)] = p.bal[static_cast<size_t>(i)] * num / den;
            }
        }

        sng::Pool snapshot = p; // restored on revert
        ++mt.n_events;

        try {
            json outputs;
            bool skipped = false;

            if (type == "exchange") {
                int i = ev.at("sold_id").get<int>();
                int j = ev.at("bought_id").get<int>();
                outputs = sng::apply_exchange(p, i, j, ju(ev.at("dx")), ts, lpp, &mt);
            } else if (type == "add") {
                std::vector<u> amts = sng::jvec(ev.at("amounts"), p.n);
                outputs = sng::apply_add(p, amts, ts, &mt);
                // Rebasing coins: AddLiquidity logs the REQUESTED amounts but
                // the pool stores the MEASURED transfer-in, which the share-
                // floor of a rebasing token can leave 1-2 wei short. The
                // event's own invariant (computed on-chain from the measured
                // amounts) pins them down — deduce, don't guess.
                // cf mode ignores *_expected ground truth (contract §3.4), so
                // the deduction is skipped there and the logged amounts stand.
                if (!cf && !rebasing.empty() && ev.contains("invariant_expected")
                    && outputs.at("invariant").get<std::string>()
                       != ev.at("invariant_expected").get<std::string>()) {
                    bool matched = false;
                    const sng::Meter mt_pre = mt;
                    for (int r : rebasing) {
                        size_t sr = static_cast<size_t>(r);
                        if (amts[sr] < 2) continue;
                        for (int d = 1; d <= 2 && !matched; ++d) {
                            p = snapshot;
                            mt = mt_pre;
                            std::vector<u> a2 = amts;
                            a2[sr] = amts[sr] - d;
                            outputs = sng::apply_add(p, a2, ts, &mt);
                            matched = outputs.at("invariant").get<std::string>()
                                      == ev.at("invariant_expected")
                                            .get<std::string>();
                        }
                        if (matched) break;
                    }
                    if (!matched) {          // deterministic: logged amounts
                        p = snapshot;
                        mt = mt_pre;
                        outputs = sng::apply_add(p, amts, ts, &mt);
                    }
                }
            } else if (type == "remove") {
                if (cf && ev.contains("burn_frac"))
                    outputs = sng::apply_remove_burn(
                        p, p.total_supply * ju(ev.at("burn_frac")) / sng::PRECISION, lpp);
                else
                    outputs = sng::apply_remove(p, ju(ev.at("supply_after")), ts, lpp);
            } else if (type == "remove_one") {
                const u burn = (cf && ev.contains("burn_frac"))
                                   ? p.total_supply * ju(ev.at("burn_frac")) / sng::PRECISION
                                   : ju(ev.at("burn"));
                outputs = sng::apply_remove_one(p, burn,
                                                ev.at("i").get<int>(), ts, lpp, &mt);
            } else if (type == "remove_imb") {
                outputs = sng::apply_remove_imb(p, sng::jvec(ev.at("amounts"), p.n),
                                                ts, lpp, &mt);
            } else if (type == "ramp_a") {
                // RampA emits initial_A = _A() at that block and
                // future_A = _future_A * A_PRECISION — both ALREADY scaled and
                // bit-identical to the storage writes ramp_A performs, so we
                // store them directly (header note 3).
                p.initial_A = ju(ev.at("initial_A"));
                p.future_A = ju(ev.at("future_A"));
                p.initial_A_time = ju(ev.at("initial_time"));
                p.future_A_time = ju(ev.at("future_time"));
                outputs = json::object();
            } else if (type == "stop_ramp") {
                u A = ju(ev.at("A"));
                p.initial_A = A;
                p.future_A = A;
                p.initial_A_time = ts;
                p.future_A_time = ts;
                outputs = json::object();
            } else if (type == "new_fee") {
                p.fee = ju(ev.at("fee"));
                p.offpeg = ju(ev.at("offpeg_fee_multiplier"));
                outputs = json::object();
            } else {
                out_events.push_back(json{{"type", type}, {"skipped", true}});
                skipped = true;
            }

            if (!skipped)
                out_events.push_back(json{{"type", type}, {"outputs", outputs}});
        } catch (const std::exception& e) {
            p = std::move(snapshot); // restore pre-event state
            ++mt.n_reverts;
            out_events.push_back(json{{"type", type}, {"revert", std::string(e.what())}});
        }

        if (probe_all || ev.value("probe", false) ||
            (probe_last && idx == n_events - 1)) {
            probes.push_back(sng::make_probe(p, idx, ts, mt));
            any_probe = true;
        }
    }

    json result;
    result["events"] = out_events;
    result["final"] = json{{"balances", sng::svec(p.bal)},
                           {"total_supply", S(p.total_supply)},
                           {"admin_balances", sng::svec(p.adminb)}};
    if (any_probe) result["probes"] = std::move(probes);
    result["meter"] = mt.to_json();
    return result;
}
