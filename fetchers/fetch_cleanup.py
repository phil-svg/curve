#!/usr/bin/env python3
"""fetch_cleanup.py — position scan over EVERY market, all chains. Emits two
datasets from one pass:

  data/cleanup.json   Spring-Cleaning: markets whose max opening LTV (N=4) is
                      under 60% and that still hold collateral or debt, with
                      every open position. USD-rounded TVL is NOT the filter —
                      a market holding 0.47 UwU against $54k of debt rounds to
                      $0 TVL and must still appear.

  data/baddebt.json   Bad Debt: every market (any LTV tier, LLV1 + LLV2, all
                      chains) where some position's debt exceeds its backing
                      (y at the oracle price + soft-liquidated x), with the
                      underwater positions listed. Marking follows the AMM
                      oracle, same convention as the bad-debt-over-time chart.

Pure multicalls (loans(i) + user_state per market) — no log scans — so the
whole run fits the UI's hourly refresh cycle.

    python3 fetch_cleanup.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "pylib"))
from fetch_markets import Rpc, _addr  # noqa: E402

MARKETS = HERE / "data" / "markets.json"
OUT_CLEAN = HERE / "data" / "cleanup.json"
OUT_BAD = HERE / "data" / "baddebt.json"

MAX_LTV_PCT = 60.0     # Spring-Cleaning candidate threshold
BAD_MIN_USD = 1.0      # ignore sub-$1 rounding dust


def max_ltv(A, ld, N=4):
    if not A or A < 2 or ld is None:
        return None
    r = (A - 1) / A
    G = A * (1 - r ** N) / (math.sqrt(A / (A - 1)) * N)
    return max(0.0, (1 - ld / 100) * G * (1 - 1 / (2 * A)) * (1 - 1e-4))


def scan_positions(rpc: Rpc, m: dict) -> list[dict]:
    """Every open position of one market: loans(i) then user_state, two
    chunked multicalls. Backing = y at the oracle USD price + x in borrowed
    USD; underwater = debt above backing."""
    ctrl = m["controller"].lower()
    n = rpc.num(ctrl, "n_loans()") or 0
    if not n:
        return []
    rows = rpc.mq([(ctrl, "loans(uint256)", i) for i in range(n)])
    users = [(_addr(r) or "").lower() for r in rows if r]
    users = [u for u in users if int(u or "0x0", 16)]
    if not users:
        return []
    st = rpc.mq([(ctrl, "user_state(address)", int(u, 16)) for u in users])
    cd = m["collateral"]["decimals"]
    bd = m["borrowed"]["decimals"]
    px = m.get("collateral_usd_price") or 0
    b_usd = m.get("borrowed_usd") or 0
    out = []
    for u, r in zip(users, st):
        if not r or len(r) < 2 + 64 * 4:
            continue
        w = [int(r[2:][k * 64:(k + 1) * 64], 16) for k in range(4)]
        y = w[0] / 10 ** cd
        x = w[1] / 10 ** bd
        debt = w[2] / 10 ** bd
        if debt <= 0 and y <= 0 and x <= 0:
            continue
        backing = y * px + x * b_usd
        debt_usd = debt * b_usd
        out.append({
            "addr": u, "y": y, "y_usd": round(y * px, 2),
            "x_usd": round(x * b_usd, 2), "debt_usd": round(debt_usd, 2),
            "backing_usd": round(backing, 2),
            "underwater": debt_usd > backing,
            "shortfall_usd": round(max(0.0, debt_usd - backing), 2),
        })
    return out


def market_head(m: dict, grp: str, ltv) -> dict:
    return {
        "group": grp, "chain": m["chain"],
        "market": f"{m['collateral']['symbol']}/{m['borrowed']['symbol']}",
        "controller": m["controller"].lower(), "amm": m["amm"],
        "max_ltv_pct": round(ltv * 100, 1) if ltv is not None else None,
        "loan_discount_pct": m["loan_discount_pct"],
        "liquidation_discount_pct": m["liquidation_discount_pct"],
        "llamma_A": m["llamma_A"],
        "amm_rate_apr_pct": m.get("amm_rate_apr_pct"),
        "oracle_price": m.get("collateral_usd_price"),
        "tvl_usd": m.get("market_tvl_usd"),
        "debt_usd": m.get("total_debt_usd"),
        "n_loans": m.get("n_loans"),
    }


def main() -> None:
    M = json.loads(MARKETS.read_text())
    rpcs: dict[str, Rpc] = {}
    clean: dict[str, dict] = {}
    bad: dict[str, dict] = {}
    t_start = time.time()

    for grp in ("LLV1", "LLV2"):
        for m in M["groups"][grp]:
            ltv = max_ltv(m["llamma_A"], m["loan_discount_pct"])
            holds = (m.get("collateral_tokens") or 0) > 0 or (m.get("total_debt") or 0) > 0
            candidate = (ltv is not None and ltv * 100 < MAX_LTV_PCT and holds)
            if not holds and not candidate:
                continue                     # empty market, nothing to scan
            rpc = rpcs.setdefault(m["chain"], Rpc(m["chain"]))
            positions = scan_positions(rpc, m)
            key = f"{m['chain']}:{m['controller'].lower()}"
            uw = [p for p in positions if p["underwater"]
                  and p["shortfall_usd"] >= BAD_MIN_USD]
            bad_usd = round(sum(p["shortfall_usd"] for p in uw), 2)
            if candidate:
                clean[key] = dict(market_head(m, grp, ltv), user_rows=positions)
            if uw:
                bad[key] = dict(market_head(m, grp, ltv),
                                bad_debt_usd=bad_usd,
                                n_positions=len(positions),
                                n_underwater=len(uw),
                                worst_shortfall_usd=max(p["shortfall_usd"] for p in uw),
                                underwater=sorted(uw, key=lambda p: -p["shortfall_usd"]))
            if candidate or uw:
                print(f"{grp} {m['chain']:<9} "
                      f"{m['collateral']['symbol']}/{m['borrowed']['symbol']:<16} "
                      f"pos={len(positions)} uw={len(uw)} bad=${bad_usd:,.0f}"
                      f"{'  [cleanup]' if candidate else ''}", flush=True)

    stamp = {"fetched_at": int(time.time()),
             "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    for path, payload in (
            (OUT_CLEAN, dict(stamp, criterion=f"max opening LTV < {MAX_LTV_PCT:.0f}% "
                             "and still holds collateral or debt", markets=clean)),
            (OUT_BAD, dict(stamp, criterion="any position with debt above backing "
                           "(y at oracle price + soft-liq x), marked at the AMM oracle",
                           markets=bad))):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, path)
    total = sum(v["bad_debt_usd"] for v in bad.values())
    print(f"wrote {OUT_CLEAN} ({len(clean)}) + {OUT_BAD} ({len(bad)}, "
          f"${total:,.0f} total)  {time.time()-t_start:.0f}s")


if __name__ == "__main__":
    main()
