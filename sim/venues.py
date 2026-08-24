"""Liquidation-venue engines: stableswap (classic), stableswap-NG, cryptoswap N=3.

Ported line-for-line from the wei-exact C++ engine in
    claude-project-folder/global-sim-ui/engine-cpp/src/engine.cpp      (stableswap)
    claude-project-folder/global-sim-ui/engine-cpp/src/crypto_math.hpp (tricrypto)
which was validated 484/484 wei-for-wei against on-chain get_dy across every
deployed Curve pool family at block 25,638,261 (kinds: ng, classic_v1/v2,
crypto2_*, crypto3). The cryptoswap N=2 venue stays `cryptoswap_2coin.py`
(unchanged — it anchors the TS-parity regression).

All venues share one EXTERNAL coin convention:
    0 = crvUSD (borrowed asset)      1 = collateral (CRV)
regardless of internal layout (the 3-coin venue maps 1 -> internal index 2).

Interface used by synth_bad_debt.py:
    get_dy(i, j, dx)  -> dy after fees (quote only, no state change)
    exec(i, j, dx)    -> dy; applies the trade (balances update + D recompute)
    pair_balances     -> [crvUSD_balance, collateral_balance] (native units)
    clone()           -> deep copy for hypothetical trades

Integer-division semantics: C++ cpp_int and Vyper both truncate toward zero.
Python's // floors, which differs on negatives — tri_get_y works with signed
cubic coefficients, so every division that can see a negative operand goes
through idiv() below. Positive-only code paths use // directly.
"""
from __future__ import annotations
import copy
from math import isqrt

PRECISION = 10 ** 18
FEE_DENOM = 10 ** 10
E18 = PRECISION


def idiv(a: int, b: int) -> int:
    """C++/Vyper signed integer division: truncate toward zero."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


# =============================================================================
# StableSwap core — engine.cpp get_D_v1 / get_D_prec / get_y / dynamic_fee
# =============================================================================

def ss_get_D_v1(xp: list[int], amp: int, n: int) -> int:
    """3pool-era D: Ann = amp*N, A_PRECISION = 1."""
    S = sum(xp)
    if S == 0:
        return 0
    D, Ann = S, amp * n
    for _ in range(255):
        D_P = D
        for x in xp:
            D_P = D_P * D // (x * n)
        Dprev = D
        D = (Ann * S + D_P * n) * D // ((Ann - 1) * D + (n + 1) * D_P)
        if abs(D - Dprev) <= 1:
            return D
    raise RuntimeError("ss D no conv")


def ss_get_D_ng(xp: list[int], amp: int, n: int) -> int:
    """NG D: amp carries A_PRECISION=100; D_P divided by n**n AFTER the loop."""
    S = sum(xp)
    if S == 0:
        return 0
    D, Ann = S, amp * n
    for _ in range(255):
        D_P = D
        for x in xp:
            D_P = D_P * D // x
        D_P //= n ** n
        Dprev = D
        D = (Ann * S // 100 + D_P * n) * D // ((Ann - 100) * D // 100 + (n + 1) * D_P)
        if abs(D - Dprev) <= 1:
            return D
    raise RuntimeError("ss D no conv")


def ss_get_y(i: int, j: int, x: int, xp: list[int], amp: int, D: int,
             n: int, a_prec: int) -> int:
    """Solve for xp[j] given xp[i] -> x. a_prec: 1 (classic v1) or 100 (ng)."""
    Ann = amp * n
    c, S_ = D, 0
    for k in range(n):
        if k == i:
            xk = x
        elif k != j:
            xk = xp[k]
        else:
            continue
        S_ += xk
        c = c * D // (xk * n)
    c = c * D * a_prec // (Ann * n)
    b = S_ + D * a_prec // Ann
    y = D
    for _ in range(255):
        y_prev = y
        y = (y * y + c) // (2 * y + b - D)
        if abs(y - y_prev) <= 1:
            return y
    raise RuntimeError("ss y no conv")


def ss_dynamic_fee(xpi: int, xpj: int, fee: int, opm: int) -> int:
    """NG midpoint dynamic fee (offpeg multiplier `opm`, 1e10 base)."""
    if opm <= FEE_DENOM:
        return fee
    xps2 = (xpi + xpj) * (xpi + xpj)
    return (opm * fee) // ((opm - FEE_DENOM) * 4 * xpi * xpj // xps2 + FEE_DENOM)


# =============================================================================
# Tricrypto core — crypto_math.hpp tri_* (cbrt, newton_D, get_y, fee)
# =============================================================================

_CBRT_T1 = 115792089237316195423570985008687907853269   # 2^256 / 1e36


def cbrt_u(x: int) -> int:
    """snekmate cbrt, positive input (callers handle sign)."""
    if x >= _CBRT_T1 * E18:
        xx, scale = x, 0
    elif x >= _CBRT_T1:
        xx, scale = x * E18, 1
    else:
        xx, scale = x * E18 * E18, 2
    log2x = xx.bit_length() - 1          # snek_log2 round-down
    rem = log2x % 3
    a = (1 << (log2x // 3)) * 1260 ** rem // 1000 ** rem
    for _ in range(7):
        a = (2 * a + xx // (a * a)) // 3
    if scale == 0:
        a *= 10 ** 12
    elif scale == 1:
        a *= 10 ** 6
    return a


def _tri_sort_desc(x: list[int]) -> list[int]:
    return sorted(x, reverse=True)


def tri_geometric_mean(x: list[int]) -> int:
    prod = x[0] * x[1] // E18 * x[2] // E18
    return cbrt_u(prod) if prod else 0


def tri_newton_D(ANN: int, gamma: int, x_unsorted: list[int], K0_prev: int = 0) -> int:
    x = _tri_sort_desc(x_unsorted)
    if x[0] <= 0:
        raise ValueError("empty pool")
    S = x[0] + x[1] + x[2]
    if K0_prev == 0:
        D = 3 * tri_geometric_mean(x)
    else:
        if S > 10 ** 36:
            D = cbrt_u(x[0] * x[1] // 10 ** 36 * x[2] // K0_prev * 27 * 10 ** 12)
        elif S > 10 ** 24:
            D = cbrt_u(x[0] * x[1] // 10 ** 24 * x[2] // K0_prev * 27 * 10 ** 6)
        else:
            D = cbrt_u(x[0] * x[1] // E18 * x[2] // K0_prev * 27)
    for _ in range(255):
        D_prev = D
        K0 = E18 * x[0] * 3 // D * x[1] * 3 // D * x[2] * 3 // D
        g1k0 = gamma + E18
        g1k0 = (g1k0 - K0 + 1) if g1k0 > K0 else (K0 - g1k0 + 1)
        mul1 = E18 * D // gamma * g1k0 // gamma * g1k0 * 10000 // ANN
        mul2 = (2 * E18 * 3) * K0 // g1k0
        neg_fprime = (S + S * mul2 // E18) + mul1 * 3 // K0 - mul2 * D // E18
        D_plus = D * (neg_fprime + S) // neg_fprime
        D_minus = D * D // neg_fprime
        if E18 > K0:
            D_minus += D * (mul1 // neg_fprime) // E18 * (E18 - K0) // K0
        else:
            D_minus -= D * (mul1 // neg_fprime) // E18 * (K0 - E18) // K0
        D = (D_plus - D_minus) if D_plus > D_minus else (D_minus - D_plus) // 2
        diff = abs(D - D_prev)
        lim = max(D, 10 ** 16)
        if diff * 10 ** 14 < lim:
            for q in range(3):
                frac = x[q] * E18 // D
                if not (10 ** 16 - 1 <= frac < 10 ** 20 + 1):
                    raise ValueError("unsafe frac")
            return D
    raise RuntimeError("tri newton_D no conv")


def tri_newton_y(ANN: int, gamma: int, x: list[int], D: int, i: int,
                 a_mult: int = 10000) -> int:
    for k in range(3):
        if k == i:
            continue
        frac = x[k] * E18 // D
        if not (10 ** 16 - 1 < frac < 10 ** 20 + 1):
            raise ValueError("unsafe x[k]")
    y = D // 3
    K0_i = E18
    S_i = 0
    xs = list(x)
    xs[i] = 0
    xs = _tri_sort_desc(xs)
    convergence_limit = max(xs[0] // 10 ** 14, D // 10 ** 14, 100)
    for j in range(2, 4):
        _x = xs[3 - j]                   # small x first
        y = y * D // (_x * 3)
        S_i += _x
    for j in range(2):
        K0_i = K0_i * xs[j] * 3 // D     # large x first
    for _ in range(255):
        y_prev = y
        K0 = K0_i * y * 3 // D
        S = S_i + y
        g1k0 = gamma + E18
        g1k0 = (g1k0 - K0 + 1) if g1k0 > K0 else (K0 - g1k0 + 1)
        mul1 = E18 * D // gamma * g1k0 // gamma * g1k0 * a_mult // ANN
        mul2 = E18 + (2 * E18) * K0 // g1k0
        yfprime = E18 * y + S * mul2 + mul1
        dyfprime = D * mul2
        if yfprime < dyfprime:
            y = y_prev // 2
            continue
        yfprime -= dyfprime
        fprime = yfprime // y
        y_minus = mul1 // fprime
        y_plus = (yfprime + E18 * D) // fprime + y_minus * E18 // K0
        y_minus += E18 * S // fprime
        y = y_prev // 2 if y_plus < y_minus else y_plus - y_minus
        diff = abs(y - y_prev)
        if diff < max(convergence_limit, y // 10 ** 14):
            frac = y * E18 // D
            if not (10 ** 16 - 1 < frac < 10 ** 20 + 1):
                raise ValueError("unsafe y")
            return y
    raise RuntimeError("tri newton_y no conv")


def tri_get_y(ANN: int, gamma: int, x: list[int], D: int, i: int) -> int:
    """Analytic cubic-root get_y (tricrypto-ng views flow), newton fallback.
    Signed arithmetic -> every division below uses idiv (truncation)."""
    if not (10 ** 17 - 1 < D < 10 ** 15 * E18 + 1):
        raise ValueError("unsafe D")
    for k in range(3):
        if k == i:
            continue
        frac = x[k] * E18 // D
        if not (10 ** 16 - 1 < frac < 10 ** 20 + 1):
            raise ValueError("unsafe x[k]")
    j, k = ((1, 2) if i == 0 else (0, 2) if i == 1 else (0, 1))
    x_j, x_k = x[j], x[k]
    gamma2 = gamma * gamma

    a = 10 ** 36 // 27
    b = (10 ** 36 // 9 + 2 * E18 * gamma // 27
         - D * D // x_j * gamma2 * ANN // 729 // 10000 // x_k)
    c = (10 ** 36 // 9 + gamma * (gamma + 4 * E18) // 27
         + idiv(idiv(idiv(gamma2 * (x_j + x_k - D), D) * ANN, 27), 10000))
    d = (E18 + gamma) * (E18 + gamma) // 27

    d0 = abs(idiv(3 * a * c, b) - b)
    divider = 1
    for thresh, dv in ((10 ** 48, 10 ** 30), (10 ** 44, 10 ** 26), (10 ** 40, 10 ** 22),
                       (10 ** 36, 10 ** 18), (10 ** 32, 10 ** 14), (10 ** 28, 10 ** 10),
                       (10 ** 24, 10 ** 6), (10 ** 20, 10 ** 2)):
        if d0 > thresh:
            divider = dv
            break

    if abs(a) > abs(b):
        additional_prec = abs(idiv(a, b))
        a = idiv(a * additional_prec, divider)
        b = idiv(b * additional_prec, divider)
        c = idiv(c * additional_prec, divider)
        d = idiv(d * additional_prec, divider)
    else:
        additional_prec = abs(idiv(b, a))
        a = idiv(idiv(a, additional_prec), divider)
        b = idiv(idiv(b, additional_prec), divider)
        c = idiv(idiv(c, additional_prec), divider)
        d = idiv(idiv(d, additional_prec), divider)

    _3ac = 3 * a * c
    delta0 = idiv(_3ac, b) - b
    delta1 = idiv(3 * _3ac, b) - 2 * b - idiv(idiv(27 * a * a, b) * d, b)
    sqrt_arg = delta1 * delta1 + idiv(4 * delta0 * delta0, b) * delta0
    if sqrt_arg <= 0:
        return tri_newton_y(ANN, gamma, x, D, i)
    sqrt_val = isqrt(sqrt_arg)

    b_cbrt = cbrt_u(b) if b >= 0 else -cbrt_u(-b)
    if delta1 > 0:
        second_cbrt = cbrt_u((delta1 + sqrt_val) // 2)
    else:
        second_cbrt = -cbrt_u((sqrt_val - delta1) // 2)
    C1 = idiv(idiv(b_cbrt * b_cbrt, E18) * second_cbrt, E18)
    root_K0 = idiv(b + idiv(b * delta0, C1) - C1, 3)
    root = idiv(idiv(idiv(idiv(D * D, 27), x_k) * D, x_j) * root_K0, a)

    frac = root * E18 // D
    if not (10 ** 16 - 1 <= frac < 10 ** 20 + 1):
        raise ValueError("unsafe y")
    return root


def tri_fee(xp: list[int], mid_fee: int, out_fee: int, fee_gamma: int) -> int:
    S = xp[0] + xp[1] + xp[2]
    K = E18 * 3 * xp[0] // S
    K = K * 3 * xp[1] // S
    K = K * 3 * xp[2] // S
    if fee_gamma > 0:
        K = fee_gamma * E18 // (fee_gamma + E18 - K)
    return (mid_fee * K + out_fee * (E18 - K)) // E18


# =============================================================================
# Venue classes
# =============================================================================

class StableswapVenue:
    """Pair venue on the StableSwap invariant. `ng=True` -> NG semantics
    (A_PRECISION=100, offpeg dynamic fee in xp units); `ng=False` -> classic
    3pool-era semantics (A raw, static fee applied after conversion).

    The collateral price is anchored via rates (as NG does for rate-oracle
    assets): rate[1] = price, so the balanced pool quotes exactly `price` and
    the curve is flat around it — the honest depth profile of a pegged venue.
    `A_ui` is the on-chain A() convention: RAW, no N^N x 10,000 multiplier.
    """

    def __init__(self, tvl_usd_1e18: int, price_1_per_0_1e18: int, A_ui: float,
                 ng: bool, fee: int, offpeg: int):
        self.ng = ng
        self.amp = int(round(A_ui * 100)) if ng else int(round(A_ui))
        self.fee = fee
        self.offpeg = offpeg
        self.rates = [PRECISION, price_1_per_0_1e18]
        self.balances = [
            tvl_usd_1e18 // 2,
            tvl_usd_1e18 * PRECISION // (2 * price_1_per_0_1e18),
        ]

    # -- interface ------------------------------------------------------------
    @property
    def pair_balances(self) -> list[int]:
        return self.balances

    def clone(self) -> "StableswapVenue":
        c = copy.copy(self)
        c.balances = list(self.balances)
        return c

    def _xp(self) -> list[int]:
        return [r * b // PRECISION for r, b in zip(self.rates, self.balances)]

    def get_dy(self, i: int, j: int, dx: int) -> int:
        assert i != j and i in (0, 1) and j in (0, 1) and dx > 0
        xp = self._xp()
        x = xp[i] + dx * self.rates[i] // PRECISION
        if self.ng:
            D = ss_get_D_ng(xp, self.amp, 2)
            y = ss_get_y(i, j, x, xp, self.amp, D, 2, 100)
            dy = xp[j] - y - 1
            f = ss_dynamic_fee((xp[i] + x) // 2, (xp[j] + y) // 2,
                               self.fee, self.offpeg) * dy // FEE_DENOM
            return (dy - f) * PRECISION // self.rates[j]
        D = ss_get_D_v1(xp, self.amp, 2)
        y = ss_get_y(i, j, x, xp, self.amp, D, 2, 1)
        dy = (xp[j] - y - 1) * PRECISION // self.rates[j]
        return dy - self.fee * dy // FEE_DENOM

    def exec(self, i: int, j: int, dx: int) -> int:
        dy = self.get_dy(i, j, dx)
        if dy >= self.balances[j]:
            raise ValueError("venue side depleted")
        self.balances[i] += dx
        self.balances[j] -= dy
        return dy


class Cryptoswap3Venue:
    """Tricrypto-ng venue. External pair (0=crvUSD, 1=collateral) maps to
    internal coins (0, 2); internal coin 1 is a passive third asset seeded
    balanced (price_scale 1.0, its literal price is irrelevant — only its xp
    share matters). TVL input stays PAIR-ONLY, so full pool TVL = 1.5x pair,
    each coin one third — the same convention the 2-coin venue's pair-only
    calibration established.

    `A_raw` is the tricrypto raw A; on-chain A() = A_raw x 27 x 10,000.
    """

    _MAP = {0: 0, 1: 2}

    def __init__(self, tvl_usd_1e18: int, price_1_per_0_1e18: int,
                 A_raw: float = 10.0, gamma: int = 1_300_000_000_000,
                 mid_fee: int = 3_000_000, out_fee: int = 80_000_000,
                 fee_gamma: int = 350_000_000_000_000):
        self.ANN = int(round(A_raw * 27 * 10000))
        self.gamma = gamma
        self.mid_fee, self.out_fee, self.fee_gamma = mid_fee, out_fee, fee_gamma
        # price_scale[k] scales internal coin k+1; passive coin at 1.0
        self.price_scale = [PRECISION, price_1_per_0_1e18]
        half = tvl_usd_1e18 // 2
        self.balances = [
            half,                                            # crvUSD
            half,                                            # passive @ ps 1.0
            tvl_usd_1e18 * PRECISION // (2 * price_1_per_0_1e18),  # collateral
        ]
        self.D = tri_newton_D(self.ANN, self.gamma, self._xr())

    def _xr(self) -> list[int]:
        return [self.balances[0],
                self.balances[1] * self.price_scale[0] // E18,
                self.balances[2] * self.price_scale[1] // E18]

    # -- interface ------------------------------------------------------------
    @property
    def pair_balances(self) -> list[int]:
        return [self.balances[0], self.balances[2]]

    def clone(self) -> "Cryptoswap3Venue":
        c = copy.copy(self)
        c.balances = list(self.balances)
        c.price_scale = list(self.price_scale)
        return c

    def get_dy(self, i: int, j: int, dx: int) -> int:
        assert i != j and i in (0, 1) and j in (0, 1) and dx > 0
        ii, jj = self._MAP[i], self._MAP[j]
        # engine.cpp get_dy_crypto (n==3): scale, get_y, -1, unscale, fee last
        xp = list(self.balances)
        xp[ii] += dx
        for k in range(2):
            xp[k + 1] = xp[k + 1] * self.price_scale[k] // E18
        y = tri_get_y(self.ANN, self.gamma, xp, self.D, jj)
        dy = xp[jj] - y - 1
        xp[jj] = y
        if jj > 0:
            dy = dy * E18 // self.price_scale[jj - 1]
        fee = tri_fee(xp, self.mid_fee, self.out_fee, self.fee_gamma) * dy // FEE_DENOM
        return dy - fee

    def exec(self, i: int, j: int, dx: int) -> int:
        dy = self.get_dy(i, j, dx)
        ii, jj = self._MAP[i], self._MAP[j]
        if dy >= self.balances[jj]:
            raise ValueError("venue side depleted")
        self.balances[ii] += dx
        self.balances[jj] -= dy
        # Same convention as the 2-coin venue: the invariant re-tunes to the
        # drained balances after each executed trade.
        self.D = tri_newton_D(self.ANN, self.gamma, self._xr())
        return dy


class Cryptoswap2Venue:
    """Interface wrapper around the existing (regression-anchored)
    Cryptoswap2Coin — its math is untouched; exec() reproduces exactly what
    synth_bad_debt's apply_swap_to_pool / push_pool_to_spot persist-branch did:
    balance update then a fresh newton_D."""

    def __init__(self, inner):
        self.inner = inner

    @property
    def pair_balances(self) -> list[int]:
        return self.inner.balances

    @property
    def balances(self) -> list[int]:          # legacy accessor (row logging)
        return self.inner.balances

    def clone(self) -> "Cryptoswap2Venue":
        from cryptoswap_2coin import Cryptoswap2Coin  # noqa: F401 (type only)
        c = copy.copy(self)
        c.inner = copy.copy(self.inner)
        c.inner.balances = list(self.inner.balances)
        return c

    def get_dy(self, i: int, j: int, dx: int) -> int:
        return self.inner.get_dy(i, j, dx)

    def exec(self, i: int, j: int, dx: int) -> int:
        from cryptoswap_2coin import newton_D
        dy = self.inner.get_dy(i, j, dx)
        self.inner.balances[i] += dx
        self.inner.balances[j] -= dy
        self.inner.D = newton_D(self.inner.A_gamma_ann, self.inner.gamma,
                                self.inner._xp(self.inner.balances))
        return dy


# =============================================================================
# Factory
# =============================================================================

# Real-pool parameter defaults for the stableswap venues (fetched on-chain,
# see verify_venues.py): crvUSD/USDT NG pool fee/offpeg; 3pool fee for classic.
NG_DEFAULT_FEE = 1_000_000            # 0.01%   (crvUSD/USDT)
NG_DEFAULT_OFFPEG = 50_000_000_000    # 5x      (crvUSD/USDT)
CLASSIC_DEFAULT_FEE = 1_000_000       # 0.01%   (3pool)


def _i(x) -> int:
    """Coerce a state field to int. Values arrive as strings (see
    fetch_markets: JS cannot hold them as numbers), but tolerate ints and even
    floats so a stale markets.json cannot crash the engine with an
    unparseable "6.4e+22"."""
    if isinstance(x, str):
        return int(float(x)) if ("e" in x or "E" in x or "." in x) else int(x)
    return int(x)


def _xp_common(st: dict) -> list[int]:
    """Real pool balances lifted into its own common unit (stored_rates for NG,
    precision multipliers otherwise)."""
    rates = st.get("rates") or []
    out = []
    for k, (b, d) in enumerate(zip(st["balances"], st["decimals"])):
        b = _i(b)
        if k < len(rates) and rates[k]:
            out.append(b * _i(rates[k]) // PRECISION)
        else:
            out.append(b * 10 ** (18 - d))
    return out


def venue_from_state(st: dict, coll_usd_1e18: int):
    """Rebuild a venue from the pool's LIVE on-chain state instead of assuming a
    balanced pool of the reported pair TVL.

    Everything is expressed in DOLLAR space: external coin 0 is the borrowed
    asset carried as dollars, coin 1 is the collateral carried as tokens with
    rate/price_scale = its USD price. That keeps the sim's units while
    preserving the real pool's imbalance, which is the whole point — a balanced
    reconstruction quoted a $2M sreUSD sale at -0.28% where the real, lopsided
    pool charges -18.84%.

    Returns None when the state cannot be mapped (pairs only), and the caller
    falls back to make_venue().
    """
    if not st:
        return None
    if st.get("n") == 3 and st["kind"] == "cryptoswap":
        return _tri_from_state(st, coll_usd_1e18)
    if st.get("n") != 2:
        return None
    ib, iq = st["i_base"], st["i_quote"]
    dec = st["decimals"]
    q_usd = (st.get("usd") or [None, None])[iq]
    if not q_usd or coll_usd_1e18 <= 0:
        return None
    q_usd_1e18 = int(q_usd * PRECISION)
    bal_q_tok = _i(st["balances"][iq]) * 10 ** (18 - dec[iq])   # 1e18 tokens
    bal_b_tok = _i(st["balances"][ib]) * 10 ** (18 - dec[ib])
    if bal_q_tok <= 0 or bal_b_tok <= 0:
        return None

    if st["kind"].startswith("stableswap"):
        ng = st["kind"] == "stableswap-ng"
        v = StableswapVenue.__new__(StableswapVenue)
        v.ng = ng
        v.amp = _i(st["A"]) * 100 if ng else _i(st["A"])
        v.fee = _i(st.get("fee") or (NG_DEFAULT_FEE if ng else CLASSIC_DEFAULT_FEE))
        v.offpeg = _i(st.get("offpeg") or 0)
        # coin 0 already dollars -> rate 1; coin 1 priced at the oracle, so the
        # venue's marginal at these balances is the market price by construction.
        v.rates = [PRECISION, coll_usd_1e18]
        v.balances = [bal_q_tok * q_usd_1e18 // PRECISION, bal_b_tok]
        return v

    # cryptoswap pair. price_scale expresses the pool's coin1 in its coin0;
    # rescaled into dollar space so coin 0 stays the borrowed asset in dollars.
    ps = _i((st.get("price_scale") or [0])[0])
    if not ps:
        return None
    ps_ext = ps if ib == 1 else PRECISION * PRECISION // ps   # base in quote
    bal = [bal_q_tok * q_usd_1e18 // PRECISION, bal_b_tok]
    scale = ps_ext * q_usd_1e18 // PRECISION
    math_kind = st.get("c2_math")
    if not math_kind:
        return None            # family unknown -> balanced approximation
    return Crypto2StateVenue(bal, scale, _i(st["A"]), _i(st["gamma"]),
                             _i(st["mid_fee"]), _i(st["out_fee"]),
                             _i(st["fee_gamma"]), math=math_kind)


def make_venue(pool_type: str, n_coins: int, tvl_usd_1e18: int,
               price_1_per_0_1e18: int, A_ui: float):
    """A_ui semantics per type:
    cryptoswap  -> RAW A for that N (on-chain A() = A_raw x N^N x 10,000)
    stableswap / stableswap-ng -> on-chain A() directly (raw, no multiplier)
    """
    if pool_type == "cryptoswap":
        if n_coins == 2:
            from cryptoswap_2coin import Cryptoswap2Coin
            return Cryptoswap2Venue(Cryptoswap2Coin(
                tvl_usd_1e18, price_1_per_0_1e18, A_raw=A_ui))
        if n_coins == 3:
            return Cryptoswap3Venue(tvl_usd_1e18, price_1_per_0_1e18, A_raw=A_ui)
        raise ValueError("Curve cryptoswap pools exist with 2 or 3 coins only")
    if pool_type == "stableswap-ng":
        return StableswapVenue(tvl_usd_1e18, price_1_per_0_1e18, A_ui,
                               ng=True, fee=NG_DEFAULT_FEE, offpeg=NG_DEFAULT_OFFPEG)
    if pool_type == "stableswap":
        return StableswapVenue(tvl_usd_1e18, price_1_per_0_1e18, A_ui,
                               ng=False, fee=CLASSIC_DEFAULT_FEE, offpeg=0)
    raise ValueError(f"unknown pool_type {pool_type!r}")


# =============================================================================
# 2-coin cryptoswap families — ported from the wei-exact engine in
#   claude-project-folder/global-sim-ui/engine-cpp/src/crypto_math.hpp
# (validated 484/484 vs on-chain get_dy at block 25,638,261).
#
# There is more than one "twocrypto" pool. The Yield Basis pools present a
# twocrypto interface but run StableswapMath inside (kind crypto2_ss there),
# which is why solving them with the twocrypto invariant put the WETH venue's
# marginal at $1,133 against a $1,862 oracle. The family is detected per pool by
# whichever invariant reproduces the pool's own stored D().
# =============================================================================

def two_newton_D(ANN: int, gamma: int, x_unsorted: list[int], K0_prev: int = 0) -> int:
    x = sorted(x_unsorted, reverse=True)
    if not (10 ** 9 - 1 < x[0] < 10 ** 15 * E18 + 1):
        raise ValueError("unsafe x0")
    if x[1] * E18 // x[0] <= 10 ** 14 - 1:
        raise ValueError("unsafe ratio")
    S = x[0] + x[1]
    if K0_prev == 0:
        D = 2 * isqrt(x[0] * x[1])
    else:
        D = isqrt(4 * x[0] * x[1] // K0_prev * E18)
        D = min(D, S)
    g1k0_base = gamma + E18
    for _ in range(255):
        D_prev = D
        if D <= 0:
            raise ValueError("D=0")
        K0 = (E18 * 4) * x[0] // D * x[1] // D
        g1k0 = g1k0_base
        g1k0 = (g1k0 - K0 + 1) if g1k0 > K0 else (K0 - g1k0 + 1)
        mul1 = E18 * D // gamma * g1k0 // gamma * g1k0 * 10000 // ANN
        mul2 = (2 * E18 * 2) * K0 // g1k0
        neg_fprime = (S + S * mul2 // E18) + mul1 * 2 // K0 - mul2 * D // E18
        D_plus = D * (neg_fprime + S) // neg_fprime
        D_minus = D * D // neg_fprime
        if E18 > K0:
            D_minus += D * (mul1 // neg_fprime) // E18 * (E18 - K0) // K0
        else:
            D_minus -= D * (mul1 // neg_fprime) // E18 * (K0 - E18) // K0
        D = (D_plus - D_minus) if D_plus > D_minus else (D_minus - D_plus) // 2
        if abs(D - D_prev) * 10 ** 14 < max(D, 10 ** 16):
            for k in range(2):
                frac = x[k] * E18 // D
                if not (10 ** 16 // 2 - 1 < frac < 10 ** 20 // 2 + 1):
                    raise ValueError("unsafe x frac")
            return D
    raise RuntimeError("two newton_D no conv")


def two_newton_y(ANN: int, gamma: int, x: list[int], D: int, i: int,
                 lim_mul: int) -> int:
    x_j = x[1 - i]
    y = D * D // (x_j * 4)
    K0_i = (E18 * 2) * x_j // D
    if not (E18 * E18 // lim_mul <= K0_i <= lim_mul):
        raise ValueError("unsafe x[i]")
    conv = max(x_j // 10 ** 14, D // 10 ** 14, 100)
    for _ in range(255):
        y_prev = y
        K0 = K0_i * y * 2 // D
        S = x_j + y
        g1k0 = gamma + E18
        g1k0 = (g1k0 - K0 + 1) if g1k0 > K0 else (K0 - g1k0 + 1)
        mul1 = E18 * D // gamma * g1k0 // gamma * g1k0 * 10000 // ANN
        mul2 = E18 + (2 * E18) * K0 // g1k0
        yfprime = E18 * y + S * mul2 + mul1
        dyfprime = D * mul2
        if yfprime < dyfprime:
            y = y_prev // 2
            continue
        yfprime -= dyfprime
        fprime = yfprime // y
        y_minus = mul1 // fprime
        y_plus = (yfprime + E18 * D) // fprime + y_minus * E18 // K0
        y_minus += E18 * S // fprime
        y = y_prev // 2 if y_plus < y_minus else y_plus - y_minus
        if abs(y - y_prev) < max(conv, y // 10 ** 14):
            return y
    raise RuntimeError("two newton_y no conv")


# MAX_GAMMA_SMALL is 0 for Math v2.0.0 (fixed asserts), 2e16 for v2.1.0.
def two_get_y(ANN: int, gamma: int, x: list[int], D: int, i: int,
              max_gamma_small: int = 2 * 10 ** 16) -> int:
    if not (10 ** 17 - 1 < D < 10 ** 15 * E18 + 1):
        raise ValueError("unsafe D")
    lim_mul = 100 * E18
    if max_gamma_small and gamma > max_gamma_small:
        lim_mul = lim_mul * max_gamma_small // gamma
    x_j = x[1 - i]
    gamma2 = gamma * gamma
    K0_i = E18 * 2 * x_j // D
    if max_gamma_small:
        if not (E18 * E18 // lim_mul <= K0_i <= lim_mul):
            raise ValueError("unsafe x[i]")
    elif not (10 ** 16 * 2 - 1 < K0_i < 10 ** 20 * 2 + 1):
        raise ValueError("unsafe x[i]")

    ag2 = ANN * gamma2
    a = 10 ** 32
    b = idiv(D * ag2 // 400000000, x_j) - 10 ** 32 * 3 - 2 * gamma * 10 ** 14
    c = (10 ** 32 * 3 + 4 * gamma * 10 ** 14 + gamma2 // 10000
         + idiv((4 * ag2 // 400000000) * x_j, D) - 4 * ag2 // 400000000)
    d = -((E18 + gamma) * (E18 + gamma)) // 10000
    delta0 = idiv(3 * a * c, b) - b
    delta1 = 3 * delta0 + b - idiv(idiv(27 * a * a, b) * d, b)
    thr = min(abs(delta0), abs(delta1), a)
    divider = 1
    for e, dv in ((48, 30), (46, 28), (44, 26), (42, 24), (40, 22), (38, 20),
                  (36, 18), (34, 16), (32, 14), (30, 12), (28, 10), (26, 8),
                  (24, 6), (20, 2)):
        if thr > 10 ** e:
            divider = 10 ** dv
            break
    a = idiv(a, divider); b = idiv(b, divider)
    c = idiv(c, divider); d = idiv(d, divider)
    delta0 = idiv(3 * a * c, b) - b
    delta1 = 3 * delta0 + b - idiv(idiv(27 * a * a, b) * d, b)
    sqrt_arg = delta1 * delta1 + idiv(4 * delta0 * delta0, b) * delta0
    if sqrt_arg <= 0:
        return two_newton_y(ANN, gamma, x, D, i, lim_mul)
    sqrt_val = isqrt(sqrt_arg)
    b_cbrt = cbrt_u(b) if b > 0 else -cbrt_u(-b)
    second = cbrt_u((delta1 + sqrt_val) // 2) if delta1 > 0 \
        else -cbrt_u((sqrt_val - delta1) // 2)
    C1 = idiv(idiv(b_cbrt * b_cbrt, E18) * second, E18)
    root = idiv(E18 * C1 - E18 * b - idiv(E18 * b, C1) * delta0, 3 * a)
    y = idiv(idiv(idiv(D * D, x_j) * root, 4), E18)
    frac = y * E18 // D
    if max_gamma_small:
        if not ((E18 * E18 // 2) // lim_mul <= frac <= lim_mul // 2):
            raise ValueError("unsafe y")
    elif not (10 ** 16 - 1 <= frac < 10 ** 20 + 1):
        raise ValueError("unsafe y")
    return y


def ss2_get_D(amp: int, xp: list[int]) -> int:
    """StableswapMath D inside a twocrypto shell (Yield Basis). `amp` already
    carries A_MULTIPLIER (10,000); Ann = amp * 2."""
    S = xp[0] + xp[1]
    if S == 0:
        return 0
    D, Ann = S, amp * 2
    for _ in range(255):
        D_P = D
        for x in xp:
            D_P = D_P * D // x
        D_P //= 4
        Dprev = D
        D = (Ann * S // 10000 + D_P * 2) * D // \
            ((Ann - 10000) * D // 10000 + 3 * D_P)
        if abs(D - Dprev) <= 1:
            return D
    raise RuntimeError("ss2 D no conv")


def ss2_get_y(amp: int, xp: list[int], D: int, i: int) -> int:
    S_, c, Ann = 0, D, amp * 2
    for k in range(2):
        if k == i:
            continue
        S_ += xp[k]
        c = c * D // (xp[k] * 2)
    c = c * D * 10000 // (Ann * 2)
    b = S_ + D * 10000 // Ann
    y = D
    for _ in range(255):
        y_prev = y
        y = (y * y + c) // (2 * y + b - D)
        if abs(y - y_prev) <= 1:
            return y
    raise RuntimeError("ss2 y no conv")


class Crypto2StateVenue:
    """2-coin cryptoswap venue seeded from a real pool. External coin 0 is the
    borrowed asset carried as DOLLARS, coin 1 the collateral as tokens, so
    price_scale is the collateral's dollar price.

    `math` picks the invariant: "two" (twocrypto-ng Math v2.x), "ss"
    (StableswapMath shell — the Yield Basis pools) or "old2" (legacy
    factory-crypto). detect_c2_math() chooses it by reproducing the pool's D().
    """

    def __init__(self, balances, price_scale, ANN, gamma, mid_fee, out_fee,
                 fee_gamma, math="two", D=None):
        self.balances = list(balances)
        self.price_scale = price_scale
        self.ANN, self.gamma = ANN, gamma
        self.mid_fee, self.out_fee, self.fee_gamma = mid_fee, out_fee, fee_gamma
        self.math = math
        self.D = D if D is not None else self._solve_D(self._xp())

    def _xp(self, bal=None):
        b = bal if bal is not None else self.balances
        return [b[0], b[1] * self.price_scale // E18]

    def _solve_D(self, xp):
        if self.math == "ss":
            return ss2_get_D(self.ANN, xp)
        return two_newton_D(self.ANN, self.gamma, xp)

    def _solve_y(self, xp, i):
        if self.math == "ss":
            return ss2_get_y(self.ANN, xp, self.D, i)
        return two_get_y(self.ANN, self.gamma, xp, self.D, i,
                         0 if self.math == "old2" else 2 * 10 ** 16)

    def _fee(self, xp):
        S = xp[0] + xp[1]
        if S == 0:
            return self.mid_fee
        K = E18 * 2 * xp[0] // S * 2 * xp[1] // S
        if self.fee_gamma > 0:
            K = self.fee_gamma * E18 // (self.fee_gamma + E18 - K)
        return (self.mid_fee * K + self.out_fee * (E18 - K)) // E18

    # -- interface -----------------------------------------------------------
    @property
    def pair_balances(self):
        return self.balances

    def clone(self):
        c = copy.copy(self)
        c.balances = list(self.balances)
        return c

    def get_dy(self, i: int, j: int, dx: int) -> int:
        assert i != j and i in (0, 1) and j in (0, 1) and dx > 0
        bal = list(self.balances)
        bal[i] += dx
        xp = self._xp(bal)
        y = self._solve_y(xp, j)
        dy = xp[j] - y - 1
        xp[j] = y
        if j == 1:
            dy = dy * E18 // self.price_scale
        return dy - self._fee(xp) * dy // FEE_DENOM

    def exec(self, i: int, j: int, dx: int) -> int:
        dy = self.get_dy(i, j, dx)
        if dy >= self.balances[j]:
            raise ValueError("venue side depleted")
        self.balances[i] += dx
        self.balances[j] -= dy
        self.D = self._solve_D(self._xp())
        return dy


def detect_c2_math(xp: list[int], D_onchain: int, ANN: int, gamma: int):
    """Which 2-coin invariant does this pool actually run? Returns (math, err)
    for the candidate whose D at the pool's own balances best matches its
    stored D(); None when nothing lands within 0.5%."""
    best = None
    for name in ("two", "ss", "old2"):
        try:
            D = ss2_get_D(ANN, xp) if name == "ss" \
                else two_newton_D(ANN, gamma, xp)
        except Exception:
            continue
        err = abs(D - D_onchain) / max(D_onchain, 1)
        if best is None or err < best[1]:
            best = (name, err)
    if best is None or best[1] > 0.005:
        return None, (best[1] if best else None)
    return best


class Tri3StateVenue:
    """Tricrypto-ng venue seeded from the real pool: all three real balances,
    the real price_scale pair and the real fee parameters, kept in the POOL'S
    OWN units. Rescaling the balances into dollars only works when the borrowed
    leg happens to be the pool's numeraire, so the dollar conversion lives at
    the interface instead: external coin 0 is the borrowed asset in dollars,
    external coin 1 the collateral in tokens, and `q_usd` bridges the two."""

    def __init__(self, balances, price_scale, i_q, i_b, q_usd_1e18, ANN, gamma,
                 mid_fee, out_fee, fee_gamma, D=None):
        self.balances = list(balances)          # native 1e18, pool order
        self.price_scale = list(price_scale)    # for pool coins 1 and 2
        self.i_q, self.i_b, self.q_usd = i_q, i_b, q_usd_1e18
        self.ANN, self.gamma = ANN, gamma
        self.mid_fee, self.out_fee, self.fee_gamma = mid_fee, out_fee, fee_gamma
        self.D = D if D is not None else tri_newton_D(ANN, gamma, self._xr())

    def _xr(self, bal=None):
        b = bal if bal is not None else self.balances
        return [b[0],
                b[1] * self.price_scale[0] // E18,
                b[2] * self.price_scale[1] // E18]

    @property
    def pair_balances(self):
        return [self.balances[self.i_q] * self.q_usd // E18,
                self.balances[self.i_b]]

    def clone(self):
        c = copy.copy(self)
        c.balances = list(self.balances)
        c.price_scale = list(self.price_scale)
        return c

    def _raw_dy(self, ii: int, jj: int, dx: int) -> int:
        xp = list(self.balances)
        xp[ii] += dx
        for k in range(2):
            xp[k + 1] = xp[k + 1] * self.price_scale[k] // E18
        y = tri_get_y(self.ANN, self.gamma, xp, self.D, jj)
        dy = xp[jj] - y - 1
        xp[jj] = y
        if jj > 0:
            dy = dy * E18 // self.price_scale[jj - 1]
        return dy - tri_fee(xp, self.mid_fee, self.out_fee,
                            self.fee_gamma) * dy // FEE_DENOM

    def get_dy(self, i: int, j: int, dx: int) -> int:
        assert i != j and i in (0, 1) and j in (0, 1) and dx > 0
        if i == 1:                              # collateral in, dollars out
            return self._raw_dy(self.i_b, self.i_q, dx) * self.q_usd // E18
        return self._raw_dy(self.i_q, self.i_b, dx * E18 // self.q_usd)

    def exec(self, i: int, j: int, dx: int) -> int:
        dy = self.get_dy(i, j, dx)
        if i == 1:
            ii, jj, dxr, dyr = self.i_b, self.i_q, dx, dy * E18 // self.q_usd
        else:
            ii, jj, dxr, dyr = self.i_q, self.i_b, dx * E18 // self.q_usd, dy
        if dyr >= self.balances[jj]:
            raise ValueError("venue side depleted")
        self.balances[ii] += dxr
        self.balances[jj] -= dyr
        self.D = tri_newton_D(self.ANN, self.gamma, self._xr())
        return dy


def _tri_from_state(st: dict, coll_usd_1e18: int):
    """3-coin cryptoswap from live state, kept in the pool's own units."""
    ib, iq = st["i_base"], st["i_quote"]
    dec, ps = st["decimals"], [_i(x) for x in (st.get("price_scale") or [])]
    if len(ps) != 2:
        return None
    q_usd = (st.get("usd") or [None, None, None])[iq]
    if not q_usd:
        return None
    bal = [_i(st["balances"][k]) * 10 ** (18 - dec[k]) for k in range(3)]
    if any(b <= 0 for b in bal):
        return None
    try:
        return Tri3StateVenue(bal, ps, iq, ib, int(q_usd * PRECISION),
                              _i(st["A"]), _i(st["gamma"]), _i(st["mid_fee"]),
                              _i(st["out_fee"]), _i(st["fee_gamma"]))
    except Exception:
        return None
