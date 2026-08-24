#!/usr/bin/env python3
"""fetch_candles.py — daily OHLC candles for every LlamaLend market's
collateral, from the Curve prices API (prices.curve.finance, the same source
curvemonitor charts), so volatility per market can be measured.

Series come from data/markets.json: one series per unique
(chain, venue pool, base, quote), where base = the collateral token (or its
ERC4626 underlying when the venue was found via redemption) and quote = the
borrowed token when the venue pool holds it, else the pool's biggest other
coin. Markets without a venue get no candles and are listed under "missing".

Candles are stored in USD, from whichever of two sources is trustworthy for the
token (`usd_source` records which):

  "token"        the collateral's own daily USD price sets the close. Used only
                 when that feed covers >=MIN_COVERAGE of the venue's trading
                 days: the API's per-token history is sparse for illiquid
                 collateral (swBTC: 173 days out of 727) and occasionally just
                 wrong (sdeUSD quoted at $0.0001), and a sparse feed inflates
                 volatility because consecutive samples span many days.
  "quote-scaled" venue ratio x the quote token's USD price. Dense and accurate
                 when the quote is a dollar stable; when the quote itself moves,
                 the two feeds' timing mismatch leaks in as noise (this is what
                 fabricated 55% "volatility" for crvUSD collateral, whose dollar
                 price never moves) — so the token feed wins wherever it is
                 dense enough.

Intraday shape (the wicks) comes from the venue ratio only when the quote is a
dollar stable, where ratio-shape IS USD-shape; otherwise candles degrade to
close-to-close and say so via `shape: "close-only"`. `vol_quote` keeps the
volatility of the raw venue ratio, which is what actually moves borrower health
when the lending token is not a dollar (wstETH/WETH).

API notes (probed 2026-08-10):
  GET /v1/ohlc/{chain}/{pool}?main_token=&reference_token=&agg_number=1
      &agg_units=day&start=&end=
  returns the price of REFERENCE priced in MAIN, so main=quote, reference=base.
  Window is capped (~200 days for daily) -> backfill in CHUNK_DAYS slices.
  Dead pools return flat candles (high==low); stale_frac_90d exposes those.
  GET /v1/usd_price/{chain}/{addr}/history?interval=day&start=&end=
  gives the daily USD price; it hangs on multi-year windows and silently
  truncates past 300 points, so it is walked in the same slices.

Incremental by design: existing candles in data/candles.json are kept and only
the last few days are re-fetched, so the hourly/daily server refresh costs one
request per series. Run directly for the initial 2-year backfill:

    python3 fetch_candles.py [--backfill-days 730]
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MARKETS = HERE / "data" / "markets.json"
OUT = HERE / "data" / "candles.json"

API = "https://prices.curve.finance/v1/ohlc/{chain}/{pool}"
USD_API = "https://prices.curve.finance/v1/usd_price/{chain}/{addr}/history"
CHUNK_DAYS = 180          # under both endpoints' caps (~200 OHLC, 300 usd pts)
DAY = 86400
OVERLAP_DAYS = 3          # re-fetch tail so the partial "today" candle heals
MIN_COVERAGE = 0.8        # token USD feed must cover this share of venue days
MAX_GAP = 1.5 * DAY       # returns are only computed across adjacent candles


def _get(url: str) -> dict | None:
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bad-debt-sim"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == 1:
                time.sleep(1.0)
    return None


def fetch_range(chain: str, pool: str, base: str, quote: str,
                start: int, end: int) -> list[dict] | None:
    """Daily candles of base priced in quote over [start, end]; None on error."""
    q = urllib.parse.urlencode({
        "main_token": quote, "reference_token": base,
        "agg_number": 1, "agg_units": "day", "start": start, "end": end,
    })
    j = _get(API.format(chain=chain, pool=pool) + "?" + q)
    if j is None or "data" not in j:
        return None
    return j["data"]


def fetch_usd_history(chain: str, addr: str, start: int, end: int) -> dict:
    """Daily USD price of `addr` as {midnight_epoch: price}. The venue OHLC is
    a RATIO (quote per base); multiplying by the quote's USD price turns the
    candles into dollars. Long windows hang / silently truncate at 300 points,
    so walk in CHUNK_DAYS slices."""
    out: dict[int, float] = {}
    s = start
    while s < end:
        e = min(s + CHUNK_DAYS * DAY, end)
        j = _get(USD_API.format(chain=chain, addr=addr)
                 + f"?interval=day&start={s}&end={e}")
        for p in (j or {}).get("data") or []:
            try:
                # API stamps are UTC midnight; timegm reads the struct as UTC
                # (time.mktime would shift it by the local zone).
                t = calendar.timegm(time.strptime(p["timestamp"][:19],
                                                  "%Y-%m-%dT%H:%M:%S"))
                out[t - t % DAY] = float(p["price"])
            except (KeyError, TypeError, ValueError):
                pass
        s = e
        time.sleep(0.15)
    return out


def quote_prices(candles: list[list], usd: dict) -> list:
    """The quote token's USD price to use for each candle, forward-filled from
    the last known day (a stale quote price beats a hole). None = no price yet
    at that point in the series."""
    out, last = [], None
    for r in candles:
        p = usd.get(r[0] - r[0] % DAY, last)
        if p is not None:
            last = p
        out.append(p)
    return out


def scale(candles: list[list], prices: list, invert: bool = False) -> list[list]:
    """Multiply (or divide) OHLC by a per-candle factor. ratio -> USD with the
    quote's price; invert=True walks back to the venue's own ratio."""
    out = []
    for r, p in zip(candles, prices):
        if not p:
            continue
        f = 1 / p if invert else p
        out.append([r[0], r[1] * f, r[2] * f, r[3] * f, r[4] * f])
    return out


def usd_candles(ratio: list[list], base_usd: dict, keep_shape: bool
                ) -> list[list]:
    """USD candles anchored on the COLLATERAL's own daily price. `keep_shape`
    re-uses the venue's intraday open/high/low as ratios of its close (valid
    only when the quote is a dollar stable); otherwise the candle spans the
    previous close to this one, which is all the data honestly supports."""
    out, prev = [], None
    for t, o, h, l, c in ratio:
        p = base_usd.get(t - t % DAY)
        if p is None or c <= 0:
            continue
        if keep_shape:
            row = [t, p * o / c, p * h / c, p * l / c, p]
        else:
            op = prev if prev is not None else p
            row = [t, op, max(op, p), min(op, p), p]
        prev = p
        out.append(row)
    return out


def ann_vol(prices: list) -> float:
    """Annualised stdev of consecutive log returns (order-preserving list)."""
    rets = [math.log(prices[k] / prices[k - 1])
            for k in range(1, len(prices))
            if prices[k] > 0 and prices[k - 1] > 0]
    if len(rets) < 10:
        return 0.0
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) \
        * math.sqrt(365)


def round_ohlc(rows: list[list]) -> list[list]:
    """8 significant digits — far past display needs, ~halves the JSON."""
    def r(v):
        if not v:
            return v
        return round(v, max(0, 8 - int(math.floor(math.log10(abs(v)))) - 1))
    return [[x[0]] + [r(v) for v in x[1:]] for x in rows]


def fetch_redeem_rates(chain: str, wrappers: list[dict], days: list[int],
                       cache: dict) -> int:
    """Daily ERC4626 assets-per-share for each vault-share collateral, read on
    chain at that day's block. There is no price feed for wrapper tokens
    (`usd_price` returns "Token data not found" for every one of them), so this
    is the only way to price the collateral rather than the thing it redeems
    to — and it is the only source that would show a vault LOSING value, which
    is exactly the tail a bad-debt sim cares about.

    One Multicall3 per day covers every wrapper at once. Post-merge blocks are
    a fixed 12 s apart, so day -> block is arithmetic; being a few blocks off
    is irrelevant for a rate that drifts ~0.015%/day."""
    from fetch_markets import Rpc, _num          # local: RPC only needed here
    todo = [d for d in days
            if any(str(d) not in cache.get(w["addr"], {}) for w in wrappers)]
    if not wrappers or not todo:
        return 0
    rpc = Rpc(chain)
    head_n, head_t = rpc.head()
    calls = [(w["addr"], "convertToAssets(uint256)", 10 ** w["decimals"])
             for w in wrappers]
    n_req = 0
    for d in todo:
        blk = head_n - (head_t - d) // 12
        if blk < 1:
            continue
        res = rpc.mq(calls, hex(blk))
        n_req += 1
        for w, r in zip(wrappers, res):
            v = _num(r)
            if v is not None:
                cache.setdefault(w["addr"], {})[str(d)] = \
                    v / 10 ** w["under_decimals"]
    return n_req


def series_from_markets(mkts: dict) -> tuple[dict, list]:
    """Unique candle series across all markets + the venue-less leftovers."""
    series: dict[str, dict] = {}
    missing: list[str] = []
    for group, ms in mkts["groups"].items():
        for m in ms:
            label = (f"{group} {m['chain']} "
                     f"{m['collateral']['symbol']}/{m['borrowed']['symbol']}")
            v = m.get("venue")
            if not v or not v.get("base_addr") or not v.get("quote_addr"):
                missing.append(label)
                continue
            # A vault share is its own asset: syrupUSDC and yvUSDC-1 both route
            # through the crvUSD/USDC pool but are different collateral, so the
            # wrapper is part of the series identity, not a footnote.
            w = v.get("wrapper")
            key = (f"{m['chain']}:{v['pool']}:{v['base_addr']}:{v['quote_addr']}"
                   + (f":{w['addr']}" if w else ""))
            s = series.setdefault(key, {
                "chain": m["chain"],
                "pool": v["pool"],
                "pool_name": v["name"],
                "base_addr": v["base_addr"],
                "base_symbol": w["symbol"] if w else m["collateral"]["symbol"],
                "under_symbol": v.get("via_redemption"),
                "quote_addr": v["quote_addr"],
                "quote_symbol": v.get("quote_symbol", "?"),
                "via_redemption": v.get("via_redemption"),
                "wrapper": w,
                "base_decimals": v.get("base_decimals"),
                "markets": [],
            })
            if label not in s["markets"]:
                s["markets"].append(label)
    return series, missing


def vol_stats(candles: list[list]) -> dict:
    """Trailing volatility from daily closes ([t,o,h,l,c] rows, sorted).
    ann_vol_* = stdev of daily log returns * sqrt(365); stale_frac_90d = share
    of the last 90 candles with high == low (no trades -> dust venue)."""
    closes = [(r[0], r[4]) for r in candles if r[4] and r[4] > 0]
    rets = []
    for k in range(1, len(closes)):
        # Only adjacent days: counting a multi-day jump as one daily return
        # would report a sparse series as far more volatile than it is.
        if closes[k][0] - closes[k - 1][0] <= MAX_GAP:
            rets.append(math.log(closes[k][1] / closes[k - 1][1]))
    out = {}
    for name, n in (("ann_vol_30d", 30), ("ann_vol_90d", 90),
                    ("ann_vol_365d", 365)):
        w = rets[-n:]
        if len(w) >= 10:
            mu = sum(w) / len(w)
            sd = math.sqrt(sum((x - mu) ** 2 for x in w) / (len(w) - 1))
            out[name] = round(sd * math.sqrt(365), 4)
        else:
            out[name] = None
    tail = candles[-90:]
    out["stale_frac_90d"] = (round(sum(1 for r in tail if r[2] == r[3])
                                   / len(tail), 3) if tail else None)
    out["last_close"] = closes[-1][1] if closes else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=730,
                    help="history target for series with no stored candles")
    a = ap.parse_args()

    mkts = json.loads(MARKETS.read_text())
    series, missing = series_from_markets(mkts)

    old, old_usd, rates = {}, {}, {}
    if OUT.exists():
        try:
            j0 = json.loads(OUT.read_text())
            old = j0.get("series", {})
            old_usd = j0.get("quote_usd", {})
            rates = j0.get("redeem_rates", {})
        except Exception:
            old, old_usd, rates = {}, {}, {}

    now = int(time.time())
    n_req = 0

    # One USD price series per distinct token — both sides of every pair, since
    # the collateral's own price sets the level and the quote's decides whether
    # the venue's intraday shape survives. Cached in the output file, so a daily
    # run only tops up the tail.
    tok_usd: dict[str, dict] = {}
    syms = {}
    for s in series.values():
        syms[f"{s['chain']}:{s['base_addr']}"] = s["base_symbol"]
        syms.setdefault(f"{s['chain']}:{s['quote_addr']}", s["quote_symbol"])
    for tk in sorted(syms):
        chain, addr = tk.split(":")
        prev = {int(k): v for k, v in (old_usd.get(tk) or {}).items()}
        start = (max(prev) - OVERLAP_DAYS * DAY) if prev \
            else now - a.backfill_days * DAY
        got = fetch_usd_history(chain, addr, start, now)
        n_req += max(1, (now - start) // (CHUNK_DAYS * DAY) + 1)
        prev.update(got)
        tok_usd[tk] = prev
        print(f"  usd {syms[tk]:<24} {len(prev)} days"
              + ("" if prev else "  (no USD feed)"))

    # A quote worth < 5%/yr of movement is a dollar stable, so the venue's
    # ratio wicks are USD wicks; anything livelier would smear its own moves
    # into the collateral's candle.
    stable = {tk: (ann_vol([v[t] for t in sorted(v)]) < 0.05) if v else False
              for tk, v in tok_usd.items()}

    # Vault-share collateral: daily redemption rates, read on chain.
    for chain in sorted({s["chain"] for s in series.values() if s.get("wrapper")}):
        wraps, seen = [], set()
        for s in series.values():
            w = s.get("wrapper")
            if s["chain"] != chain or not w or w["addr"] in seen:
                continue
            seen.add(w["addr"])
            wraps.append({**w, "under_decimals": s.get("base_decimals") or 18})
        d0 = now - a.backfill_days * DAY
        days = [d - d % DAY for d in range(d0, now, DAY)]
        got = fetch_redeem_rates(chain, wraps, days, rates)
        n_req += got
        print(f"  redemption rates {chain}: {len(wraps)} vault tokens"
              f"  [{got} req]")

    for key, s in series.items():
        bk, qk = f"{s['chain']}:{s['base_addr']}", f"{s['chain']}:{s['quote_addr']}"
        base_usd, quote_usd = tok_usd[bk], tok_usd[qk]
        prev_s = old.get(key) or {}
        # Raw venue ratios are kept alongside the USD candles: a daily run only
        # refetches 3 days, so the full ratio history has to persist for
        # vol_quote (and for rebuilding USD if a token's feed appears later).
        by_r = {r[0]: r for r in (prev_s.get("candles_quote") or [])}
        start = (max(by_r) - OVERLAP_DAYS * DAY) if by_r \
            else now - a.backfill_days * DAY
        err = None
        while start < now:
            end = min(start + CHUNK_DAYS * DAY, now)
            rows = fetch_range(s["chain"], s["pool"], s["base_addr"],
                               s["quote_addr"], start, end)
            n_req += 1
            if rows is None:
                err = f"fetch failed for window {start}..{end}"
                break
            for c in rows:
                by_r[c["time"]] = [c["time"], c["open"], c["high"],
                                   c["low"], c["close"]]
            start = end
            time.sleep(0.15)                             # be polite to the API
        ratio = [by_r[t] for t in sorted(by_r)]

        # How much of the venue's own trading calendar the collateral's USD
        # feed actually covers: a thin feed is worse than scaling the ratio.
        cov = (sum(1 for r in ratio if (r[0] - r[0] % DAY) in base_usd)
               / len(ratio)) if ratio and base_usd else 0.0
        s["usd_coverage"] = round(cov, 3)
        if cov >= MIN_COVERAGE:
            s["usd_source"] = "token"
            s["shape"] = "venue" if stable.get(qk) else "close-only"
            usd_rows = usd_candles(ratio, base_usd, stable.get(qk, False))
        elif quote_usd:
            s["usd_source"] = "quote-scaled"
            s["shape"] = "venue"
            usd_rows = scale(ratio, quote_prices(ratio, quote_usd))
        else:
            s["usd_source"] = "none"       # neither side has a USD feed
            s["shape"] = "venue"
            usd_rows = ratio

        # The venue prices the underlying; the collateral is the vault share on
        # top of it, so lift the whole candle by that day's redemption rate.
        w = s.get("wrapper")
        if w and rates.get(w["addr"]):
            rr = rates[w["addr"]]
            usd_rows = [[r[0]] + [v * rr[str(r[0] - r[0] % DAY)] for v in r[1:]]
                        for r in usd_rows if str(r[0] - r[0] % DAY) in rr]
            s["redeem_rate_last"] = rr[max(rr, key=int)]

        s["candles"] = round_ohlc(usd_rows)
        s["candles_quote"] = round_ohlc(ratio)
        s["usd"] = s["usd_source"] != "none"
        s["error"] = err
        s["vol"] = vol_stats(s["candles"]) if s["candles"] else None
        s["vol_quote"] = vol_stats(ratio) if ratio else None
        tag = (f"{len(s['candles'])} candles  {s['usd_source']}/{s['shape']}"
               + (f"  ERR {err}" if err else "")
               + (f"  vol90 {s['vol']['ann_vol_90d']}" if s.get("vol") else ""))
        print(f"  {s['base_symbol']}/{s['quote_symbol']} @ {s['pool_name']}"
              f" ({s['chain']}): {tag}")

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "fetched_at": now,
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        "requests": n_req,
        "denomination": "USD",
        "missing": missing,
        "quote_usd": {k: {str(t): round(p, 10) for t, p in v.items()}
                      for k, v in tok_usd.items()},
        "redeem_rates": rates,
        "series": series,
    }))
    os.replace(tmp, OUT)
    print(f"wrote {OUT}  ({len(series)} series, {n_req} requests,"
          f" {len(missing)} markets without venue)")


if __name__ == "__main__":
    main()
