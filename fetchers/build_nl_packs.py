#!/usr/bin/env python3
"""build_nl_packs.py — build the new-llamalend tab's chart data by hand.

The server never builds these files and runs nothing on a timer: it serves data/nl_cache/packs/*.gz, and
unless the machine names its own node in data/nl_local.json it refuses everything that would cost an RPC call. This command builds those files on a machine that may read the
chain (past history windows come from its disk cache: a run costs each reading's open 5-day window, a
new market its whole history once). It builds and nothing else: it does not pull, push, upload or
schedule anything. Getting a pack onto another machine is a manual step.

    python3 fetchers/build_nl_packs.py [--only a.json b.json]

Exit code 1 when a market failed to build.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def log(*a) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, help="market files to build (default: all of index.json)")
    a = ap.parse_args()
    import nl_api
    files = [f for f in nl_api.market_files() if not a.only or f in a.only]
    if not files:
        log("no markets in nl/markets/index.json")
        return 1
    bad = 0
    for f in files:
        t = time.time()
        try:
            r = nl_api.build_pack(f)
            log(f"built {f}: {r['points']:,} points, {r['bytes'] / 1e6:.1f} MB, {time.time() - t:.0f} s -> {nl_api.PACKS / (f + '.gz')}")
        except Exception as e:
            bad += 1
            log(f"BUILD FAILED {f}: {str(e)[:200]}")
    log(f"done: {len(files) - bad} built, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
