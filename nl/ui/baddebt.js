// The bad-debt simulation, with the readout of the Bad-Debt-Sim tab: the six
// market-health tiles (bad debt, the two real transfers, the two worlds, vs
// HODL) each with its equation, then the charts of that tab, one under the
// other in the same order, with the same colours, scales and hover rows:
// price, bad debt, soft-liq volume, composition, borrower equity, lender
// equity, health (signed log axis), hard liquidations, bands. The modelled
// borrower is fixed (4 bands, debt at the ceiling, least collateral): see
// pipeline.resolveBorrower.
import { api } from "../core/api.js";
import { buildScenario, resolveBorrower, toRunParams } from "../core/pipeline.js";
import { lineChart, bandsChart, fmtNum, fmtUsd, fmtElapsed } from "../core/charts.js";
import { preflight, fingerprint, explainRunError } from "./runs.js";
import { h, button, card, progress, toast } from "./kit.js";

const N = v => (v === null || v === undefined || v === "" ? NaN : +v);
const r6 = x => (Number.isFinite(x) ? +x.toPrecision(8) : null);
const usd = v => (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString("en-US");
const loss = v => (Math.round(v) < 0 ? "gain $" + Math.abs(Math.round(v)).toLocaleString("en-US") : "$" + Math.round(v).toLocaleString("en-US"));
const tok = v => Math.round(v).toLocaleString("en-US");
const nan = a => (a || []).map(N);

// ---- /run response -> what is stored (and exported) --------------------------------
export function trim(res, { inputs, borrower, at }) {
  const rows = res.rows || [], per = (res.soft_liq && res.soft_liq.per_block) || {};
  let o = NaN;
  const S = { t: [], badDebt: [], health: [], target: [], venue: [], oracle: [], debt: [], accrual: [], collTok: [], collUsd: [], lendUsd: [],
    hlUsd: [], hlCollUsd: [], hlProfit: [], hlCollTok: [], hlTokUsd: [], slLiq: [], slDeliq: [], slPnl: [], extArb: [] };
  const nb = rows.length && Array.isArray(rows[0].bands) ? rows[0].bands.length : 0;
  const bands = Array.from({ length: nb }, () => ({ x: [], y: [] }));
  for (const r of rows) {
    const k = String(r.blockNumber);
    if (res.oracle && res.oracle[k] !== undefined) o = +res.oracle[k];
    const sl = per[k];
    S.t.push(r6(r.elapsed_s)); S.badDebt.push(r6(r.badDebt || 0)); S.health.push(r.debtUsd === 0 ? null : r6(r.health));
    S.target.push(r6(r.target_spot)); S.venue.push(r6(r.pool_crv_spot)); S.oracle.push(r6(o));
    S.debt.push(r6(r.debtUsd)); S.accrual.push(r6(r.accrual || 1)); S.collTok.push(r6(r.comp_coll_tokens)); S.collUsd.push(r6(r.comp_coll_usd)); S.lendUsd.push(r6(r.comp_lend_usd));
    S.hlUsd.push(r6(r.hardLiqUsd || 0)); S.hlCollUsd.push(r6(r.hardLiqCollUsd || 0)); S.hlProfit.push(r6(r.hardLiqProfit || 0));
    S.hlCollTok.push(r6(r.hardLiqCollTok || 0)); S.hlTokUsd.push(r6(r.hardLiqTokUsd || 0));
    S.slLiq.push(sl && sl.dir === "LIQ" ? sl.usd : 0); S.slDeliq.push(sl && sl.dir !== "LIQ" ? sl.usd : 0);
    S.slPnl.push(sl && sl.pnl !== undefined ? r6(sl.pnl) : null); S.extArb.push(r6(r.extArbUsd || 0));
    (r.bands || []).forEach((b, i) => { if (bands[i]) { bands[i].n = b[0]; bands[i].pu = r6(b[3]); bands[i].x.push(r6(b[1])); bands[i].y.push(r6(b[2])); } });
  }
  const out = { at, label: inputs.scenario ? inputs.scenario.label : "", inputs, borrower: { ...(res.borrower || {}), ...borrower }, series: S, bands,
    sl_totals: (res.soft_liq && res.soft_liq.totals) || {}, venue_note: res.venue_note || "", timing: res.timing || {} };
  out.kpis = kpisOf(out);
  return out;
}

// the six tiles, exactly as the Bad-Debt-Sim tab defines them
export function kpisOf(R) {
  const S = R.series, n = S.t.length, last = n - 1, bw = R.borrower || {}, t = R.sl_totals || {};
  const sum = a => (a || []).reduce((x, y) => x + (N(y) || 0), 0);
  const bd = N(S.badDebt[last]) || 0, peak = Math.max(0, ...S.badDebt.map(x => N(x) || 0));
  const slCost = (t.liq_pnl || 0) + (t.deliq_pnl || 0), slSwap = (t.liq_usd || 0) + (t.deliq_usd || 0), slN = (t.liq_n || 0) + (t.deliq_n || 0);
  const hlProfit = sum(S.hlProfit), hlColl = sum(S.hlCollUsd), hlVol = sum(S.hlUsd), hlN = S.hlUsd.filter(x => N(x) > 0).length;
  const hl = hlColl > 0 ? hlColl - hlVol : hlProfit;
  // both worlds valued at the EXTERNAL spot, never the venue's marginal
  const val = i => (S.collTok[i] == null || S.lendUsd[i] == null || S.target[i] == null ? null : S.collTok[i] * S.target[i] + S.lendUsd[i]);
  const V0 = val(0), V1 = val(last);
  const k = { bad_debt: bd, peak_bad_debt: peak, sl_cost: slCost, sl_swap: slSwap, sl_n: slN, hl, hl_coll: hlColl, hl_vol: hlVol, hl_profit: hlProfit, hl_n: hlN,
    min_health: Math.min(...S.health.map(x => (x == null ? Infinity : x))), debt_usd: N(bw.debt_usd), collateral_usd: N(bw.collateral_usd), ltv_pct: N(bw.ltv_pct) };
  if (V0 != null && V1 != null && Number.isFinite(N(bw.debt_usd))) {
    const y0 = N(bw.crv) || S.collTok[0], p1 = S.target[last], D = N(bw.debt_usd), D1 = S.debt[last] != null ? S.debt[last] : D, accr = S.accrual[last] || 1;
    const hodl = V0 - y0 * p1 + D * (accr - 1), llamma = (V0 - D) - (V1 - D1), better = hodl - llamma;
    const ySeized = sum(S.hlCollTok), hlTokUsd = sum(S.hlTokUsd), timing = ySeized > 0 ? ySeized * p1 - hlTokUsd : 0;
    const parts = [["price move on the position (the HODL loss)", hodl]];
    if (hl) parts.push(["hard-liq spread", hl]);
    if (ySeized > 0) parts.push([`price move after seizure on ${tok(ySeized)} tokens (sold at $${(hlTokUsd / ySeized).toPrecision(6)}, ended at $${(+p1).toPrecision(6)})`, timing]);
    if (slCost) parts.push(["soft-liq spread", slCost]);
    // What the Bad-Debt-Sim tab leaves as "unexplained" when soft-liq ran: the round trip
    // itself. Collateral is sold as the price falls and bought back as it recovers, at a
    // higher price, so the position ends with fewer tokens. Net tokens converted, valued at
    // the end, minus the cash still held, minus the arb spread already counted. Exact when
    // no hard liquidation took tokens and cash out by another route.
    if (!(hlVol > 0) && slN > 0 && S.collTok[last] != null && S.lendUsd[last] != null) {
      const trip = (y0 - S.collTok[last]) * p1 - S.lendUsd[last] - slCost;
      if (Math.abs(trip) >= 1) parts.push([`conversion round trip: ${tok(y0 - S.collTok[last])} tokens sold in the fall and not bought back by the end (beyond the arb spread)`, trip]);
    }
    if (bd > 0) parts.push(["bad debt (charged here, but the protocol eats it)", bd]);
    const resid = llamma - parts.reduce((a, q) => a + q[1], 0);
    if (Math.abs(resid) >= 1) parts.push(["unexplained", resid]);
    Object.assign(k, { V0, V1, D, D1, y0, p1, interest: D * (accr - 1), hodl_loss: hodl, llamma_loss: llamma, vs_hodl: better, parts });
  }
  return k;
}

export function tiles(R) {
  const k = R.kpis, out = [];
  out.push({ label: "Bad debt", val: usd(k.bad_debt), cls: k.bad_debt > 0 ? "bad" : "good",
    desc: "Debt no longer covered by the position backing it. Nobody can be made to pay this: the protocol eats it.",
    eq: `max(0, debt − x − p·y) at the end = ${usd(k.bad_debt)}` + (k.peak_bad_debt > k.bad_debt ? ` · peak during the run ${usd(k.peak_bad_debt)}` : "") });
  out.push({ label: "User loss: soft / de-liq (arbs)", val: usd(k.sl_cost), cls: k.sl_cost > 0 ? "warn" : "good",
    desc: "Paid to arbitrageurs who converted the collateral: they bought below, or sold above, the market price of the moment. The only soft-liq cost that is a real transfer.",
    eq: `Σ (market value − AMM value) over ${k.sl_n} fills on ${usd(k.sl_swap)} converted = ${usd(k.sl_cost)}` });
  out.push({ label: "User loss: hard liquidations", val: usd(k.hl), cls: k.hl > 0 ? "warn" : "good",
    desc: "Collateral seized, at market, minus the debt it repaid. Part reaches the liquidator as profit; the rest is slippage and gas burned getting that collateral sold.",
    eq: k.hl_vol > 0 ? `Σ (collateral taken − debt repaid) over ${k.hl_n} slices: ${usd(k.hl_coll)} seized − ${usd(k.hl_vol)} repaid = ${usd(k.hl)}, of which ${usd(k.hl_profit)} to liquidators and ${usd(k.hl - k.hl_profit)} to venue slippage + gas`
      : "no slice was profitable to seize = $0" });
  if (k.hodl_loss !== undefined) {
    out.push({ label: "User loss if HODL", val: loss(k.hodl_loss), cls: k.hodl_loss > 0 ? "warn" : "good",
      desc: "Counterfactual: the same borrower never soft-liquidated, still holding every collateral token at the end, and still owing the same interest.",
      eq: `V₀ − y₀·p₁ + interest = ${usd(k.V0)} − ${tok(k.y0)} × $${(+k.p1).toPrecision(6)} + ${usd(k.interest)} = ${usd(k.hodl_loss)}` });
    out.push({ label: "User loss if LLAMMA", val: loss(k.llamma_loss), cls: k.llamma_loss > 0 ? "warn" : "good",
      desc: "What actually happened: equity at the start minus equity at the end. Equity = position value − debt still owed (interest included).",
      eq: `E₀ − E₁ = (${usd(k.V0)} − ${usd(k.D)}) − (${usd(k.V1)} − ${usd(k.D1)}) = ${usd(k.llamma_loss)}`,
      parts: k.parts });
    const b = Math.round(k.vs_hodl);
    out.push({ label: "vs HODL", val: (b > 0 ? "+$" : b < 0 ? "−$" : "$") + Math.abs(b).toLocaleString("en-US"), cls: b > 0 ? "good" : b < 0 ? "bad" : "",
      desc: b > 0 ? "Soft-liquidating left the borrower better off than holding through the crash." : b < 0 ? "Soft-liquidating cost the borrower more than simply holding through the crash." : "Soft-liquidating left the borrower exactly where holding would have.",
      eq: `HODL loss ${usd(k.hodl_loss)} − LLAMMA loss ${usd(k.llamma_loss)} = ${usd(k.vs_hodl)}` });
  }
  return out;
}

// the six tiles as DOM, into `host` (the bad-debt card and the MA sliders under the oracle chart show the same ones)
export function paintTiles(host, R) {
  host.replaceChildren(...tiles(R).map(c => {
    const pop = h("div", { class: "nl-bd-pop", hidden: true }, h("p", {}, c.desc), h("p", { class: "nl-mono" }, c.eq),
      c.parts ? h("ul", { class: "nl-mono" }, c.parts.map(q => h("li", {}, `${usd(q[1])}  ${q[0]}`))) : null);
    return h("div", { class: "nl-bd-tile nl-" + (c.cls || "") }, h("span", { class: "nl-bd-tl" }, c.label,
      h("button", { type: "button", class: "nl-q", title: "what this means", onClick: e => { e.stopPropagation(); const was = pop.hidden; host.querySelectorAll(".nl-bd-pop").forEach(p => { p.hidden = true; }); pop.hidden = !was; } }, "i")),
      h("b", {}, c.val), pop);
  }));
}

// One bad-debt run on the spec as it stands -> the trimmed result (what results.baddebt holds)
export async function simulate(store) {
  const spec = store.spec, rt = store.rt;
  const scenario = buildScenario(spec, rt);
  if (!scenario) throw new Error("no scenario to play: load the price history or draw a path first");
  const seed = scenario.oracle && spec.scenario.oracle_mode === "recorded" ? scenario.oracle[0] : scenario.market[0];
  const borrower = await resolveBorrower(spec, scenario.market[0], seed);
  const inputs = preflight(spec, { ...rt, scenario }).inputs, params = toRunParams(spec, scenario, borrower);
  const res = await api.run(params);
  return trim(res, { inputs, borrower, at: Math.floor(Date.now() / 1000) });
}

// the colours of the Bad-Debt-Sim tab, so the two read as one tool
const HEX = { spot: "#8b949e", oracle: "#1f6feb", bad: "#ED7D31", liq: "#58a6ff", deliq: "#70AD47", coll: "#A78BFA", lend: "#4DB6AC",
  equity: "#F28E2B", intact: "#70AD47", red: "#f85149", health: "#e3b341" };
const money = v => (v < 0 ? "−$" : "$") + Math.abs(Math.round(v || 0)).toLocaleString("en-US");
const px = v => (Number.isFinite(N(v)) ? "$" + fmtNum(N(v), 6) : "–");
// compact money for an axis, with as many decimals as the tick step needs ($1.98M / $1.99M / $2.00M)
const axisUsd = (v, step) => {
  const a = Math.abs(v), sg = v < 0 ? "−" : "", unit = a >= 1e9 ? 1e9 : a >= 1e6 ? 1e6 : a >= 1e3 ? 1e3 : 1;
  const d = unit === 1 ? 0 : step > 0 ? Math.min(3, Math.max(0, Math.ceil(-Math.log10(step / unit) - 1e-9))) : a / unit >= 10 ? 0 : 1;
  return `${sg}$${(a / unit).toFixed(d)}${unit === 1e9 ? "B" : unit === 1e6 ? "M" : unit === 1e3 ? "k" : ""}`;
};
const healthPct = v => { const a = Math.abs(v * 100); return (v < 0 ? "−" : v > 0 ? "+" : "") + (a >= 1 ? a.toFixed(0) : a >= 0.1 ? a.toFixed(1) : a.toFixed(2)) + "%"; };

// same order as the Bad-Debt-Sim tab
const CHARTS = [
  { id: "price", title: "Collateral price, $", h: 220, empty: "run the simulation to see the price path" },
  { id: "bad", title: "Bad debt per step, $", h: 300, empty: "run the simulation to see the bad debt" },
  { id: "sl", title: "Soft-liq / de-liq volume, $ per step", h: 220, empty: "run the simulation to see the soft-liquidation flow" },
  { id: "comp", title: "Collateral composition, $", h: 300, empty: "run the simulation to see the composition" },
  { id: "equity", title: "Borrower equity, $", h: 220, empty: "run the simulation to see borrower equity" },
  { id: "lender", title: "Lender equity, $", h: 220, empty: "run the simulation to see lender equity" },
  { id: "health", title: "Borrower health", h: 220, empty: "run the simulation to see borrower health", note: "log axis, each gridline 10x · below 0 = liquidatable" },
  { id: "hl", title: "Hard liquidations, $ debt repaid per step", h: 220, empty: "run the simulation to see hard-liquidation activity" },
];
const LENDER_NOTE = "What the funded principal is still worth to the lenders: lender equity = debt₀ − badDebt(t), where debt₀ is the borrower's debt at t = 0 and " +
  "badDebt = max(0, debt − x − p·y), the slice of the loan the position no longer covers even at market prices. Repayments and hard liquidations return principal " +
  "(no change here); only bad debt destroys it. Interest earned is not included.";

export function mount(host, ctx, opts = {}) {
  const { store } = ctx;
  const root = h("div", { class: "nl-bd" });
  host.append(root);
  let running = false, poll = 0, lastErr = "", charts = {}, alive = true, specToken = {}, shown = null;

  const runBtn = button("Run bad-debt simulation", { kind: "primary", onClick: run });
  const bar = progress(), reason = h("div", { class: "nl-note nl-bd-reason" }), errEl = h("div", { class: "nl-err", hidden: true });
  const stale = h("span", { class: "nl-badge nl-warn", hidden: true }, "inputs changed since this run");
  const meta = h("div", { class: "nl-note nl-grow" }), tilesEl = h("div", { class: "nl-bd-tiles" });
  const chartsEl = h("div", { class: "nl-bd-charts" });
  const resultEl = h("div", { class: "nl-bd-result nl-stack", hidden: true }, h("div", { class: "nl-row" }, meta, stale), tilesEl);
  const empty = h("div", { class: "nl-note nl-bd-empty" }, "No run yet.");
  const noBad = h("span", { class: "nl-badge nl-good", hidden: true }, "no bad debt");
  const lqBadge = h("span", { class: "nl-badge", hidden: true }), lqNote = h("div", { class: "nl-note", hidden: true }, LENDER_NOTE);
  const lqInfo = h("button", { type: "button", class: "nl-q nl-bd-i", title: "what this means", onClick: () => { lqNote.hidden = !lqNote.hidden; } }, "i");

  // what the cursor reads on each chart, the rows of the Bad-Debt-Sim tab
  const at = (k, i) => (shown ? N(shown.series[k] && shown.series[k][i]) : NaN);
  const TIPS = {
    price: i => [["spot", px(at("target", i)), HEX.spot], ["oracle", px(at("oracle", i)), HEX.oracle]],
    bad: i => [["bad debt", money(at("badDebt", i)), HEX.bad]],
    sl: i => {
      const liq = at("slLiq", i) || 0, de = at("slDeliq", i) || 0, pnl = at("slPnl", i), ext = at("extArb", i), rows = [];
      if (liq > 0 || de > 0) { rows.push(liq > 0 ? ["soft-liq vol", money(liq), HEX.liq] : ["de-liq vol", money(de), HEX.deliq]); if (Number.isFinite(pnl)) rows.push(["arb PnL", (pnl >= 0 ? "+" : "−") + "$" + Math.abs(pnl).toLocaleString("en-US"), null]); }
      else rows.push(["soft-liq vol", "$0", HEX.liq]);
      if (ext > 0) rows.push(["ext arb", money(ext), HEX.spot]);
      return rows;
    },
    comp: i => [["collateral", `${money(at("collUsd", i))} (${tok(at("collTok", i) || 0)} tok)`, HEX.coll], ["lending", money(at("lendUsd", i)), HEX.lend],
      ["total", money((at("collUsd", i) || 0) + (at("lendUsd", i) || 0)), null]],
    equity: i => { const eq = (at("collUsd", i) || 0) + (at("lendUsd", i) || 0) - (at("debt", i) || 0);
      return [["borrower equity", money(eq), eq < 0 ? HEX.red : HEX.equity], ["collateral", money(at("collUsd", i)), HEX.coll], ["band crvUSD", money(at("lendUsd", i)), HEX.lend], ["debt", money(at("debt", i)), HEX.spot]]; },
    lender: i => { const d0 = N(shown.kpis.debt_usd), bd = at("badDebt", i) || 0; return [["lender equity", money(d0 - bd), HEX.intact], ["funded principal", money(d0), HEX.spot], ["bad debt", money(bd), HEX.bad]]; },
    health: i => { const x = at("health", i);
      return !Number.isFinite(x) || shown.series.health[i] == null ? [["health", "–", HEX.health]]
        : [["health", (x * 100).toFixed(3) + "%", x < 0 ? HEX.red : HEX.health], ["status", x < 0 ? "liquidatable" : "safe", x < 0 ? HEX.red : HEX.intact],
          ["debt", money(at("debt", i)), HEX.spot], ["collateral", money((at("collUsd", i) || 0) + (at("lendUsd", i) || 0)), HEX.spot]]; },
    hl: i => (at("hlUsd", i) > 0 ? [["debt repaid", money(at("hlUsd", i)), HEX.red], ["liquidator profit", money(at("hlProfit", i)), null]] : [["hard-liq", "$0", HEX.red]]),
  };
  for (const c of CHARTS) {
    const extra = c.id === "bad" ? noBad : c.id === "lender" ? h("span", { class: "nl-row nl-tight" }, lqBadge, lqInfo) : null;
    const el = h("div", { class: "nl-bd-chart" }, h("div", { class: "nl-bd-chart-h" }, h("span", { class: "nl-bd-chart-t" }, c.title, c.note ? h("span", { class: "nl-note" }, c.note) : null), extra),
      c.id === "lender" ? lqNote : null, h("div", {}));
    chartsEl.append(el);
    const flows = ["bad", "sl", "hl"].includes(c.id);      // from 0 up, and never a degenerate axis when nothing happened
    charts[c.id] = lineChart(el.lastChild, { height: c.h, xMode: "elapsed", empty: c.empty, zeroBase: flows, minTop: flows ? 10 : undefined,
      symlog: c.id === "health" ? 1e-4 : undefined, tipRows: i => (shown ? TIPS[c.id](i) : []),
      yFmt: c.id === "health" ? healthPct : c.id === "price" ? v => fmtNum(v, 6) : axisUsd });
  }
  const bandsEl = h("div", { class: "nl-bd-chart" }, h("div", { class: "nl-bd-chart-h" }, h("span", { class: "nl-bd-chart-t" }, "Bands: one row per band, $ over time")), h("div", {}));
  chartsEl.append(bandsEl);
  charts.bands = bandsChart(bandsEl.lastChild, { xMode: "elapsed", empty: "run the simulation to see the bands",
    tipRows: i => (shown ? [...(shown.bands || []).slice().sort((p, q) => p.n - q.n).map(b => [`band ${b.n}`, `${money((N(b.y[i]) || 0) * (at("target", i) || 0))} coll · ${money(N(b.x[i]))} lend`, "#9B7BEA"]),
      ["spot", px(at("target", i)), HEX.spot], ["oracle", px(at("oracle", i)), HEX.oracle]] : []) });
  root.append(card(h("span", {}, "Bad-debt simulation"), h("div", { class: "nl-row nl-bd-run" }, runBtn, h("div", { class: "nl-grow nl-bd-runinfo" }, bar, reason)), errEl, empty, resultEl, chartsEl));

  let pf = null;
  function paintPre() {
    try { pf = preflight(store.spec, store.rt); } catch (e) { pf = { problems: [String(e.message || e)], items: {}, warn: new Set() }; }
    const blocked = (pf.problems || []).length > 0;
    runBtn.disabled = running || blocked;
    reason.textContent = running || !blocked ? "" : pf.problems.join(" · ");
    reason.classList.toggle("nl-err", blocked && !running);
    const r = store.spec.results.baddebt;
    stale.hidden = !(r && r.inputs && pf.inputs && fingerprint(pf.inputs, true) !== fingerprint(r.inputs, true));
  }

  function paintResult() {
    const R = store.spec.results.baddebt, has = !!(R && R.series && R.series.t && R.series.t.length > 1 && R.series.collTok);
    empty.hidden = has; resultEl.hidden = !has; chartsEl.hidden = !has;      // nine empty frames explain nothing: the charts arrive with the first run
    shown = has ? R : null;
    noBad.hidden = true; lqBadge.hidden = true;
    if (!has) { Object.values(charts).forEach(c => c.setData(null)); return; }
    const S = R.series, t = Float64Array.from(nan(S.t)), F = a => Float64Array.from(nan(a)), n = S.t.length;
    const when = new Date(R.at * 1000).toISOString().slice(0, 16).replace("T", " ");
    meta.textContent = `run of ${when} UTC · ${R.label} · ${S.t.length} rows over ${fmtElapsed(S.t[S.t.length - 1])} · engine ${fmtNum((R.timing && R.timing.total_s) || 0, 2)} s` + (R.venue_note ? ` · venue: ${R.venue_note}` : "");
    paintTiles(tilesEl, R);
    const lead = (R.inputs.scenario && R.inputs.scenario.lead_in_min * 60) || 0;
    const vl = lead ? [{ x: lead, label: "scenario starts" }] : [];
    const peak = R.kpis.peak_bad_debt || 0, D0 = N(R.kpis.debt_usd);
    noBad.hidden = peak > 0;
    lqBadge.hidden = false; lqBadge.className = "nl-badge " + (peak > 0 ? "nl-bad" : "nl-good");
    lqBadge.textContent = peak > 0 ? `bad debt: $${Math.round(peak).toLocaleString("en-US")} peak` : "no bad debt";
    lqBadge.title = peak > 0 ? "the run produced bad debt: the loan slice no longer covered even at market prices, subtracted from lender equity below" : "every step of this run ended with zero bad debt";

    charts.price.setData({ t, vlines: vl, series: [{ name: "spot", v: F(S.target), color: HEX.spot, width: 1.6 }, { name: "oracle", v: F(S.oracle), color: HEX.oracle, width: 2.2 }] });
    charts.bad.setData({ t, hlines: [{ y: 0, color: "#6e7681" }], series: [{ name: "bad debt", v: F(S.badDebt), color: HEX.bad, width: 2.2 }] });
    charts.sl.setData({ t, series: [{ name: "soft-liq (borrowed → AMM, collateral converts)", v: F(S.slLiq), color: HEX.liq, type: "bars" },
      { name: "de-liq (collateral → AMM, conversion reversed)", v: F(S.slDeliq), color: HEX.deliq, type: "bars" }] });
    charts.comp.setData({ t, stack: true, series: [{ name: "collateral token", v: F(S.collUsd), color: HEX.coll, type: "area" }, { name: "lending token", v: F(S.lendUsd), color: HEX.lend, type: "area" }] });
    charts.equity.setData({ t,
      series: [{ name: "equity", v: F(S.t.map((_, i) => (N(S.collUsd[i]) || 0) + (N(S.lendUsd[i]) || 0) - (N(S.debt[i]) || 0))),
        color: HEX.equity, width: 1.8, fill: "zero", fillAlpha: 0.25 }] });
    // lender equity: green while the principal is intact, red while bad debt bites; the two join at each switch
    const lq = S.badDebt.map(x => D0 - (N(x) || 0)), red = S.badDebt.map(x => (N(x) || 0) > 0), g = new Float64Array(n).fill(NaN), r = new Float64Array(n).fill(NaN);
    for (let i = 0; i < n; i++) { (red[i] ? r : g)[i] = lq[i]; if (i && red[i] !== red[i - 1]) (red[i] ? r : g)[i - 1] = lq[i - 1]; }
    charts.lender.setData({ t, hlines: [{ y: D0, label: "funded principal", color: HEX.spot }],
      series: [{ name: "intact", v: g, color: HEX.intact, width: 1.8, fill: "bottom" }, { name: "underwater: bad debt", v: r, color: HEX.red, width: 1.8, fill: "bottom" }] });
    charts.health.setData({ t, series: [{ name: "health", v: F(S.health), color: HEX.health, width: 1.6 }] });
    const anyHl = S.hlUsd.some(x => N(x) > 0);
    charts.hl.setData({ t, caption: anyHl ? "" : "no hard liquidations in this run",
      series: [{ name: "hard-liq (liquidator repays debt, takes collateral; partial slices)", v: F(S.hlUsd), color: HEX.red, type: "bars" }] });
    // a band is one price interval [p_up · (A−1)/A, p_up]; rows run from the highest price down
    const bands = (R.bands || []).slice().sort((p, q) => p.n - q.n), A = N(R.inputs.A);
    let ratio = NaN;
    for (let i = 1; i < bands.length && !Number.isFinite(ratio); i++) if (bands[i].n === bands[i - 1].n + 1 && bands[i - 1].pu > 0) ratio = bands[i].pu / bands[i - 1].pu;
    if (!Number.isFinite(ratio)) ratio = A > 1 ? (A - 1) / A : NaN;
    charts.bands.setData({ t, bands: bands.map(b => ({ n: b.n, pHi: N(b.pu), pLo: N(b.pu) * ratio, lend: F(b.x), coll: F(b.y.map((y, i) => (N(y) || 0) * (N(S.target[i]) || 0))) })) });
  }

  async function run() {
    if (running) return;
    const token = specToken;
    running = true; lastErr = ""; errEl.hidden = true; paintPre();
    bar.set(0, "sizing the borrower");
    try {
      poll = setInterval(async () => {
        try { const p = await api.progress(); if (p.running && p.total) bar.set(p.done / p.total, `engine ${p.done.toLocaleString("en-US")} / ${p.total.toLocaleString("en-US")} steps`); else bar.set(0.02, "preparing"); } catch (_) { /* keep the last frame */ }
      }, 300);
      const R = await simulate(store);
      if (!alive || token !== specToken) { toast("the market changed while this ran: result dropped"); return; }
      store.update("results", s => { s.results.baddebt = R; });
    } catch (e) {
      lastErr = explainRunError(e); errEl.textContent = lastErr; errEl.hidden = false; toast(lastErr, "error");
    } finally {
      clearInterval(poll); running = false; bar.set(null); if (alive) { paintPre(); }
    }
  }

  paintPre(); paintResult();
  const closePops = () => tilesEl.querySelectorAll(".nl-bd-pop").forEach(p => { p.hidden = true; });
  document.addEventListener("click", closePops);
  let t = 0;
  const off = store.on(tag => {
    if (tag === "spec") { specToken = {}; paintPre(); paintResult(); return; }
    if (tag === "results") { paintResult(); paintPre(); return; }
    if (["scenario", "scenario.data", "oracle.data", "params", "venue", "oracle.sources", "oracle.script"].includes(tag)) { clearTimeout(t); t = setTimeout(paintPre, 60); }
  });
  return { destroy() { alive = false; off(); clearInterval(poll); clearTimeout(t); document.removeEventListener("click", closePops);
    Object.values(charts).forEach(c => c.destroy()); root.remove(); } };
}
