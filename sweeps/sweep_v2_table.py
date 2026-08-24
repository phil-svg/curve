#!/usr/bin/env python3
"""sweep_v2_table.py — S.L./D.L. exact-tail table on the llamma-simulator_v2
model (cpp/build/ref_model_v2 --auto), author's ZCHF/crvUSD data + crvUSD/USD
aggregate, oracle EMA half-life --texp (gov recipe: 3603 s), dyn-fee mult
0.25, on-chain oracle limiter. Same output schema as sweep_ref_table.py so
the S.L./D.L. tab renders it unchanged.

    python3 sweep_v2_table.py --a-min 110 --a-max 208 --fee-min 0.015
        --fee-max 2.919 --grid 10 --loan-days 1 --texp 3603
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
V2_REPO = HERE / "llamma-simulator_v2"
BIN = HERE / "bin" / "ref_model_v2"
CACHE = HERE / "data" / "_ref_v2"
EXT_FEE = 5e-4
DYN_MULT = 0.25

from sweep_sl_dl import spread, _prog, _prog_phase, _prog_tick  # noqa: E402
from sweep_ref_table import _disc_coeff  # noqa: E402


def ensure_cache(texp: int) -> tuple[Path, Path, dict]:
    """Market/oracle JSON for the binary, keyed like sweep_ref_v2.py; the
    expensive Python build only runs when the key is new."""
    market_p = V2_REPO / "zchf_crvusd" / "zchf_crvusd_1m.json.gz"
    agg_p = V2_REPO / "zchf_crvusd" / "crvusd_usd_1m.csv"
    key = hashlib.sha1(f"{market_p}|{market_p.stat().st_size}|{agg_p}|"
                       f"{texp}|False".encode()).hexdigest()[:12]
    CACHE.mkdir(parents=True, exist_ok=True)
    mkt_f, orc_f = CACHE / f"market_{key}.json", CACHE / f"oracle_{key}.json"
    meta_f = CACHE / f"market_{key}.meta.json"
    if not (mkt_f.exists() and orc_f.exists()):
        sys.path.insert(0, str(V2_REPO))
        from zchf_crvusd import sweep_parameters as his
        market = his.load_market(market_p)
        aggregate = his.load_aggregate(agg_p, market)
        oracle = his.build_oracle(market, aggregate, texp)
        mkt_f.write_text(json.dumps(market))
        orc_f.write_text(json.dumps(oracle))
        meta_f.write_text(json.dumps({"n": len(market), "from": market[0][0],
                                      "to": market[-1][0]}))
    if not meta_f.exists():
        # cache predates the meta file: read the range from the .bin sidecar
        # (8-byte count, then 5 doubles per row) or, failing that, the JSON
        import struct
        binf = Path(str(mkt_f) + ".bin")
        if binf.exists():
            with binf.open("rb") as f:
                n = struct.unpack("<Q", f.read(8))[0]
                t0 = struct.unpack("<5d", f.read(40))[0]
                f.seek(8 + (n - 1) * 40)
                t1 = struct.unpack("<5d", f.read(40))[0]
            meta_f.write_text(json.dumps({"n": n, "from": t0, "to": t1}))
        else:
            rows = json.loads(mkt_f.read_text())
            meta_f.write_text(json.dumps({"n": len(rows), "from": rows[0][0],
                                          "to": rows[-1][0]}))
    return mkt_f, orc_f, json.loads(meta_f.read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-min", type=int, default=100)
    ap.add_argument("--a-max", type=int, default=180)
    ap.add_argument("--fee-min", type=float, default=0.05, help="percent")
    ap.add_argument("--fee-max", type=float, default=0.5, help="percent")
    ap.add_argument("--grid", type=int, default=10)
    ap.add_argument("--tail-pct", type=float, default=0.05)
    ap.add_argument("--range-size", type=int, default=4)
    ap.add_argument("--loan-days", type=float, default=80 / 1440)
    ap.add_argument("--texp", type=float, default=3603.0,
                    help="oracle EMA half-life s (gov recipe: 3603)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--realities", type=int, default=1)
    ap.add_argument("--progress-out", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=HERE / "data" / "sldl_table.json")
    args = ap.parse_args()
    if args.progress_out:
        _prog["path"] = args.progress_out
    t0 = time.time()
    _prog_phase("prep", 1)
    mkt_f, orc_f, meta = ensure_cache(int(args.texp))
    _prog_tick()
    a_grid = sorted({round(v) for v in spread(args.a_min, args.a_max, args.grid)})
    fee_grid = sorted({round(v, 4) for v in
                       spread(args.fee_min, args.fee_max, args.grid)})
    length = max(2, round(args.loan_days * 1440))
    warmup = math.ceil(10 * args.texp / 60)
    cmd = [str(BIN), "--market", str(mkt_f), "--oracle", str(orc_f),
           "--a-list", ",".join(str(a) for a in a_grid),
           "--fee-list", ",".join(repr(f / 100.0) for f in fee_grid),
           "--length", str(length), "--bands", str(args.range_size),
           "--ext-fee", str(EXT_FEE), "--dyn-mult", str(DYN_MULT),
           "--threads", str(args.threads), "--warmup", str(warmup),
           "--tail-frac", repr(args.tail_pct / 100.0), "--auto",
           "--oracle-hl", str(args.texp), "--realities", str(max(1, args.realities))]
    R = max(1, args.realities)
    _prog_phase("run", len(a_grid) * len(fee_grid) * R)
    cells, n_all, n_top = [], None, None
    with subprocess.Popen(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True) as p:
        for line in p.stdout:
            if line.startswith("reality "):   # per-reality progress (R > 1)
                _prog_tick()
                continue
            if not line.startswith("{"):
                continue
            c = json.loads(line)
            n_all, n_top = c["n_all"], c["m"]
            cells.append({"A": int(c["A"]), "fee_pct": round(c["fee"] * 100, 4),
                          "loss_pct": round(c["loss_pct"], 5),
                          "max_pct": round(c["max_pct"], 5),
                          "n_sims": c["n_sims"], "secs": c["secs"],
                          "transfer": c["transfer"]})
            if R == 1:
                _prog_tick()
            print(f"[table] A={c['A']:.0f} fee={c['fee'] * 100:g}%: "
                  f"{c['loss_pct']:.4f}% ({c['n_sims']} sims, {c['secs']:.2f}s)",
                  flush=True)
    if p.returncode != 0 or not cells:
        raise SystemExit(f"ref_model_v2 exited {p.returncode}")
    # Base-fee selection curve: AVERAGE loss over 3-day windows (every 400th
    # start minute, deterministic) at the discount-minimising A.
    fee_curve = None
    if n_all:
        import array
        best_A = min(cells, key=lambda c: 1 - (1 - c["max_pct"] / 100)
                     * _disc_coeff(c["A"], args.range_size))["A"]
        L3 = 3 * 1440
        starts_f = CACHE / "fee_curve_starts.json"
        starts = list(range(warmup, n_all - L3, 400))
        starts_f.write_text(json.dumps(starts))
        fc_fees = sorted({round(v, 4) for v in spread(0.015, 0.5, 15)})
        avg = []
        for f in fc_fees:
            out_f = CACHE / "fee_curve_out.f64"
            subprocess.run(
                [str(BIN), "--market", str(mkt_f), "--oracle", str(orc_f),
                 "--starts", str(starts_f), "--A", str(best_A),
                 "--fee", repr(f / 100.0), "--length", str(L3),
                 "--bands", str(args.range_size), "--ext-fee", str(EXT_FEE),
                 "--dyn-mult", str(DYN_MULT), "--threads", str(args.threads),
                 "--out", str(out_f)],
                check=True, capture_output=True)
            a = array.array("d")
            a.frombytes(out_f.read_bytes())
            vals = [v for v in a if v == v]
            avg.append(round(100 * sum(vals) / len(vals), 5))
            out_f.unlink(missing_ok=True)
        starts_f.unlink(missing_ok=True)
        fee_curve = {"A": best_A, "loan_days": 3,
                     "kind": f"{len(starts):,} loans, every 400th minute",
                     "fee_pct": fc_fees, "avg_loss_pct": avg}
    result = {
        "mode": "table",
        "config": {
            "model": "llamma-simulator_v2 port, C++ (cpp/src/ref_model_v2.cpp)",
            "model_variant": "v2",
            "backend": "cpp",
            "collateral": "ZCHF",
            "history": {
                "base_symbol": "ZCHF",
                "pool_name": "author dataset: zchf_crvusd_1m (ZCHF/crvUSD) "
                             "x crvUSD/USD aggregate, 1-min",
                "span_h": round((meta["to"] - meta["from"]) / 3600, 1),
                "n_points": meta["n"],
                "from_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(meta["from"])),
                "to_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(meta["to"])),
            },
            "range_size": args.range_size,
            "loan_days": args.loan_days,
            "method": "exact",
            "samples": None,
            "n_all": n_all,
            "n_top": n_top,
            "tail_pct": args.tail_pct,
            "texp_s": args.texp,
            "oracle_mode": "usd-basis",
            "ext_fee": EXT_FEE,
            "dyn_mult": DYN_MULT,
            "warmup": warmup,
            "realities": max(1, args.realities),
            "add_reverse": False,
        },
        "grid": {"A": a_grid, "fee_pct": fee_grid},
        "cells": cells,
        "fee_curve": fee_curve,
        "runtime_s": round(time.time() - t0, 1),
        "generated_at": int(time.time()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    print(f"[table] {len(cells)} cells x exact tail (v2) in "
          f"{result['runtime_s']}s -> {args.out}")


if __name__ == "__main__":
    main()
