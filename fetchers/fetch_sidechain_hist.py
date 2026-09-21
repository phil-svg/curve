#!/usr/bin/env python3
"""Daily pool history for chains the prices API does not cover
(fantom, avalanche, celo, x-layer, robinhood — kava deliberately skipped).

Everything is derived from one archive read per pool per day, JSON-RPC
batched. The RAW state is what gets stored (balances, virtual_price,
xcp_profit / xcp_profit_a, admin_balances, the block and its real
timestamp); TVL, fees and volume are re-derived from it on every run, so
a price-table or formula fix heals the whole history instead of only the
days appended afterwards:
  - TVL        balances x coin USD price (stables at $1; BTC/ETH legs
               priced from our own 2y daily closes; LP-token legs at the
               tracked base pool's own virtual_price; any other leg of a
               crypto pool whose coin 0 is priced, at the pool's own
               price_oracle against coin 0 — tokenized stocks, say)
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
  - on LOG_CHAINS (providers that answer wide getLogs cheaply) nothing is
               implied: volume is the sum of the pool's own TokenExchange
               events, a crypto-ng pool's fees are the `fee` field of those
               events, a stableswap-ng pool's fees are the growth of its
               admin_balances / admin share, plus what it paid out to the
               fee receiver that day (ng pools pay out on every liquidity
               removal, which resets the counter; its virtual_price is not
               used, it also carries the rate growth of a wrapped leg),
               and a stableswap-ng leg without a price of its own is priced
               through the pool: stored_rates x price_oracle against a
               priced leg. Raw token sums are stored, like everything else.
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
from common import sel, keccak256  # noqa: E402
from fetch_markets import Rpc  # noqa: E402

OUT_DIR = HERE / "data" / "pool_hist"
DAY = 86400
BACK_DAYS = 730
CHAINS = tuple(c for c in os.environ.get(
    "SIDE_CHAINS", "fantom,avalanche,celo,x-layer,robinhood").split(",") if c)
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
# cryptoswap families. The ng builds name two getters differently: the admin
# fee is the constant ADMIN_FEE(), the oracle's MA half time is ma_time().
CRYPTO_OLD = ("tricrypto2", "crypto2")
CRYPTO_NG = ("tricrypto_ng", "twocrypto")
# chains whose providers answer a wide getLogs cheaply: volume and swap fees
# are summed from the pools' own swap events there
LOG_CHAINS = {"robinhood"}
T_SWAP_NG = "0x" + keccak256(
    b"TokenExchange(address,int128,uint256,int128,uint256)").hex()
T_SWAP_CRYPTO_NG = "0x" + keccak256(
    b"TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)"
).hex()
T_TRANSFER = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()
LOG_QUERY_BUDGET = 48   # getLogs requests one pool may cost per run
# stableswap-ng factory per LOG chain: its fee_receiver() is where a pool's
# admin fees are paid out to
NG_FACTORY = {"robinhood": "0x8271e06E5887FE5ba05234f5315c19f3Ec90E8aD"}
# raw per-day state kept in the file next to the served columns
RAW_KEYS = ("_bal", "_adm", "_xcpa", "_blk", "_ts",
            "_rates", "_swn", "_swsold", "_swfee", "_admout")
# providers that cap a JSON-RPC batch (host fragment -> calls per batch)
BATCH_CAP = {"drpc.org": 3}
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
                    cap = next((n for host, n in BATCH_CAP.items()
                                if host in url), 0)
                    if cap and len(payload) > cap:
                        res = []
                        for j in range(0, len(payload), cap):
                            part = http_json(url, payload[j:j + cap])
                            if not isinstance(part, list):
                                raise RuntimeError("batch slice rejected")
                            res += part
                    else:
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
                    # it has the state the others lacked: ask it first from
                    # here on (a chain's own endpoint is often not archive)
                    if url != self.rpc.urls[0]:
                        self.rpc.urls = [url] + [u for u in self.rpc.urls
                                                 if u != url]
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


def coin_prices(r: dict, p: dict, d: int, btc: dict, eth: dict,
                lp_vp: dict[str, dict[int, float]]) -> list:
    """USD price per coin on day d (None where unknown). A leg without a
    price of its own is priced through the pool against a priced leg: a
    crypto pool by its price_oracle against coin 0; on LOG_CHAINS a
    stableswap-ng pool by stored_rates x price_oracle (its oracle is in
    rate-scaled units, so a wrapped leg needs its rate)."""
    n = len(p["coins"])
    px = [price_of(p["sym"][k], d, p["coins"][k].lower(), btc, eth, lp_vp)
          for k in range(n)]
    if all(x is not None for x in px):
        return px
    if p["crypto"]:
        own = r.get("poracle") or r.get("pscale") or []
        if px[0] is not None:
            for k in range(1, n):
                if px[k] is None and len(own) >= k and own[k - 1]:
                    px[k] = px[0] * own[k - 1] / 1e18
    elif p["ng"] and p.get("logs"):
        rates, po = r.get("_rates") or [], r.get("poracle") or []
        if len(rates) == n and all(rates) and len(po) == n - 1 and all(po):
            # one whole token in the pool's scaled units, and its value in coin-0 units
            worth = [rates[k] * 10 ** p["dec"][k] / 1e36 * s
                     for k, s in enumerate([1e18] + list(po))]
            a = next((k for k in range(n) if px[k] is not None), None)
            if a is not None and worth[a] > 0:
                px = [px[k] if px[k] is not None
                      else px[a] * worth[k] / worth[a] for k in range(n)]
    return px


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
    extra = {k: c.get(k) or [None] * n for k in RAW_KEYS}
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
        px = r["_px"] = coin_prices(r, p, d, btc, eth, lp_vp)
        r["tvl"] = None if any(x is None for x in px) else sum(
            bals[k] / 10 ** p["dec"][k] * px[k] for k in range(n))
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
        ng_fees = ng_day_fees(r0, r1.get("_adm"), p, adm_share)
        if ng_fees is not None:
            fees = ng_fees * span           # per-day below, like the rest
        elif lending:
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
    if p.get("logs"):
        # the open day of a stableswap-ng pool closes against the head
        if ds and p.get("_adm_head"):
            r = all_rows[ds[-1]]
            f = ng_day_fees(r, p["_adm_head"], p,
                            (r.get("admin") or 5e9) / 1e10)
            if f is not None:
                r["fees"] = f
        # summed from the pool's swap events (scan_swaps): the newest row
        # holds its day so far
        for d in ds:
            r = all_rows[d]
            px, sold, fee = r.get("_px"), r.get("_swsold"), r.get("_swfee")
            if r.get("_swn") is None or not px or not sold:
                continue
            usd = lambda raw: None if any(a and x is None for a, x in  # noqa: E731
                                          zip(raw, px)) else sum(
                a / 10 ** dec * x for a, dec, x in zip(raw, p["dec"], px) if a)
            r["vol"] = usd(sold)
            if p.get("crypto_ng") and fee:
                r["fees"] = usd(fee)
    return ds


def ng_day_fees(r0: dict, adm1, p: dict, adm_share: float):
    """Fees a stableswap-ng pool charged from row r0 to the state adm1, in
    USD, or None when a piece is missing. Per coin the pool sets exactly
    admin share x fee aside in admin_balances and pays it out to the fee
    receiver now and then, so: (growth of the counter + paid out) / share."""
    if not (p.get("logs") and p["ng"] and adm_share):
        return None
    a0, out, px = r0.get("_adm"), r0.get("_admout"), r0.get("_px")
    if not a0 or not adm1 or out is None or not px \
            or any(x is None for x in list(a0) + list(adm1) + list(px)):
        return None
    got = sum((x1 - x0 + w) / 10 ** dec * q for x0, x1, w, dec, q
              in zip(a0, adm1, out, p["dec"], px))
    return max(0.0, got) / adm_share


def get_logs(ch: Chain, addr: str, frm: int, to: int, topics: list,
             budget: list[int]) -> list:
    """One getLogs over [frm, to], halved where a provider refuses the span."""
    if budget[0] <= 0:
        raise RuntimeError("log query budget spent")
    budget[0] -= 1
    try:
        return ch.rpc.raw("eth_getLogs", [{
            "address": addr, "topics": topics,
            "fromBlock": hex(frm), "toBlock": hex(to)}])
    except Exception:  # noqa: BLE001
        if to - frm < 2000:
            raise
        mid = (frm + to) // 2
        return get_logs(ch, addr, frm, mid, topics, budget) \
            + get_logs(ch, addr, mid + 1, to, topics, budget)


def scan_swaps(ch: Chain, p: dict, all_rows: dict[int, dict]) -> int:
    """Per-day raw sums of the pool's TokenExchange events: tokens sold per
    coin, fee per coin (crypto-ng events carry it, in the bought coin), and
    the swap count. A day runs from its 00:00 block to the next row's; days
    summed on an earlier run are final, the newest row is re-summed up to
    the head every time. Returns the number of swaps read."""
    import bisect
    ds = sorted(d for d in all_rows if all_rows[d].get("_blk"))
    if not ds:
        return 0
    first = next((d for d in ds if all_rows[d].get("_swn") is None), ds[-1])
    todo = [d for d in ds if d >= first]
    starts = [all_rows[d]["_blk"] for d in todo]
    logs = get_logs(ch, p["addr"], starts[0], ch.head_n,
                    [[T_SWAP_NG, T_SWAP_CRYPTO_NG]], [LOG_QUERY_BUDGET])
    n = len(p["coins"])
    acc = {d: [0, [0] * n, [0] * n] for d in todo}
    for lg in logs:
        i = bisect.bisect_right(starts, int(lg["blockNumber"], 16)) - 1
        w = [int(lg["data"][2 + j * 64: 66 + j * 64], 16)
             for j in range((len(lg["data"]) - 2) // 64)]
        if i < 0 or len(w) < 4 or w[0] >= n or w[2] >= n:
            continue
        a = acc[todo[i]]
        a[0] += 1
        a[1][w[0]] += w[1]                       # tokens_sold, in the sold coin
        if lg["topics"][0] == T_SWAP_CRYPTO_NG and len(w) >= 5:
            a[2][w[2]] += w[4]                   # fee, in the bought coin
    # what a stableswap-ng pool paid out to the fee receiver, per coin
    paid = {d: [0] * n for d in todo}
    if p["ng"] and p.get("fee_receiver"):
        pad = lambda a: "0x" + "0" * 24 + a[2:].lower()  # noqa: E731
        for k, coin in enumerate(p["coins"]):
            for lg in get_logs(ch, coin, starts[0], ch.head_n,
                               [T_TRANSFER, pad(p["addr"]),
                                pad(p["fee_receiver"])], [LOG_QUERY_BUDGET]):
                i = bisect.bisect_right(starts, int(lg["blockNumber"], 16)) - 1
                if i >= 0:
                    paid[todo[i]][k] += int(lg["data"], 16)
    for d in todo:
        all_rows[d]["_swn"], all_rows[d]["_swsold"], all_rows[d]["_swfee"] \
            = acc[d]
        all_rows[d]["_admout"] = paid[d] if p["ng"] and p.get("fee_receiver") \
            else None
    return len(logs)


def px_calls(p: dict) -> list[tuple[str, str]]:
    """(tag, calldata) pairs for the pool's price_scale / price_oracle
    reads — crypto pools have both (no-arg when 2 coins, indexed above),
    ng stableswaps have an indexed oracle only, older families none."""
    n = len(p["coins"])
    out = []
    if p["crypto"]:
        if n == 2:
            out += [("ps0", sel("price_scale()")),
                    ("po0", sel("price_oracle()"))]
        else:
            for k in range(n - 1):
                arg = hex(k)[2:].rjust(64, "0")
                out += [(f"ps{k}", sel("price_scale(uint256)") + arg),
                        (f"po{k}", sel("price_oracle(uint256)") + arg)]
    elif p["ng"]:
        for k in range(n - 1):
            out.append((f"po{k}", sel("price_oracle(uint256)")
                        + hex(k)[2:].rjust(64, "0")))
    return out


def px_rows_of(g: dict, p: dict) -> tuple[list | None, list | None]:
    """per-day pscale/poracle lists (raw 1e18 ints, None-padded) from a
    day's decoded reads; (None, None) when the pool has no such reads."""
    n = len(p["coins"])
    if p["crypto"]:
        return ([g.get(f"ps{k}") for k in range(n - 1)],
                [g.get(f"po{k}") for k in range(n - 1)])
    if p["ng"]:
        return None, [g.get(f"po{k}") for k in range(n - 1)]
    return None, None


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
    # a young pool's first rows can sit without parameters: they are read
    # weekly and carried forward, and a re-read first day has nothing before
    # it to carry from. Such rows are read again, parameters included.
    first_p = next((d for d in sorted(have)
                    if all_rows[d].get("a") is not None), None)
    heal = [d for d in sorted(have) if all_rows[d].get("a") is None
            and (first_p is None or d < first_p)][:3]
    days += [d for d in heal if d not in days]
    days.sort()
    n = len(p["coins"])
    crypto, lending, ng = p["crypto"], p["lending"], p["ng"]
    ng_state = bool(p.get("logs") and ng)      # rates + admin balances too
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
            day_calls += px_calls(p)
            if ng_state:
                day_calls.append(("rates", sel("stored_rates()")))
            if lending or ng_state:
                day_calls += [(f"adm{k}", sel("admin_balances(uint256)")
                               + hex(k)[2:].rjust(64, "0"))
                              for k in range(n)]
            if i % 7 == 0 or d == days[-1] or d not in have or d in heal \
                    or d == max(have, default=None):
                # weekly on the backfill grid, every appended day after
                day_calls += [("A", sel("A()")), ("fee", sel("fee()")),
                              ("admin", sel("ADMIN_FEE()" if p["crypto_ng"]
                                            else "admin_fee()"))]
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
                                  ("maht", sel("ma_time()" if p["crypto_ng"]
                                               else "ma_half_time()"))]
            for tag, data in day_calls:
                calls.append(("eth_call", [{"to": p["addr"], "data": data}, bb]))
                layout.append((d, tag))
        res = ch.batch(calls)
        per_day: dict[int, dict] = {d: {} for d in days}
        for (d, tag), r in zip(layout, res):
            # stored_rates() answers a dynamic array: offset, length, values
            per_day[d][tag] = [word(r, 2 + k) for k in range(n)] \
                if tag == "rates" else word(r)

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
            row["pscale"], row["poracle"] = px_rows_of(g, p)
            row["_bal"] = bals
            row["_adm"] = [g.get(f"adm{k}") for k in range(n)] \
                if lending or ng_state else None
            row["_rates"] = g.get("rates") if ng_state else None
            row["_blk"], row["_ts"] = blocks[d]
            all_rows[d] = row
            n_ok += 1
    else:
        n_ok = n_skip = 0

    # px backfill: days committed before the pscale/poracle reads were
    # added get them fetched at their cached block, oldest first, capped
    # per run so the daily cycle stays bounded
    pcalls = px_calls(p)
    if pcalls:
        want = [d for d in sorted(all_rows)
                if all_rows[d].get("poracle") is None
                and all_rows[d].get("_blk")][:500]
        if want:
            res = ch.batch([("eth_call", [{"to": p["addr"], "data": data},
                                          hex(all_rows[d]["_blk"])])
                            for d in want for _tag, data in pcalls])
            m = len(pcalls)
            n_px = 0
            for i, d in enumerate(want):
                g = {tag: word(res[i * m + k])
                     for k, (tag, _da) in enumerate(pcalls)}
                ps, po = px_rows_of(g, p)
                if po and any(v is not None for v in po):
                    all_rows[d]["pscale"] = ps
                    all_rows[d]["poracle"] = po
                    n_px += 1
            if n_px:
                print(f"[side] {ch_name} {p['name'][:28]:28s} px backfill "
                      f"+{n_px} days", flush=True)
    if not all_rows:
        print(f"[side] {ch_name} {p['name'][:28]:28s} no data yet", flush=True)
        return
    n_sw = None
    if p.get("logs"):
        if ng_state:
            head = ch.batch([("eth_call", [{"to": p["addr"], "data":
                              sel("admin_balances(uint256)")
                              + hex(k)[2:].rjust(64, "0")}, "latest"])
                             for k in range(n)])
            adm = [word(x) for x in head]
            p["_adm_head"] = adm if all(x is not None for x in adm) else None
        try:
            n_sw = scan_swaps(ch, p, all_rows)
        except Exception as e:  # noqa: BLE001
            # volume stays unknown for the days not summed yet: retried
            print(f"[side] {ch_name} {p['name'][:28]:28s} swap events not "
                  f"read ({str(e)[:60]})", flush=True)
    ds = derive(all_rows, p, btc, eth, lp_vp)
    out = {"fetched_at": int(time.time()), "chain": ch_name,
           "address": p["addr"], "walkfix": 1, "sidechain": 1,
           "created": created, "syms": p["sym"], "decs": p["dec"],
           "impl": p["impl"], "t": ds}
    for k in PH_FIELDS + list(RAW_KEYS):
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
          f" ({len(ds)} total, tvl on {n_tvl})"
          f"{f', {n_sw} swap events read' if n_sw is not None else ''}",
          flush=True)


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
        fee_receiver = None
        if ch_name in NG_FACTORY:
            try:
                got = ch.rpc.call(NG_FACTORY[ch_name], sel("fee_receiver()"))
                if got and int(got, 16):
                    fee_receiver = "0x" + got[-40:]
            except Exception:  # noqa: BLE001
                fee_receiver = None
        pools = []
        for r in rows_cen:
            addr = r[0].lower()
            impl = (imap.get(f"{ch_name}:{addr}") or {}).get("impl", "")
            if not impl:
                # a pool the implementation map has not classified yet (it
                # runs after this script): which reads make up a day depends
                # on the family, so its history starts on the next cycle
                print(f"[side] {ch_name} {r[1][:28]:28s} not classified "
                      "yet — next cycle", flush=True)
                continue
            pools.append({"addr": addr, "name": r[1], "coins": r[3],
                          "impl": impl,
                          "crypto": impl in CRYPTO_OLD + CRYPTO_NG,
                          "crypto_ng": impl in CRYPTO_NG,
                          "logs": ch_name in LOG_CHAINS,
                          "fee_receiver": fee_receiver,
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
