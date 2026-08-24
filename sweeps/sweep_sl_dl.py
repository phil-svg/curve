#!/usr/bin/env python3
"""sweep_sl_dl.py — soft-liq / de-liq loss table over (A, base fee).

llamma-simulator_v2's question asked of OUR engine: how much does a borrower
lose to soft-liquidation churn as a function of the LLAMMA A and the base fee?
One C++ run per (A, fee, window).

WBTC test mode (current): the last 48 h of REAL 1-minute closes from the WBTC
market's venue pool, divided into N evenly-spaced 5 h windows (N = --paths;
overlapping when N > span/window). Each window is prepared the way
llamma-simulator_v2's VolatilityPriceHistoryLoader does it:
  - max_drawdown = the worst (high-low)/high over all window-length spans in
    the loaded history;
  - the window is linearly stretched from its own high so its drawdown equals
    that max: p' = w_high - (w_high - p) * r,  r = max_drawdown / window_dd.
The borrower sits at ~max LTV so its bands start immediately below spot —
same placement llamma-simulator uses — otherwise a calm 48 h never reaches
the ladder and every cell reads zero. Same windows for every (A, fee) cell.
Hard liquidation is disabled (--hard-liq 0): the metric is the surviving
borrower's loss, so the Controller must not seize the position.

    loss = 1 - (x_end + y_end*p_end) / (y0*p_end)      (vs HODL at end price)

Aggregation follows the repo: the headline is the MEAN OF THE WORST 5% of
windows (n_top = max(1, paths//20)).

    python3 sweep_sl_dl.py --a-min 100 --a-max 180 \
        --fee-min 0.05 --fee-max 0.5 --grid 12 --paths 10

The base snapshot is built once per A and the fee patched per cell — fee
lives in state.fee and does not move band placement, so 144 cells need only
12 make_snapshot calls.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
for _d in (Path(__file__).resolve().parent, HERE / "sim", HERE / "fetchers"):
    sys.path.insert(0, str(_d))
import single_user  # noqa: E402
import fetch_crash_window as fcw  # noqa: E402  (fetch_minutes + candles path)

CPP = HERE / "bin" / "routed_engine"
SCRATCH = HERE / "tmp" / "sldl"
HISTORY_TTL_S = 6 * 3600     # short spans track "now"; refetch after this
HISTORY_TTL_LONG_S = 30 * 86400   # a 1-year window barely moves; keep a month


def history_cache_path(symbol: str, span_s: int) -> Path:
    safe = symbol.replace("/", "_")
    return HERE / "data" / f"sldl_history_{safe}_{span_s // 3600}h.json"

# ---- fixed study config (everything except A, fee, window count) ------------
START_PRICE = 1.0
# Borrower sizing. LLV2 markets have an on-chain borrow_cap, so the study user
# is the whole market at its cap (debt = cap, collateral sized so that cap is
# exactly the max allowed debt). LLV1 has NO debt cap, so that anchor does not
# exist — there the borrower is a fixed $100k of collateral at max allowed
# debt instead.
LLV1_COLLATERAL_USD = 100_000.0
UTIL = 0.999                # ~max LTV -> bands right below spot (llamma-style)
N_BANDS = 4                 # llamma-simulator's default range
LOAN_DISC_PCT = 11.0
LIQ_DISC_PCT = 8.0          # engine arg only; hard-liq is off
VENUE = dict(tvl_usd=20_000_000.0, A_raw=10.0, pool_type="cryptoswap", n_coins=2)
DT_S = 60.0
# Oracle EMA in ON-CHAIN units: ma_exp_time is the exponential time constant
# tau the NG pools store (866s); the engine's EMA takes a HALF-LIFE, which is
# tau * ln2 (= ~600s, 10 min). UI-settable via --ma-exp-time.
MA_EXP_TIME_S = 866.0
GAS_USD = 10.0
TEST_COLLATERAL = "WBTC"    # WBTC test: largest-TVL market with this symbol
MIN_WINDOW_DD = 0.001       # flat-window floor so the rescale can't explode


# ---- real price history -----------------------------------------------------
def pick_market(symbol: str) -> tuple[dict, str]:
    """Largest-TVL market whose collateral symbol matches, with a venue."""
    m = json.loads((HERE / "data" / "markets.json").read_text())
    best, best_grp = None, ""
    for grp, lst in m["groups"].items():
        for mk in lst:
            if mk["collateral"]["symbol"] != symbol:
                continue
            if not (mk.get("venue") or {}).get("pool"):
                continue
            if best is None or (mk.get("market_tvl_usd") or 0) > \
                    (best.get("market_tvl_usd") or 0):
                best, best_grp = mk, grp
    if best is None:
        raise RuntimeError(f"no market with venue for collateral {symbol!r}")
    return best, best_grp


def borrower_sizing(mk: dict, grp: str) -> dict:
    """LLV2 anchors the study user to the market's borrow cap; LLV1 has no
    cap, so a fixed $100k collateral stands in."""
    cap = mk.get("borrow_cap_usd") or 0
    if grp == "LLV2" and cap > 0:
        return {"rule": "llv2-cap", "cap_usd": float(cap),
                "text": (f"LLV2 market — borrower debt = borrow cap "
                         f"(${cap:,.0f}), collateral sized so the cap is the "
                         f"max allowed debt")}
    return {"rule": "llv1-100k", "collateral_usd": LLV1_COLLATERAL_USD,
            "text": (f"LLV1 market (no debt cap on-chain) — borrower = "
                     f"${LLV1_COLLATERAL_USD:,.0f} collateral at max "
                     f"allowed debt")}


def load_history(span_s: int, symbol: str, mk: dict) -> dict:
    """Minute closes for the symbol's venue pool over the last span_s.
    Chunked 3h requests with a progress tick per chunk, so the UI ring fills
    during long backfills (a 1-year span is ~2,900 requests)."""
    cache = history_cache_path(symbol, span_s)
    ttl = HISTORY_TTL_S if span_s <= 7 * 86400 else HISTORY_TTL_LONG_S
    if cache.exists():
        h = json.loads(cache.read_text())
        if (h.get("symbol") == symbol and h.get("span_s") == span_s
                and h.get("fields") == "lhc"
                and h.get("pool", "").lower()
                == mk["venue"]["pool"].lower()
                and time.time() - h.get("fetched_at", 0) < ttl):
            return h
    v = mk["venue"]
    end = int(time.time()) // 60 * 60
    start = end - span_s
    n_chunks = (span_s + fcw.CHUNK_S - 1) // fcw.CHUNK_S
    print(f"[sldl] fetching {span_s // 3600}h of minute closes for {symbol} "
          f"venue {v['name']} ({mk['chain']}) — {n_chunks} requests")
    _prog_phase("fetch", n_chunks)
    out: dict[int, list] = {}
    t = start
    while t < end:
        e = min(t + fcw.CHUNK_S, end)
        j = fcw._get(fcw.API.format(chain=mk["chain"], pool=v["pool"].lower())
                     + f"?main_token={v['quote_addr']}"
                     + f"&reference_token={v['base_addr']}"
                     + f"&agg_number=1&agg_units=minute&start={t}&end={e}")
        for c in (j or {}).get("data") or []:
            out[c["time"]] = [c["low"], c["high"], c["close"]]
        t = e
        time.sleep(0.12)
        _prog_tick()
    pts = [[t_] + out[t_] for t_ in sorted(out)]
    # pools younger than the span just have less history — accept anything
    # that still gives a month of windows; only a truly dead feed is an error
    if len(pts) < min(span_s / 60 * 0.5, 30 * 1440):
        raise RuntimeError(f"minute history too sparse: {len(pts)} points")
    h = {"symbol": symbol, "chain": mk["chain"], "pool": v["pool"],
         "pool_name": v["name"], "span_s": span_s, "fields": "lhc",
         "from": pts[0][0], "to": pts[-1][0], "fetched_at": int(time.time()),
         "points": [[p[0]] + [round(x, 10) for x in p[1:]] for p in pts]}
    cache.write_text(json.dumps(h))
    return h


def max_window_drawdown(pts: list[list], horizon_s: float) -> float:
    """llamma-simulator's calculate_max_drawdown, wick-aware: the worst
    (high-low)/high over every horizon-length window, using the per-minute
    highs and lows (points are [t, low, high, close]). Monotonic deques."""
    hi: deque = deque()
    lo: deque = deque()
    maxdd = 0.0
    for t, l, h, _c in pts:
        t0 = t - horizon_s
        while hi and hi[0][0] < t0:
            hi.popleft()
        while lo and lo[0][0] < t0:
            lo.popleft()
        while hi and hi[-1][1] <= h:
            hi.pop()
        hi.append((t, h))
        while lo and lo[-1][1] >= l:
            lo.pop()
        lo.append((t, l))
        H, L = hi[0][1], lo[0][1]
        if H > 0:
            maxdd = max(maxdd, (H - L) / H)
    return maxdd


def warm_ema(pts: list[list], ts: list[int], t0: int, half_life_s: float) -> float:
    """llamma-simulator warms its EMA over the history BEFORE the window
    (update_emas runs from the start of the file); replicate with the last
    ~10 half-lives of (high+low)/2 before t0. Returns EMA / entry close."""
    lo_i = bisect.bisect_left(ts, int(t0 - 10 * half_life_s))
    hi_i = bisect.bisect_left(ts, t0)
    if hi_i <= lo_i:
        return 1.0
    seg = pts[lo_i:hi_i + 1]
    ema = (seg[0][1] + seg[0][2]) / 2
    t_prev = seg[0][0]
    for t, l, h, _c in seg[1:]:
        mul = 2.0 ** (-(t - t_prev) / half_life_s)
        ema = ema * mul + (l + h) / 2 * (1 - mul)
        t_prev = t
    p0 = pts[min(hi_i, len(pts) - 1)][3]
    return ema / p0 if p0 > 0 else 1.0


def step_windows(hist: dict, n: int, window_s: int,
                 wick: bool, mode: str = "stepped",
                 win_min_s: int = 1800, win_max_s: int = 3600,
                 rng=None, ma_half_s: float = 600.0,
                 add_reverse: bool = False,
                 rescale: bool = True) -> tuple[float, list[dict]]:
    """Divide the history span into n evenly-spaced windows of window_s each
    (overlapping when n > span/window), llamma drawdown-rescaled, normalized
    to start=1. Points are [t, low, high, close].

    wick=False: path = minute closes on the DT_S grid (the original mode).
    wick=True:  path visits each minute's HIGH then LOW (llamma-simulator
                trades to candle high then low every step), 30s apart, plus
                the final close — the engine runs it at dt=30s."""
    pts = hist["points"]
    if add_reverse:
        # llamma-simulator's add_reverse: append the series time-mirrored.
        t_last = pts[-1][0]
        pts = pts + [[t_last + (t_last - p[0])] + p[1:] for p in pts[::-1]]
    ts = [p[0] for p in pts]
    t_from, t_to = pts[0][0], pts[-1][0]
    dd_horizon = window_s if mode == "stepped" else win_max_s
    maxdd = max_window_drawdown(pts, dd_horizon)
    out: list[dict] = []
    for k in range(n):
        if mode == "random":
            w_s = int(win_min_s + (win_max_s - win_min_s) * rng.random())
            t0 = int(t_from + ((t_to - t_from) - w_s) * rng.random())
        else:
            w_s = window_s
            span = (t_to - t_from) - w_s
            t0 = t_from + (span * k // max(1, n - 1) if n > 1 else 0)
        i0 = bisect.bisect_left(ts, t0)
        i1 = bisect.bisect_right(ts, t0 + w_s)
        w = pts[i0:i1]
        window_s_k = w_s
        if len(w) < 0.5 * window_s_k / 60:
            raise RuntimeError(f"window {k} too sparse ({len(w)} points)")
        w_high = max(p[2] for p in w)
        w_low = min(p[1] for p in w)
        dd = max((w_high - w_low) / w_high, MIN_WINDOW_DD)
        # rescale=False: raw windows (classic llamma-simulator). True: v2's
        # VolatilityPriceHistoryLoader stretch to the span's max drawdown.
        r = (maxdd / dd) if rescale else 1.0
        resc = lambda v: w_high - (w_high - v) * r          # noqa: E731
        if wick:
            p0 = resc(w[0][2])                  # first minute's close = entry
            path = []
            for p in w:
                rel = p[0] - w[0][0]
                path.append([rel, resc(p[2]) / p0])          # high first
                path.append([rel + 30.0, resc(p[1]) / p0])   # then low
            path.append([float(window_s_k), resc(w[-1][3]) / p0])
        else:
            scaled = [resc(p[3]) for p in w]
            p0 = scaled[0]
            n_grid = int(window_s_k // DT_S) + 1
            path, j = [], 0
            for g in range(n_grid):
                tk = w[0][0] + g * DT_S
                while j + 1 < len(w) and w[j + 1][0] <= tk:
                    j += 1
                if j + 1 < len(w) and w[j + 1][0] > w[j][0]:
                    f = min(1.0, max(0.0,
                            (tk - w[j][0]) / (w[j + 1][0] - w[j][0])))
                    v = scaled[j] + (scaled[j + 1] - scaled[j]) * f
                else:
                    v = scaled[j]
                path.append([g * DT_S, v / p0])
        out.append({
            "path": path,
            "true_s": float(window_s_k),
            "p_end": path[-1][1],
            "seed": warm_ema(pts, ts, w[0][0], ma_half_s),
            "start_utc": time.strftime("%m-%d %H:%M", time.gmtime(w[0][0])),
            "real_dd_pct": round(dd * 100, 3),
            "rescale": round(r, 2),
            "dd_from_start_pct": round((1 - min(p[1] for p in path)) * 100, 2),
            "end_over_start": round(path[-1][1], 6),
        })
    return maxdd, out


# ---- engine plumbing --------------------------------------------------------
def build_base(A: int, sizing: dict, placement: str = "pinned") -> tuple[Path, dict]:
    """Snapshot for one A (fee patched per cell later — placement-neutral).
    placement="pinned": llamma-simulator anchoring — grid rebuilt so the
    ladder's top band edge sits a hair above the entry price, bands 1..N.
    Identical zero-cushion landing for every A (no grid-phase sawtooth).
    placement="maxltv": on-chain calculate_debt_n1 placement (phase lottery).
    Debt is always UTIL≈max of the ladder; what varies by rule is where the
    collateral comes from — fixed (LLV1) or derived from the cap (LLV2)."""
    wd = SCRATCH / f"A{A}_{placement}"
    wd.mkdir(parents=True, exist_ok=True)
    grid = single_user.BandGrid(A)
    ld_wei = int(LOAN_DISC_PCT / 100 * 1e18)
    if sizing["rule"] == "llv2-cap":
        # max-LTV ratio at this A, then collateral so debt==cap is max allowed
        ratio = single_user.max_borrowable(10 ** 18, N_BANDS, ld_wei,
                                           int(1e18), grid) / 1e18
        collateral_usd = sizing["cap_usd"] / (ratio * UTIL)
    else:
        collateral_usd = sizing["collateral_usd"]
    y0_wei = int(collateral_usd / START_PRICE * 1e18)
    maxb = single_user.max_borrowable(y0_wei, N_BANDS, ld_wei, int(1e18), grid)
    debt_usd = maxb / 1e18 * UTIL
    snap = wd / "base_snap.json"
    info = single_user.build(collateral_usd, debt_usd, START_PRICE, START_PRICE,
                             N_BANDS, snap, workdir=wd, verbose=False,
                             loan_discount_pct=LOAN_DISC_PCT, llamma_A=A,
                             amm_fee_wei=int(0.3e16), amm_rate_wei=0,
                             pinned=(placement == "pinned"))
    return snap, info


# Progress heartbeat for the UI's fill-circle: one tick per finished unit of
# work (fetch chunk or engine run), written atomically so a concurrent GET
# never sees a torn file. Two phases: the ring fills once for the candle
# fetch, then resets and fills again for the engine runs.
_prog = {"path": None, "phase": "run", "done": 0, "total": 0}
_prog_lock = threading.Lock()


def _prog_phase(phase: str, total: int) -> None:
    with _prog_lock:
        _prog["phase"], _prog["done"], _prog["total"] = phase, 0, total
        _write_prog_locked()


def _prog_tick() -> None:
    with _prog_lock:
        _prog["done"] += 1
        if _prog["done"] % 4 and _prog["done"] != _prog["total"]:
            return
        _write_prog_locked()


def _write_prog_locked() -> None:
    p = _prog["path"]
    if not p:
        return
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps({"phase": _prog["phase"], "done": _prog["done"],
                               "total": _prog["total"]}))
    os.replace(tmp, p)


def run_batch(snap: Path, paths_file: Path, out: Path,
              window_s: int, dt_s: float, gas_usd: float,
              ma_time_s: float, venue: dict = None) -> list[list[dict]]:
    """One engine invocation, ALL windows: the engine loads the snapshot once
    and re-initializes state per path (bit-identical to N separate runs —
    verified by diff against the unbatched output). Paths-file entries carry
    per-window warm-EMA seeds ({"seed": s, "path": [...]})."""
    venue = venue or VENUE
    n_steps = int(window_s // dt_s) + 1
    cmd = [str(CPP),
           "--snapshot", str(snap), "--discount", str(LIQ_DISC_PCT),
           "--tvl-usd", str(venue["tvl_usd"]), "--A-raw", str(venue["A_raw"]),
           "--pool-type", venue["pool_type"], "--n-coins", str(venue["n_coins"]),
           "--crash-start-spot", str(START_PRICE), "--crash-end-spot", str(START_PRICE),
           "--price-paths", str(paths_file),
           "--ma-time-s", str(ma_time_s), "--oracle-seed", str(START_PRICE),
           "--gas-usd", str(gas_usd), "--hard-liq", "0",
           "--steps", str(n_steps), "--dt-s", str(dt_s),
           "--chart-rows", "130", "--out", str(out)]
    if venue.get("ss_A"):
        cmd += ["--ss-A", str(venue["ss_A"])]
    if venue.get("fee_1e10"):
        cmd += ["--venue-fee-1e10", str(venue["fee_1e10"])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"engine failed: {r.stderr[-800:]}")
    _prog_tick()
    return json.loads(out.read_text())


def llamma_all_x(bands: list, A: int, p_o: float) -> float:
    """llamma-simulator's get_all_x on the engine's emitted band rows
    ([n, x, y, p_up]) — float port of their get_x_down: every band's
    holdings are valued by adiabatic conversion INSIDE the band's own price
    range, so collateral is never marked at a market price below the ladder.
    This is the valuation behind their 1 - all_x/initial_all_x loss."""
    total = 0.0
    sqrt_ratio = math.sqrt(A / (A - 1))
    for _n, x, y, p_up in bands:
        if x == 0 and y == 0:
            continue
        p_down = p_up * (A - 1) / A
        p_mid = p_o ** 3 / p_down ** 2 * (A - 1) / A
        if x == 0 or y == 0:
            if p_o > p_up:
                y_equiv = y if y > 0 else x / p_mid
                total += y_equiv * p_up / sqrt_ratio
                continue
            if p_o < p_down:
                total += x if x > 0 else y * p_mid
                continue
        a = p_o * A
        b = p_up / p_o * (A - 1) * x + p_o ** 2 / p_up * A * y
        disc = b * b + 4 * a * x * y
        y0 = (b + math.sqrt(disc)) / (2 * a)
        g = y0 * p_up / p_o * (A - 1)
        f = y0 * p_o ** 2 / p_up * A
        inv = (f + x) * (g + y)
        if p_o > p_up:
            y_o = max(inv / f, g) - g
            total += y_o * p_up / sqrt_ratio
        elif p_o < p_down:
            x_o = max(inv / g, f) - f
            total += x_o
        else:
            y_o = A * y0 * (1 - p_down / p_o)
            x_o = max(inv / (g + y_o), f) - f
            total += x_o + y_o * math.sqrt(p_down * p_o)
    return total


def sweep_cell(A: int, fee_pct: float, base_snap: Path, info: dict,
               windows: list[dict], paths_file: Path,
               window_s: int, dt_s: float, gas_usd: float,
               ma_time_s: float, venue: dict = None) -> dict:
    wd = SCRATCH / f"A{A}" / f"f{fee_pct}"
    wd.mkdir(parents=True, exist_ok=True)
    s = json.loads(base_snap.read_text())
    s["state"]["fee"] = str(int(round(fee_pct * 1e16)))
    snap = wd / "snap.json"
    snap.write_text(json.dumps(s))

    y0 = info["snapshot_collateral"]
    all_rows = run_batch(snap, paths_file, wd / "rows.json", window_s,
                         dt_s, gas_usd, ma_time_s, venue=venue)
    per_path = []
    for k, rows in enumerate(all_rows):
        # Random-duration windows are padded flat to the longest one so the
        # batch shares --steps; measure each at its OWN true end row.
        true_s = windows[k].get("true_s", windows[k]["path"][-1][0])
        end = min(rows, key=lambda r: abs(r["elapsed_s"] - true_s))
        p_end = windows[k].get("p_end", windows[k]["path"][-1][1])
        val = end["comp_lend_usd"] + end["comp_coll_tokens"] * p_end
        # Two readings of the SAME run (the UI toggles between them):
        #   init — llamma-simulator's 1 - all_x/initial_all_x: end value vs
        #          entry value; the market drop on unconverted collateral
        #          counts as loss.
        #   hodl — vs holding the collateral to window end: only the EXTRA
        #          loss soft-liq churn caused.
        loss_init = (1.0 - val / (y0 * START_PRICE)) * 100.0
        loss_hodl = (1.0 - val / (y0 * p_end)) * 100.0
        # llamma-simulator's own valuation: band-adiabatic all_x at the
        # window's start and end oracle — never marks below the ladder.
        ax0 = llamma_all_x(rows[0]["bands"], A, windows[k]["path"][0][1])
        ax1 = llamma_all_x(end["bands"], A, p_end)
        loss_bands = (1.0 - ax1 / ax0) * 100.0 if ax0 > 0 else 0.0
        per_path.append({
            "start_utc": windows[k]["start_utc"],
            "dd_pct": windows[k]["dd_from_start_pct"],
            "loss_init_pct": round(loss_init, 3),
            "loss_hodl_pct": round(loss_hodl, 3),
            "loss_bands_pct": round(loss_bands, 3),
            # peak crvUSD held = how deep into soft-liq the window pushed us
            "max_x_usd": max(r["comp_lend_usd"] for r in rows),
            "hard_liq_usd": sum(r["hardLiqUsd"] for r in rows),  # must be 0
        })
    cell = {"A": A, "fee_pct": fee_pct,
            "debt_usd": round(info["debt_usd"]),
            "ltv_pct": round(info["ltv_pct"], 2),
            "n1": info["n1"], "n2": info["n2"],
            "paths": per_path}
    for m in ("init", "hodl", "bands"):
        losses = sorted((p[f"loss_{m}_pct"] for p in per_path), reverse=True)
        n_top = max(1, len(losses) // 20)       # llamma's samples//20 = worst 5%
        cell[f"top5_{m}_pct"] = round(sum(losses[:n_top]) / n_top, 3)
        cell[f"worst_{m}_pct"] = losses[0]
        cell[f"mean_{m}_pct"] = round(sum(losses) / len(losses), 3)
        cell["n_top"] = n_top
    return cell


def spread(lo: float, hi: float, n: int) -> list[float]:
    if n <= 1:
        return [lo]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-min", type=int, default=100)
    ap.add_argument("--a-max", type=int, default=180)
    ap.add_argument("--fee-min", type=float, default=0.05)
    ap.add_argument("--fee-max", type=float, default=0.5)
    ap.add_argument("--grid", type=int, default=12, help="N -> NxN grid")
    ap.add_argument("--paths", type=int, default=10,
                    help="number of windows stepped across the span")
    ap.add_argument("--span-h", type=float, default=48.0,
                    help="history span to fetch (hours)")
    ap.add_argument("--window-h", type=float, default=5.0,
                    help="window length per engine run (hours)")
    ap.add_argument("--collateral", default=TEST_COLLATERAL)
    ap.add_argument("--wick", action="store_true",
                    help="path visits each minute's high then low "
                         "(llamma-simulator stepping) at dt=30s")
    ap.add_argument("--placement", choices=("pinned", "maxltv"),
                    default="pinned",
                    help="pinned = llamma-simulator anchoring (ladder top at "
                         "entry price, identical for every A); maxltv = "
                         "on-chain placement (grid-phase lottery)")
    ap.add_argument("--window-mode", choices=("random", "stepped"),
                    default="stepped",
                    help="random = llamma-simulator sampling (uniform starts, "
                         "uniform duration win-min..win-max)")
    ap.add_argument("--win-min-m", type=float, default=30.0)
    ap.add_argument("--win-max-m", type=float, default=60.0)
    ap.add_argument("--rng-seed", type=int, default=1234)
    ap.add_argument("--add-reverse", action="store_true",
                    help="append the time-mirrored series (their add_reverse)")
    ap.add_argument("--no-rescale", action="store_true",
                    help="raw windows (classic llamma-simulator); default "
                         "applies v2's drawdown rescale")
    ap.add_argument("--arb", choices=("venue", "their"), default="venue",
                    help="their = llamma-simulator conditions: gas $0, deep "
                         "flat stableswap venue at 5bp (their ext_fee)")
    ap.add_argument("--gas-usd", type=float, default=GAS_USD,
                    help="flat $/tx for the arbs (llamma-simulator has none)")
    ap.add_argument("--ma-exp-time", type=float, default=MA_EXP_TIME_S,
                    help="oracle EMA exp time constant in ON-CHAIN units "
                         "(NG ma_exp_time; 866 = 600s half-life)")
    ap.add_argument("--out", type=Path, default=HERE / "data" / "sldl.json")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--progress-out", type=Path, default=None,
                    help="heartbeat file {done,total} for the UI fill-circle")
    args = ap.parse_args()

    SCRATCH.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    span_s = int(args.span_h * 3600)
    window_s = int(args.window_h * 3600)
    if window_s >= span_s:
        raise SystemExit("--window-h must be smaller than --span-h")
    if args.progress_out:
        _prog["path"] = args.progress_out

    mk, grp = pick_market(args.collateral)
    sizing = borrower_sizing(mk, grp)
    hist = load_history(span_s, args.collateral, mk)
    dt_s = 30.0 if args.wick else DT_S
    # engine takes a HALF-LIFE; on-chain stores tau (half-life = tau * ln2)
    ma_time_s = args.ma_exp_time * math.log(2)
    import random as _random
    rng = _random.Random(args.rng_seed)
    maxdd, windows = step_windows(
        hist, args.paths, window_s, args.wick, mode=args.window_mode,
        win_min_s=int(args.win_min_m * 60), win_max_s=int(args.win_max_m * 60),
        rng=rng, ma_half_s=ma_time_s, add_reverse=args.add_reverse,
        rescale=not args.no_rescale)
    if args.window_mode == "random":
        window_s = int(max(w["true_s"] for w in windows))  # batch --steps
    venue = VENUE
    gas_usd = args.gas_usd
    if args.arb == "their":
        # llamma-simulator arb conditions: no gas, effectively infinite
        # external liquidity at a flat 5bp fee -> $2B flat stableswap at 5bp.
        gas_usd = 0.0
        venue = dict(tvl_usd=2_000_000_000.0, A_raw=10.0,
                     pool_type="stableswap", n_coins=2, ss_A=1000,
                     fee_1e10=50_000_000)
    # ALL windows in one file: each cell is a single engine invocation that
    # loads the snapshot once and runs every path (spawn/parse cost per cell,
    # not per path). Shorter random windows are padded flat to the longest.
    paths_file = SCRATCH / "paths_all.json"
    entries = []
    for w in windows:
        path = list(w["path"])
        if path[-1][0] < window_s:
            path.append([float(window_s), path[-1][1]])
        entries.append({"seed": w["seed"], "path": path})
    paths_file.write_text(json.dumps(entries))

    a_grid = sorted({round(v) for v in spread(args.a_min, args.a_max, args.grid)})
    fee_grid = sorted({round(v, 4) for v in spread(args.fee_min, args.fee_max, args.grid)})

    _prog_phase("run", len(a_grid) * len(fee_grid))   # one tick per cell now

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        bases = dict(zip(a_grid, ex.map(
            lambda A: build_base(A, sizing, args.placement), a_grid)))
        cells_in = [(A, f) for A in a_grid for f in fee_grid]
        cells = list(ex.map(
            lambda c: sweep_cell(c[0], c[1], *bases[c[0]], windows, paths_file,
                                 window_s, dt_s, gas_usd, ma_time_s, venue),
            cells_in))

    result = {
        "config": {
            "borrower_rule": sizing["text"],
            "market_group": grp,
            "util_pct": UTIL * 100,
            "n_bands": N_BANDS, "loan_discount_pct": LOAN_DISC_PCT,
            "start_price": START_PRICE, "horizon_h": args.window_h,
            "dt_s": dt_s, "ma_exp_time": args.ma_exp_time,
            "ma_time_s": round(ma_time_s, 2), "gas_usd": gas_usd,
            "wick": bool(args.wick),
            "placement": args.placement,
            "window_mode": args.window_mode,
            "win_min_m": args.win_min_m, "win_max_m": args.win_max_m,
            "add_reverse": bool(args.add_reverse),
            "rescale": not args.no_rescale,
            "arb": args.arb,
            "warm_ema_seeds": True,
            "venue": venue, "hard_liq": False, "rate_wei": 0,
            "n_paths": args.paths,
            "history": {
                "key": f"{hist['chain']}:{hist['pool']}",
                "base_symbol": hist["symbol"],
                "pool_name": hist["pool_name"],
                "span_h": args.span_h,
                "from_utc": time.strftime("%Y-%m-%d %H:%M",
                                          time.gmtime(hist["from"])),
                "to_utc": time.strftime("%Y-%m-%d %H:%M",
                                        time.gmtime(hist["to"])),
                "n_points": len(hist["points"]),
                "max_dd_pct": round(maxdd * 100, 3),
            },
            "loss_metrics": {
                "init": ("1 - (x_end + y_end*p_end)/(y0*p_start) — vs initial "
                         "value at MARKET prices (below-ladder drawdown counts)"),
                "hodl": ("1 - (x_end + y_end*p_end)/(y0*p_end) — vs HODL at "
                         "window end (excess loss from soft-liq churn only)"),
                "bands": ("1 - all_x_end/all_x_start, band-adiabatic valuation "
                          "— llamma-simulator's exact metric (get_all_x): "
                          "collateral never marked below its band"),
            },
        },
        "grid": {"A": a_grid, "fee_pct": fee_grid},
        "paths": [{k2: v for k2, v in w.items() if k2 != "path"}
                  for w in windows],
        "cells": cells,
        "runtime_s": round(time.time() - t0, 1),
        "generated_at": int(time.time()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    print(f"[sldl] {len(cells)} cells x {len(windows)} windows of "
          f"{args.window_h}h (maxdd {maxdd*100:.2f}%) "
          f"in {result['runtime_s']}s -> {args.out}")


if __name__ == "__main__":
    main()
