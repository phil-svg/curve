#!/usr/bin/env python3
"""fetch_mint.py — the crvUSD mint markets (the controllers of the mint
ControllerFactory) for the Mint Markets tab. Curve prices API only, no RPC.

  - current state: /v1/crvusd/markets/ethereum
  - daily history: /v1/crvusd/markets/ethereum/{controller}/snapshots?agg=day,
    backfilled to each market's creation in 100-day windows (budgeted,
    resumes across runs), then topped up incrementally
  - largest borrowers: /v1/crvusd/markets/ethereum/{controller}/borrowers

Weighted borrow rate, as the scrvUSD Telegram bot computes it
(scrvUSDbot/src/scrvUSD/AggregatedInterest.ts): every market's borrow APY
(LLAMMA rate() per second, compounded over a year: e^(r*T) - 1, which is the
API's borrow_apy) weighted by the controller's total_debt:
    sum(apy_i * debt_i) / sum(debt_i)
The page shows the same debt weighting over the APR (borrow_apr = r*T =
ln(1 + APY)): weighted_borrow_apr / series weighted_apr.

Mint interest goes 100 % to the DAO, so DAO revenue per day of a market is
debt_usd * borrow_apr / 365 (the APR, since interest accrues continuously).

BTC and ETH prices per day are the WBTC and WETH markets' oracle prices from
the same snapshots, so they share the day axis of the other series.

Outputs data/mint.json (markets, borrowers, weighted-rate series, totals) and
data/mint_hist/<controller>.json (daily arrays); state in data/mint_state.json.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DATA = HERE / "data"
HIST_DIR = DATA / "mint_hist"
OUT = DATA / "mint.json"
STATE = DATA / "mint_state.json"
API = "https://prices.curve.finance/v1/crvusd"
CHAIN = "ethereum"
BACKFILL_BUDGET = 200          # snapshot window calls per run (100 days each)
TOP_BORROWERS = 60             # 3 pages of the API's 20
PAUSE_S = 0.15


def _get(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curve-sim"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2.0 * (i + 1))
    raise last


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
               .timestamp())


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def atomic_write(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, p)


# -- daily history ------------------------------------------------------------
ROUND = {"apr": 4, "apy": 4, "d": 2, "du": 0, "c": 6, "cu": 0, "s": 2,
         "su": 0, "p": 6, "ap": 6, "ld": 4, "qd": 4, "mx": 3, "b": 0,
         "mi": 0, "rd": 0}


def row_compact(r: dict) -> dict:
    out = {
        "apr": r.get("borrow_apr"), "apy": r.get("borrow_apy"),
        "d": r.get("total_debt"), "du": r.get("total_debt_usd"),
        "c": r.get("total_collateral"), "cu": r.get("total_collateral_usd"),
        # crvUSD sitting in the bands = collateral already converted by
        # soft liquidation
        "s": r.get("total_stablecoin"), "su": r.get("total_stablecoin_usd"),
        "p": r.get("price_oracle"), "ap": r.get("amm_price"),
        "n": r.get("n_loans"), "A": r.get("amm_a"),
        # discounts are raw 1e18 in the snapshots (1e16 = 1 %)
        "ld": (r.get("loan_discount") or 0) / 1e16 or None,
        "qd": (r.get("liquidation_discount") or 0) / 1e16 or None,
        "mx": r.get("max_ltv"),
        "b": r.get("borrowable"),
        "mi": r.get("minted"), "rd": r.get("redeemed"),
    }
    for k, nd in ROUND.items():
        if isinstance(out.get(k), float):
            out[k] = round(out[k], nd)
    return out


def fetch_window(ctrl: str, start: int, end: int) -> list:
    q = urllib.parse.urlencode({"fetch_on_chain": "false", "agg": "day",
                                "start": start, "end": end})
    return _get(f"{API}/markets/{CHAIN}/{ctrl}/snapshots?{q}").get("data", [])


def update_snapshots(st: dict, ctrl: str, created_ts: int | None,
                     budget: list) -> dict:
    """st[ctrl] = {"days": {ts: row}, "done": bool}. Top-up first (the last
    2 cached days again, a partial day gets corrected), then backfill toward
    creation in 100-day windows until the API returns nothing older."""
    cst = st.setdefault(ctrl, {"days": {}, "done": False})
    days = cst["days"]
    now = int(time.time())

    def ingest(rows) -> int:
        got = 0
        for r in rows:
            try:
                t = _ts(r["dt"])
            except (KeyError, ValueError, TypeError):
                continue
            days[str(t)] = row_compact(r)
            got += 1
        return got

    newest = max((int(t) for t in days), default=None)
    if budget[0] > 0:
        start = (newest - 2 * 86400) if newest else max(
            created_ts or 0, now - 99 * 86400)
        try:
            budget[0] -= 1
            ingest(fetch_window(ctrl, start, now))
        except Exception as e:
            print(f"[mint] snapshots top-up {ctrl} failed: {e}")
        time.sleep(PAUSE_S)

    floor = created_ts or 0
    while budget[0] > 0 and not cst.get("done"):
        oldest = min((int(t) for t in days), default=now)
        if oldest <= floor + 86400:
            cst["done"] = True
            break
        try:
            budget[0] -= 1
            rows = fetch_window(ctrl, floor, oldest - 1)
        except Exception as e:
            print(f"[mint] snapshots backfill {ctrl} failed: {e}")
            break
        time.sleep(PAUSE_S)
        if not ingest(rows):
            cst["done"] = True
            break
    return cst


def usd_filled(r: dict) -> dict:
    """The API's older snapshots carry the USD fields as 0.0 next to real
    token amounts (6 Feb 2024 wstETH: 33.4M crvUSD of debt, $0). Where a USD
    field is 0 but its amount is not, derive it: debt and the crvUSD in the
    bands at $1 per crvUSD, collateral at the oracle price."""
    r = dict(r)
    if not r.get("du") and r.get("d"):
        r["du"] = r["d"]
    if not r.get("su") and r.get("s"):
        r["su"] = r["s"]
    if not r.get("cu") and r.get("c") and r.get("p"):
        r["cu"] = round(r["c"] * r["p"])
    return r


def hist_arrays(days: dict) -> dict:
    ts = sorted(int(t) for t in days)
    rows = {t: usd_filled(days[str(t)]) for t in ts}
    keys = ["apr", "apy", "d", "du", "c", "cu", "s", "su", "p", "ap", "n",
            "A", "ld", "qd", "mx", "b", "mi", "rd"]
    out = {"t": ts}
    for k in keys:
        out[k] = [rows[t].get(k) for t in ts]
    return out


# -- borrowers ------------------------------------------------------------------
def fetch_borrowers(ctrl: str) -> dict:
    rows, total = [], None
    for page in range(1, TOP_BORROWERS // 20 + 1):
        d = _get(f"{API}/markets/{CHAIN}/{ctrl}/borrowers?page={page}"
                 f"&per_page=20&sort_by=debt&sort_direction=desc")
        total = d.get("total_borrowers", total)
        got = d.get("borrowers") or []
        rows += [{"a": b.get("address"), "d": b.get("debt"),
                  "du": b.get("debt_usd"), "c": b.get("collateral"),
                  "cu": b.get("collateral_usd"), "h": b.get("health"),
                  "sl": bool(b.get("soft_liquidation")),
                  "pct": b.get("percent_of_total_debt")} for b in got]
        time.sleep(PAUSE_S)
        if len(got) < 20:
            break
    return {"n": total, "rows": rows, "as_of": int(time.time())}


# -- main ---------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    lst = _get(f"{API}/markets/{CHAIN}?fetch_on_chain=false&page=1"
               f"&per_page=100").get("data", [])
    st = load_json(STATE, {})
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    budget = [BACKFILL_BUDGET]
    markets = {}
    hists = {}
    for m in lst:
        ctrl = (m.get("address") or "").lower()
        if not ctrl:
            continue
        created = None
        try:
            created = _ts(m["created_at"]) if m.get("created_at") else None
        except (ValueError, TypeError):
            pass
        cst = update_snapshots(st, ctrl, created, budget)
        h = hist_arrays(cst["days"])
        hists[ctrl] = h
        atomic_write(HIST_DIR / f"{ctrl}.json", h)
        try:
            bor = fetch_borrowers(ctrl)
        except Exception as e:
            bor = {"error": str(e)[:120]}
        col = m.get("collateral_token") or {}
        apr = m.get("borrow_apr")
        du = m.get("total_debt_usd") or 0.0
        markets[ctrl] = {
            "controller": ctrl,
            "amm": (m.get("llamma") or "").lower(),
            "policy": (m.get("monetary_policy_address") or "").lower(),
            "oracle": (m.get("oracle") or "").lower(),
            "oracle_pools": [p.lower() for p in (m.get("oracle_pools") or [])],
            "factory": (m.get("factory_address") or "").lower(),
            "collateral": {"symbol": col.get("symbol"),
                           "addr": (col.get("address") or "").lower(),
                           "decimals": col.get("decimals")},
            "created_at": created,
            "borrow_apr": apr, "borrow_apy": m.get("borrow_apy"),
            "future_rate": m.get("future_rate"),
            "total_debt": m.get("total_debt"), "total_debt_usd": du,
            "n_loans": m.get("n_loans"),
            "A": m.get("amm_a"),
            "price_oracle": m.get("price_oracle"),
            "amm_price": m.get("amm_price"),
            "base_price": m.get("base_price"),
            "loan_discount_pct": (m.get("loan_discount") or 0) / 1e16 or None,
            "liquidation_discount_pct":
                (m.get("liquidation_discount") or 0) / 1e16 or None,
            "max_ltv": m.get("max_ltv"),
            "debt_ceiling": m.get("debt_ceiling"),
            "borrowable": m.get("borrowable"),
            "pending_fees": m.get("pending_fees"),
            "collected_fees": m.get("collected_fees"),
            "collateral_amount": m.get("collateral_amount"),
            "collateral_usd": m.get("collateral_amount_usd"),
            "stablecoin_amount": m.get("stablecoin_amount"),
            "stablecoin_usd": m.get("stablecoin_amount_usd"),
            "minted": m.get("minted"), "redeemed": m.get("redeemed"),
            "volume_24h_usd": m.get("volume_24h_usd"),
            "dao_rev_day": du * (apr or 0) / 100 / 365,
            "hist_days": len(h["t"]),
            "hist_complete": bool(cst.get("done")),
            "borrowers": bor,
        }

    # weighted borrow rate (the bot's definition) — now and per day
    num = sum((e["borrow_apy"] or 0) * (e["total_debt"] or 0)
              for e in markets.values())
    num_apr = sum((e["borrow_apr"] or 0) * (e["total_debt"] or 0)
                  for e in markets.values())
    den = sum((e["total_debt"] or 0) for e in markets.values())
    all_days = sorted({t for h in hists.values() for t in h["t"]})
    w_t, w_v, w_apr, tot_du, tot_rev, tot_cu = [], [], [], [], [], []
    for t in all_days:
        n = dd = na = da = du = rev = cu = 0.0
        for h in hists.values():
            try:
                i = h["t"].index(t)
            except ValueError:
                continue
            d, apy, apr = h["d"][i], h["apy"][i], h["apr"][i]
            if d and apy is not None:
                n += apy * d
                dd += d
            if d and apr is not None:
                na += apr * d
                da += d
            du += h["du"][i] or 0
            cu += h["cu"][i] or 0
            if h["du"][i] and apr is not None:
                rev += h["du"][i] * apr / 100 / 365
        if dd > 0:
            w_t.append(t)
            w_v.append(round(n / dd, 4))
            w_apr.append(round(na / da, 4) if da > 0 else None)
            tot_du.append(round(du))
            tot_rev.append(round(rev, 2))
            tot_cu.append(round(cu))
    # BTC / ETH price per day: the WBTC and WETH markets' oracle prices
    def oracle_series(symbol: str) -> list:
        h = next((hists[c] for c, e in markets.items()
                  if e["collateral"]["symbol"] == symbol), None)
        if not h:
            return [None] * len(w_t)
        m = dict(zip(h["t"], h["p"]))
        return [round(m[t], 2) if m.get(t) else None for t in w_t]

    out = {
        "generated_at": int(time.time()),
        "chain": CHAIN,
        "weighted_borrow_rate": (num / den) if den else None,
        "weighted_borrow_apr": (num_apr / den) if den else None,
        "totals": {"debt_usd": sum(e["total_debt_usd"] or 0
                                   for e in markets.values()),
                   "collateral_usd": sum(e["collateral_usd"] or 0
                                         for e in markets.values()),
                   "loans": sum(e["n_loans"] or 0 for e in markets.values()),
                   "dao_rev_day": sum(e["dao_rev_day"] for e in markets.values())},
        "series": {"t": w_t, "weighted_apy": w_v, "weighted_apr": w_apr,
                   "debt_usd": tot_du,
                   "collateral_usd": tot_cu, "dao_rev_day": tot_rev,
                   "btc_usd": oracle_series("WBTC"),
                   "eth_usd": oracle_series("WETH")},
        "markets": markets,
    }
    atomic_write(OUT, out)
    atomic_write(STATE, st)
    print(f"[mint] {len(markets)} markets, weighted borrow rate "
          f"{out['weighted_borrow_rate']:.4f} %, {len(w_t)} days of history, "
          f"{BACKFILL_BUDGET - budget[0]} snapshot calls, "
          f"{time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
