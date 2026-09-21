#!/usr/bin/env python3
"""fetch_crash_window.py — minute candles for a collateral's worst real crash.

The daily pipeline (fetch_candles.py) showed why daily closes are not enough:
CRV's Oct-10-2025 candle closed −10.4% while the minute data inside it fell
−60.3% in 83 minutes. This module finds each venue series' WORST CRASH DAY from
the stored daily candles — wick-aware: the deepest daily low relative to the
previous close — and fetches 1-minute venue OHLC for a 15-day window around it
(7 days of lead-in, the crash day, 7 days of aftermath).

Where the path comes from is decided per market by ref_feeds.plan(): the
collateral's own deep 1-minute feed where one is on disk (WETH, WBTC, tBTC, CRV,
wstETH, XAUM: a $240k sidechain pool is not ETH's price), the venue ratio times
the quote token's feed where the venue quotes in a fed token and the market lends
dollars (pufETH in wstETH), else the venue alone. Venue paths are despiked, and a
crash day only ranks as deep as its 10-minute closes confirm against the median of
the day before: the daily candles' lows and previous closes are where a thin
pool's dust prints live (see the "hygiene" block below). A real wick shorter than
~10 minutes therefore does not rank — it never showed in the replayed closes either.

The result is a % path (fraction of the window's first close, minute spacing)
that the sim replays against TODAY's spot — the shape is historical, the level
is current. Cached under data/crash_windows/; a cached window is reused only
while it still covers the current worst crash day with the current span — a
new, deeper crash (or a span change) triggers a refetch.

Minute endpoint quirk: ~180 points per request maximum, so a 15-day window is
fetched in 3-hour chunks (~120 requests, ~45 s with politeness sleeps).

    python3 fetch_crash_window.py <series_key>     # chain:pool:base:quote[:wrapper]
    python3 fetch_crash_window.py --all            # every series in candles.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fetch_candles import _get, DAY  # shared HTTP helper (retry + UA)

HERE = Path(__file__).resolve().parent.parent
CANDLES = HERE / "data" / "candles.json"
CACHE_DIR = HERE / "data" / "crash_windows"

API = "https://prices.curve.finance/v1/ohlc/{chain}/{pool}"
CHUNK_S = 3 * 3600           # 180 minute-candles per request
LEAD_DAYS = 7                # window: crash day - 7 .. crash day + 7
TAIL_DAYS = 7


def worst_crash_days(daily: list[list], n: int = 3,
                     min_gap_days: int = 8) -> list[dict]:
    """The n deepest daily LOWs vs the previous close, on the venue-ratio
    candles ([t,o,h,l,c]). Wick-aware — this is what close-only metrics
    missed. Ranked crashes are distinct events: a candidate within
    min_gap_days of an already-picked day is the same crash, not a new
    one."""
    cands = []
    for k in range(1, len(daily)):
        prev_c = daily[k - 1][4]
        low = daily[k][3]
        if prev_c <= 0 or low <= 0:
            continue
        cands.append({"drop": low / prev_c - 1,
                      "day": daily[k][0] - daily[k][0] % DAY,
                      "prev_close": prev_c, "low": low})
    cands.sort(key=lambda c: c["drop"])
    out: list[dict] = []
    for c in cands:
        if any(abs(c["day"] - o["day"]) < min_gap_days * DAY for o in out):
            continue
        out.append(c)
        if len(out) >= n:
            break
    return out


def worst_crash_day(daily: list[list]) -> dict | None:
    ws = worst_crash_days(daily, n=1)
    return ws[0] if ws else None


def fetch_minutes(chain: str, pool: str, base: str, quote: str,
                  start: int, end: int) -> list[list]:
    """[[t, close], ...] at minute spacing over [start, end)."""
    out: dict[int, float] = {}
    t = start
    while t < end:
        e = min(t + CHUNK_S, end)
        j = _get(API.format(chain=chain, pool=pool)
                 + f"?main_token={quote}&reference_token={base}"
                 + f"&agg_number=1&agg_units=minute&start={t}&end={e}")
        for c in (j or {}).get("data") or []:
            out[c["time"]] = c["close"]
        t = e
        time.sleep(0.15)
    return [[t_, out[t_]] for t_ in sorted(out)]


# ---- hygiene: what a venue's candles may and may not claim ----------------------------
# The daily candles rank the crash days by their LOW, and a thin pool's low is whatever one
# dust trade printed: WETH on Arbitrum "fell 100 %" on 2025-04-03 (seven minute-candles at
# zero between two at 0.899), CRV on mainnet "fell 81 %" on 2026-09-14 on a day whose minute
# closes never moved 1 %. Two rules, both about what the REPLAY will actually show:
#   1. impossible prints are dropped from every minute path (despike);
#   2. a crash day only ranks as deep as its despiked minute closes confirm.
DAYS_DIR = CACHE_DIR / "days"
PRINT_OUT = 0.50          # an impossible print leaves by >= 50 % from one minute to the next ...
PRINT_BACK = 0.05         # ... and the price is back within 5 % of where it left from ...
PRINT_MAX_S = 3600        # ... inside the hour. A price <= 0 is one whatever follows.
N_CANDIDATES = 8          # daily-low candidates that get their minutes checked


def despike(pts: list[list]) -> tuple[list[list], int]:
    """[[t, price], ...] without impossible prints (each replaced by the price before it, so the
    minute spacing stays). -> (clean, number of minutes replaced). Deliberately narrow: a real
    crash, however violent, does not halve a price within one minute AND undo all of it within
    the hour; a thin pool's honest 20 % wick is left alone."""
    out: list[list] = []
    removed, i, n = 0, 0, len(pts)
    while i < n:
        t, p = pts[i]
        if not out:
            if p > 0:
                out.append([t, p])
            else:
                removed += 1
            i += 1
            continue
        pre = out[-1][1]
        if not p > 0:
            out.append([t, pre]); removed += 1; i += 1
            continue
        if abs(p / pre - 1) >= PRINT_OUT:
            j = i + 1
            while j < n and pts[j][0] - t <= PRINT_MAX_S and not (
                    pts[j][1] > 0 and abs(pts[j][1] / pre - 1) <= PRINT_BACK):
                j += 1
            if j < n and pts[j][0] - t <= PRINT_MAX_S:
                for k in range(i, j):
                    out.append([pts[k][0], pre])
                removed += j - i
                i = j
                continue
        out.append([t, p])
        i += 1
    return out, removed


CONFIRM_MIN = 10          # a crash day is confirmed on 10-minute closes: one request per day (the minute
                          # endpoint caps at ~180 candles, so a day of 1-minute candles would be nine)


def day_minutes(key: str, s: dict, day: int) -> list[list]:
    """10-minute closes of one venue series over `day`: [[t, close], ...], kept on disk once the day lies
    in the past. A close is what a replay can show; the candle LOWS are where the dust prints live
    (WETH/Arbitrum 2025-04-03: lowest 10-minute close 1752, lowest low 0.00002)."""
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    safe = key.replace(":", "_").replace("/", "_")
    f = DAYS_DIR / f"{safe}_{day}_d{CONFIRM_MIN}m.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    j = _get(API.format(chain=s["chain"], pool=s["pool"])
             + f"?main_token={s['quote_addr']}&reference_token={s['base_addr']}"
             + f"&agg_number={CONFIRM_MIN}&agg_units=minute&start={day}&end={day + DAY}")
    pts = sorted([c["time"], c["close"]] for c in (j or {}).get("data") or [] if c.get("close") is not None)
    time.sleep(0.15)
    if pts and day + DAY < time.time() - 3600:
        f.write_text(json.dumps(pts))
    return pts


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2]


def _drop_in_day(pts: list[list], day: int) -> float | None:
    """Deepest close of `day` against the MEDIAN close of the day before. Not against the last close: a thin
    pool can print 4 % too high at 22:00 and sit there until someone trades again, and the way back to the
    real price would then read as a 4 % crash (sUSDe/crvUSD, 2025-01-16 -> 17)."""
    before = [p for t, p in pts if day - DAY <= t < day]
    inday = [p for t, p in pts if day <= t < day + DAY]
    if len(inday) < 5:
        return None
    ref = _median(before) if len(before) >= 5 else inday[0]
    return min(inday) / ref - 1


def _two_days(key: str, s: dict, day: int) -> list[list]:
    """The day before and the day itself, despiked together."""
    return despike(day_minutes(key, s, day - DAY) + day_minutes(key, s, day))[0]


def confirmed_crashes(key: str, s: dict, daily: list[list], n: int = 3,
                      scale=None) -> list[dict]:
    """The n deepest crash days as a replay will show them. Candidates are the deepest daily
    lows (distinct events); each is re-measured on its despiked 10-minute closes (times
    `scale(t)`, when the venue ratio is composed with a feed), and the list is re-ranked on
    that: the day's deepest close against the median close of the day before.
    Each dict: worst_crash_days' fields + wick_raw (the daily candle's claim) and confirmed."""
    byday = {c[0] - c[0] % DAY: c for c in daily}
    out = []
    for w in worst_crash_days(daily, n=N_CANDIDATES):
        c = byday.get(w["day"])
        close_drop = c[4] / w["prev_close"] - 1 if c else 0.0
        e = {**w, "wick_raw": w["drop"], "confirmed": False}
        # always ask the closes, also for a day that closed at its low: the daily candle measures against the
        # previous CLOSE, which is exactly what an upward print poisons
        try:
            pts = _two_days(key, s, w["day"])
            if scale is not None:
                pts = [[t, p * scale(t)] for t, p in pts]
            d = _drop_in_day(pts, w["day"])
        except Exception:
            d = None
        if d is None:                                                 # no closes to ask: trust the daily close, not the wick
            e["drop"] = min(0.0, close_drop)
        else:
            e["drop"], e["confirmed"] = d, True
        out.append(e)
    out.sort(key=lambda x: x["drop"])
    return [x for x in out if x["drop"] < -0.001][:n]


def _crash_menu(ws: list[dict]) -> list[dict]:
    return [{"day_utc": time.strftime("%Y-%m-%d", time.gmtime(x["day"])),
             "drop": round(x["drop"], 4),
             **({"wick_raw": round(x["wick_raw"], 4)} if "wick_raw" in x
                and abs(x["wick_raw"] - x["drop"]) > 0.005 else {})} for x in ws]


def _shape(key: str, rank: int, ws: list[dict], pts: list[list], symbol: str,
           t0: int, t1: int, extra: dict) -> dict:
    w = ws[rank]
    p0 = pts[0][1]
    return {
        "key": key, "rank": rank,
        # the ranked menu (distinct events >= 8 days apart), for the UI
        "crashes": _crash_menu(ws),
        "base_symbol": symbol,
        "fetched_at": int(time.time()),
        "window_from": t0, "window_to": t1,
        "window_from_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(pts[0][0])),
        "window_to_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(pts[-1][0])),
        "crash_day_utc": time.strftime("%Y-%m-%d", time.gmtime(w["day"])),
        "daily_wick_drop": round(w["drop"], 4),
        "path_min_frac": round(min(p[1] for p in pts) / p0, 4),      # deepest point of the % path
        "n_points": len(pts),
        # [[seconds_from_window_start, fraction_of_first_close], ...]
        "pct_path": [[p[0] - pts[0][0], round(p[1] / p0, 8)] for p in pts],
        **extra,
    }


def _venue_minutes(key: str, s: dict, cache: Path, t0: int, t1: int, force: bool) -> list[list]:
    """The venue's minute closes over [t0, t1) as [[t, fraction of the first close], ...] (the level
    cancels in every use). From a window cache when one holds exactly this window: `cache`, or one
    of the files the earlier ranking wrote (<series>.json, _r1, _r2); else fetched and kept."""
    import calendar
    safe = key.replace(":", "_").replace("/", "_")
    if not force:
        for f in (cache, CACHE_DIR / f"{safe}.json", CACHE_DIR / f"{safe}_r1.json", CACHE_DIR / f"{safe}_r2.json"):
            try:
                c = json.loads(f.read_text())
                if c.get("window_from") != t0 or c.get("window_to") != t1 or not c.get("pct_path"):
                    continue
                first = c.get("path_t0")
                if first is None:                   # older files: the first point's minute, as text
                    first = calendar.timegm(time.strptime(c["window_from_utc"], "%Y-%m-%d %H:%M"))
                return [[first + p[0], p[1]] for p in c["pct_path"]]
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                continue
    pts = fetch_minutes(s["chain"], s["pool"], s["base_addr"], s["quote_addr"], t0, t1)
    good = next((p[1] for p in pts if p[1] > 0), None)
    if len(pts) < 100 or good is None:
        return pts
    out = [[p[0], p[1] / good] for p in pts]
    cache.write_text(json.dumps({"window_from": t0, "window_to": t1, "path_t0": pts[0][0],
                                 "pct_path": [[p[0] - pts[0][0], p[1]] for p in out]}))
    return out


def build_window(key: str, force: bool = False, rank: int = 0,
                 borrowed: str | None = None) -> dict:
    """Resolve `key` against data/candles.json and return the rank-th worst crash (0 = worst) as a
    minute % path. Where the path comes from is ref_feeds.plan()'s call: the collateral's own deep
    feed, the venue ratio times the quote token's feed, or the venue alone. `borrowed` is the
    symbol the market lends (a market lending a non-dollar keeps the venue ratio)."""
    import ref_feeds
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    series = json.loads(CANDLES.read_text())["series"]
    s = series.get(key)
    if not s:
        raise KeyError(f"unknown series {key!r}")
    plan = ref_feeds.plan(s, borrowed)
    safe = key.replace(":", "_").replace("/", "_")
    symbol = s["base_symbol"]

    if plan["kind"] == "feed":
        # the collateral's own market: ranked and cut from the feed, the venue is not asked
        feed, inv = plan["feed"], bool(plan.get("invert"))
        fd = ref_feeds.daily(feed)
        if inv:                                     # a dollar priced in the fed token: every candle upside down
            fd = [[c[0], 1 / c[1], 1 / c[3], 1 / c[2], 1 / c[4]] for c in fd]
        ws = [w for w in worst_crash_days(fd, n=3) if w["drop"] < -0.001]
        if rank >= len(ws):
            raise ValueError(f"only {len(ws)} distinct crashes on record")
        w = ws[rank]
        t0, t1 = w["day"] - LEAD_DAYS * DAY, w["day"] + (TAIL_DAYS + 1) * DAY
        pts = ref_feeds.minutes(feed, t0, t1)
        if inv:
            pts = [[t, 1 / c] for t, c in pts]
        if len(pts) < 100:
            raise ValueError(f"feed window too sparse ({len(pts)} points)")
        src = ref_feeds.load(feed)["source"]
        return _shape(key, rank, ws, pts, symbol, t0, t1, {
            "source": "feed", "feed": ("1 / " if inv else "") + src, "prints_removed": 0,
            "feed_note": f"price path from {'1 / ' if inv else ''}{src} (1-minute closes), not from the venue pool: {plan['why']}"})

    daily = s.get("candles_quote") or []
    if len(daily) < 3:
        raise ValueError("series has too few daily candles")

    if plan["kind"] == "ratio_x_feed":
        import bisect
        feed = plan["feed"]
        f = ref_feeds.load(feed)

        def at(t):                                  # the feed's close in force at t
            i = bisect.bisect_right(f["t"], t) - 1
            return f["c"][i] if i >= 0 else float("nan")
        # candidates: the venue ratio's own worst days AND the feed's (the crash that matters is
        # usually the feed's: pufETH/wstETH is flat on the day ETH loses a quarter)
        fd = [c for c in ref_feeds.daily(feed) if daily[0][0] <= c[0] <= daily[-1][0]]
        cand = {w["day"]: w for w in worst_crash_days(daily, n=5)}
        for w in worst_crash_days(fd, n=5):
            cand.setdefault(w["day"], w)
        scored = []
        for day, w in cand.items():
            try:
                pts = _two_days(key, s, day)
                pts = [[t, p * at(t)] for t, p in pts if at(t) > 0]
                d = _drop_in_day(pts, day)
            except Exception:
                d = None
            if d is not None:
                scored.append({**w, "day": day, "drop": d, "confirmed": True})
        scored.sort(key=lambda x: x["drop"])
        ws = []
        for x in scored:                            # distinct events, as everywhere else
            if x["drop"] < -0.001 and not any(abs(x["day"] - o["day"]) < 8 * DAY for o in ws):
                ws.append(x)
            if len(ws) >= 3:
                break
        if rank >= len(ws):
            raise ValueError(f"only {len(ws)} distinct crashes on record")
        w = ws[rank]
        t0, t1 = w["day"] - LEAD_DAYS * DAY, w["day"] + (TAIL_DAYS + 1) * DAY
        cache = CACHE_DIR / f"{safe}__ratio_{w['day']}.json"
        ratio, removed = despike(_venue_minutes(key, s, cache, t0, t1, force))
        if len(ratio) < 100:
            raise ValueError(f"minute data too sparse ({len(ratio)} points)")
        # on the feed's minute grid, the ratio held from its last print
        grid = ref_feeds.minutes(feed, max(t0, ratio[0][0]), t1)
        pts, k = [], 0
        for t, c in grid:
            while k + 1 < len(ratio) and ratio[k + 1][0] <= t:
                k += 1
            pts.append([t, ratio[k][1] * c])
        if len(pts) < 100:
            raise ValueError("the feed does not cover this window")
        return _shape(key, rank, ws, pts, symbol, t0, t1, {
            "source": "ratio_x_feed", "feed": f["source"], "prints_removed": removed,
            "feed_note": f"price path = venue ratio {symbol}/{s.get('quote_symbol')} x {f['source']} "
                         f"(1-minute closes): {plan['why']}"})

    # the venue alone
    ws = confirmed_crashes(key, s, daily, n=3)
    if rank >= len(ws):
        raise ValueError(f"only {len(ws)} distinct crashes on record")
    w = ws[rank]
    t0, t1 = w["day"] - LEAD_DAYS * DAY, w["day"] + (TAIL_DAYS + 1) * DAY
    cache = CACHE_DIR / f"{safe}__venue_{w['day']}.json"
    pts, removed = despike(_venue_minutes(key, s, cache, t0, t1, force))
    if len(pts) < 100:
        raise ValueError(f"minute data too sparse ({len(pts)} points)")
    note = None
    if removed:
        note = f"{removed} impossible minute print{'s' if removed > 1 else ''} dropped from the venue's candles"
    return _shape(key, rank, ws, pts, symbol, t0, t1, {
        "source": "venue", "feed": None, "prints_removed": removed, "feed_note": note})


def build_all(force: bool = False, log=print) -> dict:
    """Crash window for every series in candles.json that has daily data.
    Sequential and polite (one request in flight); series without a crash
    or without minute data are reported, not fatal."""
    series = json.loads(CANDLES.read_text())["series"]
    done, skipped, failed = [], [], []
    for key, s in series.items():
        if len(s.get("candles_quote") or []) < 3:
            skipped.append((key, "no daily candles"))
            continue
        try:
            # "LLV1 ethereum wstETH/crvUSD" -> crvUSD: one window per distinct token lent against this series
            lent = sorted({str(m).rsplit("/", 1)[-1] for m in (s.get("markets") or [])}) or [None]
            for b in lent:
                r = build_window(key, force=force, borrowed=b)
                log(f"  {s.get('base_symbol', '?'):>8} {r['crash_day_utc']} "
                    f"drop {r['daily_wick_drop'] * 100:.1f}% "
                    f"{r['n_points']} pts  [{r['source']}{' ' + r['feed'] if r.get('feed') else ''}]")
            done.append(key)
        except Exception as e:
            failed.append((key, str(e)[:120]))
            log(f"  {s.get('base_symbol', '?'):>8} FAILED: {str(e)[:120]}")
    return {"done": done, "skipped": skipped, "failed": failed}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: fetch_crash_window.py <series_key>|--all [--force]")
    if "--all" in sys.argv:
        res = build_all(force="--force" in sys.argv)
        print(f"crash windows: {len(res['done'])} ok, {len(res['skipped'])} "
              f"skipped, {len(res['failed'])} failed")
        for k, why in res["failed"]:
            print(f"  failed {k}: {why}")
        raise SystemExit(0)
    lent = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--borrowed=")), None)
    r = build_window(sys.argv[1], force="--force" in sys.argv, borrowed=lent)
    print(f"{r['base_symbol']} [{r['source']}{' ' + r['feed'] if r.get('feed') else ''}]: crash day {r['crash_day_utc']} "
          f"(drop {r['daily_wick_drop']*100:.1f}%), window "
          f"{r['window_from_utc']} -> {r['window_to_utc']}, "
          f"{r['n_points']} minute points, path min {r['path_min_frac']*100:.1f}% "
          f"of start")
