"""Fetch REAL on-chain-measured bad debt per block.

At each block b, call `Controller.users_to_liquidate()` — the on-chain view
returning every underwater loan (users with health < 0 at the AMM's current
oracle-derived pricing). For each returned tuple (user, x, y, debt, health),
compute the shortfall using the same formula the real sim uses:

    shortfall_i = max(0, debt_i - x_i - crv_spot(b) × y_i)

Sum across returned users → the "no one hard-liquidates" bad debt at block b.
This is the raw on-chain reality — nothing simulated, no profit tests,
no settled tracking. Used as a third overlay line alongside the real-sim and
artificial-sim curves.
"""
from __future__ import annotations
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))

from common import eth_call, sel

CTRL = "0xEdA215b7666936DEd834f76f3fBC6F323295110A"
FROM_BLOCK = 23_549_898
TO_BLOCK   = 23_550_007
HERE = Path(__file__).resolve().parent.parent
REAL_SPOT_FILE = Path(__file__).resolve().parent.parent / "external" / "chart2_prices.json"


def decode_u2l(hex_body: str) -> list[dict]:
    """Decode tuple[]: (address user, uint x, uint y, uint debt, int256 health)."""
    body = hex_body[2:] if hex_body.startswith("0x") else hex_body
    n = int(body[64:128], 16)
    out = []
    base = 128
    for i in range(n):
        rec = body[base + i * 320: base + (i + 1) * 320]
        h = int(rec[256:320], 16)
        if h >= (1 << 255): h -= 1 << 256
        out.append({
            "user":  "0x" + rec[24:64].lower(),
            "x":     int(rec[64:128], 16),
            "y":     int(rec[128:192], 16),
            "debt":  int(rec[192:256], 16),
            "health": h,
        })
    return out


def u2l_at(b: int) -> list[dict]:
    r = eth_call(CTRL, sel("users_to_liquidate()"), b)
    return decode_u2l(r)


def main():
    spot_by_block = {r["block"]: r["spot"] for r in json.loads(REAL_SPOT_FILE.read_text())}
    dates_by_block = {r["block"]: r["date"] for r in json.loads(REAL_SPOT_FILE.read_text())}
    ts_by_block = {r["block"]: r["ts"] for r in json.loads(REAL_SPOT_FILE.read_text())}
    blocks = list(range(FROM_BLOCK, TO_BLOCK + 1))
    print(f"[u2l] fetching {len(blocks)} blocks concurrently")
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(u2l_at, blocks))
    print(f"[u2l] all fetched in {time.time()-t0:.1f}s")

    for b, users in zip(blocks, results):
        spot = spot_by_block[b]
        debt_sum = sum(u["debt"] for u in users) / 1e18
        x_sum    = sum(u["x"]    for u in users) / 1e18
        y_sum    = sum(u["y"]    for u in users) / 1e18
        collat_value = spot * y_sum
        bad_debt = round(debt_sum - (x_sum + collat_value))
        rows.append({
            "blockNumber": b,
            "timestamp": ts_by_block[b],
            "date": dates_by_block[b],
            "n_u2l": len(users),
            "onchain_bad_debt": bad_debt,
            "real_spot": spot,
            # Full user list — used by the artificial sim as its bad-debt basis
            # (aligns metric with real sim which also uses on-chain u2l).
            "users": [{"user": u["user"], "x": str(u["x"]),
                       "y": str(u["y"]), "debt": str(u["debt"])} for u in users],
        })

    out = HERE / "results" / "onchain_measured_bad_debt.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"[u2l] wrote {out}")
    for i in [0, 20, 40, 60, 80, 109]:
        r = rows[i]
        print(f"  block {r['blockNumber']} ({r['date']})  n_u2l={r['n_u2l']:>3}  "
              f"onchain_bad_debt=${r['onchain_bad_debt']:>10,}   real_spot=${r['real_spot']:.4f}")


if __name__ == "__main__":
    main()
