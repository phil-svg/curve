#!/usr/bin/env python3
"""fetch_minute_series.py — 1-minute venue candles for one collateral over
the last N hours from the Curve prices API, saved as Binance-style klines
(+ .meta.json) in the layout sweep_ref_table.py / ref_model consume.

    python3 fetch_minute_series.py --symbol WETH --chain ethereum \\
        --pool 0x7f86... --quote 0xa0b8... --base 0xc02a... --span-h 4380 \\
        --pool-name "TricryptoUSDC (WETH/USDC)"

Output: data/_ref_table_klines_<SYMBOL>_<span>h.json  [t_ms,o,h,l,c,0]
        data/_ref_table_klines_<SYMBOL>_<span>h.meta.json
Resumable: progress is checkpointed to <out>.partial.json every 50 chunks.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_crash_window as fcw  # noqa: E402  (shared _get + API + CHUNK_S)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--chain", default="ethereum")
    ap.add_argument("--pool", required=True)
    ap.add_argument("--quote", required=True, help="main_token (quote) address")
    ap.add_argument("--base", required=True, help="reference_token (collateral) address")
    ap.add_argument("--span-h", type=int, default=4380)
    ap.add_argument("--pool-name", default="")
    ap.add_argument("--sleep", type=float, default=0.15)
    args = ap.parse_args()

    out = HERE / "data" / f"_ref_table_klines_{args.symbol}_{args.span_h}h.json"
    partial = out.with_suffix(".partial.json")
    end = int(time.time()) // 60 * 60
    start = end - args.span_h * 3600
    got: dict[int, list] = {}
    t = start
    if partial.exists():
        st = json.loads(partial.read_text())
        got = {int(k): v for k, v in st["got"].items()}
        t = st["next_t"]
        print(f"[resume] {len(got)} candles, continuing from {t}", flush=True)
    n_chunks = (end - start + fcw.CHUNK_S - 1) // fcw.CHUNK_S
    done = (t - start) // fcw.CHUNK_S
    t0 = time.time()
    while t < end:
        e = min(t + fcw.CHUNK_S, end)
        j = fcw._get(fcw.API.format(chain=args.chain, pool=args.pool.lower())
                     + f"?main_token={args.quote}&reference_token={args.base}"
                     + f"&agg_number=1&agg_units=minute&start={t}&end={e}")
        for c in (j or {}).get("data") or []:
            got[c["time"]] = [c["open"], c["high"], c["low"], c["close"]]
        t = e
        done += 1
        if done % 50 == 0:
            partial.write_text(json.dumps({"got": got, "next_t": t}))
            rate = (time.time() - t0) / max(1, done - ((partial.exists() and 0) or 0))
            print(f"  {args.symbol}: {done}/{n_chunks} chunks, {len(got)} candles, "
                  f"{time.time() - t0:.0f}s", flush=True)
        time.sleep(args.sleep)
    ts = sorted(got)
    if len(ts) < 1000:
        raise SystemExit(f"too sparse: {len(ts)} candles")
    rows = [[t_ * 1000, got[t_][0], got[t_][1], got[t_][2], got[t_][3], 0.0] for t_ in ts]
    out.write_text(json.dumps(rows))
    out.with_suffix(".meta.json").write_text(json.dumps({
        "symbol": args.symbol, "pool_name": args.pool_name or args.pool,
        "from": ts[0], "to": ts[-1], "n": len(rows),
        "span_h": round((ts[-1] - ts[0]) / 3600, 1)}))
    partial.unlink(missing_ok=True)
    print(f"{args.symbol}: {len(rows)} candles {time.strftime('%Y-%m-%d', time.gmtime(ts[0]))} -> "
          f"{time.strftime('%Y-%m-%d', time.gmtime(ts[-1]))} saved to {out.name} "
          f"({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
