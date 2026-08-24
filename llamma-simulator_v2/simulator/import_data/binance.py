import asyncio
import datetime as dt
import gzip
import json
import logging
from typing import Any

import aiohttp

from simulator.settings import Pair

from .base import BaseImporter

logger = logging.getLogger(__name__)


class BinanceImporter(BaseImporter):
    name = "binance"
    interval = "1m"
    start: dt.datetime = dt.datetime(2021, 11, 1, tzinfo=dt.timezone.utc)
    end: dt.datetime = dt.datetime.now(dt.timezone.utc)

    BINANCE_BASE_URL = "https://api.binance.com"
    KLINES_PATH = "/api/v3/klines"
    chunk_minutes: int = 288  # 1440 / 5
    limit: int = 500
    concurrency: int = 8
    request_timeout: int = 30
    max_retries: int = 5
    backoff_base: float = 0.5  # seconds

    @staticmethod
    def _to_millis(d: dt.datetime) -> int:
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp() * 1000)

    @classmethod
    def _windows(cls) -> list[tuple[int, int]]:
        windows: list[tuple[int, int]] = []
        cur = cls.start
        delta = dt.timedelta(minutes=cls.chunk_minutes)
        while cur < cls.end:
            nxt = min(cur + delta, cls.end)
            windows.append((cls._to_millis(cur), cls._to_millis(nxt) - 1))
            cur = nxt
        return windows

    @classmethod
    def _base_url(cls) -> str:
        return f"{cls.BINANCE_BASE_URL}{cls.KLINES_PATH}"

    @classmethod
    async def _fetch_window(cls, session: aiohttp.ClientSession, pair: Pair, start_ms: int, end_ms: int) -> list[Any]:
        params = {
            "symbol": pair,
            "interval": cls.interval,
            "limit": str(cls.limit),
            "startTime": str(start_ms),
            "endTime": str(end_ms),
        }
        result = await cls._request_with_retries(session, cls._base_url(), params)
        logger.info(f"Fetched {pair} window for {start_ms} to {end_ms}")
        return [
            [
                r[0] // 1000,
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5]),
                float(r[7]),
            ]
            for r in result
        ]

    @classmethod
    async def _request_with_retries(cls, session: aiohttp.ClientSession, url: str, params: dict[str, str]) -> list[Any]:
        last_err: Exception | None = None
        for attempt in range(1, cls.max_retries + 1):
            try:
                async with session.get(url, params=params, timeout=cls.request_timeout) as resp:
                    if resp.status in (418, 429):
                        # Rate limited or banned; respect Retry-After if present
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            delay = float(retry_after)
                        else:
                            delay = cls.backoff_base * (2 ** (attempt - 1))
                        last_err = aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"Rate limited (status {resp.status})",
                            headers=resp.headers,
                        )
                        if attempt < cls.max_retries:
                            await asyncio.sleep(delay)
                            continue
                        resp.raise_for_status()
                    resp.raise_for_status()
                    data = await resp.json()
                    # Binance error payloads may return JSON with code/msg but 200 OK for some errors; handle conservatively
                    if isinstance(data, dict) and "code" in data and "msg" in data:
                        # Treat as retryable for server codes
                        last_err = RuntimeError(f"Binance error: {data}")
                        if attempt < cls.max_retries:
                            delay = cls.backoff_base * (2 ** (attempt - 1))
                            await asyncio.sleep(delay)
                            continue
                        raise last_err
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                if attempt < cls.max_retries:
                    delay = cls.backoff_base * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
                    continue
                raise
        # Should not reach here
        assert last_err is not None
        raise last_err

    @classmethod
    async def _bounded_fetch(
        cls, sem: asyncio.Semaphore, session: aiohttp.ClientSession, pair: Pair, start_ms: int, end_ms: int
    ) -> list[Any]:
        async with sem:
            return await cls._fetch_window(session, pair, start_ms, end_ms)

    @classmethod
    async def fetch(cls, pair: Pair) -> list[Any]:
        windows = cls._windows()
        sem = asyncio.Semaphore(cls.concurrency)
        timeout = aiohttp.ClientTimeout(total=None)
        connector = aiohttp.TCPConnector(limit=0)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks = [cls._bounded_fetch(sem, session, pair, s, e) for s, e in windows]
            results: list[list[Any]] = await asyncio.gather(*tasks)
        data: list[Any] = []
        for chunk in results:
            data.extend(chunk)
        return data

    @classmethod
    def load(cls, pair: Pair) -> list[Any]:
        path = cls.get_data_path(pair)
        with gzip.open(path, "r") as f:
            return json.load(f)
