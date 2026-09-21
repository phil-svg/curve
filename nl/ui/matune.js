// MA sliders: one per oracle component that applies an EMA (a reading that leaves its
// contract smoothed, or an ema() / asym_ema() / lag() of the script), and under them the
// six tiles of the bad-debt run on the crash that is picked above the charts.
//
// While a slider moves, only the evaluation runs (the charts follow every tick, nothing is
// refetched: the sampled EMA is undone on the grid and the new time applied). On release
// the spec is saved and the bad-debt simulation runs again; its result is the same
// results.baddebt the bad-debt card further down shows.
import { compile, sourcesUsed, smoothingOf, setNumberAt } from "../core/expr.js";
import { evaluateOracle, emaOf, buildScenario, detectSmoothing, SMOOTHED_READING } from "../core/pipeline.js";
import { simulate, paintTiles } from "./baddebt.js";
import { preflight, fingerprint, explainRunError } from "./runs.js";
import { h, card, parseNum } from "./kit.js";

// slider position 0 = no EMA, 1..1000 = 30 s .. 12 h on a log scale
const T_MIN = 30, T_MAX = 43200, STEPS = 1000, SNAP = 7;
const round3 = x => +x.toPrecision(3);
const timeAt = pos => (pos <= 0 ? 0 : round3(Math.exp(Math.log(T_MIN) + ((pos - 1) / (STEPS - 1)) * Math.log(T_MAX / T_MIN))));
const posOf = T => (!(T > 0) ? 0 : Math.round(1 + (STEPS - 1) * Math.log(Math.min(T_MAX, Math.max(T_MIN, T)) / T_MIN) / Math.log(T_MAX / T_MIN)));

// opts.card === false: no card frame (the page puts the sliders inside the chart's own card, right under the chart)
export function mount(host, ctx, opts = {}) {
  const { store } = ctx;
  const rowsEl = h("div", { class: "nl-mt-rows" }), none = h("div", { class: "nl-note", hidden: true }, "Nothing in this oracle applies an EMA.");
  const runState = h("span", { class: "nl-note" });
  const tilesEl = h("div", { class: "nl-bd-tiles" });
  const runEl = h("div", { class: "nl-bd nl-mt-run" }, h("div", { class: "nl-mt-runh" }, runState), tilesEl);
  const root = h("div", { class: "nl-mt" }, opts.card === false ? [h("div", { class: "nl-label" }, "MA times"), rowsEl, none, runEl] : card(h("span", {}, "MA times"), rowsEl, none, runEl));
  host.append(root);
  let alive = true, rows = [], sig = "", evalT = 0, runT = 0, running = false, dirty = false, doneSig = "", err = "", focused = false;

  // ---- what gets a slider ---------------------------------------------------------------------------
  function items() {
    const sp = store.spec, used = sourcesUsed(compile(sp.oracle.script)), out = [];
    for (const s of sp.oracle.sources) {
      if (!s.name || !used.has(s.name) || s.kind !== "onchain") continue;
      const e = emaOf(s);
      if (!(e.onchain > 0) && !e.active && !SMOOTHED_READING.test(s.sig || "")) continue;
      out.push({ kind: "source", key: "s:" + s.id, id: s.id, name: s.name, base: e.onchain, value: e.active ? e.use : e.onchain });
    }
    const seen = new Set();
    for (const c of smoothingOf(sp.oracle.script).calls) {
      const k = c.start + ":" + c.end;
      if (!Number.isFinite(c.value) || seen.has(k)) continue;      // two calls on one named constant: one slider
      seen.add(k);
      out.push({ kind: "script", key: `c:${c.line}:${c.fn}:${c.target}`, line: c.line, fn: c.fn, target: c.target, name: c.via || `${c.fn}(${c.target})`,
        base: null, value: c.value });
    }
    return out;
  }
  const callOf = it => smoothingOf(store.spec.oracle.script).calls.find(k => k.line === it.line && k.fn === it.fn && k.target === it.target);

  // ---- a change: live while the slider moves, saved on release ------------------------------------------
  function apply(it, T) {
    const sp = store.spec;
    if (it.kind === "source") {
      const s = sp.oracle.sources.find(x => x.id === it.id);
      if (s) s.ema = { onchain: null, use: null, ...(s.ema || {}), use: T === it.base ? null : T };
    } else {
      const cur = callOf(it);
      if (cur && Number.isFinite(cur.value)) sp.oracle.script = setNumberAt(sp.oracle.script, cur.start, cur.end, T);
    }
    if (!evalT) evalT = setTimeout(() => { evalT = 0; if (alive) evaluateOracle(store); }, 16);
  }
  function save(it) {
    clearTimeout(evalT); evalT = 0;
    store.update(it.kind === "source" ? "oracle.sources" : "oracle.script", () => {});
    evaluateOracle(store);
    wantRun(250);
  }
  // an 866 s EMA cannot be seen on 240 days: the first touch brings the picked crash into view
  function focusCrash() {
    if (focused) return;
    focused = true;
    let sc = null;
    try { sc = store.rt.scenario || buildScenario(store.spec, store.rt); } catch (_) { sc = null; }
    if (sc && sc.picked) store.setRt("oracle.focus", { focus: { a: sc.picked.t0, b: sc.picked.t1 } });
  }

  function makeRow(it) {
    const range = h("input", { type: "range", min: 0, max: STEPS, step: 1, value: posOf(it.value), "aria-label": `MA time of ${it.name}` });
    const num = h("input", { class: "nl-input nl-mono", type: "text", inputmode: "decimal", value: String(it.value), "aria-label": `MA time of ${it.name}, seconds` });
    const tick = h("i", { class: "nl-mt-tick", hidden: !(it.base > 0), title: it.base > 0 ? `on-chain: ${it.base} s` : null });
    if (it.base > 0) tick.style.left = `calc(8px + (100% - 16px) * ${posOf(it.base) / STEPS})`;
    const back = h("button", { type: "button", class: "nl-mt-back", hidden: true, title: "back to the on-chain MA time" }, it.base > 0 ? `on-chain ${it.base} s` : "");
    const el = h("div", { class: "nl-mt-row" }, h("div", { class: "nl-mt-id" }, h("b", { class: "nl-mono" }, it.name)),
      h("div", { class: "nl-mt-slide" }, range, tick), h("label", { class: "nl-mt-num" }, num, h("em", { class: "nl-unit" }, "s")), back);
    const row = { it, el, value: it.value };
    row.show = T => {
      row.value = T;
      if (document.activeElement !== num) num.value = String(T);
      range.value = posOf(T);
      back.hidden = !(it.base > 0) || T === it.base;
      el.classList.toggle("nl-mt-changed", it.base > 0 && T !== it.base);
    };
    range.addEventListener("pointerdown", focusCrash);
    range.addEventListener("keydown", focusCrash);
    range.addEventListener("input", () => {
      let T = timeAt(+range.value);
      if (it.base > 0 && Math.abs(+range.value - posOf(it.base)) <= SNAP) T = it.base;      // easy to land back on the on-chain value
      num.value = String(T); row.value = T; back.hidden = !(it.base > 0) || T === it.base;
      el.classList.toggle("nl-mt-changed", it.base > 0 && T !== it.base);
      apply(it, T);
    });
    range.addEventListener("change", () => save(it));
    const typed = () => {
      const x = parseNum(num.value);
      if (!Number.isFinite(x) || x < 0) { num.classList.add("nl-bad"); return; }
      num.classList.remove("nl-bad");
      focusCrash(); row.show(x); apply(it, x); save(it);
    };
    num.addEventListener("change", typed);
    num.addEventListener("keydown", e => { if (e.key === "Enter") { typed(); num.blur(); } });
    back.addEventListener("click", () => { row.show(it.base); apply(it, it.base); save(it); });
    row.show(it.value);
    return row;
  }

  function build() {
    const list = items(), next = JSON.stringify(list.map(i => [i.key, i.name, i.base]));
    if (next !== sig) {
      sig = next;
      rows = list.map(makeRow);
      rowsEl.replaceChildren(...rows.map(r => r.el));
      none.hidden = rows.length > 0;
    } else list.forEach((it, k) => { rows[k].it = Object.assign(rows[k].it, it); if (!root.contains(document.activeElement) || document.activeElement.type !== "range") rows[k].show(it.value); });
  }

  // ---- the bad-debt run under the sliders ------------------------------------------------------------------
  function paintRun() {
    if (!alive) return;
    const R = store.spec.results.baddebt, has = !!(R && R.kpis && R.series);
    let pf = null;
    try { pf = preflight(store.spec, store.rt); } catch (_) { pf = null; }
    const blocked = !pf || (pf.problems || []).length > 0, stale = has && pf && pf.inputs && fingerprint(pf.inputs, true) !== fingerprint(R.inputs, true);
    runState.textContent = running ? "computing…" : err ? err : blocked && pf ? pf.problems[0] : "";
    runState.classList.toggle("nl-err", !running && !!err);
    tilesEl.hidden = !has;
    tilesEl.classList.toggle("nl-mt-stale", !!stale || running);
    if (has && tilesEl._R !== R) { tilesEl._R = R; paintTiles(tilesEl, R); }
  }
  function wantRun(delay = 600) { clearTimeout(runT); runT = setTimeout(maybeRun, delay); }
  async function maybeRun() {
    if (!alive) return;
    if (running) { dirty = true; return; }
    if (store.rt.busy.sources) return wantRun(800);
    let pf = null;
    try { pf = preflight(store.spec, store.rt); } catch (_) { pf = null; }
    if (!pf || (pf.problems || []).length) return paintRun();
    const want = fingerprint(pf.inputs, true), R = store.spec.results.baddebt;
    if ((R && R.inputs && fingerprint(R.inputs, true) === want) || want === doneSig) return paintRun();
    running = true; err = ""; paintRun();
    try {
      const out = await simulate(store);
      doneSig = want;
      if (alive) store.update("results", s => { s.results.baddebt = out; });
    } catch (e) { doneSig = want; err = explainRunError(e); }
    finally { running = false; paintRun(); if (dirty) { dirty = false; wantRun(50); } }
  }

  build(); paintRun(); wantRun(400);
  detectSmoothing(store).catch(() => {});
  const closePops = () => tilesEl.querySelectorAll(".nl-bd-pop").forEach(p => { p.hidden = true; });
  document.addEventListener("click", closePops);
  const off = store.on(tag => {
    if (tag === "spec") { sig = ""; doneSig = ""; err = ""; focused = false; build(); paintRun(); wantRun(); return; }
    if (tag === "oracle.sources" || tag === "oracle.script") build();
    if (tag === "results" || tag === "busy") paintRun();
    if (["oracle.data", "scenario", "scenario.data", "params", "venue"].includes(tag)) { paintRun(); wantRun(); }
  });
  return { destroy() { alive = false; off(); clearTimeout(evalT); clearTimeout(runT); document.removeEventListener("click", closePops); root.remove(); } };
}
