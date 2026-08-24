"""Standalone Py+C++ sweep — reproduces chart1/chart3 badDebt time series.

Pipeline:
  1. Read precompute file (produced by cpp/build/sweep_precompute) →
     per-block candidate lists (user, x, y, debt).
  2. For each block, in the order the candidates were emitted:
       a) fetch block info (baseFeePerGas + timestamp),
       b) fetch ethPrice via curve pool.get_dy(1, 0, 1e18),
       c) filter fresh = candidates - settled(this_discount), drop dust,
       d) profit-test fresh (concurrent RPC), add profitable → settled,
       e) fetch Controller.users_to_liquidate(),
       f) fetch crvPrice via curve pool.get_dy(2, 0, 1e18),
       g) badDebt = sum(debt) - (sum(x) + crvPrice*sum(y)) over remaining.
  3. Emit chart JSON in the exact schema plot_all.py / plot_cpp.py consume.

Runs one --discount per invocation but supports a comma list; each discount
uses the corresponding precompute file. Multiple discounts run sequentially
because the sweep TS ref does that too — mirroring the TS ref shape lets
diffing be trivial.

Usage:
  python run_sweep.py \
      --controller 0xEdA...110A --precompute PRE_MAP \
      --from-block 23549898 --to-block 23550007 \
      --oracle-label curve --out chart1.json

  PRE_MAP: 8:path,12:path,16:path,20:path
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from common import block_info, eth_call, sel
from profit_test import (
    profit_test,
    eth_price_at,
    pool_get_dy,
)


def _addr(w: str) -> str:
    return "0x" + w[-40:].lower()


def decode_users_to_liquidate(hex_body: str) -> list[dict]:
    """Decode tuple[] with static tuple = (address, uint, uint, uint, int256).
    Layout: [offset=0x20] [length=N] [N × 160 bytes of packed static tuples]."""
    body = hex_body[2:] if hex_body.startswith("0x") else hex_body
    # word 0: offset (0x20). word 1: N.
    N = int(body[64:128], 16)
    out = []
    base = 128  # start of first element
    for i in range(N):
        rec = body[base + i * 320: base + (i + 1) * 320]
        # rec is 5 words × 64 hex chars = 320
        user = _addr(rec[0:64])
        x = int(rec[64:128], 16)
        y = int(rec[128:192], 16)
        debt = int(rec[192:256], 16)
        health = int(rec[256:320], 16)
        if health >= (1 << 255):
            health -= 1 << 256
        out.append({"user": user, "x": x, "y": y, "debt": debt, "health": health})
    return out


def users_to_liquidate(controller: str, block: int) -> list[dict]:
    data = sel("users_to_liquidate()")
    res = eth_call(controller, data, block)
    return decode_users_to_liquidate(res)


def load_precompute(path: str) -> dict[int, list[dict]]:
    arr = json.loads(Path(path).read_text())
    return {int(row["block"]): row["candidates"] for row in arr}


def hhmm_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def sweep(
    controller: str,
    from_block: int, to_block: int,
    discount_to_precompute: dict[int, str],
    profit_concurrency: int = 30,
    log_every: int = 10,
) -> tuple[list[dict], dict[str, float]]:
    """Run the sweep and return (rows, timings_ms)."""
    timings: dict[str, float] = {
        "sweep:profit-tests":         0.0,
        "sweep:users_to_liquidate":   0.0,
        "sweep:block-info":           0.0,
        "sweep:price-crv":            0.0,
        "sweep:price-eth":            0.0,
        "wall":                       0.0,
    }
    t_wall = time.time()

    rows: list[dict] = []

    # Prices cached per block (each block: crvPrice, ethPrice) — reused across discounts.
    price_cache: dict[int, tuple[float, float]] = {}
    block_cache: dict[int, tuple[int, int]] = {}  # ts, baseFeePerGas

    for discount in sorted(discount_to_precompute):
        pre_path = discount_to_precompute[discount]
        print(f"[sweep] loading precompute for discount={discount}%: {pre_path}", flush=True)
        precompute = load_precompute(pre_path)

        settled: set[str] = set()
        n = to_block - from_block + 1
        for i in range(n):
            b = from_block + i

            # Cache block info + prices.
            if b not in block_cache:
                t = time.time()
                bi = block_info(b)
                timings["sweep:block-info"] += (time.time() - t) * 1000
                block_cache[b] = (bi["timestamp"], bi["baseFeePerGas"])
            ts, base_fee = block_cache[b]

            if b not in price_cache:
                t = time.time()
                eth_p = eth_price_at(b)
                timings["sweep:price-eth"] += (time.time() - t) * 1000
                t = time.time()
                crv_p = pool_get_dy(2, 0, 10**18, b) / 1e18
                timings["sweep:price-crv"] += (time.time() - t) * 1000
                price_cache[b] = (crv_p, eth_p)
            crv_price, eth_price = price_cache[b]

            candidates = precompute.get(b, [])
            fresh = [
                c for c in candidates
                if c["user"] not in settled
                and (int(c["y"]) / 1e18) > 10
                and (int(c["debt"]) / 1e18) > 10
            ]

            # Profit test each fresh candidate (concurrent).
            profitable_count = 0
            if fresh:
                t = time.time()
                def _test(pos):
                    try:
                        return profit_test(
                            b, base_fee, eth_price,
                            int(pos["y"]), int(pos["debt"]), int(pos["x"]),
                        )
                    except Exception as e:
                        print(f"[profit err] {pos['user']} {e}", flush=True)
                        return 0.0
                with ThreadPoolExecutor(max_workers=profit_concurrency) as pool:
                    profits = list(pool.map(_test, fresh))
                for idx, p in enumerate(profits):
                    if p > 0:
                        settled.add(fresh[idx]["user"].lower())
                        profitable_count += 1
                timings["sweep:profit-tests"] += (time.time() - t) * 1000

            # Fetch on-chain users_to_liquidate for this block.
            t = time.time()
            u2l = users_to_liquidate(controller, b)
            timings["sweep:users_to_liquidate"] += (time.time() - t) * 1000

            remaining = [p for p in u2l if p["user"].lower() not in settled]
            debt_sum = sum(p["debt"] / 1e18 for p in remaining)
            x_sum    = sum(p["x"]    / 1e18 for p in remaining)
            y_sum    = sum(p["y"]    / 1e18 for p in remaining)
            collat_value_usd = crv_price * y_sum
            bad_debt = round(debt_sum - (x_sum + collat_value_usd))

            rows.append({
                "blockNumber": b,
                "LiquidationDiscount": discount,
                "timestamp": int(ts),
                "date": hhmm_utc(ts),
                "badDebt": bad_debt,
            })

            if (i + 1) % log_every == 0 or i == n - 1:
                pct = (i + 1) / n * 100
                print(f"{discount}% {i+1}/{n} ({pct:.1f}%) {b} "
                      f"candidates={len(candidates)} fresh={len(fresh)} "
                      f"profitable={profitable_count} settled={len(settled)} "
                      f"badDebt={bad_debt}", flush=True)

    timings["wall"] = (time.time() - t_wall) * 1000
    return rows, timings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", required=True)
    ap.add_argument("--precompute-map", required=True,
                    help="d1:path1,d2:path2,… — one precompute file per discount")
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    ap.add_argument("--profit-concurrency", type=int, default=30)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timings-out", type=Path, default=None)
    args = ap.parse_args()

    disc_map: dict[int, str] = {}
    for kv in args.precompute_map.split(","):
        d_str, p = kv.split(":", 1)
        disc_map[int(d_str)] = p

    rows, timings = sweep(
        args.controller,
        args.from_block, args.to_block,
        disc_map,
        profit_concurrency=args.profit_concurrency,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2))
    print(f"[sweep] wrote {args.out}  ({len(rows)} rows)")
    if args.timings_out:
        args.timings_out.write_text(json.dumps(timings, indent=2))
        print(f"[sweep] wrote timings -> {args.timings_out}")
    print(f"[sweep] wall = {timings['wall']/1000:.2f}s")


if __name__ == "__main__":
    main()
