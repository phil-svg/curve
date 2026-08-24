"""Build a LLAMMA snapshot containing exactly ONE abstracted borrower.

The whole book is replaced by a single position described by two numbers:

    collateral_usd   how much CRV collateral the borrower has, valued at the
                     scenario's starting market price (crash_start_spot)
    debt_usd         how much crvUSD they owe        (or --ltv-pct instead)

Everything else is derived through the real contracts:

  * y0   = collateral_usd / start_price                     (CRV amount)
  * n1   = Controller.calculate_debt_n1(y0, debt, N)        (on-chain call)
  * n2   = n1 + N - 1
  * the position is deposited via the AMM's real `apply_deposit` by
    make_snapshot, into a market whose active_band is pinned to the real
    market's value at the anchor block.

The position starts FRESH (pure collateral, x = 0). It is not pre-soft-liquidated:
the crash happens inside the sim window, so the auto-arb soft-liquidates it
endogenously as the price falls through its bands. That keeps the whole scenario
a function of just the two inputs, with no hidden vintage parameters.

`calculate_debt_n1` is scale-invariant in (collateral, debt) — verified: the same
debt/collateral ratio returns the same n1 at 1×, 1k× and 10k× size — so band
placement depends only on LTV, and collateral_usd only sets the position's size.

Usage:
    python single_user.py --collateral-usd 8000000 --ltv-pct 50 --out snap.json
"""
from __future__ import annotations
import argparse
import json
import math
import subprocess
import sys
from decimal import Decimal, getcontext, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))
from common import call_int256, call_uint256  # noqa: E402

V1SIM = Path(__file__).resolve().parent.parent / "v1sim_data"
HERE = Path(__file__).resolve().parent
REF_SNAP = V1SIM / "snapshots/amm_0xafca625321df8d6a068bdd8f1585d489d2acf11b_23549898.json"
MAKE_BIN = Path(__file__).resolve().parent.parent / "bin" / "make_snapshot"
CONTROLLER = "0xEdA215b7666936DEd834f76f3fBC6F323295110A"
AMM_ADDR   = "0xafca625321Df8D6A068bDD8F1585d489D2acF11b"
ANCHOR_BLOCK = 23_549_898          # 21:05 UTC, the sim window's first block
USER_ADDR = "0x0000000000000000000000000000000000000001"

# MIN_TICKS = 4 in the Controller (assert N > MIN_TICKS-1, "Need more ticks"),
# MAX_TICKS = 50. 4 is the tightest, most capital-efficient band spread a
# borrower may choose.
MIN_TICKS, MAX_TICKS = 4, 50
DEFAULT_N = 4

# --- Controller / AMM scalars at ANCHOR_BLOCK -------------------------------
# These are the market-state inputs to the band math and do NOT depend on
# loan_discount, so they are pinned once (verified by fetch_anchor_state()).
ONCHAIN_A = 30                                 # AMM.A() of the CRV market


class BandGrid:
    """LLAMMA immutables derived from the band-width factor A.

    A is a per-MARKET parameter (CRV market 30, svZCHF 180) and is distinct
    from the liquidation venue's pool A. It sets band width: band n spans a
    price ratio of (A-1)/A, so larger A = narrower bands = a tighter soft-liq
    ramp and a higher max LTV.

    Derivations follow AMM.vy's constructor exactly:
        Aminus1 = A-1,  A2 = A^2,  Aminus12 = (A-1)^2
        SQRT_BAND_RATIO   = sqrt(A/(A-1))            (round-half-up, 1e18)
        LOG_A_RATIO       = ln(A/(A-1))              (1e18)
        MAX_ORACLE_DN_POW = (A/(A-1))**50            (iterated, 1e18)

    At A = 30 the exact on-chain constants are pinned instead of derived, so
    the 180/180 calculate_debt_n1 and 49/49 max_borrowable checks against the
    deployed Controller stay bit-exact. Derived LOG_A_RATIO uses a true ln;
    the chain uses snekmate _wad_ln, which is 9 wei (2.7e-16 relative) lower.
    That cannot move a band index, but a non-default-A grid is therefore
    'exact to 1e-15', not 'bit-exact', against a hypothetical deployed AMM.
    """

    _PINNED = {30: (1017095255431215575, 33901551675681339, 5447068553010022855)}

    def __init__(self, A: int):
        A = int(A)
        if A < 2:
            raise ValueError("LLAMMA A must be >= 2")
        self.A = A
        self.Aminus1 = A - 1
        self.A2 = A * A
        self.Aminus12 = self.Aminus1 ** 2
        if A in self._PINNED:
            self.SQRT_BAND_RATIO, self.LOG_A_RATIO, self.MAX_ORACLE_DN_POW = self._PINNED[A]
            return
        getcontext().prec = 60
        self.SQRT_BAND_RATIO = int(
            (Decimal(10) ** 36 * A / self.Aminus1).sqrt()
            .to_integral_value(rounding=ROUND_HALF_UP))
        self.LOG_A_RATIO = int((Decimal(A) / self.Aminus1).ln() * 10 ** 18)
        pw = 10 ** 18
        for _ in range(50):
            pw = pw * A // self.Aminus1
        self.MAX_ORACLE_DN_POW = pw

    def immutables(self) -> dict:
        """Snapshot `immutables` fields that depend on A (for make_snapshot)."""
        return {"A": str(self.A), "Aminus1": str(self.Aminus1), "A2": str(self.A2),
                "Aminus12": str(self.Aminus12),
                "SQRT_BAND_RATIO": str(self.SQRT_BAND_RATIO),
                "LOG_A_RATIO": str(self.LOG_A_RATIO),
                "MAX_ORACLE_DN_POW": str(self.MAX_ORACLE_DN_POW)}


DEFAULT_GRID = BandGrid(ONCHAIN_A)
A, AMINUS1 = DEFAULT_GRID.A, DEFAULT_GRID.Aminus1
SQRT_BAND_RATIO = DEFAULT_GRID.SQRT_BAND_RATIO   # sqrt(A/(A-1)) in 1e18
LOGN_A_RATIO    = DEFAULT_GRID.LOG_A_RATIO       # ln(A/(A-1)) in 1e18
BASE_PRICE      = 1081865763231141001          # AMM.get_base_price()
PRICE_ORACLE    = 664636926004854906           # AMM.price_oracle()
ACTIVE_BAND     = 13                           # AMM.active_band()
ACTIVE_BAND_SKIP = 13                          # AMM.active_band_with_skip()
ONCHAIN_LOAN_DISCOUNT = 110000000000000000     # Controller.loan_discount() = 11%
DEAD_SHARES = 1000
MAX_P_BASE_BANDS = 5
MAX_SKIP_TICKS = 1024
COLLATERAL_PRECISION = BORROWED_PRECISION = 1
ONE = 10 ** 18


_U256 = 1 << 256


def _to_u256(x: int) -> int:
    return x % _U256


def solmate_exp(power: int) -> int:
    """Solmate exp polynomial, exactly as Vyper's _p_oracle_up uses it.

    Repeated integer multiplication by (A-1)/A is NOT equivalent: it drifts
    ~1e-12 relative by band 30, which propagates into max_borrowable. Ported
    from the verified C++ implementation in cpp/src/llama_amm.cpp.
    """
    if not (-41446531673892821376 < power < 135305999368893231589):
        raise ValueError("solmate_exp: power out of range")
    TWO_96, TWO_95, E18 = 1 << 96, 1 << 95, 10 ** 18

    def idiv(a, b):                      # EVM division truncates toward zero
        q = abs(a) // abs(b)
        return q if (a < 0) == (b < 0) else -q

    x = idiv(power * TWO_96, E18)
    C_K = 54916777467707473351141471128
    k = idiv(idiv(x * TWO_96, C_K) + TWO_95, TWO_96)
    x = x - k * C_K

    y = x + 1346386616545796478920950773328
    y = idiv(y * x, TWO_96) + 57155421227552351082224309758442
    p = y + x - 94201549194550492254356042504812
    p = idiv(p * y, TWO_96) + 28719021644029726153956944680412240
    p = p * x + 4385272521454847904659076985693276 * TWO_96

    q = x - 2855989394907223263936484059900
    q = idiv(q * x, TWO_96) + 50020603652535783019961831881945
    q = idiv(q * x, TWO_96) - 533845033583426703283633433725380
    q = idiv(q * x, TWO_96) + 3604857256930695427073651918091429
    q = idiv(q * x, TWO_96) - 14423608567350463180887372962807573
    q = idiv(q * x, TWO_96) + 26449188498355588339934803723976023

    pq = _to_u256(idiv(p, q))
    prod = pq * 3822833074963236453042738258902158003155416615667
    shift = k - 195
    res = (prod << shift) if shift >= 0 else (_to_u256(prod) >> (-shift))
    return _to_u256(res)


def p_oracle_up(n: int, grid: BandGrid = DEFAULT_GRID) -> int:
    """AMM.p_oracle_up(n) = base_price · exp(-n · LOG_A_RATIO)."""
    return BASE_PRICE * solmate_exp(-n * grid.LOG_A_RATIO) // ONE


def _floor_div_logratio(x_1e18: int, grid: BandGrid = DEFAULT_GRID) -> int:
    """floor(ln(x/1e18) / ln(A/(A-1))) — the Controller's wad_ln + signed-floor idiom.

    Seeded with a float log, then snapped against the integer band ladder so a
    float rounding error can never land us on the wrong side of a boundary.
    """
    n = int(math.floor(math.log(x_1e18 / 1e18) / (grid.LOG_A_RATIO / 1e18)))
    while _pow_ratio(n + 1, grid) <= x_1e18:
        n += 1
    while _pow_ratio(n, grid) > x_1e18:    # float seed is off by <=2 steps
        n -= 1
    return n


def _pow_ratio(n: int, grid: BandGrid = DEFAULT_GRID) -> int:
    """(A/(A-1))^n in 1e18 — the value whose log_{A/(A-1)} is exactly n.
    Uses the same exp the contract does, so the snap in _floor_div_logratio
    lands on the contract's own band boundaries."""
    return solmate_exp(n * grid.LOG_A_RATIO)


def get_y_effective(collateral_wei: int, n: int, discount_wei: int,
                    grid: BandGrid = DEFAULT_GRID) -> int:
    """Exact port of Controller.get_y_effective, including the DEAD_SHARES
    extra discount that the plain closed-form formula omits."""
    extra = (DEAD_SHARES * ONE) // max(collateral_wei // n, DEAD_SHARES)
    d_y = collateral_wei * (ONE - min(discount_wei + extra, ONE)) // (grid.SQRT_BAND_RATIO * n)
    y_eff = d_y
    for _ in range(1, n):
        d_y = d_y * grid.Aminus1 // grid.A
        y_eff += d_y
    return y_eff


def active_band_at(oracle_wei: int, grid: BandGrid = DEFAULT_GRID) -> int:
    """Band the AMM sits in for a given oracle price.

    Band n spans [p_oracle_up(n+1), p_oracle_up(n)], but the AMM's active_band
    only moves when a trade crosses it, so on-chain it lags the containing band
    by one: at the anchor oracle ($0.664637) the containing band is 14 while
    AMM.active_band() reads 13. That -1 is applied here, which reproduces the
    anchor exactly and generalises to any oracle price.
    """
    # Seed from the log estimate, then correct locally. A fixed scan from -1024
    # silently clamped far-out prices (a $90,000 oracle on the CRV grid lives in
    # band -2034 at A=180) and made max LTV COLLAPSE with price — the contract
    # has no such limit, its active_band follows the price via trading.
    n = _floor_div_logratio(BASE_PRICE * ONE // oracle_wei, grid) - 2
    while p_oracle_up(n + 1, grid) <= oracle_wei:      # too far down: step back
        n -= 1
    while p_oracle_up(n + 1, grid) > oracle_wei:       # scan semantics as before
        n += 1
    return n - 1


def max_p_base(oracle_wei: int = PRICE_ORACLE, grid: BandGrid = DEFAULT_GRID) -> int:
    """Exact port of Controller.max_p_base — independent of loan_discount.

    `oracle_wei` is a parameter, not the pinned constant: max_p_base is the
    highest band price at or below the ORACLE, so freezing it at the CRV
    market's value makes max_borrowable scale with 1/collateral_price. Max LTV
    must be a pure ratio (see debt_cap).
    """
    n_min = active_band_at(oracle_wei, grid)
    n1 = _floor_div_logratio(BASE_PRICE * ONE // oracle_wei, grid) + MAX_P_BASE_BANDS
    n1 = max(n1, n_min + 1)
    p_base = p_oracle_up(n1, grid)
    for _ in range(MAX_SKIP_TICKS + 1):
        n1 -= 1
        if n1 <= n_min:
            break
        p_base_prev = p_base
        p_base = p_base * grid.A // grid.Aminus1
        if p_base > oracle_wei:
            return p_base_prev
    return p_base


def max_borrowable(collateral_wei: int, n: int,
                   loan_discount_wei: int = ONCHAIN_LOAN_DISCOUNT,
                   oracle_wei: int = PRICE_ORACLE,
                   grid: BandGrid = DEFAULT_GRID) -> int:
    """Controller.max_borrowable WITHOUT its liquidity clamp.

    On-chain it returns min(health_limit, BORROWED_TOKEN.balanceOf(self) +
    current_debt); that second term is the vault's available crvUSD ($944,772 at
    the anchor block) and has nothing to do with how levered a position may be,
    so it is omitted here.
    """
    y_eff = get_y_effective(collateral_wei * COLLATERAL_PRECISION, n, loan_discount_wei, grid)
    x = max(y_eff * max_p_base(oracle_wei, grid) // ONE, 1) - 1
    return x * (ONE - 10 ** 14) // (ONE * BORROWED_PRECISION)


def calculate_debt_n1(collateral_wei: int, debt_wei: int, n: int,
                      loan_discount_wei: int = ONCHAIN_LOAN_DISCOUNT,
                      oracle_wei: int = PRICE_ORACLE,
                   grid: BandGrid = DEFAULT_GRID) -> int:
    """Exact port of Controller._calculate_debt_n1.

    Ported rather than eth_call'd because loan_discount is a UI knob here and
    the deployed Controller's value is frozen at 11%.
    """
    assert debt_wei > 0, "No loan"
    n0 = active_band_at(oracle_wei, grid)
    p_base = p_oracle_up(n0, grid)
    y_eff = get_y_effective(collateral_wei * COLLATERAL_PRECISION, n, loan_discount_wei, grid)
    ratio = y_eff * p_base // (debt_wei * BORROWED_PRECISION + 1)
    if ratio <= 0:
        raise ValueError("Amount too low")
    n1 = _floor_div_logratio(ratio, grid)
    n1 = min(n1, 1024 - n) + n0
    if p_oracle_up(n1, grid) >= oracle_wei:
        raise ValueError("Debt too high")
    return n1


def fetch_anchor_state(block: int = ANCHOR_BLOCK) -> dict:
    """Re-read the pinned scalars from chain and assert the port still agrees."""
    got = {
        "BASE_PRICE":  call_uint256(AMM_ADDR, "get_base_price()", (), block=block),
        "PRICE_ORACLE": call_uint256(AMM_ADDR, "price_oracle()", (), block=block),
        "ACTIVE_BAND": call_int256(AMM_ADDR, "active_band()", (), block=block),
        "ACTIVE_BAND_SKIP": call_int256(AMM_ADDR, "active_band_with_skip()", (), block=block),
        "ONCHAIN_LOAN_DISCOUNT": call_uint256(CONTROLLER, "loan_discount()", (), block=block),
    }
    for k, v in got.items():
        assert globals()[k] == v, f"{k} drifted: pinned {globals()[k]} vs chain {v}"
    return got


def debt_cap(collateral_usd: float, start_price: float, n_bands: int,
             loan_discount_pct: float, oracle_price: float = None,
             llamma_A: int = ONCHAIN_A) -> dict:
    """Max borrowable (and the LTV that implies) for a given collateral/N/loan_discount.

    The contract's limit is a PURE RATIO — (1 - loan_discount) x G(A,N) x
    p_base/oracle — with no price in it. So `oracle_price` must track whatever
    asset is being modelled: pinning it to the CRV market's $0.664637 while the
    collateral trades at, say, CHF's $1.23 rescales the answer by 0.6506/1.23
    and reports 47% where the truth is ~87%.

    LTV is still quoted against `start_price` (the scenario's opening market
    price), so a start_price deliberately offset from the oracle — a crash
    already in progress — legitimately shifts it.
    """
    if oracle_price is None:
        oracle_price = start_price
    y0_wei = int(collateral_usd / start_price * 1e18)
    maxb = max_borrowable(y0_wei, n_bands, int(loan_discount_pct / 100.0 * 1e18),
                          int(oracle_price * 1e18), BandGrid(llamma_A))
    return {"crv": y0_wei / 1e18,
            "max_debt_usd": maxb / 1e18,
            "max_ltv_pct": (maxb / 1e18 / collateral_usd * 100.0) if collateral_usd else 0.0}


def build(collateral_usd: float, debt_usd: float, start_price: float,
          oracle_price: float, n_bands: int, out_path: Path,
          workdir: Path | None = None, verbose: bool = True,
          loan_discount_pct: float = 11.0, clamp: bool = True,
          llamma_A: int = ONCHAIN_A,
          amm_fee_wei: int | None = None,
          amm_rate_wei: int | None = None,
          pinned: bool = False) -> dict:
    workdir = workdir or out_path.parent
    workdir.mkdir(parents=True, exist_ok=True)
    grid = BandGrid(llamma_A)

    if not (MIN_TICKS <= n_bands <= MAX_TICKS):
        raise ValueError(f"N must be between {MIN_TICKS} and {MAX_TICKS} "
                         f'(Controller: assert N > MIN_TICKS-1, "Need more ticks")')

    y0_wei = int(collateral_usd / start_price * 1e18)
    debt_wei = int(debt_usd * 1e18)
    ld_wei = int(loan_discount_pct / 100.0 * 1e18)
    oracle_wei = int(oracle_price * 1e18)

    # pinned = llamma-simulator's placement (libsimulate.py single_run):
    # the grid is re-anchored per run so the ladder's top band edge sits a
    # hair above the deposit price — p_base = p0*(A/(A-1) + 1e-4), bands
    # 1..N, active_band 0. No max-LTV geometry, hence no grid-phase lottery:
    # every A starts at the identical zero-cushion (worst-case) landing.
    if pinned:
        n1, n2 = 1, n_bands
        active_band = 0
        maxb = 0
        clamped = False
    else:
        maxb = max_borrowable(y0_wei, n_bands, ld_wei, oracle_wei, grid)
    clamped = False
    if not pinned and debt_wei >= maxb:
        if not clamp:
            raise ValueError(
                f"debt ${debt_usd:,.0f} exceeds max borrowable ${maxb/1e18:,.0f} "
                f"for {y0_wei/1e18:,.0f} CRV at N={n_bands}, loan_discount="
                f"{loan_discount_pct}% (LTV cap {maxb/1e18/collateral_usd*100:.2f}% "
                f"at ${start_price}); calculate_debt_n1 would revert 'Debt too high'.")
        # Sit just under the cap: at exactly maxb the loan is still placeable,
        # but the 1e-4 haircut inside max_borrowable leaves no headroom.
        debt_wei = maxb - 1
        debt_usd = debt_wei / 1e18
        clamped = True

    if not pinned:
        n1 = calculate_debt_n1(y0_wei, debt_wei, n_bands, ld_wei, oracle_wei, grid)
        n2 = n1 + n_bands - 1

        # active_band must follow the configured oracle, not the reference
        # snapshot's 13 — that value only describes the CRV market at
        # $0.664637. active_band_at reproduces it exactly there and
        # generalises elsewhere.
        active_band = active_band_at(oracle_wei, grid)

    pos_path = workdir / "su_positions.json"
    path_path = workdir / "su_path.json"
    pos_path.write_text(json.dumps([{
        "user": USER_ADDR, "y0": str(y0_wei),
        "n1": n1, "n2": n2, "debt": str(debt_wei),
    }]))
    # Single-entry path => deposit only, no pre-crash walk, so x starts at 0.
    path_path.write_text(json.dumps([str(int(oracle_price * 1e18))]))

    # The C++ replay reads its band-grid immutables straight from the reference
    # snapshot, so a non-default A needs a patched copy — otherwise the Python
    # band math would use A while make_snapshot/sweep_precompute used 30.
    ref_for_make = REF_SNAP
    if grid.A != ONCHAIN_A or pinned:
        ref_patched = json.loads(REF_SNAP.read_text())
        ref_patched["immutables"].update(grid.immutables())
        if pinned:
            # Effective base = immutables.BASE_PRICE * state.rate_mul / 1e18;
            # solve for the raw immutable so the effective base lands exactly
            # on the llamma-simulator anchor.
            rate_mul = int(ref_patched["state"]["rate_mul"])
            base_eff_wei = int(oracle_price * (grid.A / (grid.A - 1) + 1e-4) * 1e18)
            ref_patched["immutables"]["BASE_PRICE"] = \
                str(base_eff_wei * 10**18 // rate_mul)
        ref_for_make = workdir / f"ref_snapshot_A{grid.A}{'_pinned' if pinned else ''}.json"
        ref_for_make.write_text(json.dumps(ref_patched))

    r = subprocess.run([str(MAKE_BIN), str(ref_for_make), str(pos_path), str(path_path),
                        str(out_path), str(active_band)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"make_snapshot failed: {r.stderr[-2000:]}")

    snap = json.loads(out_path.read_text())
    # The reference snapshot is the CRV market's, so without this every
    # simulated market inherited CRV's 0.6% AMM fee and 13.0% borrow rate —
    # 35 of 55 markets have a different fee (0.05%..1.4%). rate_time is pinned
    # to the snapshot clock so accrual starts at zero, not months in.
    if amm_fee_wei is not None:
        snap["state"]["fee"] = str(int(amm_fee_wei))
    if amm_rate_wei is not None:
        snap["state"]["rate"] = str(int(amm_rate_wei))
    if amm_fee_wei is not None or amm_rate_wei is not None:
        # rate_time only. rate_mul must NOT be touched: the engine's effective
        # base price is BASE_PRICE * rate_mul (0.832537 x 1.299481 = 1.081865),
        # and 1.081865 is precisely the BASE_PRICE constant this module places
        # bands against. Forcing rate_mul to 1e18 dropped the ladder 23.1% below
        # where the loan discount puts it — the price never reached the bands
        # (no soft-liquidation at all) while health, measured against the wrong
        # ladder, went negative and hard-liquidated a healthy position.
        # Pinning rate_time to the snapshot clock already makes accrual start at
        # zero, since _rate_mul() = rate_mul * (1 + rate * (t - rate_time)).
        snap["state"]["rate_time"] = str(int(snap["timestamp"]))
        out_path.write_text(json.dumps(snap))
    u = snap["users"][USER_ADDR]
    info = {
        "collateral_usd": collateral_usd, "debt_usd": debt_usd,
        "ltv_pct": debt_usd / collateral_usd * 100.0,
        "crv": y0_wei / 1e18, "n1": n1, "n2": n2, "N": n_bands,
        "active_band": active_band,
        "max_borrowable_usd": maxb / 1e18,
        "max_ltv_pct": maxb / 1e18 / collateral_usd * 100.0,
        "loan_discount_pct": loan_discount_pct,
        "llamma_A": grid.A,
        "clamped": clamped,
        "pinned": pinned,
        "amm_fee_pct": (amm_fee_wei / 1e16) if amm_fee_wei is not None else 0.6,
        "amm_rate_apr_pct": ((amm_rate_wei or 0) * 31_536_000 / 1e16
                             if amm_rate_wei is not None else None),
        "snapshot_collateral": int(u["collateral"]) / 1e18,
        "snapshot_stablecoin": int(u["stablecoin"]) / 1e18,
        "bands": len(snap["bands"]),
    }
    if verbose:
        print(f"[single] {info['crv']:,.0f} CRV (${collateral_usd:,.0f} @ ${start_price}) "
              f"/ ${debt_usd:,.0f} debt  = LTV {info['ltv_pct']:.1f}%")
        print(f"[single] bands n1={n1} n2={n2} (N={n_bands}), active_band={active_band} "
              f"-> {'IN soft-liq at t0' if n1 <= active_band else 'above active band (fresh)'}")
        print(f"[single] max borrowable at this size = ${maxb/1e18:,.0f} "
              f"(LTV cap {maxb/1e18/collateral_usd*100:.1f}%)")
        print(f"[single] wrote {out_path}")
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collateral-usd", type=float, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--debt-usd", type=float)
    g.add_argument("--ltv-pct", type=float, help="debt = collateral × ltv/100")
    ap.add_argument("--start-price", type=float, default=0.6072,
                    help="Market price used to convert collateral USD -> CRV "
                         "(default = crash_start_spot)")
    ap.add_argument("--oracle-price", type=float, default=0.6646,
                    help="LLAMMA oracle at the anchor block; sets deposit-time price")
    ap.add_argument("--n-bands", type=int, default=DEFAULT_N,
                    help=f"Bands to spread the deposit over ({MIN_TICKS}..{MAX_TICKS})")
    ap.add_argument("--llamma-A", type=int, default=ONCHAIN_A,
                    help="LLAMMA band-width factor A of the MARKET (not the venue pool). "
                         "CRV market 30, svZCHF 180.")
    ap.add_argument("--loan-discount-pct", type=float, default=11.0,
                    help="Controller loan_discount. Sets max borrowable and hence "
                         "band placement. On-chain value is 11%%.")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    debt = a.debt_usd if a.debt_usd is not None else a.collateral_usd * a.ltv_pct / 100.0
    build(a.collateral_usd, debt, a.start_price, a.oracle_price, a.n_bands, a.out,
          loan_discount_pct=a.loan_discount_pct, llamma_A=a.llamma_A)


if __name__ == "__main__":
    main()
