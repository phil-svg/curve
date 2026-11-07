#!/usr/bin/env python3
"""Resupply tab: the crvUSD Resupply supplied to LlamaLend that is not lent out.

Resupply's crvUSD pairs (from its registry) hold LlamaLend vault shares as
collateral. In each vault, Resupply's part of the unborrowed crvUSD is

    shares held by the pair / vault totalSupply * (totalAssets - total_debt)

summed over the pairs of that vault. Sampled every 7200 blocks (about a day)
from Resupply's first block, plus the head; each run only adds the blocks it
has not sampled yet. One multicall per sampled block.

Output: data/resupply.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_markets import Rpc, _num, _addr, MULTICALL3  # noqa: E402

OUT = HERE / "data" / "resupply.json"
REGISTRY = "0x10101010E0C3171D894B71B3400668aF311e7D94"
CRVUSD = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
START = 22_034_863      # Resupply's first block
STEP = 7200             # blocks between samples, about a day


def markets(rpc: Rpc) -> list[dict]:
    """The LlamaLend vaults Resupply's crvUSD pairs hold, with their pairs."""
    r = rpc.q(REGISTRY, "getAllPairAddresses()")
    b = r[2:]
    off = int(b[:64], 16) * 2
    n = int(b[off:off + 64], 16)
    pairs = ["0x" + b[off + 64 + i * 64 + 24: off + 64 + (i + 1) * 64]
             for i in range(n)]
    try:
        names = {k.split(":")[1]: m.get("name") for k, m in json.loads(
            (HERE / "data" / "llm.json").read_text())["markets"].items()}
    except (OSError, ValueError, KeyError):
        names = {}
    out: dict[str, dict] = {}
    for p in pairs:
        vault = _addr(rpc.q(p, "collateral()"))
        # crvUSD pairs on LlamaLend only: the vault lends crvUSD and has a
        # controller (Fraxlend collateral has neither)
        if not vault or (_addr(rpc.q(vault, "asset()")) or "") != CRVUSD:
            continue
        ctrl = _addr(rpc.q(vault, "controller()"))
        if not ctrl or int(ctrl, 16) == 0:
            continue
        name = rpc.string(p, "name()") or ""
        m = out.setdefault(vault, {
            "vault": vault, "controller": ctrl, "v2": "CurveLendV2" in name,
            "name": names.get(ctrl) or (name.split("crvUSD/")[-1]
                                        .split(")")[0] + "/crvUSD"),
            "pairs": []})
        m["pairs"].append(p)
    return list(out.values())


def calls_for(mk: list[dict]) -> list:
    calls = [(MULTICALL3, "getCurrentBlockTimestamp()", None)]
    for m in mk:
        calls += [(m["vault"], "totalAssets()", None),
                  (m["vault"], "totalSupply()", None),
                  (m["controller"], "total_debt()", None)]
        calls += [(p, "totalCollateral()", None) for p in m["pairs"]]
    return calls


def sample(mk: list[dict], calls: list, block: int):
    """(timestamp, {vault: (supplied, idle)}) at one block, crvUSD units;
    None when the call failed. A vault or pair not deployed yet counts 0."""
    try:
        res = Rpc("ethereum").mq(calls, block=hex(block))
    except Exception:
        return None
    ts = _num(res[0])
    if not ts:
        return None
    out, i = {}, 1
    for m in mk:
        ta, sup, debt = (_num(res[i + k]) or 0 for k in range(3))
        i += 3
        shares = sum(_num(res[i + k]) or 0 for k in range(len(m["pairs"])))
        i += len(m["pairs"])
        frac = shares / sup if sup else 0.0
        out[m["vault"]] = (frac * ta / 1e18,
                           frac * max(ta - debt, 0) / 1e18)
    return ts, out


def main() -> None:
    t0 = time.time()
    rpc = Rpc("ethereum")
    head = int(rpc.raw("eth_blockNumber", []), 16)
    mk = markets(rpc)
    calls = calls_for(mk)
    try:
        old = json.loads(OUT.read_text())
    except (OSError, ValueError):
        old = {}
    # a new vault or pair means a rebuild: the stored rows lack its columns
    same = [(m["vault"], m["pairs"]) for m in old.get("markets") or []] \
        == [(m["vault"], m["pairs"]) for m in mk]
    # stored rows on the sample grid; the last run's head row is dropped
    rows = {b: r for b, *r in (old.get("rows") or [])
            if (b - START) % STEP == 0} if same else {}
    grid = [b for b in range(START, head, STEP) if b not in rows]
    with ThreadPoolExecutor(8) as ex:
        got = dict(zip(grid, ex.map(lambda b: sample(mk, calls, b), grid)))
    for b in [b for b, r in got.items() if r is None]:
        got[b] = sample(mk, calls, b)       # one sequential retry pass
    for b, r in got.items():
        if r:
            rows[b] = [r[0]] + [[round(r[1][m["vault"]][0]),
                                 round(r[1][m["vault"]][1])] for m in mk]
    missing = sum(1 for r in got.values() if not r)
    now = sample(mk, calls, head)
    out = {"fetched_at": int(time.time()), "head": head, "step": STEP,
           "markets": mk,
           # [block, timestamp, [supplied, idle] per market], oldest first;
           # the head row comes last and is replaced on every run
           "rows": [[b] + rows[b] for b in sorted(rows)]}
    if now:
        out["rows"].append([head, now[0]] + [
            [round(now[1][m["vault"]][0]), round(now[1][m["vault"]][1])]
            for m in mk])
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    os.replace(tmp, OUT)
    last = out["rows"][-1] if out["rows"] else None
    idle = sum(c[1] for c in last[2:]) if last else 0
    print(f"[resupply] {len(mk)} markets, {len(out['rows'])} samples "
          f"(+{len(grid)} new, {missing} failed), unborrowed now "
          f"{idle / 1e6:.2f}M crvUSD, {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
