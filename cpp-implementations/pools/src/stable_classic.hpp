#pragma once
// stable_classic.hpp — wei-exact replay state machines for CLASSIC Curve stableswap pools.
//
// Kinds:
//   "stable_v1"  3pool vintage (StableSwap3Pool.vy, vyper 0.2.4). A stored RAW
//                (A_PRECISION = 1). fee/admin_fee mutable via commit/apply.
//   "stable_v2"  Factory plain pools (Plain2Basic.vy / Plain3Basic.vy, vyper 0.3.1).
//                A stored as A*100 (A_PRECISION = 100), admin fee a constant 50%
//                in the implementation (we still read it from job params).
//
// Sources followed line-by-line (fetched 2026-08-28, master):
//   https://raw.githubusercontent.com/curvefi/curve-contract/master/contracts/pools/3pool/StableSwap3Pool.vy
//   https://raw.githubusercontent.com/curvefi/curve-factory/master/contracts/implementations/plain-3/Plain3Basic.vy
//   https://raw.githubusercontent.com/curvefi/curve-factory/master/contracts/implementations/plain-2/Plain2Basic.vy
// Kernels cross-checked against global-sim-ui/engine-cpp/src/engine.cpp
// (get_D_v1 / get_D_prec(dp_outside) / get_y(a_prec)) and
// CurveOrderFlow/src/simulations/math/StableSwap.ts.
//
// VERIFIED semantic answers to the open questions:
//   * AddLiquidity event `invariant` is D1 — the PRE-FEE invariant — in BOTH
//     vintages: `log AddLiquidity(msg.sender, amounts, fees, D1, total_supply)`.
//   * RampA units: v1 emits raw A (`log RampA(_initial_A, _future_A, ...)`, both
//     from raw storage); factory v2 emits STORED values too — `_initial_A = self._A()`
//     (already *100) and `_future_A_p = _future_A * A_PRECISION`. So in BOTH
//     vintages RampA/StopRampA event values are exactly the contract's storage
//     units and are applied to internal state verbatim.
//   * Plain2Basic.get_D computes D_P differently from Plain3Basic:
//     `D_P = D * D / xp[0] * D / xp[1] / (N_COINS)**2` (left-to-right), which is
//     the "dp outside" rounding; plain-3 divides by (x*N) inside the loop.
//     stable_v2 therefore switches the D_P branch on n == 2.
//   * v1 get_D/get_y/get_y_D fall out of the 255-iteration loop returning the
//     last iterate (break-style loop, no raise); v2 `raise`s (-> revert here).
//   * exchange applies the fee in xp units, then converts: dy = (dy_xp - fee_xp)
//     * PRECISION / rates[j]; admin part dy_admin = fee_xp * admin_fee / 1e10,
//     converted to token units, and REMOVED from balances[j] alongside dy.
//     Event tokens_bought = dy net of the total fee. (This differs in rounding
//     from the get_dy() view of v1, which converts before charging the fee —
//     we replay exchange(), not the view.)
//   * remove_liquidity_one_coin: v1 divides by PRECISION_MUL (derived here as
//     rates[i] / 1e18 — exact for classic rates 10^(36-decimals)); v2 uses
//     * PRECISION / rates[i]. Same value for such rates, kept vintage-faithful.
//   * remove_liquidity_imbalance burn: v1 = (D0-D2)*ts/D0, assert != 0, then
//     += 1; v2 = (D0-D2)*ts/D0 + 1, assert > 1. Same revert set, both kept.
//
// Remaining uncertainties (documented, not blocking):
//   * v1 FEE_INDEX (USDT) measures dx/in_amount via balanceOf deltas to absorb
//     transfer fees. USDT's fee has always been 0, so we use the event's dx
//     verbatim; a nonzero token transfer fee would break wei-parity.
//   * uint256 overflow is not modeled (cpp_int is unbounded); on-chain events
//     that DID land cannot have overflowed, so this cannot affect parity.
//   * stable_v1 with n == 2 uses the loop-form D_P (3pool shape). The old
//     curve-contract 2-coin templates (e.g. hbtc) share that shape, but only
//     the 3-coin template was verified line-by-line.
//   * remove_one with i == -1 and several coins matching dy_expected exactly
//     (possible only in a perfectly symmetric pool): the lowest index wins.
//
// Job/result schema: see run_stable_classic() at the bottom.
//
// ---- LIDO FAMILY (added 2026-08-28) ----------------------------------------
// Three stETH-family pools use LIVE-BALANCE accounting: the contract's math
// balances are balanceOf(self) - admin_balances[i] (ETH: self.balance - ...),
// verified against the DEPLOYED sources (Blockscout verified source for the
// two factory implementations; GitHub steth source is byte-identical in code
// to the deployed 0xDC24... per Blockscout's re-verification diff):
//
//   kind "stable_lido"      0xDC24316b9ae028F1497c275EB9192a3Ea0f67022
//                           StableSwapSTETH.vy (vyper 0.2.8). Differences vs
//                           stable_v2: get_D uses the LOOP form with
//                           D_P = D_P * D / (_x * N_COINS + 1)   («+1 is to
//                           prevent /0»), and remove_liquidity_imbalance burns
//                           (D0-D2)*ts/D0 with assert != 0 and NO +1.
//   kind "stable_lido_ng"   0x21E27... via EIP-1167 -> impl
//                           0x847ee1227A9900B73aEeb3a47fAc92c52FD54ed9,
//                           "v6.0.1" (vyper 0.3.7, «Uses native Ether as
//                           coins[0] and can rebase ERC20», EMA oracle). Math
//                           is IDENTICAL to stable_v2 (dp-outside get_D,
//                           +1 burn) — only the live-balance bookkeeping and
//                           the stETH wei-corner (below) differ.
//   kind "stable_lido_bal"  0x4d9f9d15... via EIP-1167 -> impl
//                           0x24d937143D3F5cF04c72bA112735151A8CAE2262,
//                           curve-factory Plain2Balances.vy @ f389123bea5b
//                           (vyper 0.2.15; deployed source == that commit
//                           exactly). Math = stable_v2, BUT exchange() and
//                           add_liquidity() MEASURE the deposited amount via
//                           balanceOf deltas, so the stETH 1-2 wei transfer
//                           corner enters the MATH (x = xp[i] + recv(dx)),
//                           while the event still logs the requested dx.
//
// Routing: replay.cpp currently forwards only kinds "stable_v1"/"stable_v2"
// to run_stable_classic, so the OPERATIVE routing is: keep kind "stable_v2"
// and add a job field "flavor": "steth" | "ng" | "balances". The kind strings
// above are also accepted by run_stable_classic (equivalent to stable_v2 +
// flavor) for when the harness learns to dispatch them.
// Optional job fields for the lido family:
//   "rebase_coin"  int   index of the rebasing coin (default: 1 for
//                        steth/ng — ETH is coin 0 —, 0 for balances/frxETH)
//   "wei_in"/"wei_out"   stETH transfer-rounding model (defaults 1/1, see
//                        below); set 0 to disable.
// Optional per-event fields (any stable_classic kind):
//   "balances": [dec..]  the contract's admin-net math balances JUST BEFORE
//                        this event; hard-syncs the model before applying it.
//                        Hook for per-block balance syncing, which these
//                        three pools NEED to fully validate (see below).
//   "total_supply_pre"   same, for the LP total supply.
//   "dx_recv"            exchange only: the TRUE balanceOf delta of the input
//                        transfer (overrides the wei-corner model; feeds the
//                        math for "balances", the bookkeeping for all).
// Measured ceiling with per-block sync injected (2026-08-28 windows):
// steth 211/211, ng 383/383, bal 7/7 events exact AND final state exact —
// the engine math is bit-exact; only balance knowledge is missing. Sync
// recipe: eth_call balances(i) at (block-1) attached to the first event of
// each block. Two intra-block cases needed more: (a) a swap in the SAME block
// as (after) the Lido rebase tx — balance = floor(sharesOf(pool, B-1) *
// totalPooledEther/totalShares at B) - admin_balances[i]@B-1 (3 such events
// in the steth window, all daily-rebase backruns at ~12:21 UTC); (b) the
// measured dx of Plain2Balances — dx_recv from the same share math.
//
// stETH wei-corner model: stETH balances are share-derived, so transferring x
// moves the receiver's/sender's balanceOf by recv(x) <= x — almost always
// x-1, sometimes x-2 («1-2 wei corner case», share rate ~1.2). The engine
// models recv(x) = x - wei_in (inbound) / x - wei_out (outbound) for nonzero
// stETH legs; for "stable_lido_bal" the inbound value also feeds the math
// (measured dx). The exact value depends on Lido's share rate at that block
// and is NOT recoverable from pool events.
//
// WHY THESE POOLS CANNOT FULLY VALIDATE FROM POOL EVENTS ALONE: the pools'
// accounting variable is stETH's live balanceOf, which moves WITHOUT any pool
// event at every daily Lido oracle rebase (~12:00-12:45 UTC; measured drift
// onset in all three replay windows matches to the minute) and by the 1-2 wei
// transfer corner. Everything else is bit-exact; supplying per-event
// "balances" (from eth_call balances(i) at the event's block - 1, applied to
// the first event of each block) closes the gap.
//
// ---- ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md, added 2026-08-29) ----
// Purely additive; with none of the new job fields set the result is
// byte-identical to before EXCEPT the always-emitted result["meter"].
//
//   job:    "probe_all" | "probe_last" | "cf"      (all default false)
//   event:  "probe"                                (default false)
//   event:  "burn_frac"   cf only — burn = total_supply * frac / 1e18 (floor)
//                         for "remove" and "remove_one"
//   event:  "rebase_mul"  cf only — [[num,den]|null, ...] per coin, applied
//                         BEFORE the event, never rolled back
//
//   result["meter"]   ALWAYS: {fee[], admin[], vol[], n_events, n_reverts}
//                     in COIN units. fee = gross (LP + admin) fee; admin =
//                     the slice actually taken out of the pool balance
//                     (this family holds no admin_balances array — the admin
//                     slice simply leaves balances[]). vol[i] = sum of the
//                     exchange input CREDITED to the pool (== the logged dx
//                     except on stETH legs, where the pool's balanceOf only
//                     moves by recv(dx)).
//   result["probes"]  only when a probe was requested:
//                     {i, bal[], sup, D, vp, spot[n-1],
//                      cfee[], cadm[], cvol[]}. No "adm" — this
//                     family has no admin_balances array.
//
//   cfee/cadm/cvol are the meter's fee/admin/vol accumulators AS OF that
//   event (same units, same conventions); the last probe's values equal
//   result["meter"] exactly. Metering commits only once an op can no longer
//   revert, so a reverted event contributes nothing and its probe shows the
//   pre-event totals. No "cfee_lp"/"cadm_lp" — this family has neither an
//   LP-denominated fee nor LP minted for the DAO.
//
//   spot[j-1] = 1e18 * (coin-j out) / (coin-0 in) for a zero-size FEE-FREE
//   trade, i.e. the limit of get_dy(0, j, dx)/dx as dx -> 0, in RAW TOKEN
//   units (xp converted back to token units with `rates`, exactly the spec's
//   stableswap recipe). Numerical derivative through get_y with
//   dxp = max(1, xp[0]/1e6) in xp units.
//   CAVEAT: 1e18-scaling a raw-unit ratio loses precision when the two coins
//   have different decimals — a DAI(18)/USDC(6) leg has a true ratio of ~1e-12
//   so `spot` lands near 1e6 and carries only ~6 significant digits (1e-6
//   relative). `spot_xp[j-1]` is therefore ALSO emitted: the same price with
//   `rates` folded in (1e18 * dxp_j / dxp_0), which always lands near 1e18 and
//   keeps full precision. Exact conversion, no information lost either way:
//       spot = spot_xp * rates[0] / rates[j]
//   For classic pools rates are pure decimal multipliers 10^(36-decimals), so
//   spot_xp is exactly the whole-token ("1 DAI per 1 USDC") price.
//
// cf-mode notes for this family:
//   * the absolute syncs "balances", "total_supply_pre" and "dx_recv" encode
//     the REAL history's state and are IGNORED in cf mode ("rebase_mul" is the
//     multiplicative replacement for the first two);
//   * the wei_in/wei_out stETH transfer-rounding MODEL is kept in cf mode —
//     it is a model of the token, not a snapshot of history;
//   * a "remove_one" event with i == -1 still consults "dy_expected" in cf
//     mode, but ONLY to infer which coin was withdrawn (the classic
//     RemoveLiquidityOne log carries no coin index and there is no other
//     source for it). It never gates or corrects the computed dy.

#include <boost/multiprecision/cpp_int.hpp>
#include <nlohmann/json.hpp>

#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace stable_classic_detail {

using u = boost::multiprecision::cpp_int;
using json = nlohmann::json;

inline const u& PRECISION() { static const u v("1000000000000000000"); return v; }  // 1e18
inline const u& FEE_DENOM() { static const u v("10000000000"); return v; }          // 1e10

// Vyper-revert stand-in: any assert / underflow / div-by-zero / raise.
struct PoolRevert : std::runtime_error {
    explicit PoolRevert(const std::string& m) : std::runtime_error(m) {}
};

// ---- vyper uint256 primitives ---------------------------------------------

// floor division; vyper reverts on division by zero
inline u fdiv(const u& a, const u& b) {
    if (b == 0) throw PoolRevert("division by zero");
    return a / b;  // both operands non-negative -> C++ '/' == floor division
}

// checked subtraction; vyper reverts on uint256 underflow
inline u fsub(const u& a, const u& b) {
    if (b > a) throw PoolRevert("uint256 underflow");
    return a - b;
}

inline u parse_u(const json& j) {
    if (j.is_string()) {
        const std::string s = j.get<std::string>();
        if (s.empty()) throw PoolRevert("empty uint string");
        return u(s);
    }
    if (j.is_number_unsigned()) return u(j.get<unsigned long long>());
    if (j.is_number_integer()) {
        long long v = j.get<long long>();
        if (v < 0) throw PoolRevert("negative uint");
        return u(v);
    }
    throw PoolRevert("bad uint value");
}

inline std::string dec(const u& x) { return x.str(); }

// ---- pool state ------------------------------------------------------------

// Lido-family sub-variant (see header). none = plain stable_v1/v2.
enum class Flavor { none, steth, ng, bal };

struct Pool {
    int n = 0;
    bool v2 = false;                 // false: A_PRECISION=1 (3pool), true: =100 (factory plain)
    Flavor flavor = Flavor::none;    // live-balance stETH-family pools
    int rebase_coin = -1;            // index of the rebasing (stETH) coin
    u wei_in = 1, wei_out = 1;       // stETH transfer-rounding model (see header)
    std::vector<u> rates;            // 10^(36 - decimals) multipliers
    std::vector<u> balances;         // the contract's math balances (admin-net)
    u total_supply;
    u fee, admin_fee;                // 1e10-scaled
    u initial_A, future_A;           // AS STORED on-chain (raw for v1, A*100 for v2)
    u initial_A_time, future_A_time;

    int a_prec() const { return v2 ? 100 : 1; }
    // amount by which the pool's stETH balanceOf actually moves when `x` is
    // transferred in/out (share-rounding model); exact for non-stETH coins
    u recv_in(int coin, const u& x) const {
        if (coin == rebase_coin && x > 0) return x > wei_in ? x - wei_in : u(0);
        return x;
    }
    u recv_out(int coin, const u& x) const {
        if (coin == rebase_coin && x > 0) return x > wei_out ? x - wei_out : u(0);
        return x;
    }
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
        json jf = json::array(), ja = json::array(), jv = json::array();
        for (const auto& x : fee) jf.push_back(x.str());
        for (const auto& x : admin) ja.push_back(x.str());
        for (const auto& x : vol) jv.push_back(x.str());
        return json{{"fee", jf}, {"admin", ja}, {"vol", jv},
                    {"n_events", n_events}, {"n_reverts", n_reverts}};
    }

    // Cumulative-as-of-now snapshot for a probe: the same accumulators
    // to_json() reports at the end of the run, read mid-run. The last probe's
    // values therefore equal result["meter"] exactly. No "cfee_lp"/"cadm_lp":
    // this family has no LP-denominated fee and mints no LP for the DAO.
    void cum_into(json& pr) const {
        json jf = json::array(), ja = json::array(), jv = json::array();
        for (const auto& x : fee) jf.push_back(x.str());
        for (const auto& x : admin) ja.push_back(x.str());
        for (const auto& x : vol) jv.push_back(x.str());
        pr["cfee"] = std::move(jf);
        pr["cadm"] = std::move(ja);
        pr["cvol"] = std::move(jv);
    }
};

// ---- _A() ramp -------------------------------------------------------------

inline u calc_A(const Pool& p, const u& ts) {
    const u& t1 = p.future_A_time;
    const u& A1 = p.future_A;
    if (ts < t1) {
        const u& A0 = p.initial_A;
        const u& t0 = p.initial_A_time;
        if (A1 > A0) return A0 + fdiv((A1 - A0) * fsub(ts, t0), fsub(t1, t0));
        return fsub(A0, fdiv((A0 - A1) * fsub(ts, t0), fsub(t1, t0)));
    }
    return A1;  // t1 == 0 or ts >= t1
}

// ---- xp --------------------------------------------------------------------

inline std::vector<u> xp_mem(const Pool& p, const std::vector<u>& bal) {
    std::vector<u> r(p.n);
    for (int i = 0; i < p.n; ++i) r[i] = fdiv(p.rates[i] * bal[i], PRECISION());
    return r;
}

// ---- get_D -----------------------------------------------------------------
// v1 (3pool): D = (Ann*S + D_P*n)*D / ((Ann-1)*D + (n+1)*D_P); falls out of the
//   255-iteration loop returning the last iterate (no raise).
// v2 (factory): A_PRECISION=100 update; plain-2 computes D_P "outside"
//   (D*D/xp0*D/xp1/n^2), plain-3 divides by (x*n) inside the loop; raises on
//   non-convergence.

inline u get_D(const std::vector<u>& xp, const u& amp, int n, bool v2,
               bool steth_plus1 = false) {
    u S = 0;
    for (const auto& x : xp) S += x;
    if (S == 0) return 0;
    u D = S;
    const u Ann = amp * n;
    for (int it = 0; it < 255; ++it) {
        u D_P = D;
        if (steth_plus1) {
            // StableSwapSTETH (vyper 0.2.8): loop form with
            // D_P = D_P * D / (_x * N_COINS + 1)   («+1 is to prevent /0»)
            for (const auto& x : xp) D_P = fdiv(D_P * D, x * n + 1);
        } else if (v2 && n == 2) {
            // Plain2Basic: D_P = D * D / xp[0] * D / xp[1] / (N_COINS)**2
            D_P = fdiv(fdiv(fdiv(D * D, xp[0]) * D, xp[1]), u(n) * n);
        } else {
            for (const auto& x : xp) D_P = fdiv(D_P * D, x * n);
        }
        const u Dprev = D;
        if (v2) {
            D = fdiv((fdiv(Ann * S, 100) + D_P * n) * D,
                     fdiv(fsub(Ann, 100) * D, 100) + (n + 1) * D_P);
        } else {
            D = fdiv((Ann * S + D_P * n) * D,
                     fsub(Ann, 1) * D + (n + 1) * D_P);
        }
        if (D > Dprev ? D - Dprev <= 1 : Dprev - D <= 1) return D;
    }
    if (v2) throw PoolRevert("get_D did not converge");
    return D;  // v1: break-style loop returns last iterate
}

inline u get_D_p(const Pool& p, const std::vector<u>& xp, const u& amp) {
    return get_D(xp, amp, p.n, p.v2, p.flavor == Flavor::steth);
}

inline u get_D_mem(const Pool& p, const std::vector<u>& bal, const u& amp) {
    return get_D_p(p, xp_mem(p, bal), amp);
}

// ---- get_y / get_y_D -------------------------------------------------------

inline u newton_y(const u& b, const u& c, const u& D, bool v2) {
    u y = D;
    for (int it = 0; it < 255; ++it) {
        const u y_prev = y;
        y = fdiv(y * y + c, fsub(2 * y + b, D));
        if (y > y_prev ? y - y_prev <= 1 : y_prev - y <= 1) return y;
    }
    if (v2) throw PoolRevert("get_y did not converge");
    return y;  // v1: break-style loop returns last iterate
}

inline u get_y(const Pool& p, int i, int j, const u& x, const std::vector<u>& xp,
               const u& amp, const u& D) {
    if (i == j) throw PoolRevert("same coin");
    if (j < 0 || j >= p.n) throw PoolRevert("j out of range");
    if (i < 0 || i >= p.n) throw PoolRevert("i out of range");
    const int n = p.n;
    const u Ann = amp * n;
    u c = D, S_ = 0;
    for (int k = 0; k < n; ++k) {
        u xk;
        if (k == i) xk = x;
        else if (k != j) xk = xp[k];
        else continue;
        S_ += xk;
        c = fdiv(c * D, xk * n);
    }
    c = fdiv(c * D * p.a_prec(), Ann * n);
    const u b = S_ + fdiv(D * p.a_prec(), Ann);
    return newton_y(b, c, D, p.v2);
}

inline u get_y_D(const Pool& p, const u& amp, int i, const std::vector<u>& xp, const u& D) {
    if (i < 0 || i >= p.n) throw PoolRevert("i out of range");
    const int n = p.n;
    const u Ann = amp * n;
    u c = D, S_ = 0;
    for (int k = 0; k < n; ++k) {
        if (k == i) continue;
        S_ += xp[k];
        c = fdiv(c * D, xp[k] * n);
    }
    c = fdiv(c * D * p.a_prec(), Ann * n);
    const u b = S_ + fdiv(D * p.a_prec(), Ann);
    return newton_y(b, c, D, p.v2);
}

// ---- exchange --------------------------------------------------------------
// Identical structure in both vintages (v1's FEE_INDEX balanceOf-delta only
// matters for fee-on-transfer tokens; USDT's fee is 0, dx used verbatim).

inline json do_exchange(Pool& p, int i, int j, const u& dx, const u& ts,
                        const u* dx_recv = nullptr, Meter* mt = nullptr) {
    const std::vector<u> old_balances = p.balances;
    const std::vector<u> xp = xp_mem(p, old_balances);

    if (i < 0 || i >= p.n) throw PoolRevert("i out of range");
    if (j < 0 || j >= p.n) throw PoolRevert("j out of range");
    // Plain2Balances MEASURES dx via balanceOf deltas, so the stETH transfer
    // rounding enters the math; the other vintages trust the logged dx.
    // Optional per-event "dx_recv" (the true balanceOf delta) overrides the
    // wei-corner model.
    const u dx_credit = dx_recv ? *dx_recv : p.recv_in(i, dx);
    const u dx_math = (p.flavor == Flavor::bal) ? dx_credit : dx;
    const u x = xp[i] + fdiv(dx_math * p.rates[i], PRECISION());
    const u amp = calc_A(p, ts);
    const u D = get_D_p(p, xp, amp);
    const u y = get_y(p, i, j, x, xp, amp, D);

    u dy = fsub(fsub(xp[j], y), 1);          // -1 just in case of rounding errors
    const u dy_fee = fdiv(dy * p.fee, FEE_DENOM());

    // Convert all to real units
    dy = fdiv(fsub(dy, dy_fee) * PRECISION(), p.rates[j]);

    u dy_admin_fee = fdiv(dy_fee * p.admin_fee, FEE_DENOM());
    dy_admin_fee = fdiv(dy_admin_fee * PRECISION(), p.rates[j]);

    // Live-balance pools: the pool's balanceOf moves by recv(x), not x, on
    // stETH legs (for Flavor::none recv_* are identity).
    p.balances[i] = old_balances[i] + dx_credit;
    // When rounding errors happen, we undercharge admin fee in favor of LP
    p.balances[j] = fsub(old_balances[j], p.recv_out(j, dy) + dy_admin_fee);

    if (mt) {
        // gross fee lands on the OUTPUT coin; dy_fee is in xp units, convert
        // to coin units the same way dy itself is converted
        mt->add_fee(j, fdiv(dy_fee * PRECISION(), p.rates[j]), dy_admin_fee);
        mt->add_vol(i, dx_credit);
    }

    return json{{"dy", dec(dy)}};
}

// ---- add_liquidity ---------------------------------------------------------
// AddLiquidity event emits D1 (the PRE-FEE invariant) in both vintages.

inline json do_add(Pool& p, const std::vector<u>& amounts, const u& ts,
                   Meter* mt = nullptr) {
    const int n = p.n;
    const u amp = calc_A(p, ts);
    const std::vector<u> old_balances = p.balances;
    const u token_supply = p.total_supply;

    u D0 = 0;
    if (p.v2) {
        D0 = get_D_mem(p, old_balances, amp);  // factory always computes (0 when S == 0)
    } else if (token_supply > 0) {
        D0 = get_D_mem(p, old_balances, amp);  // 3pool skips when supply == 0
    }

    // Plain2Balances MEASURES deposits via balanceOf deltas (stETH rounding
    // enters the math); steth/v6 trust the logged amounts but the pool's
    // balanceOf still only grows by recv(amount) on the stETH leg.
    std::vector<u> credited(n);
    std::vector<u> new_balances = old_balances;
    for (int i = 0; i < n; ++i) {
        if (token_supply == 0 && amounts[i] == 0)
            throw PoolRevert("initial deposit requires all coins");
        credited[i] = p.recv_in(i, amounts[i]);
        const u& math_amt = (p.flavor == Flavor::bal) ? credited[i] : amounts[i];
        new_balances[i] = old_balances[i] + math_amt;
    }

    const u D1 = get_D_mem(p, new_balances, amp);
    if (!(D1 > D0)) throw PoolRevert("D1 <= D0");

    std::vector<u> fees(n, u(0));
    std::vector<u> admin_take(n, u(0));   // metering only (see commit below)
    u mint_amount = 0;
    if (token_supply > 0) {
        // Only account for fees if we are not the first to deposit
        const u base_fee = fdiv(p.fee * n, u(4) * (n - 1));
        for (int i = 0; i < n; ++i) {
            const u ideal_balance = fdiv(D1 * old_balances[i], D0);
            const u difference = ideal_balance > new_balances[i]
                                     ? ideal_balance - new_balances[i]
                                     : new_balances[i] - ideal_balance;
            fees[i] = fdiv(base_fee * difference, FEE_DENOM());
            admin_take[i] = fdiv(fees[i] * p.admin_fee, FEE_DENOM());
            p.balances[i] = fsub(old_balances[i] + credited[i], admin_take[i]);
            new_balances[i] = fsub(new_balances[i], fees[i]);
        }
        const u D2 = get_D_mem(p, new_balances, amp);
        mint_amount = fdiv(token_supply * fsub(D2, D0), D0);
    } else {
        for (int i = 0; i < n; ++i) p.balances[i] = old_balances[i] + credited[i];
        mint_amount = D1;  // Take the dust if there was any
    }

    p.total_supply = token_supply + mint_amount;
    // commit metering only once the op can no longer revert
    if (mt) for (int i = 0; i < n; ++i) mt->add_fee(i, fees[i], admin_take[i]);

    json jfees = json::array();
    for (const auto& f : fees) jfees.push_back(dec(f));
    return json{{"fees", jfees},
                {"invariant", dec(D1)},
                {"supply", dec(p.total_supply)},
                {"minted", dec(mint_amount)}};
}

// ---- remove_liquidity (proportional) ---------------------------------------

// cf mode derives the burn from "burn_frac" instead of the historical
// supply_after; do_remove_burn is the shared body.
inline json do_remove_burn(Pool& p, const u& burn) {
    const u total_supply = p.total_supply;

    json jamounts = json::array();
    for (int i = 0; i < p.n; ++i) {
        const u value = fdiv(p.balances[i] * burn, total_supply);
        p.balances[i] = fsub(p.balances[i], p.recv_out(i, value));
        jamounts.push_back(dec(value));
    }
    p.total_supply = fsub(total_supply, burn);

    return json{{"amounts", jamounts}, {"supply", dec(p.total_supply)}};
}

inline json do_remove(Pool& p, const u& supply_after) {
    return do_remove_burn(p, fsub(p.total_supply, supply_after));
}

// ---- remove_liquidity_imbalance --------------------------------------------

inline json do_remove_imbalance(Pool& p, const std::vector<u>& amounts, const u& ts,
                                Meter* mt = nullptr) {
    const int n = p.n;
    const u token_supply = p.total_supply;
    if (!p.v2 && token_supply == 0) throw PoolRevert("zero total supply");

    const u amp = calc_A(p, ts);
    const std::vector<u> old_balances = p.balances;
    const u D0 = get_D_mem(p, old_balances, amp);

    std::vector<u> new_balances = old_balances;
    for (int i = 0; i < n; ++i) new_balances[i] = fsub(new_balances[i], amounts[i]);
    const u D1 = get_D_mem(p, new_balances, amp);

    std::vector<u> fees(n, u(0));
    std::vector<u> admin_take(n, u(0));   // metering only (see commit below)
    const u base_fee = fdiv(p.fee * n, u(4) * (n - 1));
    for (int i = 0; i < n; ++i) {
        const u ideal_balance = fdiv(D1 * old_balances[i], D0);   // reverts if D0 == 0
        const u difference = ideal_balance > new_balances[i]
                                 ? ideal_balance - new_balances[i]
                                 : new_balances[i] - ideal_balance;
        fees[i] = fdiv(base_fee * difference, FEE_DENOM());
        admin_take[i] = fdiv(fees[i] * p.admin_fee, FEE_DENOM());
        // balanceOf drops by recv(amount) on the stETH leg, not amount
        p.balances[i] = fsub(fsub(old_balances[i], p.recv_out(i, amounts[i])),
                             admin_take[i]);
        new_balances[i] = fsub(new_balances[i], fees[i]);
    }
    const u D2 = get_D_mem(p, new_balances, amp);

    u burn_amount = fdiv(fsub(D0, D2) * token_supply, D0);
    if (p.flavor == Flavor::steth) {
        // StableSwapSTETH: assert token_amount != 0, NO +1
        if (burn_amount == 0) throw PoolRevert("zero tokens burned");
    } else if (!p.v2) {
        if (burn_amount == 0) throw PoolRevert("zero tokens burned");
        burn_amount += 1;  // make rounding unfavorable for the "attacker"
    } else {
        burn_amount += 1;
        if (!(burn_amount > 1)) throw PoolRevert("zero tokens burned");
    }
    p.total_supply = fsub(token_supply, burn_amount);
    // commit metering only once the op can no longer revert
    if (mt) for (int i = 0; i < n; ++i) mt->add_fee(i, fees[i], admin_take[i]);

    json jfees = json::array();
    for (const auto& f : fees) jfees.push_back(dec(f));
    return json{{"fees", jfees},
                {"supply", dec(p.total_supply)},
                {"burned", dec(burn_amount)}};
}

// ---- remove_liquidity_one_coin ---------------------------------------------
// Returns (dy, dy_fee) in token units; pure — does not mutate the pool.

inline std::pair<u, u> calc_withdraw_one_coin(const Pool& p, const u& burn, int i, const u& ts) {
    if (i < 0 || i >= p.n) throw PoolRevert("i out of range");
    const int n = p.n;
    const u amp = calc_A(p, ts);
    const std::vector<u> xp = xp_mem(p, p.balances);
    const u D0 = get_D_p(p, xp, amp);

    const u D1 = fsub(D0, fdiv(burn * D0, p.total_supply));
    const u new_y = get_y_D(p, amp, i, xp, D1);

    const u base_fee = fdiv(p.fee * n, u(4) * (n - 1));
    std::vector<u> xp_reduced = xp;
    for (int j = 0; j < n; ++j) {
        u dx_expected;
        if (j == i) dx_expected = fsub(fdiv(xp[j] * D1, D0), new_y);
        else        dx_expected = fsub(xp[j], fdiv(xp[j] * D1, D0));
        xp_reduced[j] = fsub(xp[j], fdiv(base_fee * dx_expected, FEE_DENOM()));
    }

    u dy = fsub(xp_reduced[i], get_y_D(p, amp, i, xp_reduced, D1));
    u dy_0;
    if (p.v2) {
        dy_0 = fdiv(fsub(xp[i], new_y) * PRECISION(), p.rates[i]);
        dy = fdiv(fsub(dy, 1) * PRECISION(), p.rates[i]);  // withdraw less: rounding errors
    } else {
        // 3pool divides by PRECISION_MUL; classic rates are 10^(36-decimals),
        // so precisions[i] = rates[i] / 1e18 exactly.
        const u prec_i = fdiv(p.rates[i], PRECISION());
        dy_0 = fdiv(fsub(xp[i], new_y), prec_i);
        dy = fdiv(fsub(dy, 1), prec_i);
    }
    return {dy, fsub(dy_0, dy)};
}

inline u apply_remove_one(Pool& p, const u& burn, int i, const u& ts,
                          Meter* mt = nullptr) {
    const auto [dy, dy_fee] = calc_withdraw_one_coin(p, burn, i, ts);
    const u adm_i = fdiv(dy_fee * p.admin_fee, FEE_DENOM());
    p.balances[i] = fsub(p.balances[i], p.recv_out(i, dy) + adm_i);
    p.total_supply = fsub(p.total_supply, burn);
    if (mt) mt->add_fee(i, dy_fee, adm_i);
    return dy;
}

inline json do_remove_one(Pool& p, const u& burn, int i, const json& ev, const u& ts,
                          Meter* mt = nullptr) {
    if (i >= 0) {
        const u dy = apply_remove_one(p, burn, i, ts, mt);
        return json{{"dy", dec(dy)}, {"i", i}};
    }

    // Classic RemoveLiquidityOne events carry no coin index: infer it by trying
    // every coin and matching the computed dy against the event's dy_expected.
    if (!ev.contains("dy_expected")) throw PoolRevert("remove_one: i=-1 without dy_expected");
    const u dy_expected = parse_u(ev.at("dy_expected"));

    int best_i = -1;
    u best_dy = 0, best_diff = 0;
    bool exact = false;
    for (int cand = 0; cand < p.n; ++cand) {
        u dy;
        try {
            dy = calc_withdraw_one_coin(p, burn, cand, ts).first;  // pure: no state change
        } catch (const PoolRevert&) {
            continue;
        }
        const u diff = dy > dy_expected ? dy - dy_expected : dy_expected - dy;
        if (diff == 0) { best_i = cand; best_dy = dy; exact = true; break; }  // lowest i wins ties
        if (best_i < 0 || diff < best_diff) { best_i = cand; best_dy = dy; best_diff = diff; }
    }
    if (best_i < 0) throw PoolRevert("remove_one: all coin candidates revert");

    const u dy = apply_remove_one(p, burn, best_i, ts, mt);
    (void)best_dy;  // dy re-derived by the actual application; identical (calc is pure)
    if (exact) return json{{"dy", dec(dy)}, {"i", best_i}, {"inferred", true}};
    return json{{"matched", false}, {"dy", dec(dy)}, {"i", best_i}};
}

// ---- admin events ----------------------------------------------------------
// RampA/StopRampA event values are the contract's STORAGE units in both
// vintages (v1 raw A; v2 A*100 — `_future_A_p = _future_A * A_PRECISION` is
// what Plain*Basic emits). Applied verbatim.

inline json do_ramp_a(Pool& p, const json& ev) {
    p.initial_A = parse_u(ev.at("initial_A"));
    p.future_A = parse_u(ev.at("future_A"));
    p.initial_A_time = parse_u(ev.at("initial_time"));
    p.future_A_time = parse_u(ev.at("future_time"));
    return json{{"applied", true}};
}

inline json do_stop_ramp(Pool& p, const json& ev, const u& ts) {
    const u A = parse_u(ev.at("A"));
    p.initial_A = A;
    p.future_A = A;
    p.initial_A_time = ts;
    p.future_A_time = ts;
    return json{{"applied", true}};
}

inline json do_new_fee(Pool& p, const json& ev) {
    if (ev.contains("fee")) p.fee = parse_u(ev.at("fee"));
    if (ev.contains("admin_fee")) p.admin_fee = parse_u(ev.at("admin_fee"));
    return json{{"applied", true}};
}

// ---- probes (engine contract v2) -------------------------------------------
// spot[j-1] = 1e18 * (real coin-j out) / (real coin-0 in) for a zero-size
// FEE-FREE trade == lim dx->0 of get_dy(0, j, dx)/dx, in REAL (decimal-folded)
// units. Numerical derivative through the engine's own get_y, per the spec:
//   dxp = max(1, xp[0] / 1e6)          (xp units)
//   dyp = xp[j] - get_y(0, j, xp[0]+dxp, ...)
//   spot = 1e18 * (dyp * 1e18 / rates[j]) / (dxp * 1e18 / rates[0])
//        = 1e18 * dyp * rates[0] / (dxp * rates[j])
// Any revert inside the probe (non-convergence, empty pool) yields "0" for
// that leg — a probe must never abort or mutate a replay.

inline void spot_prices(const Pool& p, const u& ts, json& spot, json& spot_xp) {
    std::vector<u> xp;
    u amp = 0, D = 0, dxp = 1;
    bool ok = true;
    try {
        xp = xp_mem(p, p.balances);
        amp = calc_A(p, ts);
        D = get_D_p(p, xp, amp);
        dxp = xp[0] / 1000000;
        if (dxp == 0) dxp = 1;
    } catch (const std::exception&) {
        ok = false;
    }
    for (int j = 1; j < p.n; ++j) {
        u s = 0, sx = 0;
        if (ok && D > 0) {
            try {
                const u y = get_y(p, 0, j, xp[0] + dxp, xp, amp, D);
                if (xp[j] > y) {
                    const u dyp = xp[j] - y;
                    s = fdiv(dyp * p.rates[0] * PRECISION(), dxp * p.rates[j]);
                    sx = fdiv(dyp * PRECISION(), dxp);
                }
            } catch (const std::exception&) {
                s = 0;
                sx = 0;
            }
        }
        spot.push_back(dec(s));
        spot_xp.push_back(dec(sx));
    }
}

// One probe object: {i, bal[], sup, D, vp, spot[n-1], cfee[], cadm[], cvol[]}.
// No "adm": this family keeps no admin_balances array (the admin slice leaves
// balances[] directly).
inline json make_probe(const Pool& p, int idx, const u& ts, const Meter& mt) {
    json jbal = json::array();
    for (const auto& b : p.balances) jbal.push_back(dec(b));
    u D = 0, vp = 0;
    try {
        D = get_D_mem(p, p.balances, calc_A(p, ts));
        if (p.total_supply > 0) vp = fdiv(D * PRECISION(), p.total_supply);
    } catch (const std::exception&) {
        D = 0;
        vp = 0;
    }
    json spot = json::array(), spot_xp = json::array();
    spot_prices(p, ts, spot, spot_xp);
    json pr{{"i", idx},
            {"bal", jbal},
            {"sup", dec(p.total_supply)},
            {"D", dec(D)},
            {"vp", dec(vp)},
            {"spot", spot},
            {"spot_xp", spot_xp}};
    mt.cum_into(pr);
    return pr;
}

// ---- job driver ------------------------------------------------------------

inline std::vector<u> parse_u_array(const json& j, int n, const char* what) {
    if (!j.is_array() || static_cast<int>(j.size()) != n)
        throw std::runtime_error(std::string("bad array length for ") + what);
    std::vector<u> r(n);
    for (int i = 0; i < n; ++i) r[i] = parse_u(j[i]);
    return r;
}

inline json run(const json& job) {
    const std::string kind = job.at("kind").get<std::string>();
    Pool p;
    if (kind == "stable_v1") p.v2 = false;
    else if (kind == "stable_v2") p.v2 = true;
    else if (kind == "stable_lido") { p.v2 = true; p.flavor = Flavor::steth; }
    else if (kind == "stable_lido_ng") { p.v2 = true; p.flavor = Flavor::ng; }
    else if (kind == "stable_lido_bal") { p.v2 = true; p.flavor = Flavor::bal; }
    else throw std::runtime_error("stable_classic: unknown kind " + kind);

    // Alternative routing: kind "stable_v2" + job field "flavor"
    if (job.contains("flavor")) {
        const std::string f = job.at("flavor").get<std::string>();
        if (f == "steth") { p.v2 = true; p.flavor = Flavor::steth; }
        else if (f == "ng") { p.v2 = true; p.flavor = Flavor::ng; }
        else if (f == "balances") { p.v2 = true; p.flavor = Flavor::bal; }
        else throw std::runtime_error("stable_classic: unknown flavor " + f);
    }
    if (p.flavor != Flavor::none) {
        // steth / stETH-ng hold native ETH as coin 0, stETH as coin 1;
        // the Plain2Balances pool (stETH/frxETH) has stETH as coin 0.
        p.rebase_coin = (p.flavor == Flavor::bal) ? 0 : 1;
        if (job.contains("rebase_coin")) p.rebase_coin = job.at("rebase_coin").get<int>();
        if (job.contains("wei_in")) p.wei_in = parse_u(job.at("wei_in"));
        if (job.contains("wei_out")) p.wei_out = parse_u(job.at("wei_out"));
    }

    p.n = job.at("n").get<int>();
    if (p.n < 2) throw std::runtime_error("stable_classic: n < 2");
    p.rates = parse_u_array(job.at("rates"), p.n, "rates");

    const json& prm = job.at("params");
    p.initial_A = parse_u(prm.at("initial_A"));
    p.future_A = parse_u(prm.at("future_A"));
    p.initial_A_time = parse_u(prm.at("initial_A_time"));
    p.future_A_time = parse_u(prm.at("future_A_time"));
    p.fee = parse_u(prm.at("fee"));
    p.admin_fee = parse_u(prm.at("admin_fee"));

    const json& st = job.at("state");
    p.balances = parse_u_array(st.at("balances"), p.n, "balances");
    p.total_supply = parse_u(st.at("total_supply"));

    // ---- engine contract v2 job flags (all default OFF) --------------------
    const bool cf = job.value("cf", false);
    const bool probe_all = job.value("probe_all", false);
    const bool probe_last = job.value("probe_last", false);
    Meter mt;
    mt.init(p.n);
    json probes = json::array();
    bool any_probe = false;

    const json& jevents = job.at("events");
    const int n_events = static_cast<int>(jevents.size());

    json out_events = json::array();
    int idx = -1;
    for (const json& ev : jevents) {
        ++idx;
        const std::string type = ev.at("type").get<std::string>();
        const u ts = ev.contains("ts") ? parse_u(ev.at("ts")) : u(0);

        if (!cf) {
            // Optional hard-sync: the contract's admin-net math balances (and/or
            // total supply) JUST BEFORE this event — the hook for per-block
            // balance syncing on live-balance (rebasing stETH) pools.
            // These encode the REAL history's state, so cf mode ignores them.
            if (ev.contains("balances"))
                p.balances = parse_u_array(ev.at("balances"), p.n, "event balances");
            if (ev.contains("total_supply_pre"))
                p.total_supply = parse_u(ev.at("total_supply_pre"));
        } else if (ev.contains("rebase_mul") && !ev.at("rebase_mul").is_null()) {
            // cf replacement for the absolute syncs: bal[i] = bal[i]*num/den.
            // Exogenous -> applied before the snapshot, never rolled back.
            const json& rm = ev.at("rebase_mul");
            for (int i = 0; i < p.n && i < static_cast<int>(rm.size()); ++i) {
                if (rm[i].is_null()) continue;
                p.balances[i] = fdiv(p.balances[i] * parse_u(rm[i][0]), parse_u(rm[i][1]));
            }
        }

        // Per-event override of the stETH transfer-rounding model. The
        // job-level wei_in/wei_out are a 1-wei APPROXIMATION of share
        // math; when the harness can compute the exact rounding for this
        // event (from Lido's shares at that block) it supplies it here.
        // Restored after the event so it never leaks to the next one.
        const u save_in = p.wei_in, save_out = p.wei_out;
        if (ev.contains("wei_in")) p.wei_in = parse_u(ev.at("wei_in"));
        if (ev.contains("wei_out")) p.wei_out = parse_u(ev.at("wei_out"));

        const Pool snapshot = p;  // restored on revert
        json rec;
        ++mt.n_events;
        try {
            json outputs;
            if (type == "exchange") {
                u dx_recv;
                // dx_recv is an absolute measurement of the real history
                const bool has_recv = !cf && ev.contains("dx_recv");
                if (has_recv) dx_recv = parse_u(ev.at("dx_recv"));
                outputs = do_exchange(p, ev.at("sold_id").get<int>(),
                                      ev.at("bought_id").get<int>(),
                                      parse_u(ev.at("dx")), ts,
                                      has_recv ? &dx_recv : nullptr, &mt);
            } else if (type == "add") {
                outputs = do_add(p, parse_u_array(ev.at("amounts"), p.n, "amounts"), ts, &mt);
            } else if (type == "remove") {
                if (cf && ev.contains("burn_frac"))
                    outputs = do_remove_burn(
                        p, fdiv(p.total_supply * parse_u(ev.at("burn_frac")), PRECISION()));
                else
                    outputs = do_remove(p, parse_u(ev.at("supply_after")));
            } else if (type == "remove_one") {
                const u burn =
                    (cf && ev.contains("burn_frac"))
                        ? fdiv(p.total_supply * parse_u(ev.at("burn_frac")), PRECISION())
                        : parse_u(ev.at("burn"));
                outputs = do_remove_one(p, burn, ev.at("i").get<int>(), ev, ts, &mt);
            } else if (type == "remove_imb") {
                outputs = do_remove_imbalance(
                    p, parse_u_array(ev.at("amounts"), p.n, "amounts"), ts, &mt);
            } else if (type == "ramp_a") {
                outputs = do_ramp_a(p, ev);
            } else if (type == "stop_ramp") {
                outputs = do_stop_ramp(p, ev, ts);
            } else if (type == "new_fee") {
                outputs = do_new_fee(p, ev);
            } else {
                outputs = json{{"skipped", true}};
            }
            rec = json{{"type", type}, {"outputs", outputs}};
        } catch (const std::exception& e) {
            p = snapshot;
            ++mt.n_reverts;
            rec = json{{"type", type}, {"revert", e.what()}};
        }
        out_events.push_back(std::move(rec));

        p.wei_in = save_in;
        p.wei_out = save_out;

        if (probe_all || ev.value("probe", false) ||
            (probe_last && idx == n_events - 1)) {
            probes.push_back(make_probe(p, idx, ts, mt));
            any_probe = true;
        }
    }

    json jbal = json::array();
    for (const auto& b : p.balances) jbal.push_back(dec(b));
    json result{{"events", std::move(out_events)},
                {"final", json{{"balances", jbal}, {"total_supply", dec(p.total_supply)}}}};
    if (any_probe) result["probes"] = std::move(probes);
    result["meter"] = mt.to_json();
    return result;
}

}  // namespace stable_classic_detail

inline nlohmann::json run_stable_classic(const nlohmann::json& job) {
    return stable_classic_detail::run(job);
}
