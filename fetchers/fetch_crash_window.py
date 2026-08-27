#!/usr/bin/env python3
"""fetch_crash_window.py — minute candles for a collateral's worst real crash.

The daily pipeline (fetch_candles.py) showed why daily closes are not enough:
CRV's Oct-10-2025 candle closed −10.4% while the minute data inside it fell
−60.3% in 83 minutes. This module finds each venue series' WORST CRASH DAY from
the stored daily candles — wick-aware: the deepest daily low relative to the
previous close — and fetches 1-minute venue OHLC for a 15-day window around it
(7 days of lead-in, the crash day, 7 days of aftermath).

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


def build_window(key: str, force: bool = False, rank: int = 0) -> dict:
    """Resolve `key` against data/candles.json, find the rank-th worst
    crash day (0 = worst), fetch the minute path, cache and return it."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = key.replace(":", "_").replace("/", "_")
    cache = CACHE_DIR / (f"{safe}.json" if rank == 0
                         else f"{safe}_r{rank}.json")

    series = json.loads(CANDLES.read_text())["series"]
    s = series.get(key)
    if not s:
        raise KeyError(f"unknown series {key!r}")
    daily = s.get("candles_quote") or []
    if len(daily) < 3:
        raise ValueError("series has too few daily candles")
    ws = [w for w in worst_crash_days(daily, n=3) if w["drop"] < -0.001]
    if rank >= len(ws):
        raise ValueError(f"only {len(ws)} distinct crashes on record")
    w = ws[rank]

    t0 = w["day"] - LEAD_DAYS * DAY
    t1 = w["day"] + (TAIL_DAYS + 1) * DAY
    if cache.exists() and not force:
        try:
            c = json.loads(cache.read_text())
            # still the same crash day and the same window span -> reuse
            if c.get("window_from") == t0 and c.get("window_to") == t1:
                if "crashes" not in c:      # pre-ranking cache: annotate
                    c["rank"] = rank
                    c["crashes"] = [
                        {"day_utc": time.strftime("%Y-%m-%d",
                                                  time.gmtime(x["day"])),
                         "drop": round(x["drop"], 4)} for x in ws]
                    cache.write_text(json.dumps(c))
                return c
        except (json.JSONDecodeError, OSError):
            pass
    pts = fetch_minutes(s["chain"], s["pool"], s["base_addr"], s["quote_addr"],
                        t0, t1)
    if len(pts) < 100:
        raise ValueError(f"minute data too sparse ({len(pts)} points)")

    p0 = pts[0][1]
    lows = min(p[1] for p in pts)
    out = {
        "key": key,
        "rank": rank,
        # the ranked menu (distinct events >= 8 days apart), for the UI
        "crashes": [{"day_utc": time.strftime("%Y-%m-%d",
                                              time.gmtime(x["day"])),
                     "drop": round(x["drop"], 4)} for x in ws],
        "base_symbol": s["base_symbol"],
        "fetched_at": int(time.time()),
        "window_from": t0,
        "window_to": t1,
        "window_from_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(pts[0][0])),
        "window_to_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(pts[-1][0])),
        "crash_day_utc": time.strftime("%Y-%m-%d", time.gmtime(w["day"])),
        "daily_wick_drop": round(w["drop"], 4),
        "path_min_frac": round(lows / p0, 4),      # deepest point of the % path
        "n_points": len(pts),
        # [[seconds_from_window_start, fraction_of_first_close], ...]
        "pct_path": [[p[0] - pts[0][0], round(p[1] / p0, 8)] for p in pts],
    }
    cache.write_text(json.dumps(out))
    return out


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
            r = build_window(key, force=force)
            done.append(key)
            log(f"  {s.get('base_symbol', '?'):>8} {r['crash_day_utc']} "
                f"wick {r['daily_wick_drop'] * 100:.1f}% "
                f"{r['n_points']} pts")
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
    r = build_window(sys.argv[1], force="--force" in sys.argv)
    print(f"{r['base_symbol']}: crash day {r['crash_day_utc']} "
          f"(daily wick {r['daily_wick_drop']*100:.1f}%), window "
          f"{r['window_from_utc']} -> {r['window_to_utc']}, "
          f"{r['n_points']} minute points, path min {r['path_min_frac']*100:.1f}% "
          f"of start")
