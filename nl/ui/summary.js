// The market at a glance, and the one place its parameters are changed: the chips with a
// field in them write spec.params (the bad-debt run follows by itself), max LTV is derived.
// What is typed lives in this browser only; "Reset" next to the market picker undoes it.
import { fmtNum } from "../core/charts.js";
import { h, tokenLogo, parseNum } from "./kit.js";
import { maxLtv } from "./params.js";

const EDIT = [
  { key: "A", label: "A", min: 2, max: 10000, hint: "LLAMMA A: band width = 1/A. Higher A = narrower bands, higher max LTV, faster soft-liquidation." },
  { key: "fee_pct", label: "fee", unit: "%", min: 0, max: 50, hint: "LLAMMA swap fee charged to soft-liquidation arbitrage." },
  { key: "loan_discount_pct", label: "loan disc.", unit: "%", min: 0, max: 99, hint: "Haircut applied when a loan is opened: sets the max LTV." },
  { key: "liquidation_discount_pct", label: "liq. disc.", unit: "%", min: 0, max: 99, hint: "Haircut in the health formula: the margin a hard-liquidator earns. Must be below the loan discount." },
  { key: "borrow_cap", label: "ceiling", min: 0, max: 1e15, hint: "borrow_cap: the most debt the market permits. The bad-debt sim models the whole ceiling as one borrower." },
];

export function mount(host, ctx, opts = {}) {
  const { store } = ctx;
  const root = h("div", { class: "nl-summary" + (opts.compact ? " nl-compact" : "") });
  host.append(root);
  const idEl = h("div", { class: "nl-summary-id" }), ltv = h("b", {}), busyEl = h("span", { class: "nl-summary-chip nl-live", hidden: true }, h("i", {}, "working"), h("b", {}));
  const inputs = EDIT.map(e => {
    const inp = h("input", { class: "nl-mono", type: "text", inputmode: "decimal", spellcheck: "false", "aria-label": e.label });
    const fit = () => { inp.style.width = Math.max(3, inp.value.length + 1) + "ch"; };      // as wide as what it holds
    inp.addEventListener("input", fit);
    const commit = () => {
      const v = parseNum(inp.value), ok = Number.isFinite(v) && v >= e.min && v <= e.max;
      inp.classList.toggle("nl-bad", !ok);
      if (ok && v !== +store.spec.params[e.key]) store.update("params", s => { s.params[e.key] = v; });
    };
    inp.addEventListener("change", commit);
    inp.addEventListener("keydown", ev => { if (ev.key === "Enter") inp.blur(); });
    return { e, inp, fit, el: h("label", { class: "nl-summary-chip nl-summary-edit", title: e.hint }, h("i", {}, e.label), h("span", {}, inp, e.unit ? h("em", {}, e.unit) : null)) };
  });
  root.append(idEl, h("div", { class: "nl-summary-chips" }, ...inputs.slice(0, 4).map(x => x.el),
    h("span", { class: "nl-summary-chip" }, h("i", {}, "max LTV"), ltv), inputs[4].el, busyEl));

  let idSig = "";
  function paint() {
    const s = store.spec, p = s.params, sig = [s.meta.name, s.collateral.address, s.borrowed.address, s.chain].join("|");
    if (sig !== idSig) {
      idSig = sig;
      idEl.replaceChildren(
        h("span", { class: "nl-summary-logos" }, tokenLogo(s.collateral, s.chain, opts.compact ? 26 : 34), tokenLogo(s.borrowed, s.chain, opts.compact ? 26 : 34)),
        h("div", { class: "nl-grow" }, h("div", { class: "nl-summary-name" }, s.meta.name || `${s.collateral.symbol || "?"} · ${s.borrowed.symbol || "?"}`)));
    }
    for (const { e, inp, fit } of inputs) if (document.activeElement !== inp) { inp.value = String(+p[e.key]); inp.classList.remove("nl-bad"); fit(); }
    ltv.textContent = (maxLtv(p.A, 4, p.loan_discount_pct) || 0).toFixed(1) + " %";
    const busy = Object.values(store.rt.busy || {})[0];
    busyEl.hidden = !busy;
    if (busy) busyEl.lastChild.textContent = `${busy.label}${busy.total ? ` ${busy.done}/${busy.total}` : ""}`;
  }
  paint();
  let t = 0;
  const off = store.on(() => { clearTimeout(t); t = setTimeout(paint, 30); });
  return { destroy() { off(); clearTimeout(t); root.remove(); } };
}
