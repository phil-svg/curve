#!/usr/bin/env python3
"""sweep_ref_table.py — reproduce the llamma-simulator (v1) "loss from soft
liquidation" reference tables (A x fee grids) on a minute price series.

Backend: cpp/build/ref_model, a bit-exact C++ port of the v1 model (the
vendored Python original in refsim/ is available via --backend python).

Default recipe = the one that reproduces the author's ZCHF/crvUSD reference
table to its own sampling noise (124 cells: median ratio 1.008, mean |err|
3.1%, all cells within 10%): his ZCHF data, oracle = EMA(candle mid,
half-life 1200 s) x crvUSD/USD aggregator (the on-chain oracle path),
4 bands, 2-day loans, mirrored history, ext fee 5 bp, 100k samples per
cell, cell value = mean of the worst 0.05% (worst 50). The same recipe runs
on any venue source (WBTC etc.): the crvUSD/USD leg comes from the author's
aggregator dump where it covers, the Curve API hourly series elsewhere.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
import time
from pathlib import Path

import multiprocessing

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "refsim"))

from sweep_sl_dl import (  # noqa: E402  (same-dir reuse: sources + progress)
    pick_market, load_history, spread, _prog, _prog_phase, _prog_tick,
    TEST_COLLATERAL,
)

EXT_FEE = 5e-4          # llamma-simulator's arb fee to external platforms
TAIL_FRACTION = 10_000  # n_top = samples // 10_000 -> mean of worst 0.01%


def build_klines(hist: dict, out: Path) -> int:
    """Venue minute history (fields "lhc") -> Binance-style klines
    [t_ms, open, high, low, close, vol] with open = previous close.
    Cached on (symbol, span, window): the 1y series takes ~a minute to
    convert, and the run phase should start ticking ASAP."""
    meta_f = out.with_suffix(".meta.json")
    meta = {"symbol": hist["symbol"], "span_s": hist["span_s"],
            "from": hist["from"], "to": hist["to"],
            "n": len(hist["points"])}
    if out.exists() and meta_f.exists():
        try:
            if json.loads(meta_f.read_text()) == meta:
                return meta["n"]
        except (json.JSONDecodeError, OSError):
            pass
    rows, prev_c = [], None
    for t, lo, hi, c in hist["points"]:
        o = prev_c if prev_c is not None else (lo + hi) / 2
        rows.append([int(t) * 1000, o, hi, lo, c, 0.0])
        prev_c = c
    out.write_text(json.dumps(rows))
    meta_f.write_text(json.dumps(meta))
    return len(rows)


CRVUSD = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
AUTHOR_AGG = HERE / "zchf" / "crvusd_usd_1m.csv"
HOURLY_CACHE = HERE / "data" / "_ref_v2" / "crvusd_usd_hourly.json"


def _crvusd_usd_lookup(t_first: int, t_last: int, agg_csv: Path | None):
    """crvUSD/USD per timestamp: the author's on-chain aggregator dump where
    it covers (per minute), the Curve API hourly series (forward-filled)
    elsewhere. Returns f(t_seconds) -> price."""
    import csv as _csv
    import urllib.request
    minute: dict[int, float] = {}
    src = agg_csv or (AUTHOR_AGG if AUTHOR_AGG.exists() else None)
    if src and src.exists():
        with src.open(newline="") as f:
            for row in _csv.DictReader(f):
                minute[int(row["epoch_time"])] = int(row["answer"]) / 1e18
    hourly: dict[int, float] = {}
    if HOURLY_CACHE.exists():
        hourly = {int(k): v for k, v in
                  json.loads(HOURLY_CACHE.read_text()).items()}
    h0, h1 = t_first // 3600 * 3600, t_last // 3600 * 3600 + 3600
    need = [h for h in range(h0, h1, 3600) if h not in hourly
            and h // 60 * 60 not in minute]
    if need:
        print(f"[table] fetching {len(need)} crvUSD/USD hourly points",
              flush=True)
        lo, hi = min(need), max(need) + 3600
        t = lo
        while t < hi:
            e = min(t + 30 * 86400, hi)
            url = (f"https://prices.curve.finance/v1/usd_price/ethereum/"
                   f"{CRVUSD}/history?interval=hour&start={t}&end={e}")
            req = urllib.request.Request(url, headers={"User-Agent":
                                                       "bad-debt-sim"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    for d in json.loads(r.read()).get("data", []):
                        ts = int(time.mktime(time.strptime(
                            d["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
                                 - time.timezone)
                        hourly[ts // 3600 * 3600] = float(d["price"])
            except Exception as ex:
                print(f"[table] hourly fetch failed {t}: {ex}", flush=True)
            t = e
        HOURLY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HOURLY_CACHE.write_text(json.dumps(hourly))
    hours_sorted = sorted(hourly)

    def lookup(ts: int) -> float:
        m = ts // 60 * 60
        if m in minute:
            return minute[m]
        h = ts // 3600 * 3600
        if h in hourly:
            return hourly[h]
        # forward-fill from the latest earlier hour; 1.0 if nothing at all
        i = bisect.bisect_right(hours_sorted, h) - 1
        return hourly[hours_sorted[i]] if i >= 0 else 1.0
    return lookup


def _load_kl_rows(kl: Path) -> list:
    """Klines rows [t,o,h,l,c,(v)] from the JSON, or — when the JSON has
    been truncated to 0 bytes to save disk — from the engine's .bin sidecar
    (raw t,o,h,l,c doubles, no header). Bin row counts were verified equal
    to the JSON's before any truncation, so both sources are identical."""
    if kl.stat().st_size > 0:
        return json.loads(kl.read_text())
    import numpy as np
    raw = np.fromfile(str(kl) + ".bin", dtype="<f8").reshape(-1, 5)
    return [[t, o, h, l, c, 0.0] for t, o, h, l, c in raw]


def _disc_coeff(A: float, bands: int) -> float:
    return sum(((A - 1) / A) ** (k + 0.5) for k in range(bands)) / bands


def build_usd_oracle(kl: Path, out: Path, half_life: float,
                     agg_csv: Path | None) -> None:
    """The author's build_oracle: EMA of the candle midpoint (2^(-dt/hl)
    decay, seeded at the first open) x crvUSD/USD at that minute."""
    rows = _load_kl_rows(kl)
    usd = _crvusd_usd_lookup(int(rows[0][0]) // 1000,
                             int(rows[-1][0]) // 1000, agg_csv)
    ema = rows[0][1]
    prev = int(rows[0][0]) // 1000
    out_rows = []
    for t_ms, _o, h, l, _c, _v in rows:
        ts = int(t_ms) // 1000
        mid = (h + l) / 2
        decay = 2 ** (-(ts - prev) / half_life) if half_life else 0.0
        ema = ema * decay + mid * (1 - decay)
        out_rows.append(ema * usd(ts))
        prev = ts
    out.write_text(json.dumps(out_rows))
    print(f"[table] usd-basis oracle built: {len(out_rows)} rows, "
          f"half-life {half_life:g}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-min", type=int, default=100)
    ap.add_argument("--a-max", type=int, default=180)
    ap.add_argument("--fee-min", type=float, default=0.05)
    ap.add_argument("--fee-max", type=float, default=0.5)
    ap.add_argument("--grid", type=int, default=4, help="N -> NxN grid")
    # Defaults = the recipe that reproduces the author's ZCHF reference
    # table to its own sampling noise (124 cells, mean |err| 3.1%):
    # 4 bands, 2-day loans, 100k samples, mean of the worst 0.05%,
    # oracle = EMA(mid, half-life 1200 s) x crvUSD/USD.
    ap.add_argument("--realities", type=int, default=1,
                    help="average the exact table over N perturbed price "
                         "histories (1 = the actual history)")
    ap.add_argument("--method", choices=("exact", "classic"), default="exact",
                    help="exact = adaptive v3 exhaustive-tail search (every "
                         "start minute that can be in the tail is simulated; "
                         "deterministic, validated to <0.002%% vs the full "
                         "run at 30 min .. 4 d loans); classic = random "
                         "starts (--samples), the original recipe")
    ap.add_argument("--samples", type=int, default=100_000,
                    help="loans per cell (classic method only)")
    ap.add_argument("--tail-pct", type=float, default=0.05,
                    help="cell value = mean of the worst this-percent of "
                         "loans (reference: 0.05%% = worst 50 of 100k)")
    ap.add_argument("--range-size", type=int, default=4,
                    help="bands per position (reference: 4)")
    ap.add_argument("--loan-days", type=float, default=2.0,
                    help="fixed loan duration (reference: 2 days)")
    ap.add_argument("--texp", type=float, default=1200.0,
                    help="oracle EMA half-life seconds (reference: 1200)")
    ap.add_argument("--oracle-mode", choices=("usd-basis", "pool-ema"),
                    default="usd-basis",
                    help="usd-basis = EMA(mid) x crvUSD/USD aggregator "
                         "(the on-chain oracle path; reference); pool-ema = "
                         "EMA of the venue ratio only")
    ap.add_argument("--aggregate-csv", type=Path, default=None,
                    help="crvUSD/USD per-minute CSV (author's on-chain "
                         "aggregator dump); API hourly fills the rest")
    ap.add_argument("--collateral", default=TEST_COLLATERAL)
    ap.add_argument("--span-h", type=float, default=48.0)
    ap.add_argument("--venue-json", default=None,
                    help="JSON {chain, venue:{pool,name,base_addr,"
                         "quote_addr,...}} to fetch the minute history from "
                         "this pool instead of the collateral's market venue "
                         "(e.g. TricryptoUSDC for a 1.5y WBTC/USDC series)")
    ap.add_argument("--klines-file", type=Path, default=None,
                    help="use this klines JSON directly (needs a sibling "
                         ".meta.json) instead of a market's venue history — "
                         "e.g. the saved CHF FX 5y series")
    ap.add_argument("--backend", choices=("cpp", "python"), default="cpp",
                    help="cpp = bit-exact ref_model port (~150x faster); "
                         "python = the vendored original")
    ap.add_argument("--rng-seed", type=int, default=1234)
    ap.add_argument("--out", type=Path,
                    default=HERE / "data" / "sldl_table.json")
    ap.add_argument("--progress-out", type=Path, default=None)
    args = ap.parse_args()

    t0 = time.time()
    if args.progress_out:
        _prog["path"] = args.progress_out

    if args.klines_file:
        # pre-built series (e.g. CHF FX 5y): no market, no venue fetch
        kl = args.klines_file
        meta = json.loads(
            Path(str(kl).replace(".json", ".meta.json")).read_text())
        hist = {"symbol": meta["symbol"],
                "pool_name": meta.get("pool_name")
                or meta.get("source", ""),
                "from": meta["from"], "to": meta["to"]}
        n_rows = meta["n"]
        span_h = meta.get("span_h", args.span_h)
        _prog_phase("prep", 3)
        _prog_tick()
    else:
        if args.venue_json:
            mk = json.loads(args.venue_json)
        else:
            mk, _grp = pick_market(args.collateral)
        hist = load_history(int(args.span_h * 3600), args.collateral, mk)
        span_h = args.span_h

        # setup can take 1-2 min on a 1y series (convert + load + EMA
        # warm); tick it so the UI ring moves before the first cell lands
        _prog_phase("prep", 3)
        safe = args.collateral.replace("/", "_")
        kl = (HERE / "data"
              / f"_ref_table_klines_{safe}_{int(args.span_h)}h.json")
        n_rows = build_klines(hist, kl)
        _prog_tick()

    a_grid = sorted({round(v) for v in
                     spread(args.a_min, args.a_max, args.grid)})
    fee_grid = sorted({round(v, 4) for v in
                       spread(args.fee_min, args.fee_max, args.grid)})
    n_top = max(1, round(args.samples * args.tail_pct / 100.0))

    # USD-basis oracle: EMA(mid, half-life) x crvUSD/USD, aligned per row,
    # handed to the binary instead of its internal pool-ratio EMA
    oracle_f = None
    if args.oracle_mode == "usd-basis":
        oracle_f = Path(str(kl).replace(".json", f"_oracle_{int(args.texp)}.json"))
        if not oracle_f.exists():
            build_usd_oracle(kl, oracle_f, args.texp, args.aggregate_csv)
    _prog_tick()

    cells = []
    n_all = None
    fee_curve = None
    if args.backend == "cpp":
        # ref_model: bit-exact C++ port of the model (validated against the
        # Python original to 12 digits per sample), ~150x faster
        import subprocess
        bin_path = HERE / "bin" / "ref_model"
        if not bin_path.exists():
            raise SystemExit(f"missing {bin_path} — run `make` in cpp-src/")
        cmd = [str(bin_path), "--klines", str(kl),
               "--a-list", ",".join(str(a) for a in a_grid),
               "--fee-list", ",".join(str(f) for f in fee_grid),
               "--range-size", str(args.range_size),
               "--loan-days", str(args.loan_days),
               "--texp", str(args.texp), "--ext-fee", str(EXT_FEE)]
        if args.method == "exact":
            # adaptive v3: coarse grid + local refinement + spike/jump
            # starts + outward walk + anchor transfer, every search length
            # derived from the loan length (ref_model --auto); the cell
            # value is the mean of the worst tail_pct of ALL start minutes
            cmd += ["--auto", "--tail-frac", repr(args.tail_pct / 100.0),
                    "--realities", str(max(1, args.realities))]
        else:
            cmd += ["--samples", str(args.samples), "--n-top", str(n_top),
                    "--seed", str(args.rng_seed)]
        if oracle_f:
            cmd += ["--oracle", str(oracle_f)]
        R = max(1, args.realities) if args.method == "exact" else 1
        _prog_phase("run", len(a_grid) * len(fee_grid) * R)
        with subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as p:
            for line in p.stdout:
                if line.startswith("reality "):
                    _prog_tick()
                    continue
                if not line.startswith("{"):
                    continue
                c = json.loads(line)
                cell = {"A": int(c["A"]), "fee_pct": c["fee_pct"],
                        "loss_pct": round(c["loss_pct"], 5)}
                if args.method == "exact":
                    cell.update({"n_sims": c["n_sims"], "secs": c["secs"],
                                 "max_pct": round(c["max_pct"], 5),
                                 "transfer": c["transfer"]})
                    n_all = c["n_all"]; n_top = c["m"]
                cells.append(cell)
                if R == 1:
                    _prog_tick()
                print(f"[table] A={c['A']:.0f} fee={c['fee_pct']}%: "
                      f"{c['loss_pct']:.4f}%"
                      + (f" ({c['n_sims']} sims, {c['secs']:.2f}s)"
                         if args.method == "exact" else ""), flush=True)
        if p.returncode != 0:
            raise SystemExit(f"ref_model exited {p.returncode}")
        # ref_model emits A-major, fee-minor — same order as the python path
        # Base-fee selection curve: AVERAGE loss (all sampled loans, not the
        # tail) over 3-day windows at the discount-minimising A, fee swept
        # 0.015..0.5% — the gov-post step-2 statistic.
        if (args.method == "exact" and cells
                and all("max_pct" in c for c in cells)):
            best_A = min(cells, key=lambda c: 1 - (1 - c["max_pct"] / 100)
                         * _disc_coeff(c["A"], args.range_size))["A"]
            fc_fees = sorted({round(v, 4) for v in spread(0.015, 0.5, 15)})
            cmd2 = [str(bin_path), "--klines", str(kl),
                    "--a-list", str(best_A),
                    "--fee-list", ",".join(str(f) for f in fc_fees),
                    "--range-size", str(args.range_size),
                    "--loan-days", "3", "--texp", str(args.texp),
                    "--ext-fee", str(EXT_FEE),
                    "--samples", "4000", "--n-top", "4000", "--seed", "1"]
            if oracle_f:
                cmd2 += ["--oracle", str(oracle_f)]
            r2 = subprocess.run(cmd2, capture_output=True, text=True)
            fc = [json.loads(l) for l in r2.stdout.splitlines()
                  if l.startswith("{")]
            if len(fc) == len(fc_fees):
                fee_curve = {"A": best_A, "loan_days": 3,
                             "kind": "4,000 sampled loans",
                             "fee_pct": [c["fee_pct"] for c in fc],
                             "avg_loss_pct": [round(c["loss_pct"], 5)
                                              for c in fc]}
    else:
        # their loader/workers read module globals -> warm all, THEN fork
        multiprocessing.set_start_method("fork", force=True)
        from libsimulate import Simulator, init_multicore
        sim = Simulator(str(kl), ext_fee=EXT_FEE, add_reverse=True)
        _prog_tick()
        sim.update_emas(args.texp)
        init_multicore()
        _prog_tick()
        _prog_phase("run", len(a_grid) * len(fee_grid))
        for i, (A, fee_pct) in enumerate((A, f) for A in a_grid
                                         for f in fee_grid):
            random.seed(args.rng_seed + i)  # get_loss_rate uses random
            loss = sim.get_loss_rate(
                A, args.range_size, fee_pct / 100.0, args.texp,
                samples=args.samples, n_top_samples=n_top,
                min_loan_duration=args.loan_days,
                max_loan_duration=args.loan_days)
            cells.append({"A": A, "fee_pct": fee_pct,
                          "loss_pct": round(loss * 100, 5)})
            _prog_tick()
            print(f"[table] A={A} fee={fee_pct}%: {loss*100:.4f}%",
                  flush=True)

    result = {
        "mode": "table",
        "config": {
            "model": ("llamma-simulator port, C++ (cpp/src/ref_model.cpp)"
                      if args.backend == "cpp"
                      else "llamma-simulator (vendored, refsim/)"),
            "backend": args.backend,
            "collateral": hist["symbol"],
            "history": {
                "base_symbol": hist["symbol"],
                "pool_name": hist["pool_name"],
                "span_h": span_h,
                "n_points": n_rows,
                "from_utc": time.strftime("%Y-%m-%d %H:%M",
                                          time.gmtime(hist["from"])),
                "to_utc": time.strftime("%Y-%m-%d %H:%M",
                                        time.gmtime(hist["to"])),
            },
            "range_size": args.range_size,
            "loan_days": args.loan_days,
            "method": args.method if args.backend == "cpp" else "classic",
            "realities": max(1, args.realities),
            "samples": (None if args.method == "exact" and args.backend == "cpp"
                        else args.samples),
            "n_all": n_all if args.backend == "cpp" else None,
            "n_top": n_top,
            "tail_pct": args.tail_pct,
            "texp_s": args.texp,
            "oracle_mode": args.oracle_mode,
            "ext_fee": EXT_FEE,
            "add_reverse": True,
        },
        "grid": {"A": a_grid, "fee_pct": fee_grid},
        "cells": cells,
        "fee_curve": fee_curve,
        "runtime_s": round(time.time() - t0, 1),
        "generated_at": int(time.time()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    print(f"[table] {len(cells)} cells x "
          f"{'exact tail' if result['config']['method'] == 'exact' else str(args.samples) + ' samples'} "
          f"in {result['runtime_s']}s -> {args.out}")


if __name__ == "__main__":
    main()
