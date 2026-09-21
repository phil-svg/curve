#!/usr/bin/env python3
"""ref_feeds.py — which price feed a market's crash replay should come from.

The Bad-Debt-Sim replays a collateral's worst real crash. The shape used to come
from one place only: minute candles of the market's venue pool on the Curve
prices API. That is the right source for a token whose only market IS that pool,
and the wrong one in two cases this module settles:

  feed          The collateral (or the token it redeems into) has a deep external
                market whose 1-minute history is already on disk
                (data/_ref_table_klines_*, built by fetch_binance_klines.py /
                build_nav_klines.py: the S.L./D.L. "kl-" datasets). A $240k
                sidechain pool printing WETH at zero for seven minutes is not
                WETH's price; ETHUSDT is. The replay is cut from the feed and the
                venue is not asked at all.

  ratio_x_feed  The venue prices the collateral in something that is not a dollar
                (pufETH in wstETH, LBTC in WBTC) while the market lends dollars.
                The venue ratio alone is flat through the very crash that matters
                (ETH -25 % moves pufETH/wstETH by nothing). When the QUOTE token
                has a feed, the replay is ratio x feed, minute by minute.

  venue         Everything else: the pool is the market. (fetch_crash_window.py
                still ranks on minute-confirmed lows and drops impossible prints.)

The plan depends on what the market LENDS: wstETH against WETH wants the ratio,
wstETH against crvUSD wants dollars. A market that lends a non-dollar keeps the
venue ratio, with one exception: the mirror market (a dollar token as collateral,
a fed token lent against it, e.g. crvUSD against WETH), whose price is 1 / feed.

The table is by (chain, token address), never by symbol. Only tokens that ARE the
fed asset are listed (WETH is ETH, tBTC is priced off BTCUSDT exactly as the
S.L./D.L. tab already does). Pegged look-alikes (frxETH, weETH, pzETH, cbETH ...)
are deliberately absent: composing them with the ETH feed would assume a peg.
"""
from __future__ import annotations

import array
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DAY = 86400

FEEDS = {
    # WETH -> binance:ETHUSDT
    ("ethereum", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"): "WETH",
    ("arbitrum", "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"): "WETH",
    ("optimism", "0x4200000000000000000000000000000000000006"): "WETH",
    # WBTC -> binance:WBTCUSDT
    ("ethereum", "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"): "WBTC",
    ("arbitrum", "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"): "WBTC",
    ("optimism", "0x68f180fcce6836688e9084f035309e29bf0a2095"): "WBTC",
    # tBTC -> binance:BTCUSDT (the dataset the S.L./D.L. tab runs tBTC on)
    ("ethereum", "0x18084fba666a33d37592fa2633fd49a74dd93a88"): "tBTC",
    ("arbitrum", "0x6c84a8f1c29108f47a79964b5fe888d4f4d0de40"): "tBTC",
    ("optimism", "0x6c84a8f1c29108f47a79964b5fe888d4f4d0de40"): "tBTC",
    # CRV -> binance:CRVUSDT
    ("ethereum", "0xd533a949740bb3306d119cc777fa900ba034cd52"): "CRV",
    ("arbitrum", "0x11cdb42b0eb46d95f990bedd4695a6e3fa034978"): "CRV",
    ("optimism", "0x0994206dfe8de6ec6920ff4d779b0d950605fb53"): "CRV",
    ("fraxtal", "0x331b9182088e2a7d6d3fe4742aba1fb231aecc56"): "CRV",
    # wstETH -> stEthPerToken x binance:ETHUSDT
    ("ethereum", "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"): "wstETH",
    ("optimism", "0x1f32b1c2345538c0c6f582fcb022739c4a194ebb"): "wstETH",
    # XAUM -> binance:PAXGUSDT per gram
    ("ethereum", "0x2103e845c5e135493bb6c2a4f0b8651956ea8682"): "XAUM",
}
# what a market lends, and what a venue may quote in, for the venue ratio to be a dollar path
DOLLAR_DEBT = {"crvUSD", "USDC", "USDC.e", "USDT", "USD₮0", "DAI", "USDS", "frxUSD"}
DOLLAR_QUOTE = DOLLAR_DEBT | {"scrvUSD"}


def feed_of(chain: str, token: str | None) -> str | None:
    return FEEDS.get((str(chain), str(token or "").lower()))


def plan(series: dict, borrowed: str | None) -> dict:
    """-> {"kind": "feed" | "ratio_x_feed" | "venue", "feed": name | None, "why": text, "invert": bool}"""
    chain = series.get("chain")
    if borrowed and borrowed not in DOLLAR_DEBT:
        # the mirror market: a dollar token as collateral, a fed token lent against it (crvUSD against WETH).
        # Its price is 1 / feed. Only when the venue really quotes in the lent token; anything else keeps the venue.
        lent = feed_of(chain, series.get("quote_addr"))
        # And only for a YOUNG venue (< 1 year of candles: the Yield Basis pools, 119 days): a venue with a long
        # record of its own (crvUSD/CRV on TriCRV, since 2024-08) reaches further back than the feeds (2025-01).
        if (series.get("base_symbol") in DOLLAR_DEBT and lent and _file(lent)
                and series.get("quote_symbol") == borrowed
                and len(series.get("candles_quote") or []) < 365):
            return {"kind": "feed", "feed": lent, "invert": True,
                    "why": f"{series.get('base_symbol')} priced in {borrowed} is one over {borrowed}'s own deep feed"}
        return {"kind": "venue", "feed": None,
                "why": f"the market lends {borrowed}, not a dollar: the venue ratio is the price that matters"}
    base = feed_of(chain, series.get("base_addr"))
    if base and _file(base):
        return {"kind": "feed", "feed": base, "why": f"{series.get('under_symbol') or series.get('base_symbol')} has its own deep feed"}
    quote = feed_of(chain, series.get("quote_addr"))
    if quote and _file(quote) and series.get("quote_symbol") not in DOLLAR_QUOTE:
        return {"kind": "ratio_x_feed", "feed": quote,
                "why": f"the venue quotes in {series.get('quote_symbol')}, the market lends dollars"}
    return {"kind": "venue", "feed": None, "why": "no reference feed for this token"}


# ---- the feeds on disk ---------------------------------------------------------------
_MEMO: dict = {}            # name -> (mtime_ns, {"t": array('q'), "c": array('d'), "source": str})


def _file(name: str):
    """(bin path, meta) of the longest binance / nav series for `name`, or None."""
    best = None
    for mf in (HERE / "data").glob(f"_ref_table_klines_{name}_*.meta.json"):
        try:
            m = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        src = str(m.get("source") or "")
        if m.get("symbol") != name or not (src.startswith("binance:") or src.startswith("nav:")):
            continue
        b = Path(str(mf).replace(".meta.json", ".json.bin"))
        if b.is_file() and (best is None or m["to"] - m["from"] > best[1]["to"] - best[1]["from"]):
            best = (b, m)
    return best


def load(name: str) -> dict:
    """1-minute closes of a feed: {"t": unix seconds, "c": close, "source": text}. Rows on disk are
    [t_ms, open, high, low, close] as float64."""
    hit = _file(name)
    if not hit:
        raise KeyError(f"no reference feed {name!r} on disk")
    path, meta = hit
    stamp = path.stat().st_mtime_ns
    got = _MEMO.get(name)
    if got and got[0] == stamp:
        return got[1]
    rows = array.array("d")
    with open(path, "rb") as fh:
        rows.frombytes(fh.read())
    n = len(rows) // 5
    out = {"t": array.array("q", (int(rows[i * 5] // 1000) for i in range(n))),
           "c": array.array("d", (rows[i * 5 + 4] for i in range(n))),
           "source": str(meta.get("source") or name)}
    if len(_MEMO) >= 3:
        _MEMO.pop(next(iter(_MEMO)))
    _MEMO[name] = (stamp, out)
    return out


def minutes(name: str, t0: int, t1: int) -> list[list]:
    """[[t, close], ...] of the feed over [t0, t1)."""
    import bisect
    f = load(name)
    a, b = bisect.bisect_left(f["t"], t0), bisect.bisect_left(f["t"], t1)
    return [[f["t"][i], f["c"][i]] for i in range(a, b) if f["c"][i] > 0]


def daily(name: str) -> list[list]:
    """[[day, open, high, low, close], ...] from the feed's minute CLOSES: a day's low is the lowest
    minute close, which is exactly what a replay of that day can show."""
    f = load(name)
    out: list = []
    for t, c in zip(f["t"], f["c"]):
        if not c > 0:
            continue
        d = t - t % DAY
        if out and out[-1][0] == d:
            r = out[-1]
            if c > r[2]:
                r[2] = c
            if c < r[3]:
                r[3] = c
            r[4] = c
        else:
            out.append([d, c, c, c, c])
    return out


if __name__ == "__main__":
    import sys
    for nm in (sys.argv[1:] or sorted(set(FEEDS.values()))):
        f = load(nm)
        print(f"{nm:>7} {f['source']:<36} {len(f['t']):>8} minutes  "
              f"{time.strftime('%Y-%m-%d', time.gmtime(f['t'][0]))} .. {time.strftime('%Y-%m-%d', time.gmtime(f['t'][-1]))}")
