import asyncio
import gzip
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from simulator.settings import BASE_DIR, Pair

logger = logging.getLogger(__name__)

# Example DATA ETH-USD
# [
#   [
#     1499040000,          // Open time (timestamp) in seconds
#     3000.01634790,       // Open
#     3020.80000000,       // High
#     2970.01575800,       // Low
#     3010.01577100,       // Close
#     2434.19055334,       // Volume (base asset, here ETH)
#     148976.11427815,     // Quote asset volume (here USDT)
#   ],
#   ...
# ]


class BaseImporter(ABC):
    interval = "1m"

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def start(self) -> datetime: ...

    @property
    @abstractmethod
    def end(self) -> datetime: ...

    @classmethod
    def get_data_path(cls, pair: Pair) -> Path:
        # Compress for saving in repo
        return BASE_DIR / "data" / pair / f"{pair}-{cls.interval}-{cls.name}.json.gz"

    @classmethod
    @abstractmethod
    async def fetch(cls, pair: Pair) -> list[Any]: ...

    @classmethod
    def save(cls, pair: Pair, data: list[Any]) -> None:
        path = cls.get_data_path(pair)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "w") as f:
            f.write(json.dumps(data).encode("utf-8"))
        logger.info(f"Saved data to {path}.")

    @classmethod
    async def run_async(cls, pair: Pair) -> None:
        logger.info(f"Fetching data for {pair}")
        data = await cls.fetch(pair)
        data = sorted(data, key=lambda x: x[0])
        cls.save(pair, data)
        logger.info(f"Fetched data for {pair}.")

    @classmethod
    def run(cls, pair: Pair) -> None:
        asyncio.run(cls.run_async(pair))

    @classmethod
    @abstractmethod
    def load(cls, pair: Pair) -> list[Any]: ...
