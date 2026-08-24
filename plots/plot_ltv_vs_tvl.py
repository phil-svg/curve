#!/usr/bin/env python3
"""plot_ltv_vs_tvl.py — every LlamaLend market side by side: TVL rank vs max LTV.

Markets are LINED UP along x, largest TVL first — not placed at their TVL value,
which stacked every name on top of its neighbours. Each tick is one market,
labelled with its name and TVL, so all of them are readable.

The y axis is INVERTED on purpose: 100% max LTV sits at the bottom, 0% at the
top. Higher on the chart = less leverage permitted = the more conservative
market.

    python3 plot_ltv_vs_tvl.py
"""
from __future__ import annotations
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "images" / "max_ltv_vs_market_tvl.png"


def max_ltv(A: int, ld: float, N: int = 4) -> float | None:
    """Local mirror of Controller.max_borrowable as an LTV fraction."""
    if not A or A < 2 or ld is None:
        return None
    r = (A - 1) / A
    G = A * (1 - r ** N) / (math.sqrt(A / (A - 1)) * N)
    phase = 1 - (1 / A) / 2               # mid-band assumption
    return max(0.0, (1 - ld / 100) * G * phase * (1 - 1e-4))


def usd_short(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}k"
    return f"${v:,.0f}"


def main() -> None:
    M = json.loads((HERE / "data" / "markets.json").read_text())
    rows = []
    for grp in ("LLV1", "LLV2"):
        for m in M["groups"][grp]:
            ltv = max_ltv(m["llamma_A"], m["loan_discount_pct"])
            if ltv is None:
                continue
            name = f"{m['collateral']['symbol']}/{m['borrowed']['symbol']}"
            tag = {"ethereum": "", "optimism": " ·op", "arbitrum": " ·arb",
                   "fraxtal": " ·frax", "sonic": " ·sonic"}
            name += tag.get(m["chain"], " ·" + m["chain"])
            rows.append((m.get("market_tvl_usd") or 0, ltv * 100, grp, name))
    # HORIZONTAL layout: one row per market, names as ordinary horizontal
    # text on the left. Ascending TVL bottom-to-top, so the biggest markets
    # sit at the top of the chart.
    rows.sort(key=lambda r: r[0])

    fig, ax = plt.subplots(figsize=(12.5, max(9, 0.24 * len(rows))))
    for grp, colour, mark in (("LLV1", "#58a6ff", "o"), ("LLV2", "#e3b341", "D")):
        ys = [i for i, r in enumerate(rows) if r[2] == grp]
        xs = [r[1] for r in rows if r[2] == grp]
        ax.scatter(xs, ys, s=74, c=colour, marker=mark, alpha=.95,
                   edgecolors="#0d1117", linewidths=.8, zorder=3,
                   label={"LLV1": "LLV1 (mint markets)",
                          "LLV2": "LLV2 (lend markets)"}[grp])
    # stems tie each dot to its name; bar length = permitted leverage
    for i, (_tvl, ltv, _g, _n) in enumerate(rows):
        ax.plot([0, ltv], [i, i], color="#30363d", lw=1, zorder=1)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{r[3]}  ·  {usd_short(r[0])}" for r in rows],
                       fontsize=8.5)
    ax.set_ylim(-0.8, len(rows) - 0.2)
    ax.set_xlim(0, 100)
    ax.set_ylabel("market · TVL (collateral in the market) — ascending, largest on top")
    ax.set_xlabel("max LTV at open (%) — longer bar = more leverage permitted")
    ax.set_title("LlamaLend: max LTV per market, ranked by TVL", pad=24)
    ax.grid(axis="x", alpha=.25, zorder=0)
    # x numbers at BOTH edges: on a chart this tall the reader is usually
    # nowhere near the bottom axis.
    ax.tick_params(axis="x", labeltop=True, top=False)
    ax.legend(loc="lower right", framealpha=.25)

    for s in ax.spines.values():
        s.set_color("#30363d")
    ax.tick_params(colors="#c9d1d9")
    ax.set_facecolor("#0d1117")
    fig.patch.set_facecolor("#0d1117")
    for item in (ax.title, ax.xaxis.label, ax.yaxis.label):
        item.set_color("#e6edf3")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT, dpi=140, facecolor=fig.get_facecolor())
    print(f"wrote {OUT}  ({len(rows)} markets)")


if __name__ == "__main__":
    main()
