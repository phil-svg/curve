// The two simulators of the new-llamalend tab: the bad-debt run (routed
// engine on the server) and the S.L./D.L. sweep (wasm engine in the page).
// parts: baddebt | sldl. Every view renders from spec.results.*, so an
// imported bundle shows its results without re-running anything.
import { api } from "../core/api.js";
import { buildScenario, resolveBorrower, toRunParams, runSldl, smoothingSummary } from "../core/pipeline.js";
import { lineChart, heatTable, fmtNum, fmtUsd, fmtElapsed, fmtDateTime } from "../core/charts.js";
import { h, field, seg, button, card, progress, toast } from "./kit.js";

const PARTS = ["baddebt", "sldl"];
const HISTORY_MAX = 8;
// engine limits of the sweep (wasm/sldl_shared.js clampParams)
export const SLDL_LIMITS = { A: [2, 1000], fee: [0.001, 5], grid: [2, 16], bands: [1, 50], tail: [0.001, 50], loan: [10 / 1440, 14] };
const SLDL_MIN_ROWS = 50;          // pipeline.toSldlDataset refuses shorter histories
const BD_BANDS = [4, 50];          // Controller MIN_TICKS / MAX_TICKS, enforced by the server

// ---- small pure helpers ------------------------------------------------------------
const N = x => (x === null || x === undefined || x === "" ? NaN : +x);
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const sig = (x, n = 8) => (Number.isFinite(x) ? +(+x).toPrecision(n) : null);
const plain = x => (Number.isFinite(N(x)) ? String(+N(x).toPrecision(6)) : "–");
const pct = (x, d = 2) => (Number.isFinite(x) ? x.toFixed(d).replace("-", "−") + " %" : "–");
const sharePct = x => (x === 0 ? "0 %" : pct(x, Math.abs(x) < 1 ? 3 : 2));      // bad debt is often a sliver of the debt
const usd = x => (Number.isFinite(x) ? (x < 0 ? "−" + fmtUsd(-x) : fmtUsd(x)) : "–");
// whole numbers (fmtNum is for measurements: it prints 256 as "256.0")
const count = x => (!Number.isFinite(x) ? "–" : Math.abs(x) >= 1e6 ? fmtNum(x) : Math.round(x).toLocaleString("en-US"));
const lossPct = v => (!Number.isFinite(v) ? "–" : Math.abs(v) >= 10 ? v.toFixed(1) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3));
const day = ts => (Number.isFinite(ts) ? fmtDateTime(ts).slice(0, 10) : "–");
const fmtStep = s => (!Number.isFinite(s) ? "–" : s % 86400 === 0 ? s / 86400 + " d" : s % 3600 === 0 ? s / 3600 + " h" : s % 60 === 0 ? s / 60 + " min" : s + " s");
const fmtLoan = d => (!Number.isFinite(d) ? "–" : d >= 1 ? plain(d) + " d" : d * 24 >= 1 ? plain(d * 24) + " h" : plain(d * 1440) + " min");

export function nearestIndex(arr, x) {
  let best = -1, bd = Infinity;
  if (!Number.isFinite(x)) return -1;
  for (let i = 0; i < arr.length; i++) {
    const d = Math.abs(N(arr[i]) - x);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

// ---- bad debt: spec -> pre-flight ----------------------------------------------------
export function scenarioStats(sc) {
  const m = sc.market, t = sc.t;
  let lo = Infinity, at = 0;
  for (let i = 0; i < m.length; i++) if (m[i] < lo) { lo = m[i]; at = t[i]; }
  return { start: m[0], end: m[m.length - 1], low: lo, low_at_s: at, deepest_pct: (lo / m[0] - 1) * 100,
    duration_s: Number.isFinite(sc.duration) ? sc.duration : t[t.length - 1] - t[0], points: m.length };
}

// exactly what is missing when pipeline.buildScenario returns null
export function whyNoScenario(spec, rt) {
  const sc = spec.scenario;
  if (sc.mode === "draw") return "the drawn path needs at least two points with a positive price multiple: add points in the scenario editor";
  if (sc.mode === "linear") return "the linear scenario could not be built: check its drop % and duration";
  if (!rt.grid || !rt.market) return "no price history yet";
  if (rt.valid0 >= rt.grid.t.length)
    return rt.missing && rt.missing.length
      ? `the oracle script has no finite values because these sources are not loaded: ${rt.missing.join(", ")}`
      : "the oracle script evaluates to no finite values: fix the script or its sources";
  if ((sc.clips || []).some(c => c.t1 > c.t0))
    return "the clips cover fewer than two valid points of the loaded history: move or widen them (they may lie outside the loaded range)";
  return "no crash window could be cut from the price history";
}

// the inputs of a run as stored with its result: numbers and labels, never paths
export function inputsOf(spec, scenario) {
  const p = spec.params, b = spec.borrower, v = spec.venue, sc = spec.scenario;
  const st = scenario ? scenarioStats(scenario) : null;
  const ltv = N(b.ltv_pct);
  return {
    A: sig(N(p.A)), fee_pct: sig(N(p.fee_pct)),
    loan_discount_pct: sig(N(p.loan_discount_pct)), liquidation_discount_pct: sig(N(p.liquidation_discount_pct)),
    n_bands: sig(N(b.n_bands)), borrow_cap: sig(N(p.borrow_cap)),
    ma_exp_time: sig(N(p.ma_exp_time)),
    borrower: b.mode === "custom"
      ? { mode: "custom", collateral_usd: sig(N(b.collateral_usd)), debt_usd: sig(N(b.debt_usd)) }
      : { mode: "cap", ltv_pct: Number.isFinite(ltv) && ltv > 0 ? sig(ltv) : null },
    venue: { pool_type: String(v.pool_type || ""), tvl_usd: sig(N(v.tvl_usd)), A_raw: sig(N(v.A_raw)), ss_A: sig(N(v.ss_A)),
      n_coins: sig(N(v.n_coins)), real_state: !!v.state },
    scenario: st ? { label: scenario.label || sc.mode, mode: sc.mode, duration_s: sig(st.duration_s), deepest_pct: sig(st.deepest_pct),
      deepest_at_s: sig(st.low_at_s), start_price: sig(st.start), end_price: sig(st.end), points: st.points,
      lead_in_min: sig(Math.max(0, N(sc.lead_in_min) || 0)), tail_min: sig(Math.max(0, N(sc.tail_min) || 0)) } : null,
    oracle_mode: scenario && scenario.oracle && sc.oracle_mode === "recorded" ? "recorded" : "ema",
    // every EMA along the oracle chain as it was when this ran (per-part re-timing + the script's own)
    smoothing: smoothingSummary(spec).text, smoothing_sig: smoothingSummary(spec).sig,
  };
}

// what decides whether a stored result still belongs to the current inputs
// (the start price is left out: "latest" moves with every live read)
export function fingerprint(i, withScenario = true) {
  if (!i) return "";
  const v = i.venue || {}, b = i.borrower || {}, s = i.scenario;
  return JSON.stringify([i.A, i.fee_pct, i.loan_discount_pct, i.liquidation_discount_pct, i.n_bands, i.borrow_cap, i.ma_exp_time,
    i.oracle_mode, i.oracle_mode === "recorded" ? (i.smoothing_sig ?? null) : null, b.mode, b.ltv_pct ?? null, b.collateral_usd ?? null, b.debt_usd ?? null,
    v.pool_type, v.tvl_usd, v.A_raw, v.ss_A, v.n_coins, v.real_state,
    withScenario && s ? [s.label, Math.round(s.duration_s), Math.round(s.deepest_pct * 100), s.lead_in_min, s.tail_min] : null]);
}

export function preflight(spec, rt) {
  const p = spec.params, b = spec.borrower, v = spec.venue, sc = spec.scenario;
  const problems = [], warn = new Set();
  let scenario = null;
  try { scenario = rt.scenario || buildScenario(spec, rt); }
  catch (e) { problems.push("the scenario could not be built: " + String((e && e.message) || e)); }
  if (scenario && !(scenario.market && scenario.market.length >= 2 && scenario.market[0] > 0)) {
    problems.push("the scenario does not start at a positive price: set a start price in the scenario settings");
    scenario = null;
  }
  if (!scenario && !problems.length) problems.push(whyNoScenario(spec, rt));
  const inputs = inputsOf(spec, scenario), s = inputs.scenario, items = {};

  if (s) {
    items.scenario = `${s.label} · ${fmtElapsed(s.duration_s)} · deepest ${pct(s.deepest_pct)} at ${fmtElapsed(s.deepest_at_s)} · ` +
      `${fmtNum(s.start_price, 6)} → ${fmtNum(s.end_price, 6)}`;
    items.horizon = `lead-in ${plain(s.lead_in_min)} min + scenario ${fmtElapsed(s.duration_s)} + tail ${plain(s.tail_min)} min = ` +
      fmtElapsed(s.lead_in_min * 60 + s.duration_s + s.tail_min * 60);
  } else {
    items.scenario = "missing: " + problems[0];
    items.horizon = `lead-in ${plain(Math.max(0, N(sc.lead_in_min) || 0))} min + scenario + tail ${plain(Math.max(0, N(sc.tail_min) || 0))} min`;
    warn.add("scenario");
  }

  const nb = N(b.n_bands);
  if (b.mode === "custom") {
    const c = N(b.collateral_usd), d = N(b.debt_usd);
    items.borrower = `custom position: ${usd(c)} collateral, ${usd(d)} debt (LTV ${pct(d / c * 100, 1)}, capped at the market's max LTV), N = ${plain(nb)} bands`;
    if (!(c > 0) || !(d > 0)) { problems.push("the custom borrower needs collateral and debt above 0"); warn.add("borrower"); }
  } else {
    const ltv = N(b.ltv_pct);
    items.borrower = `one position carrying the whole borrow cap: ${usd(N(p.borrow_cap))} debt at ` +
      `${Number.isFinite(ltv) && ltv > 0 ? pct(ltv, 1) + " LTV (capped at the max)" : "the max LTV"}, N = ${plain(nb)} bands`;
    if (!(N(p.borrow_cap) > 0)) { problems.push("the borrow cap must be above 0: it is the borrower's debt"); warn.add("borrower"); }
  }
  if (!(Number.isInteger(nb) && nb >= BD_BANDS[0] && nb <= BD_BANDS[1])) {
    problems.push(`the bad-debt engine needs a whole number of ${BD_BANDS[0]} to ${BD_BANDS[1]} bands (N = ${plain(nb)})`); warn.add("borrower");
  }

  items.llamma = `A ${plain(p.A)} · fee ${plain(p.fee_pct)} % · loan discount ${plain(p.loan_discount_pct)} % · liquidation discount ${plain(p.liquidation_discount_pct)} %`;
  if (!(N(p.A) >= 2)) { problems.push("LLAMMA A must be at least 2"); warn.add("llamma"); }
  if (!(N(p.fee_pct) >= 0)) { problems.push("the AMM fee must be a number of 0 % or more"); warn.add("llamma"); }
  for (const [k, name] of [["loan_discount_pct", "loan discount"], ["liquidation_discount_pct", "liquidation discount"]])
    if (!(N(p[k]) >= 0 && N(p[k]) < 100)) { problems.push(`the ${name} must lie between 0 and 100 %`); warn.add("llamma"); }

  const crypto = v.pool_type === "cryptoswap";
  items.venue = `${v.pool_type || "?"} · TVL ${usd(N(v.tvl_usd))} · ${crypto ? "A_raw " + plain(v.A_raw) : "A " + plain(v.ss_A)} · ` +
    `${plain(Math.round(N(v.n_coins)) || 2)} coins · ${v.state ? "real pool state" : "balanced approximation"}`;
  if (!(N(v.tvl_usd) > 0)) { problems.push("the venue TVL must be above 0"); warn.add("venue"); }

  const ma = `ma_exp_time ${plain(p.ma_exp_time)} s`;
  if (inputs.oracle_mode === "recorded") items.oracle = `recorded: the script's own oracle is replayed over the clips, from ${fmtNum(scenario.oracle[0], 6)} · smoothing in force: ${inputs.smoothing}`;
  else if (sc.oracle_mode === "recorded")
    items.oracle = `engine EMA of the scenario price, ${ma} (recorded was asked for, but ` +
      (sc.mode === "history" ? "the recorded oracle has gaps over these clips)" : `a ${sc.mode} scenario has no recorded oracle)`);
  else items.oracle = `engine EMA of the scenario price, ${ma}`;

  return { scenario, inputs, problems, items, warn };
}

// ---- bad debt: /run response -> what is stored -------------------------------------------
export function badDebtKpis(S, borrower) {
  let peak = 0, peakAt = null, final = NaN, minH = Infinity, minHAt = null, hard = 0, hardRows = 0, slFinal = NaN, slPeak = -Infinity;
  for (let i = 0; i < S.t.length; i++) {
    const bd = N(S.badDebt[i]), hl = N(S.health[i]), hq = N(S.hardLiq[i]), sl = N(S.slLoss[i]);
    if (Number.isFinite(bd)) { final = bd; if (bd > peak) { peak = bd; peakAt = N(S.t[i]); } }
    if (Number.isFinite(hl) && hl < minH) { minH = hl; minHAt = N(S.t[i]); }
    if (hq > 0) { hard += hq; hardRows++; }
    if (Number.isFinite(sl)) { slFinal = sl; if (sl > slPeak) slPeak = sl; }
  }
  const debt = N(borrower.debt_usd), coll = N(borrower.collateral_usd), share = x => (debt > 0 && Number.isFinite(x) ? sig(x / debt * 100) : null);
  return {
    peak_bad_debt: sig(peak), peak_at_s: peakAt, final_bad_debt: sig(final),
    peak_bad_debt_pct: share(peak), final_bad_debt_pct: share(final),
    min_health: minH === Infinity ? null : sig(minH), min_health_at_s: minHAt,
    sl_user_loss: sig(slFinal), sl_user_loss_peak: slPeak === -Infinity ? null : sig(slPeak),
    sl_user_loss_pct_of_collateral: coll > 0 && Number.isFinite(slFinal) ? sig(slFinal / coll * 100) : null,
    hard_liq_usd: sig(hard), hard_liq_rows: hardRows, hard_liq_pct_of_debt: share(hard),
    collateral_usd: sig(coll), debt_usd: sig(debt), ltv_pct: sig(N(borrower.ltv_pct)),
  };
}

export function trimBadDebt(res, { inputs, borrower, at }) {
  const rows = res && Array.isArray(res.rows) ? res.rows : [];
  if (rows.length < 2) throw new Error("the engine returned no rows: check the scenario and the venue parameters, then run again");
  const orc = (res && res.oracle) || {};
  const S = { t: [], badDebt: [], health: [], target: [], venue: [], oracle: [], slLoss: [], hardLiq: [] };
  let lastO = null, prevT = -Infinity;
  rows.forEach((r, i) => {
    let t = N(r.elapsed_s);
    if (!Number.isFinite(t)) t = Number.isFinite(N(r.timestamp)) ? N(r.timestamp) - N(rows[0].timestamp) : i;
    if (!(t > prevT)) t = prevT + 1e-3;               // the charts bisect on t: keep it strictly increasing
    prevT = t;
    const o = N(orc[String(r.blockNumber)]);
    if (Number.isFinite(o)) lastO = o;                // sparse: carry the last known value forward
    S.t.push(sig(t));
    S.badDebt.push(sig(N(r.badDebt)));
    // a fully repaid position reports a placeholder health of 0: not a reading
    S.health.push(N(r.debtUsd) === 0 ? null : sig(N(r.health)));
    S.target.push(sig(N(r.target_spot)));
    S.venue.push(sig(N(r.pool_crv_spot)));
    S.oracle.push(lastO === null ? null : sig(lastO));
    S.slLoss.push(sig(N(r.slUserLoss)));
    S.hardLiq.push(sig(N(r.hardLiqUsd)));
  });
  const sb = (res && res.borrower) || {}, pick = (a, b) => (Number.isFinite(N(a)) ? N(a) : N(b));
  const bw = {
    collateral_usd: sig(pick(sb.collateral_usd, borrower.collateral_usd)), debt_usd: sig(pick(sb.debt_usd, borrower.debt_usd)),
    ltv_pct: sig(pick(sb.ltv_pct, N(borrower.debt_usd) / N(borrower.collateral_usd) * 100)),
    max_ltv_pct: sig(pick(sb.max_ltv_pct, borrower.max_ltv_pct)), collateral_tokens: sig(N(sb.crv)),
    n1: sig(N(sb.n1)), n2: sig(N(sb.n2)), N: sig(pick(sb.N, inputs.n_bands)), active_band: sig(N(sb.active_band)),
    clamped: !!(sb.clamped || borrower.clamped),
  };
  const sl = res.soft_liq && res.soft_liq.totals ? res.soft_liq.totals : null, keep = k => sig(N(sl[k]));
  const sc = inputs.scenario || {};
  return {
    at, label: `${sc.label || "scenario"} · A ${plain(inputs.A)} · fee ${plain(inputs.fee_pct)} % · discounts ${plain(inputs.loan_discount_pct)} / ${plain(inputs.liquidation_discount_pct)} %`,
    inputs, borrower: bw, kpis: badDebtKpis(S, bw), series: S,
    soft_liq: sl ? { liq_usd: keep("liq_usd"), deliq_usd: keep("deliq_usd"), liq_n: keep("liq_n"), deliq_n: keep("deliq_n"),
      liq_pnl: keep("liq_pnl"), deliq_pnl: keep("deliq_pnl") } : null,
    ext_arb_usd: res.ext_arb ? sig(N(res.ext_arb.total_usd)) : null,
    venue_note: res.venue_note ? String(res.venue_note) : "",
    timing: { total_s: sig(N(res.timing && res.timing.total_s), 4) },
  };
}

export function historyEntry(r) {
  const i = r.inputs || {}, k = r.kpis || {}, s = i.scenario || {}, v = i.venue || {};
  return { at: r.at, label: s.label || "", deepest_pct: s.deepest_pct ?? null, duration_s: s.duration_s ?? null,
    A: i.A, fee_pct: i.fee_pct, loan_discount_pct: i.loan_discount_pct, liquidation_discount_pct: i.liquidation_discount_pct,
    n_bands: i.n_bands, pool_type: v.pool_type, tvl_usd: v.tvl_usd, oracle_mode: i.oracle_mode, debt_usd: k.debt_usd,
    peak_bad_debt: k.peak_bad_debt, peak_bad_debt_pct: k.peak_bad_debt_pct, final_bad_debt: k.final_bad_debt, min_health: k.min_health };
}

export function explainRunError(e) {
  const m = String((e && e.message) || e);
  if (m === "routed engine failed")
    return "routed engine failed: check the venue parameters (pool type, TVL, amplification, number of coins); the engine could not run this venue";
  if (e instanceof TypeError || /failed to fetch|networkerror|load failed/i.test(m))
    return `the simulation server did not answer (${m}): check that the local server is running, then run again`;
  if (/^HTTP \d+$/.test(m)) return `the server answered ${m}: see the server log, then run again`;
  return m;
}

const cumulative = a => { let s = 0; return (a || []).map(x => (s += N(x) > 0 ? N(x) : 0)); };
// stored series keep gaps as null (JSON-safe); the charts want NaN there: on their dense path a null would be drawn as 0
const gaps = a => (Array.isArray(a) ? a.map(N) : []);

// ---- S.L./D.L.: pure helpers -----------------------------------------------------------------
export function centreOn(params) {
  const A = N(params.A), fee = N(params.fee_pct), out = {};
  if (Number.isFinite(A) && A > 0) {
    out.a_min = clamp(Math.round(A * 0.5), ...SLDL_LIMITS.A);
    out.a_max = clamp(Math.round(A * 1.5), ...SLDL_LIMITS.A);
  }
  if (Number.isFinite(fee) && fee > 0) {
    out.fee_min = clamp(+(fee * 0.25).toFixed(4), ...SLDL_LIMITS.fee);
    out.fee_max = clamp(+(fee * 3).toFixed(4), ...SLDL_LIMITS.fee);
  }
  return out;
}

// the controls as the engine will read them (same clamps, same linear grids)
export function sldlNormalised(s) {
  const c = (x, dflt, [lo, hi], int) => { let v = N(x); if (!Number.isFinite(v)) v = dflt; v = clamp(v, lo, hi); return int ? Math.round(v) : v; };
  const a0 = c(s.a_min, 100, SLDL_LIMITS.A, true), a1 = c(s.a_max, 180, SLDL_LIMITS.A, true);
  const f0 = c(s.fee_min, 0.05, SLDL_LIMITS.fee), f1 = c(s.fee_max, 0.5, SLDL_LIMITS.fee);
  return { a_min: Math.min(a0, a1), a_max: Math.max(a0, a1), fee_min: Math.min(f0, f1), fee_max: Math.max(f0, f1),
    grid: c(s.grid, 10, SLDL_LIMITS.grid, true), bands: c(s.bands, 4, SLDL_LIMITS.bands, true),
    tail_pct: c(s.tail_pct, 0.05, SLDL_LIMITS.tail), loan_days: c(s.loan_days, 80 / 1440, SLDL_LIMITS.loan),
    swapped: a0 > a1 || f0 > f1 };
}

export function sldlEstimate(sldl, rt) {
  const s = sldlNormalised(sldl);
  const distinct = (lo, hi, dp) => new Set(Array.from({ length: s.grid }, (_, i) => (lo + (hi - lo) * i / (s.grid - 1)).toFixed(dp))).size;
  const nA = distinct(s.a_min, s.a_max, 0), nFee = distinct(s.fee_min, s.fee_max, 4);
  const has = !!(rt && rt.grid && rt.oracle), tt = has ? rt.grid.t : null;
  // the sweep gets one candle per nominal grid step (pipeline.toSldlDataset), however many points the grid holds in between
  const step = has ? rt.grid.step : NaN, rows = has && tt.length > (rt.valid0 || 0) ? Math.max(0, Math.floor((tt[tt.length - 1] - tt[rt.valid0 || 0]) / step)) : 0;
  const loanRows = has ? Math.max(2, Math.round(s.loan_days * 86400 / step)) : NaN;
  return { s, nA, nFee, cells: nA * nFee, has, rows, step, loanRows, loans: has ? Math.max(0, rows - loanRows) : 0,
    from: rows ? tt[rt.valid0 || 0] : NaN, to: rows ? tt[tt.length - 1] : NaN };
}

export function sldlBlockers(spec, rt, isolated) {
  const out = [], e = sldlEstimate(spec.sldl, rt);
  if (!e.has) out.push("no price history yet: the sweep replays loans over it");
  else if (e.rows < SLDL_MIN_ROWS) out.push(`not enough history on the grid: ${e.rows} rows, the sweep needs at least ${SLDL_MIN_ROWS}`);
  else if (e.loans < 1) out.push("the loan duration is longer than the loaded history: shorten it or load more history");
  if (!isolated) out.push("this context is not cross-origin isolated, so the wasm engine cannot start (it needs SharedArrayBuffer): open the tool from its own server, not inside another page");
  return out;
}

export function sldlInputs(spec, rt) {
  const e = sldlEstimate(spec.sldl, rt), s = e.s;
  return { a_min: s.a_min, a_max: s.a_max, fee_min: sig(s.fee_min), fee_max: sig(s.fee_max), grid: s.grid, bands: s.bands,
    tail_pct: sig(s.tail_pct), loan_days: sig(s.loan_days), ma_exp_time: sig(N(spec.params.ma_exp_time) || 866),
    market_A: sig(N(spec.params.A)), market_fee_pct: sig(N(spec.params.fee_pct)),
    smoothing: smoothingSummary(spec).text, smoothing_sig: smoothingSummary(spec).sig,
    dataset: { rows: e.rows, from: sig(e.from, 12), to: sig(e.to, 12), step_s: sig(e.step), collateral: String((spec.collateral && spec.collateral.symbol) || "") } };
}

export function trimSldl(res, { inputs, at }) {
  if (!res || !Array.isArray(res.cells) || !res.cells.length)
    throw new Error("the sweep returned no cells: check the A and fee ranges, then run again");
  const cells = res.cells.map(c => ({ A: sig(N(c.A)), fee_pct: sig(N(c.fee_pct)), loss_pct: sig(N(c.loss_pct)), max_pct: sig(N(c.max_pct)),
    n_sims: Number.isFinite(N(c.n_sims)) ? N(c.n_sims) : null }));
  const uniq = k => [...new Set(cells.map(c => c[k]).filter(Number.isFinite))].sort((a, b) => a - b);
  const g = res.grid || {}, fc = res.fee_curve, cfg = res.config || {};
  return {
    at, label: `A ${inputs.a_min}–${inputs.a_max} × fee ${plain(inputs.fee_min)}–${plain(inputs.fee_max)} %, ${inputs.grid} pts`, inputs,
    grid: { A: Array.isArray(g.A) && g.A.length ? g.A.map(x => sig(N(x))) : uniq("A"),
      fee_pct: Array.isArray(g.fee_pct) && g.fee_pct.length ? g.fee_pct.map(x => sig(N(x))) : uniq("fee_pct") },
    cells,
    fee_curve: fc && Array.isArray(fc.fee_pct) && Array.isArray(fc.avg_loss_pct)
      ? { A: sig(N(fc.A)), loan_days: sig(N(fc.loan_days)), kind: String(fc.kind || ""), fee_pct: fc.fee_pct.map(x => sig(N(x))),
        avg_loss_pct: fc.avg_loss_pct.map(x => sig(N(x))) } : null,
    n_all: Number.isFinite(N(cfg.n_all)) ? N(cfg.n_all) : null, n_top: Number.isFinite(N(cfg.n_top)) ? N(cfg.n_top) : null,
    runtime_s: sig(N(res.runtime_s), 5),
  };
}

// rows = A ascending, columns = fee ascending; cells land on their nearest grid node
export function sldlMatrix(result) {
  const asc = a => [...new Set((a || []).map(N).filter(Number.isFinite))].sort((x, y) => x - y);
  const cells = Array.isArray(result.cells) ? result.cells : [];
  let A = asc(result.grid && result.grid.A), F = asc(result.grid && result.grid.fee_pct);
  if (!A.length) A = asc(cells.map(c => c.A));
  if (!F.length) F = asc(cells.map(c => c.fee_pct));
  const blank = () => A.map(() => F.map(() => NaN));
  const loss = blank(), max = blank(), at = A.map(() => F.map(() => null));
  let best = null, sims = 0;
  for (const c of cells) {
    const r = nearestIndex(A, N(c.A)), k = nearestIndex(F, N(c.fee_pct));
    if (r < 0 || k < 0) continue;
    loss[r][k] = N(c.loss_pct); max[r][k] = N(c.max_pct); at[r][k] = c;
    sims += N(c.n_sims) > 0 ? N(c.n_sims) : 0;
    if (Number.isFinite(N(c.loss_pct)) && (!best || N(c.loss_pct) < N(best.loss_pct))) best = c;
  }
  return { A, F, loss, max, at, best, sims };
}

export function marketCell(m, params) {
  const pA = N(params.A), pF = N(params.fee_pct), r = nearestIndex(m.A, pA), c = nearestIndex(m.F, pF);
  if (r < 0 || c < 0) return null;
  const inside = pA >= m.A[0] && pA <= m.A[m.A.length - 1] && pF >= m.F[0] && pF <= m.F[m.F.length - 1];
  return { r, c, inside, cell: m.at[r][c], A: m.A[r], fee_pct: m.F[c] };
}

export function feeCurvePoints(fc) {
  if (!fc || !Array.isArray(fc.fee_pct) || !Array.isArray(fc.avg_loss_pct)) return null;
  const pts = fc.fee_pct.map((x, i) => [N(x), N(fc.avg_loss_pct[i])]).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]))
    .sort((a, b) => a[0] - b[0]).filter((p, i, a) => !i || p[0] > a[i - 1][0]);
  return pts.length >= 2 ? { x: pts.map(p => p[0]), y: pts.map(p => p[1]) } : null;
}

// ---- shared DOM pieces -------------------------------------------------------------------------
function makeTile(label, hint) {
  const b = h("b"), sub = h("small", { class: "nl-runs-kpi-sub" });
  const el = h("div", { class: "nl-kpi", title: hint || null }, b, h("span", {}, label), sub);
  return { el, set(value, subText = "", state = "") {
    b.textContent = value; sub.textContent = subText || " ";
    el.className = "nl-kpi" + (state ? " nl-" + state : "");
  } };
}
function chartBox(title, extra, wide) {
  const hostEl = h("div"), titleEl = h("span", { class: "nl-sub" }, title);
  return { el: h("div", { class: "nl-runs-chart" + (wide ? " nl-runs-chart-wide" : "") }, h("div", { class: "nl-runs-chart-h" }, titleEl, extra || null), hostEl), hostEl, titleEl };
}
function shell(title, cls, bare, kids) {
  const el = bare ? h("div", { class: "nl-runs-bare" }, kids) : card(title, ...kids);
  el.classList.add("nl-runs-part", cls);
  return el;
}
function paintProgress(bar, b) {
  if (!b) { bar.classList.remove("nl-runs-indet"); bar.set(null); return; }
  const secs = b.since ? Math.max(0, Math.round((Date.now() - b.since) / 1000)) : null;
  bar.classList.toggle("nl-runs-indet", !b.total);
  bar.set(b.total ? b.done / b.total : 0,
    `${b.label || "working"}${b.total ? ` · ${count(b.done)} / ${count(b.total)}` : ""}${secs === null ? "" : ` · ${fmtElapsed(secs)}`}`);
}

// ---- part: bad debt ------------------------------------------------------------------------------
const PRE_ITEMS = [["scenario", "Scenario"], ["horizon", "Horizon"], ["borrower", "Borrower"], ["llamma", "LLAMMA"], ["venue", "Liquidation venue"], ["oracle", "Oracle"]];
const USD_MODES = [
  { value: "badDebt", label: "bad debt", hint: "debt not covered by the position, marked at the venue spot" },
  { value: "slLoss", label: "soft-liq loss", hint: "what soft and de-liquidation cost the borrower so far, marked at the venue spot" },
  { value: "hardLiq", label: "hard-liq, cumulative", hint: "debt repaid by hard liquidations, running total" },
];

function badDebtPart(store, opts) {
  const height = +opts.height > 0 ? +opts.height : 0;
  let destroyed = false, active = null, lastErr = "", usdMode = "badDebt", pf = null;

  // pre-flight
  const pre = {};
  for (const [k, label] of PRE_ITEMS) {
    const v = h("span", { class: "nl-runs-pre-v" });
    pre[k] = { v, el: h("div", { class: "nl-runs-pre-item" }, h("span", { class: "nl-runs-pre-k" }, label), v) };
  }
  const preEl = h("div", { class: "nl-runs-pre" }, PRE_ITEMS.map(([k]) => pre[k].el));

  // run row
  const runBtn = button("Run bad-debt simulation", { kind: "primary", onClick: () => run() });
  const reason = h("span", { class: "nl-runs-reason nl-note", role: "status" });
  const bar = progress();
  const errEl = h("div", { class: "nl-err nl-runs-error", role: "alert", hidden: true });

  // result
  const stateBadge = h("span", { class: "nl-badge" }), metaText = h("span", { class: "nl-note" });
  const staleBadge = h("span", { class: "nl-badge nl-warn", hidden: true,
    title: "parameters, borrower, venue or scenario differ from the ones this result was computed with: run again to refresh it" }, "inputs changed since this run");
  const T = {
    peak: makeTile("Peak bad debt", "highest debt not covered by the position (debt − band crvUSD − collateral at the venue spot) over the run"),
    final: makeTile("Final bad debt", "bad debt in the last row of the run, after the tail"),
    share: makeTile("Bad debt / debt", "peak bad debt as a share of the borrower's opening debt"),
    health: makeTile("Lowest health", "Controller.health(user, full = true); below 0 the position can be hard-liquidated. Rows after a full repayment are left out"),
    sl: makeTile("Soft-liq user loss", "value the borrower lost to soft and de-liquidation trades so far, marked at the venue spot, last row"),
    hard: makeTile("Hard-liq volume", "debt repaid by hard liquidations, summed over the run"),
    coll: makeTile("Borrower collateral", "opening collateral of the single abstracted position"),
    debt: makeTile("Borrower debt", "opening debt of the single abstracted position"),
    ltv: makeTile("LTV", "opening debt / collateral; the bands the position was placed in"),
  };
  const kpis = h("div", { class: "nl-kpis nl-runs-kpis" }, Object.values(T).map(t => t.el));

  const usdSeg = seg(USD_MODES, usdMode, v => { usdMode = v; paintUsdChart(); });
  const priceBox = chartBox("Price: scenario target, venue spot, oracle", null, true), usdBox = chartBox("Bad debt, USD", usdSeg),
        healthBox = chartBox("Health, liquidatable below 0");
  const priceChart = lineChart(priceBox.hostEl, { height: height || 220, xMode: "elapsed", empty: "no rows in this result" });
  const usdChart = lineChart(usdBox.hostEl, { height: height || 190, xMode: "elapsed", zeroBase: true, yFmt: v => usd(v), empty: "no rows in this result" });
  const healthChart = lineChart(healthBox.hostEl, { height: height || 190, xMode: "elapsed", empty: "no rows in this result",
    yFmt: v => (Number.isFinite(v) ? (v * 100).toFixed(Math.abs(v) < 0.1 ? 2 : 1) + "%" : "–") });
  const venueNote = h("div", { class: "nl-note nl-runs-venue-note" });
  const resultEl = h("div", { class: "nl-runs-result", hidden: true },
    h("div", { class: "nl-runs-meta" }, stateBadge, metaText, staleBadge), kpis,
    h("div", { class: "nl-runs-charts" }, priceBox.el, usdBox.el, healthBox.el), venueNote);
  const emptyEl = h("div", { class: "nl-note nl-runs-empty" },
    "No run yet. The run replays the scenario against one position that carries the market's debt, through the liquidation venue, and reports the debt left uncovered.");

  // history
  const histBody = h("tbody");
  const clearBtn = button("clear history", { small: true, kind: "ghost", onClick: () => store.update("results", s => { s.results.baddebt_history = []; }) });
  const histEl = h("div", { class: "nl-runs-history", hidden: true },
    h("div", { class: "nl-spread" }, h("span", { class: "nl-sub" }, `Run history, newest first (last ${HISTORY_MAX})`), clearBtn),
    h("div", { class: "nl-runs-tablewrap" }, h("table", { class: "nl-runs-table" },
      h("thead", {}, h("tr", {}, ["Run at (UTC)", "Scenario", "A", "Fee %", "Loan / liq. disc. %", "N", "Venue TVL", "Oracle", "Peak bad debt", "% of debt", "Final", "Lowest health"]
        .map((c, i) => h("th", { class: i < 2 ? "nl-runs-left" : null }, c)))), histBody)));

  const el = shell("Bad-debt simulation", "nl-runs-baddebt", opts.bare,
    [preEl, h("div", { class: "nl-runs-actions" }, runBtn, reason), bar, errEl, emptyEl, resultEl, histEl]);

  // ---- painting
  function paintPre() {
    pf = preflight(store.spec, store.rt);
    for (const [k] of PRE_ITEMS) { pre[k].v.textContent = pf.items[k]; pre[k].el.classList.toggle("nl-warn", pf.warn.has(k)); }
    paintBusy(); paintStale();
  }
  function paintBusy() {
    const b = store.rt.busy.baddebt || null, blocked = !pf || pf.problems.length > 0;
    paintProgress(bar, b);
    runBtn.disabled = !!b || blocked;
    runBtn.textContent = b ? "Running…" : "Run bad-debt simulation";
    reason.classList.toggle("nl-warn", !b && blocked);
    reason.textContent = b ? "" : blocked ? pf.problems.join(" · ")
      : "Runs the routed engine on the server. The result is stored with the market and travels with an export that includes results.";
    errEl.hidden = !lastErr || !!b; errEl.textContent = lastErr;
  }
  function paintStale() {
    const r = store.spec.results && store.spec.results.baddebt;
    if (!r || !r.inputs || !pf) { staleBadge.hidden = true; return; }
    const withSc = !!(pf.inputs.scenario && r.inputs.scenario);
    staleBadge.hidden = fingerprint(pf.inputs, withSc) === fingerprint(r.inputs, withSc);
  }
  function paintUsdChart() {
    const r = store.spec.results && store.spec.results.baddebt, S = (r && r.series) || {}, t = gaps(S.t);
    const series = usdMode === "slLoss" ? [{ name: "soft-liq user loss", v: gaps(S.slLoss), color: "--nl-warn", area: true }]
      : usdMode === "hardLiq" ? [{ name: "hard-liquidated debt, cumulative", v: cumulative(S.hardLiq), color: "--nl-c5", step: true }]
      : [{ name: "bad debt", v: gaps(S.badDebt), color: "--nl-bad", area: true }];
    usdBox.titleEl.textContent = usdMode === "slLoss" ? "Soft-liquidation user loss, USD" : usdMode === "hardLiq" ? "Hard-liquidated debt, USD" : "Bad debt, USD";
    usdChart.setData({ t, series });
  }
  function paintResult() {
    const r = store.spec.results && store.spec.results.baddebt, has = !!(r && r.kpis);
    resultEl.hidden = !has; emptyEl.hidden = has;
    if (has) {
      const k = r.kpis, b = r.borrower || {}, S = r.series || {}, i = r.inputs || {}, sc = i.scenario || {}, t = gaps(S.t);
      const peak = N(k.peak_bad_debt), fin = N(k.final_bad_debt), bdState = !(peak > 0) ? "good" : fin > 0 ? "bad" : "warn";
      stateBadge.className = "nl-badge nl-" + bdState;
      stateBadge.textContent = !(peak > 0) ? "no bad debt" : fin > 0 ? "bad debt " + usd(fin) : "bad debt healed";
      metaText.textContent = [`run ${Number.isFinite(N(r.at)) ? fmtDateTime(N(r.at)) + " UTC" : "at an unknown time"}`, r.label,
        Number.isFinite(N(r.timing && r.timing.total_s)) ? `engine ${plain(N(r.timing.total_s).toFixed(1))} s` : "",
        t.length ? `${count(t.length)} rows over ${fmtElapsed(N(t[t.length - 1]))}` : ""].filter(Boolean).join(" · ");
      T.peak.set(usd(peak), peak > 0 && Number.isFinite(N(k.peak_at_s)) ? "at " + fmtElapsed(N(k.peak_at_s)) : "never above 0", bdState);
      T.final.set(usd(fin), peak > 0 && !(fin > 0) ? "healed by the end" : "last row", fin > 0 ? "bad" : "good");
      T.share.set(sharePct(N(k.peak_bad_debt_pct)), "peak; final " + sharePct(N(k.final_bad_debt_pct)), bdState);
      const mh = N(k.min_health);
      T.health.set(Number.isFinite(mh) ? pct(mh * 100) : "–", Number.isFinite(N(k.min_health_at_s)) ? "at " + fmtElapsed(N(k.min_health_at_s)) : "", !Number.isFinite(mh) ? "" : mh < 0 ? "bad" : "good");
      const sl = N(k.sl_user_loss);
      T.sl.set(usd(sl), Number.isFinite(N(k.sl_user_loss_pct_of_collateral)) ? pct(N(k.sl_user_loss_pct_of_collateral), 3) + " of collateral" : "", !Number.isFinite(sl) ? "" : sl > 0 ? "warn" : "good");
      const hq = N(k.hard_liq_usd);
      T.hard.set(usd(hq), hq > 0 ? `${pct(N(k.hard_liq_pct_of_debt), 1)} of debt, ${k.hard_liq_rows} rows` : "none", hq > 0 ? "warn" : "good");
      T.coll.set(usd(N(b.collateral_usd)), Number.isFinite(N(b.collateral_tokens)) ? fmtNum(N(b.collateral_tokens)) + " tokens" : "");
      T.debt.set(usd(N(b.debt_usd)), i.borrower && i.borrower.mode === "custom" ? "custom position" : "the borrow cap");
      T.ltv.set(pct(N(b.ltv_pct), 2), [Number.isFinite(N(b.n1)) && Number.isFinite(N(b.n2)) ? `bands ${b.n1} to ${b.n2}` : "",
        Number.isFinite(N(b.max_ltv_pct)) ? `max ${pct(N(b.max_ltv_pct), 2)}` : "", b.clamped ? "debt clamped" : ""].filter(Boolean).join(" · "), b.clamped ? "warn" : "");

      const lead = N(sc.lead_in_min) * 60, end = lead + N(sc.duration_s), last = N(t[t.length - 1]);
      const vlines = [[lead, "scenario starts"], [end, "tail"]].filter(([x]) => x > 0 && x < last).map(([x, label]) => ({ x, label }));
      priceChart.setData({ t, vlines, series: [
        { name: "scenario target", v: gaps(S.target), color: "--nl-c1" },
        { name: "venue spot", v: gaps(S.venue), color: "--nl-c2" },
        { name: i.oracle_mode === "recorded" ? "oracle (recorded)" : "oracle (engine EMA)", v: gaps(S.oracle), color: "--nl-c4", dash: [5, 3] }] });
      paintUsdChart();
      healthChart.setData({ t, series: [
        { name: "health", v: gaps(S.health), color: "--nl-c1" },
        { name: "liquidation threshold", v: t.map(() => 0), color: "--nl-bad", dash: [4, 4], width: 1 }] });
      const notes = [r.venue_note ? "Venue: " + r.venue_note : ""];
      if (r.soft_liq) notes.push(`Soft-liq flow: ${usd(N(r.soft_liq.liq_usd))} liquidated in ${plain(r.soft_liq.liq_n)} trades, ${usd(N(r.soft_liq.deliq_usd))} de-liquidated in ${plain(r.soft_liq.deliq_n)}`);
      if (Number.isFinite(N(r.ext_arb_usd))) notes.push(`external arbitrage through the venue: ${usd(N(r.ext_arb_usd))}`);
      venueNote.textContent = notes.filter(Boolean).join(" · ");
      venueNote.hidden = !venueNote.textContent;
    }
    paintHistory(); paintStale();
  }
  function paintHistory() {
    const res = store.spec.results || {}, list = Array.isArray(res.baddebt_history) ? res.baddebt_history : [], cur = res.baddebt && res.baddebt.at;
    histEl.hidden = !list.length;
    histBody.replaceChildren(...list.map(e => {
      const peak = N(e.peak_bad_debt), fin = N(e.final_bad_debt), mh = N(e.min_health);
      const td = (txt, cls) => h("td", { class: cls || null }, txt);
      return h("tr", { class: e.at === cur ? "nl-runs-current" : null, title: e.at === cur ? "the result shown above" : null },
        td(Number.isFinite(N(e.at)) ? fmtDateTime(N(e.at)) : "–", "nl-runs-left"),
        td(`${e.label || "–"}${Number.isFinite(N(e.deepest_pct)) ? `, ${pct(N(e.deepest_pct), 1)} in ${fmtElapsed(N(e.duration_s))}` : ""}`, "nl-runs-left nl-runs-wrap"),
        td(plain(e.A)), td(plain(e.fee_pct)), td(`${plain(e.loan_discount_pct)} / ${plain(e.liquidation_discount_pct)}`), td(plain(e.n_bands)),
        td(usd(N(e.tvl_usd))), td(e.oracle_mode === "recorded" ? "recorded" : "EMA"),
        td(usd(peak), peak > 0 ? "nl-bad" : "nl-good"), td(sharePct(N(e.peak_bad_debt_pct))),
        td(usd(fin), fin > 0 ? "nl-bad" : "nl-good"), td(Number.isFinite(mh) ? pct(mh * 100) : "–", mh < 0 ? "nl-bad" : null));
    }));
  }

  // ---- the run: only this instance polls /progress; every instance mirrors rt.busy
  async function run() {
    if (active || store.rt.busy.baddebt || !pf || pf.problems.length) return;
    const live = store.spec, scenario = pf.scenario;
    // the sections the run reads, frozen now: edits made while it computes must not leak into it
    const spec = { ...live, params: { ...live.params }, borrower: { ...live.borrower }, venue: { ...live.venue }, scenario: { ...live.scenario } };
    const inputs = inputsOf(spec, scenario), me = active = { timer: 0, done: false, since: Date.now(), label: "sizing the borrower position" };
    const publish = (done, total) => store.setBusy("baddebt", { label: me.label, done, total, since: me.since, token: me });
    lastErr = "";
    publish(0, 0);
    let inflight = false;
    me.timer = setInterval(async () => {
      if (inflight) return;
      inflight = true;
      try {
        const p = await api.progress();
        if (!me.done && me.timer && p && p.running && p.total > 0) { me.label = "engine steps"; publish(+p.done || 0, +p.total); }
      } catch (_) { /* the run's own request reports a dead server */ }
      finally { inflight = false; }
    }, 300);
    try {
      const oracleSeed = scenario.oracle && spec.scenario.oracle_mode === "recorded" ? scenario.oracle[0] : scenario.market[0];
      const borrower = await resolveBorrower(spec, scenario.market[0], oracleSeed);
      if (!me.done && me.timer) { me.label = "building the position and the venue"; publish(0, 0); }
      const res = await api.run(toRunParams(spec, scenario, borrower));
      if (store.spec !== live) throw new Error("the market was replaced while the run was computing, so its result was discarded: run again");
      const trimmed = trimBadDebt(res, { inputs, borrower, at: Math.floor(Date.now() / 1000) });
      store.update("results", s => {
        s.results.baddebt = trimmed;
        s.results.baddebt_history = [historyEntry(trimmed), ...(Array.isArray(s.results.baddebt_history) ? s.results.baddebt_history : [])].slice(0, HISTORY_MAX);
      });
      toast(trimmed.kpis.peak_bad_debt > 0 ? `bad-debt run finished: peak ${usd(trimmed.kpis.peak_bad_debt)}` : "bad-debt run finished: no bad debt",
        trimmed.kpis.peak_bad_debt > 0 ? "info" : "good");
    } catch (e) {
      lastErr = explainRunError(e);
      toast(lastErr, "error");
    } finally {
      me.done = true; clearInterval(me.timer); me.timer = 0; active = null;
      const b = store.rt.busy.baddebt;
      if (b && b.token === me) store.setBusy("baddebt", null);
      if (!destroyed) paintBusy();
    }
  }

  paintPre(); paintResult();
  return {
    el,
    update(tags) {
      if (destroyed) return;
      if (tags.some(t => t !== "busy" && t !== "results" && t !== "sldl")) paintPre();
      if (tags.includes("results")) paintResult();
      if (tags.includes("busy")) paintBusy();
    },
    destroy() {
      destroyed = true;
      if (active) {                                       // the request cannot be aborted: stop polling, stay honest
        clearInterval(active.timer); active.timer = 0;
        store.setBusy("baddebt", { label: "bad-debt simulation still computing on the server", done: 0, total: 0, since: active.since, token: active });
      }
      priceChart.destroy(); usdChart.destroy(); healthChart.destroy();
    },
  };
}

// ---- part: S.L./D.L. sweep ---------------------------------------------------------------------------
const SLDL_FIELDS = [
  { key: "a_min", label: "A min", lim: SLDL_LIMITS.A, int: true, hint: "lowest LLAMMA amplification A of the sweep; the A axis is linear, rounded to whole numbers (engine limits 2 to 1000)" },
  { key: "a_max", label: "A max", lim: SLDL_LIMITS.A, int: true, hint: "highest LLAMMA amplification A of the sweep (engine limits 2 to 1000)" },
  { key: "fee_min", label: "Fee min", unit: "%", lim: SLDL_LIMITS.fee, hint: "lowest AMM fee of the sweep in percent; the fee axis is linear (engine limits 0.001 to 5 %)" },
  { key: "fee_max", label: "Fee max", unit: "%", lim: SLDL_LIMITS.fee, hint: "highest AMM fee of the sweep in percent (engine limits 0.001 to 5 %)" },
  { key: "grid", label: "Grid points per axis", lim: SLDL_LIMITS.grid, int: true, hint: "points on each axis, 2 to 16; the sweep runs grid × grid cells" },
  { key: "bands", label: "Bands N", lim: SLDL_LIMITS.bands, int: true, hint: "number of bands every simulated loan spans, 1 to 50" },
  { key: "tail_pct", label: "Worst tail", unit: "%", lim: SLDL_LIMITS.tail, hint: "the headline loss of a cell is the mean over this worst share of all loan starts, 0.001 to 50 %" },
  { key: "loan_days", label: "Loan duration", unit: "d", lim: SLDL_LIMITS.loan, hint: "length of every simulated loan in days: 10 minutes (0.00694 d) to 14 d" },
];

function sldlPart(store, opts) {
  const height = +opts.height > 0 ? +opts.height : 0;
  const isolated = !!globalThis.crossOriginIsolated && typeof SharedArrayBuffer !== "undefined";
  let destroyed = false, active = null, lastErr = "", blockers = [];

  // controls
  const fields = {};
  for (const f of SLDL_FIELDS) {
    const el = field({ label: f.label, hint: f.hint, unit: f.unit, type: "number", min: f.lim[0], max: f.lim[1], value: plain(store.spec.sldl[f.key]),
      onChange: x => {
        const v = f.int ? Math.round(x) : +x.toPrecision(6);
        store.update("sldl", s => { s.sldl[f.key] = v; });
        el.input.value = plain(v);                   // committed: show the value as stored (rounded, clamped)
      } });
    fields[f.key] = el;
  }
  const centreBtn = button("Centre on this market", { small: true, onClick: () => {
    const c = centreOn(store.spec.params);
    if (!Object.keys(c).length) { toast("set the market's A and fee first: there is nothing to centre on", "error"); return; }
    store.update("sldl", s => { Object.assign(s.sldl, c); });
  } });
  const estimate = h("div", { class: "nl-note nl-runs-estimate" });

  // run row
  const runBtn = button("Run S.L./D.L. sweep", { kind: "primary", onClick: () => run() });
  const reason = h("span", { class: "nl-runs-reason nl-note", role: "status" });
  const bar = progress();
  const retryNote = h("div", { class: "nl-note nl-runs-retry", hidden: true }, "retrying after an engine stall: the first attempt froze and was abandoned");
  const errEl = h("div", { class: "nl-err nl-runs-error", role: "alert", hidden: true });

  // result: what the S.L. / D.L. tab shows, in its order: the loss table (max loss, or the mean of
  // the worst tail), the required liquidation discount derived from it, then the base-fee curve
  let stat = "max";
  const metaText = h("span", { class: "nl-note" });
  const staleBadge = h("span", { class: "nl-badge nl-warn", hidden: true, title: "the sweep controls differ from the ones this result was computed with: run again to refresh it" }, "controls changed since this sweep");
  const statSeg = seg([{ value: "max", label: "max loss" }, { value: "mean", label: "mean of worst tail" }], stat, v => { stat = v; paintResult(); });
  const lossTitle = h("div", { class: "nl-runs-h" }), lossHost = h("div", { class: "nl-runs-heat" });
  const discTitle = h("div", { class: "nl-runs-h" }), discHost = h("div", { class: "nl-runs-heat" });
  const discBest = h("div", {}), discMine = h("div", { class: "nl-runs-mine" });
  const heatLegend = h("div", { class: "nl-note nl-runs-heat-legend" });
  const fcTitle = h("div", { class: "nl-runs-h" }, "Base fee selection"), fcSub = h("div", { class: "nl-note" }), fcHost = h("div", { class: "nl-runs-fc" }), fcBest = h("div", {});
  const fcChart = lineChart(fcHost, { height: height || 260, xFmt: x => fmtNum(x, 4) + "%", yFmt: v => (Number.isFinite(v) ? v.toFixed(3) + "%" : "–"), empty: "no fee curve in this result" });
  const fcBox = h("div", { class: "nl-runs-fcbox" }, fcTitle, fcSub, fcHost, fcBest);
  const fcNote = h("div", { class: "nl-note", hidden: true }, "The engine produced no base-fee curve for this sweep (the history is shorter than the 3-day loans the curve uses).");
  const resultEl = h("div", { class: "nl-runs-result", hidden: true },
    h("div", { class: "nl-runs-meta" }, metaText, staleBadge), statSeg,
    h("div", { class: "nl-runs-block" }, lossTitle, lossHost),
    h("div", { class: "nl-runs-block" }, discTitle, discHost, discBest, discMine, heatLegend), fcBox, fcNote);
  const emptyEl = h("div", { class: "nl-note nl-runs-empty" }, "No sweep yet.");

  const el = shell("Sweep", "nl-runs-sldl", opts.bare,
    [h("div", { class: "nl-grid nl-runs-controls" }, SLDL_FIELDS.map(f => fields[f.key])),
      h("div", { class: "nl-runs-actions" }, centreBtn), estimate,
      h("div", { class: "nl-runs-actions" }, runBtn, reason), bar, retryNote, errEl, emptyEl, resultEl]);

  // ---- painting
  function paintFields() { for (const f of SLDL_FIELDS) fields[f.key].set(plain(store.spec.sldl[f.key])); }
  function paintEstimate() {
    const spec = store.spec, e = sldlEstimate(spec.sldl, store.rt), s = e.s;
    const parts = [`${count(e.cells)} cells`];
    if (e.has && e.rows) parts.push(`${count(e.rows)} rows at ${fmtStep(e.step)} (${plain(((e.to - e.from) / 86400).toFixed(0))} d)`, `${count(e.loans)} loans of ${fmtLoan(s.loan_days)} per cell`);
    else parts.push("price history not loaded");
    if (s.swapped) parts.push("a min above its max is swapped by the engine");
    estimate.textContent = parts.join(" · ");
    blockers = sldlBlockers(spec, store.rt, isolated);
    paintBusy(); paintStale();
  }
  function paintBusy() {
    const b = store.rt.busy.sldl || null;
    paintProgress(bar, b);
    retryNote.hidden = !(b && b.attempt === 2);
    runBtn.disabled = !!b || blockers.length > 0;
    runBtn.textContent = b ? "Sweeping…" : "Run S.L./D.L. sweep";
    reason.classList.toggle("nl-warn", !b && blockers.length > 0);
    reason.textContent = b || !blockers.length ? "" : blockers.join(" · ");
    errEl.hidden = !lastErr || !!b; errEl.textContent = lastErr;
  }
  function paintStale() {
    const r = store.spec.results && store.spec.results.sldl;
    if (!r || !r.inputs) { staleBadge.hidden = true; return; }
    const now = sldlNormalised(store.spec.sldl), keys = ["a_min", "a_max", "fee_min", "fee_max", "grid", "bands", "tail_pct", "loan_days"];
    staleBadge.hidden = keys.every(k => sig(N(now[k])) === sig(N(r.inputs[k])))
      && (r.inputs.smoothing_sig === undefined || r.inputs.smoothing_sig === smoothingSummary(store.spec).sig);
  }
  function paintResult() {
    const r = store.spec.results && store.spec.results.sldl, has = !!(r && Array.isArray(r.cells) && r.cells.length);
    resultEl.hidden = !has; emptyEl.hidden = has;
    if (has) {
      const i = r.inputs || {}, d = i.dataset || {}, m = sldlMatrix(r), p = store.spec.params, mk = marketCell(m, p);
      const spanD = (N(d.to) - N(d.from)) / 86400, nAll = Number.isFinite(N(r.n_all)) ? count(N(r.n_all)) : "all", hasMax = m.max.some(row => row.some(Number.isFinite));
      metaText.textContent = [`sweep of ${Number.isFinite(N(r.at)) ? fmtDateTime(N(r.at)) + " UTC" : "an unknown time"}`, `${m.A.length} × ${m.F.length} cells`,
        Number.isFinite(N(i.loan_days)) ? `loans of ${fmtLoan(N(i.loan_days))} over N = ${plain(i.bands)} bands` : "",
        Number.isFinite(N(i.ma_exp_time)) ? `oracle EMA ${plain(i.ma_exp_time)} s` : "",
        Number.isFinite(spanD) ? `history ${plain(spanD.toFixed(1))} d at ${fmtStep(N(d.step_s))} (${day(N(d.from))} to ${day(N(d.to))})` : "",
        Number.isFinite(N(r.runtime_s)) ? `ran ${fmtElapsed(N(r.runtime_s))} in this browser` : ""].filter(Boolean).join(" · ");
      statSeg.hidden = !hasMax;
      const useMax = hasMax && stat === "max", rows = m.A.map(plain), cols = m.F.map(f => plain(f) + "%"), mark = mk ? { r: mk.r, c: mk.c } : null;
      lossTitle.textContent = `loss from soft liquidation % (${useMax ? `worst single loan of all ${nAll} loan starts`
        : `mean of the worst ${Number.isFinite(N(r.n_top)) ? count(N(r.n_top)) + " of " : ""}${nAll} loan starts per cell = worst ${plain(i.tail_pct)}%`})`;
      heatTable(lossHost, { rowLabels: rows, colLabels: cols, corner: "A \\ base fee", fmt: v => (Number.isFinite(v) ? v.toFixed(3) + "%" : "–"), values: useMax ? m.max : m.loss, mark });

      // required liquidation discount: 1 − (1 − max loss) × bands-coefficient(A), the tab's formula
      const nb = Math.max(1, Math.round(N(i.bands)) || 4);
      const coeff = A => { let sum = 0; for (let k = 0; k < nb; k++) sum += Math.pow((A - 1) / A, k + 0.5); return sum / nb; };
      const disc = m.A.map((A, ri) => m.F.map((_, ci) => (Number.isFinite(m.max[ri][ci]) ? (1 - (1 - m.max[ri][ci] / 100) * coeff(A)) * 100 : NaN)));
      let best = null;
      disc.forEach((row, ri) => row.forEach((v, ci) => { if (Number.isFinite(v) && (!best || v < best.v)) best = { r: ri, c: ci, v }; }));
      const discBlock = discTitle.parentNode;
      discBlock.hidden = !best;
      if (best) {
        discTitle.textContent = `required liquidation discount % (1 − (1 − max loss) × bands-coefficient(A), ${nb} bands; minimum in bold)`;
        heatTable(discHost, { rowLabels: rows, colLabels: cols, corner: "A \\ base fee", fmt: v => (Number.isFinite(v) ? v.toFixed(3) + "%" : "–"), values: disc, mark,
          bold: best, rowMark: best.r, tip: (ri, ci) => `max loss ${Number.isFinite(m.max[ri][ci]) ? m.max[ri][ci].toFixed(4) : "–"}%` });
        discBest.replaceChildren("The liquidation discount is minimized at A = ", h("b", {}, plain(m.A[best.r])), ` (fee ${plain(m.F[best.c])}%, ${best.v.toFixed(3)}%).`);
        const need = mk ? disc[mk.r][mk.c] : NaN, have = N(p.liquidation_discount_pct);
        discMine.hidden = !Number.isFinite(need);
        if (Number.isFinite(need)) {
          discMine.className = "nl-runs-mine " + (have >= need ? "nl-good" : "nl-bad");
          discMine.textContent = `This market (A ${plain(p.A)}, fee ${plain(p.fee_pct)}%) sits ${mk.inside ? "in" : "outside the grid, nearest"} the cell A ${plain(mk.A)} · fee ${plain(mk.fee_pct)}%: ` +
            `it needs a liquidation discount of ${need.toFixed(3)}%, and is configured with ${plain(have)}%` + (have >= need ? ", which covers it." : ", which is too little.");
        }
      }
      heatLegend.textContent = `Rows: A. Columns: base fee. Green = lowest of the table, red = highest. Outlined: the cell nearest this market (A ${plain(p.A)}, fee ${plain(p.fee_pct)}%)` +
        (mk && !mk.inside ? ", which lies outside the swept range." : ".");

      const fc = feeCurvePoints(r.fee_curve);
      fcBox.hidden = !fc; fcNote.hidden = !!fc;
      if (fc) {
        const k = r.fee_curve, mf = N(p.fee_pct);
        let mi = 0;
        fc.y.forEach((v, j) => { if (v < fc.y[mi]) mi = j; });
        fcSub.textContent = `average loss per base fee · ${plain(k.loan_days)}-day loans at A ${plain(k.A)} · ${k.kind || "sampled loans"}`;
        fcChart.setData({ t: fc.x, series: [{ name: "average loss %", v: fc.y, color: "#58a6ff", width: 2, dots: 2.6 }],
          marks: [{ x: fc.x[mi], y: fc.y[mi], r: 4.5, color: "#e3b341" }],
          vlines: mf > fc.x[0] && mf < fc.x[fc.x.length - 1] ? [{ x: mf, label: "this market's fee" }] : [] });
        fcBest.replaceChildren("The average loss is minimized at a fee of ", h("b", {}, plain(fc.x[mi]) + "%"), ".");
      }
    }
    paintStale();
  }

  async function run() {
    if (active || store.rt.busy.sldl || blockers.length) return;
    const live = store.spec, rt0 = store.rt;
    // frozen inputs: the retry inside runSldl must see the same controls and the same history
    const spec = { ...live, sldl: { ...live.sldl }, params: { ...live.params }, collateral: { ...live.collateral }, meta: { ...live.meta } };
    const rt = { grid: rt0.grid, oracle: rt0.oracle, market: rt0.market, valid0: rt0.valid0 };
    const inputs = sldlInputs(spec, rt), me = active = { since: Date.now(), quiet: false };
    const publish = (label, done, total, attempt) => store.setBusy("sldl", { label, done, total, attempt, since: me.since, token: me });
    lastErr = "";
    publish("preparing the dataset and the wasm engine", 0, 0, 1);
    let last = 0;
    try {
      const res = await runSldl(spec, rt, (done, total, label, attempt) => {
        const now = Date.now();
        if (me.quiet || (done < total && now - last < 80)) return;
        last = now;
        publish(label || "preparing", done, total, attempt);
      });
      if (store.spec !== live) throw new Error("the market was replaced while the sweep was computing, so its result was discarded: run again");
      const trimmed = trimSldl(res, { inputs, at: Math.floor(Date.now() / 1000) });
      store.update("results", s => { s.results.sldl = trimmed; });
      toast(`S.L./D.L. sweep finished: ${trimmed.cells.length} cells in ${fmtElapsed(N(trimmed.runtime_s))}`, "good");
    } catch (e) {
      lastErr = String((e && e.message) || e);
      toast(lastErr, "error");
    } finally {
      active = null;
      const b = store.rt.busy.sldl;
      if (b && b.token === me) store.setBusy("sldl", null);
      if (!destroyed) paintBusy();
    }
  }

  paintEstimate(); paintResult();
  return {
    el,
    update(tags) {
      if (destroyed) return;
      if (tags.includes("sldl")) paintFields();
      if (tags.some(t => t === "sldl" || t === "oracle.data" || t === "params")) paintEstimate();
      if (tags.includes("results") || tags.includes("params")) paintResult();
      if (tags.includes("busy")) paintBusy();
    },
    destroy() {
      destroyed = true;
      if (active) {                                       // the sweep keeps computing in the page: say so, stop relaying ticks
        active.quiet = true;
        store.setBusy("sldl", { label: "S.L./D.L. sweep still computing", done: 0, total: 0, since: active.since, token: active });
      }
      fcChart.destroy();
    },
  };
}

// ---- mount ------------------------------------------------------------------------------------------------
const RELEVANT = new Set(["spec", "results", "sldl", "busy", "params", "borrower", "venue", "oracle.data"]);

export function mount(host, ctx, opts = {}) {
  const store = ctx.store, want = PARTS.includes(opts.part) ? [opts.part] : PARTS;
  const root = h("div", { class: "nl-runs" });
  host.append(root);
  let parts = [], raf = 0, dead = false;
  const pending = new Set();

  function build() {
    for (const p of parts) p.destroy();
    root.replaceChildren();
    parts = want.map(name => (name === "baddebt" ? badDebtPart : sldlPart)(store, opts));
    for (const p of parts) root.append(p.el);
  }
  function flush() {
    raf = 0;
    if (dead) return;
    const tags = [...pending];
    pending.clear();
    if (tags.includes("spec")) build();                 // whole spec replaced: rebuild fully
    else for (const p of parts) p.update(tags);
  }
  const off = store.on(tag => {
    if (dead || !(RELEVANT.has(tag) || String(tag).startsWith("scenario"))) return;
    pending.add(tag);
    if (!raf) raf = requestAnimationFrame(flush);
  });
  build();
  return {
    destroy() {
      dead = true;
      off();
      if (raf) cancelAnimationFrame(raf);
      for (const p of parts) p.destroy();
      parts = [];
      root.remove();
    },
  };
}
