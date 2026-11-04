"""Pools whose dollar figures rest on a coin price nothing backs.

Curve's APIs price every coin on its own, from wherever it trades, so a coin
that only trades in a pool holding a few dollars can carry any price at all.
On 2 Nov 2026 bscPUSSY on Base was priced at $1,275 from a cbBTC pool holding
$24, which gave eight ethPUSSY/bscPUSSY pools $4-5B of TVL each and put $666M
of volume and $26k of DAO fees on Base in a single hour.

A coin is unbacked when, over every pool of its chain that the API values,
its own balances are worth at least MIN_USD and more than RATIO times the
other coins held next to it. On 2 Nov 2026 bscPUSSY stood at 4.5 billion
times and NIPZ (the nip$ey pool, $128k) at 1,018 times; the highest real
coins were DAI on Fantom at 173 and sbtcCrv at 102. A pool is mispriced when
unbacked coins make up more than half of its value. fetch_lp.py leaves those
pools out of the pools list and writes the set to data/mispriced.json, which
fetch_dao_revenue.py and the server's /poolhist read.

A flag stays once set: the volume and fees a pool reported while its coin
was mispriced stay in the APIs' history after the price is gone. On 4 Nov
2026 the APIs stopped pricing PUSSY, the scan found nothing, and the 2 Nov
fees came back into DAO Revenue. The pools found on 2 Nov are kept in
mispriced_seed.json next to this file, so they survive a lost data file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "mispriced.json"
SEED = Path(__file__).resolve().parent / "mispriced_seed.json"
MIN_USD = 100_000
RATIO = 500


def scan(by_chain: dict[str, list]) -> dict:
    """getPools poolData per chain -> the unbacked coins and the mispriced
    pools, keyed chain:address."""
    coins: dict = {}
    pools: dict = {}
    for ch, rows in by_chain.items():
        vals, seen = [], set()
        for p in rows or []:
            a = (p.get("address") or "").lower()
            # a broken pool's balances can be priced at 1e58 dollars, and
            # the same pool can sit in two registries
            if not a or a in seen or p.get("isBroken") \
                    or not (p.get("usdTotal") or 0) > 0:
                continue
            seen.add(a)
            cv = []
            for c in p.get("coins") or []:
                try:
                    b = int(c.get("poolBalance") or 0) \
                        / 10 ** int(c.get("decimals") or 18)
                except (TypeError, ValueError):
                    b = 0.0
                cv.append(((c.get("address") or "").lower(),
                           b * float(c.get("usdPrice") or 0)))
            vals.append((p, cv))
        bad: set = set()
        while True:
            claimed: dict = {}
            backing: dict = {}
            for _p, cv in vals:
                for a, v in cv:
                    claimed[a] = claimed.get(a, 0.0) + v
                    backing[a] = backing.get(a, 0.0) + sum(
                        w for b, w in cv if b != a and b not in bad)
            new = {a for a, v in claimed.items()
                   if v >= MIN_USD and v > RATIO * backing[a]}
            if new <= bad:
                break
            bad |= new
        sym = {(c.get("address") or "").lower(): c.get("symbol") or ""
               for p, _cv in vals for c in p.get("coins") or []}
        for a in bad:
            coins[f"{ch}:{a}"] = {"symbol": sym.get(a, ""),
                                  "usd": round(claimed[a]),
                                  "backing": round(backing[a], 2)}
        for p, cv in vals:
            if sum(v for a, v in cv if a in bad) > p["usdTotal"] / 2:
                pools[f"{ch}:{p['address'].lower()}"] = {
                    "label": p.get("name") or p.get("symbol") or "",
                    "usd": round(p["usdTotal"]),
                    "coins": [{"a": (c.get("address") or "").lower(),
                               "s": c.get("symbol") or "",
                               "n": c.get("name") or ""}
                              for c in p.get("coins") or []]}
    return {"coins": coins, "pools": pools}


def _read(f: Path) -> dict:
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def update(by_chain: dict[str, list]) -> dict:
    """Scan, add every pool and coin flagged before (data file and seed),
    write data/mispriced.json; returns the mispriced pools."""
    res = scan(by_chain)
    for old in (_read(OUT), _read(SEED)):
        for part in ("coins", "pools"):
            for k, v in (old.get(part) or {}).items():
                res[part].setdefault(k, v)
    res["fetched_at"] = int(time.time())
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(res, separators=(",", ":")))
    os.replace(tmp, OUT)
    print(f"[mispriced] {len(res['pools'])} pools on "
          f"{len(res['coins'])} unbacked coins: "
          + ", ".join(f"{v['symbol']} ({k.split(':')[0]})"
                      for k, v in res["coins"].items()), flush=True)
    return res["pools"]


def load() -> dict:
    """Every mispriced pool flagged so far, keyed chain:address."""
    return {**(_read(SEED).get("pools") or {}),
            **(_read(OUT).get("pools") or {})}
