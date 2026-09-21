"""Backend for the new-llamalend tab: a free-form LLV2 market workbench.

Read-only research tooling, served under /nlapi/ by ui_server.py:

  GET  /nlapi/ping                      health + which chains have an RPC
  GET  /nlapi/token?chain&address       ERC20 symbol / name / decimals
  GET  /nlapi/pool?chain&address        Curve pool introspection (coins, A,
                                        virtual price, price_oracle, ma time)
  POST /nlapi/call                      live eth_call batch (oracle sources)
  POST /nlapi/sample_exact              the same call through history (archive
                                        node), block-exact: read in every
                                        block where a contract behind it logged
                                        something, refined in between until
                                        straight lines match the chain
  GET  /nlapi/ema_tree?chain&address    every EMA time along the oracle chain
                                        behind a contract (pools, aggregators,
                                        wrappers), by walking what it reads
  GET  /nlapi/curve_ohlc?...            one window of prices.curve.finance
                                        OHLC, proxied so the page needs no
                                        CORS and repeat loads hit the cache
  GET  /nlapi/abi?chain&address         the verified ABI of a contract
                                        (Sourcify, then Blockscout), reduced
                                        to the read functions a source can call
  GET  /nlapi/aggregator?chain          the crvUSD/USD price aggregator the
                                        live markets of this chain read
  GET  /nlapi/pack?market=<file>        everything a registered market's charts
                                        need, prepared by hand ahead of time:
                                        one gzipped file, no RPC on a page load
  GET  /nlapi/coin_routes?chain&pool&quote   for every coin of a pool: the
                                        last-trade readings that price it in
                                        the quote token

Nothing here executes anything a visitor sends: addresses are validated as 20
bytes of hex, chains against a fixed table, the outbound hosts are constants,
and what comes back from them is reduced to checked names and ABI types.

History can never change, so windows that lie fully in the past are cached
under data/nl_cache/ keyed by the request itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
for _d in (HERE / "pylib", HERE / "fetchers"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import common  # noqa: E402

CACHE = HERE / "data" / "nl_cache"
LOCAL_CFG = HERE / "data" / "nl_local.json"   # gitignored, machine-specific


def _own_node() -> bool:
    try:
        return bool(json.loads(LOCAL_CFG.read_text()).get("rpc"))
    except (OSError, ValueError, AttributeError):
        return False


# By default this server never reads the chain for the tab: it serves the prepared market packs
# (data/nl_cache/packs/, built by hand with fetchers/build_nl_packs.py) and refuses every route
# that would cost an RPC call. Nothing here runs on a timer. Only a machine that names its own node
# in data/nl_local.json (never in the repo) gets the chain-reading routes.
PUBLIC = not _own_node()
PUBLIC_ROUTES = {"pack", "ping", "abi"}       # abi = Sourcify / Blockscout + disk cache, no RPC
UA = {"Content-Type": "application/json", "User-Agent": "curve-sim-nl"}
PRICES_API = "https://prices.curve.finance/v1/ohlc/{chain}/{pool}"


# ---- RPC --------------------------------------------------------------------
def _rpc_urls(chain: str) -> list[str]:
    """Endpoints to try, in order: NL_RPC_<CHAIN> env, then the machine-local
    config file ({"rpc": {"ethereum": ["http://..."]}} - where a LAN archive
    node goes, so its address never enters the repo), then the project
    default. History sampling needs an ARCHIVE endpoint."""
    import os
    urls: list[str] = []
    env = os.environ.get(f"NL_RPC_{chain.upper()}")
    if env:
        urls.append(env)
    try:
        urls += list((json.loads(LOCAL_CFG.read_text()).get("rpc") or {}).get(chain) or [])
    except (OSError, ValueError):
        pass
    if chain == "ethereum":
        try:
            urls.append(common.rpc_url())
        except RuntimeError:                  # a machine with only its local node configured
            pass
    else:
        try:
            import fetch_markets as fm
            urls += list(fm.CHAIN_RPCS.get(chain, []))
        except Exception:
            pass
    return list(dict.fromkeys(urls))


def _post(url: str, payload, timeout: float = 60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rpc_batch(chain: str, calls: list[tuple], chunk: int = 400,
              workers: int = 3) -> list:
    """calls = [(method, params), ...] -> results in order; a failed item is
    None (a revert is data here: the contract did not exist yet, the pool has
    no such getter, ...). Falls back to single requests on endpoints that
    refuse batches."""
    urls = _rpc_urls(chain)
    if not urls:
        raise RuntimeError(f"no RPC configured for chain {chain!r}")
    out: list = [None] * len(calls)

    def run(lo: int) -> None:
        part = calls[lo:lo + chunk]
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                for i, (m, p) in enumerate(part)]
        last = None
        for url in urls:
            for attempt in range(4):
                try:
                    res = _post(url, body)
                    if isinstance(res, dict):      # batch refused -> singles
                        res = [_post(url, b) for b in body]
                    for item in res:
                        if isinstance(item, dict) and "result" in item:
                            out[lo + int(item["id"])] = item["result"]
                    return
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code != 429:              # rate limit: back off, retry
                        break
                    time.sleep(1.5 * (attempt + 1))
                except Exception as e:             # next endpoint
                    last = e
                    break
        raise RuntimeError(f"RPC batch failed: {str(last)[:160]}")

    starts = list(range(0, len(calls), chunk))
    if len(starts) <= 1:
        for s in starts:
            run(s)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(run, starts))
    return out


# ---- ABI (static types only: that is every getter an oracle exposes) ---------
def _selector(sig: str) -> str:
    return common.keccak256(sig.replace(" ", "").encode())[:4].hex()


def _arg_types(sig: str) -> list[str]:
    inner = sig[sig.index("(") + 1: sig.rindex(")")].strip()
    return [t.strip() for t in inner.split(",")] if inner else []


def _to_int(v) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    s = str(v).strip().replace("_", "")
    if s.lower().startswith("0x"):
        return int(s, 16)
    if "**" in s:                                  # 10**18
        b, e = s.split("**", 1)
        return int(b) ** int(e)
    return int(Decimal(s))                         # "1e18", "1000000"


def encode_call(sig: str, args: list | None = None) -> str:
    types = _arg_types(sig)
    args = list(args or [])
    if len(args) != len(types):
        raise ValueError(f"{sig}: expected {len(types)} args, got {len(args)}")
    data = _selector(sig)
    for t, a in zip(types, args):
        if t == "address":
            data += str(a).lower().replace("0x", "").rjust(64, "0")
        elif t == "bool":
            data += hex(1 if a in (True, 1, "1", "true", "True") else 0)[2:].rjust(64, "0")
        elif t.startswith("uint") or t.startswith("int"):
            n = _to_int(a)
            if n < 0:
                n += 1 << 256
            data += hex(n)[2:].rjust(64, "0")
        else:
            raise ValueError(f"unsupported argument type {t!r}")
    return "0x" + data


def decode_word(hexdata: str | None, slot: int = 0, rtype: str = "uint"):
    if not hexdata or hexdata == "0x":
        return None
    body = hexdata[2:]
    w = body[64 * slot: 64 * (slot + 1)]
    if len(w) < 64:
        return None
    if rtype == "address":
        return "0x" + w[24:]
    n = int(w, 16)
    if rtype == "int" and n >= 1 << 255:
        n -= 1 << 256
    return n


def decode_string(hexdata: str | None) -> str | None:
    """ABI string, or the bytes32 some old tokens return instead."""
    if not hexdata or hexdata == "0x":
        return None
    raw = bytes.fromhex(hexdata[2:])
    try:
        if len(raw) >= 64:
            off = int.from_bytes(raw[:32], "big")
            if off + 32 <= len(raw):
                ln = int.from_bytes(raw[off:off + 32], "big")
                if 0 < ln <= len(raw) - off - 32:
                    return raw[off + 32: off + 32 + ln].decode("utf-8", "replace")
        return raw[:32].rstrip(b"\0").decode("utf-8", "replace") or None
    except Exception:
        return None


def _addr(s: str) -> str:
    s = str(s or "").strip().lower()
    if not (s.startswith("0x") and len(s) == 42):
        raise ValueError(f"not an address: {s[:60]!r}")
    int(s, 16)
    return s


def _call(to: str, sig: str, args=None, block="latest") -> tuple:
    return ("eth_call", [{"to": to, "data": encode_call(sig, args)},
                         block if isinstance(block, str) else hex(block)])


# ---- endpoints ---------------------------------------------------------------
def token_info(chain: str, address: str) -> dict:
    a = _addr(address)
    r = rpc_batch(chain, [_call(a, "symbol()"), _call(a, "name()"),
                          _call(a, "decimals()")])
    dec = decode_word(r[2])
    if r[0] is None and r[2] is None:
        raise ValueError("no ERC20 at this address (symbol/decimals revert)")
    return {"chain": chain, "address": a, "checksum": checksum(a),
            "symbol": decode_string(r[0]), "name": decode_string(r[1]),
            "decimals": dec if dec is not None and dec < 78 else None}


def checksum(addr: str) -> str:
    """EIP-55, which logo repositories key their files by."""
    a = addr.lower().replace("0x", "")
    hx = common.keccak256(a.encode()).hex()
    return "0x" + "".join(c.upper() if int(hx[i], 16) >= 8 else c for i, c in enumerate(a))


def pool_info(chain: str, address: str) -> dict:
    a = _addr(address)
    names = ["coins0", "coins1", "coins2", "coins3", "A", "A_precise", "gamma",
             "vp", "po", "po0", "po1", "ma_exp_time", "ma_time", "fee",
             "bal0", "bal1", "bal2", "bal3", "supply", "symbol", "name",
             "lp_price", "last0", "D"]
    calls = [_call(a, "coins(uint256)", [0]), _call(a, "coins(uint256)", [1]),
             _call(a, "coins(uint256)", [2]), _call(a, "coins(uint256)", [3]),
             _call(a, "A()"), _call(a, "A_precise()"), _call(a, "gamma()"),
             _call(a, "get_virtual_price()"), _call(a, "price_oracle()"),
             _call(a, "price_oracle(uint256)", [0]),
             _call(a, "price_oracle(uint256)", [1]),
             _call(a, "ma_exp_time()"), _call(a, "ma_time()"), _call(a, "fee()"),
             _call(a, "balances(uint256)", [0]), _call(a, "balances(uint256)", [1]),
             _call(a, "balances(uint256)", [2]), _call(a, "balances(uint256)", [3]),
             _call(a, "totalSupply()"), _call(a, "symbol()"), _call(a, "name()"),
             _call(a, "lp_price()"), _call(a, "last_prices(uint256)", [0]),
             _call(a, "D()")]
    r = dict(zip(names, rpc_batch(chain, calls)))
    coins = [decode_word(r[f"coins{i}"], 0, "address") for i in range(4)]
    coins = [c for c in coins if c and int(c, 16) != 0]
    if not coins:
        raise ValueError("not a Curve pool (coins(0) reverts)")
    meta = rpc_batch(chain, [c for x in coins
                             for c in (_call(x, "symbol()"), _call(x, "decimals()"))])
    toks = []
    for i, x in enumerate(coins):
        dec = decode_word(meta[2 * i + 1])
        bal = decode_word(r[f"bal{i}"])
        toks.append({"address": x, "symbol": decode_string(meta[2 * i]),
                     "decimals": dec,
                     "balance": (bal / 10 ** dec) if bal is not None and dec is not None else None})
    is_crypto = r["gamma"] is not None
    w = lambda k: decode_word(r[k])                                  # noqa: E731
    f18 = lambda k: (w(k) / 1e18) if w(k) is not None else None       # noqa: E731
    po = [f18("po")] if w("po") is not None else \
        [v for v in (f18("po0"), f18("po1")) if v is not None]
    return {
        "chain": chain, "address": a, "symbol": decode_string(r["symbol"]),
        "name": decode_string(r["name"]), "coins": toks, "n_coins": len(coins),
        "family": "cryptoswap" if is_crypto else "stableswap",
        # price_oracle(uint256) exists only on NG / multi-coin builds
        "price_oracle_takes_index": w("po") is None and w("po0") is not None,
        "A": w("A"), "A_precise": w("A_precise"), "gamma": w("gamma"),
        "virtual_price": f18("vp"), "price_oracle": po, "lp_price": f18("lp_price"),
        "ma_exp_time": w("ma_exp_time") if w("ma_exp_time") is not None else w("ma_time"),
        "fee_1e10": w("fee"),
        "total_supply": f18("supply"),
    }


def _norm_src(c: dict) -> dict:
    """One on-chain read: where, what, which return word, how to scale it."""
    return {"to": _addr(c["to"]), "sig": str(c["sig"]).replace(" ", ""),
            "args": list(c.get("args") or []), "slot": int(c.get("slot") or 0),
            "rtype": str(c.get("rtype") or "uint"),
            "decimals": int(c["decimals"]) if c.get("decimals") is not None else 18}


def _scaled(raw, src: dict):
    v = decode_word(raw, src["slot"], src["rtype"])
    if v is None or isinstance(v, str):
        return v
    return float(Decimal(v) / (Decimal(10) ** src["decimals"]))


def live_calls(body: dict) -> dict:
    chain = str(body.get("chain") or "ethereum")
    block = body.get("block") or "latest"
    srcs = [(str(c.get("id")), _norm_src(c)) for c in body.get("calls") or []]
    if len(srcs) > 400:
        raise ValueError("too many calls")
    res = rpc_batch(chain, [_call(s["to"], s["sig"], s["args"], block) for _, s in srcs]
                    + [("eth_getBlockByNumber", [block if isinstance(block, str) else hex(block), False])])
    blk = res[-1] or {}
    return {"block": int(blk["number"], 16) if blk.get("number") else None,
            "timestamp": int(blk["timestamp"], 16) if blk.get("timestamp") else None,
            "values": {i: _scaled(raw, s) for (i, s), raw in zip(srcs, res)}}


def _headers(chain: str, numbers: list[int]) -> list:
    """(number -> timestamp). Erigon's header call is ~20x lighter than a block
    with its tx-hash list; other endpoints get the portable fallback."""
    if chain == "ethereum":
        res = rpc_batch(chain, [("erigon_getHeaderByNumber", [hex(n)]) for n in numbers])
        if res and any(r is not None for r in res):
            return [int(r["timestamp"], 16) if r else None for r in res]
    res = rpc_batch(chain, [("eth_getBlockByNumber", [hex(n), False]) for n in numbers])
    return [int(r["timestamp"], 16) if r else None for r in res]


def _exact_blocks(chain: str, stamps: list[int]) -> tuple[list[int], list[int]]:
    """Block at each timestamp by interpolation search: estimate from the
    average block time, read the real header time, correct, repeat. Missed
    slots make one pass inexact; every pass shrinks the error by the local
    block-time mismatch (~10x), so a handful converge."""
    latest = rpc_batch(chain, [("eth_getBlockByNumber", ["latest", False])])[0]
    n_l, t_l = int(latest["number"], 16), int(latest["timestamp"], 16)
    n_p = max(1, n_l - 200_000)
    t_p = _headers(chain, [n_p])[0] or (t_l - 12 * (n_l - n_p))
    bt = max(0.05, (t_l - t_p) / max(1, n_l - n_p))
    est = [min(n_l, max(1, round(n_l - (t_l - ts) / bt))) for ts in stamps]
    real = _headers(chain, est)
    for _ in range(8):
        moved = False
        for i, ts in enumerate(stamps):
            if real[i] is None:
                continue
            d = ts - real[i]
            if abs(d) > bt * 1.5:
                step = round(d / bt)
                if step:
                    est[i] = min(n_l, max(1, est[i] + step))
                    moved = True
        if not moved:
            break
        real = _headers(chain, est)
    return est, [r if r is not None else ts for r, ts in zip(real, stamps)]



# ---- block-exact history: sample where something changed, not on a clock -------------
# A reading only moves in blocks where a contract behind it changed state (those blocks
# carry its logs), or smoothly with time (an EMA decaying, a vault rate creeping). So the
# exact per-block history is: the value at every such "dirty" block, plus as many points
# in between as it takes for straight lines between neighbours to match the chain within
# `tol`. Over 240 days a quiet pool is dirty in well under 1 % of blocks.
EXACT_WINDOW_S = 432_000        # 5 days; the page walks aligned windows, past ones are cached on disk
LOG_CHUNK = 10_000              # blocks per eth_getLogs
_TS: dict = {}                  # chain -> {block: timestamp}
_EDGE: dict = {}                # (chain, unix) -> block at that time


def _write_json(f: Path, obj) -> None:
    """Atomic, and safe when two requests write the same file at once (three sources watch one pool)."""
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.{time.time_ns()}.{id(obj)}.tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(f)
    except OSError:
        pass                                       # a cache: the next request simply computes it again


def _stamps_of(chain: str, blocks: list[int]) -> dict:
    memo = _TS.setdefault(chain, {})
    need = [b for b in dict.fromkeys(blocks) if b not in memo]
    for i in range(0, len(need), 5000):
        part = need[i:i + 5000]
        for b, ts in zip(part, _headers(chain, part)):
            if ts is not None:
                memo[b] = ts
    if len(memo) > 3_000_000:
        memo.clear()
    return memo


def _block_at(chain: str, ts: int) -> int:
    k = (chain, ts)
    if k not in _EDGE:
        _EDGE[k] = _exact_blocks(chain, [ts])[0][0]
    return _EDGE[k]


def _dirty_blocks(chain: str, address: str, b_lo: int, b_hi: int, latest: int) -> list[int]:
    """Blocks in [b_lo, b_hi] in which `address` emitted a log. Whole chunks that lie in
    the past are kept on disk."""
    out: set[int] = set()
    todo = []
    for c0 in range(b_lo // LOG_CHUNK * LOG_CHUNK, b_hi + 1, LOG_CHUNK):
        f = CACHE / "dirty" / chain / address / f"{c0}.json"
        if f.exists():
            try:
                out.update(json.loads(f.read_text()))
                continue
            except (OSError, ValueError):
                pass
        todo.append((c0, f))

    def scan(lo: int, hi: int) -> list[int]:
        try:
            logs = rpc_batch(chain, [("eth_getLogs", [{"address": address, "fromBlock": hex(lo), "toBlock": hex(hi)}])])[0]
        except Exception:
            logs = None
        if logs is None:                           # too many results / range refused: halve it
            if hi <= lo:
                return []
            mid = (lo + hi) // 2
            return scan(lo, mid) + scan(mid + 1, hi)
        return sorted({int(x["blockNumber"], 16) for x in logs})

    def one(item):
        c0, f = item
        hi = min(c0 + LOG_CHUNK - 1, latest)
        blocks = scan(c0, hi)
        if c0 + LOG_CHUNK - 1 < latest - 64:
            _write_json(f, blocks)
        return blocks

    if todo:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for blocks in ex.map(one, todo):
                out.update(blocks)
    return sorted(b for b in out if b_lo <= b <= b_hi)


def sample_exact(body: dict) -> dict:
    """One aligned window of a reading's block-exact history.
    raw = a stored value that only changes in a dirty block (last_price, balances, a
    Chainlink answer): held until its next point. Otherwise the reading may also drift
    with time and jump: every dirty block is read together with the block before it, and
    intervals are halved until the chord between neighbours is within `tol` (relative)."""
    chain = str(body.get("chain") or "ethereum")
    src = _norm_src(body)
    w0 = int(body["from"])
    if w0 % EXACT_WINDOW_S:
        raise ValueError("window must start on a 5-day boundary")
    w1 = w0 + EXACT_WINDOW_S
    raw_mode = bool(body.get("raw"))
    tol = min(1e-3, max(1e-8, float(body.get("tol") or 5e-6)))
    watch = list(dict.fromkeys([_addr(a) for a in (body.get("watch") or [])][:8]))
    key = hashlib.sha1(json.dumps(["exact3", chain, src, w0, raw_mode, tol, watch], sort_keys=True).encode()).hexdigest()
    f = CACHE / "exact" / f"{key}.json"
    if f.exists():
        try:
            return {**json.loads(f.read_text()), "cached": True}
        except (OSError, ValueError):
            pass
    now = int(time.time())
    if w0 > now:
        return {"t": [], "v": [], "b": [], "cached": False}
    latest = int(rpc_batch(chain, [("eth_blockNumber", [])])[0], 16)
    b_lo = _block_at(chain, w0)
    b_hi = latest if w1 > now - 30 else _block_at(chain, w1)
    if b_hi <= b_lo:
        return {"t": [], "v": [], "b": [], "cached": False}
    # safety anchors: hourly where the dirty blocks tell the whole story, every 5 minutes otherwise
    n_5min = max(2, (min(w1, now) - w0) // 300)
    dirty: set[int] = set()
    complete = bool(watch)
    for a in watch:
        d = _dirty_blocks(chain, a, b_lo, b_hi, latest)
        if len(d) > 2 * n_5min:                    # busier than the 5-minute clock: the clock is cheaper
            complete = False
            continue
        dirty.update(d)
    if not dirty:                                  # nothing logged at all (a proxy, a quiet stretch): trust the clock, not the silence
        complete = False
    # every kink and jump sits in a dirty block when the watch list is complete: hourly anchors then, the halving below does the rest
    n_anchor = max(2, n_5min // 12) if complete else n_5min
    pts = {b_lo, b_hi} | {b_lo + round(k * (b_hi - b_lo) / n_anchor) for k in range(n_anchor + 1)} | dirty
    if not raw_mode:
        pts |= {d - 1 for d in dirty if d - 1 >= b_lo}
    vals: dict[int, float | None] = {}

    def read(blocks: list[int]) -> None:
        blocks = [b for b in blocks if b not in vals]
        res = rpc_batch(chain, [_call(src["to"], src["sig"], src["args"], b) for b in blocks], chunk=1000)
        for b, r in zip(blocks, res):
            vals[b] = _scaled(r, src)

    read(sorted(pts))
    order = sorted(b for b in pts if vals.get(b) is not None)
    work = list(zip(order, order[1:]))             # intervals not explained yet
    for _ in range(14):                            # halve them until straight lines (or a logged change) explain every one
        ask = []
        for a, b in work:
            if b - a < 2:
                continue
            va, vb = vals[a], vals[b]
            if raw_mode:
                if va != vb and not (complete and b in dirty):
                    ask.append((a, b))             # a change no log explains: find its block
            elif abs(vb - va) > tol * max(abs(va), abs(vb), 1e-30):
                ask.append((a, b))
        if not ask or len(vals) + len(ask) > 400_000:
            break
        read([(a + b) // 2 for a, b in ask])
        work = []
        for a, b in ask:
            m = (a + b) // 2
            vm = vals.get(m)
            if vm is None:
                continue
            if not raw_mode:
                chord = vals[a] + (vals[b] - vals[a]) * (m - a) / (b - a)
                if abs(vm - chord) <= tol * max(abs(vm), 1e-30):
                    continue                       # the chord holds: this interval is done
            work += [(a, m), (m, b)]
    blocks = sorted(b for b, v in vals.items() if v is not None)
    ts = _stamps_of(chain, blocks)
    blocks = [b for b in blocks if b in ts]
    out = {"t": [ts[b] for b in blocks], "v": [vals[b] for b in blocks], "b": blocks,
           "dirty": len(dirty), "complete": complete}
    if w1 < now - 900 and blocks:
        _write_json(f, out)
    return {**out, "cached": False}


def curve_ohlc(q: dict) -> dict:
    """One prices-API window: `main` priced in `ref`... the API's naming is
    inverted relative to intuition (fetch_candles.py documents it): passing
    main_token=QUOTE, reference_token=BASE returns BASE priced in QUOTE. The
    page passes base/quote; this function does the swap."""
    chain = q.get("chain", ["ethereum"])[0]
    pool = _addr(q["pool"][0])
    base, quote = _addr(q["base"][0]), _addr(q["quote"][0])
    t0, t1 = int(q["from"][0]), int(q["to"][0])
    units = q.get("units", ["hour"])[0]
    num = int(q.get("number", ["1"])[0])
    if units not in ("minute", "hour", "day"):
        raise ValueError("units must be minute|hour|day")
    key = hashlib.sha1(json.dumps([chain, pool, base, quote, t0, t1, units, num])
                       .encode()).hexdigest()
    f = CACHE / "ohlc" / f"{key}.json"
    if f.exists():
        try:
            return {**json.loads(f.read_text()), "cached": True}
        except (OSError, ValueError):
            pass
    url = PRICES_API.format(chain=chain, pool=pool) + "?" + urllib.parse.urlencode({
        "main_token": quote, "reference_token": base, "agg_number": num,
        "agg_units": units, "start": t0, "end": t1})
    req = urllib.request.Request(url, headers={"User-Agent": "curve-sim-nl"})
    with urllib.request.urlopen(req, timeout=40) as r:
        j = json.loads(r.read())
    rows = [[c["time"], c["open"], c["high"], c["low"], c["close"]]
            for c in (j.get("data") or [])
            if c.get("close") is not None and c.get("open") is not None]
    rows.sort()
    out = {"rows": rows}
    if t1 < int(time.time()) - 900:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(f)
    return {**out, "cached": False}


def ema_tree(chain: str, address: str) -> dict:
    """Every EMA along the oracle chain behind one contract. Reuses the walker
    that builds data/oracles.json (fetch_oracles.walk): BFS over every contract
    the root reads, each probed for its EMA getters (ma_exp_time, ma_time,
    ma_half_time, exp_time ...). Depth = hops from the root. Cached for a day:
    oracle wiring only changes when something is redeployed."""
    a = _addr(address)
    f = CACHE / "ema_tree" / f"{chain}_{a}.json"
    if f.exists() and time.time() - f.stat().st_mtime < 86400:
        try:
            return {**json.loads(f.read_text()), "cached": True}
        except (OSError, ValueError):
            pass
    import fetch_oracles as fo
    from fetch_markets import Rpc
    rpc = Rpc(chain)
    rpc.urls = _rpc_urls(chain) or rpc.urls
    nodes = fo.walk(rpc, a, {})
    depth = {a: 0}
    order = [a]
    for cur in order:                                  # BFS order = display order
        for ref in (nodes.get(cur) or {}).get("refs", {}).values():
            r = str(ref).lower()
            if r in nodes and r not in depth:
                depth[r] = depth[cur] + 1
                order.append(r)
    out_nodes = []
    for addr in order:
        n = nodes[addr]
        out_nodes.append({"address": addr, "type": n.get("type"), "label": n.get("label") or "",
                          "ma": n.get("ma") or {}, "depth": depth[addr],
                          "refs": [str(r).lower() for r in (n.get("refs") or {}).values()
                                   if str(r).lower() in nodes]})
    times = [v for n in out_nodes for v in n["ma"].values()]
    root_ma = list((nodes.get(a) or {}).get("ma", {}).values())
    # the EMA that shapes the ROOT's reading: its own if it has one, else the
    # one its pools share; mixed chains are reported as such, never averaged
    suggested = root_ma[0] if root_ma else (times[0] if times and len(set(times)) == 1 else None)
    out = {"root": a, "nodes": out_nodes, "ema_times": sorted(set(times)),
           "suggested": suggested, "mixed": len(set(times)) > 1}
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(f)
    return {**out, "cached": False}


# ---- contract ABIs -----------------------------------------------------------
CHAIN_IDS = {"ethereum": 1, "optimism": 10, "bsc": 56, "xdai": 100, "gnosis": 100,
             "polygon": 137, "sonic": 146, "fraxtal": 252, "base": 8453,
             "arbitrum": 42161, "avalanche": 43114, "mantle": 5000,
             "hyperliquid": 999}
SOURCIFY = "https://sourcify.dev/server/v2/contract/{cid}/{addr}?fields=abi,compilation,proxyResolution"
BLOCKSCOUT = {"ethereum": "https://eth.blockscout.com",
              "optimism": "https://optimism.blockscout.com",
              "arbitrum": "https://arbitrum.blockscout.com",
              "fraxtal": "https://fraxtal.blockscout.com",
              "sonic": "https://sonic.blockscout.com",
              "base": "https://base.blockscout.com",
              "xdai": "https://gnosis.blockscout.com",
              "gnosis": "https://gnosis.blockscout.com"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_ARG_T = re.compile(r"^(u?int(\d{1,3})?|address|bool)$")            # what encode_call can write
_WORD_T = re.compile(r"^(u?int(\d{1,3})?|address|bool|bytes\d{1,2})$")  # one 32-byte return word
_ARR_T = re.compile(r"^(u?int(?:\d{1,3})?|address|bool)\[(\d{1,2})\]$")


def _get_json(url: str, timeout: float = 20, cap: int = 4_000_000):
    """GET a JSON document from one of the fixed hosts above; `cap` bounds what
    is read, so a misbehaving upstream cannot fill the server's memory."""
    req = urllib.request.Request(url, headers={"User-Agent": "curve-sim-nl",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(cap))


def _read_functions(abi) -> list[dict]:
    """The part of an ABI a source can use: view / pure functions whose
    arguments are plain words, with the 32-byte words they answer in. Every
    name and type is checked: an ABI is third-party text."""
    out = []
    for f in abi if isinstance(abi, list) else []:
        if not isinstance(f, dict) or f.get("type") != "function":
            continue
        if f.get("stateMutability") not in ("view", "pure"):
            continue
        name = str(f.get("name") or "")
        ins = f.get("inputs") or []
        if not _IDENT.match(name) or len(ins) > 6:
            continue
        if any(not _ARG_T.match(str(i.get("type") or "")) for i in ins):
            continue
        words = []
        for o in f.get("outputs") or []:
            t = str(o.get("type") or "")
            oname = str(o.get("name") or "")
            oname = oname if _IDENT.match(oname) else ""
            arr = _ARR_T.match(t)
            if _WORD_T.match(t):
                words.append({"name": oname, "type": t})
            elif arr:                                   # uint256[2]: inline words
                words += [{"name": f"{oname or 'out'}[{j}]", "type": arr.group(1)}
                          for j in range(int(arr.group(2)))]
            else:                                       # dynamic: later words are offsets
                break
            if len(words) >= 16:
                break
        if not any(w["type"].startswith(("uint", "int")) for w in words):
            continue
        out.append({"name": name,
                    "sig": f"{name}({','.join(str(i['type']) for i in ins)})",
                    "inputs": [{"name": str(i.get('name') or '') if _IDENT.match(str(i.get('name') or '')) else "",
                                "type": str(i["type"])} for i in ins],
                    "outputs": words})
        if len(out) >= 400:
            break
    return sorted(out, key=lambda x: (len(x["inputs"]), x["name"].lower()))


def _abi_from(chain: str, a: str) -> dict | None:
    cid = CHAIN_IDS.get(chain)
    if cid:
        try:
            d = _get_json(SOURCIFY.format(cid=cid, addr=a))
            if d.get("abi"):
                px = d.get("proxyResolution") or {}
                impl = [str(i.get("address") or "") for i in px.get("implementations") or []] \
                    if px.get("isProxy") else []
                return {"abi": d["abi"], "source": "sourcify",
                        "name": str((d.get("compilation") or {}).get("name") or "")[:80],
                        "language": str((d.get("compilation") or {}).get("language") or "")[:20],
                        "impl": impl[:1]}
        except Exception:
            pass
    base = BLOCKSCOUT.get(chain)
    if base:
        try:
            d = _get_json(f"{base}/api/v2/smart-contracts/{a}", timeout=25)
            if d.get("abi"):
                impl = [str(i.get("address") or "") for i in d.get("implementations") or []]
                return {"abi": d["abi"], "source": "blockscout",
                        "name": str(d.get("name") or "")[:80],
                        "language": str(d.get("language") or "")[:20], "impl": impl[:1]}
        except Exception:
            pass
    return None


def contract_abi(chain: str, address: str) -> dict:
    """Verified ABI -> the read functions a source can call. Sourcify first,
    Blockscout second; a proxy answers with its implementation's functions.
    Cached on disk: a verified contract for a month, a miss for a day."""
    a = _addr(address)
    if not re.match(r"^[a-z0-9_-]{1,24}$", chain):
        raise ValueError("unknown chain")
    f = CACHE / "abi" / f"{chain}_{a}.json"
    if f.exists():
        try:
            hit = json.loads(f.read_text())
            if time.time() - f.stat().st_mtime < (30 * 86400 if hit.get("verified") else 86400):
                return {**hit, "cached": True}
        except (OSError, ValueError):
            pass
    got = _abi_from(chain, a)
    out = {"chain": chain, "address": a, "verified": bool(got), "source": None,
           "name": "", "language": "", "proxy_of": None, "functions": []}
    if got:
        fns = _read_functions(got["abi"])
        impl = next((i.lower() for i in got["impl"]
                     if re.match(r"^0x[0-9a-fA-F]{40}$", i) and i.lower() != a), None)
        if impl:
            sub = _abi_from(chain, impl)
            if sub:
                have = {x["sig"] for x in fns}
                fns += [x for x in _read_functions(sub["abi"]) if x["sig"] not in have]
                out["proxy_of"] = impl
                got["name"] = sub["name"] or got["name"]
        name = re.sub(r"[^A-Za-z0-9_ .:()/-]", "", got["name"])[:80]
        out.update({"source": got["source"], "name": name,
                    "language": re.sub(r"[^A-Za-z]", "", got["language"])[:20],
                    "functions": fns})
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(f)
    return {**out, "cached": False}


# ---- Curve pools holding a token -------------------------------------------------
POOLS_API = "https://api.curve.finance/v1/getPools/all/{chain}"
_POOLS_MEMO: dict = {}                       # chain -> (fetched_at, poolData)
_STABLE_REG = ("main", "factory", "factory-stable-ng", "factory-crvusd", "factory-eywa")


def _curve_pools(chain: str) -> list:
    hit = _POOLS_MEMO.get(chain)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        data = _get_json(POOLS_API.format(chain=chain), timeout=60, cap=80_000_000)
        pools = (data.get("data") or {}).get("poolData") or []
        _POOLS_MEMO[chain] = (time.time(), pools)
        return pools
    except Exception:
        if hit:                              # a stale registry beats none
            return hit[1]
        raise



def coin_routes(chain: str, pool: str, quote: str) -> dict:
    """For every coin of `pool`: the on-chain readings whose product prices ONE
    TOKEN of it in `quote`, from last traded prices only (no EMA anywhere).

    A hop is one Curve pool: X in Y = last(X) / last(Y), where last(coin 0) = 1.
    Pools that keep rates (NG: ERC4626 / oracle coins) quote per unit of the
    UNDERLYING, so the hop also carries stored_rates() words: x rate(X) / rate(Y).
    A vault over the quote itself (scrvUSD for crvUSD) counts as the quote: its
    own rate is left out, which is the same as redeeming the share.
    Route: the deepest pool holding the coin and the quote, or through `pool`
    itself into a sibling coin that has such a pool, whichever is deeper at its
    thinnest hop (a thin pool's last trade can be hours old)."""
    info = pool_info(chain, pool)
    q, home = _addr(quote), info["address"]
    own = [c["address"] for c in info["coins"]]
    reg, home_tvl = [], 0.0
    for p in _curve_pools(chain):
        addr = str(p.get("address") or "").lower()
        cs = [str(c.get("address") or "").lower() for c in (p.get("coins") or [])]
        try:
            tvl = float(p.get("usdTotal") or 0)
        except (TypeError, ValueError):
            tvl = 0.0
        if addr == home:
            home_tvl = tvl
        if re.match(r"^0x[0-9a-f]{40}$", addr) and not p.get("isBroken") and 50_000 <= tvl < 1e13 \
                and any(c in own for c in cs):
            reg.append((tvl, addr, cs, str(p.get("name") or "")[:60]))
    # vaults over the quote, among everything these pools hold
    others = sorted({c for _t, _a, cs, _n in reg for c in cs if c != q and re.match(r"^0x[0-9a-f]{40}$", c)})
    assets = rpc_batch(chain, [_call(c, "asset()") for c in others]) if others else []
    quoteish = {q} | {c for c, r in zip(others, assets) if decode_word(r, 0, "address") == q}

    def direct(c):
        best = max((x for x in reg if c in x[2] and x[1] != home and any(y in quoteish for y in x[2])),
                   key=lambda x: x[0], default=None)
        if not best:
            return None, 0.0
        y = q if q in best[2] else next(y for y in best[2] if y in quoteish)
        return [(best[1], best[3], best[2], c, y)], best[0]

    first = {c: (([], float("inf")) if c in quoteish else direct(c)) for c in own}
    plans = {}
    for c in own:
        plans[c], depth = first[c]
        for d in own:
            via, d_depth = first[d]
            if d != c and via is not None and min(home_tvl, d_depth) > depth:
                plans[c], depth = [(home, info.get("name") or "", own, c, d)] + via, min(home_tvl, d_depth)
    # how each pool on a route answers: which last-price getter, and whether it keeps rates
    addrs = sorted({h[0] for pl in plans.values() if pl for h in pl})
    probe = rpc_batch(chain, [x for a in addrs for x in
                              [_call(a, s, [0] if idx else []) for s, idx in _LAST_SIGS] + [_call(a, "stored_rates()")]]) if addrs else []
    how = {}
    for k, a in enumerate(addrs):
        r = probe[5 * k: 5 * k + 5]
        sig = next(((s, idx) for (s, idx), raw in zip(_LAST_SIGS, r) if decode_word(raw) is not None), None)
        n_rates = decode_word(r[4], 1) if r[4] else None
        n_rates = n_rates if n_rates is not None and 0 < n_rates <= 8 else 0
        how[a] = (sig, [decode_word(r[4], 2 + i) for i in range(n_rates)])
    decs = {c["address"]: c["decimals"] for c in info["coins"]}
    need = sorted({t for pl in plans.values() if pl for h in pl for t in (h[3], h[4]) if t not in decs})
    for t, r in zip(need, rpc_batch(chain, [_call(t, "decimals()") for t in need]) if need else []):
        decs[t] = decode_word(r)
    out = []
    for c in info["coins"]:
        pl, hops, ok = plans[c["address"]], [], True
        for (a, name, cs, x, y) in (pl or []):
            sig, rates = how.get(a, (None, []))
            if sig is None:
                ok = False
                break
            s, idx = sig

            def last(tok):
                i = cs.index(tok)
                if i == 0:
                    return None
                if not idx and i > 1:
                    raise ValueError("pool getter takes no index")
                return {"to": a, "sig": s, "args": [i - 1] if idx else [], "slot": 0, "rtype": "uint", "decimals": 18}

            def rate(tok, skip):
                i = cs.index(tok)
                if skip or i >= len(rates) or decs.get(tok) is None or rates[i] in (None, 10 ** (36 - int(decs[tok]))):
                    return None                                  # a plain token: its rate is 1 for ever
                return {"to": a, "sig": "stored_rates()", "args": [], "slot": 2 + i, "rtype": "uint",
                        "decimals": 36 - int(decs[tok])}
            try:
                hops.append({"pool": a, "name": name, "num": last(x), "den": last(y),
                             "rate_num": rate(x, False), "rate_den": rate(y, y in quoteish and y != q)})
            except ValueError:
                ok = False
                break
        out.append({"address": c["address"], "symbol": c["symbol"], "decimals": c["decimals"],
                    "is_quote": c["address"] in quoteish, "hops": hops if pl is not None and ok else None})
    return {"chain": chain, "pool": home, "quote": q, "coins": out}


def aggregator(chain: str) -> dict:
    """The crvUSD/USD aggregator of a chain: whichever contract the live lending
    markets there read as one (data/oracles.json), with its price now."""
    seen: dict[str, int] = {}
    try:
        snap = json.loads((HERE / "data" / "oracles.json").read_text())
    except (OSError, ValueError):
        snap = {}
    for m in (snap.get("markets") or {}).values():
        if m.get("chain") != chain:
            continue
        for addr, n in (m.get("nodes") or {}).items():
            if (n or {}).get("type") == "agg":
                seen[addr.lower()] = seen.get(addr.lower(), 0) + 1
    if not seen:
        return {"chain": chain, "address": None}
    a = max(seen, key=seen.get)
    price = None
    try:
        v = decode_word(rpc_batch(chain, [_call(a, "price()")])[0])
        price = v / 1e18 if v is not None else None
    except Exception:
        pass
    return {"chain": chain, "address": a, "checksum": checksum(a), "sig": "price()",
            "price": price, "used_by_markets": seen[a]}


# ---- market packs: everything the tab's charts need, prepared ahead of any visit -------
# A page load must not cost one RPC call. For every registered market (nl/markets/index.json)
# build_pack reads what the tab would otherwise ask for piecemeal (block-exact history of
# every on-chain source the script reads, the coin-chart readings, what each reading is in
# words, contract names, EMA trees, the values at the latest block) and writes ONE gzipped file.
# The page fetches that file and nothing else; only an explicit "Load & evaluate" still samples.
# A pack is built by hand (fetchers/build_nl_packs.py), never by the server and never on a timer.
MARKETS_DIR = HERE / "nl" / "markets"
PACKS = CACHE / "packs"
_HELD = re.compile(r"^(last_price|last_prices|balances|get_balances|totalSupply|latestRoundData|latestAnswer)\(")


def market_files() -> list[str]:
    try:
        idx = json.loads((MARKETS_DIR / "index.json").read_text())
    except (OSError, ValueError):
        return []
    return [f for f in idx.get("markets") or [] if re.match(r"^[a-z0-9][a-z0-9-]*\.json$", str(f))
            and (MARKETS_DIR / f).is_file()]


def _reading_text(chain: str, src: dict, agg_addr: str | None) -> str:
    """What a reading is, in words (the chips and sliders of the tab): mirrors ui/oracle.js readingText."""
    addr, fn = _addr(src["address"]), str(src.get("sig") or "").split("(")[0]
    try:
        info = pool_info(chain, addr)
    except Exception:
        info = None
    if info and len(info.get("coins") or []) > 1:
        try:
            k = max(0, int((src.get("args") or [0])[0]))
        except (TypeError, ValueError):
            k = 0
        coins = info["coins"]
        base, coin = coins[0]["symbol"], (coins[k + 1] if k + 1 < len(coins) else coins[1])["symbol"]
        if fn == "price_oracle":
            return f"{coin} in {base} · pool EMA"
        if fn in ("last_price", "last_prices"):
            return f"{coin} in {base} · last trade"
        if fn == "get_p":
            return f"{coin} in {base} · pool spot"
        if fn == "get_virtual_price":
            return f"{info.get('symbol') or 'pool'} virtual price"
        if fn == "lp_price":
            return f"{info.get('symbol') or 'pool'} LP price"
        return f"{fn}() of {info.get('symbol') or info.get('name') or addr[:10]}"
    if agg_addr and agg_addr == addr and fn == "price":
        return "crvUSD in USD · aggregator"
    try:
        name = contract_abi(chain, addr).get("name")
    except Exception:
        name = None
    return f"{fn}() of {name or addr[:6] + '…' + addr[-4:]}"


def _exact_series(chain: str, reading: dict, raw: bool, watch: list, t_from: int, t_to: int) -> dict:
    t: list = []
    v: list = []
    w0 = t_from // EXACT_WINDOW_S * EXACT_WINDOW_S
    while w0 <= t_to:
        r = sample_exact({"chain": chain, **reading, "from": w0, "raw": raw, "watch": watch})
        for a, b in zip(r["t"], r["v"]):
            if b is not None and (not t or a > t[-1]):
                t.append(a)
                v.append(float(f"{b:.12g}"))
        w0 += EXACT_WINDOW_S
    return {"t": t, "v": v, "held": raw}


def build_pack(file: str) -> dict:
    """Read everything once, write data/nl_cache/packs/<file>.gz. Past history windows come from the
    disk cache, so a refresh costs the open window of each reading."""
    import gzip
    spec = json.loads((MARKETS_DIR / file).read_text())
    chain = str(spec.get("chain") or "ethereum")
    rng = (spec.get("oracle") or {}).get("range") or {}
    step = max(60, int(rng.get("step_s") or 300))
    t_to = int(time.time()) // step * step - step
    t_from = t_to - round(float(rng.get("days") or 240) * 86400 / step) * step
    code = "\n".join(l.split("#", 1)[0] for l in str((spec.get("oracle") or {}).get("script") or "").split("\n"))
    used = [x for x in (spec.get("oracle") or {}).get("sources") or [] if x.get("kind") == "onchain" and x.get("name")
            and re.search(rf"(?<![A-Za-z0-9_.]){re.escape(x['name'])}(?![A-Za-z0-9_])", code)
            and re.match(r"^0x[0-9a-fA-F]{40}$", str(x.get("address") or "")) and x.get("sig")]
    try:
        agg_addr = (aggregator(chain).get("address") or "").lower() or None
    except Exception:
        agg_addr = None
    out = {"file": file, "chain": chain, "built_at": int(time.time()), "from": t_from, "to": t_to,
           "series": [], "ema": {}, "texts": {}, "names": {}, "routes": None, "extra": [], "live": None}

    def ident(x: dict) -> dict:
        return {"to": _addr(x["address"] if "address" in x else x["to"]), "sig": str(x["sig"]).replace(" ", ""),
                "args": list(x.get("args") or []), "slot": int(x.get("slot") or 0), "rtype": str(x.get("rtype") or "uint"),
                "decimals": 0 if x.get("raw") else int(x["decimals"] if x.get("decimals") is not None else 18)}
    for x in used:
        rd, own = ident(x), _addr(x["address"])
        try:
            tree = ema_tree(chain, own)
        except Exception:
            tree = None
        if tree:
            out["ema"][x["name"]] = {"detected": tree.get("suggested"), "mixed": tree.get("mixed"), "times": tree.get("ema_times"),
                                     "tree": [{k: n.get(k) for k in ("address", "type", "label", "ma", "depth", "refs")} for n in tree.get("nodes") or []]}
        pools = [str(n.get("address") or "").lower() for n in (tree or {}).get("nodes") or [] if n.get("type") == "pool"]
        watch = [own] if own in pools or not pools or len(pools) > 2 else list(dict.fromkeys([own] + pools))
        out["series"].append({"reading": rd, **_exact_series(chain, rd, bool(_HELD.match(rd["sig"])), watch, t_from, t_to)})
        key = json.dumps([chain, x["address"], x["sig"], x.get("args") or []], separators=(",", ":")).lower()   # = the page's JSON.stringify(...)
        out["texts"][key] = _reading_text(chain, x, agg_addr)
        try:
            nm = contract_abi(chain, own).get("name")
            if nm:
                out["names"][own] = nm
        except Exception:
            pass
    # the coin charts: routes, and the readings on them that no source already holds
    try:
        coll, debt = (spec.get("collateral") or {}).get("address"), (spec.get("borrowed") or {}).get("address")
        routes = coin_routes(chain, coll, debt)
        out["routes"] = routes
        have = {json.dumps(sr["reading"], sort_keys=True) for sr in out["series"]}
        for c in routes.get("coins") or []:
            for hop in c.get("hops") or []:
                for r in (hop.get("num"), hop.get("den"), hop.get("rate_num"), hop.get("rate_den")):
                    if not r:
                        continue
                    rd = ident(r)
                    k = json.dumps(rd, sort_keys=True)
                    if k in have:
                        continue
                    have.add(k)
                    out["extra"].append({"reading": rd, **_exact_series(chain, rd, bool(_HELD.match(rd["sig"])), [rd["to"]], t_from, t_to)})
    except Exception:
        pass                                           # no Curve pool behind the collateral: no coin charts
    try:
        lv = live_calls({"chain": chain, "calls": [{"id": x["name"], **{k: v for k, v in ident(x).items()}} for x in used]})
        out["live"] = lv
    except Exception:
        pass
    PACKS.mkdir(parents=True, exist_ok=True)
    f = PACKS / (file + ".gz")
    tmp = f.with_name(f"{f.name}.{time.time_ns()}.tmp")
    tmp.write_bytes(gzip.compress(json.dumps(out, separators=(",", ":")).encode(), 6))
    tmp.replace(f)
    return {"file": file, "points": sum(len(x["t"]) for x in out["series"] + out["extra"]), "bytes": f.stat().st_size}


def send_pack(handler, file: str, send_json) -> None:
    """The prepared file as it lies on disk (gzip), no RPC, no JSON round trip."""
    if file not in market_files():
        return send_json(handler, 404, {"error": "not a registered market"})
    f = PACKS / (file + ".gz")
    if not f.is_file():
        return send_json(handler, 404, {"error": "the history of this market is still being prepared", "building": True})
    body = f.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def ping() -> dict:
    if PUBLIC:
        return {"ok": True, "public": True}
    try:
        n = rpc_batch("ethereum", [("eth_blockNumber", [])])[0]
        hdr = _headers("ethereum", [max(1, int(n, 16) - 2_000_000)])[0]
        return {"ok": True, "public": False, "block": int(n, 16), "archive": hdr is not None}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


# ---- dispatch ----------------------------------------------------------------
def handle(handler, method: str, send_json) -> None:
    """Route one /nlapi/ request. `send_json(handler, status, body)` is the
    server's own JSON writer, so headers stay uniform with every other route."""
    u = urlparse(handler.path)
    name = u.path[len("/nlapi/"):].strip("/")
    q = parse_qs(u.query)
    if PUBLIC and name not in PUBLIC_ROUTES:
        return send_json(handler, 403, {"error": "this server does not read the chain: a market's history is prepared "
                                                 "ahead of time, and new markets come in by pull request"})
    try:
        if method == "GET":
            if name == "ping":
                return send_json(handler, 200, ping())
            if name == "token":
                return send_json(handler, 200, token_info(
                    q.get("chain", ["ethereum"])[0], q["address"][0]))
            if name == "pool":
                return send_json(handler, 200, pool_info(
                    q.get("chain", ["ethereum"])[0], q["address"][0]))
            if name == "curve_ohlc":
                return send_json(handler, 200, curve_ohlc(q))
            if name == "ema_tree":
                return send_json(handler, 200, ema_tree(
                    q.get("chain", ["ethereum"])[0], q["address"][0]))
            if name == "abi":
                return send_json(handler, 200, contract_abi(
                    q.get("chain", ["ethereum"])[0], q["address"][0]))
            if name == "aggregator":
                return send_json(handler, 200, aggregator(q.get("chain", ["ethereum"])[0]))
            if name == "pack":
                return send_pack(handler, str((q.get("market") or [""])[0]), send_json)
            if name == "coin_routes":
                return send_json(handler, 200, coin_routes(
                    q.get("chain", ["ethereum"])[0], q["pool"][0], q["quote"][0]))
        else:
            n = int(handler.headers.get("Content-Length", "0"))
            body = json.loads(handler.rfile.read(n).decode() or "{}")
            if name == "call":
                return send_json(handler, 200, live_calls(body))
            if name == "sample_exact":
                return send_json(handler, 200, sample_exact(body))
        send_json(handler, 404, {"error": f"unknown nlapi route {name!r}"})
    except KeyError as e:
        send_json(handler, 400, {"error": f"missing parameter {e}"})
    except Exception as e:
        send_json(handler, 400, {"error": str(e)[:300]})
