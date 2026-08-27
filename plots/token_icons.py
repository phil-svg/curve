#!/usr/bin/env python3
"""token_icons.py — token logos for the matplotlib market charts, fetched
from the curve-assets CDN and cached in tmp/token_icons (empty file =
negative cache). Icons are placed with AnnotationBbox inside the axes."""
from __future__ import annotations

import urllib.request
from pathlib import Path

import matplotlib.image as mpimg
from matplotlib.offsetbox import AnnotationBbox, OffsetImage

HERE = Path(__file__).resolve().parent.parent
CACHE = HERE / "tmp" / "token_icons"


def icon_path(addr: str | None, chain: str | None = "ethereum"):
    if not addr:
        return None
    addr = addr.lower()
    d = "assets" if chain in (None, "", "ethereum") else f"assets-{chain}"
    f = CACHE / f"{d}_{addr}.png"
    if f.exists():
        return f if f.stat().st_size else None
    CACHE.mkdir(parents=True, exist_ok=True)
    url = ("https://cdn.jsdelivr.net/gh/curvefi/curve-assets/images/"
           f"{d}/{addr}.png")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curve-sim"})
        with urllib.request.urlopen(req, timeout=10) as r:
            f.write_bytes(r.read())
        return f
    except Exception:
        f.write_bytes(b"")            # negative cache
        return None


def icons_left_of_yticklabels(ax, items, gap_pts=4.0, room_in=0.28) -> None:
    """One icon per y-tick, right-aligned just LEFT of that tick's label
    text (the labels are right-aligned against the axis, so each icon's x
    comes from the label's measured extent). `items` = [(addr, chain), …]
    in tick order. Call AFTER tight_layout and before savefig — the axes
    are shifted right by `room_in` inches so the icons don't clip."""
    fig = ax.figure
    pos = ax.get_position()
    extra = room_in / fig.get_figwidth()
    ax.set_position([pos.x0 + extra, pos.y0, pos.width - extra, pos.height])
    fig.canvas.draw()                 # realize label extents
    renderer = fig.canvas.get_renderer()
    inv = ax.transAxes.inverted()
    gap = gap_pts * fig.dpi / 72.0
    for tl, (addr, chain) in zip(ax.get_yticklabels(), items):
        p = icon_path(addr, chain)
        if not p:
            continue
        try:
            img = mpimg.imread(p)
        except Exception:
            continue
        bb = tl.get_window_extent(renderer)
        x_frac = inv.transform((bb.x0 - gap, 0))[0]
        y = tl.get_position()[1]      # tick's data-coordinate y
        zoom = 15.0 / max(img.shape[0], img.shape[1])
        ab = AnnotationBbox(OffsetImage(img, zoom=zoom), (x_frac, y),
                            frameon=False,
                            xycoords=("axes fraction", "data"),
                            box_alignment=(1.0, 0.5), zorder=5)
        ab.set_clip_on(False)
        ax.add_artist(ab)


def add_icon(ax, addr, chain, xy, zoom=0.13, xycoords=None,
             box_alignment=(0.0, 0.5)) -> None:
    p = icon_path(addr, chain)
    if not p:
        return
    try:
        img = mpimg.imread(p)
    except Exception:
        return
    # normalize: curve-assets icons range 128..512px; target ~15 points
    zoom = 15.0 / max(img.shape[0], img.shape[1])
    ab = AnnotationBbox(OffsetImage(img, zoom=zoom), xy, frameon=False,
                        xycoords=xycoords or "data",
                        box_alignment=box_alignment, zorder=5)
    ab.set_clip_on(False)
    ax.add_artist(ab)
