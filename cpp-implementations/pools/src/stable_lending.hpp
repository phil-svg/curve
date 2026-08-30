#pragma once
// ============================================================================
// stable_lending.hpp — wei-exact replay state machine for the Curve IronBank
// pool (StableSwapIB, cyDAI/cyUSDC/cyUSDT) — a Compound-style lending pool
// whose coin "rates" are cToken exchange rates that accrue interest per block.
//
//   kind "stable_lending"   0x2dded6da1bf5dbdf597c45fcfaa3194e53ecfeaf
//
// DEPLOYED SOURCES FOLLOWED LINE-BY-LINE (Blockscout verified, 2026-08-29):
//   * Pool: StableSwapIB.vy, vyper 0.2.8, N_COINS=3, A_PRECISION=100,
//     PRECISION_MUL=[1, 1e12, 1e12]. get_D/get_y/get_y_D raise on
//     non-convergence; RemoveLiquidityImbalance burns (D0-D2)*ts/D0 with
//     assert != 0 and NO +1 (0.2.8 steth-style); RemoveLiquidityOne event
//     carries NO coin index (inferred here via dy match, lowest index wins).
//   * cyTokens (all three are CErc20 delegators to the same implementation
//     0x7e8844ea4c211a69ad9308ba0b6cdb3ea0bb2b05, CCollateralCapErc20Delegate,
//     solc 0.5.17): accrueInterest / exchangeRateStoredInternal /
//     mintFresh / redeemFresh ported from its CToken.sol + Exponential.sol.
//     getCashPrior() == internalCash (NOT balanceOf — donation-immune).
//     borrowRateMaxMantissa = 0.0005e16 = 5e12 (require, i.e. revert).
//   * Interest rate model (all three markets): TripleSlopeRateModel
//     0x5931ee26a25b8d4ba3ced3dc32653606a76a7612 (solc 0.5.17), params
//     immutable after construction (updateTripleRateModelInternal is only
//     called from the constructor).
//
// RATES SEMANTICS (the heart of this pool):
//   _stored_rates() [VIEWS ONLY: get_dy/get_dx/calc_*/get_virtual_price]:
//     rate  = cy.exchangeRateStored()
//     rate += rate * cy.supplyRatePerBlock()
//                  * (block.number - cy.accrualBlockNumber()) / 1e18
//     result[i] = PRECISION_MUL[i] * rate
//   _current_rates() [ALL MUTATING MATH: exchange, exchange_underlying,
//     add_liquidity, remove_liquidity_imbalance, remove_liquidity_one_coin]:
//     result[i] = PRECISION_MUL[i] * cy.exchangeRateCurrent()
//     where exchangeRateCurrent() = accrueInterest(); exchangeRateStored().
//     remove_liquidity (proportional) uses NO rates at all.
//
// COMPOUND ACCRUAL PORTED (CToken.accrueInterest, exact uint256 floors):
//     if accrualBlockNumber == block.number: no-op
//     borrowRate = TripleSlope(cash, borrows, reserves):
//         util = borrows == 0 ? 0
//              : min(borrows*1e18/(cash+borrows-reserves), roof)
//         util <= kink1: util*multiplierPerBlock/1e18 + baseRatePerBlock
//         util <= kink2: kink1*multiplierPerBlock/1e18 + baseRatePerBlock
//         else: (util-kink2)*jumpMultiplierPerBlock/1e18
//               + kink1*multiplierPerBlock/1e18 + baseRatePerBlock
//     require borrowRate <= 5e12 ("borrow rate too high")
//     sif       = borrowRate * blockDelta            (exact, no truncation)
//     interest  = sif * borrows / 1e18
//     borrows  += interest
//     reserves += reserveFactor * interest / 1e18
//     borrowIndex += sif * borrowIndex / 1e18
//     exchangeRateStored = supply == 0 ? initialExchangeRate
//                        : (cash + borrows - reserves) * 1e18 / supply
//
// MINT / REDEEM (exchange_underlying path; CCollateralCapErc20):
//     mintTokens   = floor(floor(amount * 1e36 / rate) / 1e18)
//                    (div_ScalarByExpTruncate; == floor(amount*1e18/rate))
//                    cash += amount; supply += mintTokens
//                    [rate read BEFORE the cash credit]
//     redeemAmount = floor(rate * redeemTokens / 1e18)  (mul_ScalarTruncate)
//                    require cash >= redeemAmount ("insufficient cash")
//                    cash -= redeemAmount; supply -= redeemTokens
//   exchange_underlying(i,j,dx): transferFrom underlying dx; mint(dx) on
//   cy[i] (accrues i; actualMintAmount == dx — DAI/USDC/USDT charge no
//   transfer fee, documented assumption); dx_ = minted cyTokens;
//   _exchange(i,j,dx_) with _current_rates (rates[i] is the POST-MINT rate —
//   the mint changes cash&supply and thus the floor by rounding); redeem(dy_)
//   on cy[j]; event dy = the pool's ENTIRE underlying-j balance
//   (= redeemAmount + optional per-event "dust_j", default 0).
//
// CYTOKEN STATE IS EXOGENOUS, PER EVENT: the pool holds only a slice of each
// cyToken; third parties mint/redeem/borrow on IronBank between pool events,
// so cyToken state cannot be carried across events. Each rate-using event
// carries "cy": 3x {cash, borrows, reserves, supply, borrow_index,
// accrual_block} — the mid-block-correct state JUST BEFORE the event's tx
// (normally read at block-1; the feeder reconstructs via cyToken logs when a
// tx earlier in the same block touched a cyToken). Like stable_ng's per-event
// "rates", cy state is not rolled back on revert (it is chain state, not
// pool state). The engine accrues to ev.block internally and, for
// exchange_underlying, applies its own intra-tx mint/redeem deltas.
//
// EVENT VALUE SEMANTICS (all verified against the deployed vyper):
//   * AddLiquidity.invariant   = D1, the PRE-FEE invariant.
//   * AddLiquidity.amounts     = credited cyToken amounts (for underlying
//     deposits: the measured mint output). Optional per-event
//     "underlying_amounts" makes the engine model the mints itself and
//     cross-check mintTokens == amounts wei-for-wei ("mint_check").
//   * RemoveLiquidityImbalance amounts = cyToken units (the underlying
//     variant converts BEFORE the math: amount*PRECISION_MUL[i]*1e18/rates[i]
//     — events already carry the converted values), invariant = D1 (after
//     withdrawal, before fees), burn asserts != 0 with NO +1.
//   * RemoveLiquidityOne = (lp burned, dy) — no index, no supply.
//   * RampA emits STORAGE units (initial=_A() already *100, future =
//     _future_A*100); applied verbatim. StopRampA pins both to A at ts.
//   * TokenExchangeUnderlying.tokens_sold is the CALLER-REQUESTED underlying
//     dx (pre-doTransferIn), tokens_bought the underlying sent out.
//
// Admin fees accrue implicitly (balanceOf - balances); the engine accumulates
// them per coin in "admin_fees" (final output) so the harness can compare the
// on-chain admin_balances(i) delta over the window.
//
// ENGINE CONTRACT v2 (specs/ENGINE_CF_CONTRACT.md) — purely additive; with
// none of the new job fields set the event outputs, "final" and
// "initial_probe" are byte-for-byte what they were before.
//   job:  "probe_all" / "probe_last" / "cf" (bool), per-event "probe" (bool)
//         and, in cf mode, per-event "burn_frac" (1e18 fraction of the LIVE
//         total supply to burn — used by remove and remove_one).
//   out:  result["probes"] (only when a probe was requested) and
//         result["meter"] (always).
//
//  * "spot"[j-1] is the fee-free marginal price of coin j in coin 0 units
//    **IN CYTOKEN UNITS** — i.e. d(balances[0])/d(balances[j]) with balances
//    in the pool's own accounting units (8-decimal cyTokens), 1e18-scaled.
//    It is NOT an underlying-asset price: converting to DAI/USDC/USDT terms
//    needs the cyToken exchange rates, which the harness already feeds this
//    engine per event (and which every probe echoes back as "rates" =
//    PRECISION_MUL[i] * exchangeRate, the pool's own `rates` vector).
//      spot_cy[j] = (dxp0/dxpj) * rates[j] / rates[0]
//      dxp0/dxpj  = (Ann + Dr/xp[j]) / (Ann + Dr/xp[0])   [exact stableswap
//                   closed form, Dr = D^(n+1) / (n^n * prod(xp)),
//                   Ann = A*n / A_PRECISION]
//    The rates used are the ones of the most recent rate-bearing event (they
//    are chain state, not pool state, so they are not rolled back on revert);
//    before the first such event the job's state.cy/state.block _stored_rates
//    are used — the same basis as get_virtual_price()/get_dy() views.
//  * "D" and "vp" in a probe are recomputed live from those rates
//    (vp = D * 1e18 / totalSupply, the get_virtual_price definition).
//  * "adm" is the CUMULATIVE admin fee accrued over the replay window, per
//    coin, in cyToken units — this vintage has no admin_balances storage
//    slot (admin fees are implicit: token.balanceOf(pool) - balances[i]), so
//    an absolute bucket does not exist. It is the same vector as
//    final["admin_fees"].
//  * meter: "fee"/"admin"/"vol" are all in cyToken units (= balances units).
//    Swap fees land on the output coin; add / remove_imbalance / remove_one
//    imbalance fees land per coin, exactly as the contract charges them.
//    "vol" counts the pool-facing input: exchange_underlying contributes its
//    MINTED cyToken amount, not the underlying dx. No "admin_lp" /
//    "max_ps_gap_bp": this pool mints no admin LP and has no price_scale.
//  * "cfee"/"cadm"/"cvol" in a probe are those same three accumulators AS OF
//    that event, so the last probe equals result["meter"] exactly. The meter
//    lives inside Pool, so a reverted event's state restore rolls it back and
//    that event's probe shows the not-counted (pre-event) totals. "cadm"
//    repeats "adm" by construction (the meter's admin leg IS admin_fees). No
//    "cfee_lp"/"cadm_lp", matching the absent "admin_lp".
//  * "rebase_mul" does not apply to this family and is ignored.
//
// Job/result schema: see run_stable_lending() at the bottom; the full
// integration spec lives in specs/stable_lending.md.
// ============================================================================

#include <boost/multiprecision/cpp_int.hpp>
#include <nlohmann/json.hpp>

#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace stable_lending_detail {

using u = boost::multiprecision::cpp_int;
using json = nlohmann::json;

inline const u& E18() { static const u v("1000000000000000000"); return v; }
inline const u& FEE_DENOM() { static const u v("10000000000"); return v; }  // 1e10
inline const int N = 3;
inline const u A_PRECISION = 100;

struct PoolRevert : std::runtime_error {
    explicit PoolRevert(const std::string& m) : std::runtime_error(m) {}
};

// vyper/solidity checked ops on uint256 (cpp_int is unbounded; on-chain
// events that landed cannot have overflowed, so only underflow is modeled)
inline u fdiv(const u& a, const u& b) {
    if (b == 0) throw PoolRevert("division by zero");
    return a / b;  // non-negative operands: C++ '/' == floor
}
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
inline std::vector<u> parse_arr(const json& j, int n, const char* what) {
    if (!j.is_array() || static_cast<int>(j.size()) != n)
        throw std::runtime_error(std::string("bad array length for ") + what);
    std::vector<u> r(n);
    for (int i = 0; i < n; ++i) r[i] = parse_u(j[i]);
    return r;
}
inline json svec(const std::vector<u>& v) {
    json a = json::array();
    for (const auto& x : v) a.push_back(dec(x));
    return a;
}

// ---- cyToken side: TripleSlopeRateModel + CToken accrual -------------------

struct Irm {  // per-coin rate model params + cyToken constants
    u base_rate_per_block, multiplier_per_block, jump_multiplier_per_block;
    u kink1, kink2, roof;
    u reserve_factor;         // cyToken.reserveFactorMantissa
    u borrow_rate_max;        // borrowRateMaxMantissa (5e12 deployed)
    u initial_exchange_rate;  // used iff totalSupply == 0 (never, live)
};

struct Cy {  // cyToken market state just before the event's tx
    u cash;           // internalCash (CCollateralCapErc20.getCashPrior)
    u borrows, reserves, supply, borrow_index;
    u accrual_block;
    bool loaded = false;
};

// TripleSlopeRateModel.utilizationRate — SafeMath, floors, roof cap
inline u util_rate(const Irm& m, const u& cash, const u& borrows, const u& reserves) {
    if (borrows == 0) return 0;
    u util = fdiv(borrows * E18(), fsub(cash + borrows, reserves));
    if (util > m.roof) util = m.roof;
    return util;
}

// TripleSlopeRateModel.getBorrowRate
inline u borrow_rate(const Irm& m, const u& cash, const u& borrows, const u& reserves) {
    const u util = util_rate(m, cash, borrows, reserves);
    if (util <= m.kink1)
        return fdiv(util * m.multiplier_per_block, E18()) + m.base_rate_per_block;
    if (util <= m.kink2)
        return fdiv(m.kink1 * m.multiplier_per_block, E18()) + m.base_rate_per_block;
    const u normal = fdiv(m.kink1 * m.multiplier_per_block, E18()) + m.base_rate_per_block;
    return fdiv(fsub(util, m.kink2) * m.jump_multiplier_per_block, E18()) + normal;
}

// TripleSlopeRateModel.getSupplyRate (only _stored_rates / views use it)
inline u supply_rate(const Irm& m, const u& cash, const u& borrows,
                     const u& reserves, const u& reserve_factor) {
    const u one_minus = fsub(E18(), reserve_factor);
    const u rate_to_pool = fdiv(borrow_rate(m, cash, borrows, reserves) * one_minus, E18());
    return fdiv(util_rate(m, cash, borrows, reserves) * rate_to_pool, E18());
}

// CToken.accrueInterest — mutates the market state to block `bn`
inline void accrue(Cy& c, const Irm& m, const u& bn) {
    if (c.accrual_block == bn) return;  // short-circuit 0 interest
    const u rate = borrow_rate(m, c.cash, c.borrows, c.reserves);
    if (rate > m.borrow_rate_max) throw PoolRevert("borrow rate too high");
    const u block_delta = fsub(bn, c.accrual_block);
    const u sif = rate * block_delta;                       // Exp mantissa, exact
    const u interest = fdiv(sif * c.borrows, E18());        // mul_ScalarTruncate
    c.borrows += interest;
    c.reserves = fdiv(m.reserve_factor * interest, E18()) + c.reserves;
    c.borrow_index = fdiv(sif * c.borrow_index, E18()) + c.borrow_index;
    c.accrual_block = bn;
}

// CToken.exchangeRateStoredInternal
inline u ex_rate_stored(const Cy& c, const Irm& m) {
    if (c.supply == 0) return m.initial_exchange_rate;
    return fdiv(fsub(c.cash + c.borrows, c.reserves) * E18(), c.supply);
}

// CCollateralCapErc20.mintFresh: rate is read BEFORE the cash credit;
// mintTokens = div_ScalarByExpTruncate(amount, rate) — the literal double
// floor floor(floor(amount*1e36/rate)/1e18) (== floor(amount*1e18/rate))
inline u cy_mint(Cy& c, const Irm& m, const u& amount) {
    const u rate = ex_rate_stored(c, m);
    const u mint_tokens = fdiv(fdiv(amount * E18() * E18(), rate), E18());
    c.cash += amount;
    c.supply += mint_tokens;
    return mint_tokens;
}

// CCollateralCapErc20.redeemFresh (redeemTokensIn > 0 branch)
inline u cy_redeem(Cy& c, const Irm& m, const u& redeem_tokens) {
    const u rate = ex_rate_stored(c, m);
    const u redeem_amount = fdiv(rate * redeem_tokens, E18());  // mul_ScalarTruncate
    if (c.cash < redeem_amount) throw PoolRevert("insufficient cash");
    c.cash = fsub(c.cash, redeem_amount);
    c.supply = fsub(c.supply, redeem_tokens);
    return redeem_amount;
}

// ---- pool state ------------------------------------------------------------

// Engine-contract-v2 revenue meter. Lives INSIDE Pool on purpose: the driver
// snapshots/restores Pool around every event, so a reverted event accrues
// nothing. Never read by the consensus math. (The admin slice is already
// accumulated by Pool::admin_fees, which the meter reuses verbatim.)
struct Meters {
    std::vector<u> fee;   // gross fee charged per coin, cyToken units
    std::vector<u> vol;   // gross exchange input per coin, cyToken units
};

struct Pool {
    std::vector<u> precision_mul;   // [1, 1e12, 1e12]
    std::vector<u> balances;        // self.balances (cyToken units)
    u total_supply;                 // LP token totalSupply
    u fee, admin_fee;               // 1e10-scaled
    u initial_A, future_A;          // stored *A_PRECISION
    u initial_A_time, future_A_time;
    std::vector<Irm> irm;           // per coin
    std::vector<u> admin_fees;      // accumulated admin fees per coin (window)
    Meters m;                       // contract-v2 meter (not pool state)
};

// StableSwapIB._A()
inline u calc_A(const Pool& p, const u& ts) {
    const u& t1 = p.future_A_time;
    const u& A1 = p.future_A;
    if (ts < t1) {
        const u& A0 = p.initial_A;
        const u& t0 = p.initial_A_time;
        if (A1 > A0) return A0 + fdiv((A1 - A0) * fsub(ts, t0), fsub(t1, t0));
        return fsub(A0, fdiv((A0 - A1) * fsub(ts, t0), fsub(t1, t0)));
    }
    return A1;
}

// StableSwapIB.get_D — loop-form D_P, A_PRECISION update, raise on no-conv
inline u get_D(const std::vector<u>& xp, const u& amp) {
    u S = 0;
    for (const auto& x : xp) S += x;
    if (S == 0) return 0;
    u D = S;
    const u Ann = amp * N;
    for (int it = 0; it < 255; ++it) {
        u D_P = D;
        for (const auto& x : xp) D_P = fdiv(D_P * D, x * N);
        const u Dprev = D;
        D = fdiv((fdiv(Ann * S, A_PRECISION) + D_P * N) * D,
                 fdiv(fsub(Ann, A_PRECISION) * D, A_PRECISION) + (N + 1) * D_P);
        if (D > Dprev ? D - Dprev <= 1 : Dprev - D <= 1) return D;
    }
    throw PoolRevert("get_D did not converge");
}

inline std::vector<u> xp_mem(const std::vector<u>& rates, const std::vector<u>& bal) {
    std::vector<u> r(N);
    for (int i = 0; i < N; ++i) r[i] = fdiv(rates[i] * bal[i], E18());
    return r;
}

// StableSwapIB.get_y (computes D internally from xp_, as deployed)
inline u get_y(int i, int j, const u& x,
               const std::vector<u>& xp, const u& amp) {
    if (i == j) throw PoolRevert("same coin");
    if (j < 0 || j >= N) throw PoolRevert("j out of range");
    if (i < 0 || i >= N) throw PoolRevert("i out of range");
    const u D = get_D(xp, amp);
    const u Ann = amp * N;
    u c = D, S_ = 0;
    for (int k = 0; k < N; ++k) {
        u xk;
        if (k == i) xk = x;
        else if (k != j) xk = xp[k];
        else continue;
        S_ += xk;
        c = fdiv(c * D, xk * N);
    }
    c = fdiv(c * D * A_PRECISION, Ann * N);
    const u b = S_ + fdiv(D * A_PRECISION, Ann);
    u y = D;
    for (int it = 0; it < 255; ++it) {
        const u y_prev = y;
        y = fdiv(y * y + c, fsub(2 * y + b, D));
        if (y > y_prev ? y - y_prev <= 1 : y_prev - y <= 1) return y;
    }
    throw PoolRevert("get_y did not converge");
}

// StableSwapIB.get_y_D
inline u get_y_D(const u& amp, int i, const std::vector<u>& xp, const u& D) {
    if (i < 0 || i >= N) throw PoolRevert("i out of range");
    const u Ann = amp * N;
    u c = D, S_ = 0;
    for (int k = 0; k < N; ++k) {
        if (k == i) continue;
        S_ += xp[k];
        c = fdiv(c * D, xp[k] * N);
    }
    c = fdiv(c * D * A_PRECISION, Ann * N);
    const u b = S_ + fdiv(D * A_PRECISION, Ann);
    u y = D;
    for (int it = 0; it < 255; ++it) {
        const u y_prev = y;
        y = fdiv(y * y + c, fsub(2 * y + b, D));
        if (y > y_prev ? y - y_prev <= 1 : y_prev - y <= 1) return y;
    }
    throw PoolRevert("get_y_D did not converge");
}

// ---- per-event cyToken state loading ---------------------------------------

inline std::vector<Cy> load_cy(const json& ev) {
    if (!ev.contains("cy"))
        throw PoolRevert("missing per-event cy state for a rate-using event");
    const json& arr = ev.at("cy");
    if (!arr.is_array() || arr.size() != static_cast<size_t>(N))
        throw PoolRevert("bad cy array");
    std::vector<Cy> out(N);
    for (int i = 0; i < N; ++i) {
        const json& c = arr[i];
        out[i].cash = parse_u(c.at("cash"));
        out[i].borrows = parse_u(c.at("borrows"));
        out[i].reserves = parse_u(c.at("reserves"));
        out[i].supply = parse_u(c.at("supply"));
        out[i].borrow_index = parse_u(c.at("borrow_index"));
        out[i].accrual_block = parse_u(c.at("accrual_block"));
        out[i].loaded = true;
    }
    return out;
}

// StableSwapIB._current_rates(): accrue every market to `bn`, then
// rates[i] = PRECISION_MUL[i] * exchangeRateStored()
inline std::vector<u> current_rates(const Pool& p, std::vector<Cy>& cy, const u& bn) {
    std::vector<u> rates(N);
    for (int i = 0; i < N; ++i) {
        accrue(cy[i], p.irm[i], bn);
        rates[i] = p.precision_mul[i] * ex_rate_stored(cy[i], p.irm[i]);
    }
    return rates;
}

// StableSwapIB._stored_rates() at block `bn` (VIEW ONLY — no mutation):
// rate += rate * supplyRatePerBlock * (bn - accrualBlockNumber) / 1e18
inline std::vector<u> stored_rates(const Pool& p, const std::vector<Cy>& cy, const u& bn) {
    std::vector<u> rates(N);
    for (int i = 0; i < N; ++i) {
        const Cy& c = cy[i];
        u rate = ex_rate_stored(c, p.irm[i]);
        const u sr = supply_rate(p.irm[i], c.cash, c.borrows, c.reserves,
                                 p.irm[i].reserve_factor);
        rate += fdiv(rate * sr * fsub(bn, c.accrual_block), E18());
        rates[i] = p.precision_mul[i] * rate;
    }
    return rates;
}

// ---- pool operations -------------------------------------------------------

// StableSwapIB._exchange (shared by exchange and exchange_underlying):
// returns dy (cyToken units) and mutates balances + admin fee accumulator
inline u exchange_core(Pool& p, const std::vector<u>& rates, int i, int j,
                       const u& dx, const u& ts) {
    if (i < 0 || i >= N) throw PoolRevert("i out of range");
    if (j < 0 || j >= N) throw PoolRevert("j out of range");
    const std::vector<u> old_balances = p.balances;
    const std::vector<u> xp = xp_mem(rates, old_balances);

    const u x = xp[i] + fdiv(dx * rates[i], E18());
    const u amp = calc_A(p, ts);
    const u y = get_y(i, j, x, xp, amp);

    u dy = fsub(fsub(xp[j], y), 1);  // -1 just in case of rounding errors
    const u dy_fee = fdiv(dy * p.fee, FEE_DENOM());

    dy = fdiv(fsub(dy, dy_fee) * E18(), rates[j]);
    u dy_admin_fee = fdiv(dy_fee * p.admin_fee, FEE_DENOM());
    dy_admin_fee = fdiv(dy_admin_fee * E18(), rates[j]);

    p.balances[i] = old_balances[i] + dx;
    p.balances[j] = fsub(fsub(old_balances[j], dy), dy_admin_fee);
    p.admin_fees[j] += dy_admin_fee;

    // meter only: dy_fee is an xp-space amount; the coin-unit gross fee is
    // the same conversion the contract applies to dy / dy_admin_fee.
    p.m.fee[j] += fdiv(dy_fee * E18(), rates[j]);
    p.m.vol[i] += dx;
    return dy;
}

inline json accrual_report(const std::vector<Cy>& cy) {
    json a = json::array();
    for (const auto& c : cy)
        a.push_back(json{{"borrows", dec(c.borrows)},
                         {"reserves", dec(c.reserves)},
                         {"borrow_index", dec(c.borrow_index)}});
    return a;
}
inline json rates_report(const Pool& p, const std::vector<Cy>& cy) {
    json a = json::array();
    for (int i = 0; i < N; ++i) a.push_back(dec(ex_rate_stored(cy[i], p.irm[i])));
    return a;
}

// exchange(i, j, dx) — wrapped cyToken units both legs
inline json do_exchange(Pool& p, const json& ev, const u& ts, const u& bn) {
    std::vector<Cy> cy = load_cy(ev);
    const std::vector<u> rates = current_rates(p, cy, bn);
    const u dy = exchange_core(p, rates, ev.at("sold_id").get<int>(),
                               ev.at("bought_id").get<int>(),
                               parse_u(ev.at("dx")), ts);
    return json{{"dy", dec(dy)},
                {"ex_rates", rates_report(p, cy)},
                {"cy_post", accrual_report(cy)}};
}

// exchange_underlying(i, j, dx) — mint through cy[i], swap, redeem cy[j]
inline json do_exchange_underlying(Pool& p, const json& ev, const u& ts, const u& bn) {
    std::vector<Cy> cy = load_cy(ev);
    const int i = ev.at("sold_id").get<int>();
    const int j = ev.at("bought_id").get<int>();
    if (i < 0 || i >= N) throw PoolRevert("i out of range");
    if (j < 0 || j >= N) throw PoolRevert("j out of range");
    const u dx = parse_u(ev.at("dx"));  // underlying units, caller-requested

    // cyToken(coins[i]).mint(dx): accrueInterest, then mintFresh.
    // actualMintAmount == dx (DAI/USDC/USDT: no transfer fee — documented).
    accrue(cy[i], p.irm[i], bn);
    const u dx_wrapped = cy_mint(cy[i], p.irm[i], dx);

    // _exchange → _current_rates(): accrues all (no-op for i), rates read
    // AFTER the mint — rates[i] reflects the post-mint cash/supply rounding.
    const std::vector<u> rates = current_rates(p, cy, bn);
    const u dy_wrapped = exchange_core(p, rates, i, j, dx_wrapped, ts);

    // cyToken(coins[j]).redeem(dy_): accrue (no-op) + redeemFresh
    const u redeemed = cy_redeem(cy[j], p.irm[j], dy_wrapped);

    // event dy = ERC20(underlying_j).balanceOf(pool) — the whole balance;
    // "dust_j" (underlying held by the pool before the event, normally 0)
    const u dust = ev.contains("dust_j") ? parse_u(ev.at("dust_j")) : u(0);
    return json{{"dy", dec(redeemed + dust)},
                {"dx_wrapped", dec(dx_wrapped)},
                {"dy_wrapped", dec(dy_wrapped)},
                {"ex_rates", rates_report(p, cy)},
                {"cy_post", accrual_report(cy)}};
}

// add_liquidity(_amounts) — amounts in the event are credited cyToken units.
// With "underlying_amounts" the engine models the mints and cross-checks.
inline json do_add(Pool& p, const json& ev, const u& ts, const u& bn) {
    std::vector<Cy> cy = load_cy(ev);
    const std::vector<u> rates = current_rates(p, cy, bn);  // BEFORE the mints
    std::vector<u> amounts = parse_arr(ev.at("amounts"), N, "amounts");

    json mint_check = json::array();
    if (ev.contains("underlying_amounts")) {
        const std::vector<u> ua = parse_arr(ev.at("underlying_amounts"), N,
                                            "underlying_amounts");
        for (int i = 0; i < N; ++i) {
            if (ua[i] == 0) { mint_check.push_back(nullptr); continue; }
            const u minted = cy_mint(cy[i], p.irm[i], ua[i]);
            mint_check.push_back(minted == amounts[i]);
            amounts[i] = minted;  // the pool credits the measured mint output
        }
    }

    const u amp = calc_A(p, ts);
    const u token_supply = p.total_supply;
    const std::vector<u> old_balances = p.balances;
    const u D0 = get_D(xp_mem(rates, old_balances), amp);

    std::vector<u> new_balances = old_balances;
    for (int i = 0; i < N; ++i) {
        if (amounts[i] == 0) {
            if (token_supply == 0) throw PoolRevert("initial deposit requires all coins");
        } else {
            new_balances[i] = new_balances[i] + amounts[i];
        }
    }
    const u D1 = get_D(xp_mem(rates, new_balances), amp);
    if (!(D1 > D0)) throw PoolRevert("D1 <= D0");

    std::vector<u> fees(N, u(0));
    u mint_amount = 0;
    if (token_supply != 0) {
        const u base_fee = fdiv(p.fee * N, u(4) * (N - 1));
        for (int i = 0; i < N; ++i) {
            const u new_balance = new_balances[i];
            const u ideal_balance = fdiv(D1 * old_balances[i], D0);
            const u difference = ideal_balance > new_balance
                                     ? ideal_balance - new_balance
                                     : new_balance - ideal_balance;
            fees[i] = fdiv(base_fee * difference, FEE_DENOM());
            const u admin_cut = fdiv(fees[i] * p.admin_fee, FEE_DENOM());
            p.balances[i] = fsub(new_balance, admin_cut);
            p.admin_fees[i] += admin_cut;
            p.m.fee[i] += fees[i];   // meter only
            new_balances[i] = fsub(new_balances[i], fees[i]);
        }
        const u D2 = get_D(xp_mem(rates, new_balances), amp);
        mint_amount = fdiv(token_supply * fsub(D2, D0), D0);
    } else {
        p.balances = new_balances;
        mint_amount = D1;
    }
    p.total_supply = token_supply + mint_amount;

    json out{{"amounts", svec(amounts)},
             {"fees", svec(fees)},
             {"invariant", dec(D1)},
             {"supply", dec(p.total_supply)},
             {"minted", dec(mint_amount)},
             {"ex_rates", rates_report(p, cy)}};
    if (!mint_check.empty()) out["mint_check"] = mint_check;
    return out;
}

// remove_liquidity — proportional, NO rates, NO accrual.
// burn_override != nullptr <=> cf mode supplied a "burn_frac" (the historical
// supply_after describes a state path that no longer exists).
inline json do_remove(Pool& p, const json& ev, const u* burn_override = nullptr) {
    const u total_supply = p.total_supply;
    const u burn = burn_override
                       ? *burn_override
                       : fsub(total_supply, parse_u(ev.at("supply_after")));
    std::vector<u> amounts(N);
    for (int i = 0; i < N; ++i) {
        amounts[i] = fdiv(p.balances[i] * burn, total_supply);
        p.balances[i] = fsub(p.balances[i], amounts[i]);
    }
    p.total_supply = fsub(total_supply, burn);
    return json{{"amounts", svec(amounts)}, {"supply", dec(p.total_supply)}};
}

// remove_liquidity_imbalance(_amounts) — amounts already in cyToken units
inline json do_remove_imb(Pool& p, const json& ev, const u& ts, const u& bn) {
    std::vector<Cy> cy = load_cy(ev);
    const std::vector<u> rates = current_rates(p, cy, bn);
    const std::vector<u> amounts = parse_arr(ev.at("amounts"), N, "amounts");

    const u amp = calc_A(p, ts);
    const std::vector<u> old_balances = p.balances;
    const u D0 = get_D(xp_mem(rates, old_balances), amp);

    std::vector<u> new_balances = old_balances;
    for (int i = 0; i < N; ++i)
        if (amounts[i] > 0) new_balances[i] = fsub(new_balances[i], amounts[i]);
    const u D1 = get_D(xp_mem(rates, new_balances), amp);

    std::vector<u> fees(N, u(0));
    const u base_fee = fdiv(p.fee * N, u(4) * (N - 1));
    for (int i = 0; i < N; ++i) {
        const u ideal_balance = fdiv(D1 * old_balances[i], D0);
        const u new_balance = new_balances[i];
        const u difference = ideal_balance > new_balance
                                 ? ideal_balance - new_balance
                                 : new_balance - ideal_balance;
        fees[i] = fdiv(base_fee * difference, FEE_DENOM());
        const u admin_cut = fdiv(fees[i] * p.admin_fee, FEE_DENOM());
        p.balances[i] = fsub(new_balance, admin_cut);
        p.admin_fees[i] += admin_cut;
        p.m.fee[i] += fees[i];   // meter only
        new_balances[i] = fsub(new_balances[i], fees[i]);
    }
    const u D2 = get_D(xp_mem(rates, new_balances), amp);

    const u token_supply = p.total_supply;
    const u burn_amount = fdiv(fsub(D0, D2) * token_supply, D0);
    if (burn_amount == 0) throw PoolRevert("zero tokens burned");  // NO +1 (0.2.8)
    p.total_supply = fsub(token_supply, burn_amount);

    return json{{"fees", svec(fees)},
                {"invariant", dec(D1)},
                {"supply", dec(p.total_supply)},
                {"burned", dec(burn_amount)},
                {"ex_rates", rates_report(p, cy)}};
}

// _calc_withdraw_one_coin(_token_amount, i, use_underlying=False, rates) —
// pure; returns (dy, dy_fee) in cyToken units
inline std::pair<u, u> calc_one(const Pool& p, const std::vector<u>& rates,
                                const u& burn, int i, const u& ts) {
    if (i < 0 || i >= N) throw PoolRevert("i out of range");
    const u amp = calc_A(p, ts);
    const std::vector<u> xp = xp_mem(rates, p.balances);
    const u D0 = get_D(xp, amp);
    const u D1 = fsub(D0, fdiv(burn * D0, p.total_supply));
    const u new_y = get_y_D(amp, i, xp, D1);

    std::vector<u> xp_reduced = xp;
    const u base_fee = fdiv(p.fee * N, u(4) * (N - 1));
    for (int j = 0; j < N; ++j) {
        u dx_expected;
        if (j == i) dx_expected = fsub(fdiv(xp[j] * D1, D0), new_y);
        else        dx_expected = fsub(xp[j], fdiv(xp[j] * D1, D0));
        xp_reduced[j] = fsub(xp_reduced[j], fdiv(base_fee * dx_expected, FEE_DENOM()));
    }
    u dy = fsub(xp_reduced[i], get_y_D(amp, i, xp_reduced, D1));
    dy = fdiv(fsub(dy, 1) * E18(), rates[i]);
    const u dy_fee = fsub(fdiv(fsub(xp[i], new_y) * E18(), rates[i]), dy);
    return {dy, dy_fee};
}

inline u apply_one(Pool& p, const std::vector<u>& rates, const u& burn, int i, const u& ts) {
    const auto [dy, dy_fee] = calc_one(p, rates, burn, i, ts);
    const u admin_cut = fdiv(dy_fee * p.admin_fee, FEE_DENOM());
    p.balances[i] = fsub(p.balances[i], dy + admin_cut);
    p.admin_fees[i] += admin_cut;
    p.m.fee[i] += dy_fee;   // meter only (already coin units)
    p.total_supply = fsub(p.total_supply, burn);
    return dy;
}

// remove_liquidity_one_coin — the event has NO coin index: pass "i" >= 0 when
// known, else i == -1 + "dy_expected" and the engine infers it by dy match
// (pure calc per candidate; exact match short-circuits, lowest index wins).
inline json do_remove_one(Pool& p, const json& ev, const u& ts, const u& bn,
                          const u* burn_override = nullptr) {
    std::vector<Cy> cy = load_cy(ev);
    const std::vector<u> rates = current_rates(p, cy, bn);
    const u burn = burn_override ? *burn_override : parse_u(ev.at("burn"));
    int i = ev.contains("i") ? ev.at("i").get<int>() : -1;

    if (i < 0) {
        if (!ev.contains("dy_expected"))
            throw PoolRevert("remove_one: i=-1 without dy_expected");
        const u dy_expected = parse_u(ev.at("dy_expected"));
        int best_i = -1;
        u best_diff = 0;
        for (int cand = 0; cand < N; ++cand) {
            u dy;
            try { dy = calc_one(p, rates, burn, cand, ts).first; }
            catch (const PoolRevert&) { continue; }
            const u diff = dy > dy_expected ? dy - dy_expected : dy_expected - dy;
            if (diff == 0) { best_i = cand; break; }
            if (best_i < 0 || diff < best_diff) { best_i = cand; best_diff = diff; }
        }
        if (best_i < 0) throw PoolRevert("remove_one: all coin candidates revert");
        i = best_i;
    }
    const u dy = apply_one(p, rates, burn, i, ts);
    return json{{"dy", dec(dy)}, {"i", i},
                {"supply", dec(p.total_supply)},
                {"ex_rates", rates_report(p, cy)}};
}

// ---- engine contract v2: spot price + probes --------------------------------

// Fee-free marginal price of coin j in coin 0 units, in CYTOKEN units,
// 1e18-scaled (see the header block). Exact stableswap closed form:
//   F = Ann*S + D - Ann*D - D^(n+1)/(n^n*prod)  =>  dF/dx_i = Ann + Dr/x_i
//   dxp0/dxpj = (Ann + Dr/xp[j]) / (Ann + Dr/xp[0])
// with Ann = amp*n/A_PRECISION and Dr = D^(n+1)/(n^n*prod(xp)); the integer
// form below mirrors the twocrypto MATH.get_p shape (multiply through by
// xp[0]) so every division is a floor of a >=1e26-magnitude quantity.
// Empty / degenerate state -> all zeros.
// Never throws: a probe must not be able to fail a replay.
inline std::vector<u> spot_cy(const Pool& p, const std::vector<u>& rates,
                              const u& ts) {
    std::vector<u> out(N - 1, u(0));
    try {
        if (static_cast<int>(rates.size()) != N) return out;
        for (int k = 0; k < N; ++k)
            if (rates[k] == 0 || p.balances[k] == 0) return out;
        const std::vector<u> xp = xp_mem(rates, p.balances);
        for (int k = 0; k < N; ++k)
            if (xp[k] == 0) return out;
        const u amp = calc_A(p, ts);
        const u D = get_D(xp, amp);
        if (D == 0) return out;

        u Dr = fdiv(D, u(N) * N * N);                   // D / n^n
        for (int k = 0; k < N; ++k) Dr = fdiv(Dr * D, xp[k]);
        const u xp0_A = fdiv(amp * N * xp[0], A_PRECISION);   // Ann * xp[0]
        const u den = xp0_A + Dr;
        if (den == 0) return out;
        for (int j = 1; j < N; ++j) {
            const u num = xp0_A + fdiv(Dr * xp[0], xp[j]);
            // price_xp * rates[j] / rates[0], all in one floor division
            out[j - 1] = fdiv(E18() * num * rates[j], den * rates[0]);
        }
    } catch (const std::exception&) {
        return std::vector<u>(N - 1, u(0));
    }
    return out;
}

// probe object (engine contract v2 §2). "adm" is the cumulative window admin
// accrual — this vintage has no admin_balances slot. "rates" is echoed so the
// cyToken-unit "spot" can be converted to underlying terms.
inline json make_probe(const Pool& p, const std::vector<u>& rates, const u& ts,
                       int idx) {
    json pr{{"i", idx},
            {"bal", svec(p.balances)},
            {"sup", dec(p.total_supply)},
            {"adm", svec(p.admin_fees)}};
    // cumulative meter as of this event — mirrors result["meter"] exactly
    // (no "cfee_lp"/"cadm_lp": this pool has no LP-denominated fee and mints
    // no LP for the DAO). "cadm" repeats "adm" by construction: the meter's
    // admin leg IS Pool::admin_fees, which only ever grows in this window.
    pr["cfee"] = svec(p.m.fee);
    pr["cadm"] = svec(p.admin_fees);
    pr["cvol"] = svec(p.m.vol);
    if (static_cast<int>(rates.size()) != N) {
        pr["no_rates"] = true;   // no rate-bearing event seen yet
        return pr;
    }
    u D = 0;
    try {
        D = get_D(xp_mem(rates, p.balances), calc_A(p, ts));
    } catch (const std::exception&) {
        D = 0;   // degenerate state: report zeros rather than failing the run
    }
    pr["D"] = dec(D);
    pr["vp"] = dec(p.total_supply == 0 ? u(0) : fdiv(D * E18(), p.total_supply));
    pr["spot"] = svec(spot_cy(p, rates, ts));
    pr["rates"] = svec(rates);
    return pr;
}

// ---- admin events ----------------------------------------------------------
// RampA logs storage units (initial = _A() already *100, future = A*100)

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
    p.fee = parse_u(ev.at("fee"));
    p.admin_fee = parse_u(ev.at("admin_fee"));
    return json{{"applied", true}};
}

// ---- job driver ------------------------------------------------------------

inline json run(const json& job) {
    Pool p;
    if (job.at("kind").get<std::string>() != "stable_lending")
        throw std::runtime_error("stable_lending: wrong kind");
    if (job.at("n").get<int>() != N)
        throw std::runtime_error("stable_lending: N_COINS is 3");

    p.precision_mul = parse_arr(job.at("precision_mul"), N, "precision_mul");

    const json& prm = job.at("params");
    p.initial_A = parse_u(prm.at("initial_A"));
    p.future_A = parse_u(prm.at("future_A"));
    p.initial_A_time = parse_u(prm.at("initial_A_time"));
    p.future_A_time = parse_u(prm.at("future_A_time"));
    p.fee = parse_u(prm.at("fee"));
    p.admin_fee = parse_u(prm.at("admin_fee"));

    const json& jirm = job.at("irm");
    if (!jirm.is_array() || jirm.size() != static_cast<size_t>(N))
        throw std::runtime_error("stable_lending: irm must be per-coin [3]");
    p.irm.resize(N);
    for (int i = 0; i < N; ++i) {
        const json& m = jirm[i];
        Irm& t = p.irm[i];
        t.base_rate_per_block = parse_u(m.at("base_rate_per_block"));
        t.multiplier_per_block = parse_u(m.at("multiplier_per_block"));
        t.jump_multiplier_per_block = parse_u(m.at("jump_multiplier_per_block"));
        t.kink1 = parse_u(m.at("kink1"));
        t.kink2 = parse_u(m.at("kink2"));
        t.roof = parse_u(m.at("roof"));
        t.reserve_factor = parse_u(m.at("reserve_factor"));
        t.borrow_rate_max = m.contains("borrow_rate_max")
                                ? parse_u(m.at("borrow_rate_max"))
                                : u("5000000000000");
        t.initial_exchange_rate = m.contains("initial_exchange_rate")
                                      ? parse_u(m.at("initial_exchange_rate"))
                                      : u(0);
    }

    const json& st = job.at("state");
    p.balances = parse_arr(st.at("balances"), N, "balances");
    p.total_supply = parse_u(st.at("total_supply"));
    p.admin_fees.assign(N, u(0));
    p.m.fee.assign(N, u(0));
    p.m.vol.assign(N, u(0));

    // ---- engine contract v2 job flags (all default OFF) --------------------
    auto truthy = [](const json& v) {
        if (v.is_boolean()) return v.get<bool>();
        if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
        if (v.is_number_integer()) return v.get<long long>() != 0;
        return false;
    };
    auto jbool = [&](const char* k) {
        return job.contains(k) && truthy(job.at(k));
    };
    const bool probe_all = jbool("probe_all");
    const bool probe_last = jbool("probe_last");
    const bool cf_mode = jbool("cf");

    // Optional probe of the VIEW-side _stored_rates path: state.cy holds the
    // cyToken states at state.block; outputs stored_rates + virtual_price for
    // comparison against on-chain get_virtual_price() at that block.
    json initial_probe;
    std::vector<u> cur_rates;   // basis for contract-v2 probes (see header)
    if (st.contains("cy") && st.contains("block")) {
        json fake{{"cy", st.at("cy")}};
        std::vector<Cy> cy0 = load_cy(fake);
        const u bn0 = parse_u(st.at("block"));
        const std::vector<u> sr = stored_rates(p, cy0, bn0);
        const u D = get_D(xp_mem(sr, p.balances),
                          calc_A(p, st.contains("ts") ? parse_u(st.at("ts")) : u(0)));
        initial_probe = json{{"stored_rates", svec(sr)},
                             {"virtual_price", dec(fdiv(D * E18(), p.total_supply))}};
        cur_rates = sr;
    }

    json out_events = json::array();
    json probes = json::array();
    bool any_probe = false;
    long long n_events = 0, n_reverts = 0;

    const json& events = job.at("events");
    const std::size_t n_ev_total = events.size();
    std::size_t ev_idx = 0;

    for (const json& ev : events) {
        const std::string type = ev.at("type").get<std::string>();
        const u ts = ev.contains("ts") ? parse_u(ev.at("ts")) : u(0);
        const u bn = ev.contains("block") ? parse_u(ev.at("block")) : u(0);

        // cf mode: burn amounts become fractions of the LIVE supply.
        // Outside cf mode burn_frac is ignored entirely.
        u burn_frac_abs = 0;
        const bool have_frac = cf_mode && ev.contains("burn_frac");
        if (have_frac)
            burn_frac_abs =
                fdiv(p.total_supply * parse_u(ev.at("burn_frac")), E18());
        const u* burn_ov = have_frac ? &burn_frac_abs : nullptr;

        const Pool snapshot = p;  // restored on revert (cy state is per-event)
        json rec;
        bool reverted = false;
        try {
            json outputs;
            if (type == "exchange") {
                outputs = do_exchange(p, ev, ts, bn);
            } else if (type == "exchange_underlying") {
                outputs = do_exchange_underlying(p, ev, ts, bn);
            } else if (type == "add") {
                outputs = do_add(p, ev, ts, bn);
            } else if (type == "remove") {
                outputs = do_remove(p, ev, burn_ov);
            } else if (type == "remove_one") {
                outputs = do_remove_one(p, ev, ts, bn, burn_ov);
            } else if (type == "remove_imb") {
                // the imbalance path is specified by coin amounts, not by a
                // supply share — burn_frac does not apply (contract §3.1)
                outputs = do_remove_imb(p, ev, ts, bn);
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
            reverted = true;
            rec = json{{"type", type}, {"revert", e.what()}};
        }

        // ---- engine contract v2 bookkeeping (never touches pool state) ----
        ++n_events;
        if (reverted) ++n_reverts;
        // every rate-using op echoes the exchange rates it read; keep the
        // latest as the probe basis (rates are chain state -> not rolled back)
        if (!reverted && rec.at("outputs").contains("ex_rates")) {
            const json& er = rec.at("outputs").at("ex_rates");
            if (er.is_array() && er.size() == static_cast<size_t>(N)) {
                cur_rates.assign(N, u(0));
                for (int i = 0; i < N; ++i)
                    cur_rates[i] = p.precision_mul[i] * parse_u(er[i]);
            }
        }
        bool want_probe =
            probe_all || (probe_last && ev_idx + 1 == n_ev_total) ||
            (ev.contains("probe") && truthy(ev.at("probe")));
        if (want_probe) {
            any_probe = true;
            probes.push_back(
                make_probe(p, cur_rates, ts, static_cast<int>(ev_idx)));
        }
        ++ev_idx;

        out_events.push_back(std::move(rec));
    }

    json result{{"events", std::move(out_events)},
                {"final", json{{"balances", svec(p.balances)},
                               {"total_supply", dec(p.total_supply)},
                               {"admin_fees", svec(p.admin_fees)}}}};
    if (!initial_probe.is_null()) result["initial_probe"] = initial_probe;

    // ---- engine contract v2 result additions -------------------------------
    // no "admin_lp" (no LP is ever minted for the DAO) and no "max_ps_gap_bp"
    // (no price_scale); everything is in cyToken units.
    result["meter"] = json{{"fee", svec(p.m.fee)},
                           {"admin", svec(p.admin_fees)},
                           {"vol", svec(p.m.vol)},
                           {"n_events", n_events},
                           {"n_reverts", n_reverts}};
    if (any_probe) result["probes"] = probes;
    return result;
}

}  // namespace stable_lending_detail

inline nlohmann::json run_stable_lending(const nlohmann::json& job) {
    return stable_lending_detail::run(job);
}
