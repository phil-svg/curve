#!/usr/bin/env python3
"""fetch_oracles.py — walk every market's price-oracle graph and cache it.

For each market in data/markets.json (all groups, all chains):
  AMM.price_oracle_contract() is the root. From there every referenced
  contract is probed with one multicall battery and classified:

    chainlink   latestRoundData() + description(); proxies also aggregator()
    pool        a Curve pool: coins(0) + A(); ma param depends on family —
                ma_exp_time (stableswap-ng), ma_time (two/tricrypto-ng),
                ma_half_time (legacy crypto)
    vault       ERC4626: asset() + convertToAssets — a redemption rate,
                no EMA of its own
    agg         crvUSD AggregatorStablePrice: sigma() + price_pairs(i),
                EMA via TVL-weighting across pools (exp_time)
    wrapper     an oracle contract that quotes price()/price_w() and points
                at other nodes (may add its own EMA on top)
    unknown     has code but matched nothing above

  Every EMA parameter found at ANY layer is recorded (ma_exp_time, ma_time,
  ma_half_time, exp_time — seconds). References probed: POOL/pool, pools(i),
  price_pairs(i), AGG/agg, aggregator, feed/FEED, CHAINLINK_AGGREGATOR,
  vault/VAULT, asset, underlying, oracle/ORACLE, staked_oracle, CORE_ORACLE,
  stableswap/STABLESWAP, tricrypto/TRICRYPTO, POOL_A/POOL_B, chained
  price_oracle_contract.

Nodes are cached per (chain, address) — many markets share the crvUSD agg —
and the walk is capped at depth 4 / 24 nodes per market.

Output: data/oracles.json
  { fetched_at, markets: { "<chain>:<controller>": {
      root, amm_ma: {...}, nodes: { "<addr>": {type, label, ma: {...},
      refs: {getter: addr}, meta: {...}} } } } }

    python3 fetch_oracles.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "pylib"))
from fetch_markets import Rpc, _num, _addr, _string  # noqa: E402
from common import sel  # noqa: E402

MARKETS = HERE / "data" / "markets.json"
OUT = HERE / "data" / "oracles.json"
TITLES = HERE / "data" / "contract_titles.json"

# verified-source @title lookup (label-less wrappers/aggs get a real name)
BLOCKSCOUT = {"ethereum": "https://eth.blockscout.com",
              "optimism": "https://optimism.blockscout.com",
              "arbitrum": "https://arbitrum.blockscout.com",
              "fraxtal": "https://fraxtal.blockscout.com",
              "sonic": "https://sonic.blockscout.com"}


def contract_title(chain: str, addr: str, cache: dict) -> str | None:
    """The @title line of the verified source (Blockscout), ContractName
    as fallback — cached forever in data/contract_titles.json ('' = miss,
    also cached: unverified contracts stay unverified)."""
    key = f"{chain}:{addr}"
    if key in cache:
        return cache[key] or None
    title = ""
    base = BLOCKSCOUT.get(chain)
    if base:
        import re
        import urllib.request
        try:
            req = urllib.request.Request(
                f"{base}/api?module=contract&action=getsourcecode"
                f"&address={addr}", headers={"User-Agent": "curve-sim"})
            with urllib.request.urlopen(req, timeout=12) as r:
                d = json.loads(r.read())
            r0 = (d.get("result") or [{}])[0]
            src = r0.get("SourceCode") or ""
            mt = re.search(r"@title\s+(.+)", src)
            title = ((mt.group(1) if mt else "")
                     or r0.get("ContractName") or "").strip()[:60]
        except Exception:
            title = ""
        time.sleep(0.25)
    cache[key] = title
    return title or None

MAX_DEPTH = 6           # deepest real chain seen: 5 (sreUSD -> agg ->
MAX_NODES = 40          # adapter -> NG pool); caps are safety nets only —
                        # the closure pass below guarantees no ref is ever
                        # silently dropped inside the caps

# ---- the probe battery ------------------------------------------------------
# EMA parameters, all plain uint getters, seconds (or scaled — see _ma_clean).
MA_FNS = ["ma_exp_time()", "ma_time()", "ma_half_time()", "exp_time()",
          "MA_EXP_TIME()", "ma_period()"]

# Address-returning reference getters, probed on every node.
REF_FNS = ["POOL()", "pool()", "AGG()", "agg()", "aggregator()",
           "AGGREGATOR()", "CHAINLINK_AGGREGATOR()", "chainlink_aggregator()",
           "feed()", "FEED()", "CHAINLINK_FEED()", "price_oracle_contract()",
           "VAULT()", "vault()", "asset()", "underlying()", "ORACLE()",
           "oracle()", "staked_oracle()", "STAKED_ORACLE()", "CORE_ORACLE()",
           "core_oracle()", "STABLESWAP()", "stableswap()", "TRICRYPTO()",
           "tricrypto()", "POOL_A()", "POOL_B()", "SFRXETH()", "WSTETH()",
           "redemption_oracle()", "rate_oracle()",
           # OracleProxy / PriceOracleProxy: the factory admin can swap the
           # real oracle after deployment — it hides behind implementation()
           "implementation()", "IMPLEMENTATION()",
           # StableSwapNGAdapter: view shim whose __default__ forwards every
           # call to TARGET (a real NG pool) so the old-ABI crvUSD agg can
           # read it — without this ref it duck-types as a pool
           "TARGET()"]

# Indexed references: (getter, max index). pools(i) — CryptoFromPoolsRate*;
# price_pairs(i) — the crvUSD agg (returns (pool, is_inverse)).
IDX_FNS = [("pools(uint256)", 4), ("price_pairs(uint256)", 8),
           ("POOLS(uint256)", 4)]

# Identity / classification probes.
ID_FNS = ["description()", "symbol()", "name()", "decimals()", "A()",
          "gamma()", "coins(uint256)", "sigma()", "latestRoundData()",
          "convertToAssets(uint256)", "price()", "price_w()",
          "get_virtual_price()", "N_COINS()",
          # rate providers (wstETH, LSTs, RWA redemption contracts)
          "getRate()", "stEthPerToken()", "exchangeRate()",
          "getExchangeRate()", "latestAnswer()"]

def _battery(addr: str) -> list[tuple[str, str]]:
    calls = []
    for fn in MA_FNS + REF_FNS:
        calls.append((addr, sel(fn)))
    for fn, mx in IDX_FNS:
        for i in range(mx):
            calls.append((addr, sel(fn) + hex(i)[2:].rjust(64, "0")))
    for fn in ID_FNS:
        d = sel(fn)
        if "uint256" in fn:
            arg = 10 ** 18 if fn.startswith("convertToAssets") else 0
            d += hex(arg)[2:].rjust(64, "0")
        calls.append((addr, d))
    return calls


N_MA, N_REF = len(MA_FNS), len(REF_FNS)
N_IDX = sum(mx for _, mx in IDX_FNS)


def _ma_clean(fn: str, v: int | None) -> int | None:
    """A believable EMA time in seconds: 1 min .. 30 days. ma_time() on
    two/tricrypto-ng is stored / ln(2)-scaled in some versions — keep raw;
    the UI labels the getter it came from."""
    if v is None or not (10 <= v <= 2_592_000):
        return None
    return v


def _clean(s: str | None) -> str | None:
    """Old factory pools store a 32-byte null-padded 'symbol' — a truthy
    string of garbage. Normalize every label candidate through this."""
    return (s or "").replace("\x00", "").strip() or None


def probe_node(rpc: Rpc, addr: str) -> dict:
    """One multicall battery -> {type, label, ma{}, refs{}, meta{}}.
    rpc.mq takes (to, fn, arg) triples; the battery carries pre-encoded
    calldata, so it goes through rpc._mq_chunk on (to, calldata) pairs."""
    calls = _battery(addr)
    out = []
    for i in range(0, len(calls), 200):
        out += rpc._mq_chunk([(to, d) for to, d in calls[i:i + 200]])
    k = 0
    ma = {}
    for fn in MA_FNS:
        v = _ma_clean(fn, _num(out[k]))
        if v is not None:
            ma[fn[:-2]] = v
        k += 1
    refs = {}
    for fn in REF_FNS:
        a = _addr(out[k])
        if a and int(a, 16) > 2 ** 20:
            refs[fn[:-2]] = a
        k += 1
    for fn, mx in IDX_FNS:
        base = fn.split("(")[0]
        for i in range(mx):
            r = out[k]
            k += 1
            if not r:
                continue
            a = _addr(r)  # first word (price_pairs: (pool, is_inverse))
            if a and int(a, 16) > 2 ** 20:
                refs[f"{base}({i})"] = a
    idres = {}
    for fn in ID_FNS:
        idres[fn] = out[k]
        k += 1
    meta: dict = {}
    desc = _string(idres["description()"])
    sym = _string(idres["symbol()"])
    name = _string(idres["name()"])
    has = lambda f: idres[f] is not None
    # classify
    if has("latestRoundData()") and (desc or "aggregator" in refs):
        typ = "chainlink"
        meta["description"] = desc
        if _num(idres["decimals()"]) is not None:
            meta["decimals"] = _num(idres["decimals()"])
    elif "TARGET" in refs and has("coins(uint256)"):
        # StableSwapNGAdapter: answers every pool probe because __default__
        # forwards them to TARGET, but holds nothing — the liquidity, EMA
        # params and LP holders all live at the TARGET pool.
        typ = "adapter"
        ma = {}          # the ma getters were answered by TARGET, not it
    elif has("coins(uint256)") and (has("A()") or has("get_virtual_price()")):
        typ = "pool"
        meta["A"] = _num(idres["A()"])
        if has("gamma()"):
            meta["family"] = "cryptoswap"
        else:
            meta["family"] = "stableswap"
        if _num(idres["N_COINS()"]) is not None:
            meta["n_coins"] = _num(idres["N_COINS()"])
        meta["symbol"] = sym or name
    elif "asset" in refs and has("convertToAssets(uint256)"):
        typ = "vault"
        meta["symbol"] = sym
        meta["rate_1e18"] = _num(idres["convertToAssets(uint256)"])
    elif has("sigma()") and any(r.startswith("price_pairs") for r in refs):
        typ = "agg"          # crvUSD AggregatorStablePrice
        meta["symbol"] = sym
    elif (has("getRate()") or has("stEthPerToken()") or has("exchangeRate()")
          or has("getExchangeRate()")):
        typ = "rate"         # LST / RWA redemption-rate provider, no EMA
        for f in ("getRate()", "stEthPerToken()", "exchangeRate()",
                  "getExchangeRate()"):
            v = _num(idres[f])
            if v:
                meta["rate_fn"] = f[:-2]
                meta["rate_1e18"] = str(v)
                break
    elif has("price()") or has("price_w()"):
        typ = "wrapper"
    else:
        typ = "unknown"
    label = _clean(desc) or _clean(sym) or _clean(name)
    if typ == "adapter":
        # desc/sym/name came from the TARGET pool through the fallback —
        # leave it blank so the verified-source @title names the shim
        label = None
    if typ == "pool" and not label:
        # derive the label from the coins when the pool's own symbol is
        # the null-padded garbage (crvUSD agg components are like this)
        nc = meta.get("n_coins") or 2
        cs = rpc._mq_chunk([(addr, sel("coins(uint256)")
                             + hex(i)[2:].rjust(64, "0")) for i in range(nc)])
        toks = [t for t in (_addr(r) if r else None for r in cs)
                if t and int(t, 16)]
        ss = rpc._mq_chunk([(t, sel("symbol()")) for t in toks])
        syms = [_clean(_string(r)) for r in ss]
        if any(syms):
            label = "/".join(s or "?" for s in syms)
            meta["symbol"] = label
    node = {"type": typ, "ma": ma, "refs": refs, "meta": meta}
    if label:
        node["label"] = label
    if has("price()"):
        p = _num(idres["price()"])
        if p is not None:
            node["price_1e18"] = str(p)
    return node


def embedded_addrs(rpc: Rpc, addr: str) -> list[str]:
    """Addresses baked into the bytecode as immutables (padded to 32 bytes).
    Vyper oracles routinely reference their pool/feed through an immutable
    WITHOUT a getter — the WFRAX/Fraxtal oracle and the LLV2-Optimism
    chainlink wrappers are like this. The regex also matches code fragments
    that merely LOOK like padded addresses (PUSH sequences), so every
    candidate is getCode-verified: only actual contracts survive."""
    import re
    try:
        code = rpc.raw("eth_getCode", [addr, "latest"])
    except Exception:
        return []
    if not code or code == "0x":
        return []
    cands = []
    # 0{24}-padded = Vyper/solc immutables; 73-prefixed = PUSH20, how
    # Solidity inlines `public constant` addresses (ReusdOracle-style).
    # Opcode-soup false matches die at the getCode verification below.
    for m in re.finditer(r"(?:0{24}|73)([a-f0-9]{40})", code[2:].lower()):
        a = "0x" + m.group(1)
        # constants like 10**10 also pad to this shape; require address-like
        # entropy in the high bytes before spending a getCode on it.
        if int(a, 16) > 2 ** 80 and a != addr.lower() and a not in cands:
            cands.append(a)
    out = []
    for a in cands[:16]:
        try:
            c = rpc.raw("eth_getCode", [a, "latest"])
        except Exception:
            continue
        if c and c != "0x":
            out.append(a)
        if len(out) >= 8:
            break
    return out


def walk(rpc: Rpc, root: str, cache: dict) -> dict[str, dict]:
    """BFS the reference graph from root; returns {addr: node}."""
    seen: dict[str, dict] = {}
    frontier = [(root.lower(), 0)]
    while frontier and len(seen) < MAX_NODES:
        addr, depth = frontier.pop(0)
        if addr in seen or depth > MAX_DEPTH:
            continue
        if addr not in cache:
            try:
                node = probe_node(rpc, addr)
                # Wrapper/unknown nodes hide references in getterless
                # immutables — ALWAYS merge the bytecode-embedded contracts,
                # not only when no named getter hit: a wrapper may expose one
                # ref and hide three more (multi-feed chainlink wrappers do).
                # only WRAPPERS get the bytecode/storage discovery: an
                # "unknown" is a token/registry (no price getters answered)
                # and its embedded refs are noise — the ZCHF token e.g.
                # points at Frankencoin's FPS equity, which points back,
                # creating a bogus cycle in the flow map
                if node["type"] == "wrapper":
                    known = {a.lower() for a in node["refs"].values()}
                    for i, a in enumerate(embedded_addrs(rpc, addr)):
                        if a.lower() not in known:
                            node["refs"][f"embedded({i})"] = a
                    # getter-less storage proxies (arbitrum
                    # PriceOracleProxy): the oracle sits in a plain slot
                    if not node["refs"]:
                        for slot in range(4):
                            try:
                                v = rpc.raw("eth_getStorageAt",
                                            [addr, hex(slot), "latest"])
                            except Exception:
                                break
                            if not v or not (2 ** 80 < int(v, 16) < 2 ** 160):
                                continue
                            a2 = "0x" + v[-40:]
                            try:
                                c2 = rpc.raw("eth_getCode", [a2, "latest"])
                            except Exception:
                                continue
                            if c2 and c2 != "0x":
                                node["refs"][f"storage({slot})"] = a2
                cache[addr] = node
            except Exception as e:
                cache[addr] = {"type": "error", "error": str(e)[:80],
                               "ma": {}, "refs": {}, "meta": {}}
        node = cache[addr]
        seen[addr] = node
        # tokens (ERC20 leaves) are not worth walking further: a vault's
        # asset() points at a token, not another oracle. Walk only nodes that
        # could quote something.
        for a in node["refs"].values():
            if a.lower() not in seen:
                frontier.append((a.lower(), depth + 1))
    # Relationship pass: a node reached through an `aggregator` ref is a
    # Chainlink implementation even though its reads revert for us —
    # AccessControlled aggregators only answer whitelisted callers (their
    # proxy), so no probe can classify them directly.
    for a, n in seen.items():
        for g, ra in n["refs"].items():
            child = seen.get(ra.lower())
            if child is None:
                continue
            if "aggregator" in g.lower() and child["type"] == "unknown":
                child["type"] = "chainlink-agg"
                if n.get("label") and not child.get("label"):
                    child["label"] = n["label"] + " (implementation)"
    # Closure pass: the BFS caps must never SILENTLY cut a referenced
    # node (sreUSD's agg sits one hop deeper than everywhere else, which
    # once dropped the NG pools behind the adapters). Probe every still-
    # dangling ref target, depth ignored, up to a hard node cap.
    for _ in range(8):
        dangling = {ra.lower() for n in seen.values()
                    for ra in n["refs"].values() if ra.lower() not in seen}
        if not dangling or len(seen) >= 48:
            break
        for a in sorted(dangling):
            if len(seen) >= 48:
                break
            if a not in cache:
                try:
                    cache[a] = probe_node(rpc, a)
                except Exception as e:
                    cache[a] = {"type": "error", "error": str(e)[:80],
                                "ma": {}, "refs": {}, "meta": {}}
            seen[a] = cache[a]
    # Relationship pass for closure-added nodes too
    for a, n in seen.items():
        for g, ra in n["refs"].items():
            child = seen.get(ra.lower())
            if child is not None and "aggregator" in g.lower() \
                    and child["type"] == "unknown":
                child["type"] = "chainlink-agg"
                if n.get("label") and not child.get("label"):
                    child["label"] = n["label"] + " (implementation)"
    # Prune pure-noise leaves the bytecode scan dragged in: no type signal,
    # no ma, no refs, and nothing else points at them via a NAMED getter.
    named = {ra.lower() for n in seen.values()
             for g, ra in n["refs"].items() if not g.startswith("embedded")}
    drop = {a for a, n in seen.items()
            if n["type"] == "unknown" and not n["ma"] and not n["refs"]
            and not n.get("label") and a != root.lower() and a not in named}
    for a in drop:
        del seen[a]
    # A dropped node takes its incoming edges with it — and if the hard
    # cap ever leaves a ref unresolved, cut that edge too rather than ship
    # an arrow into nothing (loudly, so it can't rot unnoticed).
    for a, n in seen.items():
        gone = {g for g, ra in n["refs"].items() if ra.lower() not in seen}
        for g in gone:
            if n["refs"][g].lower() not in drop:
                print(f"[oracles]   WARNING unresolved ref pruned: "
                      f"{a[:10]} --{g}--> {n['refs'][g][:10]}", flush=True)
            del n["refs"][g]
    return seen


def refresh_values() -> None:
    """Light 30-min-cycle pass: re-read ONLY the dynamic numbers of the
    already-mapped graph — price(), share rate, pool EMA — a few
    multicall batches per chain. The full graph walk stays manual."""
    data = json.loads(OUT.read_text())
    mkts = data["markets"]
    fns = [("price()", "price"), ("convertToAssets(uint256)", "rate"),
           ("price_oracle()", "ema"), ("price_oracle(uint256)", "ema0"),
           ("getRate()", "grate"), ("stEthPerToken()", "srate"),
           ("exchangeRate()", "xrate")]
    per_chain: dict[str, set] = {}
    for m in mkts.values():
        for a in (m.get("nodes") or {}):
            per_chain.setdefault(m["chain"], set()).add(a)
    t0 = time.time()
    for chain, addr_set in per_chain.items():
        try:
            rpc = Rpc(chain)
            addrs = sorted(addr_set)
            calls = []
            for a in addrs:
                for fn, _tag in fns:
                    d = sel(fn)
                    if "uint256" in fn:
                        arg = 10 ** 18 if fn.startswith("convertToAssets") else 0
                        d += hex(arg)[2:].rjust(64, "0")
                    calls.append((a, d))
            res = []
            for i in range(0, len(calls), 200):
                res += rpc._mq_chunk(calls[i:i + 200])
            vals: dict = {}
            k = 0
            for a in addrs:
                v = {}
                for fn, tag in fns:
                    n_ = _num(res[k])
                    k += 1
                    if n_:
                        v[tag] = n_
                vals[a] = v
            for m in mkts.values():
                if m["chain"] != chain:
                    continue
                for a, n in (m.get("nodes") or {}).items():
                    v = vals.get(a) or {}
                    if "price" in v:
                        n["price_1e18"] = str(v["price"])
                    if n.get("type") == "vault" and "rate" in v:
                        n.setdefault("meta", {})["rate_1e18"] = str(v["rate"])
                    if n.get("type") == "rate":
                        for tag in ("grate", "srate", "xrate"):
                            if tag in v:
                                n.setdefault("meta", {})["rate_1e18"] = \
                                    str(v[tag])
                                break
                    # adapters expose price_oracle() explicitly (forwarding
                    # TARGET's EMA), so they carry an ema like a pool
                    if n.get("type") in ("pool", "adapter"):
                        ema = v.get("ema") or v.get("ema0")
                        if ema:
                            n["ema_1e18"] = str(ema)
        except Exception as e:
            print(f"[oracles] value refresh {chain} failed: {str(e)[:120]}")
    data["values_at"] = int(time.time())
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, OUT)
    print(f"[oracles] values refreshed in {time.time() - t0:.1f}s "
          f"({sum(len(s) for s in per_chain.values())} nodes)")


def main() -> None:
    M = json.loads(MARKETS.read_text())
    out: dict = {}
    rpcs: dict[str, Rpc] = {}
    caches: dict[str, dict] = {}
    try:
        tcache = json.loads(TITLES.read_text())
    except (OSError, ValueError):
        tcache = {}
    t0 = time.time()
    for grp in ("LLV1", "LLV2"):
        for m in M["groups"][grp]:
            chain = m["chain"]
            rpc = rpcs.setdefault(chain, Rpc(chain))
            cache = caches.setdefault(chain, {})
            key = f"{chain}:{m['controller']}"
            amm = m["amm"]
            root = None
            r = rpc.q(amm, "price_oracle_contract()")
            if r:
                root = _addr(r)
            entry: dict = {"group": grp, "chain": chain,
                           "market": f"{m['collateral']['symbol']}/"
                                     f"{m['borrowed']['symbol']}",
                           "amm": amm, "root": root}
            if root and int(root, 16):
                nodes = walk(rpc, root, cache)
                for a, n in nodes.items():
                    if not n.get("label"):
                        t = contract_title(chain, a, tcache)
                        if t:
                            n["label"] = t
                entry["nodes"] = nodes
                n_ma = sum(1 for n in nodes.values() if n["ma"])
                print(f"{grp} {chain:<9} {entry['market']:<22} root {root[:10]} "
                      f"nodes={len(nodes)} with_ma={n_ma} "
                      f"types={sorted(set(n['type'] for n in nodes.values()))}",
                      flush=True)
            else:
                entry["nodes"] = {}
                print(f"{grp} {chain:<9} {entry['market']:<22} NO price_oracle_contract",
                      flush=True)
            out[key] = entry
    OUT.parent.mkdir(parents=True, exist_ok=True)
    ttmp = TITLES.with_suffix(".json.tmp")
    ttmp.write_text(json.dumps(tcache, indent=0))
    os.replace(ttmp, TITLES)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "fetched_at": int(time.time()),
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "markets": out,
    }, indent=1))
    os.replace(tmp, OUT)
    print(f"wrote {OUT}  ({len(out)} markets, {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    if "--values" in sys.argv:
        refresh_values()
    else:
        main()
