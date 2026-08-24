#!/usr/bin/env python3
"""fetch_binance_klines.py — 1-minute klines from Binance into the packed
.bin format the engines read (t_ms,o,h,l,c as raw doubles, 0-byte .json
twin, .meta.json sidecar).

Bulk months come from the public archive (data.binance.vision, one zip per
month); the current month tops up over the REST API. Rows are

    python3 fetchers/fetch_binance_klines.py --symbol ETHUSDT --since 2025-01
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = "https://data.binance.vision/data/spot/monthly/klines/{s}/1m/{s}-1m-{ym}.zip"
API = ("https://api.binance.com/api/v3/klines?symbol={s}&interval=1m"
       "&startTime={t0}&limit=1000")


def month_range(since: str):
    y, m = int(since[:4]), int(since[5:7])
    now = time.gmtime()
    while (y, m) < (now.tm_year, now.tm_mon):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def fetch_month(sym: str, ym: str) -> list[tuple]:
    req = urllib.request.Request(ARCHIVE.format(s=sym, ym=ym),
                                 headers={"User-Agent": "curve-sim"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                zf = zipfile.ZipFile(io.BytesIO(r.read()))
            break
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    rows = []
    with zf.open(zf.namelist()[0]) as f:
        for row in csv.reader(io.TextIOWrapper(f)):
            t = int(row[0])
            if t > 10 ** 14:          # 2025+ archives use microseconds
                t //= 1000
            rows.append((float(t), float(row[1]), float(row[2]),
                         float(row[3]), float(row[4])))
    return rows


def fetch_api_from(sym: str, t0_ms: int) -> list[tuple]:
    rows = []
    while True:
        with urllib.request.urlopen(API.format(s=sym, t0=t0_ms),
                                    timeout=30) as r:
            j = json.loads(r.read())
        if not j:
            return rows
        for k in j:
            rows.append((float(k[0]), float(k[1]), float(k[2]),
                         float(k[3]), float(k[4])))
        t0_ms = j[-1][0] + 60_000
        if len(j) < 1000:
            return rows
        time.sleep(0.12)


def write_series(out_base: Path, rows: list[tuple], symbol: str,
                 source: str) -> None:
    """rows -> <base>.json.bin (+ 0-byte .json with older mtime, so the
    engines' freshness check picks the sidecar) + .meta.json."""
    rows.sort(key=lambda r: r[0])
    dedup, last = [], -1.0
    for r in rows:
        if r[0] > last:
            dedup.append(r)
            last = r[0]
    binf = Path(str(out_base) + ".bin")
    with binf.open("wb") as f:
        for r in dedup:
            f.write(struct.pack("<5d", *r))
    out_base.write_bytes(b"")
    older = binf.stat().st_mtime - 100
    os.utime(out_base, (older, older))
    meta = {"symbol": symbol, "source": source, "n": len(dedup),
            "from": int(dedup[0][0] // 1000), "to": int(dedup[-1][0] // 1000),
            "span_h": round((dedup[-1][0] - dedup[0][0]) / 3.6e6, 1)}
    Path(str(out_base).replace(".json", ".meta.json")).write_text(
        json.dumps(meta))
    days = (dedup[-1][0] - dedup[0][0]) / 86_400_000
    print(f"[binance] {out_base.name}: {len(dedup):,} rows, {days:.0f} days")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="e.g. ETHUSDT")
    ap.add_argument("--since", default="2025-01", help="first month YYYY-MM")
    ap.add_argument("--out-name", default=None,
                    help="collateral name for the output file "
                         "(default: the symbol)")
    args = ap.parse_args()
    rows: list[tuple] = []
    for ym in month_range(args.since):
        try:
            got = fetch_month(args.symbol, ym)
        except urllib.error.HTTPError as e:
            if e.code == 404:      # symbol listed later than --since
                print(f"[binance] {args.symbol} {ym}: no archive (skip)")
                continue
            raise
        rows.extend(got)
        print(f"[binance] {args.symbol} {ym}: {len(got):,}")
    t0 = int(rows[-1][0]) + 60_000 if rows else int(
        time.mktime(time.strptime(args.since, "%Y-%m")) * 1000)
    rows.extend(fetch_api_from(args.symbol, t0))
    name = args.out_name or args.symbol
    span_h = int(round((rows[-1][0] - rows[0][0]) / 3.6e6))
    out = ROOT / "data" / f"_ref_table_klines_{name}_{span_h}h.json"
    write_series(out, rows, name, f"binance:{args.symbol}")


if __name__ == "__main__":
    main()
