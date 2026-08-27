#!/usr/bin/env python3
"""fetch_markets.py — enumerate LlamaLend V1 + V2 markets and cache their
parameters for the UI's market selector.

  LLV1  one-way lending factory, Ethereum 0xeA6876…05E0  (`controllers(i)`)
  LLV2  factory, Ethereum 0x8f6b56…b0bd + Optimism 0x5f9407…3640
        (`markets(i)` -> [vault, controller, amm, collateral, borrowed,
                          price_oracle, monetary_policy])

Per market: collateral/borrowed token (symbol, decimals), AMM A + fee,
loan_discount, liquidation_discount, oracle price, total debt, n_loans.

All bulk reads go through Multicall3.aggregate3 with allowFailure=true — one
eth_call per ~MQ_CHUNK reads instead of one HTTP round-trip each, and
deterministic reverts (borrow_cap() on V1 markets, asset() on non-ERC4626
collateral) come back as success=false instead of triggering retry loops.

No auto-refresh by design — run this script to (re)write data/markets.json;
the UI serves it as-is via GET /markets and shows the fetched_at stamp.

    python3 fetch_markets.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
for _d in (HERE / "pylib", HERE / "sim"):
    sys.path.insert(0, str(_d))
from common import rpc_url, sel  # noqa: E402  (mainnet RPC + keccak selector)

OUT = HERE / "data" / "markets.json"

OP_RPCS = ["https://optimism.gateway.tenderly.co", "https://mainnet.optimism.io",
           "https://optimism-rpc.publicnode.com",
           "https://optimism.drpc.org", "https://1rpc.io/op"]

# Public RPC pools per chain. Fraxtal's providers 403 the default urllib
# User-Agent, hence the curl UA in Rpc.raw.
CHAIN_RPCS = {
    "optimism": OP_RPCS,
    "arbitrum": ["https://arbitrum.gateway.tenderly.co",
                 "https://arb1.arbitrum.io/rpc",
                 "https://arbitrum-one-rpc.publicnode.com",
                 "https://arbitrum.drpc.org", "https://1rpc.io/arb"],
    "fraxtal":  ["https://rpc.frax.com", "https://fraxtal-rpc.publicnode.com",
                 "https://fraxtal.drpc.org"],
    "sonic":    ["https://rpc.soniclabs.com", "https://sonic-rpc.publicnode.com",
                 "https://sonic.drpc.org"],
    "base":     ["https://mainnet.base.org", "https://base-rpc.publicnode.com",
                 "https://base.drpc.org", "https://base.gateway.tenderly.co"],
    "polygon":  ["https://polygon-bor-rpc.publicnode.com",
                 "https://polygon-rpc.com", "https://polygon.drpc.org"],
    "avalanche": ["https://avalanche-c-chain-rpc.publicnode.com",
                  "https://api.avax.network/ext/bc/C/rpc"],
    "fantom":   ["https://rpcapi.fantom.network", "https://fantom.drpc.org",
                 "https://fantom-rpc.publicnode.com"],
    "xdai":     ["https://gnosis-rpc.publicnode.com", "https://rpc.gnosischain.com"],
    "bsc":      ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org"],
    "celo":     ["https://forno.celo.org", "https://celo-rpc.publicnode.com"],
    "kava":     ["https://evm.kava.io", "https://kava-evm-rpc.publicnode.com"],
    "x-layer":  ["https://rpc.xlayer.tech", "https://xlayerrpc.okx.com"],
    "hyperliquid": ["https://rpc.hyperliquid.xyz/evm"],
}

# Dwellir per-chain endpoints (verified against the account 2026-08-27):
# prepended when the mainnet RPC is a Dwellir URL, reusing its key.
_DWELLIR_SLUGS = {
    "arbitrum": "api-arbitrum-mainnet-archive",
    "optimism": "api-optimism-mainnet-archive",
    "base":     "api-base-mainnet-archive",
    "polygon":  "api-polygon-mainnet-full",
    "xdai":     "api-gnosis-mainnet",
    "celo":     "api-celo-mainnet-archive",
}
try:
    _u = rpc_url()
    if ".dwellir.com" in _u:
        _key = _u.rstrip("/").rsplit("/", 1)[-1]
        for _ch, _slug in _DWELLIR_SLUGS.items():
            CHAIN_RPCS.setdefault(_ch, []).insert(
                0, f"https://{_slug}.n.dwellir.com/{_key}")
except Exception:
    pass

# any WEB3_HTTP_<CHAIN> in the env/.env becomes that chain's first
# provider (e.g. WEB3_HTTP_AVALANCHE, WEB3_HTTP_BSC, WEB3_HTTP_HYPERLIQUID)
def _env_chain_rpcs() -> None:
    lines = []
    envf = HERE / ".env"
    if envf.exists():
        lines += envf.read_text().splitlines()
    lines += [f"{k}={v}" for k, v in os.environ.items()
              if k.startswith("WEB3_HTTP_")]
    for line in lines:
        if not line.startswith("WEB3_HTTP_") or "=" not in line:
            continue
        name, url = line.split("=", 1)
        ch = name[len("WEB3_HTTP_"):].lower().replace("_", "-")
        url = url.strip()
        if ch in ("mainnet", "ethereum") or not url:
            continue
        lst = CHAIN_RPCS.setdefault(ch, [])
        if url not in lst:
            lst.insert(0, url)
_env_chain_rpcs()

FACTORIES = [
    # (group, chain, factory, kind)
    ("LLV1", "ethereum", "0xeA6876DDE9e3467564acBeE1Ed5bac88783205E0", "v1"),
    ("LLV2", "ethereum", "0x8f6b56ec5ddf1f2691a1059f1d3cd97ac9eab0bd", "v2"),
    ("LLV2", "optimism", "0x5f94073e3f51c1fff92ffc6b4b06b7af193b3640", "v2"),
]

# LLV1 chains with no locally-known factory address: enumerate from the Curve
# lending API instead (registryId "oneway"). Verified against the Ethereum
# factory scan: the API census matches it 1:1, so it is a complete list, dead
# markets included. LLV2 ("oneway-v2") is factory-scanned above.
API_LLV1_CHAINS = ["optimism", "arbitrum", "fraxtal", "sonic"]
LEND_VAULTS_API = "https://api.curve.finance/v1/getLendingVaults/all"

# Multicall3 — same address on Ethereum mainnet and Optimism.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_AGG3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])
MQ_CHUNK = 200           # inner calls per eth_call (well under provider gas caps)


# ------------------------------------------------------- return-data decoders
def _num(r: str | None) -> int | None:
    return int(r[2:66], 16) if r else None


def _addr(r: str | None, word: int = 0) -> str | None:
    return "0x" + r[2:][word * 64:(word + 1) * 64][24:] if r else None


def _string(r: str | None) -> str | None:
    """symbol()/name(): dynamic string or bytes32-packed (MKR-style)."""
    if not r:
        return None
    b = bytes.fromhex(r[2:])
    if len(b) >= 64:
        try:
            off = int.from_bytes(b[:32], "big")
            ln = int.from_bytes(b[off:off + 32], "big")
            if 0 < ln <= 64:
                return b[off + 32:off + 32 + ln].decode(errors="replace")
        except Exception:
            pass
    return b.rstrip(b"\0").decode(errors="replace") or None


# --------------------------------------------------- aggregate3 ABI plumbing
def enc_aggregate3(calls: list[tuple[str, str]]) -> str:
    """calls: (to, calldata_hex) pairs -> aggregate3 calldata, allowFailure=1."""
    elems = []
    for to, data in calls:
        d = bytes.fromhex(data[2:])
        pad = b"\0" * ((32 - len(d) % 32) % 32)
        elems.append(int(to, 16).to_bytes(32, "big")
                     + (1).to_bytes(32, "big")           # allowFailure
                     + (0x60).to_bytes(32, "big")        # bytes offset in tuple
                     + len(d).to_bytes(32, "big") + d + pad)
    offs, cum = [], 32 * len(calls)
    for e in elems:
        offs.append(cum)
        cum += len(e)
    return SEL_AGG3 + ((0x20).to_bytes(32, "big")
                       + len(calls).to_bytes(32, "big")
                       + b"".join(o.to_bytes(32, "big") for o in offs)
                       + b"".join(elems)).hex()


def dec_aggregate3(ret_hex: str) -> list[tuple[bool, bytes]]:
    """aggregate3 return -> [(success, returnData)] in call order."""
    b = bytes.fromhex(ret_hex[2:])
    arr = b[int.from_bytes(b[:32], "big"):]
    n = int.from_bytes(arr[:32], "big")
    da = arr[32:]
    out = []
    for i in range(n):
        el = da[int.from_bytes(da[i * 32:(i + 1) * 32], "big"):]
        boff = int.from_bytes(el[32:64], "big")
        blen = int.from_bytes(el[boff:boff + 32], "big")
        out.append((int.from_bytes(el[:32], "big") == 1,
                    el[boff + 32:boff + 32 + blen]))
    return out


class Rpc:
    def __init__(self, chain: str):
        self.urls = ([rpc_url()] if chain == "ethereum"
                     else list(CHAIN_RPCS.get(chain, OP_RPCS)))

    def raw(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params, "id": 1}).encode()
        last = None
        for url in self.urls:
            for _ in range(3):
                try:
                    req = urllib.request.Request(
                        url, data=body, headers={"Content-Type": "application/json",
                                                 "User-Agent": "curl/8.4.0"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        resp = json.loads(r.read())
                    if "error" in resp:
                        raise RuntimeError(resp["error"])
                    self.urls = [url] + [u for u in self.urls if u != url]
                    return resp["result"]
                except Exception as e:
                    last = e
                    time.sleep(0.3)
        raise last

    def call(self, to: str, data: str, block: str = "latest"):
        return self.raw("eth_call", [{"to": to, "data": data}, block])

    def head(self) -> tuple[int, int]:
        """(block number, timestamp) of the chain head."""
        n = int(self.raw("eth_blockNumber", []), 16)
        b = self.raw("eth_getBlockByNumber", [hex(n), False])
        return n, int(b["timestamp"], 16)

    def q(self, to: str, fn: str, arg: int | None = None):
        data = sel(fn) + (hex(arg)[2:].rjust(64, "0") if arg is not None else "")
        try:
            r = self.call(to, data)
            return None if r in ("0x", None) else r
        except Exception:
            return None

    def num(self, to, fn, arg=None):
        return _num(self.q(to, fn, arg))

    def addr(self, to, fn, arg=None, word=0):
        return _addr(self.q(to, fn, arg), word)

    def string(self, to, fn):
        return _string(self.q(to, fn))

    def mq(self, calls: list[tuple[str, str, int | None]],
           block: str = "latest") -> list[str | None]:
        """Batched eth_calls via Multicall3. calls: (to, fn, arg) triples;
        returns hex-or-None per call, order preserved (None = reverted/empty,
        same semantics as q()). A failing chunk is bisected so one bad call
        cannot sink the whole batch."""
        enc = [(to, sel(fn) + (hex(arg)[2:].rjust(64, "0")
                               if arg is not None else ""))
               for to, fn, arg in calls]
        out: list[str | None] = []
        for i in range(0, len(enc), MQ_CHUNK):
            out += self._mq_chunk(enc[i:i + MQ_CHUNK], block)
        return out

    def _mq_chunk(self, chunk, block="latest"):
        if not chunk:
            return []
        try:
            ret = self.call(MULTICALL3, enc_aggregate3(chunk), block)
        except Exception:
            if len(chunk) == 1:
                return [None]
            mid = len(chunk) // 2
            return self._mq_chunk(chunk[:mid], block) + \
                self._mq_chunk(chunk[mid:], block)
        if not ret:
            return [None] * len(chunk)
        return [("0x" + d.hex()) if ok and d else None
                for ok, d in dec_aggregate3(ret)]


POOLS_API = "https://api.curve.finance/v1/getPools/all/{chain}"


def fetch_pools(chain: str) -> list[dict]:
    """All Curve pools on `chain` from the prices/pools API (same source
    curvemonitor uses)."""
    url = POOLS_API.format(chain=chain)
    req = urllib.request.Request(url, headers={"User-Agent": "bad-debt-sim"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    return d["data"]["poolData"]


def usd_prices(pools: list[dict]) -> dict[str, float]:
    """address -> USD price, from the coin metadata the pools API already
    returns. The sim's single unit is the DOLLAR, so every token amount that
    reaches it (debt denominated in WETH, a price quoted in tBTC) has to be
    multiplied by one of these first. Deepest pool wins when a token appears in
    several, since a dust pool's quoted price is the least trustworthy."""
    best: dict[str, tuple[float, float]] = {}
    for p in pools:
        tvl = float(p.get("usdTotal") or 0)
        for c in p.get("coins") or []:
            a = (c.get("address") or "").lower()
            try:
                px = float(c["usdPrice"])
            except (TypeError, ValueError, KeyError):
                continue
            if not a or px <= 0:
                continue
            if a not in best or tvl > best[a][1]:
                best[a] = (px, tvl)
    return {a: v[0] for a, v in best.items()}


# registryId -> (pool_type in our venue engines, n_coins rule, ANN divisor)
# Divisor turns the API's amplificationCoefficient into our A field:
#   stableswap*: A() is raw (divisor 1); cryptoswap: A_raw = ANN / (N^N · 10⁴).
_REG = {
    "main": ("stableswap", 1), "factory": ("stableswap", 1),
    "factory-stable-ng": ("stableswap-ng", 1), "factory-crvusd": ("stableswap-ng", 1),
    "crypto": ("cryptoswap", 40_000), "factory-crypto": ("cryptoswap", 40_000),
    "factory-twocrypto": ("cryptoswap", 40_000),
    "factory-tricrypto": ("cryptoswap", 270_000),
}


def best_venue(pools: list[dict], chain: str, token: str,
               borrowed: str | None = None) -> dict | None:
    """Biggest pair-TVL Curve pool holding `token`. Pools that ALSO hold the
    borrowed token are preferred (a like-kind pool — cvxCRV/CRV — cannot exit
    a liquidation to the borrowed side); if none holds both, biggest overall.
    Pair-TVL = the token's USD balance + the largest other coin's USD balance
    (matches the sim's pair-only venue semantics)."""
    token = token.lower()
    if borrowed:
        both = _best_venue_scan(pools, chain, token, borrowed.lower())
        if both:
            return both
    return _best_venue_scan(pools, chain, token, None)


def _best_venue_scan(pools: list[dict], chain: str, token: str,
                     must_hold: str | None) -> dict | None:
    best = None
    for p in pools:
        if p.get("isBroken"):
            continue
        reg = p.get("registryId")
        if reg not in _REG:
            continue
        coins = p.get("coins") or []
        addrs = [(c.get("address") or "").lower() for c in coins]
        if token not in addrs:
            continue
        if must_hold is not None and must_hold not in addrs:
            continue
        usd = []
        for c in coins:
            try:
                usd.append(float(c["poolBalance"]) / 10 ** int(c["decimals"])
                           * float(c["usdPrice"]))
            except (TypeError, ValueError, KeyError):
                usd.append(0.0)
        ti = addrs.index(token)
        others = [u for k, u in enumerate(usd) if k != ti]
        pair_tvl = usd[ti] + (max(others) if others else 0.0)
        if pair_tvl <= 0:
            continue
        # Quote side of the pair (candles/vol are measured in it): the borrowed
        # token when the pool holds it, else the biggest-USD other coin.
        if must_hold is not None:
            qi = addrs.index(must_hold)
        else:
            qi = max((k for k in range(len(coins)) if k != ti),
                     key=lambda k: usd[k], default=None)
        if qi is None:
            continue
        ptype, div = _REG[reg]
        n_coins = 3 if reg == "factory-tricrypto" else 2
        try:
            ann = float(p.get("amplificationCoefficient"))
        except (TypeError, ValueError):
            continue
        v = {
            "pool": p["address"],
            "name": p.get("name") or "/".join(c.get("symbol", "?") for c in coins),
            "coins": [c.get("symbol", "?") for c in coins],
            "base_addr": token,          # collateral (or its ERC4626 underlying)
            "quote_addr": addrs[qi],
            "quote_symbol": coins[qi].get("symbol", "?"),
            "pair_tvl_usd": round(pair_tvl),
            "usd_total": round(p.get("usdTotal") or 0),
            "pool_type": ptype,
            "n_coins": n_coins,
            "A": round(ann / div, 3) if div > 1 else round(ann),
            "ann": round(ann),
            "registry": reg,
            "monitor_url": f"https://curvemonitor.com/platform/pools/{chain}/{p['address']}",
        }
        if best is None or v["pair_tvl_usd"] > best["pair_tvl_usd"]:
            best = v
    return best


def load_tokens(rpc: Rpc, addrs: list[str], cache: dict) -> None:
    """symbol() + decimals() for every address not yet cached — one multicall."""
    todo = [a for a in dict.fromkeys(addrs) if a not in cache]
    if not todo:
        return
    res = rpc.mq([(a, f, None) for a in todo for f in ("symbol()", "decimals()")])
    for k, a in enumerate(todo):
        cache[a] = {"addr": a, "symbol": _string(res[2 * k]) or a[:8],
                    "decimals": _num(res[2 * k + 1]) or 18}


CTRL_FNS = ("loan_discount()", "liquidation_discount()", "total_debt()",
            "n_loans()", "borrow_cap()", "admin_percentage()",
            "admin_fees()")  # borrow_cap/admin_*: V2-only, revert on V1
# rate()/get_rate_mul(): the sim used to inherit the CRV market's 13% APR and
# 0.6% AMM fee for every market because the reference snapshot supplies them.
AMM_FNS = ("A()", "fee()", "price_oracle()", "rate()", "get_rate_mul()")


def read_markets_bulk(rpc: Rpc, chain: str, raw: list[tuple], toks: dict,
                      usd: dict[str, float] | None = None
                      ) -> list[tuple[int, dict | None]]:
    """raw: (i, controller, amm, collateral, borrowed) per market. One
    multicall for all market fields + one for unseen token metadata.
    Returns (i, market-dict-or-None) preserving factory order."""
    ok = [r for r in raw
          if all(x and int(x, 16) for x in r[1:])]
    load_tokens(rpc, [t for r in ok for t in r[3:5]], toks)
    calls = []
    for _, c, a, coll, borrowed in ok:
        calls += [(c, f, None) for f in CTRL_FNS]
        calls += [(a, f, None) for f in AMM_FNS]
        # The market's own TVL: collateral deposited in its AMM. Nothing else in
        # this file measures market SIZE — total_debt is what is borrowed
        # against it, and venue TVL belongs to the liquidation pool, not here.
        calls.append((coll, "balanceOf(address)", int(a, 16)))
        # Unborrowed lender liquidity sits in the Controller (on-chain
        # max_borrowable clamps to BORROWED_TOKEN.balanceOf(self) + debt) ->
        # utilization = debt / (debt + available).
        calls.append((borrowed, "balanceOf(address)", int(c, 16)))
    res = rpc.mq(calls)
    W = len(CTRL_FNS) + len(AMM_FNS) + 2
    by_i: dict[int, dict | None] = {}
    usd = usd or {}
    for k, (i, c, a, coll, borrowed) in enumerate(ok):
        (ld, liq, debt, n_loans, cap, admin_pct, admin_acc, A, fee,
         price, rate, rate_mul,
         coll_bal, avail_bal) = (_num(res[k * W + j]) for j in range(W))
        if ld is None or A is None:
            by_i[i] = None
            continue
        ct, bt = toks[coll], toks[borrowed]
        # Everything downstream is priced in DOLLARS. price_oracle() is the
        # collateral quoted in the BORROWED token, so a market that borrows WETH
        # or tBTC needs that leg's own dollar price before any of it means
        # anything (824 WETH is not $824). borrowed_usd None => the market is
        # unsimulatable, and the UI must say so rather than silently assume 1.
        b_usd = usd.get(borrowed.lower())
        px = round(price / 1e18, 12) if price is not None else None
        by_i[i] = {
            "chain": chain,
            "controller": c,
            "amm": a,
            "collateral": ct,
            "borrowed": bt,
            "llamma_A": A,
            "loan_discount_pct": round(ld / 1e16, 4),
            "liquidation_discount_pct": round(liq / 1e16, 4) if liq is not None else None,
            "amm_fee_pct": round(fee / 1e16, 4) if fee is not None else None,
            "amm_fee_wei": fee,
            "amm_rate_wei": rate,             # per-second, 1e18 (AMM.rate())
            "amm_rate_mul_wei": (str(rate_mul) if rate_mul is not None else None),
            "amm_rate_apr_pct": (round(rate * 31_536_000 / 1e16, 4)
                                 if rate is not None else None),
            "oracle_price": px,               # collateral quoted in BORROWED
            "borrowed_usd": b_usd,            # USD per borrowed token
            "collateral_usd_price": (round(px * b_usd, 12)
                                     if (px is not None and b_usd) else None),
            "total_debt": round(debt / 10 ** bt["decimals"]) if debt is not None else None,
            "total_debt_usd": (round(debt / 10 ** bt["decimals"] * b_usd)
                               if (debt is not None and b_usd) else None),
            "borrow_cap": round(cap / 10 ** bt["decimals"]) if cap is not None else None,
            "borrow_cap_usd": (round(cap / 10 ** bt["decimals"] * b_usd)
                               if (cap is not None and b_usd) else None),
            "n_loans": n_loans,
            # LLV2 only: DAO's share of the borrow interest + what has
            # accrued so far (both revert -> None on V1)
            "admin_fee_share_pct": (round(admin_pct / 1e16, 4)
                                    if admin_pct is not None else None),
            "admin_fees_accrued": (admin_acc / 10 ** bt["decimals"]
                                   if admin_acc is not None else None),
            "available_borrowed": (avail_bal / 10 ** bt["decimals"]
                                   if avail_bal is not None else None),
            "available_usd": (round(avail_bal / 10 ** bt["decimals"] * b_usd)
                              if (avail_bal is not None and b_usd) else None),
            "utilization_pct": (round(debt / (debt + avail_bal) * 100, 2)
                                if (debt is not None and avail_bal is not None
                                    and debt + avail_bal > 0) else None),
            "collateral_tokens": (coll_bal / 10 ** ct["decimals"]
                                  if coll_bal is not None else None),
            "market_tvl_usd": (round(coll_bal / 10 ** ct["decimals"] * px * b_usd)
                               if (coll_bal is not None and px is not None and b_usd)
                               else None),
        }
    return [(r[0], by_i.get(r[0])) for r in raw]


def attach_venues_bulk(rpc: Rpc, pools: list[dict], chain: str,
                       ms: list[dict], toks: dict) -> None:
    """Pick each market's liquidation venue: biggest pair-TVL pool holding the
    collateral; if none exists (svZCHF), unwrap one ERC4626 layer via asset()
    and search for the UNDERLYING instead (svZCHF -> ZCHF redemption).
    The asset() probes for all venue-less markets go out as one multicall."""
    miss = []
    for m in ms:
        v = best_venue(pools, chain, m["collateral"]["addr"], m["borrowed"]["addr"])
        if v:
            v["via_redemption"] = None
        m["venue"] = v
        if v is None:
            miss.append(m)
    if not miss:
        return
    ares = rpc.mq([(m["collateral"]["addr"], "asset()", None) for m in miss])
    unders = {}
    for m, r in zip(miss, ares):
        u = _addr(r)
        if u and int(u, 16):
            unders[m["controller"]] = u
    load_tokens(rpc, list(unders.values()), toks)
    for m in miss:
        u = unders.get(m["controller"])
        if not u:
            continue
        v = best_venue(pools, chain, u, m["borrowed"]["addr"])
        if v:
            # The pool prices the UNDERLYING; the collateral is the vault share
            # on top of it, so keep the wrapper's identity and decimals — its
            # USD price is the underlying's times the ERC4626 redemption rate.
            v["via_redemption"] = toks[u]["symbol"]
            v["wrapper"] = dict(m["collateral"])
            v["base_decimals"] = toks[u]["decimals"]
            m["venue"] = v
    # the venue pool's oracle time constant (ma_exp_time on stableswap-ng,
    # ma_time on cryptoswap — both T/ln2, so half-life = value * ln 2)
    vs = [m["venue"] for m in ms if m.get("venue")]
    for fn, half in (("ma_exp_time()", False), ("ma_time()", False),
                     ("ma_half_time()", True)):   # cryptoswap v1: half-life
        todo = [v for v in vs if "ma_exp_time" not in v]
        if not todo:
            break
        res = rpc.mq([(v["pool"], fn, None) for v in todo])
        for v, r in zip(todo, res):
            n = _num(r)
            if n and 0 < n < 10 ** 7:
                v["ma_exp_time"] = round(n / 0.6931471805599453) if half                     else n


def _words(r: str | None) -> list[int]:
    """Decode a dynamic uint256[] return into python ints."""
    if not r:
        return []
    b = bytes.fromhex(r[2:])
    if len(b) < 64:
        return []
    arr = b[int.from_bytes(b[:32], "big"):]
    n = int.from_bytes(arr[:32], "big")
    if n > 8:                                   # not an array we understand
        return []
    return [int.from_bytes(arr[32 + 32 * k:64 + 32 * k], "big") for k in range(n)]


# Per-family getters. The venue used to be reconstructed as a PERFECTLY BALANCED
# pool from pair TVL alone, which quoted a $2M sale of sreUSD at -0.28% where the
# real, lopsided pool charges -18.84%. These reads give the sim the actual
# balances, rates/price_scale, invariant and fee parameters instead.
SS_STATE_FNS = ("A()", "fee()", "offpeg_fee_multiplier()", "stored_rates()")
CS_STATE_FNS = ("A()", "gamma()", "D()", "mid_fee()", "out_fee()", "fee_gamma()")


def attach_venue_state(rpc: Rpc, ms: list[dict], toks: dict,
                       usd: dict[str, float] | None = None) -> None:
    """Read each venue pool's live state. Stored under venue["state"]; None when
    a pool does not expose what its family needs, and the caller then falls back
    to the balanced approximation."""
    # One read per (pool, base, quote): several markets share a pool (both wstETH
    # markets -> TricryptoLLAMA) but each holds its own venue dict, and the base/
    # quote indices differ per market, so the state has to be fanned back out to
    # every one of them rather than written onto the first only.
    vs, by_key = [], {}
    for m in ms:
        v = m.get("venue")
        if not v or not v.get("pool"):
            continue
        k = (v["pool"], (v.get("base_addr") or "").lower(),
             (v.get("quote_addr") or "").lower())
        by_key.setdefault(k, []).append(v)
        if len(by_key[k]) == 1:
            vs.append(v)
    if not vs:
        return
    # coins(i) + balances(i) for up to n_coins, plus the family scalars.
    # Stableswap-NG pools are not always pairs (the registry gives no coin
    # count), so probe up to MAXC slots and keep the ones that resolve; the
    # cryptoswap families have a fixed count from their registry id.
    MAXC = 4
    calls = []
    for v in vs:
        n = MAXC if v["pool_type"].startswith("stableswap") else int(v.get("n_coins") or 2)
        v["_probe_n"] = n
        calls += [(v["pool"], "coins(uint256)", i) for i in range(n)]
        calls += [(v["pool"], "balances(uint256)", i) for i in range(n)]
        if v["pool_type"].startswith("stableswap"):
            calls += [(v["pool"], f, None) for f in SS_STATE_FNS]
        else:
            calls += [(v["pool"], f, None) for f in CS_STATE_FNS]
            # twocrypto exposes price_scale(), tricrypto price_scale(i)
            calls += [(v["pool"], "price_scale()", None)]
            calls += [(v["pool"], "price_scale(uint256)", i) for i in range(n - 1)]
    res = rpc.mq(calls)
    p = 0
    new_tokens = []
    for v in vs:
        n = v.pop("_probe_n")
        coins = [_addr(res[p + i]) for i in range(n)]; p += n
        bal = [_num(res[p + i]) for i in range(n)]; p += n
        live = [k for k in range(n) if coins[k] and int(coins[k], 16)]
        coins = [coins[k] for k in live]
        bal = [bal[k] for k in live]
        st: dict | None = None
        # EVERY wei-scale field is stored as a STRING. This state travels to the
        # engine via the browser, and JavaScript has no integers: JSON.parse
        # turns price_scale 64397369689360346674035 into a double and hands back
        # "6.438123661042281e+22", which the engine cannot parse and which has
        # already lost seven digits. Anything above 2^53 must never be a JSON
        # number here.
        S = lambda x: None if x is None else str(x)          # noqa: E731
        if v["pool_type"].startswith("stableswap"):
            A, fee, opm, rates_r = (res[p + i] for i in range(len(SS_STATE_FNS)))
            p += len(SS_STATE_FNS)
            st = {"kind": "stableswap-ng" if v["pool_type"] == "stableswap-ng"
                          else "stableswap",
                  "A": _num(A), "fee": _num(fee),
                  "offpeg": _num(opm),
                  "rates": [str(r) for r in _words(rates_r)]}
        else:
            A, gamma, D, mid, out, fg = (res[p + i] for i in range(len(CS_STATE_FNS)))
            p += len(CS_STATE_FNS)
            ps1 = _num(res[p]); p += 1
            psn = [_num(res[p + i]) for i in range(n - 1)]; p += n - 1
            scale = [x for x in psn if x] or ([ps1] if ps1 else [])
            st = {"kind": "cryptoswap", "A": _num(A), "gamma": S(_num(gamma)),
                  "D": S(_num(D)), "mid_fee": _num(mid), "out_fee": _num(out),
                  "fee_gamma": S(_num(fg)),
                  "price_scale": [str(x) for x in scale]}
        coins_l = [(c or "").lower() for c in coins]
        if len(coins_l) < 2 or None in bal or st.get("A") is None:
            v["state"] = None
            continue
        st["coins"] = coins_l
        st["n"] = len(coins_l)
        st["balances"] = [str(b) for b in bal]
        try:
            st["i_base"] = coins_l.index(v["base_addr"].lower())
            st["i_quote"] = coins_l.index(v["quote_addr"].lower())
        except ValueError:
            v["state"] = None
            continue
        new_tokens += coins_l
        v["state"] = st
    load_tokens(rpc, new_tokens, toks)
    usd = usd or {}
    for v in vs:
        st = v.get("state")
        if st:
            st["decimals"] = [toks[c]["decimals"] for c in st["coins"]]
            # Which 2-coin invariant does this pool actually run? The Yield
            # Basis pools present a twocrypto interface but run StableswapMath
            # inside, and solving them with the twocrypto invariant put the
            # WETH venue's marginal at $1,133 against a $1,862 oracle. Decided
            # by which D reproduces the pool's own stored D().
            if st["kind"] == "cryptoswap" and st["n"] == 2 and st.get("D"):
                import venues as _v
                xp = [int(st["balances"][0]) * 10 ** (18 - st["decimals"][0]),
                      int(st["balances"][1]) * 10 ** (18 - st["decimals"][1])
                      * int(st["price_scale"][0]) // 10 ** 18]
                kind, err = _v.detect_c2_math(xp, int(st["D"]), int(st["A"]),
                                              int(st["gamma"]))
                st["c2_math"] = kind
                st["c2_math_err"] = (round(err, 8) if err is not None else None)
            # Per-coin USD price: the venue is rebuilt in DOLLAR space, so the
            # real balances have to be valued before the invariant is solved.
            st["usd"] = [usd.get(c) for c in st["coins"]]
            if not st["usd"][st["i_quote"]]:
                st = v["state"] = None       # cannot value the proceeds leg
        k = (v["pool"], (v.get("base_addr") or "").lower(),
             (v.get("quote_addr") or "").lower())
        for twin in by_key.get(k, ()):
            twin["state"] = st


def fetch_api_vaults() -> list[dict]:
    """All lending vaults from the Curve API (both registries, all chains)."""
    req = urllib.request.Request(LEND_VAULTS_API,
                                 headers={"User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read())
    return j.get("data", {}).get("lendingVaultData", [])


def main() -> None:
    groups: dict[str, list] = {"LLV1": [], "LLV2": []}
    tok_cache_by_chain: dict[str, dict] = {}
    pools_by_chain: dict[str, list] = {}
    usd_by_chain: dict[str, dict] = {}
    rpc_by_chain: dict[str, Rpc] = {}
    api_vaults = fetch_api_vaults()
    # (group, chain, factory-or-None, kind) — API sources carry their raw rows.
    sources: list[tuple] = list(FACTORIES)
    for chain in API_LLV1_CHAINS:
        vs = [v for v in api_vaults
              if v.get("blockchainId") == chain and v.get("registryId") == "oneway"]
        if vs:
            sources.append(("LLV1", chain, None, "api", vs))
    for src in sources:
        group, chain, factory, kind = src[0], src[1], src[2], src[3]
        rpc = rpc_by_chain.setdefault(chain, Rpc(chain))
        toks = tok_cache_by_chain.setdefault(chain, {})
        if chain not in pools_by_chain:
            pools_by_chain[chain] = fetch_pools(chain)
            usd_by_chain[chain] = usd_prices(pools_by_chain[chain])
            print(f"pools API {chain}: {len(pools_by_chain[chain])} pools, "
                  f"{len(usd_by_chain[chain])} token USD prices")
        if kind == "api":
            vs = src[4]
            raw = [(i, v["controllerAddress"].lower(), v["ammAddress"].lower(),
                    v["assets"]["collateral"]["address"].lower(),
                    v["assets"]["borrowed"]["address"].lower())
                   for i, v in enumerate(vs)]
            print(f"{group} {chain} (curve API): {len(raw)} markets")
        else:
            n = rpc.num(factory, "market_count()")
            if n is None:
                print(f"!! {group} {chain} {factory}: market_count() failed, skipped")
                continue
            print(f"{group} {chain} {factory}: {n} markets")
            if kind == "v1":
                rows = rpc.mq([(factory, "controllers(uint256)", i) for i in range(n)])
                ctrls = [(i, _addr(r)) for i, r in enumerate(rows)]
                ctrls = [(i, c) for i, c in ctrls if c and int(c, 16)]
                trip = rpc.mq([(c, f, None) for _, c in ctrls
                               for f in ("amm()", "collateral_token()",
                                         "borrowed_token()")])
                raw = [(i, c, _addr(trip[3 * k]), _addr(trip[3 * k + 1]),
                        _addr(trip[3 * k + 2])) for k, (i, c) in enumerate(ctrls)]
            else:
                rows = rpc.mq([(factory, "markets(uint256)", i) for i in range(n)])
                raw = []
                for i, r in enumerate(rows):
                    if not r:
                        continue
                    w = ["0x" + r[2:][k * 64:(k + 1) * 64][24:] for k in range(7)]
                    raw.append((i, w[1], w[2], w[3], w[4]))
        pairs = read_markets_bulk(rpc, chain, raw, toks, usd_by_chain[chain])
        ctrl_by_i = {r[0]: r[1] for r in raw}
        live = [m for _, m in pairs if m]
        attach_venues_bulk(rpc, pools_by_chain[chain], chain, live, toks)
        attach_venue_state(rpc, live, toks, usd_by_chain[chain])
        for i, m in pairs:
            if m is None:
                print(f"   {i}: {ctrl_by_i.get(i)} unreadable, skipped")
                continue
            groups[group].append(m)
            v = m["venue"]
            vs = (f"venue {v['name']} ${v['pair_tvl_usd']:,} {v['pool_type']}"
                  + (f" via {v['via_redemption']}" if v.get("via_redemption") else "")
                  ) if v else "venue NONE"
            cp = m.get("collateral_usd_price")
            print(f"   {i}: {m['collateral']['symbol']}/{m['borrowed']['symbol']}"
                  f"  A={m['llamma_A']} ld={m['loan_discount_pct']}%"
                  f" liq={m['liquidation_discount_pct']}%"
                  f" fee={m['amm_fee_pct']}% rate={m['amm_rate_apr_pct']}%"
                  f" px={m['oracle_price']}"
                  f" collUSD={('$%.6g' % cp) if cp else 'NO USD PRICE'}"
                  f" debt=${(m.get('total_debt_usd') or 0):,}"
                  f" tvl=${(m.get('market_tvl_usd') or 0):,} | {vs}"
                  + ("" if (v and v.get("state")) else "  [no pool state]"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "fetched_at": int(time.time()),
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "groups": groups,
    }, indent=1))
    os.replace(tmp, OUT)  # atomic: the UI's /markets never sees a partial file
    print(f"wrote {OUT}  (LLV1 {len(groups['LLV1'])}, LLV2 {len(groups['LLV2'])})")


if __name__ == "__main__":
    main()
