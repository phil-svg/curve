// The page: one market, top to bottom, nothing behind steps. A market comes in by pull
// request (nl/markets/, core/registry.js) with its tokens, parameters, oracle and monetary
// policy, so there is nothing to set up here. What the page is for is playing with it: the
// oracle against the spot price, the MA times, the parameters (in the strip on top), and the
// two simulations. The chart on top is also the crash's chart: a pick zooms it there.
// One rule keeps it readable: what is rarely changed is a fold that says what it is set to
// in its summary line, so it only needs opening to change it.
import * as market from "./ui/market.js";
import * as summary from "./ui/summary.js";
import * as params from "./ui/params.js";
import * as oracle from "./ui/oracle.js";
import * as ema from "./ui/ema.js";
import * as flow from "./ui/flow.js";
import * as crash from "./ui/crash.js";
import * as baddebt from "./ui/baddebt.js";
import * as runs from "./ui/runs.js";
import * as matune from "./ui/matune.js";
import { fmtUsd } from "./core/charts.js";
import { h } from "./ui/kit.js";

const C = { market, summary, params, oracle, ema, flow, crash, baddebt, runs, matune };

function mounter(ctx) {
  const live = [];
  const m = {
    // put("oracle", {part: "chart"}) -> a fresh host element with the component inside
    put(name, opts = {}, cls = "") {
      const host = h("div", { class: "nl-slot " + cls });
      try { live.push(C[name].mount(host, ctx, opts)); }
      catch (e) { console.error("[nl] mount", name, e); host.append(h("div", { class: "nl-err" }, `${name}: ${e.message}`)); }
      return host;
    },
    track(inst) { live.push(inst); return inst; },
    destroyAll() { while (live.length) { try { live.pop().destroy(); } catch (e) { console.error("[nl] destroy", e); } } },
  };
  return m;
}

// A fold: title + a live one-line summary of what is inside; the content is only
// mounted while it is open, and the open state is remembered.
function fold(m, ctx, key, title, summaryOf, build) {
  const { store } = ctx, LS = "nl.fold." + key;
  let sub = null, open = false, t = 0;
  try { open = localStorage.getItem(LS) === "1"; } catch (_) { /* private mode */ }
  const sum = h("span", { class: "nl-fold-sum" }), body = h("div", { class: "nl-stack nl-fold-body" });
  const det = h("details", { class: "nl-fold", open }, h("summary", {}, h("b", {}, title), sum), body);
  const paint = () => { let s = ""; try { s = summaryOf(store) || ""; } catch (_) { s = ""; } if (sum.textContent !== s) sum.textContent = s; };
  const sync = () => {
    if (det.open && !sub) { sub = mounter(ctx); body.replaceChildren(...build(sub)); }
    else if (!det.open && sub) { sub.destroyAll(); sub = null; body.replaceChildren(); }
    try { localStorage.setItem(LS, det.open ? "1" : "0"); } catch (_) { /* private mode */ }
  };
  det.addEventListener("toggle", sync);
  if (open) sync();
  paint();
  const off = store.on(() => { clearTimeout(t); t = setTimeout(paint, 120); });
  m.track({ destroy() { off(); clearTimeout(t); if (sub) sub.destroyAll(); } });
  return det;
}

const plain = x => (Number.isFinite(+x) ? String(+(+x).toPrecision(6)) : "–");
const venueSummary = st => { const v = st.spec.venue; return `${v.pool_type} · ${fmtUsd(+v.tvl_usd)} · ${v.pool_type === "cryptoswap" ? "A_raw " + plain(v.A_raw) : "A " + plain(v.ss_A)}${v.state ? " · real pool state" : ""}`; };
const historySummary = st => { const r = st.spec.oracle.range, s = +r.step_s; return `${plain(r.days)} days at ${s % 3600 === 0 ? s / 3600 + " h" : s / 60 + " min"}`; };
export function mount(host, ctx) {
  const { store } = ctx;
  const root = h("div", { class: "nl-page" });
  host.append(root);
  let m = null, t = 0;
  function build() {
    if (m) m.destroyAll();
    m = mounter(ctx);
    root.replaceChildren(
      m.put("market", { part: "pick" }),
      m.put("summary", { compact: true }),
      // the oracle against the spot price. Above the chart: which recorded crash, and how much deeper it is played (the
      // chart zooms there and the sims replay it). Under it: a slider per MA time, and what each does to the bad-debt run.
      m.put("oracle", { part: "chart", height: 460, chartFirst: true, figures: "none", zoom: true,
        above: m.put("crash"), below: m.put("matune", { card: false }), title: "Price chart" }, "nl-oracle-hero"),
      m.put("flow"),
      h("div", { class: "nl-folds" },
        fold(m, ctx, "venue", "Liquidation venue", venueSummary, f => [f.put("params", { part: "venue" })]),
        fold(m, ctx, "history", "Price history", historySummary, f => [f.put("oracle", { part: "range", title: "Price history" })]),
        fold(m, ctx, "smoothing", "What the MA times do", () => "", f => [f.put("ema", { part: "chart", height: 260 })])),
      m.put("baddebt"),
      m.put("runs", { part: "sldl" }),
      h("div", { class: "nl-folds" }, fold(m, ctx, "howto", "Add a market", () => "by pull request", f => [f.put("market", { part: "howto" })])));
  }
  build();
  // another market (or Reset): everything is rebuilt on it
  const off = store.on(tag => { if (tag === "spec") { clearTimeout(t); t = setTimeout(build, 0); } });
  return { destroy() { off(); clearTimeout(t); if (m) m.destroyAll(); root.remove(); } };
}
