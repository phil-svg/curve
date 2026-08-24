#!/usr/bin/env python3
"""plot_params_vs_tvl.py — every LlamaLend market side by side: the CURRENT
on-chain LLAMMA parameters, one chart for A and one for the AMM base fee.

Same layout as the Spring-Cleaning charts: horizontal, one row per market,
ascending TVL bottom-to-top (largest on top), x numbers at both edges. Both
values come from data/markets.json, which fetch_markets.py refreshes hourly
via Multicall3 (A and fee are two of the batched per-market eth_calls).

    python3 plot_params_vs_tvl.py
"""
from __future__ import annotations
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
IMAGES = HERE / "images"

TAG = {"ethereum": "", "optimism": " ·op", "arbitrum": " ·arb",
       "fraxtal": " ·frax", "sonic": " ·sonic"}


def usd_short(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}k"
    return f"${v:,.0f}"


def load_rows(value_key: str) -> list[tuple]:
    M = json.loads((HERE / "data" / "markets.json").read_text())
    rows = []
    for grp in ("LLV1", "LLV2"):
        for m in M["groups"][grp]:
            v = m.get(value_key)
            if v is None:
                continue
            name = (f"{m['collateral']['symbol']}/{m['borrowed']['symbol']}"
                    + TAG.get(m["chain"], " ·" + m["chain"]))
            rows.append((m.get("market_tvl_usd") or 0, float(v), grp, name))
    rows.sort(key=lambda r: r[0])       # ascending TVL, largest ends up on top
    return rows


def render(rows: list[tuple], out: Path, xlabel: str, title: str,
           xmax: float, xfmt=lambda v: f"{v:g}") -> None:
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
        ax.annotate(xfmt(v), (v, i), textcoords="offset points",
                    xytext=(7, -2.5), fontsize=7, color="#8b949e", zorder=4)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{r[3]}  ·  {usd_short(r[0])}" for r in rows],
                       fontsize=8.5)
    ax.set_ylim(-0.8, len(rows) - 0.2)
    ax.set_xlim(0, xmax)
    ax.set_ylabel("market · TVL (collateral in the market) — ascending, largest on top")
    ax.set_xlabel(xlabel)
    ax.set_title(title, pad=24)
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
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {out}  ({len(rows)} markets)")


def main() -> None:
    rows_a = load_rows("llamma_A")
    render(rows_a, IMAGES / "llamma_a_vs_market_tvl.png",
           "LLAMMA A — higher = narrower bands",
           "LlamaLend: current LLAMMA A per market, ranked by TVL",
           xmax=max(r[1] for r in rows_a) * 1.12,
           xfmt=lambda v: f"{v:.0f}")
    rows_f = load_rows("amm_fee_pct")
    render(rows_f, IMAGES / "amm_fee_vs_market_tvl.png",
           "AMM base fee (%) — the dynamic fee applies on top",
           "LlamaLend: current AMM base fee per market, ranked by TVL",
           xmax=max(r[1] for r in rows_f) * 1.15,
           xfmt=lambda v: f"{v:.2f}")


if __name__ == "__main__":
    main()
