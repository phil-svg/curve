// Smoothing along the oracle chain: which EMA sits inside every part the
// oracle reads, what the script adds on top, and a knob for each. Whatever is
// set here is what BOTH simulators run on: the bad-debt sim replays the
// resulting oracle over the scenario clips, the S.L./D.L. sweep runs on the
// resulting history.
//   parts: chain (everything) | rows (one rich row per part) | grid (one dense table)
//          list (the knobs, one per line) | knobs (the knobs, compact grid) | chart
import { api } from "../core/api.js";
import { compile, sourcesUsed, smoothingOf, setNumberAt } from "../core/expr.js";
import { evaluateOracle, emaOf, worstWindow, smoothingSummary, detectSmoothing, SMOOTHED_READING as SMOOTHED } from "../core/pipeline.js";
import { lowerBound } from "../core/series.js";
import { lineChart, fmtNum, fmtDateTime } from "../core/charts.js";
import { h, field, seg, select, button, card, toast, shortAddr, parseNum } from "./kit.js";

const halfLife = T => (T > 0 ? `${fmtDur(T * Math.LN2)} half-life` : "no smoothing");
function fmtDur(s) { return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${(s / 60).toFixed(1)} min` : `${(s / 3600).toFixed(1)} h`; }

export function mount(host, ctx, opts = {}) {
  const { store } = ctx, part = ["knobs", "rows", "grid", "list", "chart"].includes(opts.part) ? opts.part : "chain";
  const root = h("div", { class: "nl-ema nl-ema-" + part });
  host.append(root);
  let chart = null, view = { part: "oracle", win: "1d" }, detecting = false, rows = new Map(), alive = true;

  const usedSources = () => {
    const used = sourcesUsed(compile(store.spec.oracle.script));
    return store.spec.oracle.sources.filter(s => s.name && used.has(s.name) && s.kind !== "const");
  };
  const setEma = (id, patch) => {
    store.update("oracle.sources", sp => { const s = sp.oracle.sources.find(x => x.id === id); if (s) s.ema = { onchain: null, use: null, ...(s.ema || {}), ...patch }; });
    evaluateOracle(store);
  };
  const numOrNull = v => { const x = parseNum(v); return v === "" || v === null || !Number.isFinite(x) || x < 0 ? null : x; };

  // detection lives in the pipeline (the flowchart needs it too)
  async function detect(force) {
    if (detecting) return;
    detecting = true; paintRows();
    try { await detectSmoothing(store, { force }); } catch (e) { toast("EMA detection failed: " + e.message, "error"); }
    detecting = false;
    if (alive) build();
  }

  // ---- the effect of one re-timed part, from the evaluated grid -----------------
  function effectOf(name) {
    const p = store.rt.parts && store.rt.parts[name], g = store.rt.grid;
    if (!p || !g || p.used === p.sampled) return null;
    let worst = 0, at = -1;
    for (let i = store.rt.valid0 || 0; i < p.used.length; i++) {
      const d = Math.abs(p.used[i] / p.sampled[i] - 1);
      if (d > worst) { worst = d; at = i; }
    }
    return at < 0 ? null : { pct: worst * 100, t: g.t[at] };
  }

  function treeView(s) {
    const e = s.ema || {};
    if (s.kind !== "onchain") return h("div", { class: "nl-note" }, s.kind === "dataset" ? "a recorded dataset: no contract behind it" : "no contract behind this part");
    if (!e.tree) return h("div", { class: "nl-note" }, detecting ? "reading the contracts behind this part…" : "not inspected yet");
    const withMa = e.tree.filter(n => Object.keys(n.ma || {}).length).length;
    const list = h("ul", { class: "nl-ema-tree" }, e.tree.map(n => {
      const ma = Object.entries(n.ma || {});
      return h("li", { style: { paddingLeft: (n.depth * 14) + "px" }, class: ma.length ? "nl-ema-has" : "" },
        h("span", { class: "nl-ema-type" }, n.type), h("span", { class: "nl-ema-lab" }, n.label || shortAddr(n.address)),
        h("span", { class: "nl-ema-addr nl-mono" }, shortAddr(n.address)),
        ma.length ? h("b", { class: "nl-ema-ma nl-mono" }, ma.map(([k, v]) => `${k} ${v} s`).join(" · ")) : h("span", { class: "nl-note" }, "no EMA of its own"));
    }));
    const det = h("details", { class: "nl-ema-chain" }, h("summary", {},
      `${e.tree.length} contract${e.tree.length > 1 ? "s" : ""} behind it · ${withMa} with an EMA` + ((e.times || []).length ? `: ${e.times.join(", ")} s` : "") + (e.mixed ? " (mixed)" : "")), list);
    det.open = e.tree.length <= 3;
    return det;
  }

  function sourceRow(s) {
    const e = emaOf(s), smoothed = s.kind === "onchain" && SMOOTHED.test(s.sig || "");
    const what = s.kind === "onchain" ? `${s.sig || "?"} @ ${shortAddr(s.address)}` : s.kind === "dataset" ? `dataset ${s.key} · ${s.column || "close"}` : s.kind;
    const eff = h("div", { class: "nl-ema-eff nl-note" });
    const t0 = field({ label: "EMA inside the reading", unit: "s", value: e.onchain || "", placeholder: smoothed ? "unknown" : "none",
      hint: "The EMA time the contract already applied to this value (a pool's ma_exp_time). It is what gets undone before your own time is applied. Leave blank for a raw reading.",
      onChange: v => setEma(s.id, { onchain: numOrNull(v) }) });
    const t1 = field({ label: "simulate with", unit: "s", value: Number.isFinite(e.use) ? e.use : "", placeholder: e.onchain ? `as sampled (${e.onchain})` : "as sampled",
      hint: "EMA time to simulate this part with. Blank = exactly as sampled. 0 = strip the EMA and use the implied raw input. Applied to the cached samples: nothing is refetched.",
      onChange: v => setEma(s.id, { use: numOrNull(v) }) });
    const quick = h("div", { class: "nl-row nl-tight" }, [["as sampled", null], ["none", 0], ["×2", "x2"], ["×4", "x4"]].map(([lab, v]) =>
      button(lab, { small: true, kind: "ghost", onClick: () => {
        const base = emaOf(store.spec.oracle.sources.find(x => x.id === s.id)).onchain;
        setEma(s.id, { use: v === "x2" ? (base || 866) * 2 : v === "x4" ? (base || 866) * 4 : v });
      } })));
    const el = h("div", { class: "nl-ema-row" + (e.active ? " nl-ema-active" : "") },
      h("div", { class: "nl-ema-id" }, h("b", { class: "nl-mono" }, s.name), h("span", { class: "nl-note nl-mono" }, what),
        smoothed ? h("span", { class: "nl-badge" }, "EMA-smoothed reading") : s.kind === "onchain" ? h("span", { class: "nl-badge" }, "raw reading") : null),
      treeView(s), h("div", { class: "nl-ema-fields" }, t0, t1, quick), eff);
    rows.set(s.id, { el, t0, t1, eff });
    return el;
  }

  function paintRows() {
    for (const s of usedSources()) {
      const r = rows.get(s.id);
      if (!r) continue;
      const e = emaOf(s), x = effectOf(s.name);
      if (r.el.classList) r.el.classList.toggle("nl-ema-active", e.active);
      r.t0.set(e.onchain || ""); r.t1.set(Number.isFinite(e.use) ? e.use : "");
      r.eff.textContent = e.active
        ? `${e.onchain ? halfLife(e.onchain) : "raw"} → ${halfLife(e.use)}` + (x ? ` · moves this part by up to ${x.pct.toFixed(3)} % (${fmtDateTime(x.t)} UTC)` : " · load the history to see the effect")
        : e.onchain ? `${halfLife(e.onchain)}, left as sampled` : "no EMA inside, none added";
    }
  }

  // ---- knobs the script itself carries ------------------------------------------
  function scriptKnobs() {
    const sm = smoothingOf(store.spec.oracle.script);
    if (!sm.calls.length) return [h("div", { class: "nl-note" }, "The script applies no ema(), ema_hl(), asym_ema() or lag() of its own.")];
    const seen = new Set();
    return sm.calls.filter(c => { const k = c.start + ":" + c.end; if (seen.has(k)) return false; seen.add(k); return true; }).map(c => {
      if (!Number.isFinite(c.value)) return h("div", { class: "nl-ema-srow" }, h("span", { class: "nl-mono" }, `line ${c.line}: ${c.fn}(${c.target}, ${c.arg})`),
        h("span", { class: "nl-note" }, "the time is an expression: edit it in the script"));
      const f = scriptField(c, `${c.fn}(${c.target})` + (c.via ? ` · ${c.via}` : ""));
      return h("div", { class: "nl-ema-srow" }, f, h("span", { class: "nl-note" }, `line ${c.line} · ${halfLifeOf(c)}`));
    });
  }
  const halfLifeOf = c => c.fn === "lag" ? `${fmtDur(c.value)} delay` : c.fn === "ema_hl" ? `${fmtDur(c.value)} half-life` : halfLife(c.value);

  function engineKnob() {
    return field({ label: "engine EMA (ma_exp_time)", unit: "s", type: "number", min: 1, value: store.spec.params.ma_exp_time,
      hint: "Only used when the bad-debt sim has no recorded oracle to replay: drawn / linear scenarios, or oracle mode = ema. Then the engine smooths the scenario price with this one EMA.",
      onChange: v => store.update("params", sp => { sp.params.ma_exp_time = v; }) });
  }

  // ---- the chart ----------------------------------------------------------------
  function paintChart() {
    if (!chart) return;
    const rt = store.rt, g = rt.grid;
    if (!g || !rt.oracle) { chart.setData(null); return; }
    let a = rt.valid0 || 0, b = g.t.length;
    if (view.win !== "all") {
      const w = worstWindow(rt, view.win === "7d" ? 7 * 86400 : 86400);
      if (w) { a = Math.max(a, lowerBound(g.t, w.t0)); b = Math.min(b, lowerBound(g.t, w.t1) + 1); }
    }
    const cut = arr => arr.subarray(a, b), series = [];
    if (view.part === "oracle") {
      series.push({ name: "market", v: cut(rt.market), color: "--nl-c2", width: 1.2 });
      if (rt.oracleBase) series.push({ name: "oracle, parts as sampled", v: cut(rt.oracleBase), color: "--nl-c6", dash: [5, 4] });
      series.push({ name: rt.oracleBase ? "oracle, as configured" : "oracle", v: cut(rt.oracle), color: "--nl-c1", width: 2 });
    } else {
      const p = rt.parts && rt.parts[view.part];
      if (!p) { chart.setData(null); return; }
      if (p.implied) series.push({ name: "implied raw input", v: cut(p.implied), color: "--nl-c2", width: 1.1 });
      series.push({ name: "as sampled on-chain", v: cut(p.sampled), color: "--nl-c6", dash: p.used !== p.sampled ? [5, 4] : [] });
      if (p.used !== p.sampled) series.push({ name: "as simulated", v: cut(p.used), color: "--nl-c1", width: 2 });
    }
    chart.setData({ t: g.t.subarray(a, b), series });
  }

  function gridWarning() {
    const g = store.rt.grid, step = (g && g.step) || store.spec.oracle.range.step_s;
    const times = [...usedSources().map(s => emaOf(s)).flatMap(e => [e.onchain, e.active ? e.use : 0]), ...smoothingOf(store.spec.oracle.script).calls.map(c => c.value)].filter(x => x > 0);
    const smallest = Math.min(...times);
    if (!times.length || step <= smallest) return null;
    return h("div", { class: "nl-ema-warn" }, `The history grid is ${fmtDur(step)} but the shortest EMA here is ${fmtDur(smallest)}: at that step an EMA has almost fully caught up between two samples, so its lag is invisible. Use a 5 min grid (Oracle range) to study EMA timing.`);
  }

  function knobField(s, label) {
    const e = emaOf(s);
    const f = field({ label, unit: "s", value: Number.isFinite(e.use) ? e.use : "", placeholder: e.onchain ? `as sampled (${e.onchain})` : "as sampled", mono: true,
      hint: "EMA time to simulate this oracle part with. Blank = as sampled, 0 = no EMA. Applied to the cached samples: nothing is refetched.", onChange: v => setEma(s.id, { use: numOrNull(v) }) });
    return f;
  }
  function gridTable(srcs) {
    const tr = (cells, cls = "") => h("tr", { class: cls }, cells.map(c => h("td", {}, c)));
    const body = srcs.map(s => {
      const e = emaOf(s), tree = (s.ema && s.ema.tree) || [], withMa = tree.filter(n => Object.keys(n.ma || {}).length);
      const t0 = field({ label: "", unit: "s", value: e.onchain || "", placeholder: "none", mono: true, onChange: v => setEma(s.id, { onchain: numOrNull(v) }) });
      const t1 = knobField(s, ""), eff = h("span", { class: "nl-note" });
      rows.set(s.id, { el: h("span"), t0, t1, eff });
      return tr([h("b", { class: "nl-mono" }, s.name), h("span", { class: "nl-note nl-mono" }, s.kind === "onchain" ? `${s.sig} @ ${shortAddr(s.address)}` : s.kind),
        h("span", { class: "nl-note" }, s.kind !== "onchain" ? "" : !tree.length ? "…" : `${tree.length} contract${tree.length > 1 ? "s" : ""}, ${withMa.length} with an EMA` + ((s.ema.times || []).length ? ` (${s.ema.times.join(", ")} s)` : "")),
        t0, t1, eff], e.active ? "nl-ema-active" : "");
    });
    const sk = smoothingOf(store.spec.oracle.script).calls.filter(c => Number.isFinite(c.value)).map(c => {
      const f = scriptField(c, "");
      return tr([h("b", { class: "nl-mono" }, `${c.fn}(${c.target})`), h("span", { class: "nl-note" }, `script line ${c.line}${c.via ? ` · ${c.via}` : ""}`), h("span", { class: "nl-note" }, c.what), h("span", { class: "nl-note" }, "set by the script"), f, h("span", { class: "nl-note" }, halfLifeOf(c))]);
    });
    return h("div", { class: "nl-ema-gridwrap" }, h("table", { class: "nl-ema-grid" }, h("thead", {}, h("tr", {}, ["part", "reads", "behind it", "EMA inside", "simulate with", "effect"].map(x => h("th", {}, x)))), h("tbody", {}, body, sk)));
  }
  function scriptField(c, label) {
    return field({ label, unit: "s", type: "number", min: 0, value: c.value, mono: true, hint: `${c.what} on script line ${c.line}. Changing it rewrites that number in the script.`,
      onChange: v => {
        const cur = smoothingOf(store.spec.oracle.script).calls.find(k => k.line === c.line && k.fn === c.fn && k.target === c.target);
        if (!cur || !Number.isFinite(cur.value)) return;
        store.update("oracle.script", sp => { sp.oracle.script = setNumberAt(sp.oracle.script, cur.start, cur.end, v); });
        evaluateOracle(store);
      } });
  }

  function build() {
    rows = new Map();
    if (chart) { chart.destroy(); chart = null; }
    const srcs = usedSources();
    if (part === "chart") {
      const partSel = select([{ value: "oracle", label: "the oracle (final)" }, ...srcs.map(s => ({ value: s.name, label: `part: ${s.name}` }))], view.part, v => { view.part = v; paintChart(); });
      const winSeg = seg([{ value: "1d", label: "worst day" }, { value: "7d", label: "worst week" }, { value: "all", label: "all" }], view.win, v => { view.win = v; paintChart(); });
      const chartHost = h("div", {});
      root.replaceChildren(card([h("span", {}, "What the smoothing does"), h("span", { class: "nl-row nl-tight" }, partSel, winSeg)], chartHost));
      chart = lineChart(chartHost, { height: opts.height || 250, empty: "load the price history to see the smoothing at work" });
      paintChart();
      return;
    }
    if (part === "grid" || part === "list") {
      const warn = gridWarning();
      const body = part === "grid" ? gridTable(srcs)
        : h("div", { class: "nl-ema-list" }, srcs.filter(s => s.kind === "onchain" || emaOf(s).active).map(s => {
            const e = emaOf(s), f = knobField(s, `${s.name} · ${e.onchain ? `sampled with ${e.onchain} s` : "raw reading"}`), eff = h("span", { class: "nl-note" });
            rows.set(s.id, { el: f, t0: { set() {} }, t1: f, eff });
            return h("div", { class: "nl-ema-srow" }, f, eff);
          }), smoothingOf(store.spec.oracle.script).calls.filter(c => Number.isFinite(c.value)).map(c =>
            h("div", { class: "nl-ema-srow" }, scriptField(c, `${c.fn}(${c.target})${c.via ? " · " + c.via : ""} · script line ${c.line}`), h("span", { class: "nl-note" }, halfLifeOf(c)))));
      root.replaceChildren(card([h("span", {}, "MA times along the chain"), button(detecting ? "reading contracts…" : "Re-read the contracts", { small: true, kind: "ghost", disabled: detecting, onClick: () => detect(true) })],
        h("div", { class: "nl-note" }, "\"EMA inside\" is what the contract already applied to the value it reports. \"Simulate with\" replaces it (blank = unchanged, 0 = none) on the loaded samples, for both simulations."),
        warn, body));
      paintRows();
      return;
    }
    if (part === "knobs") {
      const fields = srcs.filter(s => emaOf(s).onchain || emaOf(s).active || s.kind === "onchain").map(s => {
        const e = emaOf(s);
        const f = field({ label: `${s.name}${e.onchain ? ` (sampled ${e.onchain} s)` : " (raw)"}`, unit: "s", value: Number.isFinite(e.use) ? e.use : "", placeholder: "as sampled", mono: true,
          hint: "EMA time to simulate this oracle part with. Blank = as sampled, 0 = no EMA.", onChange: v => setEma(s.id, { use: numOrNull(v) }) });
        rows.set(s.id, { el: f, t0: { set() {} }, t1: f, eff: h("span") });
        return f;
      });
      const sk = scriptKnobs().map(x => x.querySelector ? (x.querySelector(".nl-field") || x) : x);
      root.replaceChildren(card(h("span", {}, "MA times"),
        h("div", { class: "nl-grid nl-wide-cols" }, fields, sk, engineKnob()),
        h("div", { class: "nl-note" }, "Blank = as sampled on-chain, 0 = no MA. Both simulations use these; nothing is refetched.")));
      return;
    }
    const partSel = select([{ value: "oracle", label: "the oracle (final)" }, ...srcs.map(s => ({ value: s.name, label: `part: ${s.name}` }))], view.part, v => { view.part = v; paintChart(); });
    const winSeg = seg([{ value: "1d", label: "worst day" }, { value: "7d", label: "worst week" }, { value: "all", label: "all" }], view.win, v => { view.win = v; paintChart(); });
    const chartHost = h("div", {});
    const warn = gridWarning();
    root.replaceChildren(card(h("span", {}, "Smoothing along the oracle chain"),
      h("div", { class: "nl-note" }, "Every part the oracle reads may already carry an EMA from its own contract, and the script can add more. Here each one is visible and adjustable: \"simulate with\" undoes the sampled EMA on the grid and applies yours instead (exactly reversible, no refetch). Both simulations run on the result."),
      warn,
      h("div", { class: "nl-spread" }, h("div", { class: "nl-sub" }, "Parts the oracle reads"),
        button(detecting ? "reading contracts…" : "Re-read the contracts", { small: true, kind: "ghost", disabled: detecting, onClick: () => detect(true) })),
      srcs.length ? h("div", { class: "nl-ema-rows" }, srcs.map(sourceRow)) : h("div", { class: "nl-note" }, "The script reads no source yet."),
      h("div", { class: "nl-sub" }, "EMAs the script applies"), h("div", { class: "nl-ema-srows" }, scriptKnobs()),
      h("div", { class: "nl-sub" }, "When no recorded oracle is replayed"), h("div", { class: "nl-ema-srows" }, h("div", { class: "nl-ema-srow" }, engineKnob(),
        h("span", { class: "nl-note" }, "drawn and linear scenarios, or oracle mode = ema"))),
      part === "rows" ? null : h("div", { class: "nl-spread" }, h("div", { class: "nl-sub" }, "What the smoothing does"), h("div", { class: "nl-row nl-tight" }, partSel, winSeg)),
      part === "rows" ? null : chartHost));
    if (part !== "rows") chart = lineChart(chartHost, { height: opts.height || 250, empty: "load the price history to see the smoothing at work" });
    paintRows(); paintChart();
  }
  function paintSummary() { if (root._sum) root._sum.textContent = "In effect: " + smoothingSummary(store.spec).text; }

  build();
  detect(false);
  let lastSig = JSON.stringify(usedSources().map(s => [s.id, s.kind, s.sig, s.address]));
  const off = store.on(tag => {
    if (tag === "spec") { view.part = "oracle"; build(); detect(false); return; }
    if (tag === "oracle.sources" || tag === "oracle.script") {
      const sig = JSON.stringify(usedSources().map(s => [s.id, s.kind, s.sig, s.address]));
      const scriptEdit = tag === "oracle.script" && !root.contains(document.activeElement);
      if (sig !== lastSig || scriptEdit) { lastSig = sig; build(); detect(false); } else { paintRows(); paintSummary(); }
    }
    if (tag === "oracle.data") { paintRows(); paintChart(); paintSummary(); }
    if (tag === "params") paintSummary();
  });
  return { destroy() { alive = false; off(); if (chart) chart.destroy(); root.remove(); } };
}
