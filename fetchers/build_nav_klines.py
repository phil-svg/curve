#!/usr/bin/env python3
"""build_nav_klines.py — price histories for yield-wrapper collaterals:
on-chain NAV rate x underlying USD feed (the same USD-basis construction
as the ZCHF dataset). Plus XAUM (= PAXG, per-gram).

Per wrapper: convertToAssets(1e18) (or stEthPerToken for wstETH) sampled
once per day over the span via historic multicalls — one Multicall3 per
day covers every token — then forward-filled onto the underlying's
1-minute grid and multiplied through o/h/l/c. Wrappers whose underlying
has no market feed (frxUSD, USDS, DOLA, reUSD, fxUSD) use a flat 1.0 peg:
their series carry NAV drift only. Output is the packed .bin format
(fetch_binance_klines.write_series).

    python3 fetchers/build_nav_klines.py
"""
from __future__ import annotations

import glob
import re
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
for _d in (ROOT / "pylib", ROOT / "sim", ROOT / "sweeps"):
    sys.path.insert(0, str(_d))
from fetch_markets import Rpc, _num                     # noqa: E402
from fetch_binance_klines import write_series           # noqa: E402

SPAN_START = "2025-01-04"          # match the ZCHF dataset's era
DAY_MS = 86_400_000

# name -> (mainnet token, rate selector, underlying feed key)
TOKENS = {
    "wstETH":    ("0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",
                  "stEthPerToken()", "WETH"),
    "svZCHF":    ("0xe5f130253ff137f9917c0107659a4c5262abf6b0",
                  "convertToAssets(uint256)", "ZCHFUSD"),
    "syrupUSDC": ("0x80ac24aa929eaf5013f6436cda2a7ba190f5cc0b",
                  "convertToAssets(uint256)", "USDC"),
    "sUSDe":     ("0x9d39a5de30e57443bff2a8307a4256c8797a3497",
                  "convertToAssets(uint256)", "USDE"),
    "sfrxUSD":   ("0xcf62f905562626cfcdd2261162a51fd02fc9c5b6",
                  "convertToAssets(uint256)", None),
    "sUSDS":     ("0xa3931d71877c0e7a3148cb7eb4463524fec27fbd",
                  "convertToAssets(uint256)", None),
    "sDOLA":     ("0xb45ad160634c528cc3d2926d9807104fa3157305",
                  "convertToAssets(uint256)", None),
    "sreUSD":    ("0x557ab1e003951a73c12d16f0fea8490e39c33c35",
                  "convertToAssets(uint256)", None),
    "fxSAVE":    ("0x7743e50f534a7f9f1791dde7dcd89f7783eefc39",
                  "convertToAssets(uint256)", None),
}
OZ_PER_GRAM = 31.1034768           # XAUM is per gram, PAXG per troy ounce


def latest_bin(name: str) -> np.ndarray:
    """Newest packed series for a collateral name -> (n,5) [t_ms,o,h,l,c]."""
    cands = glob.glob(str(ROOT / "data"
                          / f"_ref_table_klines_{name}_*h.json.bin"))
    assert cands, f"no series for {name} — run fetch_binance_klines first"
    span = lambda f: int(re.search(r"_(\d+)h\.json\.bin$", f).group(1))
    a = np.fromfile(max(cands, key=span), dtype="<f8").reshape(-1, 5)
    return a


def zchf_usd_grid() -> np.ndarray:
    """ZCHF/USD minutes: his_klines (ZCHF/crvUSD, t in s) x crvUSD/USD from
    the author's aggregator dump, forward-filled over its small gaps (no
    network fallback)."""
    import csv as _csv
    a = np.fromfile(ROOT / "zchf" / "his_klines.json.bin",
                    dtype="<f8").reshape(-1, 5)
    ts, px = [], []
    with (ROOT / "zchf" / "crvusd_usd_1m.csv").open(newline="") as f:
        for row in _csv.DictReader(f):
            ts.append(int(row["epoch_time"]))
            px.append(float(row["usd_per_crvusd"]))
    ts_a, px_a = np.array(ts, dtype="i8"), np.array(px)
    idx = np.clip(np.searchsorted(ts_a, a[:, 0].astype("i8"), "right") - 1,
                  0, len(ts_a) - 1)
    f_ = px_a[idx]
    out = a.copy()
    if out[0, 0] < 1e12:                      # engine convention: ms
        out[:, 0] *= 1000.0
    for c in range(1, 5):
        out[:, c] *= f_
    return out


def flat_grid(t0_ms: int, t1_ms: int) -> np.ndarray:
    ts = np.arange(t0_ms, t1_ms + 1, 60_000, dtype="f8")
    out = np.ones((len(ts), 5))
    out[:, 0] = ts
    return out


def sample_rates() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Daily NAV rate per token via one historic multicall per day."""
    rpc = Rpc("ethereum")
    head, head_ts = rpc.head()
    t0 = int(time.mktime(time.strptime(SPAN_START, "%Y-%m-%d")))
    days = np.arange(t0, head_ts - 3600, 86_400, dtype="i8")
    names = list(TOKENS)
    rates = {n: np.full(len(days), np.nan) for n in names}
    from concurrent.futures import ThreadPoolExecutor

    def one(i):
        ts = int(days[i])
        block = max(1, head - int((head_ts - ts) / 12.05))
        res = rpc.mq([(TOKENS[n][0], TOKENS[n][1],
                       10 ** 18 if "uint256" in TOKENS[n][1] else None)
                      for n in names], block=hex(block))
        for n, r in zip(names, res):
            v = _num(r)
            if v and 10 ** 16 < v < 10 ** 20:
                rates[n][i] = v / 1e18
        if i % 50 == 0:
            print(f"[nav] sampled day {i}/{len(days)}", flush=True)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, range(len(days))))
    return days.astype("f8") * 1000.0, rates


def main() -> None:
    cache = ROOT / "tmp" / "nav_rates.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        j = json.loads(cache.read_text())
        day_ms = np.array(j["day_ms"])
        rates = {k: np.array([np.nan if v is None else v for v in vv])
                 for k, vv in j["rates"].items()}
        print(f"[nav] rates from cache ({len(day_ms)} days)")
    else:
        day_ms, rates = sample_rates()
        cache.write_text(json.dumps({
            "day_ms": day_ms.tolist(),
            "rates": {k: [None if np.isnan(v) else v for v in vv]
                      for k, vv in rates.items()}}))

    feeds: dict[str, np.ndarray] = {}
    for key in {u for _, _, u in TOKENS.values() if u} | {"PAXG"}:
        feeds[key] = zchf_usd_grid() if key == "ZCHFUSD" else latest_bin(key)

    # XAUM = PAXG per gram (no NAV leg)
    px = feeds["PAXG"].copy()
    px[:, 1:5] /= OZ_PER_GRAM
    span_h = int(round((px[-1, 0] - px[0, 0]) / 3.6e6))
    write_series(ROOT / "data" / f"_ref_table_klines_XAUM_{span_h}h.json",
                 [tuple(r) for r in px], "XAUM", "binance:PAXGUSDT/gram")

    for name, (_addr, _sel, under) in TOKENS.items():
        r = rates[name]
        ok = ~np.isnan(r)
        if not ok.any():
            print(f"[nav] {name}: no valid samples, SKIPPED")
            continue
        first = np.argmax(ok)                 # deployment edge
        base = (feeds[under] if under
                else flat_grid(int(day_ms[first]), int(day_ms[-1])))
        t = base[:, 0]
        lo = max(day_ms[first], t[0])
        sel_rows = base[(t >= lo)]
        idx = np.clip(np.searchsorted(day_ms, sel_rows[:, 0], "right") - 1,
                      first, len(day_ms) - 1)
        rr = r.copy()
        for i in range(first + 1, len(rr)):   # ffill sampling gaps
            if np.isnan(rr[i]):
                rr[i] = rr[i - 1]
        f = rr[idx]
        out = sel_rows.copy()
        for c in range(1, 5):
            out[:, c] *= f
        span_h = int(round((out[-1, 0] - out[0, 0]) / 3.6e6))
        write_series(ROOT / "data"
                     / f"_ref_table_klines_{name}_{span_h}h.json",
                     [tuple(rw) for rw in out], name,
                     f"nav:{_sel.split('(')[0]} x {under or 'flat 1.0'}")


if __name__ == "__main__":
    main()
