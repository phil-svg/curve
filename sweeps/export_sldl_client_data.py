#!/usr/bin/env python3
"""Export the data files the browser-side S.L./D.L. runner needs.

  data/sldl_usd_1m.bin     dense minute crvUSD/USD table from the author's
                           aggregator CSV: uint64 t0, uint64 n, f64[n]
                           (NaN where the CSV has no row). texp-independent;
                           committed to git (~6 MB).
  data/sldl_zchf_market.bin   the v2 author market (zchf_crvusd_1m.json.gz)
  data/sldl_zchf_market.meta.json   in the engine's v2 sidecar format
                           (uint64 n + n x 40B rows, t in seconds).
                           Generated per machine (needs the .gz), NOT in git.

Run with no args to build whatever inputs exist; --zchf-market-only skips
the CSV (for a machine that already has sldl_usd_1m.bin from git).
"""
import argparse
import csv
import gzip
import json
import math
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
USD_CSV = HERE / "zchf" / "crvusd_usd_1m.csv"
V2_GZ = HERE / "llamma-simulator_v2" / "zchf_crvusd" / "zchf_crvusd_1m.json.gz"
USD_OUT = HERE / "data" / "sldl_usd_1m.bin"
MKT_OUT = HERE / "data" / "sldl_zchf_market.bin"


def export_usd() -> None:
    minute = {}
    with USD_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            minute[int(row["epoch_time"])] = int(row["answer"]) / 1e18
    t0 = min(minute)
    t1 = max(minute)
    n = (t1 - t0) // 60 + 1
    vals = [math.nan] * n
    for t, v in minute.items():
        vals[(t - t0) // 60] = v
    with USD_OUT.open("wb") as f:
        f.write(struct.pack("<QQ", t0, n))
        f.write(struct.pack(f"<{n}d", *vals))
    print(f"{USD_OUT.name}: {len(minute)} rows -> {n} minutes "
          f"({USD_OUT.stat().st_size / 1e6:.1f} MB)")


def export_zchf_market() -> None:
    with gzip.open(V2_GZ, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    rows = [[int(r[0]) // 1000, *map(float, r[1:5])] for r in raw]
    with MKT_OUT.open("wb") as f:
        f.write(struct.pack("<Q", len(rows)))
        for r in rows:
            f.write(struct.pack("<5d", *r))
    MKT_OUT.with_suffix(".meta.json").write_text(json.dumps(
        {"n": len(rows), "from": rows[0][0], "to": rows[-1][0],
         "span_h": round((rows[-1][0] - rows[0][0]) / 3600, 1)}))
    print(f"{MKT_OUT.name}: {len(rows)} rows "
          f"({MKT_OUT.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zchf-market-only", action="store_true")
    args = ap.parse_args()
    if not args.zchf_market_only:
        if not USD_CSV.exists():
            sys.exit(f"missing {USD_CSV}")
        export_usd()
    if V2_GZ.exists():
        export_zchf_market()
    else:
        print(f"note: {V2_GZ} missing — v2 zchf stays server-side here")


if __name__ == "__main__":
    main()
