"""Tiny localhost UI for the generalised bad-debt sim.

Run:   python3 src/ui_server.py [--port 8765]
Open:  http://127.0.0.1:8765/

Endpoints:
  GET  /             -> HTML page (form + inline SVG chart)
  GET  /onchain      -> on-chain measured bad-debt series (fixed reference)
  POST /run          -> body JSON with sim params, runs pipeline, returns per-block series

Every /run invocation calls build_ema_precompute.py + synth_bad_debt.py as
subprocesses (same pipeline the CLI uses); intermediate files land in tmp/
and get cleaned up after the response is sent.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_R = Path(__file__).resolve().parent
for _d in (_R, _R / "sim", _R / "fetchers"):
    sys.path.insert(0, str(_d))
import single_user  # noqa: E402
import venues  # noqa: E402

HERE = Path(__file__).resolve().parent
CASE = HERE.parent
V1SIM = CASE.parent.parent
BUILD_SCRIPT = HERE / "sim" / "build_ema_precompute.py"
SYNTH_SCRIPT = HERE / "sim" / "synth_bad_debt.py"
ROUTED_SCRIPT = HERE / "sim" / "routed_sim.py"
# C++ routed engine + the fixed gas model it runs under (user decision:
# 100 gwei hardcoded; ETH price only sets the $ cost of that gas).
CPP_ENGINE = HERE / "bin" / "routed_engine"
BASE_FEE_GWEI = 100.0        # legacy, no longer priced in
ETH_PRICE_USD = 2000.0       # legacy, no longer priced in
GAS_USD = 10.0               # flat dollar gas per tx (arb round trip / hard liq)


def _aggregate_arb_log(path: Path, stride: int = 1, last_id: int | None = None) -> dict:
    """Per-block + total soft-liq stats from an arb-log file (same schema for
    the C++ sweep and routed_sim).
      LIQ  (i=0): dx = crvUSD in → usd = dx
      DELIQ(i=1): dx = coll in   → usd = dx × target price
    PnL is an inventory mark at the schedule price.

    stride > 1: the C++ engine decimates rows to buckets whose ids are the
    bucket-END step (stride-1, 2*stride-1, …, last). Trades on non-emitted
    steps are folded into their bucket's id so the SL chart's per-row lookup
    still finds them."""
    def bucket(b: int) -> int:
        if stride <= 1:
            return b
        e = ((b // stride) + 1) * stride - 1
        return min(e, last_id) if last_id is not None else e
    per_block = {}
    tot = {"liq_usd": 0.0, "deliq_usd": 0.0, "liq_n": 0, "deliq_n": 0,
           "liq_pnl": 0.0, "deliq_pnl": 0.0}
    try:
        for tr in json.loads(path.read_text()):
            dx = int(tr["dx"]) / 1e18
            if dx == 0:
                continue
            dy = int(tr["dy"]) / 1e18
            tgt = int(tr["target_p"]) / 1e18
            if int(tr["i"]) == 0:
                usd = dx
                pnl = dy * tgt - dx
                d = "LIQ"
                tot["liq_usd"] += usd
                tot["liq_n"] += 1
                tot["liq_pnl"] += pnl
            else:
                usd = dx * tgt
                pnl = dy - dx * tgt
                d = "DELIQ"
                tot["deliq_usd"] += usd
                tot["deliq_n"] += 1
                tot["deliq_pnl"] += pnl
            bid = bucket(int(tr["block"]))
            cur = per_block.get(str(bid))
            if cur:
                cur["usd"] += round(usd)
                cur["pnl"] += round(pnl)
            else:
                per_block[str(bid)] = {"usd": round(usd), "dir": d,
                                       "pnl": round(pnl)}
    except FileNotFoundError:
        pass
    return {"per_block": per_block,
            "totals": {k: (round(v) if isinstance(v, float) else v)
                       for k, v in tot.items()}}
ONCHAIN_FILE = HERE / "results" / "onchain_measured_bad_debt.json"
EVENTS_FILE  = V1SIM / "data" / "events" / "merged_23549899_23550007.json"
# The sim runs on ONE abstracted borrower, so the real Deposit/Withdraw/UserState
# events (which name real addresses that do not exist in this book) must not be
# replayed. This file keeps only BlockTick + SetRate.
BLOCKTICKS_FILE = V1SIM / "data" / "events" / "blockticks_23549899_23550007.json"
SPOT_FILE    = HERE / "external" / "chart2_prices.json"
_onchain_sl_cache = None


def onchain_sl():
    """Real soft-liq/de-liq volume per block from the 18 actual on-chain
    TokenExchange events. DELIQ legs (CRV in) valued at that block's real
    tricrypto spot. Static — cached after first read."""
    global _onchain_sl_cache
    if _onchain_sl_cache is not None:
        return _onchain_sl_cache
    spot = {r["block"]: r["spot"] for r in json.loads(SPOT_FILE.read_text())}
    per_block = {}
    tot = {"liq_usd": 0.0, "deliq_usd": 0.0, "liq_n": 0, "deliq_n": 0,
           "liq_pnl": 0.0, "deliq_pnl": 0.0}
    for e in json.loads(EVENTS_FILE.read_text()):
        if e.get("kind") != "TokenExchange":
            continue
        b = e["block"]
        sold   = int(e["sold"])   / 1e18
        bought = int(e["bought"]) / 1e18
        p = spot.get(b, 0)
        # Inventory-mark PnL: value the CRV leg at the block's real tricrypto spot.
        if int(e["i"]) == 0:
            usd = sold;         pnl = bought * p - sold;   d = "LIQ"
            tot["liq_usd"] += usd;   tot["liq_n"] += 1;   tot["liq_pnl"] += pnl
        else:
            usd = sold * p;     pnl = bought - sold * p;   d = "DELIQ"
            tot["deliq_usd"] += usd; tot["deliq_n"] += 1; tot["deliq_pnl"] += pnl
        cur = per_block.get(str(b))
        if cur:
            cur["usd"] += round(usd); cur["pnl"] += round(pnl)
        else:
            per_block[str(b)] = {"usd": round(usd), "dir": d, "pnl": round(pnl)}
    _onchain_sl_cache = {"per_block": per_block,
                         "totals": {k: (round(v) if isinstance(v, float) else v)
                                    for k, v in tot.items()}}
    return _onchain_sl_cache
INDEX_HTML   = HERE / "ui_index.html"
SCRATCH = HERE / "tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)

# Single progress file: _run_lock serializes sims, so there is only ever one.
PROGRESS_FILE = SCRATCH / "progress.json"
# S.L./D.L. sweep heartbeat — same single-file reasoning.
SLDL_PROG = SCRATCH / "sldl_progress.json"
# S.L./D.L. sweeps run in the visitor's browser (wasm engines, /wasm/*).
# The whole server-side compute path below is kept intact — flip this to
# re-enable POST /sldl_run.
SLDL_SERVER_COMPUTE = False
_PK_CACHE: dict = {"at": 0.0, "data": None, "busy": False}  # /pegkeeper


def _pk_refresh() -> None:
    """Fetch PegKeeper state into _PK_CACHE (2 prices-API calls + one
    multicall). Runs in a background thread after first load so /pegkeeper
    always answers from cache instantly; a failed refresh keeps the old
    entry."""
    import urllib.request as _rq

    def _get(u):
        req = _rq.Request(u, headers={"User-Agent": "bad-debt-sim"})
        with _rq.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    try:
        P = "https://prices.curve.finance/v1"
        CRVUSD = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
        j = _get(f"{P}/crvusd/pegkeepers/ethereum")
        px = _get(f"{P}/usd_price/ethereum/{CRVUSD}")["data"]
        keepers = j.get("keepers", [])
        rpc_error = None
        try:
            from fetch_markets import Rpc, _num
            res = Rpc("ethereum").mq(
                [(CRVUSD, "balanceOf(address)",
                  int(k["address"], 16)) for k in keepers])
            for k, r_ in zip(keepers, res):
                bal = _num(r_)
                k["unused_mint"] = (round(bal / 1e18)
                                    if bal is not None else None)
                k["ceiling"] = (round(bal / 1e18 + (k["total_debt"] or 0))
                                if bal is not None else None)
        except Exception as e:
            for k in keepers:
                k["unused_mint"] = k["ceiling"] = None
            rpc_error = str(e)[:200]
        _PK_CACHE["data"] = {
            "keepers": keepers,
            "crvusd_usd": px.get("usd_price"),
            "price_updated": px.get("last_updated"),
            "rpc_error": rpc_error,
            "fetched_at": int(time.time()),
        }
        _PK_CACHE["at"] = time.time()
    finally:
        _PK_CACHE["busy"] = False


def _pk_warm() -> None:
    try:
        _PK_CACHE["busy"] = True
        _pk_refresh()
    except Exception:
        pass
_YB_CACHE: dict = {}                          # /yb, keyed (days, points)


def _yb_series(days: int, points: int) -> dict:
    """Yield Basis cryptopool imbalance history: crvUSD share of pool value,
    sampled at `points` blocks over the last `days`. Mirrors yield-basis/
    yb-research-scripts plot_yb_pools_imbalance.py: markets 3..5 on the YB
    factory, imbalance = b0 / (b0 + b1*10^(18-dec)*price_oracle/1e18)."""
    from fetch_markets import Rpc, _num, _addr, sel, MULTICALL3
    FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
    rpc = Rpc("ethereum")
    head = int(rpc.raw("eth_blockNumber", []), 16)
    # Every factory market, deduped by pool: markets 0-2 and 3-5 reuse the
    # same three 2025 BTC pools, 6 is the 2025 WETH pool, 7-10 are the
    # newer (May 2026) pools that now hold most of the TVL. The research
    # script's hard-coded 3..5 only saw the old set.
    n_mk = rpc.num(FACTORY, "market_count()") or 0
    pools, seen = [], set()
    # LTs (leverage tokens) are per MARKET, not per pool: markets 1 and 4
    # share the cbBTC pool but have different LTs. Profits (pricePerShare,
    # as in yb-research-scripts/plot_yb_pools_pps.py) are tracked per LT.
    lts = []
    sym_cache: dict = {}
    for i in range(n_mk):
        r = rpc.q(FACTORY, "markets(uint256)", i)
        words = ["0x" + r[2:][k * 64:(k + 1) * 64][24:] for k in range(4)]
        asset, pool, lt = words[0], words[1], words[3]
        if int(pool, 16) == 0:
            continue
        if asset not in sym_cache:
            sym_cache[asset] = (rpc.string(asset, "symbol()") or asset[:8],
                                rpc.num(asset, "decimals()") or 18)
        sym, dec = sym_cache[asset]
        if int(lt, 16):
            try:
                staker = "0x" + rpc.q(lt, "staker()")[-40:]
            except Exception:
                staker = None
            lts.append({"symbol": sym, "lt": lt, "staker": staker,
                        "market_index": i})
        if pool in seen:
            continue
        seen.add(pool)
        pools.append({"symbol": sym, "asset": asset, "pool": pool,
                      "decimals": dec, "market_index": i})
    # label: newest pool per asset keeps the bare symbol, earlier ones get
    # "(old pool)" — the chart draws those dashed
    latest = {}
    for p in pools:
        latest[p["symbol"]] = p["market_index"]
    for p in pools:
        p["old"] = p["market_index"] != latest[p["symbol"]]
        p["label"] = p["symbol"] + (" (old pool)" if p["old"] else "")
    # LT rank per asset: 0 = newest market (solid line), 1, 2 = older
    # (dashed / dotted); label carries the market index
    for sym_ in {l["symbol"] for l in lts}:
        grp = sorted([l for l in lts if l["symbol"] == sym_],
                     key=lambda l: -l["market_index"])
        for rank, l in enumerate(grp):
            l["rank"] = rank
            l["old"] = rank > 0
            l["label"] = f"{sym_} LT #{l['market_index']}"
    blocks = [head - int((days * 7200) * (1 - k / (points - 1)))
              for k in range(points)]
    # crvUSD/USD aggregator (AggregateStablePrice) so the USD view of the
    # balances chart is real USD, not crvUSD-equivalent
    CRVUSD_AGG = "0x18672b1b0c623a30089A280Ed9256379fb0E4E62"
    calls = [(MULTICALL3, "getCurrentBlockTimestamp()", None),
             (CRVUSD_AGG, "price()", None)]
    # per pool: 2 balances, oracle, and the donation reserve (LP shares
    # parked in the pool, released to LPs over donation_duration = 7 d)
    NPER = 7
    for p in pools:
        calls += [(p["pool"], "balances(uint256)", 0),
                  (p["pool"], "balances(uint256)", 1),
                  (p["pool"], "price_oracle()", None),
                  (p["pool"], "donation_shares()", None),
                  (p["pool"], "totalSupply()", None),
                  # LP profit of the pool itself: virtual_price (1.0 at
                  # launch) and xcp_profit (the pool's accumulated profit
                  # counter, of which LPs get half)
                  (p["pool"], "virtual_price()", None),
                  (p["pool"], "xcp_profit()", None)]
    LT0 = len(calls)
    for l in lts:
        calls += [(l["lt"], "pricePerShare()", None),
                  (l["staker"] or l["lt"], "previewRedeem(uint256)", 10 ** 18)]

    # one multicall per sampled block; 8 in flight (own Rpc per thread —
    # Rpc reorders its endpoint list on failure) cuts a cold 120-point
    # window from ~2 min to ~15 s on the public RPCs
    def sample(b):
        try:
            return b, Rpc("ethereum").mq(calls, block=hex(b))
        except Exception:
            return b, None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(ex.map(sample, blocks))
    # a throttled burst leaves holes; one sequential retry pass fills most
    for b in [b for b in blocks if not results.get(b)]:
        results[b] = sample(b)[1]
    n_ok = sum(1 for b in blocks if results.get(b))
    if n_ok < points // 2:
        # don't let a bad RPC spell overwrite a good cached window
        raise RuntimeError(f"RPC sampled only {n_ok}/{points} blocks")

    series = {p["label"]: [] for p in pools}
    asset_bal = {p["label"]: [] for p in pools}   # asset leg, whole units
    asset_usd = {p["label"]: [] for p in pools}   # asset leg, USD
    tvl_usd = {p["label"]: [] for p in pools}     # whole pool, USD
    don_usd = {p["label"]: [] for p in pools}     # donation reserve, USD
    don_pct = {p["label"]: [] for p in pools}     # ... as % of the pool
    vp = {p["label"]: [] for p in pools}          # pool virtual_price
    xcp = {p["label"]: [] for p in pools}         # pool xcp_profit
    pps = {l["label"]: [] for l in lts}           # LT pricePerShare
    staked = {l["label"]: [] for l in lts}        # staker value of 1 LT
    tvl = {p["label"]: None for p in pools}   # crvUSD-eq at the last sample
    times = []
    for b in blocks:
        res = results.get(b)
        if not res:
            continue
        ts = _num(res[0])
        if ts is None:
            continue
        times.append(ts)
        agg = _num(res[1])
        usd_per_crvusd = agg / 1e18 if agg else 1.0
        for j, p in enumerate(pools):
            b0, b1, po, ds, sup, vpr, xcpp = (_num(res[2 + NPER * j + k])
                                              for k in range(NPER))
            if None in (b0, b1, po):
                for d_ in (series, asset_bal, asset_usd, tvl_usd,
                           don_usd, don_pct, vp, xcp):
                    d_[p["label"]].append(None)   # pool not deployed yet
                continue
            vp[p["label"]].append(round(vpr / 1e18, 6) if vpr else None)
            xcp[p["label"]].append(round(xcpp / 1e18, 6) if xcpp else None)
            val = b0 + b1 * 10 ** (18 - p["decimals"]) * po // 10 ** 18
            series[p["label"]].append(
                round(b0 / val * 100, 3) if val > 0 else None)
            asset_bal[p["label"]].append(round(b1 / 10 ** p["decimals"], 4))
            asset_usd[p["label"]].append(
                round(b1 / 10 ** p["decimals"] * po / 1e18 * usd_per_crvusd))
            pool_usd = val / 1e18 * usd_per_crvusd
            tvl_usd[p["label"]].append(round(pool_usd))
            if ds is not None and sup:
                don_usd[p["label"]].append(round(ds / sup * pool_usd))
                don_pct[p["label"]].append(round(ds / sup * 100, 4))
            else:
                don_usd[p["label"]].append(None)
                don_pct[p["label"]].append(None)
            if val > 0:
                tvl[p["label"]] = round(val / 1e18)
        for j, l in enumerate(lts):
            v_pps = _num(res[LT0 + 2 * j])
            v_red = _num(res[LT0 + 2 * j + 1])
            pps[l["label"]].append(round(v_pps / 1e18, 6) if v_pps else None)
            staked[l["label"]].append(
                round(v_red / 1e18 * v_pps / 1e18, 6)
                if (v_pps and v_red and l["staker"]) else None)
    return {"days": days, "points": len(times), "t": times,
            "series": series,
            "asset_bal": asset_bal, "asset_usd": asset_usd,
            "tvl_usd": tvl_usd,
            "donation_usd": don_usd, "donation_pct": don_pct,
            "vp": vp, "xcp_profit": xcp,
            "pps": pps, "staked": staked,
            "lts": [{"symbol": l["symbol"], "label": l["label"],
                     "lt": l["lt"], "staker": l["staker"],
                     "market_index": l["market_index"], "rank": l["rank"],
                     "old": l["old"]} for l in lts],
            "pools": [{"symbol": p["symbol"], "label": p["label"],
                       "pool": p["pool"], "market_index": p["market_index"],
                       "old": p["old"], "tvl_crvusd": tvl[p["label"]]}
                      for p in pools],
            "head_block": head, "generated_at": int(time.time())}

YB_TTL_S = 900                 # yb_ window freshness
_YB_LOCKS: dict = {}           # per-window single-flight refresh
_YB_LOCKS_GUARD = threading.Lock()


def _yb_refresh(key: tuple) -> None:
    """Re-sample one yb_ window into _YB_CACHE. Single-flight: a second
    caller for the same window returns as soon as the first finishes (and
    finds the fresh entry). A failed sample keeps the old entry."""
    with _YB_LOCKS_GUARD:
        lock = _YB_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        lock.acquire()          # wait for the in-flight refresh, then reuse
        lock.release()
        if key in _YB_CACHE:
            return
        raise RuntimeError("yb_ refresh failed")
    try:
        c = _YB_CACHE.get(key)
        if c and time.time() - c["at"] < YB_TTL_S:
            return
        _YB_CACHE[key] = {"at": time.time(), "data": _yb_series(*key)}
    finally:
        lock.release()


def _yb_keepwarm() -> None:
    """Keep every window the user has opened fresh so the tab never has to
    wait on a cold RPC sampling pass: re-sample each cached window just
    before its TTL runs out. Network-bound, ~20 s per window."""
    while True:
        time.sleep(60)
        for key, c in list(_YB_CACHE.items()):
            if time.time() - c["at"] > YB_TTL_S - 120:
                try:
                    _yb_refresh(key)
                except Exception as e:
                    print(f"[ui] yb_ keep-warm {key} failed: {str(e)[:120]}")


PY = sys.executable
_run_lock = threading.Lock()   # serialize sims so they don't fight for CPU / cache


# ---- pool history: on-demand cached proxy of the Curve prices API --------
# The pool pages need ~2 years of daily snapshots/volume/TVL. Fetching that
# from the browser was slow on every visit AND gappy: the snapshots endpoint
# silently returns far fewer rows than the requested range (3pool: 51 days
# for a 300-day ask), so it must be walked backwards by min-timestamp.
# We fetch once here, join on a UNION day axis, forward-fill the parameter
# columns, and cache to data/pool_hist/ for POOL_HIST_TTL.
POOL_HIST_DIR = HERE / "data" / "pool_hist"
POOL_HIST_TTL = 6 * 3600
POOL_HIST_DAYS = 730
_PH_DAY = 86400


def _ph_get(path: str):
    import urllib.request as _u
    req = _u.Request("https://prices.curve.finance/v1" + path,
                     headers={"User-Agent": "curve-sim"})
    with _u.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def _ph_walk(mk, start: int, end: int, max_calls: int = 16) -> list:
    """Cursor pagination that survives BOTH truncation styles: /snapshots
    returns the NEWEST rows of the range (walk the end down), /volume/usd
    returns the OLDEST (walk the start up). Each call shrinks the still-
    uncovered interval from whichever side the chunk touched."""
    rows, lo, hi = [], start, end
    for _ in range(max_calls):
        if lo >= hi:
            break
        try:
            chunk = mk(lo, hi) or []
        except Exception:
            break
        if not chunk:
            break
        rows += chunk
        ts = [int(c.get("timestamp") or 0) for c in chunk]
        mn, mx = min(ts), max(ts)
        touches_lo = mn <= lo + _PH_DAY
        touches_hi = mx >= hi - 2 * _PH_DAY
        if touches_lo and touches_hi:
            break                        # range covered
        if touches_lo:
            lo = mx + 1                  # oldest-first endpoint
        elif touches_hi:
            hi = mn - 1                  # newest-first endpoint
        else:
            break                        # neither end reached — bail out
    return rows


_PH_FIELDS = ["bapr", "vol", "fees", "tvl", "a", "gamma", "fee", "admin",
              "offpeg", "mid", "out", "fg",
              # cryptoswap state (research tab: repeg-lag / real-APR work);
              # pscale/poracle are per-coin ARRAYS, 1e18-scaled
              "pscale", "poracle", "vp", "xcp", "maht"]
_PH_PARAMS = [("a", "a"), ("gamma", "gamma"), ("fee", "fee"),
              ("admin", "admin_fee"), ("offpeg", "offpeg_fee_multiplier"),
              ("mid", "mid_fee"), ("out", "out_fee"), ("fg", "fee_gamma"),
              ("pscale", "price_scale"), ("poracle", "price_oracle"),
              ("vp", "virtual_price"), ("xcp", "xcp_profit"),
              ("maht", "ma_half_time")]


def _ph_build(chain: str, addr: str, cached: dict | None = None) -> dict:
    """Build (or incrementally extend) one pool's daily history. Completed
    days never change, so with a cache we only fetch from the last cached
    day (re-fetching that one — it may have been mid-day) to now: 3 small
    calls instead of a full 2-year crawl."""
    now = int(time.time())
    full_start = now - POOL_HIST_DAYS * _PH_DAY
    prev_t = (cached or {}).get("t") or []
    start = max(full_start, prev_t[-1] - _PH_DAY) if prev_t else full_start
    snaps = _ph_walk(lambda a, b: _ph_get(
        f"/snapshots/{chain}/{addr}?interval=day&start={a}&end={b}")
        .get("data"), start, now)
    vols = _ph_walk(lambda a, b: _ph_get(
        f"/volume/usd/{chain}/{addr}?interval=day&start={a}&end={b}")
        .get("data"), start, now)
    # the tvl endpoint additionally caps the RANGE at 6 months
    tvls = _ph_walk(lambda a, b: _ph_get(
        f"/snapshots/{chain}/{addr}/tvl?interval=day"
        f"&start={max(a, b - 175 * _PH_DAY)}&end={b}")
        .get("data"), start, now)
    day = lambda ts: int(ts) // _PH_DAY * _PH_DAY
    sn, vo, tv = {}, {}, {}
    for r in snaps:
        sn[day(r["timestamp"])] = r
    for r in vols:
        vo[day(r["timestamp"])] = r
    for r in tvls:
        tv[day(r["timestamp"])] = r
    # seed from the cache, then overlay the freshly fetched days
    rows: dict[int, dict] = {}
    lastp = {k: None for k, _ in _PH_PARAMS}
    for i, d in enumerate(prev_t):
        rows[d] = {k: cached[k][i] for k in _PH_FIELDS}
    if prev_t:                       # params continue from the cache edge
        for k, _ in _PH_PARAMS:
            lastp[k] = rows[prev_t[-1]][k]
    for d in sorted(set(sn) | set(vo) | set(tv)):
        row = rows.setdefault(d, {k: None for k in _PH_FIELDS})
        s_ = sn.get(d)
        if s_:
            for k, f in _PH_PARAMS:
                if s_.get(f) is not None:
                    lastp[k] = s_[f]
            row["bapr"] = s_.get("base_daily_apr")
        for k, _ in _PH_PARAMS:
            row[k] = lastp[k]
        v_ = vo.get(d)
        if v_:
            row["vol"], row["fees"] = v_.get("volume"), v_.get("fees")
        t_ = tv.get(d)
        if t_:
            row["tvl"] = t_.get("tvl_usd")
    days = sorted(d for d in rows if d >= full_start)
    out = {"fetched_at": now, "chain": chain, "address": addr, "t": days}
    for k in _PH_FIELDS:
        out[k] = [rows[d][k] for d in days]
    return out


def pool_hist(chain: str, addr: str) -> dict:
    POOL_HIST_DIR.mkdir(parents=True, exist_ok=True)
    f = POOL_HIST_DIR / f"{chain}_{addr}.json"
    cached = None
    if f.is_file():
        try:
            cached = json.loads(f.read_text())
        except (OSError, ValueError):
            cached = None
    if cached and "pscale" not in cached:
        cached = None          # pre-cryptoswap-state cache: full rebuild
    if cached and time.time() - cached.get("fetched_at", 0) < POOL_HIST_TTL:
        return cached
    try:
        fresh = _ph_build(chain, addr, cached)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fresh))
        os.replace(tmp, f)
        return fresh
    except Exception as e:
        if cached:            # stale beats broken
            return cached
        return {"error": f"upstream fetch failed: {str(e)[:120]}"}


PX_HIST_DIR = HERE / "data" / "px_hist"
PX_HIST_TTL = 6 * 3600
# a research page requests several coins at once; parallel cold builds
# tripped the prices-API rate limit, so upstream crawls are serialised
_PX_LOCK = threading.Lock()


def _px_build(chain: str, addr: str) -> dict:
    """Daily USD close history for one token from the prices API
    (/usd_price/.../history, 300-row cap, oldest-first — same walker as
    the pool history). Timestamps come back as ISO strings here."""
    now = int(time.time())
    start = now - POOL_HIST_DAYS * _PH_DAY

    def rows(a, b):
        time.sleep(0.4)                  # politeness between walk calls
        out = None
        for attempt, wait in ((0, 2), (1, 5), (2, 0)):
            try:
                out = _ph_get(f"/usd_price/{chain}/{addr}/history"
                              f"?interval=day&start={a}&end={b}"
                              ).get("data") or []
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(wait)         # back off over a rate-limit blip
        for r in out:
            ts = r.get("timestamp")
            if isinstance(ts, str):        # "2026-08-18T00:00:00"
                r["timestamp"] = int(datetime.fromisoformat(ts)
                                     .replace(tzinfo=timezone.utc)
                                     .timestamp())
        return out

    # forward-only walk: this endpoint is oldest-first, and a token whose
    # feed starts mid-window would make _ph_walk's chunk touch neither
    # boundary and bail after one call (that bit cbBTC) — so just keep
    # advancing the start past the newest row until now is reached
    px = {}
    lo = start
    for _ in range(16):
        chunk = rows(lo, now)
        if not chunk:
            break
        mx = 0
        for r in chunk:
            ts = int(r["timestamp"])
            mx = max(mx, ts)
            px[ts // _PH_DAY * _PH_DAY] = r.get("price")
        if mx >= now - 2 * _PH_DAY or mx + 1 <= lo:
            break
        lo = mx + 1
    days = sorted(d for d in px if px[d] is not None)
    # a walk that died mid-crawl leaves a series that stops in the past —
    # caching it would freeze a useless window for 6h. treat as failure.
    if days and days[-1] < now - 5 * _PH_DAY:
        raise RuntimeError("incomplete walk (rate-limited?)")
    return {"fetched_at": now, "chain": chain, "address": addr,
            "t": days, "px": [px[d] for d in days]}


def px_hist(chain: str, addr: str) -> dict:
    PX_HIST_DIR.mkdir(parents=True, exist_ok=True)
    f = PX_HIST_DIR / f"{chain}_{addr}.json"
    cached = None
    if f.is_file():
        try:
            cached = json.loads(f.read_text())
        except (OSError, ValueError):
            cached = None
    if cached and cached.get("t") \
            and time.time() - cached.get("fetched_at", 0) < PX_HIST_TTL:
        return cached
    try:
        with _PX_LOCK:
            fresh = _px_build(chain, addr)
        if fresh.get("t"):     # never cache an empty series — retry next hit
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(fresh))
            os.replace(tmp, f)
        return fresh if fresh.get("t") or not cached else cached
    except Exception as e:
        if cached:            # stale beats broken
            return cached
        return {"error": f"upstream fetch failed: {str(e)[:120]}"}


def _http_json(handler: BaseHTTPRequestHandler, status: int, body: dict):
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(payload)


# ---- hourly markets.json refresh -------------------------------------------
# fetch_markets.py is cheap since the Multicall3 rewrite (~19 s, 18 RPC
# requests), so the server re-runs it once per hour, anchored to a fixed
# minute-of-hour ("refreshed hourly at :17"). GET /refresh_status feeds the
# UI's freshness line; fetch_markets writes atomically (tmp + os.replace), so
# /markets never serves a half-written snapshot.
MARKETS_FILE = HERE / "data" / "markets.json"
FETCH_SCRIPT = HERE / "fetchers" / "fetch_markets.py"
REFRESH_EVERY_S = 1800

# Daily collateral candles (fetch_candles.py, Curve prices API). Candles are
# daily, so they refresh once a day, piggybacked on the same scheduler thread
# right after a market refresh (new venues get their series the same cycle).
CANDLES_FILE = HERE / "data" / "candles.json"
CANDLES_SCRIPT = HERE / "fetchers" / "fetch_candles.py"
CANDLES_EVERY_S = 20 * 3600   # "once a day", tolerant of restart jitter

_refresh = {"anchor_min": None, "next_at": None, "running": False,
            "started_at": None, "last_err": None, "last_duration_s": None,
            "candles_at": None, "candles_running": False,
            "candles_last_err": None, "candles_last_duration_s": None}


def _markets_fetched_at():
    try:
        return json.loads(MARKETS_FILE.read_text()).get("fetched_at")
    except Exception:
        return None


_KL_EXCLUDE = {"USDC", "USDE", "PAXG"}   # peg/base feeds, not collaterals


def _kl_sources() -> dict:
    """Collateral series built by fetchers/fetch_binance_klines or
    build_nav_klines (meta carries a `source` field): key 'kl-<name>' ->
    {path, days}. Newest/longest span per name wins."""
    best: dict = {}
    for mf in (HERE / "data").glob("_ref_table_klines_*.meta.json"):
        try:
            m = json.loads(mf.read_text())
        except ValueError:
            continue
        src = str(m.get("source", ""))
        if not (src.startswith("binance:") or src.startswith("nav:")):
            continue
        name = m.get("symbol")
        if not name or name in _KL_EXCLUDE:
            continue
        days = (m["to"] - m["from"]) / 86400
        if name not in best or days > best[name][1]:
            best[name] = (Path(str(mf).replace(".meta.json", ".json")), days)
    return {f"kl-{n}": {"path": p, "days": round(d)}
            for n, (p, d) in best.items()}


def _kl_daily() -> None:
    """Daily top-up of the S.L./D.L. price histories (fetchers/
    update_klines.py: Binance API append + incremental NAV rebuild) at
    04:10 local time. Takes _run_lock so a running sweep never sees a
    series swapped under it."""
    while True:
        now = time.time()
        lt = time.localtime(now)
        nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                           4, 10, 0, 0, 0, -1))
        if nxt <= now:
            nxt += 86_400
        time.sleep(nxt - now)
        try:
            with _run_lock:
                p = subprocess.run(
                    [PY, str(HERE / "fetchers" / "update_klines.py")],
                    capture_output=True, text=True, timeout=3600)
            tail = ((p.stdout or "") + (p.stderr or ""))[-1500:]
            print(f"[ui] klines daily update rc={p.returncode}\n{tail}",
                  flush=True)
        except Exception as e:
            print(f"[ui] klines daily update failed: {e}", flush=True)


def _next_hour_at(minute: int, after: float) -> float:
    # anchored to minute-of-hour, stepping by the refresh interval (30 min
    # -> fires at :MM and :MM+30 of every hour)
    lt = time.localtime(after)
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour,
                     minute % 30, 0, 0, 0, -1))
    while t <= after:
        t += REFRESH_EVERY_S
    return t


def _do_refresh():
    _refresh.update(running=True, started_at=time.time())
    t0 = time.time()
    try:
        p = subprocess.run([PY, str(FETCH_SCRIPT)],
                           capture_output=True, text=True, timeout=900)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        _refresh["last_err"] = None
        print(f"[ui] markets refreshed in {time.time() - t0:.1f} s")
    except Exception as e:
        _refresh["last_err"] = str(e)[:300]
        print(f"[ui] markets refresh FAILED: {_refresh['last_err']}")
    finally:
        _refresh.update(running=False,
                        last_duration_s=round(time.time() - t0, 1))
    # Spring-Cleaning rides the same cycle. Incremental after its first
    # backfill (data/cleanup.json carries scanned_to per market), so this is
    # seconds, not minutes — but a failure must never take markets down with
    # it, hence its own try block.
    t1 = time.time()
    try:
        p = subprocess.run([PY, str(HERE / "fetchers" / "fetch_cleanup.py")],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        print(f"[ui] cleanup refreshed in {time.time() - t1:.1f} s")
    except Exception as e:
        print(f"[ui] cleanup refresh FAILED: {str(e)[:300]}")
    # LP end-user distribution of the flagship pools (event-driven:
    # only event-touched addresses get balance re-reads).
    t3 = time.time()
    try:
        p = subprocess.run([PY, str(HERE / "fetchers" / "fetch_lp.py")],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        print(f"[ui] lp holders refreshed in {time.time() - t3:.1f} s")
    except Exception as e:
        print(f"[ui] lp holders refresh FAILED: {str(e)[:300]}")
    # Affected lenders of the bad-debt markets (vault-share Transfer scan,
    # incremental after the first backfill via data/lenders_scan.json).
    t2 = time.time()
    try:
        p = subprocess.run([PY, str(HERE / "fetchers" / "fetch_lenders.py")],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        print(f"[ui] lenders refreshed in {time.time() - t2:.1f} s")
    except Exception as e:
        print(f"[ui] lenders refresh FAILED: {str(e)[:300]}")
    # Re-render the Spring-Cleaning charts from the fresh markets.json —
    # the same matplotlib PNGs that live in images/.
    for script in ("plot_ltv_vs_tvl.py", "plot_discounts_vs_tvl.py"):
        try:
            p = subprocess.run([PY, str(HERE / "plots" / script)],
                               capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                raise RuntimeError((p.stderr or "").strip()[-200:])
        except Exception as e:
            print(f"[ui] {script} FAILED: {str(e)[:200]}")
    # LLM tab dataset (Curve prices API only — snapshots, borrowers; no RPC).
    t4 = time.time()
    try:
        p = subprocess.run([PY, str(HERE / "fetchers" / "fetch_llm.py")],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        print(f"[ui] llm refreshed in {time.time() - t4:.1f} s")
    except Exception as e:
        print(f"[ui] llm refresh FAILED: {str(e)[:300]}")
    # DAO revenue by source (pool admin fees + crvUSD mint interest +
    # LlamaLend V2 admin cut) — prices API + the local llm/markets files.
    t5 = time.time()
    try:
        p = subprocess.run([PY, str(HERE / "fetchers" / "fetch_dao_revenue.py")],
                           capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        print(f"[ui] dao revenue refreshed in {time.time() - t5:.1f} s")
    except Exception as e:
        print(f"[ui] dao revenue refresh FAILED: {str(e)[:300]}")
    # pool-history warmer: keep every census pool's 2y daily history fresh
    # on disk so /poolhist never fetches upstream during a page visit.
    # Incremental after the first sweep (last-day top-up, ~3 calls/pool).
    t6 = time.time()
    try:
        cen = json.loads((HERE / "data" / "census.json").read_text())["pools"]
        targets = [(ch, r[0].lower()) for ch, rows in cen.items()
                   for r in rows if isinstance(r, list) and r]
        cutoff = time.time() - POOL_HIST_TTL
        stale = []
        for ch, a in targets:
            f = POOL_HIST_DIR / f"{ch}_{a}.json"
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                pass
            stale.append((ch, a))
        if stale:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(6) as ex:
                list(ex.map(lambda t7: pool_hist(*t7), stale))
        print(f"[ui] pool hist: warmed {len(stale)} of {len(targets)} "
              f"pools in {time.time() - t6:.0f} s")
    except Exception as e:
        print(f"[ui] pool hist warm FAILED: {str(e)[:300]}")
    # Oracle-graph live values (price/rate/EMA numbers on the LLM flow map)
    # — light multicall pass over the mapped nodes, seconds.
    try:
        p = subprocess.run([PY, str(HERE / "fetchers" / "fetch_oracles.py"),
                            "--values"],
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-200:])
        print("[ui] oracle values refreshed")
    except Exception as e:
        print(f"[ui] oracle values FAILED: {str(e)[:200]}")


def _candles_fetched_at():
    try:
        return json.loads(CANDLES_FILE.read_text()).get("fetched_at")
    except Exception:
        return None


def _do_candles():
    _refresh["candles_running"] = True
    t0 = time.time()
    try:
        p = subprocess.run([PY, str(CANDLES_SCRIPT)],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:]
                               or f"exit code {p.returncode}")
        _refresh["candles_last_err"] = None
        _refresh["candles_at"] = _candles_fetched_at() or time.time()
        print(f"[ui] candles refreshed in {time.time() - t0:.1f} s")
        # crash windows for EVERY market follow the daily candles: new
        # markets get theirs, and a series whose worst day moved gets
        # refetched (build_window checks the cached window against the
        # current worst crash day). Network-bound, sequential, ~45 s/series
        # when something actually needs fetching, instant otherwise.
        t1 = time.time()
        q = subprocess.run([PY, str(HERE / "fetchers" / "fetch_crash_window.py"), "--all"],
                           capture_output=True, text=True, timeout=7200)
        tail = (q.stdout or "").strip().splitlines()
        print(f"[ui] crash windows: {tail[-1] if tail else 'no output'} "
              f"({time.time() - t1:.0f} s)")
    except Exception as e:
        _refresh["candles_last_err"] = str(e)[:300]
        # failed attempt still counts for scheduling: retry next cycle, not
        # every 15 s against a down API
        _refresh["candles_at"] = time.time()
        print(f"[ui] candles refresh FAILED: {_refresh['candles_last_err']}")
    finally:
        _refresh["candles_running"] = False
        _refresh["candles_last_duration_s"] = round(time.time() - t0, 1)


def _refresher():
    fetched = _markets_fetched_at()
    now = time.time()
    if fetched is None or now - fetched >= REFRESH_EVERY_S:
        _refresh["anchor_min"] = time.localtime(now).tm_min
        _refresh["next_at"] = now          # stale on boot -> refresh right away
    else:
        _refresh["anchor_min"] = time.localtime(fetched).tm_min
        _refresh["next_at"] = _next_hour_at(_refresh["anchor_min"], now)
    _refresh["candles_at"] = _candles_fetched_at() or 0
    print(f"[ui] markets refresh every 30m (anchor :{_refresh['anchor_min'] % 30:02d})"
          f" (next {time.strftime('%H:%M', time.localtime(_refresh['next_at']))})")
    while True:
        time.sleep(min(15.0, max(0.5, _refresh["next_at"] - time.time())))
        if time.time() >= _refresh["next_at"]:
            _do_refresh()
            _refresh["next_at"] = _next_hour_at(_refresh["anchor_min"],
                                                time.time())
        if time.time() - _refresh["candles_at"] >= CANDLES_EVERY_S:
            _do_candles()


def run_pipeline(params: dict) -> dict:
    """Run one full sim with the given params. Returns per-block rows."""
    tag = uuid.uuid4().hex[:8]
    oracle  = SCRATCH / f"oracle_{tag}.json"
    target  = SCRATCH / f"target_{tag}.json"
    pre     = SCRATCH / f"pre_{tag}.json"
    acct    = SCRATCH / f"acct_{tag}.json"
    art     = SCRATCH / f"art_{tag}.json"
    arb_log = SCRATCH / f"arb_{tag}.json"

    disc = float(params["liquidation_discount_pct"])
    # UI toggle "liq. discount for health-math on crvUSD from S.L.". True =
    # on-chain rule (Controller.vy v1 L1099-1100 scales ALL of get_x_down by
    # 1-discount). False = band-crvUSD counts at par in the liquidation
    # trigger (engine --xpar-health); settlement burns x 1:1 either way.
    # C++ routed engine only.
    discount_x_sl = bool(params.get("discount_x_sl", True))
    ma   = int(params["ma_time_s"])
    cs_start = float(params["crash_start_spot"])
    cs_end   = float(params["crash_end_spot"])
    cs_off   = float(params["crash_start_offset_s"])
    cs_dur   = float(params["crash_duration_s"])
    A_raw    = float(params["A_raw"])
    tvl_usd  = float(params["tvl_usd"])
    soft_liq = bool(params.get("soft_liq", True))
    # Oracle EMA seed == crash-start spot: the UI no longer exposes a
    # separate seed (two prices with one hardcoded to CRV produced three
    # silent mis-scalings). CLI callers can still pass oracle_seed.
    oracle_seed = float(params.get("oracle_seed", cs_start))
    collateral_usd = float(params.get("collateral_usd", 8_000_000))
    debt_usd       = float(params.get("debt_usd", 5_600_000))
    n_bands        = int(params.get("n_bands", single_user.DEFAULT_N))
    loan_disc      = float(params.get("loan_discount_pct", 11.0))
    llamma_A       = int(params.get("llamma_A", single_user.ONCHAIN_A))
    pool_type      = str(params.get("pool_type", "cryptoswap"))
    n_coins        = int(params.get("n_coins", 2))
    ss_A           = float(params.get("ss_A", 500))
    horizon_min    = params.get("horizon_min", None)
    # Per-market LLAMMA parameters. Absent => the reference snapshot's (CRV's)
    # values stand, which is what every market used to get.
    amm_fee_wei    = params.get("amm_fee_wei", None)
    amm_rate_wei   = params.get("amm_rate_wei", None)
    # Live pool state for the liquidation venue (markets.json venue["state"]).
    venue_state    = params.get("venue_state", None)
    # Replay of a real price history instead of the linear start->end ramp:
    # [[t_seconds_from_sim_start, spot], ...]. Routed engine only.
    price_path     = params.get("price_path", None)
    if pool_type not in ("cryptoswap", "stableswap", "stableswap-ng"):
        return {"error": f"unknown pool_type {pool_type!r}"}
    if price_path is not None:
        try:
            price_path = [[float(t), float(p)] for t, p in price_path]
            if len(price_path) < 2:
                raise ValueError("need at least two points")
        except Exception as e:
            return {"error": f"bad price_path: {e}"}

    # Bad debt = UNBACKED debt. Its measurement basis must NOT move with the
    # parameter being swept: flagging the basis at the swept discount makes a
    # 20% run measure a different quantity than an 8% run (75 flagged users vs
    # 46 on the real book) and inflates high discounts. Sweeping the accounting
    # pass at 100% flags every borrower with debt, so the basis is the whole
    # book at every discount and the discount enters only via settlements.
    market_disc = 100

    # Venue-routed engine (routed_sim.py) is the default: soft-liq arbs route
    # through the venue pool and an external arb bot with per-block capacity
    # maintains the venue against the schedule. legacy=true runs the old
    # C++-sweep pipeline (infinite-depth arb) for comparison.
    legacy = bool(params.get("legacy", False))
    if legacy and params.get("price_path"):
        return {"error": "price_path replay needs the routed engine (legacy=false)"}
    ext_cap = params.get("ext_arb_cap_usd", None)   # None/absent = unlimited
    arb_gas = params.get("arb_gas", None)           # None = routed default

    t0 = time.perf_counter()

    # --- build the single abstracted borrower -------------------------------
    snapshot = SCRATCH / f"snap_{tag}.json"
    # "pin ladder top" (Curve-sim placement): grid re-anchored so the ladder
    # top edge sits a hair above the start price — no grid-phase lottery.
    pinned = bool(params.get("pinned_ladder", False))
    try:
        su_info = single_user.build(collateral_usd, debt_usd, cs_start, oracle_seed,
                                    n_bands, snapshot,
                                    workdir=SCRATCH / f"su_{tag}", verbose=False,
                                    pinned=pinned,
                                    loan_discount_pct=loan_disc, llamma_A=llamma_A,
                                    amm_fee_wei=(int(amm_fee_wei)
                                                 if amm_fee_wei not in (None, "") else None),
                                    amm_rate_wei=(int(amm_rate_wei)
                                                  if amm_rate_wei not in (None, "") else None))
    except Exception as e:
        return {"error": str(e)}

    # Resolve the venue ONCE in Python and hand both engines the same integers,
    # so a real-pool seed can never mean two different pools.
    vstate_file = None
    venue_note = None
    if venue_state:
        try:
            v = venues.venue_from_state(venue_state, int(cs_start * 1e18))
        except Exception as e:
            v, venue_note = None, f"venue state unusable: {e}"
        blob = None
        if isinstance(v, venues.StableswapVenue):
            blob = {"venue_kind": "stableswap", "ng": bool(v.ng),
                    "amp": str(v.amp), "fee": str(v.fee), "offpeg": str(v.offpeg),
                    "bal0": str(v.balances[0]), "bal1": str(v.balances[1]),
                    "rate1": str(v.rates[1])}
        elif isinstance(v, venues.Crypto2StateVenue):
            blob = {"venue_kind": "crypto2", "math": v.math,
                    "balances": [str(x) for x in v.balances],
                    "price_scale": str(v.price_scale), "A": str(v.ANN),
                    "gamma": str(v.gamma), "mid_fee": str(v.mid_fee),
                    "out_fee": str(v.out_fee), "fee_gamma": str(v.fee_gamma),
                    "D": str(v.D)}
        elif isinstance(v, venues.Tri3StateVenue):
            blob = {"venue_kind": "tri3",
                    "balances": [str(x) for x in v.balances],
                    "price_scale": [str(x) for x in v.price_scale],
                    "i_q": v.i_q, "i_b": v.i_b, "q_usd": str(v.q_usd),
                    "A": str(v.ANN), "gamma": str(v.gamma),
                    "mid_fee": str(v.mid_fee), "out_fee": str(v.out_fee),
                    "fee_gamma": str(v.fee_gamma), "D": str(v.D)}
        if blob:
            vstate_file = SCRATCH / f"vstate_{tag}.json"
            vstate_file.write_text(json.dumps(blob))
            venue_note = f"real pool state ({blob['venue_kind']})"
        elif venue_note is None:
            venue_note = "balanced approximation (state not mappable)"

    if not legacy:
        # C++ engine (cpp/build/routed_engine): one step per DT_S seconds over
        # the whole horizon — a 7-day window runs 50,400 steps in ~40 s where
        # the Python engine was pinned to 110. Gas is a fixed base fee.
        # engine="python" forces the (bit-identical, 110-step) Python reference.
        DT_S = 12.0
        horizon_s = float(horizon_min) * 60.0 if horizon_min else \
            (cs_off + cs_dur + 600.0)
        use_python = str(params.get("engine", "")) == "python"
        steps = 110 if use_python else max(2, int(horizon_s // DT_S) + 1)
        chart_rows = 1100
        stride = 1 if steps <= chart_rows else (steps + chart_rows - 1) // chart_rows

        if use_python:
            routed_cmd = [PY, str(ROUTED_SCRIPT),
                          "--snapshot", str(snapshot),
                          "--discount", str(disc),
                          "--tvl-usd", str(tvl_usd),
                          "--A-raw", str(A_raw),
                          "--pool-type", pool_type,
                          "--n-coins", str(n_coins),
                          "--ss-A", str(ss_A),
                          "--crash-start-spot", str(cs_start),
                          "--crash-end-spot", str(cs_end),
                          "--crash-start-offset-s", str(cs_off),
                          "--crash-duration-s", str(cs_dur),
                          *(["--horizon-min", str(float(horizon_min))] if horizon_min else []),
                          "--ma-time-s", str(ma),
                          "--oracle-seed", str(oracle_seed),
                          "--gas-usd", str(GAS_USD),
                          "--out", str(art),
                          "--oracle-out", str(oracle),
                          "--arb-log-out", str(arb_log)]
            if ext_cap is not None and str(ext_cap) != "":
                routed_cmd += ["--ext-arb-cap-usd", str(float(ext_cap))]
        else:
            routed_cmd = [str(CPP_ENGINE),
                          "--snapshot", str(snapshot),
                          "--discount", str(disc),
                          "--tvl-usd", str(tvl_usd),
                          "--A-raw", str(A_raw),
                          "--pool-type", pool_type,
                          "--n-coins", str(n_coins),
                          "--ss-A", str(ss_A),
                          "--crash-start-spot", str(cs_start),
                          "--crash-end-spot", str(cs_end),
                          "--crash-start-offset-s", str(cs_off),
                          "--crash-duration-s", str(cs_dur),
                          "--ma-time-s", str(ma),
                          "--oracle-seed", str(oracle_seed),
                          "--gas-usd", str(GAS_USD),
                          "--steps", str(steps),
                          "--dt-s", str(DT_S),
                          "--chart-rows", str(chart_rows),
                          "--out", str(art),
                          "--oracle-out", str(oracle),
                          "--arb-log-out", str(arb_log),
                          "--progress-out", str(PROGRESS_FILE)]
            try:
                PROGRESS_FILE.write_text(json.dumps({"done": 0, "total": steps}))
            except OSError:
                pass
            if amm_rate_wei not in (None, ""):
                routed_cmd += ["--amm-rate-wei", str(int(amm_rate_wei))]
            if not discount_x_sl:
                routed_cmd += ["--xpar-health", "1"]
            if vstate_file:
                routed_cmd += ["--venue-state", str(vstate_file)]
        if arb_gas is not None and str(arb_gas) != "":
            routed_cmd += ["--arb-gas", str(int(arb_gas))]
        if price_path is not None:
            pp = SCRATCH / f"path_{tag}.json"      # via file: paths blow up argv
            pp.write_text(json.dumps(price_path))
            routed_cmd += ["--price-path", str(pp)]
        r = subprocess.run(routed_cmd, capture_output=True, text=True)
        try:
            PROGRESS_FILE.unlink()
        except OSError:
            pass
        if r.returncode != 0:
            return {"error": "routed engine failed",
                    "stdout": r.stdout, "stderr": r.stderr}
        t_synth = time.perf_counter() - t0
        rows = json.loads(art.read_text())
        try:
            oracle_series = {k: int(v) / 1e18
                             for k, v in json.loads(oracle.read_text()).items()}
        except Exception:
            oracle_series = {}
        sl = _aggregate_arb_log(arb_log, stride=stride, last_id=steps - 1)
        ext_total = sum(r0.get("extArbUsd") or 0 for r0 in rows)
        for p in (oracle, art, arb_log, snapshot, vstate_file or oracle):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        return {
            "rows": rows,
            "oracle": oracle_series,
            "borrower": su_info,
            "soft_liq": {"enabled": True, **sl},
            "ext_arb": {"total_usd": round(ext_total)},
            "venue_note": venue_note,
            "timing": {"precompute_s": 0.0, "synth_s": t_synth, "total_s": t_synth},
        }

    build_cmd = [PY, str(BUILD_SCRIPT),
                 "--ma-half-time", str(ma),
                 "--discount", str(disc),
                 "--crash-start-spot", str(cs_start),
                 "--crash-end-spot",   str(cs_end),
                 "--crash-start-offset-s", str(cs_off),
                 "--crash-duration-s",     str(cs_dur),
                 "--oracle-out", str(oracle),
                 "--target-out", str(target),
                 "--precompute-out", str(pre),
                 "--arb-log-out", str(arb_log),
                 "--oracle-seed", str(oracle_seed),
                 "--market-discount", str(market_disc),
                 "--accounting-out", str(acct),
                 "--snapshot", str(snapshot),
                 "--events", str(BLOCKTICKS_FILE)]
    if horizon_min:
        build_cmd += ["--horizon-min", str(float(horizon_min))]
    if not soft_liq:
        build_cmd.append("--no-soft-liq")
    r1 = subprocess.run(build_cmd, capture_output=True, text=True)
    if r1.returncode != 0:
        return {"error": "build_ema_precompute failed",
                "stdout": r1.stdout, "stderr": r1.stderr}
    t_pre = time.perf_counter() - t0

    t1 = time.perf_counter()
    r2 = subprocess.run([PY, str(SYNTH_SCRIPT),
                         "--discount", str(disc),
                         "--tvl-usd", str(tvl_usd),
                         "--A-raw", str(A_raw),
                         "--crash-start-spot", str(cs_start),
                         "--crash-end-spot",   str(cs_end),
                         "--crash-start-offset-s", str(cs_off),
                         "--crash-duration-s",     str(cs_dur),
                         *(["--horizon-min", str(float(horizon_min))] if horizon_min else []),
                         "--pool-type", pool_type,
                         "--n-coins", str(n_coins),
                         "--ss-A", str(ss_A),
                         "--precompute-file", str(pre),
                         "--accounting-file", str(acct),
                         # Whole-book basis => must floor per user, else healthy
                         # borrowers' negative terms cancel real shortfalls.
                         "--accounting-floor",
                         # One large position is never liquidatable in a single
                         # atomic dump; Controller.liquidate_extended lets a
                         # liquidator take profitable slices instead.
                         "--partial-liq",
                         "--out", str(art)],
                        capture_output=True, text=True)
    if r2.returncode != 0:
        return {"error": "synth_bad_debt failed",
                "stdout": r2.stdout, "stderr": r2.stderr}
    t_synth = time.perf_counter() - t1

    rows = json.loads(art.read_text())

    # Per-block oracle (EMA of the crash schedule at the chosen ma_time). This
    # is what LLAMMA prices health against, and it is NOT in the synth rows —
    # it lives in the oracle file the precompute step generated. Read it before
    # the cleanup below deletes it.
    try:
        oracle_series = {k: int(v) / 1e18 for k, v in json.loads(oracle.read_text()).items()}
    except Exception:
        oracle_series = {}

    sl = _aggregate_arb_log(arb_log)

    for p in (oracle, target, pre, acct, art, arb_log, snapshot):
        try: p.unlink()
        except FileNotFoundError: pass
    return {
        "rows": rows,
        "oracle": oracle_series,
        "borrower": su_info,
        "soft_liq": {"enabled": soft_liq, **sl},
        "timing": {"precompute_s": t_pre, "synth_s": t_synth, "total_s": t_pre + t_synth},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "BadDebtUI/0.1"

    def end_headers(self):
        # Cross-origin isolation so the browser-side sim engines (wasm +
        # pthreads, /wasm/*) get SharedArrayBuffer. Site-wide is safe here:
        # the only cross-origin embeds are jsdelivr token icons (they send
        # CORP: cross-origin) and prices-API fetches (CORS, ACAO: *).
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Silence default noisy per-request logging; keep errors visible.
        if "code" in fmt or "error" in fmt.lower():
            super().log_message(fmt, *args)

    # page deep links only — must not collide with API routes (/yb is the
    # data endpoint, the page path is /yb_)
    TAB_PATHS = ("home", "sim", "cleaning",
                 "bad-debt", "sldl", "util", "pegkeeper", "yb", "lp",
                 "pools", "llm", "lending-markets", "dao-revenue",
                 "map",
                 # legacy pre-rename paths still serve the page
                 "bad-debt-sim", "spring-cleaning", "s.l.-d.l.",
                 "high-util", "yb_")

    def do_GET(self):
        # /<tab> deep links (e.g. /yb_, /Pegkeeper) serve the page; the page
        # reads the path and opens that tab. Some labels coincide with API
        # routes (/pegkeeper is also the data endpoint), so only a browser
        # NAVIGATION (Accept: text/html) gets the page — fetch() sends */*
        # and falls through to the API below.
        seg = self.path.split("?")[0].strip("/").lower()
        # tabs may carry state segments (/util/v2/daily-dao-rev); the page
        # reads them. /map/* stays with the bundle handler below.
        head = seg.split("/")[0]
        wants_page = "text/html" in (self.headers.get("Accept") or "")
        if self.path in ("/", "/index.html") or (
                head in self.TAB_PATHS and head != "map" and wants_page):
            body = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/map/"):
            # the curve-map bundle (map tab iframe): index.html + its assets
            name = self.path.split("?")[0][len("/map/"):] or "index.html"
            f = (HERE / "map" / name).resolve()
            if not str(f).startswith(str((HERE / "map").resolve())) or not f.is_file():
                self.send_response(404); self.end_headers(); return
            ctype = {".html": "text/html; charset=utf-8", ".json": "application/json",
                     ".png": "image/png", ".js": "text/javascript",
                     ".css": "text/css"}.get(f.suffix, "application/octet-stream")
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/wasm/"):
            # the browser-side sim runner: engine modules + JS (fixed
            # whitelist; this must never become a generic file server)
            name = self.path.split("?")[0][len("/wasm/"):]
            if name not in ("ref_model_v1.js", "ref_model_v1.wasm",
                            "ref_model_v2.js", "ref_model_v2.wasm",
                            "sldl_client.js", "sldl_shared.js"):
                self.send_response(404); self.end_headers(); return
            f = HERE / "wasm" / name
            if not f.is_file():
                self.send_response(404); self.end_headers(); return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/wasm" if name.endswith(".wasm")
                             else "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            # the page appends ?v=<mtime>, so long immutable caching is safe
            self.send_header("Cache-Control",
                             "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/sldl_data/"):
            # packed series for the browser-side sldl runner. ETag-based:
            # the kl bins are rewritten by the daily top-up, so a repeat
            # visitor pays one conditional request, not a 33 MB download.
            name = self.path.split("?")[0][len("/sldl_data/"):]
            files = {"usd_1m.bin": HERE / "data" / "sldl_usd_1m.bin",
                     "usd_hourly.json":
                         HERE / "data" / "_ref_v2" / "crvusd_usd_hourly.json",
                     "zchf_v2_market.bin":
                         HERE / "data" / "sldl_zchf_market.bin",
                     "zchf.bin": HERE / "zchf" / "his_klines.json.bin"}
            for k, v in _kl_sources().items():
                files[f"{k}.bin"] = Path(str(v["path"]) + ".bin")
            f = files.get(name)
            if f is None or not f.is_file():
                self.send_response(404); self.end_headers(); return
            st = f.stat()
            tag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.end_headers()
                return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/json" if name.endswith(".json")
                             else "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] in ("/landing_art.png", "/favicon.png"):
            f = HERE / "data" / ("landing_torus.png"
                                 if self.path.startswith("/landing_art")
                                 else "favicon.png")
            if not f.exists():
                self.send_response(404); self.end_headers(); return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/onchain":
            rows = json.loads(ONCHAIN_FILE.read_text())
            trimmed = [{"blockNumber": r["blockNumber"],
                        "date": r["date"],
                        "timestamp": r["timestamp"],
                        "onchain_bad_debt": r["onchain_bad_debt"]} for r in rows]
            _http_json(self, 200, {"rows": trimmed})
            return
        if self.path == "/onchain_sl":
            _http_json(self, 200, onchain_sl())
            return
        if self.path == "/markets":
            # Snapshot written by fetch_markets.py; the server refreshes it
            # hourly (see _refresher) and the write is atomic.
            if not MARKETS_FILE.exists():
                _http_json(self, 404, {"error": "run fetchers/fetch_markets.py first"})
                return
            _http_json(self, 200, json.loads(MARKETS_FILE.read_text()))
            return
        if self.path == "/oracles":
            # Oracle-graph snapshot from fetch_oracles.py (not auto-refreshed:
            # oracle wiring changes only when the DAO redeploys something).
            f = HERE / "data" / "oracles.json"
            if not f.exists():
                _http_json(self, 404, {"error": "run fetchers/fetch_oracles.py first"})
                return
            _http_json(self, 200, json.loads(f.read_text()))
            return
        if self.path in ("/cleanup", "/baddebt", "/lenders", "/lp", "/llm",
                         "/dao_revenue", "/impl"):
            # cleanup/baddebt from fetch_cleanup.py, lenders from
            # fetch_lenders.py, llm from fetch_llm.py — all on the cycle.
            f = HERE / "data" / (self.path[1:] + ".json")
            if not f.exists():
                _http_json(self, 404, {"error": "not built yet — the "
                                                "refresh cycle writes it"})
                return
            _http_json(self, 200, json.loads(f.read_text()))
            return
        if self.path.startswith("/poolhist"):
            # 2y daily pool history, cached server-side — ?m=chain:0xpool
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("m") or [""])[0].lower()
            ch, _, pa = key.partition(":")
            if not (ch.isalnum() or ch.replace("-", "").isalnum()) \
                    or not pa.startswith("0x") \
                    or not all(c in "0123456789abcdefx" for c in pa):
                _http_json(self, 400, {"error": "bad ?m="})
                return
            _http_json(self, 200, pool_hist(ch, pa))
            return
        if self.path.startswith("/pxhist"):
            # 2y daily USD closes for one token, cached — ?t=chain:0xtoken
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("t") or [""])[0].lower()
            ch, _, ta = key.partition(":")
            if not (ch.isalnum() or ch.replace("-", "").isalnum()) \
                    or not ta.startswith("0x") \
                    or not all(c in "0123456789abcdefx" for c in ta):
                _http_json(self, 400, {"error": "bad ?t="})
                return
            _http_json(self, 200, px_hist(ch, ta))
            return
        if self.path.startswith("/llmhist"):
            # per-market daily history arrays (fetch_llm.py) — ?m=chain:ctrl
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("m") or [""])[0].lower()
            ch, _, ctrl = key.partition(":")
            if not (ch.isalnum() and ctrl.startswith("0x")
                    and all(c in "0123456789abcdefx" for c in ctrl)):
                _http_json(self, 400, {"error": "bad ?m="})
                return
            f = HERE / "data" / "llm_hist" / f"{ch}_{ctrl}.json"
            if not f.is_file():
                _http_json(self, 404, {"error": "no history for this market"})
                return
            _http_json(self, 200, json.loads(f.read_text()))
            return
        if self.path == "/sldl_sources":
            # data choices for the S.L./D.L. dropdown, with span so the UI
            # can warn when a series is shorter than a year. "meta" + the
            # "client" block feed the browser-side wasm runner.
            out = []
            try:
                z = json.loads(
                    (HERE / "zchf" / "his_klines.meta.json").read_text())
                out.append({"key": "zchf", "label": "ZCHF",
                            "days": round((z["to"] - z["from"]) / 86400),
                            "meta": z})
            except (OSError, ValueError):
                out.append({"key": "zchf", "label": "ZCHF", "days": None})
            for k, v in sorted(_kl_sources().items()):
                row = {"key": k, "label": k[3:], "days": v["days"]}
                try:
                    row["meta"] = json.loads(Path(
                        str(v["path"]).replace(".json", ".meta.json"))
                        .read_text())
                except (OSError, ValueError):
                    pass
                out.append(row)
            zchf_v2 = HERE / "data" / "sldl_zchf_market.bin"
            client = {"wasm_v": int((HERE / "wasm" / "ref_model_v2.wasm")
                                    .stat().st_mtime)
                      if (HERE / "wasm" / "ref_model_v2.wasm").exists()
                      else None,
                      "zchf_v2": zchf_v2.exists()}
            if client["zchf_v2"]:
                try:
                    client["zchf_v2_meta"] = json.loads(
                        (HERE / "data" / "sldl_zchf_market.meta.json")
                        .read_text())
                except (OSError, ValueError):
                    client["zchf_v2"] = False
            _http_json(self, 200, {"sources": out, "client": client})
            return
        if self.path == "/sldl_progress":
            # Sweep heartbeat (sweep_sl_dl.py --progress-out) so the tab's
            # circle can FILL with actual progress instead of just spinning.
            try:
                _http_json(self, 200, {"running": True,
                                       **json.loads(SLDL_PROG.read_text())})
            except (OSError, ValueError):
                _http_json(self, 200, {"running": False})
            return
        if self.path == "/validation":
            # classic-vs-grid-refine validation results (validate_screen.py)
            f = HERE / "data" / "validation_screen.json"
            if not f.exists():
                _http_json(self, 404, {"error": "run validate_screen.py first"})
                return
            _http_json(self, 200, json.loads(f.read_text()))
            return
        if self.path == "/validation_duration":
            # adaptive v3 (ref_model --auto) vs exhaustive truth across loan
            # durations (validate_duration.py)
            f = HERE / "data" / "validation_duration.json"
            if not f.exists():
                _http_json(self, 404, {"error": "run validate_duration.py first"})
                return
            _http_json(self, 200, json.loads(f.read_text()))
            return
        if self.path == "/sldl":
            # Last S.L./D.L. reference-table result (sweep_ref_table.py).
            # Only ever written by POST /sldl_run, so serving it stale is
            # fine.
            f = HERE / "data" / "sldl_table.json"
            if not f.exists():
                _http_json(self, 404, {"error": "no sweep yet — hit Run"})
                return
            _http_json(self, 200, json.loads(f.read_text()))
            return
        if self.path.startswith("/chart/"):
            # The Spring-Cleaning parameter charts — the SAME matplotlib PNGs
            # that live in images/, re-rendered by the hourly cycle. Fixed
            # whitelist: this must never become a generic file server.
            name = self.path[len("/chart/"):].split("?")[0]
            if name not in ("max_ltv_vs_market_tvl.png",
                            "loan_discount_vs_market_tvl.png",
                            "liquidation_discount_vs_market_tvl.png",
                            "llamma_a_vs_market_tvl.png",
                            "amm_fee_vs_market_tvl.png"):
                self.send_error(404)
                return
            f = HERE / "images" / name
            if not f.exists():
                self.send_error(404)
                return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/progress":
            # How far the running engine has got. Absent file = nothing running.
            try:
                _http_json(self, 200, {"running": True,
                                       **json.loads(PROGRESS_FILE.read_text())})
            except (OSError, ValueError):
                _http_json(self, 200, {"running": False})
            return
        if self.path == "/refresh_status":
            _http_json(self, 200, {
                "now": time.time(),
                "fetched_at": _markets_fetched_at(),
                "running": _refresh["running"],
                "started_at": _refresh["started_at"] if _refresh["running"] else None,
                "next_at": _refresh["next_at"],
                "anchor_min": _refresh["anchor_min"],
                "last_err": _refresh["last_err"],
                "last_duration_s": _refresh["last_duration_s"],
                "candles_fetched_at": _refresh["candles_at"] or None,
                "candles_running": _refresh["candles_running"],
                "candles_last_err": _refresh["candles_last_err"],
            })
            return
        if self.path.startswith("/crashwindow"):
            # Minute-resolution % path of the collateral's worst real crash
            # (fetch_crash_window.py). First call per series fetches ~56 API
            # pages (~20 s) and caches permanently; later calls are instant.
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("key") or [""])[0]
            if not key:
                _http_json(self, 400, {"error": "missing ?key="})
                return
            try:
                rank = max(0, min(2, int((q.get("rank") or ["0"])[0])))
            except ValueError:
                rank = 0
            try:
                import fetch_crash_window
                _http_json(self, 200,
                           fetch_crash_window.build_window(key, rank=rank))
            except Exception as e:
                _http_json(self, 404, {"error": str(e)})
            return
        if self.path == "/candles":
            # Daily collateral OHLC + vol summary per venue series
            # (fetch_candles.py; refreshed daily by the scheduler thread).
            if not CANDLES_FILE.exists():
                _http_json(self, 404, {"error": "run fetchers/fetch_candles.py first"})
                return
            _http_json(self, 200, json.loads(CANDLES_FILE.read_text()))
            return
        if self.path.startswith("/yb"):
            q = parse_qs(urlparse(self.path).query)
            try:
                days = max(1, min(365, int(q.get("days", ["30"])[0])))
                points = max(20, min(300, int(q.get("points", ["120"])[0])))
            except ValueError:
                days, points = 30, 120
            key = (days, points)
            c = _YB_CACHE.get(key)
            if c is None:
                # first load of this window: nothing to serve, block on it
                try:
                    _yb_refresh(key)
                except Exception as e:
                    _http_json(self, 502, {"error": str(e)[:300]})
                    return
            elif time.time() - c["at"] > YB_TTL_S:
                # stale-while-revalidate: answer with the cached window NOW,
                # refresh it in the background (single-flight per key)
                threading.Thread(target=_yb_refresh, args=(key,),
                                 daemon=True).start()
            _http_json(self, 200, _YB_CACHE[key]["data"])
            return
        if self.path == "/pegkeeper":
            # crvUSD PegKeeper state: keepers from the Curve prices API,
            # ceilings read on-chain (the factory PREMINTS each keeper's
            # allowance, so ceiling = crvUSD.balanceOf(keeper) + debt).
            # Stale-while-revalidate: only the very first call ever blocks;
            # afterwards the cache answers and a daemon thread refreshes.
            now = time.time()
            if _PK_CACHE["data"] is None:
                try:
                    _PK_CACHE["busy"] = True
                    _pk_refresh()
                except Exception as e:
                    _http_json(self, 502, {"error": str(e)[:300]})
                    return
            elif now - _PK_CACHE["at"] > 60 and not _PK_CACHE["busy"]:
                _PK_CACHE["busy"] = True
                threading.Thread(target=_pk_refresh, daemon=True).start()
            _http_json(self, 200, _PK_CACHE["data"])
            return
        if self.path.startswith("/debtcap"):
            # Live max-borrowable so the UI can cap the debt field as soon as
            # collateral / N / loan_discount change, without running a sim.
            q = parse_qs(urlparse(self.path).query)
            def f(k, d):
                try: return float(q.get(k, [d])[0])
                except (TypeError, ValueError): return d
            try:
                _http_json(self, 200, single_user.debt_cap(
                    f("collateral_usd", 8_000_000), f("start_price", 0.6072),
                    int(f("n_bands", single_user.DEFAULT_N)), f("loan_discount_pct", 11.0),
                    oracle_price=f("oracle_seed", f("start_price", 0.6072)),
                    llamma_A=int(f("llamma_A", single_user.ONCHAIN_A))))
            except Exception as e:
                _http_json(self, 400, {"error": str(e)})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/sldl_run":
            if not SLDL_SERVER_COMPUTE:
                _http_json(self, 403, {
                    "error": "server-side sweeps are switched off — the "
                             "sim runs in your browser (needs a current, "
                             "cross-origin-isolated browser)"})
                return
            # A×fee sweep (sweep_sl_dl.py). Default 12x12 grid x 3 paths =
            # 12 snapshots + 432 short C++ runs, ~75 s. Grid/paths are UI
            # knobs, clamped here so a typo can't queue an hour of engine
            # runs. Under _run_lock so it never races a sim for CPU.
            length = int(self.headers.get("Content-Length", "0"))
            try:
                p = json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                p = {}

            def num(key, dflt, lo, hi, as_int=False):
                try:
                    v = float(p.get(key, dflt))
                except (TypeError, ValueError):
                    v = dflt
                v = max(lo, min(hi, v))
                return int(round(v)) if as_int else v
            a_lo = num("a_min", 100, 2, 1000, as_int=True)
            a_hi = num("a_max", 180, 2, 1000, as_int=True)
            f_lo = num("fee_min", 0.05, 0.001, 5.0)
            f_hi = num("fee_max", 0.5, 0.001, 5.0)
            grid = num("grid", 10, 2, 16, as_int=True)
            paths = num("paths", 10, 1, 40, as_int=True)
            ma_exp = num("ma_exp_time", 866, 10, 86400)
            # Data sources. The UI offers two:
            #   zchf = the reference-table author's ZCHF/crvUSD 1-min series
            #          (debugging_sldl/zchf_crvusd), file-backed, USD-basis
            #          oracle from his crvUSD/USD aggregator dump
            #   wbtc = WBTC/USDC 1-min candles from TricryptoUSDC over the
            #          last 1.5 years (the Yield Basis WBTC/crvUSD venue the
            #          markets file points at only exists since May 2026).
            #          USDC-quoted -> oracle = EMA of the series itself.
            # Older keys stay accepted for scripts (engine mode + the saved
            # CHF FX 5y file).
            SOURCES = {"wbtc-48h": ("WBTC", 48.0, None),
                       "wbtc-1y": ("WBTC", 8760.0, None),
                       "chf-1y": ("svZCHF", 8760.0, None),
                       "wbtc": ("WBTC", 13140.0, {
                           "chain": "ethereum",
                           "venue": {
                               "pool": "0x7f86bf177dd4f3494b841a37e810a34dd56c829b",
                               "name": "TricryptoUSDC (WBTC/USDC)",
                               "base_addr": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
                               "quote_addr": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                               "quote_symbol": "USDC"}})}
            source = str(p.get("source", "zchf"))
            if source == "zchf-author":
                source = "zchf"
            FILE_SOURCES = {
                "chf-fx-5y": HERE / "data" / "chf_fx_5y_klines.json",
                "zchf": HERE / "zchf" / "his_klines.json",
            }
            for _k, _v in _kl_sources().items():
                FILE_SOURCES[_k] = _v["path"]
            fx_klines = FILE_SOURCES.get(source)
            coll, span_h, venue_override = SOURCES.get(source,
                                                       SOURCES["wbtc-48h"])
            # mode "table" (default, the UI's only mode) = the reference-table
            # reproduction: llamma-simulator v1 model (C++ port), deep-tail
            # statistic. "engine" = the wei-exact C++ sweep, scripts only.
            mode = str(p.get("mode", "table"))
            if mode not in ("engine", "table"):
                mode = "table"
            if mode == "table":
                # Defaults = the recipe that reproduces the author's ZCHF
                # reference table (4 bands, 2-day loans, worst 0.05%, oracle
                # EMA half-life 1200 s x crvUSD/USD). Samples default 25k:
                # measured 1.6% drift from the 100k recipe on the anchor
                # cells, inside the 100k run's own seed scatter, at 1/4 the
                # runtime (see scratch sldl_ladder: 10k is the edge, below
                # that the tail is <5 loans).
                samples = num("samples", 25_000, 200, 1_000_000,
                              as_int=True)
                bands = num("bands", 4, 1, 50, as_int=True)
                loan_days = num("loan_days", 80 / 1440, 10 / 1440, 14.0)
                tail_pct = num("tail_pct", 0.05, 0.001, 50.0)
                oracle_hl = num("oracle_hl", 1200, 30, 86400)
                oracle_mode = str(p.get("oracle_mode", "usd-basis"))
                if oracle_mode not in ("usd-basis", "pool-ema"):
                    oracle_mode = "usd-basis"
                if source in ("chf-fx-5y", "wbtc"):   # already USD-quoted
                    oracle_mode = "pool-ema"
                cells = grid * grid
                capped = None
                # model: v1 (llamma-simulator, the published reference
                # table) or v2 (llamma-simulator_v2, the ZCHF gov post:
                # on-chain oracle limiter, dyn-fee mult 0.25, hl 3603 s).
                # v2 sources: the author's ZCHF data, or any packed USD
                # kline series (oracle = EMA(mid) with aggregate 1.0).
                model = str(p.get("model", "v1"))
                if model not in ("v1", "v2"):
                    model = "v1"
                # N perturbed price realities, averaged per cell (1 = off);
                # runtime scales ~linearly with N
                realities = num("realities", 1, 1, 100, as_int=True)
                if model == "v2":
                    cmd = [PY, str(HERE / "sweeps" / "sweep_v2_table.py"),
                           "--a-min", str(min(a_lo, a_hi)),
                           "--a-max", str(max(a_lo, a_hi)),
                           "--fee-min", str(min(f_lo, f_hi)),
                           "--fee-max", str(max(f_lo, f_hi)),
                           "--grid", str(grid),
                           "--tail-pct", str(num("tail_pct", 0.05, 0.001, 50.0)),
                           "--range-size", str(num("bands", 4, 1, 50, as_int=True)),
                           "--loan-days", str(num("loan_days", 80 / 1440, 10 / 1440, 14.0)),
                           "--texp", str(num("oracle_hl", 3603, 30, 86400)),
                           "--realities", str(realities),
                           "--progress-out", str(SLDL_PROG)]
                    if source != "zchf":
                        if not (fx_klines and fx_klines.exists()):
                            _http_json(self, 400,
                                       {"error": f"{source}: v2 needs a "
                                                 "file-backed kline series"})
                            return
                        cmd += ["--klines-file", str(fx_klines)]
                    with _run_lock:
                        SLDL_PROG.unlink(missing_ok=True)
                        try:
                            r = subprocess.run(cmd, capture_output=True,
                                               text=True, timeout=7200)
                        finally:
                            SLDL_PROG.unlink(missing_ok=True)
                    if r.returncode != 0:
                        _http_json(self, 500, {"error": r.stderr[-1000:]})
                        return
                    _http_json(self, 200, json.loads(
                        (HERE / "data" / "sldl_table.json").read_text()))
                    return
                # method: "exact" (default) = adaptive v3 exhaustive-tail
                # search, deterministic, the search lengths derived from
                # the loan length (validated 30 min .. 4 d, Validation tab);
                # ~0.2-0.7 s per cell. "classic" = random starts (samples),
                # the original recipe, kept for scripts/comparison.
                method = str(p.get("method", "exact"))
                if method not in ("exact", "classic"):
                    method = "exact"
                if method == "classic":
                    # work ~ cells x samples x loan_days; ~30 s per 100k x
                    # 2d cell on 4 threads -> cap at ~45 min
                    budget = 18_000_000
                    if cells * samples * loan_days > budget:
                        capped = samples
                        samples = max(200, int(budget / (cells * loan_days)))
                cmd = [PY, str(HERE / "sweeps" / "sweep_ref_table.py"),
                       "--realities", str(realities),
                       "--a-min", str(min(a_lo, a_hi)),
                       "--a-max", str(max(a_lo, a_hi)),
                       "--fee-min", str(min(f_lo, f_hi)),
                       "--fee-max", str(max(f_lo, f_hi)),
                       "--grid", str(grid), "--samples", str(samples),
                       "--method", method,
                       "--tail-pct", str(tail_pct),
                       "--range-size", str(bands),
                       "--loan-days", str(loan_days),
                       "--texp", str(oracle_hl),
                       "--oracle-mode", oracle_mode,
                       "--progress-out", str(SLDL_PROG)]
                if fx_klines:
                    if not fx_klines.exists():
                        _http_json(self, 400,
                                   {"error": f"{fx_klines.name} not found "
                                             f"({source} source)"})
                        return
                    cmd += ["--klines-file", str(fx_klines)]
                else:
                    cmd += ["--collateral", coll, "--span-h", str(span_h)]
                    if venue_override:
                        cmd += ["--venue-json", json.dumps(venue_override)]
                with _run_lock:
                    SLDL_PROG.unlink(missing_ok=True)
                    try:
                        r = subprocess.run(cmd, capture_output=True,
                                           text=True, timeout=7200)
                    finally:
                        SLDL_PROG.unlink(missing_ok=True)
                if r.returncode != 0:
                    _http_json(self, 500, {"error": r.stderr[-1000:]})
                    return
                d = json.loads(
                    (HERE / "data" / "sldl_table.json").read_text())
                if capped:
                    d["config"]["samples_capped_from"] = capped
                _http_json(self, 200, d)
                return
            # llamma-simulator parity options (all default to THEIR semantics:
            # pinned placement, random 30-60min windows, wick stepping, no-gas
            # deep-venue arb). "maxltv"/"stepped"/"venue" restore the on-chain
            # study modes.
            placement = str(p.get("placement", "pinned"))
            if placement not in ("pinned", "maxltv"):
                placement = "pinned"
            window_mode = str(p.get("window_mode", "random"))
            if window_mode not in ("random", "stepped"):
                window_mode = "random"
            arb = str(p.get("arb", "their"))
            if arb not in ("their", "venue"):
                arb = "their"
            if fx_klines:
                _http_json(self, 400,
                           {"error": f"{source} is a file-backed series with "
                                     "no market/venue attached — reference "
                                     "table mode only"})
                return
            cmd = [PY, str(HERE / "sweeps" / "sweep_sl_dl.py"),
                   "--a-min", str(min(a_lo, a_hi)), "--a-max", str(max(a_lo, a_hi)),
                   "--fee-min", str(min(f_lo, f_hi)), "--fee-max", str(max(f_lo, f_hi)),
                   "--grid", str(grid), "--paths", str(paths),
                   "--collateral", coll, "--span-h", str(span_h),
                   "--ma-exp-time", str(ma_exp),
                   "--placement", placement,
                   "--window-mode", window_mode,
                   "--arb", arb,
                   "--progress-out", str(SLDL_PROG)]
            if bool(p.get("wick", True)):
                cmd.append("--wick")
            if bool(p.get("add_reverse", False)):
                cmd.append("--add-reverse")
            with _run_lock:
                SLDL_PROG.unlink(missing_ok=True)
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=7200)
                finally:
                    SLDL_PROG.unlink(missing_ok=True)
            if r.returncode != 0:
                _http_json(self, 500, {"error": r.stderr[-1000:]})
                return
            _http_json(self, 200, json.loads((HERE / "data" / "sldl.json").read_text()))
            return
        if self.path != "/run":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            params = json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError as e:
            _http_json(self, 400, {"error": f"bad JSON: {e}"})
            return

        with _run_lock:
            result = run_pipeline(params)

        if "error" in result:
            _http_json(self, 500, result)
        else:
            _http_json(self, 200, result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    # THREADED on purpose: a sim now runs for tens of seconds, and a single
    # -threaded server made every other endpoint queue behind it — /debtcap in
    # particular, which the market selector awaits to size the borrower, so
    # picking a market mid-run left collateral/debt/LTV stale. Heavy work is
    # still serialized by _run_lock; only the cheap reads gain concurrency.
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[ui] serving  http://{args.host}:{args.port}/")
    print(f"[ui] scratch  {SCRATCH}")
    threading.Thread(target=_refresher, daemon=True).start()
    threading.Thread(target=_pk_warm, daemon=True).start()
    threading.Thread(target=_kl_daily, daemon=True).start()

    # pre-warm the yb_ tab's default window so the first click is instant
    def _yb_warm():
        # default window first (the tab opens on it), then the other two so
        # a window switch is served from cache instead of a cold ~25 s
        # sampling pass; the keep-warm loop then owns all three
        for key in ((30, 120), (7, 120), (90, 120)):
            try:
                _yb_refresh(key)
                print(f"[ui] yb_ {key[0]}d window pre-warmed", flush=True)
            except Exception as e:
                print(f"[ui] yb_ pre-warm {key} failed: {str(e)[:120]}",
                      flush=True)
    threading.Thread(target=_yb_warm, daemon=True).start()
    threading.Thread(target=_yb_keepwarm, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
