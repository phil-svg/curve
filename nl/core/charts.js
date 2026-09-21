// Small canvas charts for the new-llamalend tab. Colours come from the CSS
// custom properties of the host element, so each of the five designs themes
// its charts by setting variables, never by touching this file.
import { minMaxBuckets, lowerBound } from "./series.js";

// a colour is either a custom property of the host ("--nl-c1") or a literal ("#ED7D31")
const literal = name => typeof name === "string" && !name.startsWith("--");
const css = (el, name, dflt) => (literal(name) ? name : (getComputedStyle(el).getPropertyValue(name) || "").trim() || dflt);
const cssRef = name => (literal(name) ? name : `var(${name})`);
export const PALETTE = ["--nl-c1", "--nl-c2", "--nl-c3", "--nl-c4", "--nl-c5", "--nl-c6"];

export function fmtNum(x, sig = 5) {
  if (!Number.isFinite(x)) return "–";
  const a = Math.abs(x);
  if (a >= 1e9) return (x / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (x / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return Math.round(x).toLocaleString("en-US");
  if (a >= 100) return x.toFixed(1);
  if (a === 0) return "0";
  return Number(x.toPrecision(sig)).toString();
}
export function fmtUsd(x) { return Number.isFinite(x) ? "$" + fmtNum(x, 4) : "–"; }
export function fmtElapsed(s) {
  if (!Number.isFinite(s)) return "–";
  const a = Math.abs(s);
  if (a < 90) return Math.round(s) + "s";
  if (a < 5400) return (s / 60).toFixed(a < 600 ? 1 : 0) + "m";
  if (a < 172800) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
}
const two = n => String(n).padStart(2, "0");
export function fmtTime(ts, spanS) {
  const d = new Date(ts * 1000);
  const ymd = `${d.getUTCFullYear()}-${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())}`;
  if (spanS > 86400 * 200) return ymd.slice(0, 7);
  if (spanS > 86400 * 3) return ymd.slice(5);
  return `${ymd.slice(5)} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
}
export const fmtDateTime = ts => {
  const d = new Date(ts * 1000);
  return `${d.getUTCFullYear()}-${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
};

function niceTicks(lo, hi, count) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / Math.max(1, count), mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}

// lineChart(host, {height, xMode: "time"|"elapsed", xFmt(x), yFmt(y), zeroBase, minTop, empty,
//                  symlog: eps (signed log axis, linear inside +/-eps: every decade the same height),
//                  tipRows(i) -> [[label, value, colour]] (replaces the default tooltip rows),
//                  zoom: true (wheel zooms around the cursor, drag / horizontal scroll / Shift+wheel pans,
//                  double-click or the reset button shows everything again; y rescales to what is in view)})
//   .setData({t, series:[{name, v, color, width, dash, step, type: "line"|"bars"|"area",
//                         fill: "zero"|"bottom", fillAlpha, area (= faint fill to the bottom), legend: false}],
//             stack: bool (area series pile up), vlines:[{x, label}], hlines:[{y, label, color, width}], bands:[{from, to}],
//             marks:[{x, y, r, color}] (highlighted points), caption: text centred in the plot})   series: dots = point radius,
//             top: true = drawn over the others, halo: px of panel colour around the line
//   opts.onHover(x | null) / .setHover(x): one cursor line across stacked charts
//   opts.onView(view | null) fires when the user zooms or pans; .setView(a, b) shows a window pushed in from outside (0, 0 = everything)
export function lineChart(host, opts = {}) {
  host.classList.add("nl-chart");
  host.innerHTML = `<div class="nl-chart-legend"></div><div class="nl-chart-body"><canvas></canvas><div class="nl-chart-tip" hidden></div><button type="button" class="nl-chart-reset" hidden>show all</button></div>`;
  const legend = host.querySelector(".nl-chart-legend"), body = host.querySelector(".nl-chart-body"),
        cv = host.querySelector("canvas"), tip = host.querySelector(".nl-chart-tip"), resetBtn = host.querySelector(".nl-chart-reset");
  body.style.height = (opts.height || 220) + "px";
  const M = { l: 58, r: 12, t: 10, b: 24 };
  let data = null, hoverX = null, geom = null, view = null;      // view = {a, b}: the x window on screen (null = everything)

  function draw() {
    const W = body.clientWidth, H = body.clientHeight;
    if (!W || !H) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.width = W + "px"; cv.style.height = H + "px";
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const dim = css(host, "--nl-dim", "#8b949e"), line = css(host, "--nl-line", "#30363d");
    g.font = `11px ${css(host, "--nl-mono", "ui-monospace, SFMono-Regular, Menlo, monospace")}`;
    if (!data || !data.t || data.t.length < 2) {
      g.fillStyle = dim; g.textAlign = "center";
      g.fillText(opts.empty || "no data yet", W / 2, H / 2);
      geom = null;
      return;
    }
    // zoomed: draw the slice in view (one sample beyond each edge, clipped), so every loop below stays as it is
    const T = data.t, N = T.length, x0 = view ? view.a : T[0], x1 = view ? view.b : T[N - 1];
    const i0 = view ? Math.max(0, lowerBound(T, x0) - 1) : 0, i1 = view ? Math.min(N, lowerBound(T, x1) + 2) : N;
    const win = a => (i0 === 0 && i1 === N ? a : a.subarray ? a.subarray(i0, i1) : a.slice(i0, i1));
    const t = win(T), n = t.length, series = view ? data.series.map(s => ({ ...s, v: win(s.v) })) : data.series;
    let lo = Infinity, hi = -Infinity;
    // stacked areas: each series sits on the sum of the ones before it
    const base = new Map();
    if (data.stack) {
      const acc = new Float64Array(n);
      for (const s of series) {
        if (s.type !== "area") continue;
        base.set(s, Float64Array.from(acc));
        for (let i = 0; i < n; i++) if (Number.isFinite(s.v[i])) acc[i] += s.v[i];
      }
      for (let i = 0; i < n; i++) { if (acc[i] > hi) hi = acc[i]; if (acc[i] < lo) lo = acc[i]; }
    }
    for (const s of series) for (let i = 0; i < n; i++) {
      const v = s.v[i];
      if (Number.isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }
    for (const hl of data.hlines || []) { if (hl.y < lo) lo = hl.y; if (hl.y > hi) hi = hl.y; }
    if (lo === Infinity) { lo = 0; hi = 1; }
    if (opts.zeroBase || data.stack || series.some(s => s.type === "bars")) lo = Math.min(0, lo);
    if (opts.minTop !== undefined) hi = Math.max(hi, opts.minTop);
    let tr = v => v, ticks = null;
    if (opts.symlog) {
      // signed log: keeps the sign, is linear inside +/-eps so 0 exists, gives every decade the same height
      const eps = opts.symlog, top = Math.pow(10, Math.ceil(Math.log10(Math.max(eps * 10, Math.abs(lo), Math.abs(hi)))));
      tr = v => Math.sign(v) * Math.log10(1 + Math.abs(v) / eps);
      const neg = lo < 0, pos = hi > 0 || !neg;
      ticks = [0];
      for (let e = Math.round(Math.log10(eps)); e <= Math.log10(top) + 1e-9; e++) { if (pos) ticks.push(Math.pow(10, e)); if (neg) ticks.push(-Math.pow(10, e)); }
      lo = neg ? -tr(top) : 0; hi = pos ? tr(top) : 0;
    } else {
      if (hi === lo) { hi = lo + Math.abs(lo || 1) * 0.01; lo -= Math.abs(lo || 1) * 0.01; }
      const pad = (hi - lo) * 0.08; lo -= opts.zeroBase && lo === 0 ? 0 : pad; hi += pad;
    }
    const pw = W - M.l - M.r, ph = H - M.t - M.b;
    const X = x => M.l + ((x - x0) / (x1 - x0 || 1)) * pw, Y = v => M.t + (1 - (tr(v) - lo) / (hi - lo)) * ph;
    geom = { X, x0, x1, pw };
    // grid + axes
    g.strokeStyle = line; g.fillStyle = dim; g.lineWidth = 1; g.textAlign = "right"; g.textBaseline = "middle";
    const yt = ticks || niceTicks(lo, hi, 4), yStep = !ticks && yt.length > 1 ? yt[1] - yt[0] : undefined;
    for (const v of yt) {
      const y = Math.round(Y(v)) + 0.5;
      g.globalAlpha = 0.55; g.beginPath(); g.moveTo(M.l, y); g.lineTo(W - M.r, y); g.stroke(); g.globalAlpha = 1;
      g.fillText(opts.yFmt ? opts.yFmt(v, yStep) : fmtNum(v), M.l - 6, y);   // the step lets a custom formatter keep neighbouring ticks apart
    }
    g.textAlign = "center"; g.textBaseline = "top";
    const nx = Math.max(2, Math.floor(pw / 130));
    for (let k = 0; k <= nx; k++) {
      const xv = x0 + ((x1 - x0) * k) / nx;
      g.textAlign = k === nx ? "right" : "center";          // the last label ends at the plot's edge instead of being cut by it
      g.fillText(opts.xFmt ? opts.xFmt(xv) : opts.xMode === "elapsed" ? fmtElapsed(xv) : fmtTime(xv, x1 - x0), X(xv), H - M.b + 6);
    }
    g.save(); g.beginPath(); g.rect(M.l, M.t, pw, ph); g.clip();       // nothing is drawn outside the plot, zoomed or not
    for (const b of data.bands || []) {
      g.fillStyle = css(host, b.color || "--nl-accent", "#58a6ff"); g.globalAlpha = 0.10;
      g.fillRect(X(Math.max(x0, b.from)), M.t, Math.max(1, X(Math.min(x1, b.to)) - X(Math.max(x0, b.from))), ph);
      g.globalAlpha = 1;
    }
    // series
    for (const hl of data.hlines || []) {
      const y = Math.round(Y(hl.y)) + 0.5;
      g.strokeStyle = css(host, hl.color || "--nl-dim", "#8b949e"); g.setLineDash([4, 4]); g.lineWidth = hl.width || 1;
      g.beginPath(); g.moveTo(M.l, y); g.lineTo(W - M.r, y); g.stroke(); g.setLineDash([]);
      if (hl.label) { g.fillStyle = dim; g.textAlign = "right"; g.textBaseline = "bottom"; g.fillText(hl.label, W - M.r - 2, y - 2); }
    }
    series.map((s, si) => [s, si]).sort((p, q) => (p[0].top ? 1 : 0) - (q[0].top ? 1 : 0)).forEach(([s, si]) => {
      const col = css(host, s.color || PALETTE[si % PALETTE.length], "#58a6ff");
      if (s.type === "bars") {                     // per-step flows: a stem from 0 to the value
        const bw = Math.max(1, Math.min(6, pw / n - 1)), y0 = Y(0);
        g.fillStyle = col;
        for (let i = 0; i < n; i++) {
          const v = s.v[i];
          if (!Number.isFinite(v) || v === 0) continue;
          const y = Y(v);
          g.fillRect(X(t[i]) - bw / 2, Math.min(y, y0), bw, Math.max(1, Math.abs(y0 - y)));
        }
        return;
      }
      if (s.type === "area") {
        const b = base.get(s);
        g.beginPath();
        for (let i = 0; i < n; i++) { const top = (b ? b[i] : 0) + (Number.isFinite(s.v[i]) ? s.v[i] : 0); i ? g.lineTo(X(t[i]), Y(top)) : g.moveTo(X(t[i]), Y(top)); }
        for (let i = n - 1; i >= 0; i--) g.lineTo(X(t[i]), Y(b ? b[i] : 0));
        g.closePath(); g.globalAlpha = 0.55; g.fillStyle = col; g.fill(); g.globalAlpha = 1;
        // a crisp top edge over the translucent fill
        g.strokeStyle = col; g.lineWidth = s.width || 1.8; g.lineJoin = "round"; g.beginPath();
        for (let i = 0; i < n; i++) { const top = (b ? b[i] : 0) + (Number.isFinite(s.v[i]) ? s.v[i] : 0); i ? g.lineTo(X(t[i]), Y(top)) : g.moveTo(X(t[i]), Y(top)); }
        g.stroke();
        return;
      }
      const dense = n > pw * 2;
      if (s.fill || s.area) {                      // under every unbroken run of the line, down to 0 or to the bottom
        const yb = s.fill === "zero" ? Y(0) : M.t + ph;
        g.fillStyle = col; g.globalAlpha = s.fillAlpha ?? (s.fill ? 0.22 : 0.12);
        let run = [];
        const flush = () => {
          if (run.length > 1) { g.beginPath(); g.moveTo(run[0][0], yb); for (const q of run) g.lineTo(q[0], q[1]); g.lineTo(run[run.length - 1][0], yb); g.closePath(); g.fill(); }
          run = [];
        };
        for (let i = 0; i < n; i++) { if (Number.isFinite(s.v[i])) run.push([X(t[i]), Y(s.v[i])]); else flush(); }
        flush();
        g.globalAlpha = 1;
      }
      g.lineJoin = "round";
      g.beginPath();
      let pen = false;
      if (dense) {
        const b = minMaxBuckets(t, s.v, 0, n, Math.floor(pw));
        for (let k = 0; k < b.t.length; k++) {
          if (!Number.isFinite(b.lo[k])) { pen = false; continue; }
          const x = X(b.t[k]);
          if (!pen) { g.moveTo(x, Y(b.last[k])); pen = true; }
          g.lineTo(x, Y(b.lo[k])); g.lineTo(x, Y(b.hi[k])); g.lineTo(x, Y(b.last[k]));
        }
      } else {
        for (let i = 0; i < n; i++) {
          const v = s.v[i];
          if (!Number.isFinite(v)) { pen = false; continue; }
          const x = X(t[i]), y = Y(v);
          if (!pen) { g.moveTo(x, y); pen = true; }
          else if (s.step) { g.lineTo(x, Y(s.v[i - 1])); g.lineTo(x, y); }
          else g.lineTo(x, y);
        }
      }
      // halo: the same path first in the panel colour, wider, so this line stays readable where others cross it
      if (s.halo) { g.strokeStyle = css(host, "--nl-panel", "#0d1117"); g.lineWidth = (s.width || 1.6) + 2 * s.halo; g.setLineDash([]); g.stroke(); }
      g.strokeStyle = col; g.lineWidth = s.width || 1.6; g.setLineDash(s.dash || []);
      g.stroke(); g.setLineDash([]);
      if (s.dots && !dense) {
        g.fillStyle = col;
        for (let i = 0; i < n; i++) if (Number.isFinite(s.v[i])) { g.beginPath(); g.arc(X(t[i]), Y(s.v[i]), s.dots, 0, Math.PI * 2); g.fill(); }
      }
    });
    for (const mk of data.marks || []) { g.fillStyle = css(host, mk.color || "--nl-warn", "#e3b341"); g.beginPath(); g.arc(X(mk.x), Y(mk.y), mk.r || 4, 0, Math.PI * 2); g.fill(); }
    g.restore();
    if (data.caption) { g.fillStyle = dim; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(data.caption, M.l + pw / 2, M.t + ph / 2); }
    const placed = [];                                      // label boxes already drawn: [x0, x1, row]
    for (const vl of data.vlines || []) {
      const x = Math.round(X(vl.x)) + 0.5;
      g.strokeStyle = css(host, "--nl-dim", "#8b949e"); g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(x, M.t); g.lineTo(x, M.t + ph); g.stroke(); g.setLineDash([]);
      if (!vl.label) continue;
      // to the right of its line, or to the left when it would leave the plot; one row down for every label it would cover
      const w = g.measureText(vl.label).width, left = x + 4 + w > M.l + pw, a = left ? x - 4 - w : x + 4, b = a + w;
      let row = 0;
      while (placed.some(q => q[2] === row && a < q[1] + 6 && b > q[0] - 6)) row++;
      placed.push([a, b, row]);
      g.fillStyle = dim; g.textAlign = "left"; g.textBaseline = "top"; g.fillText(vl.label, a, M.t + 2 + row * 14);
    }
    if (hoverX !== null) {
      const x = Math.round(X(hoverX)) + 0.5;
      g.strokeStyle = css(host, "--nl-fg", "#e6edf3"); g.globalAlpha = 0.35;
      g.beginPath(); g.moveTo(x, M.t); g.lineTo(x, M.t + ph); g.stroke(); g.globalAlpha = 1;
    }
  }
  function paintLegend() {
    legend.innerHTML = !data ? "" : data.series.map((s, si) => s.legend === false ? "" :
      `<span class="nl-chart-key"><i style="background:${cssRef(s.color || PALETTE[si % PALETTE.length])}"></i>${s.name}</span>`).join("");
  }
  body.addEventListener("mousemove", e => {
    if (!geom || !data) return;
    const r = body.getBoundingClientRect(), px = e.clientX - r.left;
    const xv = geom.x0 + ((px - M.l) / geom.pw) * (geom.x1 - geom.x0);
    if (xv < geom.x0 || xv > geom.x1) { hoverX = null; tip.hidden = true; draw(); if (opts.onHover) opts.onHover(null); return; }
    let i = Math.min(data.t.length - 1, lowerBound(data.t, xv));
    if (i > 0 && xv - data.t[i - 1] < data.t[i] - xv) i--;
    hoverX = data.t[i];
    if (opts.onHover) opts.onHover(hoverX);
    tip.hidden = false;
    tip.innerHTML = `<b>${opts.xFmt ? opts.xFmt(data.t[i]) : opts.xMode === "elapsed" ? fmtElapsed(data.t[i]) : fmtDateTime(data.t[i])}</b>` +
      (opts.tipRows ? opts.tipRows(i).map(([lab, val, col]) => `<div><i style="background:${col ? cssRef(col) : "transparent"}"></i>${lab}<span>${val}</span></div>`).join("")
        : data.series.map((s, si) => `<div><i style="background:${cssRef(s.color || PALETTE[si % PALETTE.length])}"></i>${s.name}<span>${(opts.yFmt || fmtNum)(s.v[i])}</span></div>`).join(""));
    tip.style.left = Math.min(r.width - tip.offsetWidth - 8, Math.max(M.l, px + 12)) + "px";
    tip.style.top = "8px";
    draw();
  });
  body.addEventListener("mouseleave", () => { hoverX = null; tip.hidden = true; draw(); if (opts.onHover) opts.onHover(null); });
  // quiet = a view pushed in from another chart (opts.onView is for the chart the user is working)
  const setView = (a, b, quiet) => {
    if (!data || !data.t || data.t.length < 2) return;
    const A = data.t[0], B = data.t[data.t.length - 1], span = Math.min(B - A, Math.max(((B - A) / data.t.length) * 8, b - a));
    if (!(b > a) || span >= (B - A) * 0.999) view = null;
    else { const lo = Math.min(Math.max(a, A), B - span); view = { a: lo, b: lo + span }; }
    resetBtn.hidden = !view || !opts.zoom;
    draw();
    if (!quiet && opts.onView) opts.onView(view ? { ...view } : null);
  };
  if (opts.zoom) {
    body.classList.add("nl-chart-zoomable");
    body.addEventListener("wheel", e => {
      if (!geom || !data) return;
      let dx = e.deltaX, dy = e.deltaY;
      if (e.deltaMode === 1) { dx *= 16; dy *= 16; } else if (e.deltaMode === 2) { dx *= 400; dy *= 400; }
      if (e.shiftKey && Math.abs(dy) > Math.abs(dx)) { dx = dy; dy = 0; }
      if (!view && Math.abs(dy) >= Math.abs(dx) && dy > 0) return;   // everything is in view and the wheel asks for more: let the page scroll
      e.preventDefault();
      const span = geom.x1 - geom.x0;
      if (Math.abs(dx) > Math.abs(dy)) { const d = (dx / geom.pw) * span; return setView(geom.x0 + d, geom.x1 + d); }
      if (!dy) return;
      const r = body.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left - M.l) / geom.pw));
      const xc = geom.x0 + f * span, ns = span * Math.exp(Math.min(240, Math.max(-240, dy)) * (e.ctrlKey ? 0.01 : 0.0018));
      setView(xc - f * ns, xc - f * ns + ns);
    }, { passive: false });
    let drag = null;
    body.addEventListener("pointerdown", e => { if (!geom || !view || e.button !== 0 || e.target === resetBtn) return; drag = { px: e.clientX, a: view.a, b: view.b }; body.setPointerCapture(e.pointerId); body.classList.add("nl-chart-dragging"); });
    body.addEventListener("pointermove", e => { if (!drag || !geom) return; const d = ((drag.px - e.clientX) / geom.pw) * (drag.b - drag.a); setView(drag.a + d, drag.b + d); });
    const end = () => { drag = null; body.classList.remove("nl-chart-dragging"); };
    body.addEventListener("pointerup", end); body.addEventListener("pointercancel", end);
    body.addEventListener("dblclick", () => { if (view) setView(0, 0); });
    resetBtn.addEventListener("click", () => setView(0, 0));
  }
  const ro = new ResizeObserver(draw);
  ro.observe(body);
  return {
    setData(d) {
      // a zoomed view survives new data on the same axis, and is dropped when the axis itself changed
      if (view && !(d && d.t && d.t.length > 1 && view.a >= d.t[0] && view.b <= d.t[d.t.length - 1])) { view = null; resetBtn.hidden = true; if (opts.onView) opts.onView(null); }
      data = d; paintLegend(); draw();
    },
    setView: (a, b) => setView(a, b, true),
    setHover(x) { if (hoverX !== x && tip.hidden) { hoverX = x; draw(); } },      // the cursor line of a sibling chart
    get view() { return view ? { ...view } : null; },
    redraw: draw,
    destroy() { ro.disconnect(); host.innerHTML = ""; },
  };
}

// Small multiples, one thin row per LLAMMA band on one shared time axis and one shared
// $ scale: collateral from the baseline, the lending token stacked above it.
// bandsChart(host, {xMode, tipRows(i)}) .setData({t, bands:[{n, pLo, pHi, coll, lend}]})   (coll / lend in $)
const BAND_COLL = "#9B7BEA", BAND_LEND = "#4DB6AC", BAND_EDGE = "#cfc4f7";
export function bandsChart(host, opts = {}) {
  host.classList.add("nl-chart");
  host.innerHTML = `<div class="nl-chart-legend"><span class="nl-chart-key"><i style="background:${BAND_COLL}"></i>collateral token</span>` +
    `<span class="nl-chart-key"><i style="background:${BAND_LEND}"></i>lending token</span></div><div class="nl-chart-body"><canvas></canvas><div class="nl-chart-tip" hidden></div></div>`;
  const body = host.querySelector(".nl-chart-body"), cv = host.querySelector("canvas"), tip = host.querySelector(".nl-chart-tip");
  const M = { l: 118, r: 12, t: 8, b: 24 };
  let data = null, hoverX = null, geom = null;
  const rowH = nb => Math.max(30, Math.min(84, Math.round(560 / nb)));

  function draw() {
    const ok = !!(data && data.t && data.t.length > 1 && data.bands && data.bands.length), nb = ok ? data.bands.length : 0, rh = ok ? rowH(nb) : 0;
    const want = (ok ? M.t + nb * rh + M.b : 120) + "px";
    if (body.style.height !== want) body.style.height = want;
    const W = body.clientWidth, H = body.clientHeight;
    if (!W || !H) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.width = W + "px"; cv.style.height = H + "px";
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const dim = css(host, "--nl-dim", "#8b949e"), line = css(host, "--nl-line", "#30363d"), fg = css(host, "--nl-fg", "#e6edf3");
    const mono = css(host, "--nl-mono", "ui-monospace, SFMono-Regular, Menlo, monospace");
    g.font = `11px ${mono}`;
    if (!ok) { g.fillStyle = dim; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(opts.empty || "no band data", W / 2, H / 2); geom = null; return; }
    const t = data.t, n = t.length, x0 = t[0], x1 = t[n - 1], pw = W - M.l - M.r;
    const X = x => M.l + ((x - x0) / (x1 - x0 || 1)) * pw;
    geom = { x0, x1, pw };
    let vMax = 10;
    for (const b of data.bands) for (let i = 0; i < n; i++) { const v = (b.coll[i] || 0) + (b.lend[i] || 0); if (v > vMax) vMax = v; }
    const top = vMax * 1.12;                       // headroom: a full band must still read as a chart
    data.bands.forEach((b, r) => {
      const yTop = M.t + r * rh, yBase = yTop + rh - 8, hPx = rh - 14, Y = v => yBase - (v / top) * hPx;
      g.fillStyle = r % 2 ? "rgba(255,255,255,0.018)" : "transparent"; g.fillRect(M.l, yTop + 2, pw, rh - 6);
      g.strokeStyle = line; g.lineWidth = 1; g.globalAlpha = 0.6; g.strokeRect(M.l + 0.5, yTop + 2.5, pw - 1, rh - 7); g.globalAlpha = 1;
      const area = (lower, upper, col) => {
        g.beginPath();
        for (let i = 0; i < n; i++) i ? g.lineTo(X(t[i]), Y(upper(i))) : g.moveTo(X(t[i]), Y(upper(i)));
        for (let i = n - 1; i >= 0; i--) g.lineTo(X(t[i]), Y(lower(i)));
        g.closePath(); g.globalAlpha = 0.9; g.fillStyle = col; g.fill(); g.globalAlpha = 1;
      };
      const c = i => b.coll[i] || 0, st = i => c(i) + (b.lend[i] || 0);
      area(() => 0, c, BAND_COLL);
      area(c, st, BAND_LEND);
      g.strokeStyle = BAND_EDGE; g.lineWidth = 1.2; g.beginPath();
      let peak = 0;
      for (let i = 0; i < n; i++) { const v = st(i); if (v > peak) peak = v; i ? g.lineTo(X(t[i]), Y(v)) : g.moveTo(X(t[i]), Y(v)); }
      g.stroke();
      // the row's label: the band, its price interval, the most it ever held
      const mid = yTop + rh / 2, lx = M.l - 8, tight = rh < 44;
      g.textAlign = "right"; g.textBaseline = "middle";
      g.fillStyle = fg; g.font = `10.5px ${mono}`; g.fillText(`band ${b.n}`, lx, tight ? mid : mid - 11);
      if (!tight) {
        g.fillStyle = dim; g.font = `9px ${mono}`;
        if (Number.isFinite(b.pHi)) g.fillText(`$${fmtNum(b.pLo, 5)}-$${fmtNum(b.pHi, 5)}`, lx, mid + 1);
        g.fillText("peak $" + Math.round(peak).toLocaleString("en-US"), lx, mid + 13);
      }
    });
    g.font = `11px ${mono}`; g.fillStyle = dim; g.textAlign = "center"; g.textBaseline = "top";
    const nx = Math.max(2, Math.floor(pw / 130));
    for (let k = 0; k <= nx; k++) { const xv = x0 + ((x1 - x0) * k) / nx; g.textAlign = k === nx ? "right" : "center"; g.fillText(opts.xMode === "elapsed" ? fmtElapsed(xv) : fmtTime(xv, x1 - x0), X(xv), H - M.b + 6); }
    if (hoverX !== null) {
      const x = Math.round(X(hoverX)) + 0.5;
      g.strokeStyle = fg; g.globalAlpha = 0.35; g.beginPath(); g.moveTo(x, M.t); g.lineTo(x, H - M.b); g.stroke(); g.globalAlpha = 1;
    }
  }
  body.addEventListener("mousemove", e => {
    if (!geom || !data) return;
    const r = body.getBoundingClientRect(), px = e.clientX - r.left;
    const xv = geom.x0 + ((px - M.l) / geom.pw) * (geom.x1 - geom.x0);
    if (xv < geom.x0 || xv > geom.x1) { hoverX = null; tip.hidden = true; draw(); return; }
    let i = Math.min(data.t.length - 1, lowerBound(data.t, xv));
    if (i > 0 && xv - data.t[i - 1] < data.t[i] - xv) i--;
    hoverX = data.t[i];
    const usd = v => "$" + Math.round(v || 0).toLocaleString("en-US");
    const rows = opts.tipRows ? opts.tipRows(i) : data.bands.map(b => [`band ${b.n}`, `${usd(b.coll[i])} coll · ${usd(b.lend[i])} lend`, BAND_COLL]);
    tip.hidden = false;
    tip.innerHTML = `<b>${opts.xMode === "elapsed" ? fmtElapsed(data.t[i]) : fmtDateTime(data.t[i])}</b>` +
      rows.map(([lab, val, col]) => `<div><i style="background:${col ? cssRef(col) : "transparent"}"></i>${lab}<span>${val}</span></div>`).join("");
    tip.style.left = Math.min(r.width - tip.offsetWidth - 8, Math.max(M.l, px + 12)) + "px";
    tip.style.top = "8px";
    draw();
  });
  body.addEventListener("mouseleave", () => { hoverX = null; tip.hidden = true; draw(); });
  const ro = new ResizeObserver(draw);
  ro.observe(body);
  draw();
  return { setData(d) { data = d; draw(); }, redraw: draw, destroy() { ro.disconnect(); host.innerHTML = ""; } };
}

// heat table for the S.L./D.L. grid: rows = A, columns = fee
// mark = the outlined cell, bold = the cell printed bold, rowMark = the accented row, tip(r, c) = a cell's tooltip
export function heatTable(host, { title, rowLabels, colLabels, values, fmt, mark, bold, rowMark, tip, corner = "A \\ fee %" }) {
  let lo = Infinity, hi = -Infinity;
  for (const r of values) for (const v of r) if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const tone = v => {
    if (!Number.isFinite(v)) return "transparent";
    const f = hi > lo ? (v - lo) / (hi - lo) : 0;
    return `color-mix(in oklab, var(--nl-good) ${Math.round((1 - f) * 100)}%, var(--nl-bad))`;
  };
  host.innerHTML = `<div class="nl-heat">${title ? `<div class="nl-heat-title">${title}</div>` : ""}
    <div class="nl-heat-scroll"><table><thead><tr><th>${corner}</th>${colLabels.map(c => `<th>${c}</th>`).join("")}</tr></thead>
    <tbody>${values.map((row, r) => `<tr class="${rowMark === r ? "nl-heat-row" : ""}"><th>${rowLabels[r]}</th>${row.map((v, c) =>
      `<td class="${mark && mark.r === r && mark.c === c ? "nl-heat-mark" : ""}${bold && bold.r === r && bold.c === c ? " nl-heat-bold" : ""}" style="--tone:${tone(v)}" title="${tip ? tip(r, c) : `A ${rowLabels[r]} · fee ${colLabels[c]}`}">${(fmt || fmtNum)(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
}
