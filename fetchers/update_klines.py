#!/usr/bin/env python3
"""update_klines.py — daily top-up of the S.L./D.L. price histories.

Pure-Binance series (meta source "binance:<SYMBOL>") get the missing
minutes appended from the REST API. Then build_nav_klines rebuilds the
NAV-wrapper series and XAUM on the refreshed feeds — its daily-rate cache
is incremental, so a daily run costs a couple of multicalls. A replaced
file (the span in the name grew) is deleted together with its derived
_oracle_* sidecars; series the fetchers didn't write (no "source" in the
meta, e.g. the venue caches) are never touched.

    python3 fetchers/update_klines.py [--skip-nav]
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_binance_klines import fetch_api_from, write_series  # noqa: E402


def _trio(base: Path) -> list[Path]:
    return [base, Path(str(base) + ".bin"),
            Path(str(base).replace(".json", ".meta.json"))]


def _sidecars(base: Path) -> list[Path]:
    """Derived per-texp oracle files (v1 usd-basis) for a kline base."""
    return [Path(p) for p in glob.glob(str(base)[:-len(".json")]
                                       + "_oracle_*")]


def _metas() -> list[tuple[Path, dict]]:
    out = []
    for mf in sorted((ROOT / "data").glob("_ref_table_klines_*.meta.json")):
        try:
            out.append((mf, json.loads(mf.read_text())))
        except ValueError:
            continue
    return out


def update_binance() -> int:
    grew = 0
    for mf, m in _metas():
        src = str(m.get("source") or "")
        if not src.startswith("binance:") or "/" in src:
            continue                    # nav / derived (XAUM) / venue cache
        sym = src.split(":", 1)[1]
        base = Path(str(mf).replace(".meta.json", ".json"))
        binf = Path(str(base) + ".bin")
        if not binf.exists():
            print(f"[update] {m['symbol']}: no .bin, skipped")
            continue
        a = np.fromfile(binf, dtype="<f8").reshape(-1, 5)
        fresh = fetch_api_from(sym, int(a[-1, 0]) + 60_000)
        if not fresh:
            print(f"[update] {m['symbol']}: up to date")
            continue
        rows = [tuple(r) for r in a] + fresh
        span_h = int(round((rows[-1][0] - rows[0][0]) / 3.6e6))
        new_base = (ROOT / "data"
                    / f"_ref_table_klines_{m['symbol']}_{span_h}h.json")
        write_series(new_base, rows, m["symbol"], src)
        if new_base != base:
            for p in _trio(base) + _sidecars(base):
                p.unlink(missing_ok=True)
        grew += 1
    return grew


def prune_nav() -> None:
    """build_nav_klines writes new-span files; keep only the longest span
    per nav-built name (nav:* and the derived XAUM)."""
    best: dict[str, tuple[float, Path]] = {}
    victims: list[Path] = []
    for mf, m in _metas():
        src = str(m.get("source") or "")
        if not (src.startswith("nav:")
                or (src.startswith("binance:") and "/" in src)):
            continue
        name, days = m["symbol"], m["to"] - m["from"]
        base = Path(str(mf).replace(".meta.json", ".json"))
        if name not in best:
            best[name] = (days, base)
        elif days > best[name][0]:
            victims.append(best[name][1])
            best[name] = (days, base)
        else:
            victims.append(base)
    for base in victims:
        print(f"[update] pruning {base.name}")
        for p in _trio(base) + _sidecars(base):
            p.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-nav", action="store_true",
                    help="only top up the Binance feeds")
    args = ap.parse_args()
    update_binance()
    if not args.skip_nav:
        r = subprocess.run(
            [sys.executable, str(ROOT / "fetchers" / "build_nav_klines.py")],
            timeout=3000)
        if r.returncode != 0:
            raise SystemExit(f"build_nav_klines exited {r.returncode}")
        prune_nav()


if __name__ == "__main__":
    main()
