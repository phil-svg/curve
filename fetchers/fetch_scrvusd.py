#!/usr/bin/env python3
"""fetch_scrvusd.py — scrvUSD (savings crvUSD) for the scrvUSD tab.

History from the Curve prices API only, no RPC:
  - /v1/crvusd/savings/yield        daily assets, supply, projected APY,
                                    price per share (<= 300 rows per call,
                                    walked in windows, then topped up)
The page shows the APR: ln(1 + APY), the mint markets' convention (their
borrow_apr is exactly ln(1 + borrow_apy)); against the vault's on-chain
profitUnlockingRate APR it is off by 0.002 points (2026-09-24).
  - /v1/crvusd/savings/revenue      every strategy report (gain / loss),
                                    all pages

Output: data/scrvusd.json, cut to what the page draws (it crosses the
network on every visit); the per-day cache lives in data/scrvusd_state.json.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "scrvusd.json"
STATE = HERE / "data" / "scrvusd_state.json"
API = "https://prices.curve.finance/v1/crvusd/savings"
LAUNCH = 1727740800            # 2024-10-01, before the first yield row
WINDOW = 290 * 86400           # the yield endpoint returns <= 300 rows
PAUSE_S = 0.15


def _get(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curve-sim"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2.0 * (i + 1))
    raise last


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
               .timestamp())


def atomic_write(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, p)


def yield_history(prev: dict) -> dict:
    """Daily rows keyed by day ts; cached rows are kept, the last 3 days
    are always fetched again (the open day moves)."""
    rows = {int(t): r for t, r in (prev or {}).items()}
    now = int(time.time())
    start = (max(rows) - 3 * 86400) if rows else LAUNCH
    while start < now:
        end = min(now, start + WINDOW)
        d = _get(f"{API}/yield?agg_number=1&agg_units=day&start={start}"
                 f"&end={end}")
        for r in d.get("data") or []:
            t = int(r["timestamp"]) // 86400 * 86400
            rows[t] = {"a": r.get("assets"), "s": r.get("supply"),
                       "y": r.get("proj_apy"), "p": r.get("price")}
        start = end
        time.sleep(PAUSE_S)
    return {str(t): rows[t] for t in sorted(rows)}


def revenue_history() -> dict:
    reports, total, page = [], None, 1
    while True:
        d = _get(f"{API}/revenue?page={page}&per_page=100")
        total = d.get("total_distributed", total)
        h = d.get("history") or []
        for r in h:
            try:
                reports.append({
                    "t": _ts(r["dt"]),
                    "gain": int(r.get("gain") or 0) / 1e18,
                    "loss": int(r.get("loss") or 0) / 1e18,
                    "debt": int(r.get("current_debt") or 0) / 1e18,
                    "fees": int(r.get("total_fees") or 0) / 1e18,
                    "tx": r.get("tx_hash")})
            except (KeyError, ValueError, TypeError):
                continue
        if len(h) < 100 or len(reports) >= (d.get("count") or 0):
            break
        page += 1
        time.sleep(PAUSE_S)
    reports.sort(key=lambda r: r["t"])
    daily = {}
    for r in reports:
        day = r["t"] // 86400 * 86400
        daily[day] = daily.get(day, 0.0) + r["gain"] - r["loss"]
    return {"first_t": reports[0]["t"] if reports else None,
            "n_reports": len(reports),
            "total_distributed": (int(total) / 1e18) if total else None,
            "daily": {"t": sorted(daily), "v": [round(daily[t], 2)
                                                 for t in sorted(daily)]}}


def main() -> None:
    t0 = time.time()
    prev = {}
    try:
        prev = json.loads(STATE.read_text()).get("yield_days") or {}
    except (OSError, ValueError):
        pass
    yd = yield_history(prev)
    rev = revenue_history()
    ts = [int(t) for t in yd]
    rnd = lambda v: None if v is None else round(v)
    out = {
        "generated_at": int(time.time()),
        "yield": {"t": ts,
                  "assets": [rnd(yd[str(t)]["a"]) for t in ts],
                  "supply": [rnd(yd[str(t)]["s"]) for t in ts],
                  "apr": [round(100 * math.log1p(yd[str(t)]["y"] / 100), 4)
                          if yd[str(t)]["y"] is not None else None
                          for t in ts]},
        "revenue": rev,
    }
    atomic_write(STATE, {"yield_days": yd})
    atomic_write(OUT, out)
    print(f"[scrvusd] {len(ts)} yield days, {rev['n_reports']} reports, "
          f"{time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
