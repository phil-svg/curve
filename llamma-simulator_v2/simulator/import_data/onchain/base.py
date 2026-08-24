import asyncio
import datetime as dt
import gzip
import json
import os
import logging
from enum import Enum
from typing import Any, Callable

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from simulator.settings import Pair

from simulator.import_data.base import BaseImporter
from simulator.import_data.onchain.contracts import (
    WSTETH_PRICE_DECIMALS,
    PriceFunction,
    get_wsteth_price_function,
)

logger = logging.getLogger(__name__)


class OnchainContract(Enum):
    WSTETH = (Pair.WSTETH_ETH, get_wsteth_price_function, WSTETH_PRICE_DECIMALS)

    def __init__(
        self,
        pair: Pair,
        builder: Callable[[AsyncWeb3], PriceFunction],
        decimals: int,
    ) -> None:
        self.pair = pair
        self.builder = builder
        self.decimals = decimals

    @classmethod
    def for_pair(cls, pair: Pair) -> "OnchainContract":
        for item in cls:
            if item.pair == pair:
                return item
        raise ValueError(f"Unsupported on-chain pair {pair}")


async def _get_price_at_block(block_number: int, price_function: PriceFunction, decimals: int) -> float:
    raw_price = await price_function.call(block_identifier=block_number)
    if block_number % 100 == 0:
        logger.info(f"Imported prices for block %r", block_number)
    return float(raw_price) / (10**decimals)


class OnchainImporter(BaseImporter):
    name = "onchain"
    interval_seconds = 60
    start: dt.datetime = dt.datetime(2025, 10, 10, tzinfo=dt.timezone.utc)
    end: dt.datetime = dt.datetime(2025, 10, 11, tzinfo=dt.timezone.utc)

    rpc_url = os.getenv("ONCHAIN_RPC_URL", "http://localhost:8545")
    anchor_block = 23543614
    max_concurrency = 50

    @classmethod
    async def get_klines(cls, price_function: PriceFunction, price_decimals: int) -> list[list[Any]]:
        w3 = AsyncWeb3(AsyncHTTPProvider(cls.rpc_url))
        block_number = cls.anchor_block
        block_prices: list[tuple[int, float]] = []
        sem = asyncio.Semaphore(cls.max_concurrency)

        start_ts = int(cls.start.timestamp())
        end_ts = int(cls.end.timestamp())

        while True:
            block = await w3.eth.get_block(block_number)
            timestamp = block.get("timestamp")
            if timestamp is None or timestamp >= end_ts:
                break
            if timestamp >= start_ts:
                async with sem:
                    fixed_price = await _get_price_at_block(block_number, price_function, price_decimals)
                block_prices.append((int(timestamp), fixed_price))
            block_number += 1

        if not block_prices:
            return []

        block_prices.sort(key=lambda row: row[0])
        step = cls.interval_seconds
        aligned_start = (start_ts // step) * step
        if aligned_start != start_ts:
            aligned_start += step

        candles: list[list[Any]] = []
        index = 0
        while aligned_start < end_ts:
            bucket_end = aligned_start + step
            bucket_prices: list[float] = []
            while index < len(block_prices) and block_prices[index][0] < bucket_end:
                if block_prices[index][0] >= aligned_start:
                    bucket_prices.append(block_prices[index][1])
                index += 1
            if bucket_prices:
                candles.append(
                    [
                        aligned_start,
                        bucket_prices[0],
                        max(bucket_prices),
                        min(bucket_prices),
                        bucket_prices[-1],
                        0.0,
                        0.0,
                    ]
                )
            aligned_start += step
        return candles

    @classmethod
    async def fetch(cls, pair: Pair) -> list[Any]:
        contract = OnchainContract.for_pair(pair)
        w3 = AsyncWeb3(AsyncHTTPProvider(cls.rpc_url))
        price_function = contract.builder(w3)
        return await cls.get_klines(price_function, contract.decimals)

    @classmethod
    def load(cls, pair: Pair) -> list[Any]:
        path = cls.get_data_path(pair)
        with gzip.open(path, "r") as f:
            return json.load(f)
