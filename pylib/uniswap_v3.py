"""UniV3 exact-input quoter — Python port of old code/crvUSDsimulation_smol/
src/uniswap/V3/Math.ts. Big-int arithmetic (Python `int` is unbounded).

Fetches pool state (slot0, liquidity, fee, tickSpacing, token0, token1) and
per-tick liquidityNet via eth_call at the target block. Then walks tick
crossings until the input is spent or MAX_STEPS is hit — same algorithm and
same rounding as the TS ref, so results should be byte-identical.
"""
from __future__ import annotations
from typing import Callable
from common import eth_call, sel, encode_int, hex_int


Q32   = 1 << 32
Q96   = 1 << 96
Q128  = 1 << 128
Q256  = 1 << 256
TWO_POW_160_MIN1 = (1 << 160) - 1
ONE_E6 = 1_000_000

MIN_TICK = -887272
MAX_TICK =  887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

POWERS = [
    0xfffcb933bd6fad37aa2d162d1a594001,
    0xfff97272373d413259a46990580e213a,
    0xfff2e50f5f656932ef12357cf3c7fdcc,
    0xffe5caca7e10e4e61c3624eaa0941cd0,
    0xffcb9843d60f6159c9db58835c926644,
    0xff973b41fa98c081472e6896dfb254c0,
    0xff2ea16466c96a3843ec78b326b52861,
    0xfe5dee046a99a2a811c461f1969c3053,
    0xfcbe86c7900a88aedcffc83b479aa3a4,
    0xf987a7253ac413176f2b074cf7815e54,
    0xf3392b0822b70005940c7a398e4b70f3,
    0xe7159475a2c29b7443b29c7fa6e889d9,
    0xd097f3bdfd2022b8845ad8f792aa5825,
    0xa9f746462d870fdf8a65dc1f90e061e5,
    0x70d869a156d2a1b890bb3df62baf32f7,
    0x31be135f97d08fd981231505542fcfa6,
    0x09aa508b5b7a84e1c677de54f3e99bc9,
    0x005d6af8dedb81196699c329225ee604,
    0x0002216e584f5fa1ea926041bedfe98,
    0x00000048a170391f7dc42444e8fa2,
]


def _div_ru(a: int, d: int) -> int:
    return a // d if a % d == 0 else a // d + 1


def _mul_div(a: int, b: int, d: int) -> int:
    return (a * b) // d


def _mul_div_ru(a: int, b: int, d: int) -> int:
    p = a * b
    q = p // d
    return q if p % d == 0 else q + 1


def sqrt_price_x96_from_tick(tick: int) -> int:
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError("tick out of range")
    abs_tick = -tick if tick < 0 else tick
    ratio = POWERS[0] if (abs_tick & 1) else Q128
    for i in range(1, len(POWERS)):
        if (abs_tick & (1 << i)) != 0:
            ratio = (ratio * POWERS[i]) >> 128
    if tick > 0:
        ratio = (Q256 - 1) // ratio
    r_shift = ratio >> 32
    round_up = (ratio & (Q32 - 1)) != 0
    sqp = r_shift + (1 if round_up else 0)
    if sqp < MIN_SQRT_RATIO:
        return MIN_SQRT_RATIO
    if sqp > MAX_SQRT_RATIO:
        return MAX_SQRT_RATIO
    return sqp


def _next_sqrt_from_amount0_ru(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    if amount == 0:
        return sqrt_p
    numerator1 = liquidity << 96
    if add:
        product = amount * sqrt_p
        denom = numerator1 + product
        if denom >= numerator1:
            return _mul_div_ru(numerator1, sqrt_p, denom)
        return _div_ru(numerator1, numerator1 // sqrt_p + amount)
    else:
        product = amount * sqrt_p
        if not (product // amount == sqrt_p and numerator1 > product):
            raise RuntimeError("underflow amount0-sub")
        denom = numerator1 - product
        return _mul_div_ru(numerator1, sqrt_p, denom)


def _next_sqrt_from_amount1_rd(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    if add:
        q = (amount << 96) // liquidity if amount <= TWO_POW_160_MIN1 else _mul_div(amount, Q96, liquidity)
        return sqrt_p + q
    else:
        q = _div_ru(amount << 96, liquidity) if amount <= TWO_POW_160_MIN1 else _mul_div_ru(amount, Q96, liquidity)
        if sqrt_p <= q:
            raise RuntimeError("sqrt underflow")
        return sqrt_p - q


def _amt0_delta(sqrt_a: int, sqrt_b: int, L: int, round_up: bool) -> int:
    sa, sb = (sqrt_b, sqrt_a) if sqrt_a > sqrt_b else (sqrt_a, sqrt_b)
    if sa == 0:
        raise RuntimeError("sqrtA=0")
    num1 = L << 96
    num2 = sb - sa
    if round_up:
        return _div_ru(_mul_div_ru(num1, num2, sb), sa)
    return _mul_div(num1, num2, sb) // sa


def _amt1_delta(sqrt_a: int, sqrt_b: int, L: int, round_up: bool) -> int:
    sa, sb = (sqrt_b, sqrt_a) if sqrt_a > sqrt_b else (sqrt_a, sqrt_b)
    if round_up:
        return _mul_div_ru(L, sb - sa, Q96)
    return (L * (sb - sa)) // Q96


def _floor_to_spacing(tick: int, spacing: int) -> int:
    # Match TS Math.floor(tick/spacing) semantics (truncates toward -inf for negatives).
    # Python's // already floors toward -inf for negatives — but the TS code uses
    # Math.floor which also floors toward -inf. So consistent.
    import math
    return math.floor(tick / spacing) * spacing


# --------------- eth_call helpers (raw ABI, no web3.py) ---------------

def _pool_state(pool: str, block: int,
                fee_pips: int | None = None,
                tick_spacing: int | None = None,
                token0: str | None = None,
                token1: str | None = None) -> dict:
    """Return {sqrtPriceX96, currentTick, liquidity, feePips, tickSpacing, token0, token1}."""
    # slot0() returns (sqrtPriceX96 uint160, tick int24, obsIdx uint16, obsCard uint16,
    #                  obsCardNext uint16, feeProtocol uint8, unlocked bool).
    # 7 return values, but ABI-encoded as 7 × 32-byte words (right-padded/packed as ints).
    slot0_res = eth_call(pool, sel("slot0()"), block)
    hexb = slot0_res[2:]
    sqrt_p = int(hexb[0:64], 16)
    tick_raw = int(hexb[64:128], 16)
    # Solidity ABI sign-extends int24 (and any narrower signed type) to the
    # FULL 256-bit word — so negative ticks come back as 0xff…ffXXXX, and
    # sign-extension must happen from bit 255, not bit 23. Same rule applies
    # to int128, int64, int32, etc.
    if tick_raw >= (1 << 255):
        tick_raw -= 1 << 256
    liq_res = eth_call(pool, sel("liquidity()"), block)
    L = int(liq_res, 16)
    if fee_pips is None:
        fee_pips = int(eth_call(pool, sel("fee()"), block), 16)
    if tick_spacing is None:
        ts_res = eth_call(pool, sel("tickSpacing()"), block)
        ts = int(ts_res, 16)
        if ts >= (1 << 255):
            ts -= 1 << 256
        tick_spacing = ts
    if token0 is None:
        r = eth_call(pool, sel("token0()"), block)
        token0 = "0x" + r[2:][24:64]
    if token1 is None:
        r = eth_call(pool, sel("token1()"), block)
        token1 = "0x" + r[2:][24:64]
    return {
        "sqrtPriceX96": sqrt_p,
        "currentTick": tick_raw,
        "liquidity": L,
        "feePips": fee_pips,
        "tickSpacing": tick_spacing,
        "token0": token0.lower(),
        "token1": token1.lower(),
    }


def _tick_info(pool: str, tick: int, block: int) -> dict:
    """ticks(int24) returns (liquidityGross uint128, liquidityNet int128, feeGrowthOutside0X128 uint256,
    feeGrowthOutside1X128 uint256, tickCumulativeOutside int56, secondsPerLiquidityOutsideX128 uint160,
    secondsOutside uint32, initialized bool). ~8 words."""
    data = sel("ticks(int24)") + encode_int(tick)
    r = eth_call(pool, data, block)
    hexb = r[2:]
    def w(i): return hexb[i*64:(i+1)*64]
    liq_gross = int(w(0), 16)
    liq_net_raw = int(w(1), 16)
    # int128 also gets sign-extended to 256 bits by ABI encoding — extract
    # from bit 255, not bit 127.
    if liq_net_raw >= (1 << 255):
        liq_net_raw -= 1 << 256
    initialized = int(w(7), 16) != 0
    return {"liquidityGross": liq_gross, "liquidityNet": liq_net_raw, "initialized": initialized}


# --------------- Public entry point ---------------

def get_dy_exact_input(
    pool: str, token_in_addr: str, token_in_dec: int,
    token_out_addr: str, token_out_dec: int,
    amount_in_human: float, block: int,
    fee_pips: int | None = None, tick_spacing: int | None = None,
    token0: str | None = None, token1: str | None = None,
) -> float:
    """Human-units in → human-units out."""
    if not (amount_in_human > 0):
        return 0.0
    st = _pool_state(pool, block, fee_pips, tick_spacing, token0, token1)
    in_addr = token_in_addr.lower()
    out_addr = token_out_addr.lower()
    if not ((in_addr == st["token0"] and out_addr == st["token1"]) or
            (in_addr == st["token1"] and out_addr == st["token0"])):
        raise RuntimeError("tokenIn/tokenOut do not match pool token0/token1")
    zero_for_one = (in_addr == st["token0"])
    scale_in = 10 ** token_in_dec
    amount_remaining = int(round(amount_in_human * scale_in))
    sqrt_p = st["sqrtPriceX96"]
    L = st["liquidity"]
    ts = st["tickSpacing"]
    tickL = _floor_to_spacing(st["currentTick"], ts)
    tickU = tickL + ts
    sqrt_lower = sqrt_price_x96_from_tick(tickL)
    sqrt_upper = sqrt_price_x96_from_tick(tickU)
    fee = st["feePips"]

    out_acc = 0
    MAX_STEPS = 25
    steps = 0
    while amount_remaining > 0 and L > 0 and steps < MAX_STEPS:
        steps += 1
        target_sqrt = sqrt_lower if zero_for_one else sqrt_upper
        if zero_for_one:
            amount_in_to_target = _amt0_delta(target_sqrt, sqrt_p, L, True)
            amount_out_at_target = _amt1_delta(target_sqrt, sqrt_p, L, False)
        else:
            amount_in_to_target = _amt1_delta(sqrt_p, target_sqrt, L, True)
            amount_out_at_target = _amt0_delta(sqrt_p, target_sqrt, L, False)
        amount_less_fee = _mul_div(amount_remaining, ONE_E6 - fee, ONE_E6)
        if amount_less_fee >= amount_in_to_target and amount_in_to_target > 0:
            gross_in = _mul_div_ru(amount_in_to_target, ONE_E6, ONE_E6 - fee)
            amount_remaining -= gross_in
            out_acc += amount_out_at_target
            sqrt_p = target_sqrt
            boundary_tick = tickL if zero_for_one else tickU
            info = _tick_info(pool, boundary_tick, block)
            if info["initialized"]:
                if zero_for_one:
                    L -= info["liquidityNet"]
                else:
                    L += info["liquidityNet"]
            if zero_for_one:
                tickU = tickL
                tickL -= ts
            else:
                tickL = tickU
                tickU += ts
            sqrt_lower = sqrt_price_x96_from_tick(tickL)
            sqrt_upper = sqrt_price_x96_from_tick(tickU)
        else:
            if amount_less_fee == 0:
                break
            if zero_for_one:
                sqrt_next = _next_sqrt_from_amount0_ru(sqrt_p, L, amount_less_fee, True)
                amount_out = _amt1_delta(sqrt_next, sqrt_p, L, False)
                out_acc += amount_out
                amount_remaining = 0
                sqrt_p = sqrt_next
            else:
                sqrt_next = _next_sqrt_from_amount1_rd(sqrt_p, L, amount_less_fee, True)
                amount_out = _amt0_delta(sqrt_p, sqrt_next, L, False)
                out_acc += amount_out
                amount_remaining = 0
                sqrt_p = sqrt_next
            break
    scale_out = 10 ** token_out_dec
    # TS returns Number(outAcc)/Number(scaleOut) which does IEEE-754 division of
    # possibly-truncated ints. Replicate exactly.
    return float(out_acc) / float(scale_out)
