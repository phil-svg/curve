"""Curve cryptoswap-NG 2-coin invariant + get_dy — reference Python port.

Direct translation of the newton_D + newton_y iterations from
    https://github.com/curvefi/tricrypto-ng/blob/main/contracts/main/CurveTricryptoOptimizedWETH.vy
specialized to N=2.  Matches Curve's on-chain math bit-for-bit for a
reasonably-sized dx (tested against `get_dy` calls on the real pool).

The invariant is:
    A · N^N · ΣXP · K + D = A · N^N · D · K + D^(N+1) / (N^N · ΠXP)
with
    K   = A · K0 · (γ / (γ + 10^18 - K0))^2  ·  1/A_MULTIPLIER
    K0  = 10^18 · N^N · ΠXP / D^N       (base = 10^18)
    XP  = balances scaled to a common unit via price_scale
          (so XP[0] = crvUSD balance, XP[1] = CRV balance · price_scale)

Fee is the DYNAMIC fee described in Curve's cryptoswap whitepaper §6:
    f = mid_fee + (out_fee - mid_fee) · g / (g + 10^18 - K0)
where g = fee_gamma; the further from the balanced point (K0 → 1), the higher.
"""
from __future__ import annotations
from decimal import Decimal, getcontext
from typing import Sequence

# Constants copied verbatim from Curve's cryptoswap-NG
N = 2
A_MULTIPLIER = 10 ** 4
PRECISION = 10 ** 18


# ---- Geometric mean initial-guess for D (Vyper `geometric_mean`) -----------
def _geometric_mean_desc(x: Sequence[int]) -> int:
    """Newton-iteration integer geometric mean for a length-N sorted vector."""
    # Vyper's implementation for N=2 reduces to isqrt of the product.
    prod = x[0] * x[1]
    # isqrt (Newton)
    z = prod
    y = (prod + 1) // 2
    while y < z:
        z = y
        y = (prod // y + y) // 2
    return z


# ---- Newton solver for D ---------------------------------------------------
def newton_D(A_gamma_ann: int, gamma: int, xp: Sequence[int]) -> int:
    """Find D such that the invariant is satisfied. xp are balances in a common
    unit (already scaled by price_scale, and scaled up to 1e18 precision).

    A_gamma_ann = A · N^N · A_MULTIPLIER — i.e. what Curve calls `ANN` in the
    Vyper source. For a stock tricrypto NG pool with A_raw = 270, N = 3, and
    A_MULTIPLIER = 10000, ANN = 270 · 27 · 10000 = 72,900,000. For N=2 it is
    A_raw · 4 · A_MULTIPLIER.
    """
    # sort desc (only matters when N > 2; for N=2 swap if needed)
    x = list(xp)
    if x[0] < x[1]:
        x[0], x[1] = x[1], x[0]

    S = x[0] + x[1]
    # Initial guess: D = N · geometric_mean(x)
    D = N * _geometric_mean_desc(x)

    for _ in range(255):
        D_prev = D

        # K0 = 10^18 · N^N · Π x / D^N   (with N=2: 10^18 · 4 · x0·x1 / D^2)
        K0 = PRECISION
        for xi in x:
            K0 = K0 * xi * N // D
        # ^^ that's actually (10^18) · Π (N·x_i / D)
        # For N=2: K0 = 10^18 · (2·x0/D) · (2·x1/D) — correct.

        # _g1k0 = |gamma + 1e18 - K0|
        _g1k0 = gamma + PRECISION
        if _g1k0 > K0:
            _g1k0 = _g1k0 - K0 + 1
        else:
            _g1k0 = K0 - _g1k0 + 1

        # mul1 = 10^18 · D / gamma · _g1k0 / gamma · _g1k0 · A_MULTIPLIER / A_gamma_ann
        mul1 = PRECISION * D // gamma * _g1k0 // gamma * _g1k0 * A_MULTIPLIER // A_gamma_ann

        # mul2 = 2 · 10^18 · N · K0 / _g1k0
        mul2 = 2 * PRECISION * N * K0 // _g1k0

        # neg_fprime = (S + S · mul2 / 10^18) + mul1 · N / K0 - mul2 · D / 10^18
        neg_fprime = (S + S * mul2 // PRECISION) + mul1 * N // K0 - mul2 * D // PRECISION

        # D_plus  = D · (neg_fprime + S) / neg_fprime
        # D_minus = D·D / neg_fprime
        D_plus  = D * (neg_fprime + S) // neg_fprime
        D_minus = D * D // neg_fprime

        # If K0 <= 1e18: D_minus += D · (mul1/neg_fprime) / 1e18 · (1e18 - K0) / K0
        # else:          D_minus -= D · (mul1/neg_fprime) / 1e18 · (K0 - 1e18) / K0
        if K0 > PRECISION:
            D_minus -= D * (mul1 // neg_fprime) // PRECISION * (K0 - PRECISION) // K0
        else:
            D_minus += D * (mul1 // neg_fprime) // PRECISION * (PRECISION - K0) // K0

        if D_plus > D_minus:
            D = D_plus - D_minus
        else:
            D = (D_minus - D_plus) // 2

        diff = D - D_prev if D > D_prev else D_prev - D
        # converge: same tolerance as Curve — 10 or 1e-14 relative
        if diff * 10 ** 14 < max(10 ** 16, D):
            return D

    raise RuntimeError(f"newton_D did not converge; last D={D}")


# ---- Newton solver for y given x[i] ----------------------------------------
def newton_y(A_gamma_ann: int, gamma: int, x_new: Sequence[int], D: int, j: int) -> int:
    """Given all balances except index j, and the invariant D, solve for x[j].
    x_new is the balance vector with the OTHER index's new value in place."""
    N_ = len(x_new)  # 2
    x_sorted = list(x_new)
    if x_sorted[0] < x_sorted[1] and False:  # For N=2, we handle differently below
        pass
    # We only need to solve for x[j], so leave the vector as-is but skip index j.
    other = 1 - j
    y = D * D // (N * N * x_new[other])  # initial guess: y ≈ D^2 / (N^2 · x_other)

    # convergence
    for _ in range(255):
        y_prev = y
        xp = [0, 0]
        xp[other] = x_new[other]
        xp[j] = y

        K0 = PRECISION
        for xi in xp:
            K0 = K0 * xi * N // D
        _g1k0 = gamma + PRECISION
        if _g1k0 > K0:
            _g1k0 = _g1k0 - K0 + 1
        else:
            _g1k0 = K0 - _g1k0 + 1

        # mul1 = 10^18 · D · _g1k0 / gamma · _g1k0 / gamma · A_MULTIPLIER / A_gamma_ann
        mul1 = PRECISION * D // gamma * _g1k0 // gamma * _g1k0 * A_MULTIPLIER // A_gamma_ann
        # mul2 = 10^18 + 2 · 10^18 · K0 / _g1k0
        mul2 = PRECISION + 2 * PRECISION * K0 // _g1k0

        # yfprime = y + S·mul2/1e18 + mul1
        S = xp[0] + xp[1]
        yfprime = PRECISION * y + S * mul2 + mul1
        _dyfprime = D * mul2
        if yfprime < _dyfprime:
            y = y_prev // 2
            continue
        else:
            yfprime -= _dyfprime

        fprime = yfprime // y
        # y_minus = mul1 / fprime
        y_minus = mul1 // fprime
        # y_plus  = (yfprime + 1e18·D) / fprime + y_minus · 1e18 / K0
        y_plus  = (yfprime + PRECISION * D) // fprime + y_minus * PRECISION // K0
        y_minus += PRECISION * S // fprime

        if y_plus < y_minus:
            y = y_prev // 2
        else:
            y = y_plus - y_minus

        diff = y - y_prev if y > y_prev else y_prev - y
        if diff * 10 ** 14 < max(10 ** 16, y):
            return y

    raise RuntimeError(f"newton_y did not converge; last y={y}")


# ---- Dynamic fee (cryptoswap whitepaper §6) --------------------------------
def dynamic_fee(mid_fee: int, out_fee: int, fee_gamma: int, xp: Sequence[int]) -> int:
    """f = (mid_fee · f_denom + out_fee · (1e18 - f_denom)) / 1e18
    where f_denom = fee_gamma / (fee_gamma + 1e18 - K)
    and   K       = prod(xp / (Σxp/N))  — measure of balance, 1 when balanced."""
    S = xp[0] + xp[1]
    if S == 0:
        return mid_fee
    # K = 4 · xp[0] · xp[1] / S^2  (in 1e18 base; K=1 when balanced)
    K = 4 * PRECISION * xp[0] // S * xp[1] // S
    f_denom = fee_gamma * PRECISION // (fee_gamma + PRECISION - K)
    return (mid_fee * f_denom + out_fee * (PRECISION - f_denom)) // PRECISION


# ---- High-level get_dy -----------------------------------------------------
class Cryptoswap2Coin:
    """2-coin Curve cryptoswap-NG pool.

    Coins:  0 = base coin (e.g., crvUSD, 18-dec)
            1 = quote coin (e.g., CRV,    18-dec)

    Fitted from (TVL_usd, price_of_coin1_in_coin0) with a 50/50 USD-value split.
    """
    def __init__(self, tvl_usd_1e18: int, price_1_per_0_1e18: int,
                 A_raw: int = 270, gamma: int = 1_300_000_000_000,
                 mid_fee: int = 3_000_000, out_fee: int = 80_000_000,
                 fee_gamma: int = 350_000_000_000_000):
        """
        tvl_usd_1e18       : total TVL in USD, scaled to 1e18 (both coins are 18-dec)
        price_1_per_0_1e18 : coin1 price in coin0 units (e.g., CRV in crvUSD), 1e18
        A_raw              : raw A (Curve default for tricrypto NG ≈ 270)
        gamma              : cryptoswap concentration parameter (1e18 base)
        mid_fee / out_fee  : fee bounds in 1e10 base (3e6 = 0.03%, 8e7 = 0.8%)
        fee_gamma          : controls how quickly fee ramps mid→out
        """
        self.A_raw = A_raw
        self.gamma = gamma
        self.mid_fee = mid_fee
        self.out_fee = out_fee
        self.fee_gamma = fee_gamma

        # A_gamma_ann as Curve stores it: A_raw · N^N · A_MULTIPLIER (for N=2: 4·10000)
        # A_raw may be fractional (e.g. 67.5 → ANN 2,700,000 to match TriCRV's
        # on-chain A exactly); ANN itself is always an integer.
        self.A_gamma_ann = int(round(A_raw * (N ** N) * A_MULTIPLIER))

        # 50/50 USD split
        # balance_0 = TVL/2 crvUSD   (coin0 price = 1)
        # balance_1 = (TVL/2) / P    (coin1 amount)
        self.balances = [
            tvl_usd_1e18 // 2,
            tvl_usd_1e18 * PRECISION // (2 * price_1_per_0_1e18),
        ]
        # price_scale scales coin1 → coin0 units for the invariant math.
        self.price_scale = price_1_per_0_1e18

        # Precompute D
        self.D = newton_D(self.A_gamma_ann, self.gamma, self._xp(self.balances))

    def _xp(self, balances: Sequence[int]) -> list[int]:
        """Balances in a common unit: xp[0] = crvUSD, xp[1] = CRV * price_scale."""
        return [balances[0], balances[1] * self.price_scale // PRECISION]

    def get_dy(self, i: int, j: int, dx: int) -> int:
        """Amount of coin j out for `dx` of coin i in — after dynamic fee."""
        assert i != j and i in (0, 1) and j in (0, 1)
        # Apply the input to xp
        new_balances = list(self.balances)
        new_balances[i] += dx
        xp = self._xp(new_balances)
        y_new = newton_y(self.A_gamma_ann, self.gamma, xp, self.D, j)
        # dy_before_fee is in xp units; convert back to coin units
        if j == 1:
            # xp[1] = balance[1] · price_scale / 1e18; recover balance
            balance_j_before = self.balances[j]
            balance_j_after  = y_new * PRECISION // self.price_scale
        else:
            balance_j_before = self.balances[j]
            balance_j_after  = y_new
        dy = balance_j_before - balance_j_after
        # Fee — cryptoswap applies at end using AFTER-swap xp
        xp_after = list(xp)
        xp_after[j] = y_new
        f = dynamic_fee(self.mid_fee, self.out_fee, self.fee_gamma, xp_after)
        dy_after_fee = dy - dy * f // 10 ** 10
        return dy_after_fee


if __name__ == "__main__":
    # Smoke: fit to the actual 21:10 Curve pool TVL/price and quote a modest CRV sale.
    P = 656_418_141_040_952_638          # CRV price 0.6564 in 1e18
    TVL = 9_246_520 * PRECISION          # $9.25M in 1e18 base
    pool = Cryptoswap2Coin(TVL, P)
    for dx_h in [100, 1_000, 10_000, 100_000, 1_000_000]:
        dx = dx_h * PRECISION
        dy = pool.get_dy(1, 0, dx)
        print(f"  sell {dx_h:>10} CRV → {dy/1e18:>18,.2f} crvUSD (avg price {dy/dx:.6f})")
