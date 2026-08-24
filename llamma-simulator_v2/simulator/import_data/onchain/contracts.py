from __future__ import annotations

from typing import Any, Protocol

from web3 import AsyncWeb3

WSTETH_ADDRESS = "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
STETH_ADDRESS = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
WSTETH_PRICE_DECIMALS = 18


class PriceFunction(Protocol):
    async def call(self, *, block_identifier: int | None = None) -> Any: ...


class _WstethEthPriceFunction(PriceFunction):
    def __init__(self, w3: AsyncWeb3) -> None:
        wsteth_abi = [
            {
                "inputs": [{"internalType": "uint256", "name": "_wstETHAmount", "type": "uint256"}],
                "name": "getStETHByWstETH",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ]
        steth_abi = [
            {
                "inputs": [{"internalType": "uint256", "name": "_sharesAmount", "type": "uint256"}],
                "name": "getPooledEthByShares",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [{"internalType": "uint256", "name": "_pooledEth", "type": "uint256"}],
                "name": "getSharesByPooledEth",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            },
        ]
        wsteth_address = w3.to_checksum_address(WSTETH_ADDRESS)
        steth_address = w3.to_checksum_address(STETH_ADDRESS)
        self._wsteth = w3.eth.contract(address=wsteth_address, abi=wsteth_abi)
        self._steth = w3.eth.contract(address=steth_address, abi=steth_abi)

    async def call(self, *, block_identifier: int | None = None) -> int:
        wsteth_amount = 10**18
        if block_identifier is None:
            steth_amount = await self._wsteth.functions.getStETHByWstETH(wsteth_amount).call()
            steth_shares = await self._steth.functions.getSharesByPooledEth(steth_amount).call()
            return await self._steth.functions.getPooledEthByShares(steth_shares).call()
        steth_amount = await self._wsteth.functions.getStETHByWstETH(wsteth_amount).call(
            block_identifier=block_identifier
        )
        steth_shares = await self._steth.functions.getSharesByPooledEth(steth_amount).call(
            block_identifier=block_identifier
        )
        return await self._steth.functions.getPooledEthByShares(steth_shares).call(
            block_identifier=block_identifier
        )


def get_wsteth_price_function(w3: AsyncWeb3) -> PriceFunction:
    return _WstethEthPriceFunction(w3)
