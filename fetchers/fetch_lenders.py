#!/usr/bin/env python3
"""fetch_lenders.py — who eats the bad debt: the lenders of each affected
market and the distribution of the loss across them.

For every market in data/baddebt.json with bad_debt_usd > 0, resolve its
ERC-4626 vault (Curve API), enumerate every address that ever received
vault shares (Transfer logs, scanned adaptively and cached incrementally
in data/lenders_scan.json), refresh their share balances, and fold gauge
stakers in (gauge deposit tokens are 1:1 with vault shares — the gauge's
own vault balance is replaced by its stakers, one level deep). Each
lender's pro-rata slice of the market's bad debt goes to
data/lenders.json for the Bad Debt tab.

    python3 fetchers/fetch_lenders.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE / "pylib"))
sys.path.insert(0, str(HERE / "sim"))
from fetch_markets import Rpc, _num, LEND_VAULTS_API  # noqa: E402

BAD = HERE / "data" / "baddebt.json"
SCAN = HERE / "data" / "lenders_scan.json"
OUT = HERE / "data" / "lenders.json"
TRANSFER = ("0xddf252ad1be2c89b69c2b068fc378daa"
            "952ba7f163c4a11628f55a4df523b3ef")   # keccak Transfer(a,a,u256)
ZERO = "0x" + "0" * 40
MAX_REQS = 800          # per-chain getLogs budget per run
TOP_N = 50              # lenders listed per market; the rest aggregate


def creation_block(rpc: Rpc, addr: str, head: int) -> int:
    """First block where the contract has code (binary search, ~30 calls)."""
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi) // 2
        if rpc.raw("eth_getCode", [addr, hex(mid)]) in ("0x", None):
            lo = mid + 1
        else:
            hi = mid
    return lo


def transfer_tos(rpc: Rpc, addrs: list[str], frm: int, to: int,
                 budget: list[int], step: int | None = None
                 ) -> dict[str, set[str]]:
    """Transfer recipients per contract for [frm, to] in one address-array
    getLogs pass, splitting the range when a provider rejects it (a stated
    "max(imum) block range N" is used directly). `step` forces a chunk size
    up front — for providers that answer over-long ranges with a silent
    empty list instead of an error (rpc.frax.com). budget[0] caps total
    requests; exhausting it raises — a partial holder set must never pass
    as complete."""
    if frm > to:
        return {}
    if step and to - frm + 1 > step:
        out: dict[str, set[str]] = {}
        for lo in range(frm, to + 1, step):
            part = transfer_tos(rpc, addrs, lo, min(lo + step - 1, to),
                                budget, step)
            for a, s in part.items():
                out.setdefault(a, set()).update(s)
        return out
    if budget[0] <= 0:
        raise RuntimeError("log-scan request budget exhausted")
    budget[0] -= 1
    try:
        logs = rpc.raw("eth_getLogs", [{
            "address": addrs, "topics": [TRANSFER],
            "fromBlock": hex(frm), "toBlock": hex(to)}])
        out: dict[str, set[str]] = {}
        for lg in logs:
            tp = lg.get("topics") or []
            if len(tp) >= 3 and tp[0] == TRANSFER:
                out.setdefault(lg["address"].lower(), set()).add(
                    "0x" + tp[2][-40:].lower())
        return out
    except Exception as e:
        if to - frm < 2:
            raise
        import re
        lim = re.search(r"max(?:imum)? block range:?\s*(\d+)", str(e))
        sub = (int(lim.group(1)) if lim and int(lim.group(1)) > 1
               else (to - frm + 1) // 2)
        out = {}
        for lo in range(frm, to + 1, sub):
            part = transfer_tos(rpc, addrs, lo,
                                min(lo + sub - 1, to), budget)
            for a, s in part.items():
                out.setdefault(a, set()).update(s)
        return out


def scan_holders(rpc: Rpc, addrs: list[str], head: int, cache: dict,
                 step: int | None = None) -> dict[str, set[str]]:
    """Ever-holders per contract via one shared chunked Transfer-log pass,
    incremental from each contract's cached last-scanned block. New
    contracts start at their creation block (binary-searched in parallel,
    cached)."""
    fresh = [a for a in addrs if cache.get(a, {}).get("last") is None
             and f"_created:{a}" not in cache]
    if fresh:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(8, len(fresh))) as ex:
            for a, c in zip(fresh, ex.map(
                    lambda a: creation_block(rpc, a, head), fresh)):
                cache[f"_created:{a}"] = c
    ents = {a: cache.setdefault(a, {"last": None, "holders": []})
            for a in addrs}
    frm = min(cache[f"_created:{a}"] if e["last"] is None else e["last"] + 1
              for a, e in ents.items())
    tos = transfer_tos(rpc, addrs, frm, head, [MAX_REQS], step)
    out = {}
    for a, e in ents.items():
        holders = set(e["holders"]) | tos.get(a, set())
        holders.discard(ZERO)
        e["holders"] = sorted(holders)
        e["last"] = head
        out[a] = holders
    return out


def balances(rpc: Rpc, token: str, addrs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(addrs), 400):
        chunk = addrs[i:i + 400]
        res = rpc.mq([(token, "balanceOf(address)", int(a, 16))
                      for a in chunk])
        for a, r in zip(chunk, res):
            v = _num(r)
            if v:
                out[a] = v
    return out


def main() -> None:
    bad = json.loads(BAD.read_text())
    req = urllib.request.Request(LEND_VAULTS_API,
                                 headers={"User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        vaults = json.loads(r.read()).get("data", {}) \
                                     .get("lendingVaultData", [])
    by_ctrl = {}
    for v in vaults:
        c = (v.get("controllerAddress") or "").lower()
        ch = (v.get("blockchainId") or "").lower()
        if c:
            by_ctrl[f"{ch}:{c}"] = {
                "vault": (v.get("address") or "").lower(),
                "gauge": (v.get("gaugeAddress") or "").lower() or None}

    try:
        scan = json.loads(SCAN.read_text())
    except (OSError, ValueError):
        scan = {}
    out_markets: dict[str, dict] = {}
    by_chain: dict[str, list[tuple[str, dict]]] = {}
    for key, m in bad.get("markets", {}).items():
        if (m.get("bad_debt_usd") or 0) > 0:
            by_chain.setdefault(m["chain"], []).append((key, m))
    # biggest bad debt first, so a chain with throttled providers (the OP
    # public RPCs) can only ever delay smaller markets, not block bigger
    chains = sorted(by_chain, key=lambda ch: -sum(
        m.get("bad_debt_usd") or 0 for _, m in by_chain[ch]))
    for chain in chains:
        items = by_chain[chain]
        # one shared chunked log pass per chain covers every vault + gauge
        todo = []
        for key, m in items:
            entry: dict = {"market": m["market"], "chain": chain}
            out_markets[key] = entry
            va = by_ctrl.get(key)
            if not va or not va["vault"]:
                entry["error"] = "no vault found for this controller"
                continue
            todo.append((entry, m, va["vault"], va["gauge"]))
        if not todo:
            continue
        # a chain whose scan failed sits out 2 h — no point burning the
        # request budget against rate-limited providers every 30 min
        if (scan.get(chain) or {}).get("_retry_after", 0) > time.time():
            for entry, *_ in todo:
                entry["error"] = "provider backoff — retrying later"
            continue
        try:
            rpc = Rpc(chain)
            head = int(rpc.raw("eth_blockNumber", []), 16) - 2
            ch_scan = scan.setdefault(chain, {})
            contracts = sorted({c for _, _, v, g in todo
                                for c in (v, g) if c})
        except Exception as e:
            for entry, *_ in todo:
                entry["error"] = str(e)[:200]
            print(f"[lenders] {chain}: scan FAILED {e}", flush=True)
            continue
        # attempt 1: provider-negotiated ranges. If a market's balances
        # don't account for its vault supply, the provider silently dropped
        # history (rpc.frax.com does) — rescan in forced 50k chunks.
        for step in (None, 50_000):
            try:
                holders_map = scan_holders(rpc, contracts, head, ch_scan,
                                           step)
            except Exception as e:
                for entry, *_ in todo:
                    entry["error"] = str(e)[:200]
                print(f"[lenders] {chain}: scan FAILED {e}", flush=True)
                for a in contracts:      # rescan after the backoff
                    ch_scan.pop(a, None)
                ch_scan["_retry_after"] = time.time() + 7200
                break
            process_chain(rpc, todo, holders_map)
            low = [e2 for e2, *_ in todo
                   if e2.get("coverage_pct", 100) < 50]
            if not low:
                ch_scan.pop("_retry_after", None)
                break
            for a in contracts:
                ch_scan.pop(a, None)
            if step is None:
                print(f"[lenders] {chain}: low coverage — provider "
                      "dropped history, retrying in 50k chunks", flush=True)
            else:
                for e2 in low:
                    e2["error"] = ("incomplete log scan — the provider "
                                   "drops history silently")
                ch_scan["_retry_after"] = time.time() + 7200
                break
        SCAN.write_text(json.dumps(scan))
    SCAN.write_text(json.dumps(scan))
    OUT.write_text(json.dumps({
        "fetched_at": int(time.time()),
        "note": "pro-rata over vault shares; gauge stakers resolved one "
                "level; contract holders (e.g. wrappers) shown as-is",
        "markets": out_markets}))
    print(f"[lenders] wrote {OUT.name}: {len(out_markets)} markets")


def process_chain(rpc: Rpc, todo: list, holders_map: dict) -> None:
    for entry, m, vault, gauge in todo:
        chain = entry["chain"]
        entry.pop("error", None)
        entry.pop("coverage_pct", None)
        try:
            direct = balances(rpc, vault, sorted(holders_map[vault]))
            res = rpc.mq([(vault, "totalSupply()", None),
                          (vault, "totalAssets()", None)])
            tot_sup, tot_assets = _num(res[0]), _num(res[1])
            if not tot_sup:
                entry["error"] = "vault totalSupply is zero"
                continue
            # integrity: the found balances must account for the supply
            coverage = sum(direct.values()) / tot_sup
            if coverage < 0.999:
                entry["coverage_pct"] = round(coverage * 100, 2)
            staked: dict[str, int] = {}
            if gauge and gauge in direct:
                # the gauge's vault shares belong to its stakers 1:1
                staked = balances(rpc, gauge,
                                  sorted(holders_map.get(gauge, set())))
                del direct[gauge]
            shares: dict[str, list] = {}      # addr -> [direct, via_gauge]
            for a, v in direct.items():
                shares.setdefault(a, [0, 0])[0] += v
            for a, v in staked.items():
                shares.setdefault(a, [0, 0])[1] += v
            usd = m.get("borrowed_usd") or 1.0
            bd_usd = m["bad_debt_usd"]
            rows = []
            for a, (d, g) in shares.items():
                sh = d + g
                frac = sh / tot_sup
                rows.append({
                    "addr": a, "via_gauge": g > d,
                    "share_pct": round(frac * 100, 2),
                    "supplied_usd": round(frac * (tot_assets or 0)
                                          / 1e18 * usd, 2),
                    "loss_usd": round(frac * bd_usd, 2)})
            rows.sort(key=lambda r: -r["loss_usd"])
            entry.update({
                "vault": vault, "gauge": gauge,
                "n_lenders": len(rows),
                "supplied_total_usd": round((tot_assets or 0)
                                            / 1e18 * usd, 2),
                "lenders": rows[:TOP_N]})
            if len(rows) > TOP_N:
                rest = rows[TOP_N:]
                entry["others"] = {
                    "n": len(rest),
                    "loss_usd": round(sum(r["loss_usd"] for r in rest), 2)}
            print(f"[lenders] {chain} {m['market']}: "
                  f"{len(rows)} lenders, top loss "
                  f"${rows[0]['loss_usd']:,.0f}" if rows else
                  f"[lenders] {chain} {m['market']}: no lenders",
                  flush=True)
        except Exception as e:
            entry["error"] = str(e)[:200]
            print(f"[lenders] {chain} {m['market']}: FAILED {e}",
                  flush=True)


if __name__ == "__main__":
    main()
