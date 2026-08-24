"""Artificial bad-debt sim for the CRV crash — the 'pool abstraction only'
version (first pass).

  Real crash → real LLAMMA state per block  (from existing precompute)
  Real crash → real chainlink price          (unchanged)
  Real crash → single ARTIFICIAL pool as the liquidator's only venue

The artificial pool is a 2-coin cryptoswap fitted to
  TVL = pair-only Curve tricrypto TVL at 10-Oct-2025 21:10 UTC  ×  1.5
  spot = 63¢ (user-defined crash-start price)
It DRAINS across the crash — every accepted hard-liquidation dumps the user's
collateral into the pool, updating its balances for the next liquidator.

Bad-debt formula matches the real sim's exactly:
  badDebt = Σ (debt - x - pool_spot × y)  over remaining candidates
where pool_spot is the artificial pool's current CRV price (starts at 63¢,
drifts down as CRV is dumped into it).

Output: JSON of per-block badDebt for 12% liquidation discount.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))

from cryptoswap_2coin import PRECISION
from venues import make_venue
from common import block_info, eth_call, sel, encode_int, call_uint256

# --- Config ------------------------------------------------------------------
FROM_BLOCK   = 23_549_898
TO_BLOCK     = 23_550_007

# Discount and TVL multiplier are CLI args (see main()).
DEFAULT_DISCOUNT_PCT = 12
DEFAULT_TVL_MULTIPLIER = 1.5

# LINEAR crash spec on the artificial pool's spot — endpoints match the real
# Curve tricrypto get_dy(2,0,1e18) at those exact minutes:
#   * 21:15 UTC   spot = 0.6072   crvUSD per CRV   (real block 23,549,944)
#   * 21:23 UTC   spot = 0.2657   crvUSD per CRV   (real block 23,549,984)
# Before 21:15  → pool spot held at CRASH_START_SPOT (no exogenous pressure)
# 21:15 → 21:23 → linear interpolation (8 minutes = 480 s)
# After 21:23   → pool spot held at CRASH_END_SPOT
# The crash window begins at 21:05 UTC (block 23,549,898) so the START offset
# from window-start is 10 min, END offset is 18 min.
DEFAULT_CRASH_START_OFFSET_S = 10 * 60   # 21:15 - 21:05
DEFAULT_CRASH_DURATION_S     = 8 * 60    # 21:15 → 21:23
DEFAULT_CRASH_START_SPOT     = 0.6072
DEFAULT_CRASH_END_SPOT       = 0.2657

# The following are populated in main() so target_spot_at() sees the CLI values.
CRASH_START_OFFSET_S = DEFAULT_CRASH_START_OFFSET_S
CRASH_END_OFFSET_S   = DEFAULT_CRASH_START_OFFSET_S + DEFAULT_CRASH_DURATION_S
CRASH_START_SPOT     = DEFAULT_CRASH_START_SPOT
CRASH_END_SPOT       = DEFAULT_CRASH_END_SPOT
POOL_INIT_CRV_PRICE  = DEFAULT_CRASH_START_SPOT

# Standard V1 profit-test constants (must match earlier code)
GAS_VIA_CRVUSD = 7_250_000
# Cheap side-route (TS: UniV3 CRV→USDT, 850k gas). Only viable for DUST dumps —
# the real UniV3 CRV/USDT pool was nearly empty during the crash (measured:
# 800k CRV quoted $765; and at $1k-$25k sizes its quotes already lagged the
# tricrypto enough to flip TS's decisions negative). Emulated as: same pool
# quote, cheaper gas, available only below a dust-size cap. Calibrated so the
# TS regression stays exact at every discount (25k cap over-settled 12/16%).
GAS_VIA_USDT       = 850_000
USDT_ROUTE_MAX_USD = 1_000

HERE = Path(__file__).resolve().parent.parent
V1SIM = Path(__file__).resolve().parent.parent / "v1sim_data"
def precompute_for(discount: int) -> Path:
    return V1SIM / "data/precompute" / f"curve_{discount}pct.json"

# Real curve tricrypto pool SPOT price per block (from earlier chart2 run).
# Real sim uses this same value for the crvPrice term in bad-debt accounting;
# reusing it here ensures the ONLY difference vs real sim is the profit-test venue.
REAL_SPOT_FILE = Path(__file__).resolve().parent.parent / "external" / "chart2_prices.json"

# On-chain Controller.users_to_liquidate() per block (with user lists).
# This is the SAME basis the real sim uses for its badDebt calculation: shortfall
# of on-chain u2l users minus whatever the sim's profit-tests settled.
ONCHAIN_U2L_FILE = HERE_PARENT = Path(__file__).resolve().parent.parent / \
    "results" / "onchain_measured_bad_debt.json"


# --- Load pair-only TVL at 21:10 --------------------------------------------
def load_pool_tvl_at_2110() -> float:
    """Sum crvUSD balance + CRV·price at block 23,549,919. WETH excluded."""
    CURVE_POOL = "0x4eBdF703948dDCEA3B11f675B4D1Fba9d2414A14"
    b = 23_549_919
    b0 = call_uint256(CURVE_POOL, "balances(uint256)", (0,), block=b)
    b2 = call_uint256(CURVE_POOL, "balances(uint256)", (2,), block=b)
    p_crv = call_uint256(CURVE_POOL, "price_oracle(uint256)", (1,), block=b)
    tvl_pair_wei = b0 + b2 * p_crv // PRECISION
    return tvl_pair_wei / 1e18


# --- ETH price fetcher (same as real sim: curve pool get_dy(1,0,1e18)) ------
def eth_price_at(b: int) -> float:
    """Same as earlier profit_test: pool.get_dy(1, 0, 1e18) / 1e18."""
    CURVE_POOL = "0x4eBdF703948dDCEA3B11f675B4D1Fba9d2414A14"
    data = sel("get_dy(uint256,uint256,uint256)") + encode_int(1) + encode_int(0) + encode_int(10**18)
    r = int(eth_call(CURVE_POOL, data, b), 16)
    return r / 1e18


# --- Simple in-memory cache of price & block info (avoid re-fetch during dev)
_CACHE_FILE = HERE / "data" / "block_cache.json"

def _load_cache():
    if _CACHE_FILE.exists():
        return json.loads(_CACHE_FILE.read_text())
    return {"eth_price": {}, "base_fee": {}}

def _save_cache(c):
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(c))


# --- Artificial pool profit test --------------------------------------------
def artificial_profit_test(pool: Cryptoswap2Coin, base_fee: int, eth_price: float,
                           collat_wei: int, debt_wei: int, soft_liq_x_wei: int) -> tuple[float, int]:
    """Return (profit_usd, crvusd_out_wei_from_pool). Mirrors real sim's
    profit_test but the only route is the artificial pool."""
    missing = debt_wei - soft_liq_x_wei
    # Sell `collat_wei` CRV (coin 1) into the pool → crvUSD (coin 0)
    try:
        dy_wei = pool.get_dy(1, 0, int(collat_wei))
    except Exception:
        return -1.0, 0
    gain = (dy_wei - missing) / 1e18
    gas_cost = (GAS_VIA_CRVUSD * eth_price * base_fee) / 1e18
    profit = gain - gas_cost
    # Cheap-gas side route for small dumps (mirrors TS's 850k-gas UniV3 path).
    if dy_wei / 1e18 <= USDT_ROUTE_MAX_USD:
        gas_cheap = (GAS_VIA_USDT * eth_price * base_fee) / 1e18
        profit = max(profit, gain - gas_cheap)
    return profit, dy_wei


def get_f_remove(frac_wei: int, health_limit_wei: int) -> int:
    """Exact port of Controller._get_f_remove (controllerCRVLong.py:1109).

        f_remove = ((1 + h/2)/(1 + h) * (1 - frac) + frac) * frac

    A partial liquidation withdraws a SMALLER share of the position than the
    share of debt it repays, so the liquidator's discount on a partial is worse
    than on a full one. Full liquidation (frac = 1e18) returns 1e18 exactly.
    """
    ONE = 10 ** 18
    if frac_wei >= ONE:
        return ONE
    f = ((ONE + health_limit_wei // 2) * (ONE - frac_wei)) // (ONE + health_limit_wei)
    return ((f + frac_wei) * frac_wei) // ONE


def best_partial_liquidation(pool: Cryptoswap2Coin, base_fee: int, eth_price: float,
                             collat_wei: int, debt_wei: int, soft_liq_x_wei: int,
                             discount_wei: int,
                             gas_usd: float | None = None) -> tuple[float, int, int, int]:
    """Profit-maximising `frac` for Controller.liquidate_extended.

    Real liquidators are not forced to take a whole position. `liquidate_extended`
    takes `frac` (1e18 = 100%), repays `debt*frac`, and withdraws `f_remove` of
    the user's x AND y. For a position that is large relative to the venue, the
    full dump is loss-making on slippage while a slice is still profitable — so
    an all-or-nothing test reports "unliquidatable" where the real market would
    grind the position down over successive blocks.

    Profit(frac) = dy(y·f_remove) + x·f_remove - debt·frac - gas
    Revenue is concave in frac (slippage), cost is linear -> ternary search.

    Returns (profit_usd, frac_wei, f_remove_wei, crvusd_out_wei).
    """
    ONE = 10 ** 18

    def profit_at(frac: int) -> tuple[float, int, int]:
        if frac <= 0:
            return -1.0, 0, 0
        fr = get_f_remove(frac, discount_wei)
        y_take = collat_wei * fr // ONE
        x_take = soft_liq_x_wei * fr // ONE
        d_repay = debt_wei * frac // ONE
        if y_take <= 0 or d_repay <= 0:
            return -1.0, fr, 0
        try:
            dy = pool.get_dy(1, 0, int(y_take))
        except Exception:
            return -1.0, fr, 0
        gain = (dy + x_take - d_repay) / 1e18
        if gas_usd is not None:          # flat per-tx dollar cost
            return gain - gas_usd, fr, dy
        gas = (GAS_VIA_CRVUSD * eth_price * base_fee) / 1e18
        p = gain - gas
        if dy / 1e18 <= USDT_ROUTE_MAX_USD:
            p = max(p, gain - (GAS_VIA_USDT * eth_price * base_fee) / 1e18)
        return p, fr, dy

    lo, hi = 0, ONE
    for _ in range(50):
        if hi - lo < ONE // 10_000:
            break
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        if profit_at(m1)[0] < profit_at(m2)[0]:
            lo = m1
        else:
            hi = m2
    cands = [(lo + hi) // 2, ONE]          # always also test the full liquidation
    best = max(cands, key=lambda f: profit_at(f)[0])
    p, fr, dy = profit_at(best)
    return p, best, fr, dy


def apply_swap_to_pool(pool, crv_in_wei: int, crvusd_out_wei: int):
    """Liquidator dumps CRV into the venue. `crvusd_out_wei` is the quote the
    caller already obtained — venue.exec recomputes the identical dy from the
    same state, updates balances and re-tunes the invariant."""
    pool.exec(1, 0, crv_in_wei)


def artificial_crv_spot(pool) -> float:
    """Marginal price of CRV in crvUSD from the artificial pool — used for
    bad-debt collateral valuation, mirroring real sim's getCrvPrice()."""
    # Quote a tiny CRV amount so the marginal price is stable.
    tiny = 10 ** 15  # 0.001 CRV
    dy = pool.get_dy(1, 0, tiny)
    return dy / tiny  # crvUSD per CRV


def push_pool_to_spot(pool, target_spot: float,
                      tolerance: float = 5e-4) -> tuple[int, int]:
    """Inject exogenous CRV (or crvUSD) into `pool` to move its marginal spot
    to `target_spot`. Bisects on the trade amount. Returns (direction, dx_wei):
    direction is +1 for a CRV-in trade (spot ↓), -1 for a crvUSD-in trade (spot ↑),
    0 if no trade needed.

    Represents "the rest of the market" pressuring the pool along the imposed
    linear crash schedule — happens BEFORE our liquidator dumps at that block.
    Venue-generic: hypothetical trades run on a clone via venue.exec, which is
    exactly the balance-update + invariant-retune the old inline code did."""
    cur = artificial_crv_spot(pool)
    if abs(cur - target_spot) < tolerance:
        return 0, 0
    direction = +1 if cur > target_spot else -1
    i, j = (1, 0) if direction == +1 else (0, 1)

    def _spot_after(dx_wei: int) -> float:
        c = pool.clone()
        c.exec(i, j, dx_wei)
        return artificial_crv_spot(c)

    lo, hi = 1, pool.pair_balances[1 if direction == +1 else 0] * 3
    best = 0
    for _ in range(60):
        if hi <= lo:
            break
        mid = (lo + hi) // 2
        try:
            spot_at_mid = _spot_after(mid)
        except Exception:
            hi = mid
            continue
        if direction == +1:
            # We're selling CRV → want spot to go DOWN to target.
            if spot_at_mid > target_spot:
                lo = mid + 1
            else:
                hi = mid
                best = mid
        else:
            if spot_at_mid < target_spot:
                lo = mid + 1
            else:
                hi = mid
                best = mid
    if best == 0:
        return 0, 0
    pool.exec(i, j, best)
    return direction, best


def target_spot_at(elapsed_s: float) -> float:
    """Piecewise linear crash schedule:
      * elapsed < CRASH_START_OFFSET_S            → flat at CRASH_START_SPOT
      * CRASH_START_OFFSET_S ≤ e < END_OFFSET_S  → linear interp
      * elapsed ≥ CRASH_END_OFFSET_S              → flat at CRASH_END_SPOT
    """
    if elapsed_s <= CRASH_START_OFFSET_S:
        return CRASH_START_SPOT
    if elapsed_s >= CRASH_END_OFFSET_S:
        return CRASH_END_SPOT
    dur = CRASH_END_OFFSET_S - CRASH_START_OFFSET_S
    frac = (elapsed_s - CRASH_START_OFFSET_S) / dur
    return CRASH_START_SPOT - (CRASH_START_SPOT - CRASH_END_SPOT) * frac


def hhmm_utc(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tvl-multiplier", type=float, default=DEFAULT_TVL_MULTIPLIER,
                    help="Multiplier applied to the pair-only TVL at 21:10 (default 1.5)")
    ap.add_argument("--discount", type=float, default=DEFAULT_DISCOUNT_PCT,
                    help="Liquidation discount in percent (fractional OK; needs matching precompute)")
    ap.add_argument("--precompute-file", type=Path, default=None,
                    help="Override the default precompute path — use this to swap in an "
                         "EMA-of-pool-spot precompute produced by build_ema_precompute.py")
    ap.add_argument("--out", type=Path, default=None,
                    help="Override the output JSON path")
    ap.add_argument("--spot-file", type=Path, default=None,
                    help="Per-block pool-spot override {block: price_1e18} — replaces the "
                         "linear crash schedule (one-off experiments; UI never passes this)")
    ap.add_argument("--basis", choices=["precompute", "onchain"], default="precompute",
                    help="Bad-debt basis. 'precompute' (default, UI): health<=0 users at the "
                         "swept discount. 'onchain': Controller.users_to_liquidate() at the "
                         "market's real discount — what the TS reference uses.")
    ap.add_argument("--accounting-file", type=Path, default=None,
                    help="Precompute swept at the MARKET discount → bad-debt accounting basis "
                         "(TS structure: settle at swept discount, account over market-flagged "
                         "users, net sum clamped at zero). Overrides --basis/--no-floor.")
    ap.add_argument("--no-floor", action="store_true",
                    help="Sum debt-x-spot*y net across users (TS behaviour) instead of "
                         "flooring each user at zero")
    ap.add_argument("--accounting-floor", action="store_true",
                    help="Floor each accounting-basis user at zero instead of net-summing. "
                         "REQUIRED when --accounting-file is a whole-book sweep (discount 100), "
                         "because then the basis contains healthy users whose negative terms "
                         "would otherwise cancel real shortfalls. Bad debt = UNBACKED debt = "
                         "sum of per-user max(0, debt - x - spot*y); with a discount-independent "
                         "basis this makes the swept discount enter ONLY through settlements.")
    ap.add_argument("--A-raw", type=float, default=270,
                    help="Cryptoswap A parameter (raw, pre-multiplier; on-chain "
                         "A() = A_raw x N^N x 10,000). Default 270.")
    ap.add_argument("--pool-type", choices=["cryptoswap", "stableswap", "stableswap-ng"],
                    default="cryptoswap",
                    help="Venue invariant. Engines ported from the 484/484 wei-exact "
                         "global-sim-ui C++ (verified here by verify_venues.py: 15/15 "
                         "EXACT vs 3pool / USDe-USDC NG / TriCRV).")
    ap.add_argument("--n-coins", type=int, default=2,
                    help="cryptoswap only: 2 or 3 (Curve has no other sizes). "
                         "TVL stays PAIR-ONLY; N=3 seeds a balanced passive third coin.")
    ap.add_argument("--ss-A", type=float, default=500,
                    help="stableswap/-ng only: on-chain A() (RAW — no multiplier).")
    ap.add_argument("--crash-start-spot", type=float, default=DEFAULT_CRASH_START_SPOT)
    ap.add_argument("--crash-end-spot",   type=float, default=DEFAULT_CRASH_END_SPOT)
    ap.add_argument("--crash-start-offset-s", type=float, default=DEFAULT_CRASH_START_OFFSET_S,
                    help="Seconds after window-start (21:05 UTC) at which the linear crash begins")
    ap.add_argument("--crash-duration-s",     type=float, default=DEFAULT_CRASH_DURATION_S,
                    help="Duration of the linear crash in seconds")
    ap.add_argument("--tvl-usd", type=float, default=None,
                    help="Absolute pool TVL in USD (overrides --tvl-multiplier if given)")
    ap.add_argument("--horizon-min", type=float, default=None,
                    help="Total simulated minutes across the 110 steps (must match the "
                         "value given to build_ema_precompute). Default = the real "
                         "21.8-minute block window.")
    ap.add_argument("--partial-liq", action="store_true",
                    help="Allow Controller.liquidate_extended-style PARTIAL liquidations: each "
                         "block a liquidator takes the profit-maximising `frac` of a candidate "
                         "instead of an all-or-nothing whole-position dump. Required for any "
                         "position that is large relative to the venue, otherwise it reads as "
                         "unliquidatable at every discount and the discount lever does nothing.")
    args = ap.parse_args()

    accounting_by_block = None
    if args.accounting_file:
        accounting_by_block = {int(r["block"]): r["candidates"]
                               for r in json.loads(args.accounting_file.read_text())}
        print(f"[synth] accounting basis: {args.accounting_file.name} "
              f"({sum(len(v) for v in accounting_by_block.values())} flagged rows)")

    spot_by_block = None
    if args.spot_file:
        spot_by_block = {int(k): int(v) / 1e18
                         for k, v in json.loads(args.spot_file.read_text()).items()}
        print(f"[synth] spot-file override: {len(spot_by_block)} blocks "
              f"(${min(spot_by_block.values()):.4f}..${max(spot_by_block.values()):.4f})")

    # Propagate CLI values to module globals so target_spot_at() sees them.
    global CRASH_START_OFFSET_S, CRASH_END_OFFSET_S, CRASH_START_SPOT, CRASH_END_SPOT, POOL_INIT_CRV_PRICE
    CRASH_START_OFFSET_S = args.crash_start_offset_s
    CRASH_END_OFFSET_S   = args.crash_start_offset_s + args.crash_duration_s
    CRASH_START_SPOT     = args.crash_start_spot
    CRASH_END_SPOT       = args.crash_end_spot
    POOL_INIT_CRV_PRICE  = args.crash_start_spot

    t0 = time.time()

    # -- Load precompute ---------------------------------------------------
    pre_path = args.precompute_file or precompute_for(args.discount)
    print(f"[synth] loading precompute {pre_path}")
    pre_arr = json.loads(pre_path.read_text())
    by_block = {int(r["block"]): r["candidates"] for r in pre_arr}
    # Live whole-book (x, y) per block — emitted by sweep_precompute for every
    # user with debt, healthy or not, so the composition series has no gaps.
    # Missing on precomputes from a pre-book_x binary -> rows carry nulls.
    book_by_block = {int(r["block"]): (int(r["book_x"]), int(r["book_y"]))
                     for r in pre_arr if "book_x" in r}
    # Per-band [n, x, y, p_up] snapshots (non-empty bands, post-arb) for the
    # UI's 3D bands chart. Same staleness caveat as book_x above.
    bands_by_block = {int(r["block"]): r["bands"] for r in pre_arr if "bands" in r}
    n_blocks = TO_BLOCK - FROM_BLOCK + 1
    print(f"[synth] {n_blocks} blocks in range; {sum(len(v) for v in by_block.values())} total candidates")

    # -- Initialize artificial pool ----------------------------------------
    if args.tvl_usd is not None:
        pool_tvl_usd = args.tvl_usd
        print(f"[synth] artificial pool TVL (explicit) = ${pool_tvl_usd:,.0f}")
    else:
        tvl_at_2110 = load_pool_tvl_at_2110()
        pool_tvl_usd = tvl_at_2110 * args.tvl_multiplier
        print(f"[synth] pool TVL @21:10 (pair-only) = ${tvl_at_2110:,.0f}")
        print(f"[synth] × {args.tvl_multiplier} → artificial pool TVL = ${pool_tvl_usd:,.0f}")
    A_ui = args.ss_A if args.pool_type.startswith("stableswap") else args.A_raw
    print(f"[synth] venue = {args.pool_type}"
          f"{f'/{args.n_coins}' if args.pool_type == 'cryptoswap' else ''}"
          f"   init CRV price = ${POOL_INIT_CRV_PRICE}   A = {A_ui}")

    pool = make_venue(args.pool_type, args.n_coins,
                      int(pool_tvl_usd * 1e18),
                      int(POOL_INIT_CRV_PRICE * 1e18), A_ui)
    # Freshly-fitted cryptoswap has marginal spot < price_scale due to fee
    # overhead. Snap marginal spot to the crash-start value with a small
    # exogenous crvUSD-in trade so t=0 lines up with the linear schedule.
    push_pool_to_spot(pool, CRASH_START_SPOT)
    print(f"[synth] after init snap: marginal spot = ${artificial_crv_spot(pool):.4f}")

    # -- Real curve pool SPOT per block (for the crvPrice term in bad-debt) --
    real_spot_by_block = {r["block"]: r["spot"] for r in json.loads(REAL_SPOT_FILE.read_text())}
    print(f"[synth] loaded real curve-pool spot for {len(real_spot_by_block)} blocks "
          f"(range ${min(real_spot_by_block.values()):.4f}..${max(real_spot_by_block.values()):.4f})")

    # -- On-chain users_to_liquidate per block (bad-debt basis, matches real sim) --
    u2l_by_block = {r["blockNumber"]: r["users"]
                    for r in json.loads(ONCHAIN_U2L_FILE.read_text())}

    # -- Cache for block info + prices (avoid slow refetch) ----------------
    cache = _load_cache()
    def get_block_ctx(b: int) -> tuple[int, int, float]:
        sb = str(b)
        if sb in cache["base_fee"]:
            return cache["base_fee"][sb][0], cache["base_fee"][sb][1], cache["eth_price"][sb]
        bi = block_info(b)
        ep = eth_price_at(b)
        cache["base_fee"][sb] = [bi["timestamp"], bi["baseFeePerGas"]]
        cache["eth_price"][sb] = ep
        return bi["timestamp"], bi["baseFeePerGas"], ep

    # -- Sweep loop --------------------------------------------------------
    rows = []
    settled: set[str] = set()
    ts0, _, _ = get_block_ctx(FROM_BLOCK)   # crash-start timestamp

    # Stretched timeline: same 110 steps, more seconds between them. Must use
    # the identical formula to build_ema_precompute.timeline() or the schedule
    # and the oracle would disagree about what time each block is.
    sim_ts = None
    if args.horizon_min is not None:
        _n = TO_BLOCK - FROM_BLOCK + 1
        _dt = args.horizon_min * 60.0 / (_n - 1)
        sim_ts = {FROM_BLOCK + i: int(round(ts0 + i * _dt)) for i in range(_n)}
        print(f"[synth] horizon {args.horizon_min:g} min -> {_dt:.0f}s per step")

    # Partial-liquidation bookkeeping. The C++ replay evolves every position
    # under soft-liquidation on its FULL size (it knows nothing about our hard
    # liquidations), so we carry two multipliers per user and scale the
    # precompute's per-block (x, y, debt) by them:
    #   rem_debt[u] = 1 - Σ frac        (debt still outstanding)
    #   rem_coll[u] = 1 - Σ f_remove    (x and y still in the user's bands)
    # Scaling is exact for the x/y split because soft-liquidation acts
    # proportionally across a user's bands; what it misses is the second-order
    # effect of the already-removed collateral on subsequent soft-liq depth.
    rem_debt: dict[str, float] = {}
    rem_coll: dict[str, float] = {}
    disc_wei = int(args.discount / 100.0 * 1e18)
    y0_tokens: float | None = None      # book collateral before any conversion

    def scaled(c: dict) -> tuple[int, int, int]:
        """(y, debt, x) for candidate `c` after prior partial liquidations."""
        u = c["user"].lower()
        fd, fc = rem_debt.get(u, 1.0), rem_coll.get(u, 1.0)
        return (int(int(c["y"]) * fc), int(int(c["debt"]) * fd), int(int(c["x"]) * fc))

    for i in range(n_blocks):
        b = FROM_BLOCK + i
        ts, base_fee, eth_price = get_block_ctx(b)
        if sim_ts is not None:
            ts = sim_ts[b]          # gas/eth stay REAL; only the clock stretches

        # STEP 1 — exogenous market pressure pushes the pool spot along the
        # linear crash schedule (63¢ → 27¢ over 9 min, then flat at 27¢).
        target = spot_by_block.get(b) if spot_by_block else target_spot_at(ts - ts0)
        if target is None:
            target = target_spot_at(ts - ts0)
        push_pool_to_spot(pool, target)

        candidates = by_block.get(b, [])
        # Same filters as real sim — but under --partial-liq the dust thresholds
        # must be applied to what is LEFT of the position, not its original size.
        if args.partial_liq:
            fresh = []
            for c in candidates:
                if c["user"].lower() in settled:
                    continue
                _y, _d, _x = scaled(c)
                if _y / 1e18 > 10 and _d / 1e18 > 10:
                    fresh.append(c)
        else:
            fresh = [c for c in candidates
                     if c["user"] not in settled
                     and (int(c["y"]) / 1e18) > 10
                     and (int(c["debt"]) / 1e18) > 10]

        n_profitable = 0
        # This block's executed hard-liquidation volume (debt repaid, $) and the
        # liquidators' net profit — per-block series for the UI's hard-liq chart.
        hard_usd = 0.0
        hard_profit = 0.0
        for pos in fresh:
            u = pos["user"].lower()
            if args.partial_liq:
                y, d, x = scaled(pos)
                if y <= 0 or d <= 0:
                    settled.add(u)
                    continue
                profit, frac, f_rem, dy_wei = best_partial_liquidation(
                    pool, base_fee, eth_price, y, d, x, disc_wei)
                if profit <= 0:
                    continue
                y_take = y * f_rem // 10 ** 18
                rem_debt[u] = rem_debt.get(u, 1.0) * (1.0 - frac / 1e18)
                rem_coll[u] = rem_coll.get(u, 1.0) * (1.0 - f_rem / 1e18)
                n_profitable += 1
                hard_usd += d * frac / 1e18 / 1e18
                hard_profit += profit
                if rem_debt[u] <= 1e-9:
                    settled.add(u)
                apply_swap_to_pool(pool, y_take, dy_wei)
            else:
                y = int(pos["y"]); d = int(pos["debt"]); x = int(pos["x"])
                profit, dy_wei = artificial_profit_test(pool, base_fee, eth_price, y, d, x)
                if profit > 0:
                    settled.add(u)
                    n_profitable += 1
                    hard_usd += d / 1e18
                    hard_profit += profit
                    # DRAIN the pool: liquidator got dy_wei crvUSD out, pool got y CRV in.
                    apply_swap_to_pool(pool, y, dy_wei)

        # STEP 2b — the exogenous market re-absorbs the liquidators' dump within
        # the block: restore the pool's marginal to the schedule target before
        # valuation. Without this, a settlement's transient price impact (huge
        # for small pools — one dump can halve a $100k pool's spot) devalues
        # every OTHER user's collateral in the same block, creating phantom
        # bad-debt spikes at block 1 and during the crash leg.
        if n_profitable > 0:
            push_pool_to_spot(pool, target)

        # Bad-debt formula: fully self-contained inside the sim's own world.
        # Basis  = precompute candidates at this block (health <= 0 under the
        #          SIM's own oracle — every knob the user changes flows into
        #          these), filtered to remove anyone the sim already settled.
        # Value  = using the artificial pool's current marginal spot (not the
        #          real Curve pool spot). Together this makes bad-debt respond
        #          to inputs: a rising or flat pool → few/no underwater users →
        #          near-zero bad debt, as expected. The on-chain measured line
        #          in the UI stays as an unchanged reference alongside.
        crv_spot_artif = artificial_crv_spot(pool)
        crv_spot_real  = real_spot_by_block.get(b)   # kept only for display in output rows
        if accounting_by_block is not None:
            # TS structure: settlements happen at the SWEPT discount (candidates
            # above), but bad debt is accounted over users the MARKET flags
            # (market-discount sweep), net-summed and clamped at zero. This is
            # exactly what the TS chart does: u2l (market 8%) minus settled(D).
            basis = accounting_by_block.get(b, [])
            remaining = [c for c in basis if c["user"].lower() not in settled]
            if args.partial_liq:
                # Account only the part of each position that has NOT been
                # hard-liquidated away yet.
                _t = []
                for c in remaining:
                    _y, _d, _x = scaled(c)
                    _t.append(_d / 1e18 - _x / 1e18 - crv_spot_artif * (_y / 1e18))
            else:
                _t = [int(c["debt"]) / 1e18 - int(c["x"]) / 1e18
                      - crv_spot_artif * (int(c["y"]) / 1e18) for c in remaining]
            if args.accounting_floor:
                bad_debt = round(sum(max(0.0, t) for t in _t))
            else:
                bad_debt = round(max(0.0, sum(_t)))
        else:
            basis = candidates if args.basis == "precompute" else u2l_by_block.get(b, [])
            remaining = [c for c in basis if c["user"].lower() not in settled]
            # Per-user floor at zero: bad debt is UNBACKED debt. A user whose
            # collateral at spot exceeds their debt contributes 0, not a negative
            # (LLAMMA's health test is conservative — get_x_down + discount — so it
            # flags users who are still over-collateralized at spot; without the
            # floor those users offset real shortfalls and totals can go negative).
            # --no-floor reproduces the TS reference's net sum instead.
            _terms = (int(c["debt"]) / 1e18 - int(c["x"]) / 1e18
                      - crv_spot_artif * (int(c["y"]) / 1e18) for c in remaining)
            bad_debt = round(sum(_terms if args.no_floor else (max(0.0, t) for t in _terms)))

        # Position composition after this block's soft-liq + hard-liq removals.
        # The C++ book totals know nothing about our hard liquidations, so scale
        # by the same multipliers `scaled()` uses. Exact when all removals hit a
        # single user (the UI's single-borrower book); with several affected
        # users the per-user split of the book total is unknown, so the series
        # stays unscaled there (book-level approximation).
        bk = book_by_block.get(b)
        comp_scale = 1.0
        debt_scale = 1.0
        _rm_users = set(rem_coll) | {u for u in settled}
        if len(_rm_users) == 1:
            _u = next(iter(_rm_users))
            comp_scale = rem_coll.get(_u, 0.0 if _u in settled else 1.0)
            # Debt shrinks on its own schedule (frac), not with the collateral
            # (f_remove) — needed so the UI can state equity = position - debt.
            debt_scale = rem_debt.get(_u, 0.0 if _u in settled else 1.0)
        comp_lend = round(bk[0] / 1e18 * comp_scale) if bk else None
        comp_coll_t = bk[1] / 1e18 * comp_scale if bk else None
        if bk is not None and y0_tokens is None:
            y0_tokens = bk[1] / 1e18          # collateral before any conversion

        # What liquidation protection COSTS the borrower, cumulative to here:
        # the retained position valued as if its collateral had never been
        # converted, minus what it is actually worth now. This is the LLAMMA
        # round trip (converted out low, back in high) plus AMM fees — far more
        # than the sliver the arbitrageur books as profit, which is only the
        # edge on each individual trade. Baseline carries the same hard-liq
        # scale as the position so the two are compared like for like; at t=0
        # nothing has converted and it is exactly 0.
        sl_user_loss = None
        if bk is not None and y0_tokens is not None:
            sl_user_loss = round(comp_scale * y0_tokens * crv_spot_artif
                                 - (comp_coll_t * crv_spot_artif + comp_lend))
        # Bands for the 3D view: [n, x_usd, y_tokens, p_up], hard-liq scaled
        # by the same factor (removals act proportionally across bands).
        row_bands = [[int(n), round(int(x) / 1e18 * comp_scale, 2),
                      round(int(y) / 1e18 * comp_scale, 4), round(int(pu) / 1e18, 6)]
                     for n, x, y, pu in bands_by_block.get(b, [])] \
                    if b in bands_by_block else None

        rows.append({
            "blockNumber": b,
            "LiquidationDiscount": args.discount,
            "timestamp": ts,
            "date": hhmm_utc(ts),
            "elapsed_s": ts - ts0,
            "badDebt": bad_debt,
            "comp_lend_usd":    comp_lend,
            "comp_coll_tokens": round(comp_coll_t, 2) if comp_coll_t is not None else None,
            "comp_coll_usd":    round(comp_coll_t * crv_spot_artif) if comp_coll_t is not None else None,
            "real_curve_spot": round(crv_spot_real, 6),
            "target_spot":     round(target, 6),
            "pool_crv_spot":   round(crv_spot_artif, 6),
            "pool_crvusd_bal": round(pool.pair_balances[0] / 1e18, 0),
            "pool_crv_bal":    round(pool.pair_balances[1] / 1e18, 0),
            "profitable_this_block": n_profitable,
            "settled_total": len(settled),
            "hardLiqUsd":    round(hard_usd),
            "hardLiqProfit": round(hard_profit),
            "slUserLoss":    sl_user_loss,
            "debtFrac":      round(debt_scale, 8),
            "bands": row_bands,
        })

        if (i + 1) % 10 == 0 or i == n_blocks - 1:
            print(f"  block {b} ({rows[-1]['date']}) badDebt=${bad_debt:>10,}  "
                  f"real_spot=${crv_spot_real:.4f}  pool_spot=${crv_spot_artif:.4f}  "
                  f"pool=[${pool.pair_balances[0]/1e18:.0f}, {pool.pair_balances[1]/1e18:.0f}]  "
                  f"settled={len(settled)}")

    _save_cache(cache)

    tag = f"{args.tvl_multiplier:g}x".replace(".", "p")
    # If we used a custom precompute (EMA-oracle mode), infer a suffix from its
    # filename (e.g., "ema_300s") so results don't clobber the baseline runs.
    suffix = ""
    if args.precompute_file:
        stem = args.precompute_file.stem   # e.g. "precompute_ema_300s_12pct"
        if stem.startswith("precompute_"):
            # Grab the middle segment(s): "ema_300s"
            parts = stem.split("_")
            # parts = ["precompute", "ema", "300s", "12pct"]
            if len(parts) >= 3:
                suffix = "_" + "_".join(parts[1:-1])
    out = args.out or (
        HERE / "results" / f"artificial_bad_debt_{args.discount}pct_tvl_{tag}{suffix}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"\n[synth] wrote {out}  ({time.time()-t0:.1f}s)")
    print(f"[synth] final pool state:")
    print(f"        crvUSD:  ${pool.pair_balances[0]/1e18:,.0f}")
    print(f"        CRV:      {pool.pair_balances[1]/1e18:,.0f}   (spot ${crv_spot_artif:.4f})")
    print(f"[synth] users settled by artificial pool: {len(settled)}")
    for u in sorted(settled):
        print(f"[synth] settled: {u}")


if __name__ == "__main__":
    main()
