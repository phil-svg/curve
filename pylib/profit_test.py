"""Profit test — port of getSingleHardLiquidationTest in old code/…/Crv_HardLiquidations.ts.

Given (block, ethPrice, collat_wei, debt_wei, softLiq_x_wei), asks:
would a hard-liquidator net anything after gas, going via curve pool OR
via UniV3 CRV→USDT? Returns profitAfterGasBeforeTip (float, USD).

RPC pricing calls are cheap so we do them at profit-test time — same as TS.
"""
from __future__ import annotations
from common import call_uint256, sel, encode_int, eth_call
from uniswap_v3 import get_dy_exact_input


# Contracts (mainnet)
CRVUSD_CRV_POOL = "0x4eBdF703948dDCEA3B11f675B4D1Fba9d2414A14"  # crvUSD/WETH/CRV tricrypto
UNIV3_CRV_USDT_0300 = "0x07B1c12BE0d62fe548a2b4b025Ab7A5cA8DEf21E"
CRV_ADDR  = "0xD533a949740bb3306d119CC777fa900bA034cd52"
USDT_ADDR = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
CRV_DEC = 18
USDT_DEC = 6

# UniV3 CRV/USDT pool: token0 = CRV, token1 = USDT, fee = 3000, tickSpacing = 60.
UNI_FEE_PIPS = 3000
UNI_TICK_SPACING = 60

GAS_VIA_CRVUSD = 7_250_000
GAS_VIA_USDT   =   850_000


def pool_get_dy(i: int, j: int, dx_wei: int, block: int) -> int:
    """Curve tricrypto get_dy(uint256 i, uint256 j, uint256 dx) → uint256."""
    data = sel("get_dy(uint256,uint256,uint256)") + encode_int(i) + encode_int(j) + encode_int(dx_wei)
    res = eth_call(CRVUSD_CRV_POOL, data, block)
    return int(res, 16)


def eth_price_at(block: int) -> float:
    """Same as TS getEthPrice: pool.get_dy(1, 0, 1e18) / 1e18."""
    dy = pool_get_dy(1, 0, 10**18, block)
    return dy / 1e18


def profit_test(
    block: int, base_fee_per_gas: int, eth_price: float,
    collat_wei: int, debt_wei: int, soft_liq_x_wei: int,
) -> float:
    """Reproduce TS getSingleHardLiquidationTest.

    Returns profitAfterGasBeforeTip in USD (float). All arithmetic mirrors TS.
    """
    missing = int(debt_wei) - int(soft_liq_x_wei)

    # 1. Sell collateral for crvUSD via the tricrypto pool.
    dy_crvusd_pool_wei = pool_get_dy(2, 0, int(collat_wei), block)
    # TS does: (dy - missing) / 1e18   — where dy is a bigint stringified into JS Number,
    # then subtracted from missing (also a bigint stringified into Number), divided by 1e18.
    # In IEEE-754, that's Number(dy - missing) / 1e18.  Replicate:
    crv_usd_gain_curve_pool = (dy_crvusd_pool_wei - missing) / 1e18

    # 2. Sell collateral for USDT via UniV3 CRV/USDT 0.3% pool.
    amount_in_human = collat_wei / 1e18
    usdt_returned = get_dy_exact_input(
        UNIV3_CRV_USDT_0300,
        CRV_ADDR, CRV_DEC,
        USDT_ADDR, USDT_DEC,
        amount_in_human, block,
        fee_pips=UNI_FEE_PIPS, tick_spacing=UNI_TICK_SPACING,
        token0=CRV_ADDR, token1=USDT_ADDR,
    )
    # TS: crvUSDgainUniswapV3 = usdtReturnedUniswapV3 - missingCrvUSDAmounttoFullyRepayRaw / 1e18
    crv_usd_gain_univ3 = usdt_returned - missing / 1e18

    tx_gas_usage = GAS_VIA_CRVUSD if crv_usd_gain_curve_pool > crv_usd_gain_univ3 else GAS_VIA_USDT
    tx_cost_for_gas = (tx_gas_usage * eth_price * base_fee_per_gas) / 1e18
    return max(crv_usd_gain_curve_pool, crv_usd_gain_univ3) - tx_cost_for_gas
