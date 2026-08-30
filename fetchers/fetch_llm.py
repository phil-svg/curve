#!/usr/bin/env python3
"""fetch_llm.py — security dataset for the LLM (LlamaLend markets) tab.

Everything comes from the Curve prices API (prices.curve.finance) — zero
RPC. Per market (the LLV1/LLV2 set already in data/markets.json):

  - market row from /v1/lending/markets/{chain}: vault, gauge, monetary
    policy, oracle + oracle_pools, created_at, current rates
  - daily history from .../{controller}/snapshots?agg=day — backfilled to
    inception once (budgeted across runs), then topped up incrementally
  - borrowers from /v1/lending/users/{chain}/{controller}/users, filtered
    to positions that are still OPEN. That endpoint returns every address
    that has ever borrowed, each frozen at its last known state, so an
    unfiltered read shows loans closed years ago as if they were live (one
    market with 2 loans and $2.21 of debt listed a $1.66M borrower whose
    position closed in May 2024). The indexer re-stamps a position every
    time it snapshots the market and leaves a closed one frozen at its
    closing tx, so the market's n_loans most-recently-stamped rows are its
    open loans -- verified to reproduce n_loans AND total_debt on all 99
    markets across ethereum, arbitrum, optimism, fraxtal and sonic.
  - parameter timeline derived from the daily snapshots (a change in
    loan/liquidation discount or A shows up as a day-boundary step)

Outputs:  data/llm.json            summary + borrowers + timelines
          data/llm_hist/<chain>_<controller>.json   daily series arrays
State:    data/llm_state.json      per-market snapshot cache + cursors
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "pylib"))
sys.path.insert(0, str(HERE / "fetchers"))
DATA = HERE / "data"
HIST_DIR = DATA / "llm_hist"
OUT = DATA / "llm.json"
STATE = DATA / "llm_state.json"
API = "https://prices.curve.finance/v1/lending"

BACKFILL_BUDGET = 400          # snapshot window calls per run (100 days each)
TOP_BORROWERS = 200            # must exceed the busiest market's open loans
                               # (71 today) or live rows fall off page 1
PAUSE_S = 0.15                 # be polite to the API
STALE_SNAP_S = 24 * 3600       # beyond this the row's health/collateral are
                               # history — quiet markets are snapshotted
                               # rarely (one sonic market sits 7 weeks back)
DEBT_TOL = 0.005               # fresh rows must rebuild total_debt to 0.5%
AGED_TOL = 0.10                # an aged snapshot has not seen the interest
                               # accrued since, so judge it loosely


# ---- AMM fee timeline (the ONE thing the API snapshots don't carry) --------
# SetFee logs per chain in a single address-array getLogs pass, incremental
# from the last scanned block — a handful of RPC calls per cycle.
def _topic(sig: str) -> str:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


def _block_ts(rpc, blk: int, cache: dict) -> int:
    if blk not in cache:
        b = rpc.raw("eth_getBlockByNumber", [hex(blk), False])
        cache[blk] = int(b["timestamp"], 16)
    return cache[blk]


def _block_at_ts(rpc, ts: int, head: int, cache: dict) -> int:
    lo, hi = 1, head
    while lo < hi:
        mid = (lo + hi) // 2
        if _block_ts(rpc, mid, cache) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _fee_logs(rpc, addrs, frm, to, topic):
    return rpc.raw("eth_getLogs", [{
        "fromBlock": hex(frm), "toBlock": hex(to),
        "address": addrs, "topics": [topic]}])


def fee_timelines(groups: dict, created: dict, st: dict) -> dict:
    """key -> {"points": [[ts, fee_pct], ...]} — deployment fee first,
    then every SetFee change. Scan state persists in st["fee_scan"]."""
    from common import sel
    from fetch_markets import Rpc
    topic = _topic("SetFee(uint256)")
    fs = st.setdefault("fee_scan", {})
    by_chain: dict = {}
    for grp, lst in groups.items():
        for m in lst:
            by_chain.setdefault(m["chain"], []).append(m)
    out: dict = {}
    for chain, ms in by_chain.items():
        cst = fs.setdefault(chain, {"last": None, "events": {}, "init": {}})
        try:
            rpc = Rpc(chain)
            head = int(rpc.raw("eth_blockNumber", []), 16)
            bts: dict = {}
            frm = (cst["last"] + 1) if cst["last"] else None
            if frm is None:
                anchors = [created.get(f"{chain}:{m['controller'].lower()}")
                           for m in ms]
                anchors = [a for a in anchors if a]
                t0 = (min(anchors) - 86400) if anchors else None
                frm = _block_at_ts(rpc, t0, head, bts) if t0 else max(
                    1, head - 3_000_000)
            addrs = [m["amm"] for m in ms if m.get("amm")]
            if frm <= head:
                try:
                    logs = _fee_logs(rpc, addrs, frm, head, topic)
                except Exception as e:
                    mm = re.search(r"max(?:imum)? block range:?\s*(\d+)",
                                   str(e))
                    step = int(mm.group(1)) if mm else 100_000
                    logs, b = [], frm
                    while b <= head:
                        logs += _fee_logs(rpc, addrs, b,
                                          min(head, b + step - 1), topic)
                        b += step
                for lg in sorted(logs, key=lambda x: int(
                        x["blockNumber"], 16)):
                    a = lg["address"].lower()
                    blk = int(lg["blockNumber"], 16)
                    pct = round(int(lg["data"], 16) / 1e16, 4)
                    evs = cst["events"].setdefault(a, [])
                    ts = _block_ts(rpc, blk, bts)
                    if not any(e2[0] == ts and e2[1] == pct for e2 in evs):
                        evs.append([ts, pct, blk])
                    # fee BEFORE the first change: archive read at blk-1
                    if a not in cst["init"]:
                        try:
                            r = rpc.raw("eth_call", [
                                {"to": a, "data": sel("fee()")},
                                hex(evs[0][2] - 1)])
                            cst["init"][a] = round(int(r, 16) / 1e16, 4)
                        except Exception:
                            cst["init"][a] = None
                cst["last"] = head
        except Exception as e:
            print(f"[llm] fee scan {chain} failed: {str(e)[:120]}")
        for m in ms:
            key = f"{chain}:{m['controller'].lower()}"
            a = (m.get("amm") or "").lower()
            evs = sorted(cst["events"].get(a, []))
            cur = m.get("amm_fee_pct")
            t0 = created.get(key)
            pts = []
            first = (cst["init"].get(a) if evs else cur)
            if t0 and first is not None:
                pts.append([t0, first])
            for ts, pct, _b in evs:
                pts.append([ts, pct])
            if not pts and cur is not None:
                pts.append([t0 or int(time.time()) - 86400, cur])
            out[key] = {"points": pts}
    return out


def _get(url: str, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curve-sim"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2.0 * (i + 1))
            last = e
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


def api_market_rows() -> dict:
    """controller(lower) -> market row, across every lending chain."""
    rows = {}
    for ch in _get(f"{API}/chains/").get("data", []):
        try:
            for m in _get(f"{API}/markets/{ch}?per_page=500").get("data", []):
                c = (m.get("controller") or "").lower()
                if c:
                    m["_chain"] = ch
                    rows[f"{ch}:{c}"] = m
        except Exception as e:
            print(f"[llm] markets list {ch} failed: {e}")
        time.sleep(PAUSE_S)
    return rows


def fetch_snapshot_window(chain, ctrl, start, end):
    q = urllib.parse.urlencode({"agg": "day", "start": start, "end": end})
    d = _get(f"{API}/markets/{chain}/{ctrl}/snapshots?{q}")
    return d.get("data", [])


ROUND = {"ba": 4, "la": 4, "u": 2, "d": 2, "du": 0, "a": 2, "au": 0,
         "c": 4, "cu": 0, "p": 6, "pu": 6, "ld": 4, "qd": 4}


def row_compact(r: dict) -> dict:
    c = r.get("collateral_balance") or 0
    cu = r.get("collateral_balance_usd") or 0
    au = r.get("total_assets_usd") or 0
    du = r.get("total_debt_usd") or 0
    d_raw, a_raw = r.get("total_debt") or 0, r.get("total_assets") or 0
    out = {
        "ba": r.get("borrow_apr"), "la": r.get("lend_apr"),
        # raw token units, not USD: both sides share the unit, so no
        # price-conversion wobble pushing utilization past 100%
        "u": min(100.0, d_raw / a_raw * 100) if a_raw else None,
        "d": r.get("total_debt"), "du": du,
        "a": r.get("total_assets"), "au": au,
        "c": c, "cu": cu,
        "p": r.get("price_oracle"),
        "pu": (cu / c) if c else None,
        "n": r.get("n_loans"),
        "A": r.get("amm_a"),
        # discounts are raw 1e18 in the snapshots (1e16 = 1%)
        "ld": (r.get("loan_discount") or 0) / 1e16 or None,
        "qd": (r.get("liquidation_discount") or 0) / 1e16 or None,
    }
    for k, nd in ROUND.items():
        if isinstance(out.get(k), float):
            out[k] = round(out[k], nd)
    return out


def update_snapshots(st: dict, key: str, chain: str, ctrl: str,
                     created_ts: int | None, budget: list) -> dict:
    """Incremental daily-snapshot cache for one market. st[key] =
    {"days": {ts_str: row}, "done_to": oldest_reached_ts | None}."""
    cst = st.setdefault(key, {"days": {}, "done_to": None})
    days = cst["days"]
    now = int(time.time())

    def ingest(rows) -> int:
        got = 0
        for r in rows:
            try:
                t = _ts(r["timestamp"])
            except (KeyError, ValueError):
                continue
            days[str(t)] = row_compact(r)
            got += 1
        return got

    # top-up: everything since the newest cached day (re-fetch the last
    # 2 days so a partially-aggregated day gets corrected)
    newest = max((int(t) for t in days), default=None)
    if budget[0] > 0:
        start = (newest - 2 * 86400) if newest else max(
            created_ts or 0, now - 99 * 86400)
        try:
            budget[0] -= 1
            ingest(fetch_snapshot_window(chain, ctrl, start, now))
        except Exception as e:
            print(f"[llm] snapshots top-up {key} failed: {e}")
        time.sleep(PAUSE_S)

    # backfill toward inception (windowed, budgeted, resumes across runs)
    floor = created_ts or 0
    while budget[0] > 0 and cst.get("done_to") != "done":
        oldest = min((int(t) for t in days), default=now)
        if oldest <= floor + 86400:
            cst["done_to"] = "done"
            break
        try:
            budget[0] -= 1
            rows = fetch_snapshot_window(chain, ctrl, floor, oldest - 1)
        except Exception as e:
            print(f"[llm] snapshots backfill {key} failed: {e}")
            break
        time.sleep(PAUSE_S)
        if not ingest(rows):
            cst["done_to"] = "done"
            break
        cst["done_to"] = min(int(t) for t in days)
    return cst


def timeline_from_days(days: dict) -> list:
    """Parameter-change events out of the daily series."""
    tracked = (("A", "A"), ("ld", "loan discount %"),
               ("qd", "liquidation discount %"))
    ts_sorted = sorted(int(t) for t in days)
    events, prev = [], {}
    for t in ts_sorted:
        row = days[str(t)]
        for k, label in tracked:
            v = row.get(k)
            if v is None:
                continue
            if k in prev and prev[k] != v:
                events.append({"t": t, "param": label,
                               "old": prev[k], "new": v})
            prev[k] = v
    return events


def fetch_borrower_rows(chain, ctrl, n_loans):
    """Raw user rows, newest-stamped first (the API's own order).

    Pages only as far as the open set requires — n_loans rows plus a margin
    for anyone who transacted after the last snapshot.
    """
    want = max(TOP_BORROWERS, (n_loans or 0) + 25)
    rows, count, page = [], None, 1
    while page <= 10:
        d = _get(f"{API}/users/{chain}/{ctrl}/users"
                 f"?page={page}&per_page={min(want, 500)}")
        got = d.get("data") or []
        rows += got
        count = d.get("count")
        if len(got) < min(want, 500) or len(rows) >= want:
            break
        page += 1
    return rows, count


def borrower_view(rows, count, n_loans, coll_usd_price, borrowed_usd_price,
                  api_debt):
    """Open positions only, plus the evidence that the filter is sound.

    The indexer re-stamps a position every time it snapshots the market and
    leaves a closed one frozen at its closing tx, so the market's n_loans
    most-recently-stamped rows ARE its open loans. Verified against live
    on-chain n_loans and total_debt on all 99 markets across five chains.

    Two things are then reported rather than assumed: `as_of`, the age of
    the newest snapshot behind these rows (health and collateral are only
    as fresh as that), and `gate`, whether the rows actually rebuild the
    market's own total_debt. A market whose snapshot has aged accrues
    interest the rows have not seen, so the tolerance widens with age
    instead of crying wolf.
    """
    rows = sorted(rows, key=lambda u: u.get("last") or "", reverse=True)
    known = n_loans is not None
    live = rows[:n_loans] if known else rows
    out = []
    for u in live:
        try:
            debt = float(u.get("debt") or 0)
            coll = float(u.get("collateral") or 0)
            bor = float(u.get("borrowed") or 0)   # borrowed tokens in AMM (SL)
        except ValueError:
            continue
        out.append({
            "a": u.get("user"),
            "d": round(debt, 2),
            "du": round(debt * (borrowed_usd_price or 1), 0),
            "cu": round(coll * (coll_usd_price or 0)
                        + bor * (borrowed_usd_price or 1), 0),
            "h": (round(float(u["health"]), 4)
                  if u.get("health") not in (None, "") else None),
            "sl": bool(u.get("soft_liquidation")),
            "t": _ts(u["last"]) if u.get("last") else None,
        })
    out.sort(key=lambda r: -r["du"])
    as_of = max((r["t"] for r in out if r["t"]), default=None)
    aged = bool(as_of and as_of < time.time() - STALE_SNAP_S)
    gate = None
    if known and api_debt is not None:
        live_debt = sum(float(u.get("debt") or 0) for u in live)
        err = abs(live_debt - api_debt) / max(api_debt, 1.0)
        gate = {"ok": err <= (AGED_TOL if aged else DEBT_TOL),
                "live": round(live_debt, 2), "market": round(api_debt, 2),
                "err_pct": round(err * 100, 3)}
    return {"n": len(out), "ever": count, "rows": out,
            "closed": max(0, (count or len(rows)) - len(out)) if known
                      else None,
            "as_of": as_of, "aged": aged, "unknown": not known, "gate": gate}


def exit_pools_map(mkts: dict) -> dict:
    """key -> [{a, n, tvl}]: every Curve pool >= $50k holding the market's
    collateral or its vault-underlying (census written by fetch_lp),
    TVL-sorted — the LLM tab's exit-liquidity list."""
    try:
        census = json.loads((DATA / "census.json").read_text())["pools"]
    except (OSError, ValueError, KeyError):
        return {}
    try:
        og = json.loads((DATA / "oracles.json").read_text())["markets"]
    except (OSError, ValueError, KeyError):
        og = {}
    out: dict = {}
    for group, lst in mkts.get("groups", {}).items():
        for m in lst:
            ch = m["chain"]
            key = f"{ch}:{m['controller'].lower()}"
            toks = {m["collateral"]["addr"].lower()}
            for a, n in ((og.get(key) or {}).get("nodes") or {}).items():
                if n.get("type") == "vault":
                    for g, r in (n.get("refs") or {}).items():
                        if g in ("asset", "underlying"):
                            toks.add(r.lower())
            rows = [{"a": a, "n": nm, "tvl": tvl, "c": coins[:4]}
                    for a, nm, tvl, coins in census.get(ch, [])
                    if tvl >= 50_000 and any(c in toks for c in coins)]
            rows.sort(key=lambda r: -r["tvl"])
            out[key] = rows
    return out


def main() -> None:
    mkts = load_json(DATA / "markets.json", None)
    if not mkts:
        raise SystemExit("run fetch_markets.py first")
    st = load_json(STATE, {})
    api_rows = api_market_rows()
    created: dict = {}
    for group, lst in mkts.get("groups", {}).items():
        for m in lst:
            key = f"{m['chain']}:{m['controller'].lower()}"
            row = api_rows.get(key) or {}
            if row.get("created_at"):
                try:
                    created[key] = _ts(row["created_at"])
                except ValueError:
                    pass
    fee_tl = fee_timelines(mkts["groups"], created, st)
    expools = exit_pools_map(mkts)
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    budget = [BACKFILL_BUDGET]
    out = {"generated_at": int(time.time()), "markets": {}}
    n_aged = n_unk = n_gate = 0

    for group, lst in mkts.get("groups", {}).items():
        for m in lst:
            chain, ctrl = m["chain"], m["controller"].lower()
            key = f"{chain}:{ctrl}"
            row = api_rows.get(key) or {}
            created_ts = created.get(key)

            cst = update_snapshots(st, key, chain, ctrl, created_ts, budget)
            days = cst["days"]
            ts_sorted = sorted(int(t) for t in days)
            hist = {"t": ts_sorted}
            for k in ("ba", "la", "u", "d", "du", "a", "au",
                      "c", "cu", "p", "pu", "n", "A", "ld", "qd"):
                hist[k] = [days[str(t)].get(k) for t in ts_sorted]
            # older cached days carry the USD-wobble utilization —
            # re-derive from raw token units and clamp at 100
            hist["u"] = [
                min(100.0, (dd or 0) / aa * 100) if aa else uu
                for dd, aa, uu in zip(hist["d"], hist["a"], hist["u"])]
            atomic_write(HIST_DIR / f"{chain}_{ctrl}.json", hist)

            entry = {
                "group": group, "chain": chain,
                "name": f"{m['collateral']['symbol']}/{m['borrowed']['symbol']}",
                "controller": ctrl, "amm": m.get("amm"),
                "vault": (row.get("vault") or "").lower() or None,
                "gauge": (row.get("gauge_address") or "").lower() or None,
                "policy": (row.get("policy") or "").lower() or None,
                "oracle": (row.get("oracle") or "").lower() or None,
                "oracle_pools": [p.lower() for p in
                                 (row.get("oracle_pools") or [])],
                "created_at": created_ts,
                "hist_days": len(ts_sorted),
                "hist_complete": cst.get("done_to") == "done",
                "timeline": timeline_from_days(days),
            }
            entry["exit_pools"] = expools.get(key) or []
            entry["fee_tl"] = fee_tl.get(key)
            pts = (entry["fee_tl"] or {}).get("points") or []
            for i in range(1, len(pts)):
                entry["timeline"].append({
                    "t": pts[i][0], "param": "AMM fee %",
                    "old": pts[i - 1][1], "new": pts[i][1]})
            entry["timeline"].sort(key=lambda x: x["t"])
            n_loans = row.get("n_loans")
            try:
                api_debt = float(row["total_debt"])
            except (KeyError, TypeError, ValueError):
                api_debt = None
            try:
                raw, count = fetch_borrower_rows(chain, ctrl, n_loans)
                entry["borrowers"] = borrower_view(
                    raw, count, n_loans, m.get("collateral_usd_price"),
                    m.get("borrowed_usd"), api_debt)
            except Exception as e:
                entry["borrowers"] = {"n": n_loans, "rows": [],
                                      "error": str(e)[:120]}
            b = entry["borrowers"]
            n_aged += bool(b.get("aged"))
            n_unk += bool(b.get("unknown"))
            n_gate += bool(b.get("gate") and not b["gate"]["ok"])
            time.sleep(PAUSE_S)
            out["markets"][key] = entry

    print(f"[llm] borrowers: {n_gate} markets failed the total_debt gate, "
          f"{n_aged} on a snapshot older than {STALE_SNAP_S // 3600}h, "
          f"{n_unk} with no n_loans to filter by")

    atomic_write(STATE, st)
    atomic_write(OUT, out)
    n_hist = sum(1 for v in out["markets"].values() if v["hist_days"])
    n_done = sum(1 for v in out["markets"].values() if v["hist_complete"])
    print(f"[llm] {len(out['markets'])} markets · history on {n_hist} "
          f"({n_done} complete to inception) · "
          f"backfill budget left {budget[0]}/{BACKFILL_BUDGET}")


if __name__ == "__main__":
    main()
