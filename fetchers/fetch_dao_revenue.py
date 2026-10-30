#!/usr/bin/env python3
"""DAO revenue by source, daily, over a trailing window.

Three sources, each with its own split rule — the whole point of the tab is
that these are NOT the same fraction of the headline number:

  pool swap fees   the DAO's cut of trading+liquidity fees. The cut is the
                   pool's admin_fee, which really does vary: 100% on 3pool,
                   0% on Yield-Basis pools (their fees stay with LPs), 50%
                   on the usual factory pool. The prices API already applies
                   the per-pool admin_fee; we count the WHOLE resulting take
                   (fees_to_dao + fees_to_treasury) as DAO revenue — the
                   ~10% treasury slice is a routing of that revenue, not a
                   reduction of it — and record the split alongside.

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

  pegkeeper        a PegKeeper mints crvUSD into its pool above peg and
                   withdraws+burns below; the value extracted doing that is
                   DAO revenue, claimed as LP tokens via withdraw_profit().
                   The prices API's per-keeper event feed carries a
                   cumulative lifetime profit (LP units) that steps ONLY on
                   Profit events, so daily revenue = the day's cumulative
                   delta x that day's virtual_price. This is claim-time
                   (cash) recognition -- profit accrued but not yet claimed
                   is not booked to any day -- so the series is bursty by
                   nature, matching how the income actually arrives.

Keeps 365 periods of 24 hours so the tab can toggle 30d / 1y without
refetching. The periods end NOW (the last full hour), not at UTC midnight:
the 30d and 1y figures are the last 30 x 24 h and 365 x 24 h, and read the
same way as the pools list's last 24 h. /v1/chains/fees takes any window, so
the newest EXACT periods are asked for directly; older ones are split from
the UTC days, which never change and are cached in dao_revenue_state.json (a
cold start is ~365 calls, every later run is 1-2 plus EXACT). The lending
leg reads the local llm_hist / markets datasets, so it costs no network.
"""
from __future__ import annotations

import bisect
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "data" / "dao_revenue.json"
STATE = HERE / "data" / "dao_revenue_state.json"
API = "https://prices.curve.finance"
CURVE_API = "https://api.curve.finance/api"
UA = {"User-Agent": "curve-sim"}
DAYS = 365          # full window kept in the file; the UI slices 30d / 1y
DAY = 86400
HOUR = 3600
EXACT = 31          # newest periods read directly, older ones from UTC days
MAX_ROWS = 100      # crvUSD snapshot rows per call — the API's hard cap
POOL_ROWS = 300     # pool volume/snapshot rows per call — same idea
POOL_TTL = 24 * 3600  # the per-pool sweep costs thousands of calls and only
                      # finds new earners: every run refreshes the pools that
                      # earned lately (lb_refresh), this one starts daily
LB_BUDGET_S = 400   # sweep seconds per run (the refresh cycle kills this
                    # step at 600 s); what is left resumes next run
LB_ACTIVE_DAYS = 40 # a pool with fees this recently is refreshed every run

# /v1/chains/fees reported EVERY fee field at exactly half its true value for
# days up to 2026-03-01 inclusive, and correctly from 2026-03-02 on. The
# cutover is a clean overnight step, not a drift: measured on the Yield Basis
# pools, whose fees the endpoint reports as `lp_fees_yb`, the ratio of that
# field to the same pools' summed per-pool fees is 0.500 on every day through
# 03-01 and 1.000 on every day from 03-02.
# Arbitrated on-chain rather than between two APIs: 3pool takes a 100% admin
# fee, so its admin_balances delta plus sweeps IS the DAO's revenue. On
# 2025-10-15 that came to $4,062.84; the per-pool endpoint said $3,612.91 and
# the halved chain-level figure implied $1,806.45. The per-pool side is right
# and this endpoint was under-reporting, so the older half is scaled back up.
HALVED_UNTIL = 1772409600     # 2026-03-02 00:00 UTC — first correct day
HALVED_FACTOR = 2.0


def get(path: str, tries: int = 3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(API + path, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:       # a pool or chain the API does not have:
                return None         # thousands in a sweep, no point retrying
            last = e
            time.sleep(1.0)
        except Exception as e:      # transient API hiccup
            last = e
            time.sleep(1.0)
    print(f"[dao-rev]   {path[:70]} failed: {str(last)[:90]}", flush=True)
    return None


def get_curve(path: str, tries: int = 2):
    """api.curve.finance — the pool REGISTRIES live here, not on the prices
    API, whose own per-chain listing omits most side-chain pools."""
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(CURVE_API + path, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(1.0)
    print(f"[dao-rev]   curve{path[:60]} failed: {str(last)[:80]}", flush=True)
    return None


def day_starts(n: int) -> list[int]:
    """UTC midnights, oldest first, excluding today (still accruing)."""
    today = int(time.time()) // DAY * DAY
    return [today - i * DAY for i in range(n, 0, -1)]


def spread(per: dict[int, float], days: list[int],
           carry: bool = False) -> dict[int, float]:
    """Values per UTC day -> the 24 h periods in `days`: a period straddles
    two UTC days and takes each by its share of the hours. carry: the newest
    value goes on into days not reported yet (interest accrues, fees do not);
    only for a series reported up to the last few days, so that a closed
    market does not accrue forever."""
    last = max(per) if per else None
    if last is None or days[-1] + DAY - last > 3 * DAY:
        carry = False
    out: dict[int, float] = {}
    for s in days:
        d = s // DAY * DAY
        f = (s - d) / DAY
        a = per[last] if carry and d > last else per.get(d)
        b = per[last] if carry and d + DAY > last else per.get(d + DAY)
        if a is not None or b is not None:
            out[s] = (a or 0.0) * (1 - f) + (b or 0.0) * f
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def fee_row(r: dict) -> dict:
    agg = r.get("aggregated") or {}
    rows = [c for c in (r.get("chains") or []) if c.get("chain")]
    return {"dao": agg.get("fees_to_dao") or 0.0,
            "lp": agg.get("fees_to_lp") or 0.0,
            "tre": agg.get("fees_to_treasury") or 0.0,
            "ch": {c["chain"]: c.get("fees_to_dao") or 0.0 for c in rows},
            "cht": {c["chain"]: c.get("fees_to_treasury") or 0.0
                    for c in rows}}


def pool_fees(days: list[int], st: dict) -> dict:
    """The protocol's WHOLE take per chain per day: fees_to_dao PLUS the
    ~10% fees_to_treasury slice. The DAO's revenue is 100% of what the
    admin fee collects — that 10% is routed to the treasury, not forgone —
    so the headline counts it and the split is disclosed separately.
    The API applies each pool's admin_fee upstream, so YB pools (0%) and
    3pool (100%) are already handled correctly."""
    # One window per day is the only granularity the endpoint offers, so a
    # full year is 365 calls on a cold cache. Every completed day is final,
    # so cache it and re-fetch only the days we have never seen (plus the
    # newest, which may have been mid-day when we first stored it). Days
    # cached before the schema carried per-chain treasury ("cht") are
    # refetched once.
    cache: dict = st.setdefault("pool_days", {})
    utc = day_starts(DAYS)
    want = [d for d in utc
            if str(d) not in cache or "cht" not in cache[str(d)]]
    if str(utc[-1]) in cache and utc[-1] not in want:
        want.append(utc[-1])

    def one(d: int):
        return d, get(f"/v1/chains/fees?start={d}&end={d + DAY}")

    if want:
        print(f"[dao-rev] pool fees: fetching {len(want)} uncached days "
              f"({len(utc) - len(want)} from cache)", flush=True)
        with ThreadPoolExecutor(8) as ex:
            for d, r in ex.map(one, want):
                if r:
                    cache[str(d)] = fee_row(r)
    else:
        print("[dao-rev] pool fees: all days cached", flush=True)

    totals, lp, treasury = {}, {}, {}
    by_chain: dict[str, dict[int, float]] = {}
    n_fixed = 0
    for d in utc:
        e = cache.get(str(d))
        if not e:
            continue
        k = HALVED_FACTOR if d < HALVED_UNTIL else 1.0
        n_fixed += k != 1.0
        totals[d] = (e["dao"] + e["tre"]) * k
        lp[d], treasury[d] = e["lp"] * k, e["tre"] * k
        cht = e.get("cht") or {}
        for ch, v in (e.get("ch") or {}).items():
            by_chain.setdefault(ch, {})[d] = (v + (cht.get(ch) or 0.0)) * k
    print(f"[dao-rev] pool fees: {len(totals)}/{len(utc)} days, "
          f"{len(by_chain)} chains, treasury slice included"
          + (f", {n_fixed} corrected for the pre-{time.strftime('%Y-%m-%d', time.gmtime(HALVED_UNTIL))}"
             f" upstream halving" if n_fixed else ""), flush=True)
    # UTC days -> the periods ending now; the newest ones read directly
    totals, lp, treasury = (spread(x, days) for x in (totals, lp, treasury))
    by_chain = {c: spread(v, days) for c, v in by_chain.items()}
    with ThreadPoolExecutor(8) as ex:
        for s, r in ex.map(one, days[-EXACT:]):
            if not r:
                continue
            e = fee_row(r)
            totals[s] = e["dao"] + e["tre"]
            lp[s], treasury[s] = e["lp"], e["tre"]
            for c, v in by_chain.items():
                v[s] = (e["ch"].get(c) or 0.0) + (e["cht"].get(c) or 0.0)
    return {"total": [totals.get(d) for d in days],
            "to_lp": [lp.get(d) for d in days],
            "to_treasury": [treasury.get(d) for d in days],
            "halved_until": HALVED_UNTIL, "halved_days": n_fixed,
            "by_chain": {c: [v.get(d) for d in days]
                         for c, v in by_chain.items()}}


def mint_interest(days: list[int]) -> dict:
    """100% of interest on crvUSD minted in the 9 factory mint markets."""
    lst = get("/v1/crvusd/markets/ethereum?fetch_on_chain=false&per_page=50")
    rows = (lst or {}).get("data") or []
    start, end = days[0] // DAY * DAY, days[-1] + DAY

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
            per = spread(per, days, carry=True)
            by_market[key] = [per.get(d) for d in days]
            for d, v in per.items():
                totals[d] = totals.get(d, 0.0) + v
    print(f"[dao-rev] mint interest: {len(by_market)} markets", flush=True)
    return {"total": [totals.get(d) for d in days], "by_market": by_market,
            "coins": coins}


def pegkeeper_profit(days: list[int]) -> dict:
    """Claimed PegKeeper profit per day, valued at the pool's virtual price.

    The per-keeper event feed reports a cumulative lifetime profit (in LP
    tokens) on every event, and it steps only on Profit events. Walking the
    feed newest-first until one Profit event BEFORE the window is in hand
    gives an exact baseline, so each in-window Profit event's delta is the
    LP amount claimed right then. Keepers on the same pool (retired +
    current generations) are merged into one market row.
    """
    lst = get("/v1/crvusd/pegkeepers/ethereum")
    keepers = (lst or {}).get("keepers") or []
    start = days[0]

    def one(k):
        if not (k.get("total_profit") or 0) > 0:
            return k, []          # never claimed anything, feed is empty
        evs, page = [], 1
        while page <= 60:
            r = get(f"/v1/crvusd/pegkeepers/ethereum/{k['address']}"
                    f"?page={page}&pagination={POOL_ROWS}")
            chunk = (r or {}).get("events") or []
            evs += [e for e in chunk if e.get("action_type") == "Profit"]
            # stop once a pre-window Profit event anchors the baseline, or
            # the feed ends (keeper born inside the window -> baseline 0)
            if len(chunk) < POOL_ROWS or any(
                    e["timestamp"] < start for e in evs):
                break
            page += 1
        return k, sorted(evs, key=lambda e: e["block_number"])

    # daily virtual_price per pool, to value LP-token claims in USD
    def vps(pool):
        out, w_end = {}, days[-1] + DAY
        while w_end > start:
            w_start = max(start, w_end - POOL_ROWS * DAY)
            rows = ((get(f"/v1/snapshots/ethereum/{pool}?interval=day"
                         f"&start={w_start}&end={w_end}") or {})
                    .get("data") or [])
            if not rows:
                break
            for x in rows:
                ts = x.get("timestamp")
                vp = x.get("virtual_price")
                if ts is not None and vp:
                    out[int(ts) // DAY * DAY] = float(vp) / 1e18
            w_end = w_start
        return out

    by_market: dict[str, dict[int, float]] = {}
    names: dict[str, str] = {}
    coins: dict[str, str] = {}
    with ThreadPoolExecutor(5) as ex:
        for k, evs in ex.map(one, keepers):
            base = None
            for e in evs:                      # newest pre-window cum
                if e["timestamp"] < start:
                    base = e["profit"]
            pool = (k.get("pool_address") or "").lower()
            per = by_market.setdefault(pool, {})
            prev = base if base is not None else 0.0
            for e in evs:
                if e["timestamp"] < start:
                    continue
                d = start + (int(e["timestamp"]) - start) // DAY * DAY
                lp = max(0.0, (e.get("profit") or 0.0) - prev)
                prev = e.get("profit") or prev
                if d <= days[-1] and lp > 0:
                    per[d] = per.get(d, 0.0) + lp
            names[pool] = k.get("pool") or pool
            other = [c for c in (k.get("pair") or [])
                     if (c.get("symbol") or "") != "crvUSD"]
            if other:
                coins[pool] = other[0].get("address") or ""

    totals: dict[int, float] = {}
    out_markets: dict[str, list] = {}
    out_coins: dict[str, str] = {}
    for pool, per in by_market.items():
        if not per:
            continue
        vp = vps(pool)
        usd_per = {}
        for d, lp in per.items():
            v = lp * (vp.get(d // DAY * DAY)
                      or (vp[max(vp)] if vp else 1.0))
            usd_per[d] = v
            totals[d] = totals.get(d, 0.0) + v
        key = f"{names[pool]}|{pool}"
        out_markets[key] = [usd_per.get(d) for d in days]
        out_coins[key] = coins.get(pool, "")
    tot = sum(totals.values())
    print(f"[dao-rev] pegkeeper profit: {len(out_markets)} pools, "
          f"${tot:,.0f} over {len(days)}d", flush=True)
    return {"total": [totals.get(d) for d in days], "by_market": out_markets,
            "coins": out_coins}


def pool_leaderboard(days: list[int], st: dict, force: bool = False,
                     deadline: float = float("inf")) -> dict:
    """DAO swap-fee revenue per POOL, across every chain.

    /v1/chains/fees only aggregates by chain, so the per-pool split has to be
    rebuilt from each pool's own daily fees x that day's admin_fee:
      daily DAO fee = volume-endpoint `fees` (total pool fee) * admin_fee
    admin_fee is read per day rather than assumed, because it genuinely
    differs per pool (3pool 100%, Yield Basis 0%, typical factory 50%) and
    can be changed by a DAO vote.

    The universe is every pool in every registry of every chain, from
    getPlatforms + getPools, unioned with data/census.json. Two traps this
    avoids:

      no TVL gate -- fees follow VOLUME, and the two come apart badly on
      the long tail. Every dollar of bsc's DAO fees comes from pools under
      $25k of TVL, and on ethereum 140 such pools earned $38k in 30 days.
      `usdTotal` is also 0 for any pool whose tokens cannot be priced, so a
      TVL gate drops real earners silently rather than visibly.

      not the prices API's own pool list -- /v1/chains/{chain} knows only 35
      of bsc's 445 pools and 105 of base's 1116, yet its per-pool volume
      endpoint happily serves the ones it omits. Enumerating from the
      registries and asking about each pool recovers $19k (bsc) and $65k
      (base) of 30-day fees that listing alone would miss.
    """
    lb_sweep(days, st, force, deadline)
    lb_refresh(days, st)
    # the last 30 x 24 h and 365 x 24 h: whole UTC days, today's so far, and
    # the oldest day by its share of the hours
    def rolling(r: dict, lo: int) -> float:
        first = lo // DAY * DAY
        return sum(v * (1 - (lo - first) / DAY) if int(d) == first else v
                   for d, v in r.items() if int(d) >= first)

    out = {}
    for key, e in (st.get("pool_lb") or {}).items():
        if "r" not in e:                 # built before the rows were kept
            out[key] = e
            continue
        tot365 = rolling(e["r"], days[0])
        if tot365 > 0.005:
            out[key] = {"n": e["n"], "c": e["c"], "k": e["k"],
                        "d30": rolling(e["r"], days[-30]), "d365": tot365}
    return out


def lb_universe() -> list:
    try:
        cen = json.loads((HERE / "data" / "census.json").read_text())["pools"]
    except (OSError, KeyError, ValueError):
        cen = {}
        print("[dao-rev] census.json missing — universe from the API only",
              flush=True)
    seen: dict = {}
    for ch, rows in cen.items():
        for r in rows:
            if isinstance(r, list) and len(r) >= 2:
                seen[f"{ch}:{r[0].lower()}"] = (
                    ch, r[0].lower(), r[1], (r[3] if len(r) > 3 else []) or [])
    n_census = len(seen)
    plat = ((get_curve("/getPlatforms") or {}).get("data") or {})
    jobs = [(ch, reg) for ch, regs in (plat.get("platforms") or {}).items()
            for reg in regs]

    def registry(t):
        ch, reg = t
        return ch, (((get_curve(f"/getPools/{ch}/{reg}") or {}).get("data")
                     or {}).get("poolData") or [])

    with ThreadPoolExecutor(8) as ex:
        for ch, rows in ex.map(registry, jobs):
            for p in rows:
                a = (p.get("address") or "").lower()
                key = f"{ch}:{a}"
                if not a or key in seen:
                    continue
                coins = [(c.get("address") or "").lower()
                         for c in (p.get("coins") or [])]
                seen[key] = (ch, a, p.get("name") or a, coins[:3])
    targets = list(seen.values())
    print(f"[dao-rev] pool leaderboard universe: {len(targets)} pools across "
          f"{len(plat.get('platforms') or {})} chains ({n_census} from "
          f"census, {len(targets) - n_census} added from the registries)",
          flush=True)
    return targets


def lb_pool(t, start: int, end: int):
    """One pool's DAO fees per UTC day, today's so far included."""
    ch, addr, label, coins = t
    # A pool's admin_fee is the whole protocol take, and since the headline
    # counts the whole take too (fees_to_dao plus the ~10% treasury slice),
    # fees x admin_fee is directly the right quantity — no share scaling.

    def windows_between(fetch, lo, hi):
        """Walk the range backwards.

        The endpoints cap at POOL_ROWS ROWS, which is not POOL_ROWS days:
        /v1/snapshots returns ~6 rows a day even at interval=day, so a
        300-day request comes back holding only the newest ~50 days. Step
        by the oldest row actually returned, not by the width of the window
        asked for, or the walk skips the 250 days in between.
        """
        out = []
        w_end = hi
        while w_end > lo:
            w_start = max(lo, w_end - POOL_ROWS * DAY)
            rows = fetch(w_start, w_end)
            if not rows:
                break
            out += rows
            ts = [int(x["timestamp"]) for x in rows
                  if x.get("timestamp") is not None]
            if len(rows) < POOL_ROWS:
                w_end = w_start          # whole window fit: move past it
            elif ts and min(ts) < w_end:
                w_end = min(ts)          # capped: resume at the oldest row
            else:
                break                    # no progress possible
        return out

    vol = windows_between(lambda a, b: ((get(
        f"/v1/volume/usd/{ch}/{addr}?interval=day&start={a}&end={b}")
        or {}).get("data") or []), start, end)
    earned = [x for x in vol
              if x.get("timestamp") is not None and (x.get("fees") or 0) > 0
              and int(x["timestamp"]) >= start]
    if not earned:
        return None
    # admin_fee is only needed over the span the pool actually earned in,
    # which for the long tail is a few days rather than the whole year
    lo = min(int(x["timestamp"]) for x in earned) // DAY * DAY
    hi = max(int(x["timestamp"]) for x in earned) // DAY * DAY + DAY
    snap = windows_between(lambda a, b: ((get(
        f"/v1/snapshots/{ch}/{addr}?start={a}&end={b}")
        or {}).get("data") or []), lo, hi)
    af = {int(x["timestamp"]) // DAY * DAY: (x["admin_fee"] or 0) / 1e10
          for x in snap if x.get("timestamp") is not None
          and x.get("admin_fee") is not None}
    keys = sorted(af)
    r: dict[str, float] = {}
    for x in earned:
        d = int(x["timestamp"]) // DAY * DAY
        share = af.get(d)
        if share is None and keys:
            # nearest snapshot IN TIME, preferring one at or before the
            # day — never simply the newest, which would backdate a
            # later governance change onto every earlier day
            i = bisect.bisect_right(keys, d)
            share = af[keys[i - 1]] if i else af[keys[0]]
        v = (x.get("fees") or 0.0) * (share or 0.0)
        if v > 0:
            r[str(d)] = round(v, 2)
    if not r:
        return None
    return f"{ch}:{addr}", {"n": label, "c": ch, "k": coins[:3],
                            "s": af[keys[-1]] if keys else 0.0, "r": r}


def lb_sweep(days: list[int], st: dict, force: bool, deadline: float) -> None:
    """Every pool's year, resumable: a sweep takes thousands of calls, more
    than one run's limit, so it stops at `deadline` and goes on next run. The
    finished sweep replaces the previous one at once, so the list never shows
    a half-built state."""
    scan = st.get("lb_scan")
    age = time.time() - (st.get("pools_at") or 0)
    if not scan:
        if st.get("pool_lb") and not force and age < POOL_TTL:
            print(f"[dao-rev] pool sweep: last one {age / 3600:.1f}h old, "
                  f"{len(st['pool_lb'])} pools", flush=True)
            return
        scan = st["lb_scan"] = {"targets": lb_universe(), "i": 0, "out": {}}
    targets, start = scan["targets"], days[0] // DAY * DAY - DAY
    end = days[-1] + 2 * DAY
    t0, i0 = time.time(), scan["i"]
    with ThreadPoolExecutor(12) as ex:
        while scan["i"] < len(targets) and time.time() < deadline:
            block = targets[scan["i"]:scan["i"] + 120]
            for r in ex.map(lambda t: lb_pool(t, start, end), block):
                if r:
                    scan["out"][r[0]] = r[1]
            scan["i"] += len(block)
            save_state(st)
    print(f"[dao-rev] pool sweep: {scan['i']}/{len(targets)} pools "
          f"(+{scan['i'] - i0} in {time.time() - t0:.0f}s), "
          f"{len(scan['out'])} with fees", flush=True)
    if scan["i"] >= len(targets):
        st["pool_lb"] = scan["out"]
        st["pools_at"] = int(time.time())
        st.pop("lb_scan", None)


def lb_refresh(days: list[int], st: dict) -> None:
    """Yesterday and today for every pool that earned lately, each run: the
    sweep is far too slow to keep a window ending now up to date. A day's
    share comes from the pool's newest admin_fee."""
    pools = st.get("pool_lb") or {}
    today = int(time.time()) // DAY * DAY
    cut = str(today - LB_ACTIVE_DAYS * DAY)
    active = [(k, e) for k, e in pools.items()
              if "r" in e and e.get("s") and max(e["r"]) >= cut]

    def one(t):
        k, e = t
        ch, _, addr = k.partition(":")
        return e, (get(f"/v1/volume/usd/{ch}/{addr}?interval=day"
                       f"&start={today - 2 * DAY}&end={today + DAY}") or {})

    t0 = time.time()
    n = 0
    with ThreadPoolExecutor(12) as ex:
        for e, r in ex.map(one, active):
            for x in r.get("data") or []:
                if x.get("timestamp") is None or x.get("fees") is None:
                    continue
                d = int(x["timestamp"]) // DAY * DAY
                e["r"][str(d)] = round(x["fees"] * e["s"], 2)
                n += 1
    old = str(days[0] - 2 * DAY)
    for e in pools.values():
        if "r" in e:
            e["r"] = {d: v for d, v in e["r"].items() if d >= old}
    print(f"[dao-rev] pool refresh: {len(active)} recent earners, {n} days "
          f"in {time.time() - t0:.0f}s", flush=True)


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
    first = days[0] // DAY * DAY
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
            if d < first:
                continue
            debt = du[i] if i < len(du) else None
            apr = ba[i] if i < len(ba) else None
            if debt is None or apr is None:
                continue
            per[d] = debt * (apr / 100.0) / 365.0 * share
        if per:
            mk_key = f"{m.get('name', '?')}|{key}"
            coins[mk_key] = colad.get(key.lower(), "")
            per = spread(per, days, carry=True)
            by_market[mk_key] = [per.get(d) for d in days]
            for d, v in per.items():
                totals[d] = totals.get(d, 0.0) + v
    print(f"[dao-rev] lending V2 interest: {len(by_market)} markets",
          flush=True)
    return {"total": [totals.get(d) for d in days], "by_market": by_market,
            "coins": coins}


def main() -> None:
    t0 = time.time()
    # 24 h periods ending at the last full hour
    anchor = int(t0) // HOUR * HOUR
    days = [anchor - i * DAY for i in range(DAYS, 0, -1)]
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
            "pegkeeper": pegkeeper_profit(days),
        },
    }
    lb = pool_leaderboard(days, st, force, t0 + LB_BUDGET_S)
    pf = out["sources"]["pool_fees"]
    pf["by_pool"] = lb
    pf["pools_at"] = st.get("pools_at")
    # The leaderboard is an INDEPENDENT reconstruction (per-pool fees x that
    # pool's admin_fee), so it is a real cross-check on the chain-level series
    # rather than a restatement of it. Both are recorded, along with how far
    # apart they land, so the tab can show the residual instead of implying
    # the two agree. What is left after the coverage and halving fixes is the
    # fees the per-pool `fees` field does not carry -- the endpoint reports
    # trading fees, while the chain-level total also counts the liquidity
    # fees charged on imbalanced deposits and withdrawals.
    for w, n in (("d30", 30), ("d365", len(days))):
        chain_tot = sum(v for v in (pf["total"] or [])[-n:] if v)
        lb_tot = sum(p[w] for p in lb.values())
        pf.setdefault("lb_total", {})[w] = lb_tot
        pf.setdefault("chain_total", {})[w] = chain_tot
        gap = (lb_tot / chain_tot - 1) * 100 if chain_tot else 0.0
        pf.setdefault("lb_gap_pct", {})[w] = round(gap, 2)
        print(f"[dao-rev]   {n}d pool fees: leaderboard ${lb_tot:,.0f} vs "
              f"chain-level ${chain_tot:,.0f}  ({gap:+.1f}%)", flush=True)
    save_state(st)
    s = out["sources"]
    for k in s:
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
