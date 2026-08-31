#!/usr/bin/env python3
"""fetch_lp.py — end-user LP distribution of the top-N Curve pools (all
chains), maintained event-driven: ERC-20 balances only move on events,
so each cycle scans one batched Transfer/Staked/Withdrawn getLogs window
per chain since the last block and re-reads balances ONLY for touched
addresses. New pools entering the top-N are seeded on the fly (Ethplorer
top-100 on ethereum, a chunked Transfer scan elsewhere); coverage
(balances found / totalSupply) is recomputed every cycle and shown in
the UI as the honesty metric.

Wrapper unwrapping, all auto-discovered:
  - pool gauge (Curve API), 1:1 with staked LP
  - the stETH gauge's Lido StakingRewards hop (alias)
  - Convex BaseRewardPools (Booster registry), tracked only where the
    voter proxy holds >= 1% of LP supply; stakers enumerated by
    Staked/Withdrawn events (backfilled once from contract creation)
  - Yield Basis pools (factory registry): end users hold the market's
    LT / its YB gauge, scaled by the YB AMM's share of the raw pool LP
  - crvUSD PegKeepers (prices API) are terminal protocol equity

Writes data/lp.json (the /lp endpoint); state in data/lp_state.json.

    python3 fetchers/fetch_lp.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE / "pylib"))
sys.path.insert(0, str(HERE / "sim"))
from common import sel  # noqa: E402
from fetch_markets import Rpc, _num  # noqa: E402
from fetch_lenders import transfer_tos, creation_block  # noqa: E402
from Crypto.Hash import keccak  # noqa: E402

STATE = HERE / "data" / "lp_state.json"
LOCK = HERE / "tmp" / "lp_fetch.pid"


import threading

_IO_LOCK = threading.Lock()
_PUB_AT = [0.0]


def write_state(st: dict) -> None:
    with _IO_LOCK:
        for attempt in range(5):
            try:
                tmp = STATE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(st))
                tmp.replace(STATE)
                return
            except RuntimeError:      # another thread mutated mid-dump
                time.sleep(0.05 * (attempt + 1))
OUT = HERE / "data" / "lp.json"
CVX_SEED = HERE / "data" / "convex_stakers_seed.json"
MIN_TVL = 100_000       # track every pool at or above this TVL
MIN_VOL = 100_000       # ...or with daily volume at or above this
MAX_POOLS = 400         # hard safety cap
LIST_N = 60             # rows kept per pool in lp.json
# per-run onboarding budgets: the pool set converges over a few cycles
# instead of one multi-hour blocking run; anything not yet onboarded shows
# as low coverage / a collective until its turn comes
SEED_BUDGET = 150       # new contracts seeded per run
CVX_BUDGET = 40          # Convex reward-pool backfills per run
LABEL_BUDGET = 1200     # new address label lookups per run

BOOSTER = "0xf403c135812408bfbe8713b5a23a04b3d48aae31"
YB_FACTORY = "0x370a449febb9411c95bf897021377fe0b7d100c0"
CVX_PROXY = "0x989aeb4d175e16225e39e87d0d97a3360524ad80"
SD_LOCKER = "0x52f541764e6e90eebc5c21ff570de0e2d63766b6"
LIDO_SR = "0x4f48031b0ef8accea3052af00a3279fba31b50d8"
WINTERMUTE = "0xe74b28c2eae8679e3ccc3a94d5d0de83ccb84705"

# label, kind ("user"|"protocol"|"collective"); kind drives the UI suffix
KNOWN = {
    WINTERMUTE: ("Wintermute exploiter", "user"),
    CVX_PROXY: ("Convex stakers", "collective"),
    SD_LOCKER: ("StakeDAO lockers", "collective"),
    "0xf147b8125d2ef93fb6965db97d6746952a133934":
        ("Yearn veCRV voter", "collective"),
    # verified: token() == 3Crv
    "0xa464e6dcda8ac41e03616f95f4bc98a13b8922dc":
        ("Curve fee distributor", "protocol"),
    "0xba12222222228d8ba445958a75a0704d566bf2c8":
        ("Balancer vault", "collective"),
    # Safe proxy, no name(); explorer-tagged Frax Finance: Comptroller
    "0xb1748c79709f4ba2dd82834b8c82d4a505003f27":
        ("Frax treasury (Comptroller)", "protocol"),
}


def _topic(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


T_TRANSFER = _topic("Transfer(address,address,uint256)")
T_STAKED = _topic("Staked(address,uint256)")
T_WITHDRAWN = _topic("Withdrawn(address,uint256)")


def http(u: str) -> dict:
    req = urllib.request.Request(u, headers={"User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _call(rpc: Rpc, to: str, fn: str, arg=None):
    data = sel(fn) + (hex(arg)[2:].rjust(64, "0") if arg is not None else "")
    return rpc.raw("eth_call", [{"to": to, "data": data}, "latest"])


def erc20_name(rpc: Rpc, a: str) -> str:
    for s in ("name()", "symbol()"):
        try:
            r = _call(rpc, a, s)
            if r and len(r) > 130:
                b = bytes.fromhex(r[2:])
                ln = int.from_bytes(b[32:64], "big")
                nm = b[64:64 + ln].decode("utf-8", "replace").strip()
                if nm:
                    return nm
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------- registry
def oracle_pools() -> dict:
    """(chain, addr) -> label for every pool a market oracle reads or a
    market's venue — security-relevant, tracked regardless of TVL."""
    need: dict = {}
    try:
        og = json.loads((HERE / "data" / "oracles.json").read_text())["markets"]
        for m in og.values():
            for a, n in (m.get("nodes") or {}).items():
                if n.get("type") == "pool":
                    need[(m["chain"], a.lower())] = (n.get("label") or "").strip()
    except Exception:
        pass
    try:
        mk = json.loads((HERE / "data" / "markets.json").read_text())["groups"]
        for lst in mk.values():
            for m in lst:
                v = m.get("venue") or {}
                if v.get("pool"):
                    need.setdefault((m["chain"], v["pool"].lower()),
                                    v.get("name") or "")
    except Exception:
        pass
    return need


def exit_tokens() -> dict:
    """chain -> {token addrs}: every market's collateral plus its
    unwrapped underlying (vault asset edges in the oracle graph) — the
    coins whose pools count as potential exit liquidity."""
    toks: dict = {}
    try:
        mk = json.loads((HERE / "data" / "markets.json").read_text())["groups"]
        for lst in mk.values():
            for m in lst:
                toks.setdefault(m["chain"], set()).add(
                    m["collateral"]["addr"].lower())
    except Exception:
        pass
    try:
        og = json.loads((HERE / "data" / "oracles.json").read_text())["markets"]
        for m in og.values():
            for a, n in (m.get("nodes") or {}).items():
                if n.get("type") == "vault":
                    for g, r in (n.get("refs") or {}).items():
                        if g in ("asset", "underlying"):
                            toks.setdefault(m["chain"], set()).add(r.lower())
    except Exception:
        pass
    return toks


def top_pools() -> list[dict]:
    pools = []
    vols: dict = {}     # (chain, addr) -> daily volume USD
    for ch in http("https://api.curve.finance/v1/getPlatforms"
                   )["data"]["platforms"]:
        try:
            for v in http(f"https://api.curve.finance/v1/getVolumes/{ch}"
                          )["data"].get("pools", []):
                vols[(ch, v["address"].lower())] = v.get("volumeUSD") or 0
        except Exception:
            pass
        try:
            for p in http(f"https://api.curve.finance/v1/getPools/all/{ch}"
                          )["data"]["poolData"]:
                if p.get("usdTotal") or \
                        vols.get((ch, p["address"].lower()), 0) >= MIN_VOL:
                    pools.append((p.get("usdTotal") or 0, ch, p))
        except Exception:
            pass
    pools.sort(key=lambda x: -x[0])
    vol_of = lambda ch, p: vols.get((ch, p["address"].lower()), 0)
    # slim census for the LLM tab's exit-liquidity join (>= $25k TVL,
    # or active enough that its LP pie matters regardless of TVL)
    census: dict = {}
    for tvl, ch, p in pools:
        if tvl < 25_000 and vol_of(ch, p) < MIN_VOL:
            continue
        census.setdefault(ch, []).append(
            [p["address"].lower(), p.get("name") or p.get("symbol") or "",
             round(tvl),
             [(c.get("address") or "").lower()
              for c in (p.get("coins") or [])]])
    ctmp = HERE / "data" / "census.json.tmp"
    ctmp.write_text(json.dumps({"fetched_at": int(time.time()),
                                "pools": census}, separators=(",", ":")))
    ctmp.replace(HERE / "data" / "census.json")
    pools_kept = [p for p in pools if p[0] >= MIN_TVL
                  or vol_of(p[1], p[2]) >= MIN_VOL][:MAX_POOLS]
    # oracle-read + venue pools ride along below the TVL cutoff
    must = oracle_pools()
    kept_keys = {(ch, p["address"].lower()) for _t, ch, p in pools_kept}
    for tvl, ch, p in pools:
        k = (ch, p["address"].lower())
        if k in must and k not in kept_keys:
            pools_kept.append((tvl, ch, p))
            kept_keys.add(k)
    # exit-liquidity candidates (>= $50k holding a market collateral or
    # its underlying) get tracked too, so their LP pies exist
    etoks = exit_tokens()
    for tvl, ch, p in pools:
        if tvl < 50_000:
            break                      # pools is TVL-sorted
        k = (ch, p["address"].lower())
        if k in kept_keys:
            continue
        tset = etoks.get(ch) or ()
        if any((c.get("address") or "").lower() in tset
               for c in (p.get("coins") or [])):
            pools_kept.append((tvl, ch, p))
            kept_keys.add(k)
    names: dict[str, dict[str, str]] = {}
    for _tvl, ch, p in pools:
        nm = p.get("name") or p.get("symbol") or ""
        if not nm:
            continue
        m = names.setdefault(ch, {})
        m[p["address"].lower()] = f"Curve pool: {nm}"
        lp = (p.get("lpTokenAddress") or "").lower()
        if lp:
            m[lp] = f"Curve pool: {nm}"
        g = (p.get("gaugeAddress") or "").lower()
        if g:
            m[g] = f"gauge of {nm}"
    out = []
    for tvl, ch, p in pools_kept:
        coins = [{"a": (c.get("address") or "").lower(),
                  "s": c.get("symbol") or "",
                  "n": c.get("name") or ""}
                 for c in (p.get("coins") or [])]
        out.append({"key": f"{ch}:{p['address'].lower()}",
                    "label": p.get("name") or p.get("symbol") or p["address"],
                    "chain": ch, "tvl": tvl,
                    "pool": p["address"].lower(),
                    "coins": coins,
                    "lp": (p.get("lpTokenAddress") or p["address"]).lower(),
                    "gauge": (p.get("gaugeAddress") or "").lower() or None})
    # oracle pools the census does not list at all (delisted or tiny
    # factory pools) — minimal entries; factory-pool LP token = the pool
    for (ch, a), lbl in must.items():
        if (ch, a) not in kept_keys:
            out.append({"key": f"{ch}:{a}", "label": lbl or a,
                        "chain": ch, "tvl": 0, "pool": a, "coins": [],
                        "lp": a, "gauge": None})
    return out, names


def convex_map(rpc: Rpc) -> dict:
    """lptoken -> BaseRewardPool for every Booster pid (ethereum)."""
    n = int(_call(rpc, BOOSTER, "poolLength()"), 16)
    out = {}
    for i0 in range(0, n, 120):
        res = rpc.mq([(BOOSTER, "poolInfo(uint256)", i)
                      for i in range(i0, min(i0 + 120, n))])
        for r in res:
            if r and len(r) >= 2 + 64 * 6:
                w = [r[2 + j * 64: 2 + (j + 1) * 64] for j in range(6)]
                out["0x" + w[0][24:]] = "0x" + w[3][24:]
    return out


def yb_map(rpc: Rpc) -> dict:
    """pool -> {amm, lt} from the Yield Basis factory (ethereum)."""
    n = int(_call(rpc, YB_FACTORY, "market_count()"), 16)
    out = {}
    for i in range(n):
        r = _call(rpc, YB_FACTORY, "markets(uint256)", i)
        w = [r[2 + j * 64: 2 + (j + 1) * 64] for j in range(len(r[2:]) // 64)]
        addrs = ["0x" + x[24:] for x in w]
        if len(addrs) >= 4:
            out[addrs[1]] = {"amm": addrs[2], "lt": addrs[3]}
    return out


def pegkeepers() -> set[str]:
    try:
        j = http("https://prices.curve.finance/v1/crvusd/pegkeepers/ethereum")
        return {k["address"].lower() for k in j.get("keepers", [])}
    except Exception:
        return set()


# ------------------------------------------------------------------- state
def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
        if st.get("version") == 2:
            st.setdefault("labels", {})
            return st
    except (OSError, ValueError):
        pass
    st = {"version": 2, "chains": {}, "kinds": {}, "yb_gauges": {},
          "cvx_done": [], "labels": {}}
    return st


def chain_st(st: dict, chain: str) -> dict:
    return st["chains"].setdefault(
        chain, {"last": None, "holders": {}, "balances": {}})


def seed_contract(rpc: Rpc, chain: str, cst: dict, c: str,
                  head: int) -> bool:
    """Holder-universe seed for a contract not yet tracked. Returns True
    when seeded; an Ethplorer failure on ethereum just waits for the next
    cycle (a full Transfer scan of a busy mainnet token is not a sane
    fallback)."""
    if c in cst["holders"]:
        return True
    if chain == "ethereum":
        print(f"[lp] seeding {chain} {c[:10]}… via Ethplorer", flush=True)
        try:
            time.sleep(1.1)
            hs = http(f"https://api.ethplorer.io/getTopTokenHolders/{c}"
                      f"?apiKey=freekey&limit=100")["holders"]
            cst["holders"][c] = sorted({h["address"].lower() for h in hs})
            return True
        except Exception as e:
            print(f"[lp]   ethplorer failed ({str(e)[:60]}) — trying a "
                  "bounded chain scan", flush=True)
            try:
                # complete ever-holder set from creation; budget-capped so
                # a dense token skips to next cycle instead of wedging
                frm = creation_block(rpc, c, head)
                tos = transfer_tos(rpc, [c], frm, head, [300])
                cst["holders"][c] = sorted(tos.get(c, set()))
                return True
            except Exception as e2:
                print(f"[lp]   chain scan failed ({str(e2)[:50]}) — "
                      "retrying next cycle", flush=True)
                return False
    print(f"[lp] seeding {chain} {c[:10]}… via Transfer scan", flush=True)
    try:
        frm = creation_block(rpc, c, head)
        tos = transfer_tos(rpc, [c], frm, head, [1500])
        cst["holders"][c] = sorted(tos.get(c, set()))
        return True
    except Exception as e:
        print(f"[lp]   scan failed ({str(e)[:60]}) — retrying next cycle",
              flush=True)
        return False


def backfill_rewards_batch(rpc: Rpc, cst: dict, rws: list[str],
                           head: int, st: dict) -> None:
    """All pending reward pools in ONE chunked address-array log pass —
    the union span costs the same as the oldest pool alone."""
    todo = []
    for rw in rws:
        if rw in st["cvx_done"]:
            cst["holders"].setdefault(rw, [])
            continue
        if CVX_SEED.exists():
            hit = False
            for v in json.loads(CVX_SEED.read_text()).values():
                if v["rewards"].lower() == rw:
                    cst["holders"][rw] = sorted(v["stakers"])
                    st["cvx_done"].append(rw)
                    hit = True
                    break
            if hit:
                continue
        todo.append(rw)
    if not todo:
        return
    print(f"[lp] backfilling {len(todo)} Convex reward pools in one pass",
          flush=True)
    with ThreadPoolExecutor(min(8, len(todo))) as ex:
        creations = list(ex.map(lambda a: creation_block(rpc, a, head),
                                todo))
    frm = min(creations)
    users: dict[str, set] = {rw: set() for rw in todo}
    STEPB = 100_000
    for b in range(frm, head + 1, STEPB):
        logs = rpc.raw("eth_getLogs", [{
            "address": todo, "topics": [[T_STAKED, T_WITHDRAWN]],
            "fromBlock": hex(b), "toBlock": hex(min(b + STEPB - 1, head))}])
        for lg in logs:
            users[lg["address"].lower()].add(
                "0x" + lg["topics"][1][-40:].lower())
    for rw in todo:
        cst["holders"][rw] = sorted(users[rw])
        st["cvx_done"].append(rw)
        print(f"[lp]   {rw[:10]}…: {len(users[rw])} ever-stakers",
              flush=True)


def backfill_rewards(rpc: Rpc, cst: dict, rw: str, head: int,
                     st: dict) -> None:
    """Convex staker set from Staked/Withdrawn events (once per pool)."""
    if rw in st["cvx_done"]:
        cst["holders"].setdefault(rw, [])
        return
    if CVX_SEED.exists():
        for v in json.loads(CVX_SEED.read_text()).values():
            if v["rewards"].lower() == rw:
                cst["holders"][rw] = sorted(v["stakers"])
                st["cvx_done"].append(rw)
                return
    print(f"[lp] backfilling Convex stakers {rw[:10]}…", flush=True)
    frm = creation_block(rpc, rw, head)
    users: set[str] = set()
    STEPB = 100_000
    for b in range(frm, head + 1, STEPB):
        logs = rpc.raw("eth_getLogs", [{
            "address": rw, "topics": [[T_STAKED, T_WITHDRAWN]],
            "fromBlock": hex(b), "toBlock": hex(min(b + STEPB - 1, head))}])
        for lg in logs:
            users.add("0x" + lg["topics"][1][-40:].lower())
    cst["holders"][rw] = sorted(users)
    st["cvx_done"].append(rw)
    print(f"[lp]   {len(users)} ever-stakers", flush=True)


def parallel_scan(rpc: Rpc, addrs: list[str], frm: int, head: int,
                  step: int = 100_000, workers: int = 6,
                  deadline: float | None = None) -> dict[str, set]:
    """Fixed windows fetched concurrently — Dwellir's ~6s/window archive
    latency stops mattering. Each window still splits adaptively inside
    transfer_tos if its logs overflow."""
    wins = [(lo, min(lo + step - 1, head))
            for lo in range(frm, head + 1, step)]
    out: dict[str, set] = {}
    def one(w):
        return transfer_tos(rpc, addrs, w[0], w[1], [80],
                            deadline=deadline)
    with ThreadPoolExecutor(workers) as ex:
        for part in ex.map(one, wins):
            for a, s_ in part.items():
                out.setdefault(a, set()).update(s_)
    return out


# chains with a tenderly public gateway: one FULL-chain-span getLogs per
# address group instead of hundreds of 100k windows (verified: polygon 9
# pools' whole history in 8.9s; mainnet exact-parity vs windowed Dwellir)
TENDERLY_GW = {"ethereum": "mainnet", "optimism": "optimism",
               "arbitrum": "arbitrum", "base": "base", "polygon": "polygon",
               "avalanche": "avalanche", "xdai": "gnosis", "celo": "celo",
               "sonic": "sonic", "fraxtal": "fraxtal"}


def fast_seed_eth(missing: list[str], head: int, ch: str = "ethereum") -> dict:
    """One FULL-chain-span Transfer scan for a whole address group via the
    tenderly gateway (~seconds instead of ~100 Dwellir windows). Exact
    log-set parity vs windowed Dwellir was verified (block+logIndex
    identical); the totalSupply coverage reconciliation downstream still
    catches any silent truncation and forces the windowed rescan."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{"fromBlock": "0x1", "toBlock": hex(head),
                    "address": missing, "topics": [T_TRANSFER]}]}).encode()
    req = urllib.request.Request(
        f"https://{TENDERLY_GW[ch]}.gateway.tenderly.co", body,
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
    if "error" in d:
        raise RuntimeError(str(d["error"].get("message", "rpc error"))[:80])
    tos: dict = {}
    for lg in d["result"]:
        tp = lg.get("topics") or []
        if len(tp) >= 3 and tp[0] == T_TRANSFER:
            tos.setdefault(lg["address"].lower(), set()).add(
                "0x" + tp[2][-40:].lower())
    return tos


def seed_chain_batch(rpc: Rpc, ch: str, cst: dict, missing: list[str],
                     head: int, use_creation: bool = False) -> list[str]:
    """Seed several contracts of one chain in a single chunked
    Transfer-log pass. use_creation narrows the span via threaded
    creation-block searches — worth it only on reliable archive nodes
    (Dwellir); flaky public nodes scan from 0 instead."""
    if not missing:
        return []
    print(f"[lp] seeding {len(missing)} {ch} contracts in one pass",
          flush=True)
    dl = time.time() + (1500 if use_creation else 360)
    done_fast: list[str] = []
    if ch in TENDERLY_GW:
        try:
            tos = fast_seed_eth(missing, head, ch)
            # a contract with ZERO recipients is either truly unused or a
            # silent gateway gap (seen: crvUSD agg pools with real
            # totalSupply but 0 logs from tenderly) — those fall through
            # to the windowed path instead of being trusted
            zero = [c for c in missing if not tos.get(c)]
            for c in missing:
                if tos.get(c):
                    cst["holders"][c] = sorted(tos[c])
                    done_fast.append(c)
            if not zero:
                return done_fast
            print(f"[lp]   fast {ch}: {len(zero)} contracts returned 0 "
                  "logs — windowed verify", flush=True)
            missing = zero
        except Exception as e:
            print(f"[lp]   fast {ch} seed failed ({str(e)[:60]}) — "
                  "windowed fallback path", flush=True)
    try:
        frm = 0
        if use_creation:
            with ThreadPoolExecutor(min(8, len(missing))) as ex:
                frm = min(ex.map(lambda a: creation_block(rpc, a, head),
                                 missing))
            tos = parallel_scan(rpc, missing, frm, head, deadline=dl)
        else:
            tos = transfer_tos(rpc, missing, frm, head, [800], deadline=dl)
        for c in missing:
            cst["holders"][c] = sorted(tos.get(c, set()))
            if not cst["holders"][c]:
                cst.setdefault("verified_empty", {})[c] = True
        return done_fast + missing
    except Exception as e:
        print(f"[lp]   batch seed failed ({str(e)[:60]}) — falling back "
              "to per-contract", flush=True)
    done = []
    for c in missing:
        try:
            if use_creation:
                tos = parallel_scan(rpc, [c], frm, head,
                                    deadline=time.time() + 600)
            else:
                tos = transfer_tos(rpc, [c], 0, head, [400],
                                   deadline=time.time() + 150)
            cst["holders"][c] = sorted(tos.get(c, set()))
            if not cst["holders"][c]:
                cst.setdefault("verified_empty", {})[c] = True
            done.append(c)
        except Exception as e:
            print(f"[lp]   {c[:10]}… seed failed ({str(e)[:50]})",
                  flush=True)
    return done_fast + done


def scan_chain(rpc: Rpc, cst: dict, head: int) -> set[str]:
    """Incremental batched log pass over every tracked contract."""
    if cst["last"] is None or cst["last"] >= head:
        cst["last"] = head
        return set()
    frm = cst["last"] + 1
    touched: set[str] = set()
    addrs = sorted(cst["holders"])
    if addrs:
        logs = rpc.raw("eth_getLogs", [{
            "address": addrs,
            "topics": [[T_TRANSFER, T_STAKED, T_WITHDRAWN]],
            "fromBlock": hex(frm), "toBlock": hex(head)}])
        for lg in logs:
            c = lg["address"].lower()
            for t in lg.get("topics", [])[1:3]:
                a = "0x" + t[-40:].lower()
                if int(a, 16) == 0:
                    continue
                touched.add(a)
                hs = cst["holders"][c]
                if a not in hs:
                    hs.append(a)
    cst["last"] = head
    return touched


def refresh_balances(rpc: Rpc, cst: dict,
                     touched: set[str] | None) -> None:
    for c, hs in cst["holders"].items():
        todo = hs if touched is None else [a for a in hs if a in touched]
        bals = cst["balances"].setdefault(c, {})
        for i in range(0, len(todo), 400):
            chunk = todo[i:i + 400]
            res = rpc.mq([(c, "balanceOf(address)", int(a, 16))
                          for a in chunk])
            for a, r in zip(chunk, res):
                v = _num(r)
                if v:
                    bals[a] = str(v)
                else:
                    bals.pop(a, None)


def find_yb_gauge(rpc: Rpc, cst: dict, lt: str, st: dict) -> str | None:
    """The LT's gauge = its biggest contract holder named 'YB Gauge…'."""
    if lt in st["yb_gauges"]:
        return st["yb_gauges"][lt]
    for a, _v in sorted(cst["balances"].get(lt, {}).items(),
                        key=lambda kv: -int(kv[1]))[:5]:
        if rpc.raw("eth_getCode", [a, "latest"]) != "0x":
            if erc20_name(rpc, a).startswith("YB Gauge"):
                st["yb_gauges"][lt] = a
                return a
    st["yb_gauges"][lt] = None
    return None


# ----------------------------------------------------------------- compose
def smart_label(st: dict, names: dict, chain: str, a: str,
                pks: set[str]) -> tuple[str, str]:
    """(label, kind): KNOWN actors > PegKeepers > the Curve registry
    (a holder that is itself a pool/LP/gauge) > cached erc20 name >
    plain wallet/contract."""
    if a in KNOWN:
        return KNOWN[a]
    if a in pks:
        return "crvUSD PegKeeper", "protocol"
    nm = names.get(chain, {}).get(a)
    if nm:
        return nm, "collective"
    if st["kinds"].get(a) == "EOA":
        return "", "user"
    return st["labels"].get(a, ""), "contract"


_TS: dict[str, int] = {}        # per-run totalSupply cache (batched)


def prefetch_supplies(rpcs: dict, pools: list[dict], ybm: dict) -> None:
    _TS.clear()
    by_ch: dict[str, set[str]] = {}
    for p in pools:
        yb = ybm.get(p["pool"]) if p["chain"] == "ethereum" else None
        toks = {p["lp"]}
        if yb:
            toks.add(yb["lt"])
        by_ch.setdefault(p["chain"], set()).update(toks)
    for ch, toks in by_ch.items():
        try:
            tl = sorted(toks)
            for i in range(0, len(tl), 300):
                chunk = tl[i:i + 300]
                res = rpcs[ch].mq([(t, "totalSupply()", None)
                                   for t in chunk])
                for t, r in zip(chunk, res):
                    v = _num(r)
                    if v:
                        _TS[t] = v
        except Exception as e:
            print(f"[lp] {ch}: supply prefetch failed ({str(e)[:60]})",
                  flush=True)


def total_supply(rpc: Rpc, token: str) -> int:
    if token not in _TS:
        try:
            _TS[token] = int(_call(rpc, token, "totalSupply()"), 16)
        except Exception:
            _TS[token] = 0
    return _TS[token]


def compose_pool(rpc: Rpc, st: dict, p: dict, cvx: dict,
                 pks: set[str], ybm: dict, names: dict) -> dict:
    chain = p["chain"]
    cst = chain_st(st, chain)
    yb = ybm.get(p["pool"]) if chain == "ethereum" else None
    lp = yb["lt"] if yb else p["lp"]
    gauge = (st["yb_gauges"].get(lp) if yb else p["gauge"])
    rw = cvx.get(p["lp"]) if chain == "ethereum" and not yb else None
    if rw and rw not in cst["holders"]:
        rw = None                       # not tracked (Convex share < 1%)
    ts = total_supply(rpc, lp)
    rows: dict[str, int] = {}
    for a, v in cst["balances"].get(lp, {}).items():
        if a in ((gauge or ""), LIDO_SR) or (yb and a == yb["amm"]):
            continue
        rows[a] = rows.get(a, 0) + int(v)
    if gauge:
        for a, v in cst["balances"].get(gauge, {}).items():
            if rw and a == CVX_PROXY:
                continue
            rows[a] = rows.get(a, 0) + int(v)
    if rw:
        for a, v in cst["balances"].get(rw, {}).items():
            rows[a] = rows.get(a, 0) + int(v)
    scale = 1.0
    if yb:
        # LT shares cover the AMM's slice of the raw pool LP (~100%)
        raw = cst["balances"].get(p["lp"], {})
        raw_ts = total_supply(rpc, p["lp"])
        amm = int(raw.get(yb["amm"], 0))
        scale = amm / raw_ts if raw_ts else 1.0
    total = sum(rows.values())
    coverage = total / ts * 100 if ts else 0.0
    ranked = sorted(rows.items(), key=lambda kv: -kv[1])
    # how many holders cover 95% of what we account for
    n95, cum = 0, 0
    for _a, v in ranked:
        cum += v
        n95 += 1
        if total and cum / total >= 0.95:
            break
    listed = ranked[:LIST_N]
    top = []
    for a, v in listed:
        pct = v / ts * 100 * scale if ts else 0.0
        label, kind = smart_label(st, names, chain, a, pks)
        top.append({"addr": a, "pct": round(pct, 3),
                    "usd": round(pct * p["tvl"] / 100),
                    "label": label, "kind": kind})
    return {"label": p["label"], "chain": chain, "tvl": round(p["tvl"]),
            "pool": p["pool"], "coins": p.get("coins") or [],
            "n_holders": len(rows), "n95": n95,
            "coverage_pct": round(min(100.0, coverage), 2),
            "rest_pct": round(max(0.0, 100 * scale
                                  - sum(r["pct"] for r in top)), 2),
            "top": top}


def main() -> None:
    import os
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text())
            os.kill(pid, 0)
            print(f"[lp] another instance is running (pid {pid}) — exiting")
            return
        except (ValueError, ProcessLookupError, PermissionError):
            pass                        # stale lock
    LOCK.parent.mkdir(exist_ok=True)
    LOCK.write_text(str(os.getpid()))
    try:
        _main_locked()
    finally:
        LOCK.unlink(missing_ok=True)


def _main_locked() -> None:
    st = load_state()
    pools, names = top_pools()
    eth = Rpc("ethereum")
    cvx = convex_map(eth)
    ybm = yb_map(eth)
    pks = pegkeepers()

    from fetch_markets import CHAIN_RPCS
    by_chain: dict[str, list[dict]] = {}
    skipped_chains: set[str] = set()
    for p in pools:
        if p["chain"] != "ethereum" and p["chain"] not in CHAIN_RPCS:
            skipped_chains.add(p["chain"])   # never guess an RPC
            continue
        by_chain.setdefault(p["chain"], []).append(p)
    if skipped_chains:
        print(f"[lp] no RPC configured for {sorted(skipped_chains)} — "
              "their pools are skipped", flush=True)
    pools = [p for p in pools if p["chain"] in by_chain]
    rpcs = {ch: (eth if ch == "ethereum" else Rpc(ch)) for ch in by_chain}
    seed_left = [SEED_BUDGET]
    cvx_left = [CVX_BUDGET]
    prefetch_supplies(rpcs, pools, ybm)   # one multicall batch per chain

    def publish_throttled(**kw):
        # serialized + rate-limited: parallel chains all want to publish
        with _IO_LOCK:
            if time.time() - _PUB_AT[0] < 10 and not kw.get("labels"):
                return
            _PUB_AT[0] = time.time()
        publish(rpcs, pools, st, cvx, pks, ybm, names, **kw)

    def chain_pass(ch):
      ps = by_chain[ch]
      try:
        rpc = rpcs[ch]
        cst = chain_st(st, ch)
        head = int(rpc.raw("eth_blockNumber", []), 16) - 2
        # incremental scan over what's already tracked
        touched = scan_chain(rpc, cst, head)
        # onboard anything new (pool promoted into the top-N, new gauge…)
        new: list[str] = []
        wanted: list[str] = []
        for p in ps:
            yb = ybm.get(p["pool"]) if ch == "ethereum" else None
            contracts = [p["lp"]]
            if yb:
                contracts.append(yb["lt"])
            if p["gauge"] and not yb:
                contracts.append(p["gauge"])
            ve = cst.setdefault("verified_empty", {})
            for c in contracts:
                if c in wanted:
                    continue
                if c not in cst["holders"]:
                    wanted.append(c)
                elif not cst["holders"][c] and not ve.get(c):
                    # zero holders on record but never windowed-verified —
                    # a possible silent gateway gap; re-seed it
                    wanted.append(c)
        if wanted and seed_left[0] > 0:
            take = wanted[:seed_left[0]]
            seed_left[0] -= len(take)
            GROUP = 20
            for i in range(0, len(take), GROUP):
                new += seed_chain_batch(rpc, ch, cst, take[i:i + GROUP],
                                        head,
                                        use_creation=(ch in (
                                            # chains whose first provider
                                            # answers historical getCode —
                                            # creation-block narrowing +
                                            # the longer scan deadline
                                            "ethereum", "polygon",
                                            "avalanche", "sonic")))
                write_state(st)          # checkpoint per group
                try:                     # and let the tab grow immediately
                    refresh_balances(rpc, cst, None)
                    publish_throttled()
                except Exception as e:
                    print(f"[lp] group publish failed ({str(e)[:50]})",
                          flush=True)
        refresh_balances(rpc, cst, None if new else touched)
        # YB gauges live in the LT holder set — discover, then seed them
        for p in ps:
            yb = ybm.get(p["pool"]) if ch == "ethereum" else None
            if yb:
                g = find_yb_gauge(rpc, cst, yb["lt"], st)
                if g and g not in cst["holders"] and seed_left[0] > 0:
                    seed_left[0] -= 1
                    if seed_contract(rpc, ch, cst, g, head):
                        new.append(g)
        # Convex reward pools where the proxy actually matters (>= 1%)
        if ch == "ethereum":
            pending_rw: list[str] = []
            for p in ps:
                rw = cvx.get(p["lp"])
                if not rw or ybm.get(p["pool"]):
                    continue
                ts = total_supply(rpc, p["lp"])
                g = p["gauge"]
                held = int(cst["balances"].get(g, {}).get(CVX_PROXY, 0)) \
                    if g else 0
                if ts and held / ts >= 0.01 and rw not in cst["holders"] \
                        and rw not in pending_rw and cvx_left[0] > 0:
                    cvx_left[0] -= 1
                    pending_rw.append(rw)
            if pending_rw:
                backfill_rewards_batch(rpc, cst, pending_rw, head, st)
                new += [rw for rw in pending_rw if rw in cst["holders"]]
        if new:
            refresh_balances(rpc, cst, None)
        elif touched:
            print(f"[lp] {ch}: {len(touched)} addresses touched", flush=True)
        write_state(st)
      except Exception as e:
        print(f"[lp] {ch}: chain pass FAILED ({str(e)[:80]}) — other "
              "chains continue", flush=True)
      try:
        publish_throttled()
      except Exception as e:
        print(f"[lp] interim publish failed ({str(e)[:60]})", flush=True)

    # chains hit DIFFERENT endpoints (tenderly gateways, Dwellir slugs,
    # public RPCs) — running them concurrently is pure wall-clock win
    order = sorted(by_chain, key=lambda c: c != "ethereum")
    with ThreadPoolExecutor(min(6, max(1, len(order)))) as ex:
        list(ex.map(chain_pass, order))

    publish(rpcs, pools, st, cvx, pks, ybm, names, labels=True, eth=eth)


def publish(rpcs, pools, st, cvx, pks, ybm, names,
            labels=False, eth=None) -> None:
    """Compose whatever is currently seeded and write lp.json. labels=True
    additionally resolves EOA/name labels for listed rows (final pass)."""
    # labels for listed rows only: EOA check, then erc20 name, threaded
    def safe_compose(p):
        try:
            return compose_pool(rpcs[p["chain"]], st, p, cvx, pks, ybm,
                                names)
        except Exception as e:
            print(f"[lp] compose failed for {p['label'][:30]} "
                  f"({str(e)[:60]})", flush=True)
            return None
    draft = {p["key"]: c for p in pools if (c := safe_compose(p))}
    need: list[tuple[str, str]] = []
    seen: set[str] = set()
    for p in pools:
        if not labels or p["key"] not in draft:
            continue
        for r in draft[p["key"]]["top"]:
            a = r["addr"]
            if (r["kind"] == "contract" and not r["label"] and a not in seen
                    and (a not in st["kinds"] or a not in st["labels"])):
                seen.add(a)
                need.append((p["chain"], a))
    if len(need) > LABEL_BUDGET:
        print(f"[lp] labeling {LABEL_BUDGET} of {len(need)} new addresses "
              "this run", flush=True)
        need = need[:LABEL_BUDGET]
    if need:
        def resolve(item):
            ch, a = item
            rpc = rpcs[ch]
            if rpc.raw("eth_getCode", [a, "latest"]) == "0x":
                return a, "EOA", ""
            return a, "contract", erc20_name(rpc, a)
        with ThreadPoolExecutor(8) as ex:
            for a, k, nm in ex.map(resolve, need):
                st["kinds"][a] = k
                st["labels"][a] = nm
        draft = {p["key"]: c for p in pools if (c := safe_compose(p))}
    write_state(st)
    OUT.write_text(json.dumps({"fetched_at": int(time.time()),
                               "pools": draft}))
    for v in draft.values():
        t = v["top"][0] if v["top"] else None
        print(f"[lp] {v['label'][:36]:36s} {v['n_holders']:5d} holders "
              f"cov {v['coverage_pct']:6.2f}%  top "
              f"{t['pct'] if t else 0:6.2f}%", flush=True)


if __name__ == "__main__":
    main()
