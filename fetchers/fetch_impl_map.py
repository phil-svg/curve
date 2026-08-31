#!/usr/bin/env python3
"""On-chain implementation map for the Implementations tab.

Classifies, one by one, against the chain itself (eth_call probes):
  - every census pool -> engine family/version (stableswap classic v1 /
    factory plain / lido flavors / NG / meta classic|NG / underlying-
    lending vintage; crypto2 / tricrypto2 / tricrypto-NG / twocrypto;
    Yield Basis twocrypto fork)
  - every lending market's CURRENT monetary policy (re-read from the
    controller each run — policies can be swapped; swaps are recorded)
    + the policy's implementation kind from its marker methods
  - the crvUSD PegKeepers (V1 vs V2 via regulator()) and their pools
  - the Yield Basis stack from the YB factory (pool / AMM / LT vault)

Writes data/impl_map.json. Ethereum probes run against the mainnet RPC
(WEB3_HTTP_MAINNET); other chains use the public pools from
fetch_markets.CHAIN_RPCS. Pools on chains where no RPC answers fall back
to unverified "unknown".
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "pylib"))
sys.path.insert(0, str(HERE / "fetchers"))

from fetch_markets import Rpc  # noqa: E402
from fetch_lp import YB_FACTORY, http, pegkeepers  # noqa: E402

OUT = HERE / "data" / "impl_map.json"

# steth/ankr/reth-era pools that read live balances (the "lido flavors"
# bucket of stable_classic.hpp) — fingerprint-identical to classic
# otherwise, so they are pinned by address
LIDO_FLAVORS = {
    "0xdc24316b9ae028f1497c275eb9192a3ea0f67022",   # steth
    "0xa96a65c051bf88b4095ee1f2451c2a9d43f53ae2",   # ankreth
    "0xf9440930043eb3997fc70e1339dbb11f341de7a8",   # reth
    "0x828b154032950c8ff7cf8085d841723db2696056",   # steth-concentrated
    "0x21e27a5e5513d6e65c4f830167390997aa84843a",   # steth-ng
}

POOL_PROBES = [("gamma()", None), ("version()", None), ("base_pool()", None),
               ("BASE_POOL()", None),
               ("stored_rates()", None), ("offpeg_fee_multiplier()", None),
               ("A_precise()", None), ("underlying_coins(uint256)", 0),
               ("underlying_coins(int128)", 0), ("MATH()", None),
               ("price_scale()", None), ("price_scale(uint256)", 0),
               # EMA-time params: on-chain only, surfaced on pool pages
               ("ma_exp_time()", None), ("D_ma_time()", None),
               ("ma_time()", None)]

POLICY_PROBES = [("sigma()", None), ("rate0()", None), ("min_rate()", None),
                 ("max_rate()", None), ("target_utilization()", None),
                 ("TARGET_U()", None), ("slope()", None),
                 ("ma_rate()", None),
                 ("base_rate()", None),
                 ("parameters()", None)]


def classify_pool(m: dict, addr: str, yb_pools: set) -> str:
    if addr in yb_pools:
        return "yb_twocrypto"
    if m["gamma()"]:
        if m["MATH()"]:
            return "twocrypto" if m["price_scale()"] else "tricrypto_ng"
        return "crypto2" if m["price_scale()"] else "tricrypto2"
    if addr in LIDO_FLAVORS:
        return "lido_flavors"
    base = m["base_pool()"] or (m["BASE_POOL()"]
                                if m["BASE_POOL()"]
                                and int(m["BASE_POOL()"], 16) else None)
    if base:
        return "meta_ng" if m["stored_rates()"] else "meta_classic"
    if m["stored_rates()"] and m["offpeg_fee_multiplier()"]:
        return "stableswap_ng"
    if m["underlying_coins(uint256)"] or m["underlying_coins(int128)"]:
        return "lending_underlying"
    if m["A_precise()"]:
        return "factory_plain"
    return "classic_v1"


def classify_policy(m: dict) -> str:
    if m["slope()"] and m["base_rate()"]:
        # single-instance FlatTime-LinearMonetaryPolicy (rate is a pure
        # clamped function of time; utilization-independent)
        return "flat_linear"
    if m["min_rate()"] and m["max_rate()"]:
        return "semilog"
    if m["ma_rate()"]:
        return "secondary_ema"
    if m["target_utilization()"] or m["TARGET_U()"]:
        return "secondary"
    if m["sigma()"] and m["rate0()"]:
        return "mint_agg"
    p = m["parameters()"]
    if p:
        words = (len(p) - 2) // 64
        # 4-word struct (u_inf, A, r_minf, shift) = the plain secondary
        # policy; the 7/8-word struct is the LLV2 dynamic policy
        return "secondary" if words <= 4 else "llv2_dynamic"
    return "unknown"


def _string(rpc, to):
    try:
        r = rpc.q(to, "version()")
        if not r or len(r) < 130:
            return None
        b = bytes.fromhex(r[2:])
        ln = int.from_bytes(b[32:64], "big")
        return b[64:64 + ln].decode("utf-8", "replace").strip() or None
    except Exception:
        return None


def main() -> None:
    t0 = time.time()
    prev = {}
    try:
        prev = json.loads(OUT.read_text())
    except (OSError, ValueError):
        pass
    cen = json.loads((HERE / "data" / "census.json").read_text())["pools"]
    llm = json.loads((HERE / "data" / "llm.json").read_text())["markets"]

    rpcs: dict[str, Rpc] = {}
    def rpc_for(ch):
        if ch not in rpcs:
            rpcs[ch] = Rpc(ch)
        return rpcs[ch]

    # ---- Yield Basis stack (ethereum) -----------------------------------
    yb = {"factory": YB_FACTORY, "markets": []}
    yb_pools: set[str] = set()
    try:
        eth = rpc_for("ethereum")
        n = eth.num(YB_FACTORY, "market_count()") or 0
        rows = eth.mq([(YB_FACTORY, "markets(uint256)", i) for i in range(n)])
        for r in rows:
            if not r:
                continue
            w = [r[2 + j * 64: 2 + (j + 1) * 64]
                 for j in range(len(r[2:]) // 64)]
            a = ["0x" + x[24:] for x in w]
            if len(a) >= 4:
                yb["markets"].append(
                    {"asset": a[0], "pool": a[1], "amm": a[2], "lt": a[3]})
                yb_pools.add(a[1].lower())
    except Exception as e:
        print(f"[impl] yb factory walk failed: {e}", flush=True)
        yb = prev.get("yb", yb)
        yb_pools = {m["pool"].lower() for m in yb.get("markets", [])}

    # ---- pools, chain by chain, one probe-multicall sweep per chain ------
    pools = {}
    for ch, rows in cen.items():
        addrs = [r[0].lower() for r in rows if isinstance(r, list) and r]
        try:
            rpc = rpc_for(ch)
            calls = [(a, fn, arg) for a in addrs for fn, arg in POOL_PROBES]
            res = rpc.mq(calls)
            P = len(POOL_PROBES)
            for i, a in enumerate(addrs):
                m = {fn: res[i * P + j] for j, (fn, _)
                     in enumerate(POOL_PROBES)}
                impl = classify_pool(m, a, yb_pools)
                ver = None
                if m["version()"] and len(m["version()"]) >= 130:
                    b = bytes.fromhex(m["version()"][2:])
                    ln = int.from_bytes(b[32:64], "big")
                    ver = b[64:64 + ln].decode("utf-8", "replace").strip() \
                        or None
                oc = {}
                for fn, key in (("ma_exp_time()", "ma_exp_time"),
                                ("D_ma_time()", "D_ma_time"),
                                ("ma_time()", "ma_time")):
                    x = m.get(fn)
                    if x and x != "0x" and len(x) >= 66:
                        v = int(x[:66], 16)
                        if 0 < v < 10**9:
                            oc[key] = v
                pools[f"{ch}:{a}"] = {"impl": impl, "verified": True,
                                      **({"version": ver} if ver else {}),
                                      **({"params": oc} if oc else {})}
        except Exception as e:
            print(f"[impl] {ch}: probe sweep failed ({str(e)[:80]}) — "
                  f"keeping previous / unknown", flush=True)
            for a in addrs:
                pools[f"{ch}:{a}"] = (prev.get("pools", {}) or {}).get(
                    f"{ch}:{a}", {"impl": "unknown", "verified": False})

    # ---- monetary policies: re-read from each controller, log swaps ------
    policies = {}
    prev_pol = prev.get("policies", {})
    by_chain: dict[str, list] = {}
    for key, mkt in llm.items():
        by_chain.setdefault(mkt["chain"], []).append(mkt)
    for ch, mkts in by_chain.items():
        try:
            rpc = rpc_for(ch)
            cur = rpc.mq([(m["controller"], "monetary_policy()", None)
                          for m in mkts])
            probe_addrs = []
            for m, r in zip(mkts, cur):
                pol = ("0x" + r[-40:]) if r and len(r) >= 42 else m.get("policy")
                probe_addrs.append((m, pol))
            calls = [(pol, fn, arg) for _, pol in probe_addrs if pol
                     for fn, arg in POLICY_PROBES]
            res = rpc.mq(calls) if calls else []
            P = len(POLICY_PROBES)
            i = 0
            for m, pol in probe_addrs:
                key = f"{ch}:{m['controller'].lower()}"
                if not pol:
                    policies[key] = {"policy": None, "kind": "unknown",
                                     "verified": False}
                    continue
                mk = {fn: res[i * P + j] for j, (fn, _)
                      in enumerate(POLICY_PROBES)}
                mk.setdefault("peg_keepers(uint256)", None)
                i += 1
                ent = {"policy": pol.lower(),
                       "kind": classify_policy(mk), "verified": True}
                old = prev_pol.get(key)
                hist = list((old or {}).get("swaps") or [])
                if old and old.get("policy") \
                        and old["policy"] != ent["policy"]:
                    hist.append({"from": old["policy"], "to": ent["policy"],
                                 "at": int(time.time())})
                    print(f"[impl] POLICY SWAP {key}: {old['policy']} -> "
                          f"{ent['policy']}", flush=True)
                if hist:
                    ent["swaps"] = hist
                policies[key] = ent
        except Exception as e:
            print(f"[impl] {ch}: policy sweep failed ({str(e)[:80]})",
                  flush=True)
            for m in mkts:
                key = f"{ch}:{m['controller'].lower()}"
                policies[key] = prev_pol.get(key, {
                    "policy": (m.get("policy") or "").lower() or None,
                    "kind": "unknown", "verified": False})

    # ---- pegkeepers (ethereum): V1 vs V2 via regulator() ------------------
    pks = []
    try:
        j = http("https://prices.curve.finance/v1/crvusd/pegkeepers/ethereum")
        keepers = j.get("keepers", [])
        eth = rpc_for("ethereum")
        regs = eth.mq([(k["address"], "regulator()", None) for k in keepers])
        for k, r in zip(keepers, regs):
            reg = ("0x" + r[-40:]) if r and len(r) >= 42 else None
            pks.append({"address": k["address"].lower(),
                        "pool": k.get("pool"),
                        "pool_address": (k.get("pool_address") or "").lower(),
                        "version": "V2" if reg else "V1",
                        **({"regulator": reg} if reg else {})})
    except Exception as e:
        print(f"[impl] pegkeepers failed: {e}", flush=True)
        pks = prev.get("pegkeepers", [])

    out = {"fetched_at": int(time.time()), "pools": pools,
           "policies": policies, "pegkeepers": pks, "yb": yb}
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(OUT)
    from collections import Counter
    cnt = Counter(v["impl"] for v in pools.values())
    print(f"[impl] {len(pools)} pools classified in {time.time()-t0:.0f}s: "
          + ", ".join(f"{k}={v}" for k, v in cnt.most_common()), flush=True)
    print(f"[impl] {len(policies)} policies "
          f"({Counter(p['kind'] for p in policies.values())}), "
          f"{len(pks)} pegkeepers, {len(yb['markets'])} yb markets",
          flush=True)


if __name__ == "__main__":
    main()
