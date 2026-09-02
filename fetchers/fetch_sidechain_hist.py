#!/usr/bin/env python3
"""Daily pool history for chains the prices API does not cover
(fantom, avalanche, celo, x-layer — kava deliberately skipped).

Everything is derived from one archive read per pool per day, JSON-RPC
batched. The RAW state is what gets stored (balances, virtual_price,
xcp_profit / xcp_profit_a, admin_balances, the block and its real
timestamp); TVL, fees and volume are re-derived from it on every run, so
a price-table or formula fix heals the whole history instead of only the
days appended afterwards:
  - TVL        balances x coin USD price (stables at $1; BTC/ETH legs
               priced from our own 2y daily closes; LP-token legs at the
               tracked base pool's own virtual_price)
  - fees/day   from cumulative accumulators, credited to the day the
               activity happened (delta between that day's 00:00 UTC
               block and the next):
               stableswap: dvp/vp x TVL / (1 - admin_share), minus the
                           base pool's own growth on a metapool's LP leg
               tricrypto2: d(xcp_profit)/xcp_profit x TVL, with admin
                           claims added back via d(xcp_profit_a)
               lending pools (aave/geist): d(admin_balances) / admin_share
               (their vp also carries lending interest, so vp is unusable)
  - volume     fees / fee_rate (estimate — no cumulative counter on-chain)
  - params     A/fee/admin/offpeg + crypto knobs, sampled weekly in the
               backfill and daily going forward (governance-set, slow)

A day is only committed when every core read for it resolved; anything
else (rate limit, pruned node, provider error) is left out and retried on
the next run. Every backfill block is verified against its real timestamp
and refined until it sits at the day boundary — sidechains do not have a
constant block cadence, so interpolating between monthly anchors alone
mis-dates rows by hours to days.

Output: data/pool_hist/<chain>_<addr>.json in the exact _PH_FIELDS shape
ui_server.py serves (plus the raw arrays), so the pool pages just render.
ui_server never rebuilds these chains from the prices API
(SIDE_HIST_CHAINS guard).

Backfill runs once (file absent -> up to 2 years); the refresh cycle then
appends the missing days and refreshes the newest one.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "pylib"))
sys.path.insert(0, str(HERE / "fetchers"))
from common import sel  # noqa: E402
from fetch_markets import Rpc  # noqa: E402

OUT_DIR = HERE / "data" / "pool_hist"
DAY = 86400
BACK_DAYS = 730
CHAINS = tuple(c for c in os.environ.get(
    "SIDE_CHAINS", "fantom,avalanche,celo,x-layer").split(",") if c)
# the ui_server this script runs beside (px fallback); the server exports
# its port so a non-default --port still works
SERVER = f"http://127.0.0.1:{os.environ.get('CURVE_SIM_PORT', '8765')}"
PX_MAX_AGE = 6 * 3600
BLOCK_TOL = 90          # seconds a daily block may sit from 00:00 UTC

# must mirror ui_server._PH_FIELDS
PH_FIELDS = ["bapr", "vol", "fees", "tvl", "a", "gamma", "fee", "admin",
             "offpeg", "mid", "out", "fg", "pscale", "poracle", "vp",
             "xcp", "maht", "aep", "astep", "dd", "dpp", "dsm",
             "maet", "dmat"]
PARAM_TAGS = ("A", "fee", "admin", "offpeg", "gamma", "mid", "out", "fg",
              "aep", "astep", "maht")
PARAM_FIELD = {"A": "a", "fee": "fee", "admin": "admin", "offpeg": "offpeg",
               "gamma": "gamma", "mid": "mid", "out": "out", "fg": "fg",
               "aep": "aep", "astep": "astep", "maht": "maht"}

USD_SYMS = {"usdc", "usdt", "dai", "usdm", "usd₮", "usd₮0", "usdt0",
            "fusdt", "usdg", "frxusd", "frax", "usdp", "nxusd", "yusd",
            "mai", "mimatic", "mim", "busd", "usdc.e", "usdt.e", "dai.e",
            "avdai", "avusdc", "avusdt", "gdai", "gusdc", "gfusdt", "cusd",
            "usdglo", "bdai", "busdc", "busdt", "2crv", "3crv", "musd"}
BTC_SYMS = {"wbtc", "renbtc", "btc.b", "wbtc.e", "avwbtc", "gwbtc",
            "renbtc.e", "btc"}
ETH_SYMS = {"weth", "eth", "weth.e", "aveth", "avweth", "geth", "gweth"}
# legs that are worthless for the whole tracked window (post-depeg UST):
# priced at zero rather than leaving the pool's TVL unknown
DEAD_SYMS = {"ust": 0.0}
# LP tokens priced at their pool's virtual price (token addr -> pool addr)
LP_TOKENS = {
    "0x1337bedc9d22ecbe766df105c9623922a27963ec":      # av3CRV
        "0x7f90122bf0700f9e7e1f688fe926940e8839f353",
}
# plain pools whose coins are rebasing interest-bearing tokens: their
# virtual_price grows with the lending interest too, so fees must come
# from admin_balances like the lending pools
REBASING_POOLS = {
    "0x37c9be6c81990398e9b87494484afc6a4608c25d",      # avalanche blizz bDAI/bUSDC/bUSDT
}


def http_json(url: str, payload, tries: int = 2, timeout: int = 45):
    body = json.dumps(payload).encode()
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, body, headers={
                "Content-Type": "application/json",
                "User-Agent": "curl/8.4.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise last


def http_json_get(url: str, timeout: int = 120):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Chain:
    """Batched JSON-RPC over the chain's provider list (env override
    first, as in fetch_markets). A batch is accepted from the first
    provider that answers every item — public nodes that are not archive
    return per-item errors with HTTP 200, which must not count as an
    answer. Providers that fail outright are dropped for the run."""

    def __init__(self, name: str):
        self.name = name
        self.rpc = Rpc(name)
        self.bad: set[str] = set()
        self.chunk = 40               # halves when a provider caps batches
        self.head_n, self.head_ts = self.rpc.head()
        # day-start block index, shared by every pool on the chain and
        # persisted so later runs only bisect the new day
        self.blk_file = HERE / "data" / "side_blocks" / f"{name}.json"
        self.blk_cache: dict[int, tuple[int, int]] = {}
        if self.blk_file.is_file():
            try:
                self.blk_cache = {int(k): (int(v[0]), int(v[1])) for k, v in
                                  json.loads(self.blk_file.read_text()).items()
                                  # a block hours off its day is a bad
                                  # entry, not a chain gap: redo it
                                  if abs(int(v[1]) - int(k)) <= 6 * 3600}
            except (OSError, ValueError, TypeError):
                self.blk_cache = {}

    @property
    def urls(self) -> list[str]:
        return [u for u in self.rpc.urls if u not in self.bad] \
            or list(self.rpc.urls)

    def batch(self, calls: list[tuple[str, list]]) -> list:
        out = [None] * len(calls)
        i0 = 0
        while i0 < len(calls):
            chunk = calls[i0:i0 + self.chunk]
            payload = [{"jsonrpc": "2.0", "id": k, "method": m, "params": p}
                       for k, (m, p) in enumerate(chunk)]
            best: dict[int, object] = {}
            shrink = False
            for url in self.urls:
                try:
                    res = http_json(url, payload)
                except Exception:  # noqa: BLE001
                    self.bad.add(url)
                    continue
                if isinstance(res, dict):
                    # whole-batch rejection ("too many calls in batch"):
                    # halve the chunk for the rest of the run
                    shrink = True
                    continue
                if not isinstance(res, list):
                    continue
                # an empty "0x" at a historical block is what a node
                # without that state answers — not a result either
                vals = {r["id"]: r["result"] for r in res
                        if isinstance(r, dict) and isinstance(r.get("id"), int)
                        and r.get("result") not in (None, "0x")}
                if len(vals) > len(best):
                    best = vals
                if len(best) == len(chunk):
                    break
            if shrink and not best and self.chunk > 5:
                self.chunk = max(5, self.chunk // 2)
                continue                    # same offset, smaller chunk
            for k in range(len(chunk)):
                out[i0 + k] = best.get(k)
            # leftovers one by one, every provider in turn (legit reverts
            # and pre-creation reads stay None)
            for k, (m, p) in enumerate(chunk):
                if out[i0 + k] is None:
                    out[i0 + k] = self._single(m, p)
            i0 += len(chunk)
        return out

    def _single(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for url in self.urls:
            try:
                r = http_json(url, payload, tries=1, timeout=30)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(r, dict) and r.get("result") not in (None, "0x"):
                return r["result"]
        return None

    def block_ts(self, n: int) -> int:
        b = self.rpc.raw("eth_getBlockByNumber", [hex(n), False])
        return int(b["timestamp"], 16)

    def block_at(self, ts: int, lo: int = 1, hi: int | None = None) -> int:
        """first block with timestamp >= ts (bisection)."""
        hi = hi or self.head_n
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.block_ts(mid) < ts:
                lo = mid
            else:
                hi = mid
        return hi

    def day_blocks(self, days: list[int], lo_hint: int = 1
                   ) -> dict[int, tuple[int, int | None]]:
        """day-start ts -> (block, block timestamp). Monthly bisected
        anchors give a first guess by interpolation; every guess is then
        verified in batch and moved by its timestamp error along the
        local block rate until it sits within BLOCK_TOL of the day start
        (or the round budget is spent — the real timestamp is kept)."""
        if not days:
            return {}
        days = sorted(days)
        need = [d for d in days if d not in self.blk_cache]
        if need:
            self._build_blocks(need, lo_hint)
            self.blk_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.blk_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({str(k): list(v) for k, v in
                                       sorted(self.blk_cache.items())}))
            tmp.replace(self.blk_file)
        return {d: self.blk_cache[d] for d in days if d in self.blk_cache}

    def _build_blocks(self, days: list[int], lo_hint: int) -> None:
        # two exact anchors (bisection), linear first guess, then batched
        # verify-and-move rounds along the local block rate
        known: set[tuple[int, int]] = set(self.blk_cache.values())
        lo = max(1, lo_hint)
        below = [b for b, t in known if t <= days[0]]
        if below:
            lo = max(lo, max(below) - 10)
        n0 = self.block_at(days[0], lo)
        pts = [(n0, self.block_ts(n0))]
        if days[-1] > days[0]:
            n1 = self.block_at(days[-1], max(1, n0 - 10))
            pts.append((n1, self.block_ts(n1)))
        known |= set(pts)
        (b0, t0), (b1, t1) = pts[0], pts[-1]
        avg = (b1 - b0) / (t1 - t0) if t1 > t0 else \
            self.head_n / max(1, self.head_ts)
        guess = {d: max(1, min(self.head_n, int(b0 + (d - t0) * avg)))
                 for d in days}
        ts_of: dict[int, int] = {}
        for _round in range(14):
            need = [d for d in days if d not in ts_of]
            if not need:
                break
            res = self.batch([("eth_getBlockByNumber", [hex(guess[d]), False])
                              for d in need])
            for d, r in zip(need, res):
                if r and isinstance(r, dict) and r.get("timestamp"):
                    ts_of[d] = int(r["timestamp"], 16)
                    known.add((guess[d], ts_of[d]))
            pts_now = sorted(known)
            moved = False
            for d in need:
                if d not in ts_of:
                    continue
                err = ts_of[d] - d
                if abs(err) <= BLOCK_TOL:
                    continue
                slope = self._local_slope(pts_now, guess[d], avg)
                new = int(round(guess[d] - err * slope))
                new = max(1, min(self.head_n, new))
                # chains that only mint blocks with transactions can have
                # no block near midnight at all: once the step is down to
                # a couple of blocks we are at the boundary — keep it
                # (anything still oscillating falls to the bisection below)
                if abs(new - guess[d]) <= 2:
                    continue
                guess[d] = new
                del ts_of[d]
                moved = True
            if not moved:
                break
        # anything still open: bisect between the nearest known blocks
        pts_now = sorted(known)
        for d in days:
            if d in ts_of:
                continue
            lo_c = [b for b, t in pts_now if t < d]
            hi_c = [b for b, t in pts_now if t >= d]
            lo_b = max(lo_c) if lo_c else 1
            hi_b = min(hi_c) if hi_c else self.head_n
            try:
                b = self.block_at(d, lo_b, hi_b)
                ts_of[d] = self.block_ts(b)
                guess[d] = b
            except Exception:  # noqa: BLE001
                continue
        for d in days:
            if d in ts_of:
                self.blk_cache[d] = (guess[d], ts_of[d])

    @staticmethod
    def _local_slope(known: list[tuple[int, int]], block: int,
                     avg: float) -> float:
        below = [p for p in known if p[0] < block]
        above = [p for p in known if p[0] > block]
        if below and above:
            (b0, t0), (b1, t1) = below[-1], above[0]
            if t1 > t0 and b1 > b0:
                return (b1 - b0) / (t1 - t0)
        return avg if avg > 0 else 0.2

    def has_code(self, addr: str, block: int) -> bool:
        try:
            c = self.rpc.raw("eth_getCode", [addr, hex(block)])
        except Exception:  # noqa: BLE001
            return True         # unknown: do not drop the day
        return bool(c) and c != "0x"

    def creation_day(self, addr: str) -> int | None:
        """day-start ts of the first block where addr has code (bisection
        over block numbers); None when it has none at the head."""
        if not self.has_code(addr, self.head_n):
            return None
        lo, hi = 1, self.head_n
        if self.has_code(addr, lo):
            return self.block_ts(lo) // DAY * DAY
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.has_code(addr, mid):
                hi = mid
            else:
                lo = mid
        return self.block_ts(hi) // DAY * DAY


def word(res, i: int = 0) -> int | None:
    if not isinstance(res, str) or res == "0x" or len(res) < 2 + (i + 1) * 64:
        return None
    return int(res[2 + i * 64: 2 + (i + 1) * 64], 16)


WBTC_ETH = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
WETH_ETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def px_series(token: str, now_day: int) -> dict[int, float]:
    """Our own 2y daily USD closes for an ethereum token. The server's
    /pxhist rebuilds past its TTL, so it is asked first whenever the
    on-disk copy is stale; the file is the fallback."""
    f = HERE / "data" / "px_hist" / f"ethereum_{token}.json"
    file_d = None
    if f.is_file():
        try:
            file_d = json.loads(f.read_text())
        except (OSError, ValueError):
            file_d = None
    fresh = bool(file_d) and \
        time.time() - file_d.get("fetched_at", 0) < PX_MAX_AGE and \
        file_d.get("t") and max(file_d["t"]) // DAY * DAY >= now_day - DAY
    d = file_d if fresh else None
    if d is None:
        try:
            srv = http_json_get(f"{SERVER}/pxhist?t=ethereum:{token}")
            if srv.get("t") and srv.get("px"):
                d = srv
        except Exception:  # noqa: BLE001
            d = None
    if d is None:
        d = file_d
    if not d or not d.get("t"):
        return {}
    return {t // DAY * DAY: p for t, p in zip(d["t"], d["px"])
            if p is not None}


def lookup(series: dict[int, float], day: int, back_days: int = 3):
    """exact day, else the newest earlier value within back_days (the
    current day's close does not exist yet while the day is running)."""
    if not series:
        return None
    v = series.get(day)
    if v is not None:
        return v
    for k in range(1, back_days + 1):
        v = series.get(day - k * DAY)
        if v is not None:
            return v
    return None


def price_of(sym: str, day: int, coin_addr: str, btc: dict, eth: dict,
             lp_vp: dict[str, dict[int, float]]) -> float | None:
    s = sym.lower()
    if s in USD_SYMS:
        return 1.0
    if s in DEAD_SYMS:
        return DEAD_SYMS[s]
    if s in BTC_SYMS:
        return lookup(btc, day)
    if s in ETH_SYMS:
        return lookup(eth, day)
    pool_addr = LP_TOKENS.get(coin_addr, coin_addr)
    if pool_addr in lp_vp:                  # LP token of a tracked pool
        return lookup(lp_vp[pool_addr], day)
    return None


def decode_symbol(sres) -> str:
    if not isinstance(sres, str) or sres == "0x":
        return ""
    try:
        b = bytes.fromhex(sres[2:])
    except ValueError:
        return ""
    if len(b) >= 64:
        ln = int.from_bytes(b[32:64], "big")
        return b[64:64 + ln].decode("utf-8", "replace").strip()
    return b.rstrip(b"\0").decode("utf-8", "replace")     # bytes32 symbol


def load_cached(f: Path) -> dict | None:
    if not f.is_file():
        return None
    try:
        c = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if c.get("sidechain") != 1:
        return None                     # old empty API artefact
    return c


def rows_from_cache(c: dict) -> dict[int, dict]:
    n = len(c["t"])
    extra = {k: c.get(k) or [None] * n
             for k in ("_bal", "_adm", "_xcpa", "_blk", "_ts")}
    rows = {}
    for i, d in enumerate(c["t"]):
        r = {k: (c.get(k) or [None] * n)[i] for k in PH_FIELDS}
        for k, arr in extra.items():
            r[k] = arr[i]
        rows[d] = r
    return rows


def derive(all_rows: dict[int, dict], p: dict, btc: dict, eth: dict,
           lp_vp: dict[str, dict[int, float]]) -> list[int]:
    """Re-derive tvl / fees / vol for every day from the raw state.
    Returns the sorted day list."""
    ds = sorted(all_rows)
    n = len(p["coins"])
    crypto, lending = p["crypto"], p["lending"]
    # TVL from raw balances (rows from older files without _bal keep theirs)
    for d in ds:
        r = all_rows[d]
        bals = r.get("_bal")
        if not bals or len(bals) != n or any(b is None for b in bals):
            continue
        tvl = 0.0
        for k in range(n):
            px = price_of(p["sym"][k], d, p["coins"][k].lower(), btc, eth,
                          lp_vp)
            if px is None:
                tvl = None
                break
            tvl += bals[k] / 10 ** p["dec"][k] * px
        r["tvl"] = tvl
    # value share of LP-token legs (metapool correction)
    lp_legs = [(k, LP_TOKENS.get(p["coins"][k].lower(), p["coins"][k].lower()))
               for k in range(n)
               if LP_TOKENS.get(p["coins"][k].lower(), p["coins"][k].lower())
               in lp_vp]
    for d in ds:
        all_rows[d]["fees"] = None
        all_rows[d]["vol"] = None
    for prev, d in zip(ds, ds[1:]):
        r0, r1 = all_rows[prev], all_rows[d]
        span = max(1, (d - prev) // DAY)
        tvl = r0.get("tvl")
        adm_share = (r0.get("admin") or 5e9) / 1e10
        fees = None
        if lending:
            a0, a1 = r0.get("_adm"), r1.get("_adm")
            if a0 and a1 and all(x is not None for x in a0 + a1):
                dadm = sum(max(0, x1 - x0) / 10 ** dec for x0, x1,
                           dec in zip(a0, a1, p["dec"]))
                fees = dadm / adm_share if adm_share else None
        elif crypto:
            x0, x1 = r0.get("xcp"), r1.get("xcp")
            xa0, xa1 = r0.get("_xcpa"), r1.get("_xcpa")
            if x0 and x1 and tvl:
                gross = x1 - x0
                # an admin claim knocks a*(xcp - xcp_a) off xcp_profit and
                # moves xcp_a to the new level: add the claimed part back
                if xa0 is not None and xa1 is not None and xa1 > xa0 \
                        and adm_share < 1:
                    gross += (xa1 - xa0) * adm_share / (1 - adm_share)
                if gross >= 0:
                    fees = gross / x0 * tvl
        else:
            v0, v1 = r0.get("vp"), r1.get("vp")
            if v0 and v1 and tvl:
                growth = v1 / v0 - 1
                for k, base in lp_legs:          # base pool's own growth
                    bv0 = lookup(lp_vp[base], prev, 0)
                    bv1 = lookup(lp_vp[base], d, 0)
                    bals = r0.get("_bal")
                    if bv0 and bv1 and bals and bals[k] is not None and tvl:
                        w = bals[k] / 10 ** p["dec"][k] * bv0 / tvl
                        growth -= w * (bv1 / bv0 - 1)
                if growth >= 0:
                    lp_fees = growth * tvl
                    fees = lp_fees / (1 - adm_share) \
                        if adm_share < 1 else lp_fees
        if fees is not None:
            fees /= span
        r0["fees"] = fees
        fr = (r0.get("fee") or 0) / 1e10
        r0["vol"] = fees / fr if fees and fr else None
    return ds


def process_pool(ch: Chain, ch_name: str, p: dict, now_day: int,
                 btc: dict, eth: dict, lp_vp: dict[str, dict[int, float]]
                 ) -> None:
    f = OUT_DIR / f"{ch_name}_{p['addr']}.json"
    cached = load_cached(f)
    all_rows = rows_from_cache(cached) if cached else {}
    created = (cached or {}).get("created")
    have = set(all_rows)
    start = max(now_day - BACK_DAYS * DAY, created or 0)
    days = [d for d in range(start, now_day + DAY, DAY)
            if d <= now_day and (d not in have or d == max(have, default=-1))]
    # a cached row sampled at a block the (since corrected) index no
    # longer agrees with was read from the wrong day — redo it
    idx = ch.blk_cache
    days += [d for d in have if d not in days and all_rows[d].get("_blk")
             and (d not in idx or idx[d][0] != all_rows[d]["_blk"])]
    days.sort()
    n = len(p["coins"])
    crypto, lending, ng = p["crypto"], p["lending"], p["ng"]
    if days and created is None:
        created = ch.creation_day(p["addr"])
        if created is None:
            print(f"[side] {ch_name} {p['name'][:28]:28s} no code at head",
                  flush=True)
            return
        days = [d for d in days if d >= created]
    if days:
        # bisection can start at the newest cached block below the first
        # wanted day instead of block 1
        earlier = [all_rows[d]["_blk"] for d in all_rows
                   if d < days[0] and all_rows[d].get("_blk")]
        blocks = ch.day_blocks(days, max(1, max(earlier) - 10) if earlier else 1)
        days = [d for d in days if d in blocks]    # unverified days wait
    if days:
        calls, layout = [], []
        for i, d in enumerate(days):
            bb = hex(blocks[d][0])
            day_calls = [("vp", sel("get_virtual_price()"))]
            day_calls += [(f"bal{k}", sel("balances(uint256)")
                           + hex(k)[2:].rjust(64, "0")) for k in range(n)]
            if crypto:
                day_calls += [("xcp", sel("xcp_profit()")),
                              ("xcpa", sel("xcp_profit_a()"))]
            if lending:
                day_calls += [(f"adm{k}", sel("admin_balances(uint256)")
                               + hex(k)[2:].rjust(64, "0"))
                              for k in range(n)]
            if i % 7 == 0 or d == days[-1] or d not in have:
                # weekly on the backfill grid, every appended day after
                day_calls += [("A", sel("A()")), ("fee", sel("fee()")),
                              ("admin", sel("admin_fee()"))]
                if ng:
                    day_calls.append(("offpeg",
                                      sel("offpeg_fee_multiplier()")))
                if crypto:
                    day_calls += [("gamma", sel("gamma()")),
                                  ("mid", sel("mid_fee()")),
                                  ("out", sel("out_fee()")),
                                  ("fg", sel("fee_gamma()")),
                                  ("aep", sel("allowed_extra_profit()")),
                                  ("astep", sel("adjustment_step()")),
                                  ("maht", sel("ma_half_time()"))]
            for tag, data in day_calls:
                calls.append(("eth_call", [{"to": p["addr"], "data": data}, bb]))
                layout.append((d, tag))
        res = ch.batch(calls)
        per_day: dict[int, dict] = {d: {} for d in days}
        for (d, tag), r in zip(layout, res):
            per_day[d][tag] = word(r)

        # params carry forward from the newest cached row
        last_p: dict[str, int | None] = {}
        before = [d for d in all_rows if d < days[0]]
        if before:
            rb = all_rows[max(before)]
            for tag, fld in PARAM_FIELD.items():
                last_p[tag] = rb.get(fld)
        n_ok = n_skip = 0
        for d in days:
            g = per_day[d]
            for k in PARAM_TAGS:
                if g.get(k) is not None:
                    last_p[k] = g[k]
            bals = [g.get(f"bal{k}") for k in range(n)]
            core_ok = g.get("vp") is not None and all(b is not None for b in bals)
            if crypto:
                core_ok = core_ok and g.get("xcp") is not None
            if lending:
                core_ok = core_ok and all(g.get(f"adm{k}") is not None
                                          for k in range(n))
            if not core_ok:
                n_skip += 1
                continue                    # retried on the next run
            row = {k: None for k in PH_FIELDS}
            row["vp"] = g["vp"]
            for tag, fld in PARAM_FIELD.items():
                row[fld] = last_p.get(tag)
            row["xcp"] = g.get("xcp")
            row["_xcpa"] = g.get("xcpa")
            row["_bal"] = bals
            row["_adm"] = [g.get(f"adm{k}") for k in range(n)] if lending else None
            row["_blk"], row["_ts"] = blocks[d]
            all_rows[d] = row
            n_ok += 1
    else:
        n_ok = n_skip = 0

    if not all_rows:
        print(f"[side] {ch_name} {p['name'][:28]:28s} no data yet", flush=True)
        return
    ds = derive(all_rows, p, btc, eth, lp_vp)
    out = {"fetched_at": int(time.time()), "chain": ch_name,
           "address": p["addr"], "walkfix": 1, "sidechain": 1,
           "created": created, "syms": p["sym"], "decs": p["dec"],
           "impl": p["impl"], "t": ds}
    for k in PH_FIELDS + ["_bal", "_adm", "_xcpa", "_blk", "_ts"]:
        out[k] = [all_rows[d].get(k) for d in ds]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(f)
    lp_vp[p["addr"]] = {d: all_rows[d]["vp"] / 1e18 for d in ds
                        if all_rows[d].get("vp")}
    n_tvl = sum(1 for d in ds if all_rows[d].get("tvl") is not None)
    print(f"[side] {ch_name} {p['name'][:28]:28s} +{n_ok} days"
          f"{f' ({n_skip} unresolved, retry next run)' if n_skip else ''}"
          f" ({len(ds)} total, tvl on {n_tvl})", flush=True)


def main() -> None:
    # one instance at a time: a manual backfill and the refresh cycle's run
    # would otherwise write the same files
    import fcntl
    lock_path = HERE / "data" / "side_blocks" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[side] another run is in progress — skipping", flush=True)
        return
    census = json.loads((HERE / "data" / "census.json").read_text())["pools"]
    imap = json.loads((HERE / "data" / "impl_map.json").read_text())["pools"]
    now_day = int(time.time()) // DAY * DAY
    btc = px_series(WBTC_ETH, now_day)
    eth = px_series(WETH_ETH, now_day)
    if not btc or not eth:
        print("[side] WARNING: no BTC/ETH price series — BTC/ETH legs "
              "stay unpriced this run", flush=True)

    for ch_name in CHAINS:
        rows_cen = census.get(ch_name) or []
        if not rows_cen:
            continue
        try:
            ch = Chain(ch_name)
        except Exception as e:  # noqa: BLE001
            print(f"[side] {ch_name}: no provider answers ({str(e)[:80]})",
                  flush=True)
            continue
        pools = []
        for r in rows_cen:
            addr = r[0].lower()
            impl = (imap.get(f"{ch_name}:{addr}") or {}).get("impl", "")
            pools.append({"addr": addr, "name": r[1], "coins": r[3],
                          "impl": impl,
                          "crypto": impl in ("tricrypto2", "crypto2"),
                          "lending": impl == "lending_underlying"
                          or addr in REBASING_POOLS,
                          "ng": impl in ("stableswap_ng", "meta_ng")})
        # base pools first: their vp prices the LP-token legs of the rest
        bases = set(LP_TOKENS.values())
        pools.sort(key=lambda p: 0 if p["addr"] in bases else 1)
        # coin metadata once per chain
        meta_calls = []
        for p in pools:
            for c in p["coins"]:
                meta_calls += [("eth_call", [{"to": c, "data": sel("symbol()")},
                                             "latest"]),
                               ("eth_call", [{"to": c, "data": sel("decimals()")},
                                             "latest"])]
        try:
            meta = ch.batch(meta_calls)
        except Exception as e:  # noqa: BLE001
            print(f"[side] {ch_name}: coin metadata failed ({str(e)[:80]})",
                  flush=True)
            continue
        mi = 0
        for p in pools:
            p["sym"], p["dec"] = [], []
            for _c in p["coins"]:
                p["sym"].append(decode_symbol(meta[mi]))
                p["dec"].append(word(meta[mi + 1]) or 18)
                mi += 2

        lp_vp: dict[str, dict[int, float]] = {}
        for p in pools:
            try:
                process_pool(ch, ch_name, p, now_day, btc, eth, lp_vp)
            except Exception as e:  # noqa: BLE001
                print(f"[side] {ch_name} {p['name'][:28]:28s} FAILED: "
                      f"{type(e).__name__}: {str(e)[:100]}", flush=True)


if __name__ == "__main__":
    main()
