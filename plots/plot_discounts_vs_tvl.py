#!/usr/bin/env python3
"""plot_discounts_vs_tvl.py — every LlamaLend market side by side, TVL ascending,
against its LOAN discount (chart 1) and its LIQUIDATION discount (chart 2).

These are the raw on-chain parameters, not the derived max-LTV: loan_discount
caps what can be borrowed, liquidation_discount sets where health crosses zero.
Both axes run 0% at the bottom, so bottom = smallest haircut = most leverage /
thinnest buffer — the same reading direction as the max-LTV chart.

    python3 plot_discounts_vs_tvl.py
"""
from __future__ import annotations
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
IMAGES = HERE / "images"


def usd_short(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}k"
    return f"${v:,.0f}"


def load_rows(field: str):
    M = json.loads((HERE / "data" / "markets.json").read_text())
    rows = []
    for grp in ("LLV1", "LLV2"):
        for m in M["groups"][grp]:
            v = m.get(field)
            if v is None:
                continue
            name = f"{m['collateral']['symbol']}/{m['borrowed']['symbol']}"
            tag = {"ethereum": "", "optimism": " ·op", "arbitrum": " ·arb",
                   "fraxtal": " ·frax", "sonic": " ·sonic"}
            name += tag.get(m["chain"], " ·" + m["chain"])
            rows.append((m.get("market_tvl_usd") or 0, v, grp, name))
    rows.sort(key=lambda r: r[0])          # ascending: smallest left
    return rows


def draw(field: str, nice: str, out_name: str, note: str) -> None:
    rows = load_rows(field)
    # HORIZONTAL: one row per market, names as ordinary horizontal text on
    # the left, TVL ascending bottom-to-top (largest markets on top).
    fig, ax = plt.subplots(figsize=(12.5, max(9, 0.24 * len(rows))))
    for grp, colour, mark in (("LLV1", "#58a6ff", "o"), ("LLV2", "#e3b341", "D")):
        ys = [i for i, r in enumerate(rows) if r[2] == grp]
        xs = [r[1] for r in rows if r[2] == grp]
        ax.scatter(xs, ys, s=74, c=colour, marker=mark, alpha=.95,
                   edgecolors="#0d1117", linewidths=.8, zorder=3,
                   label={"LLV1": "LLV1 (mint markets)",
                          "LLV2": "LLV2 (lend markets)"}[grp])
    for i, (_tvl, v, _g, _n) in enumerate(rows):
        ax.plot([0, v], [i, i], color="#30363d", lw=1, zorder=1)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{r[3]}  ·  {usd_short(r[0])}" for r in rows],
                       fontsize=8.5)
    ax.set_ylim(-0.8, len(rows) - 0.2)
    ax.set_xlim(0, 55)                     # shared scale across both charts
    ax.set_ylabel("market · TVL (collateral in the market) — ascending, largest on top")
    ax.set_xlabel(f"{nice} (%) — shorter bar = smaller haircut")
    ax.set_title(f"LlamaLend: {nice} per market, ranked by TVL — {note}", pad=24)
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
    IMAGES.mkdir(parents=True, exist_ok=True)
    out = IMAGES / out_name
    fig.tight_layout()
    fig.savefig(out, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {out}  ({len(rows)} markets)")


if __name__ == "__main__":
    draw("loan_discount_pct", "loan discount",
         "loan_discount_vs_market_tvl.png",
         "caps how much can be borrowed")
    draw("liquidation_discount_pct", "liquidation discount",
         "liquidation_discount_vs_market_tvl.png",
         "sets where health crosses zero")
