"""Convert chart2_prices.json (float chainlink prices, 8-dec sourced) into the
per-block {block: price_1e18} JSON the C++ sweep_precompute expects as
--oracle-file. No RPC — reuses already-fetched data.

Usage:
  python build_chainlink_oracle_file.py <chart2_prices.json> <out.json>
"""
from __future__ import annotations
import json, sys
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 50

def main():
    src = Path(sys.argv[1]); out = Path(sys.argv[2])
    rows = json.loads(src.read_text())
    prices: dict[str, str] = {}
    for r in rows:
        b = int(r["block"])
        # chart2 stored chainlink as float(chainlinkRaw)/1e8; recover to 1e18 by
        # * 1e18 via Decimal to avoid float error creeping into the on-chain math.
        v = Decimal(str(r["chainlink"])) * Decimal(10) ** 18
        prices[str(b)] = str(int(v))
    out.write_text(json.dumps(prices))
    print(f"wrote {out} ({len(prices)} blocks, {rows[0]['block']}..{rows[-1]['block']})")

if __name__ == "__main__":
    main()
