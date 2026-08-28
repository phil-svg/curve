#!/usr/bin/env python3
"""DAO revenue by source, daily, over a trailing window.

Three sources, each with its own split rule — the whole point of the tab is
that these are NOT the same fraction of the headline number:

  pool swap fees   the DAO's cut of trading+liquidity fees. The cut is the
                   pool's admin_fee, which really does vary: 100% on 3pool,
                   0% on Yield-Basis pools (their fees stay with LPs), 50%
                   on the usual factory pool. The prices API already applies
                   the per-pool admin_fee and hands back fees_to_dao, so we
                   take that rather than re-deriving it from volume x fee.

  crvUSD mint      interest paid on crvUSD minted against collateral. There
                   are no lenders in a mint market, so 100% of the interest
                   is DAO revenue. Only the 9 controllers of the mint
                   ControllerFactory count -- Yield Basis and Resupply
                   (sreUSD) borrow crvUSD outside that factory, so they
                   never appear here and their interest is not DAO revenue.
                   daily = total_debt_usd * rate / 365   (rate is decimal APR;
                   validated against /v1/dao/fees/crvusd/weekly: our estimate
                   $5,960/wk vs $5,950-6,439/wk actually collected on WBTC.)

  LlamaLend V2     lend markets take an admin cut of borrower interest --
                   Controller.admin_percentage(), 10% on every V2 market
                   today. V1 lend markets have no such cut (the getter does
                   not exist), so 100% of their interest goes to lenders and
                   they contribute nothing here.

Keeps a 365-day daily series so the tab can toggle 30d / 1y without
refetching. Completed days never change, so chain-fee days are cached in
dao_revenue_state.json and only new days are fetched (a cold start is ~365
calls, every later run is 1-2). The lending leg reads the local llm_hist /
markets datasets, so it costs no network at all.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "dao_revenue.json"
STATE = HERE / "data" / "dao_revenue_state.json"
API = "https://prices.curve.finance"
UA = {"User-Agent": "curve-sim"}
DAYS = 365          # full window kept in the file; the UI slices 30d / 1y
DAY = 86400
MAX_ROWS = 100      # crvUSD snapshot rows per call — the API's hard cap
POOL_ROWS = 300     # pool volume/snapshot rows per call — same idea
POOL_TTL = 12 * 3600  # per-pool leaderboard costs ~900 calls; rebuild at most
                      # this often and serve the cache in between


def get(path: str, tries: int = 3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(API + path, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:      # transient API hiccup
            last = e
            time.sleep(1.0)
    print(f"[dao-rev]   {path[:70]} failed: {str(last)[:90]}", flush=True)
    return None


def day_starts(n: int) -> list[int]:
    """UTC midnights, oldest first, excluding today (still accruing)."""
    today = int(time.time()) // DAY * DAY
    return [today - i * DAY for i in range(n, 0, -1)]


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def pool_fees(days: list[int], st: dict) -> dict:
    """fees_to_dao per chain per day. The API applies each pool's admin_fee,
    so YB pools (0%) and 3pool (100%) are already handled correctly."""
    # One window per day is the only granularity the endpoint offers, so a
    # full year is 365 calls on a cold cache. Every completed day is final,
    # so cache it and re-fetch only the days we have never seen (plus the
    # newest, which may have been mid-day when we first stored it).
    cache: dict = st.setdefault("pool_days", {})
    want = [d for d in days if str(d) not in cache]
    if days and str(days[-1]) in cache:
        want.append(days[-1])

    def one(d: int):
        return d, get(f"/v1/chains/fees?start={d}&end={d + DAY}")

    if want:
        print(f"[dao-rev] pool fees: fetching {len(want)} uncached days "
              f"({len(days) - len(want)} from cache)", flush=True)
        with ThreadPoolExecutor(8) as ex:
            for d, r in ex.map(one, want):
                if not r:
                    continue
                agg = r.get("aggregated") or {}
                cache[str(d)] = {
                    "dao": agg.get("fees_to_dao") or 0.0,
                    "lp": agg.get("fees_to_lp") or 0.0,
                    "tre": agg.get("fees_to_treasury") or 0.0,
                    "ch": {c["chain"]: c.get("fees_to_dao") or 0.0
                           for c in (r.get("chains") or []) if c.get("chain")},
                }
    else:
        print("[dao-rev] pool fees: all days cached", flush=True)

    totals, lp, treasury = {}, {}, {}
    by_chain: dict[str, dict[int, float]] = {}
    for d in days:
        e = cache.get(str(d))
        if not e:
            continue
        totals[d], lp[d], treasury[d] = e["dao"], e["lp"], e["tre"]
        for ch, v in (e.get("ch") or {}).items():
            by_chain.setdefault(ch, {})[d] = v
    print(f"[dao-rev] pool fees: {len(totals)}/{len(days)} days, "
          f"{len(by_chain)} chains", flush=True)
    return {"total": [totals.get(d) for d in days],
            "to_lp": [lp.get(d) for d in days],
            "to_treasury": [treasury.get(d) for d in days],
            "by_chain": {c: [v.get(d) for d in days]
                         for c, v in by_chain.items()}}


def mint_interest(days: list[int]) -> dict:
    """100% of interest on crvUSD minted in the 9 factory mint markets."""
    lst = get("/v1/crvusd/markets/ethereum?fetch_on_chain=false&per_page=50")
    rows = (lst or {}).get("data") or []
    start, end = days[0], days[-1] + DAY

    def one(m):
        ctrl = m.get("address")
        col = m.get("collateral_token") or {}
        sym = col.get("symbol") or "?"
        cadr = col.get("address") or ""
        per: dict[int, float] = {}
        # the endpoint returns at most MAX_ROWS rows newest-first, so walk
        # the window backwards in chunks instead of asking for a year
        w_end = end
        while w_end > start:
            w_start = max(start, w_end - MAX_ROWS * DAY)
            r = get(f"/v1/crvusd/markets/ethereum/{ctrl}/snapshots"
                    f"?agg=day&start={w_start}&end={w_end}")
            chunk = (r or {}).get("data") or []
            for row in chunk:
                dt = row.get("dt")
                if not dt:
                    continue
                ts = int(datetime.fromisoformat(dt).replace(
                    tzinfo=timezone.utc).timestamp()) // DAY * DAY
                debt = row.get("total_debt_usd") or 0.0
                rate = row.get("rate") or 0.0        # decimal APR
                per[ts] = debt * rate / 365.0
            if not chunk:
                break
            w_end = w_start
        return ctrl, sym, cadr, per

    by_market: dict[str, list] = {}
    coins: dict[str, str] = {}
    totals: dict[int, float] = {}
    with ThreadPoolExecutor(5) as ex:
        for ctrl, sym, cadr, per in ex.map(one, rows):
            key = f"{sym}|{ctrl}"
            coins[key] = cadr
            by_market[key] = [per.get(d) for d in days]
            for d, v in per.items():
                if d in set(days):
                    totals[d] = totals.get(d, 0.0) + v
    print(f"[dao-rev] mint interest: {len(by_market)} markets", flush=True)
    return {"total": [totals.get(d) for d in days], "by_market": by_market,
            "coins": coins}


def pool_leaderboard(days: list[int], st: dict, force: bool = False) -> dict:
    """DAO swap-fee revenue per POOL, across every chain.

    /v1/chains/fees only aggregates by chain, so the per-pool split has to be
    rebuilt from each pool's own daily fees x that day's admin_fee:
      daily DAO fee = volume-endpoint `fees` (total pool fee) * admin_fee
    admin_fee is read per day rather than assumed, because it genuinely
    differs per pool (3pool 100%, Yield Basis 0%, typical factory 50%) and
    can be changed by a DAO vote.

    The universe is data/census.json (~450 pools >= $25k TVL). Long-tail
    pools below that threshold are not covered, so the caller reports the
    covered fraction rather than implying the list is exhaustive.
    """
    cache = st.get("pool_lb") or {}
    age = time.time() - (st.get("pools_at") or 0)
    if cache and not force and age < POOL_TTL:
        print(f"[dao-rev] pool leaderboard: cached "
              f"({age / 3600:.1f}h old, {len(cache)} pools)", flush=True)
        return cache

    try:
        cen = json.loads((HERE / "data" / "census.json").read_text())["pools"]
    except (OSError, KeyError, ValueError):
        print("[dao-rev] census.json missing — no pool leaderboard",
              flush=True)
        return cache
    targets = [(ch, r[0], r[1], (r[3] if len(r) > 3 else []) or [])
               for ch, rows in cen.items() for r in rows
               if isinstance(r, list) and len(r) >= 2]
    start, end = days[0], days[-1] + DAY
    d30 = set(days[-30:])
    dayset = set(days)

    def windows(fetch):
        """Walk the range backwards; the endpoints cap at POOL_ROWS rows."""
        out = []
        w_end = end
        while w_end > start:
            w_start = max(start, w_end - POOL_ROWS * DAY)
            rows = fetch(w_start, w_end)
            if not rows:
                break
            out += rows
            w_end = w_start
        return out

    def one(t):
        ch, addr, label, coins = t
        vol = windows(lambda a, b: ((get(
            f"/v1/volume/usd/{ch}/{addr}?interval=day&start={a}&end={b}")
            or {}).get("data") or []))
        if not vol:
            return None
        snap = windows(lambda a, b: ((get(
            f"/v1/snapshots/{ch}/{addr}?interval=day&start={a}&end={b}")
            or {}).get("data") or []))
        af = {int(x["timestamp"]) // DAY * DAY: (x.get("admin_fee") or 0) / 1e10
              for x in snap if x.get("timestamp") is not None}
        tot30 = tot365 = 0.0
        for x in vol:
            ts = x.get("timestamp")
            if ts is None:
                continue
            d = int(ts) // DAY * DAY
            if d not in dayset:
                continue
            share = af.get(d)
            if share is None:            # no snapshot that day: nearest known
                share = af.get(max(af), 0.0) if af else 0.0
            v = (x.get("fees") or 0.0) * share
            tot365 += v
            if d in d30:
                tot30 += v
        if tot365 <= 0.005:
            return None
        return f"{ch}:{addr}", {"n": label, "c": ch, "k": coins[:3],
                                "d30": tot30, "d365": tot365}

    print(f"[dao-rev] pool leaderboard: rebuilding over {len(targets)} "
          f"census pools (~{len(targets) * 2} calls)", flush=True)
    t0 = time.time()
    out: dict = {}
    with ThreadPoolExecutor(8) as ex:
        for r in ex.map(one, targets):
            if r:
                out[r[0]] = r[1]
    st["pool_lb"] = out
    st["pools_at"] = int(time.time())
    print(f"[dao-rev] pool leaderboard: {len(out)} pools with fees "
          f"in {time.time() - t0:.0f}s", flush=True)
    return out


def lend_interest(days: list[int]) -> dict:
    """LlamaLend V2's admin cut of borrower interest, from the local LLM
    dataset. V1 has no cut, so it is skipped entirely."""
    try:
        llm = json.loads((HERE / "data" / "llm.json").read_text())
    except OSError:
        print("[dao-rev] llm.json missing — lending leg empty", flush=True)
        return {"total": [None] * len(days), "by_market": {}}
    shares, colad = {}, {}
    try:
        mk = json.loads((HERE / "data" / "markets.json").read_text())
        for lst in mk["groups"].values():
            for m in lst:
                k = f"{m['chain']}:{m['controller'].lower()}"
                colad[k] = (m.get("collateral") or {}).get("addr") or ""
                if m.get("admin_fee_share_pct"):
                    shares[k] = m["admin_fee_share_pct"] / 100.0
    except (OSError, KeyError):
        pass

    by_market: dict[str, list] = {}
    coins: dict[str, str] = {}
    totals: dict[int, float] = {}
    dayset = set(days)
    for key, m in (llm.get("markets") or {}).items():
        if m.get("group") != "LLV2":
            continue
        share = shares.get(key.lower())
        if not share:            # no admin_percentage -> no DAO revenue
            continue
        ch, _, ctrl = key.partition(":")
        f = HERE / "data" / "llm_hist" / f"{ch}_{ctrl}.json"
        if not f.is_file():
            continue
        h = json.loads(f.read_text())
        t, du, ba = h.get("t") or [], h.get("du") or [], h.get("ba") or []
        per: dict[int, float] = {}
        for i, ts in enumerate(t):
            d = int(ts) // DAY * DAY
            if d not in dayset:
                continue
            debt = du[i] if i < len(du) else None
            apr = ba[i] if i < len(ba) else None
            if debt is None or apr is None:
                continue
            per[d] = debt * (apr / 100.0) / 365.0 * share
        if per:
            mk_key = f"{m.get('name', '?')}|{key}"
            coins[mk_key] = colad.get(key.lower(), "")
            by_market[mk_key] = [per.get(d) for d in days]
            for d, v in per.items():
                totals[d] = totals.get(d, 0.0) + v
    print(f"[dao-rev] lending V2 interest: {len(by_market)} markets",
          flush=True)
    return {"total": [totals.get(d) for d in days], "by_market": by_market,
            "coins": coins}


def main() -> None:
    t0 = time.time()
    days = day_starts(DAYS)
    st = load_state()
    force = "--pools" in sys.argv
    out = {
        "fetched_at": int(time.time()),
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "days": days,
        "sources": {
            "pool_fees": pool_fees(days, st),
            "mint_interest": mint_interest(days),
            "lend_v2_interest": lend_interest(days),
        },
    }
    lb = pool_leaderboard(days, st, force)
    pf = out["sources"]["pool_fees"]
    pf["by_pool"] = lb
    pf["pools_at"] = st.get("pools_at")
    # The leaderboard is an INDEPENDENT reconstruction (per-pool fees x that
    # pool's admin_fee) and does not tie exactly to the chain-level series:
    # over 30d it lands a bit under (census only covers pools >= $25k TVL),
    # but on older dates it runs ABOVE, because /v1/chains/fees under-reports
    # historically -- spot-checked 2025-12-21 ethereum, where every census
    # pool has a same-day admin_fee snapshot and the per-pool sum is verifiable
    # ($6,006) against the endpoint's $3,143. Record both, claim neither is
    # the other's percentage.
    for w, n in (("d30", 30), ("d365", len(days))):
        chain_tot = sum(v for v in (pf["total"] or [])[-n:] if v)
        lb_tot = sum(p[w] for p in lb.values())
        pf.setdefault("lb_total", {})[w] = lb_tot
        pf.setdefault("chain_total", {})[w] = chain_tot
        print(f"[dao-rev]   {n}d pool fees: leaderboard ${lb_tot:,.0f} vs "
              f"chain-level ${chain_tot:,.0f}", flush=True)
    save_state(st)
    s = out["sources"]
    for k in ("pool_fees", "mint_interest", "lend_v2_interest"):
        tot = sum(v for v in s[k]["total"] if v)
        out.setdefault("totals_window", {})[k] = tot
        print(f"[dao-rev]   {k:<18} {len(days)}d ${tot:,.0f}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out))
    os.replace(tmp, OUT)
    print(f"[dao-rev] wrote {OUT} in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
