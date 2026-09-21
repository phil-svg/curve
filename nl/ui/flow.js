// The oracle as a flowchart, left to right, the oracle right-most: the contracts
// behind each part (with the EMA time each one runs), the parts the script
// reads, every script line, the oracle. Every MA time sits ON its box.
//
// Wires never run over or under a box. They leave a box at its back (right
// edge), travel only in the gaps between columns, and enter at the front (left
// edge). A wire that skips columns gets its own thin lane through each column
// it crosses, so it passes BETWEEN boxes.
//
// Click a box to wrap away everything that flows into it (again to unwrap): a
// box stays as long as it still reaches the oracle or the market through a box
// that is not wrapped.
import { compile, graphOf, smoothingOf, setNumberAt } from "../core/expr.js";
import { evaluateOracle, emaOf, detectSmoothing, SMOOTHED_READING } from "../core/pipeline.js";
import { fmtNum } from "../core/charts.js";
import { h, card, shortAddr, parseNum, button } from "./kit.js";

const LS = "nl.flow.wrapped";

export function mount(host, ctx, opts = {}) {
  const { store } = ctx;
  let alive = true, sig = "", nodes = new Map(), chains = [], colEls = [];
  let wrapped = new Set();                                  // ids of the boxes whose inputs are wrapped away
  try { wrapped = new Set(JSON.parse(localStorage.getItem(LS) || "[]")); } catch (_) { /* private mode */ }
  const saveWrapped = () => { try { localStorage.setItem(LS, JSON.stringify([...wrapped])); } catch (_) { /* private mode */ } };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "nl-flow-wires");
  const cols = h("div", { class: "nl-flow-cols" }), board = h("div", { class: "nl-flow-board" }, svg, cols);
  const unwrapBtn = button("unwrap all", { small: true, kind: "ghost", onClick: () => { wrapped.clear(); saveWrapped(); applyWrap(); } });
  const tools = h("span", { class: "nl-row nl-tight" }, unwrapBtn);
  const legend = h("div", { class: "nl-flow-legend nl-note" }, ["contract", "part", "line", "oracle", "market"].map(k => h("span", { class: "nl-flow-key nl-flow-lg-" + k }, k === "part" ? "part the script reads" : k === "line" ? "script line" : k === "contract" ? "contract behind a part" : k)));
  const root = h("div", { class: "nl-flow" });
  root.append(opts.card === false ? h("div", { class: "nl-stack" }, h("div", { class: "nl-spread" }, legend, tools), board)
    : card([h("span", {}, "Oracle flowchart"), tools], legend, board));
  host.append(root);

  const setEma = (id, patch) => {
    store.update("oracle.sources", sp => { const s = sp.oracle.sources.find(x => x.id === id); if (s) s.ema = { onchain: null, use: null, ...(s.ema || {}), ...patch }; });
    evaluateOracle(store);
  };
  const numOrNull = v => { const x = parseNum(v); return v === "" || !Number.isFinite(x) || x < 0 ? null : x; };
  const numInput = (value, placeholder, title, onCommit) => {
    const el = h("input", { class: "nl-input nl-mono nl-flow-in", type: "text", inputmode: "decimal", value: value ?? "", placeholder, title, spellcheck: "false" });
    el.addEventListener("change", () => onCommit(el.value.trim()));
    el.addEventListener("keydown", e => { if (e.key === "Enter") el.blur(); });
    return el;
  };

  // a box; `col` is its column in script order (contracts negative)
  function node(id, kind, col, title, sub, extra) {
    const val = h("div", { class: "nl-flow-val nl-mono" }), fold = h("span", { class: "nl-flow-fold nl-mono", hidden: true });
    const el = h("div", { class: "nl-flow-node nl-flow-k-" + kind, dataset: { id } }, h("div", { class: "nl-flow-head" }, h("b", { class: "nl-mono" }, title), fold),
      sub ? h("div", { class: "nl-flow-sub nl-mono", title: sub }, sub) : null, val, extra || null);
    el.addEventListener("click", e => {                     // inputs and buttons on a box keep their own clicks
      if (e.target.closest("input, button, a, select") || !nodes.get(id) || !nodes.get(id).hasInputs) return;
      if (wrapped.has(id)) wrapped.delete(id); else wrapped.add(id);
      saveWrapped(); applyWrap();
    });
    nodes.set(id, { id, el, val, kind, fold, col, order: nodes.size });
    return el;
  }

  function build() {
    const spec = store.spec, g = graphOf(compile(spec.oracle.script), spec.oracle.sources.map(s => s.name)), sm = smoothingOf(spec.oracle.script);
    nodes = new Map();
    const edges = [];
    for (const n of g.nodes.filter(x => x.kind === "source" || x.kind === "missing")) {
      const s = spec.oracle.sources.find(x => x.name === n.id), id = "p:" + n.id;
      if (!s) { node(id, "missing", 0, n.id, "not a source yet", null); continue; }
      const e = emaOf(s), smoothed = s.kind === "onchain" && SMOOTHED_READING.test(s.sig || "");
      const what = s.kind === "onchain" ? `${s.sig || "?"} @ ${shortAddr(s.address)}` : s.kind === "dataset" ? `dataset ${s.key}` : s.kind === "const" ? `constant ${s.value}` : s.kind;
      const chip = h("div", { class: "nl-flow-ma" }, h("span", { class: "nl-flow-mal" }, e.onchain ? `MA ${e.onchain} s` : smoothed ? "MA unknown" : "no MA inside"), h("span", { class: "nl-flow-arrow" }, "→"),
        numInput(Number.isFinite(e.use) ? e.use : "", e.onchain ? "same" : "none", "MA time to simulate this part with, in seconds. Blank = as sampled, 0 = strip the MA.", v => setEma(s.id, { use: numOrNull(v) })), h("span", { class: "nl-flow-u" }, "s"));
      node(id, "part", 0, n.id, what, chip);
      nodes.get(id).chip = chip; nodes.get(id).src = s.id;
      // the contracts behind it, deeper = further left
      const rootAddr = (s.address || "").toLowerCase(), all = (s.ema && s.ema.tree) || [];
      const parentOf = t => all.find(q => (q.refs || []).includes(t.address) && q.depth === t.depth - 1);
      // A contract that is itself another part of the script (the aggregator inside a
      // feed) is drawn once, as a pointer to that part: its own chain hangs off the part.
      const otherPart = t => spec.oracle.sources.find(o => o.id !== s.id && o.name && (o.address || "").toLowerCase() === t.address && g.nodes.some(k => k.id === o.name));
      const hidden = t => { for (let q = parentOf(t); q && q.depth > 0; q = parentOf(q)) if (otherPart(q)) return true; return false; };
      // adapters only forward calls to a pool: skip them and hang the pool on their parent
      const shownParent = t => { let q = parentOf(t); while (q && q.depth > 0 && q.type === "adapter") q = parentOf(q); return q; };
      const depthOf = t => { let d = 0; for (let q = t; q && q.depth > 0; q = shownParent(q)) d++; return d; };
      for (const t of all.filter(x => x.depth > 0 && ["pool", "agg", "wrapper", "vault", "chainlink", "chainlink-agg"].includes(x.type) && !hidden(x))) {
        const cid = `c:${n.id}:${t.address}`, ma = Object.entries(t.ma || {}), ref = otherPart(t);
        node(cid, "contract", -depthOf(t), t.label || (t.type === "agg" ? "crvUSD aggregator" : shortAddr(t.address)), `${t.type} · ${shortAddr(t.address)}`,
          h("div", { class: "nl-flow-ma nl-flow-ro" }, (ma.length ? ma.map(([k, v]) => `${k} ${v} s`).join(" · ") : "no MA of its own") + (ref ? ` · also part "${ref.name}"` : "")));
        const parent = shownParent(t);
        edges.push([cid, !parent || parent.address === rootAddr ? id : `c:${n.id}:${parent.address}`]);
      }
      const self = all.find(q => q.depth === 0 && Object.keys(q.ma || {}).length);
      if (self && !smoothed) chip.title = `the contract runs ${Object.entries(self.ma).map(([k, v]) => `${k} ${v} s`).join(", ")}, but this getter is not the smoothed one`;
    }
    for (const n of g.nodes.filter(x => x.kind !== "source" && x.kind !== "missing")) {
      const id = "v:" + n.id, calls = sm.calls.filter(c => c.lhs === n.id && Number.isFinite(c.value)), konst = sm.consts.find(c => c.name === n.id);
      const chips = calls.filter(c => !c.via).map(c => h("div", { class: "nl-flow-ma" }, h("span", { class: "nl-flow-mal" }, `${c.fn}(${c.target})`),
        numInput(c.value, "", `${c.what}, script line ${c.line}`, v => rewrite(c, v)), h("span", { class: "nl-flow-u" }, "s")));
      if (konst && sm.calls.some(c => c.via === konst.name)) chips.push(h("div", { class: "nl-flow-ma" }, h("span", { class: "nl-flow-mal" }, "MA time"),
        numInput(konst.value, "", `used by ${sm.calls.filter(c => c.via === konst.name).map(c => `${c.fn}(${c.target})`).join(", ")}`, v => rewriteAt(konst, v)), h("span", { class: "nl-flow-u" }, "s")));
      const expr = (n.text || "").replace(/^[^=]+=\s*/, "").replace(/\s*#.*$/, "");
      // a line that reads nothing (a constant) is an input: it stands with the parts (depth 0)
      node(id, n.kind === "oracle" ? "oracle" : n.kind === "market" ? "market" : "line", n.depth, n.id, konst ? "a constant the lines read" : expr,
        chips.length ? h("div", { class: "nl-stack nl-flow-chips" }, chips) : null);
      if (konst) nodes.get(id).konst = true;
    }
    for (const e of g.edges) edges.push([(nodes.has("p:" + e.from) ? "p:" : "v:") + e.from, "v:" + e.to]);
    layout(edges);
    sig = structure();
    paintValues();
    applyWrap();
    setTimeout(() => { if (alive) board.scrollLeft = board.scrollWidth; }, 60);    // the output is what is in view first
  }

  // columns, the lanes of the wires that skip columns, and an order inside every column that keeps wires short
  function layout(edges) {
    const ranks = [...new Set([...nodes.values()].map(n => n.col))].sort((a, b) => a - b), rankOf = new Map(ranks.map((c, i) => [c, i]));
    const items = ranks.map(() => []);                      // per column: boxes and lanes
    for (const n of nodes.values()) { n.rank = rankOf.get(n.col); items[n.rank].push(n); }
    chains = [];
    edges.forEach(([a, z], k) => {
      const A = nodes.get(a), Z = nodes.get(z);
      if (!A || !Z || Z.rank <= A.rank) return;
      const chain = [A];
      for (let r = A.rank + 1; r < Z.rank; r++) { const lane = { id: `l:${k}:${r}`, lane: true, el: h("div", { class: "nl-flow-lane" }), rank: r, order: 1e6 + k }; items[r].push(lane); chain.push(lane); }
      chain.push(Z);
      chains.push({ chain, contract: A.kind === "contract" });
    });
    const prev = new Map(), next = new Map();               // item -> the items it is wired to in the neighbouring columns
    for (const { chain } of chains) for (let i = 1; i < chain.length; i++) {
      if (!prev.has(chain[i])) prev.set(chain[i], []);
      prev.get(chain[i]).push(chain[i - 1]);
      if (!next.has(chain[i - 1])) next.set(chain[i - 1], []);
      next.get(chain[i - 1]).push(chain[i]);
    }
    items.forEach(col => col.sort((p, q) => p.order - q.order));
    const place = col => col.forEach((it, i) => { it.pos = col.length > 1 ? i / (col.length - 1) : 0.5; });
    items.forEach(place);
    const sweep = (r, nb) => {                              // every item goes to the mean height of what it is wired to
      items[r].forEach(it => { const ns = nb.get(it) || []; it.key = ns.length ? ns.reduce((a, x) => a + x.pos, 0) / ns.length : it.pos; });
      items[r].sort((p, q) => p.key - q.key || p.order - q.order);
      place(items[r]);
    };
    for (let pass = 0; pass < 3; pass++) {
      for (let r = 1; r < items.length; r++) sweep(r, prev);
      for (let r = items.length - 2; r >= 0; r--) sweep(r, next);
    }
    colEls = items.map(col => h("div", { class: "nl-flow-col" }, col.map(it => it.el)));
    cols.replaceChildren(...colEls);
  }

  function rewrite(c, v) {
    const cur = smoothingOf(store.spec.oracle.script).calls.find(k => k.line === c.line && k.fn === c.fn && k.target === c.target);
    if (cur && Number.isFinite(parseNum(v))) rewriteAt(cur, v);
  }
  function rewriteAt(span, v) {
    const x = parseNum(v);
    if (!Number.isFinite(x) || x < 0) return;
    store.update("oracle.script", sp => { sp.oracle.script = setNumberAt(sp.oracle.script, span.start, span.end, x); });
    evaluateOracle(store);
  }
  const structure = () => JSON.stringify([store.spec.oracle.script, store.spec.oracle.sources.map(s => [s.id, s.name, s.kind, s.sig, s.address, s.ema && s.ema.onchain, s.ema && s.ema.tree ? s.ema.tree.length : 0])]);

  // what stays on screen: walk back from the ends of the chart (oracle, market), and do not
  // walk through a wrapped box. A box shared with another, unwrapped route stays visible.
  function applyWrap() {
    const inputs = new Map(), hasOut = new Set();
    for (const { chain } of chains) { const a = chain[0].id, z = chain[chain.length - 1].id; if (!inputs.has(z)) inputs.set(z, []); inputs.get(z).push(a); hasOut.add(a); }
    const seen = new Set(), stack = [...nodes.keys()].filter(id => !hasOut.has(id));
    while (stack.length) { const id = stack.pop(); if (seen.has(id)) continue; seen.add(id); if (!wrapped.has(id)) stack.push(...(inputs.get(id) || [])); }
    const behind = id => { const out = new Set(), st = [...(inputs.get(id) || [])]; while (st.length) { const x = st.pop(); if (!out.has(x)) { out.add(x); st.push(...(inputs.get(x) || [])); } } return out; };
    for (const [id, n] of nodes) {
      n.hasInputs = (inputs.get(id) || []).length > 0;
      n.el.hidden = !seen.has(id);
      const on = wrapped.has(id) && n.hasInputs, hiddenN = on ? [...behind(id)].filter(x => !seen.has(x)).length : 0;
      n.el.classList.toggle("nl-flow-can", n.hasInputs);
      n.el.classList.toggle("nl-flow-wrapped", on);
      n.el.title = !n.hasInputs ? "" : on ? "click to unwrap what flows into this box" : "click to wrap away everything that flows into this box";
      n.fold.hidden = !n.hasInputs;
      n.fold.textContent = on ? `+${hiddenN}` : "−";
    }
    for (const { chain } of chains) { const gone = chain[0].el.hidden || chain[chain.length - 1].el.hidden; chain.slice(1, -1).forEach(l => { l.el.hidden = gone; }); }
    for (const c of colEls) c.hidden = ![...c.children].some(k => !k.hidden);
    unwrapBtn.hidden = ![...wrapped].some(id => nodes.has(id) && nodes.get(id).hasInputs);
    requestWires();
  }

  function paintValues() {
    const rt = store.rt, live = rt.liveVars || {};
    for (const [id, n] of nodes) {
      let v = NaN;
      if (id.startsWith("v:")) { v = live[id.slice(2)]; if (!Number.isFinite(v) && rt.vars && rt.vars[id.slice(2)] instanceof Float64Array) { const a = rt.vars[id.slice(2)]; v = a[a.length - 1]; } }
      else if (id.startsWith("p:")) { const d = rt.sources[n.src]; v = d ? (Number.isFinite(d.live) ? d.live : d.v ? d.v[d.v.length - 1] : NaN) : NaN; if (n.chip) n.el.classList.toggle("nl-flow-retimed", emaOf(store.spec.oracle.sources.find(s => s.id === n.src)).active); }
      if (v > 1e9) v /= 1e18;
      n.val.textContent = n.konst ? "" : Number.isFinite(v) ? fmtNum(v, 7) : "";
    }
  }

  // ---- wires: out of the back of a box, up or down only in the gap between two columns, into the front of the next
  let wt = 0;
  const requestWires = () => { clearTimeout(wt); wt = setTimeout(wires, 30); };
  function wires() {
    if (!alive) return;
    const b = board.getBoundingClientRect(), W = cols.scrollWidth, H = cols.scrollHeight;
    svg.setAttribute("width", W); svg.setAttribute("height", H);
    svg.style.width = W + "px"; svg.style.height = H + "px";
    const at = el => { const r = el.getBoundingClientRect(); return { l: r.left - b.left + board.scrollLeft, r: r.right - b.left + board.scrollLeft, y: r.top - b.top + board.scrollTop + r.height / 2 }; };
    const colBox = colEls.map(c => (c.hidden ? null : at(c)));
    // every hop between two neighbouring columns, grouped by gap, so parallel runs get their own x in the gap
    const hops = new Map();
    for (const ch of chains) {
      ch.pts = null;
      if (ch.chain[0].el.hidden || ch.chain[ch.chain.length - 1].el.hidden) continue;
      ch.pts = ch.chain.map(it => at(it.el));
      ch.cx = [];
      for (let i = 1; i < ch.chain.length; i++) {
        const r = ch.chain[i - 1].rank;
        if (!hops.has(r)) hops.set(r, []);
        hops.get(r).push({ ch, i, y1: ch.pts[i - 1].y, y2: ch.pts[i].y });
      }
    }
    for (const [r, list] of hops) {
      // the gap: from the back of this column to the front of the next one that is on screen
      let nr = r + 1;
      while (nr < colBox.length && !colBox[nr]) nr++;
      const gl = colBox[r] ? colBox[r].r : 0, gr = colBox[nr] ? colBox[nr].l : gl + 40, moving = list.filter(x => Math.abs(x.y2 - x.y1) > 1);
      // wires going down turn right to left by where they start, wires going up left to right: they cross less
      moving.sort((p, q) => (p.y2 > p.y1) - (q.y2 > q.y1) || (p.y2 > p.y1 ? q.y1 - p.y1 : p.y1 - q.y1));
      moving.forEach((x, k) => { x.ch.cx[x.i] = gl + (gr - gl) * (0.18 + 0.64 * (moving.length > 1 ? k / (moving.length - 1) : 0.5)); });
    }
    let html = "";
    for (const ch of chains) {
      if (!ch.pts) continue;
      let d = `M${ch.pts[0].r},${ch.pts[0].y}`;
      for (let i = 1; i < ch.pts.length; i++) {
        const p = ch.pts[i - 1], q = ch.pts[i], cx = ch.cx[i];
        if (cx !== undefined) {
          const dy = q.y - p.y, s = Math.sign(dy), rr = Math.max(0, Math.min(7, Math.abs(dy) / 2, cx - p.r, q.l - cx));
          d += ` L${cx - rr},${p.y} Q${cx},${p.y} ${cx},${p.y + s * rr} L${cx},${q.y - s * rr} Q${cx},${q.y} ${cx + rr},${q.y} L${q.l},${q.y}`;
        } else d += ` L${q.l},${q.y}`;
        if (i < ch.pts.length - 1) d += ` L${q.r},${q.y}`;       // straight through the lane, between the boxes of that column
      }
      html += `<path class="${ch.contract ? "nl-flow-w-c" : ""}" d="${d}"/>`;
    }
    svg.innerHTML = html;
  }

  build();
  detectSmoothing(store).catch(() => {});
  const ro = new ResizeObserver(requestWires);
  ro.observe(board);
  const off = store.on(tag => {
    if (tag === "spec") return build();
    if (tag === "oracle.sources" || tag === "oracle.script") { if (structure() !== sig && !board.contains(document.activeElement)) build(); else if (structure() !== sig) setTimeout(() => { if (alive && structure() !== sig) build(); }, 400); else paintValues(); }
    if (tag === "oracle.data") paintValues();
  });
  return { destroy() { alive = false; off(); ro.disconnect(); clearTimeout(wt); root.remove(); } };
}
