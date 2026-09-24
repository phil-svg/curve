// From spec to numbers: load the sources the script reads, evaluate the
// oracle on one grid, cut/draw the scenario, and shape the inputs of the two
// simulators (bad-debt /run on the server, S.L./D.L. wasm sweep in the page).
import { compile, evaluate, sourcesUsed } from "./expr.js";
import { makeGrid, toGrid, toGridLinear, unionGrid, median, lastFinite, lowerBound, reEma, impliedInput } from "./series.js";
import { smoothingOf } from "./expr.js";
import { api, EXACT_WINDOW_S } from "./api.js";

export function resolveRange(spec) {
  const r = spec.oracle.range, step = Math.max(60, +r.step_s || 3600);
  if (r.from && r.to) return { from: Math.floor(r.from / step) * step, to: Math.floor(r.to / step) * step, step };
  const to = Math.floor(Date.now() / 1000 / step) * step - step;
  return { from: to - Math.round((+r.days || 365) * 86400 / step) * step, to, step };
}

// On-chain history is BLOCK-EXACT (api.sampleExact): read in every block where a contract behind the reading logged
// something, refined in between. A HELD reading is a stored value that only changes in such a block; everything else may
// also drift with time (an EMA, a vault rate, a virtual price over rate-bearing coins) and is joined by straight lines.
export const HELD_READING = /^(last_price|last_prices|balances|get_balances|totalSupply|latestRoundData|latestAnswer)\(/;
export function howOf(s) {
  const own = String(s.address || "").toLowerCase();
  const pools = (((s.ema || {}).tree) || []).filter(n => n.type === "pool").map(n => String(n.address || "").toLowerCase());
  // its own logs, and the pools behind it while they are few: scanning a busy aggregator's five pools costs more than the 5-minute clock it replaces
  const watch = pools.includes(own) || !pools.length || pools.length > 2 ? [own] : [own, ...pools];
  return { raw: HELD_READING.test(s.sig || ""), watch: [...new Set(watch)] };
}
// the grid step is no part of an on-chain source's identity any more, and its range only in whole windows and hours
const sigOf = (spec, s, range) => s.kind === "onchain"
  ? JSON.stringify([spec.chain, "exact", s.address, s.sig, s.args, s.slot, s.rtype, s.decimals, s.raw, howOf(s), Math.floor(range.from / EXACT_WINDOW_S), Math.floor(range.to / 3600)])
  : JSON.stringify([spec.chain, s.kind, s.pool, s.base, s.quote, s.units, s.key, s.column, s.value,
    s.kind === "upload" ? (s.rows || []).length : 0, range.from, range.to, range.step]);

// ---- the prepared pack: what a page load reads instead of asking the chain ---------------------------
// The server keeps one file per registered market (nl_api.build_pack, built by hand: fetchers/build_nl_packs.py): the
// block-exact series of every on-chain source, the coin-chart readings, labels, contract names, EMA trees and
// the values at its latest block. Opening the tab reads that file and makes NO RPC call; only an explicit
// "Load & evaluate" samples the chain (for a source the pack does not hold, or to be fresher than the pack).
const sameReading = (s, r) => s.kind === "onchain" && String(s.address || "").toLowerCase() === r.to && String(s.sig || "").replace(/\s+/g, "") === r.sig
  && JSON.stringify((s.args || []).map(String)) === JSON.stringify((r.args || []).map(String)) && (s.slot || 0) === r.slot
  && (s.raw ? 0 : (s.decimals ?? 18)) === r.decimals;
export const packSeries = (pack, reading) => pack && [...pack.series, ...(pack.extra || [])].find(x => JSON.stringify(x.reading) === JSON.stringify(reading));
export async function loadPack(store) {
  const { marketOf } = await import("./registry.js");
  const m = await marketOf(store.spec).catch(() => null);
  if (!m) return false;
  let pack = null;
  for (let k = 0; k < 6 && !pack; k++) {                   // a freshly started server may still be writing it
    pack = await api.pack(m.file);
    if (!pack) await new Promise(r => setTimeout(r, 5000));
  }
  if (!pack) return false;
  const spec = store.spec, range = resolveRange(spec), values = (pack.live && pack.live.values) || {};
  // EMA trees first: they decide a source's watch list, and with them in the spec nothing asks /nlapi/ema_tree
  const missing = spec.oracle.sources.filter(s => pack.ema[s.name] && !(s.ema && s.ema.tree));
  if (missing.length) store.update("oracle.sources", sp => { for (const s of sp.oracle.sources) { const e = pack.ema[s.name]; if (e && !(s.ema && s.ema.tree)) {
    s.ema = { onchain: null, use: null, ...(s.ema || {}), ...e }; if (SMOOTHED_READING.test(s.sig || "") && !(s.ema.onchain > 0) && e.detected) s.ema.onchain = e.detected; } } });
  for (const s of spec.oracle.sources) {
    const hit = pack.series.find(x => sameReading(s, x.reading));
    if (!hit) { if (s.kind === "const") store.rt.sources[s.id] = { status: "ok", sig: sigOf(spec, s, range), const: +s.value, n: 1, live: +s.value }; continue; }
    store.rt.sources[s.id] = { status: "ok", sig: sigOf(spec, s, range), t: Float64Array.from(hit.t), v: Float64Array.from(hit.v), n: hit.t.length, exact: true, held: !!hit.held,
      live: values[s.name], fromPack: true };
  }
  store.rt.pack = pack;
  applyLive(store, pack.live ? pack.live.block : null, pack.live ? pack.live.timestamp : null);
  evaluateOracle(store);
  return true;
}
// the script on the sources' live values (whoever put them there: a pack, or liveValues' own read)
export function applyLive(store, block = store.rt.liveBlock, timestamp = store.rt.liveAt) {
  const env = {};
  for (const s of store.spec.oracle.sources) { const d = store.rt.sources[s.id]; if (s.name && d && Number.isFinite(d.live)) env[s.name] = Float64Array.of(d.live); }
  const { vars } = evaluate(compile(store.spec.oracle.script), env, { t: Float64Array.of(0), step: 1 });
  const live = {};
  for (const [k, v] of Object.entries(vars)) { let x = v instanceof Float64Array ? v[0] : v; if (x > 1e9) x /= 1e18; live[k] = x; }
  store.setRt("oracle.data", { liveVars: live, liveBlock: block, liveAt: timestamp });
  return live;
}

let loadToken = 0;
// Loads every source the script reads (all of them when `all`), then evaluates.
export async function loadSources(store, { force = false, all = false } = {}) {
  // which contracts stand behind each reading decides which blocks get read: know them before the first fetch
  try { await detectSmoothing(store, { explicit: true }); } catch (e) { console.warn("[nl] ema tree", e); }
  const my = ++loadToken, spec = store.spec, range = resolveRange(spec);
  const used = sourcesUsed(compile(spec.oracle.script));
  const todo = spec.oracle.sources.filter(s => s.name && (all || used.has(s.name)));
  const rtS = store.rt.sources;
  let done = 0;
  store.setBusy("sources", { label: "loading sources", done: 0, total: todo.length });
  await Promise.all(todo.map(async s => {
    const sig = sigOf(spec, s, range), cur = rtS[s.id];
    if (!force && cur && cur.status === "ok" && cur.sig === sig) { done++; return; }
    rtS[s.id] = { status: "loading", progress: 0, sig, live: cur && cur.live };
    store.emit("oracle.data");
    let lastPaint = 0;
    const prog = (a, b) => {
      rtS[s.id].progress = b ? a / b : 0;
      if (Date.now() - lastPaint > 120) { lastPaint = Date.now(); store.emit("oracle.data"); }
    };
    try {
      let series = null;
      if (s.kind === "onchain") {
        if (!/^0x[0-9a-fA-F]{40}$/.test(s.address || "")) throw new Error("needs a contract address");
        if (!s.sig) throw new Error("needs a function signature");
        series = await api.sampleExact(spec.chain, s, range, howOf(s), prog);
      } else if (s.kind === "curve") {
        series = await api.curveSeries(spec.chain, s, range, prog);
      } else if (s.kind === "dataset") {
        series = await api.datasetSeries(s, prog);
      } else if (s.kind === "upload") {
        const rows = (s.rows || []).filter(r => Number.isFinite(r[0]) && Number.isFinite(r[1])).sort((a, b) => a[0] - b[0]);
        if (!rows.length) throw new Error("no rows pasted yet");
        series = { t: Float64Array.from(rows.map(r => r[0])), v: Float64Array.from(rows.map(r => r[1])) };
      } else if (s.kind === "const") {
        series = { const: +s.value };
      }
      if (my !== loadToken && rtS[s.id].sig !== sig) return;      // superseded
      rtS[s.id] = { status: "ok", sig, live: rtS[s.id].live, ...(series.const !== undefined
        ? { const: series.const, n: 1 } : { t: series.t, v: series.v, n: series.t.length, exact: !!series.exact, held: !!series.held }) };
    } catch (e) {
      rtS[s.id] = { status: "error", err: String(e.message || e), sig: null };
    }
    done++;
    store.setBusy("sources", { label: "loading sources", done, total: todo.length });
    store.emit("oracle.data");
  }));
  store.setBusy("sources", null);
  evaluateOracle(store);
}

// What an MA slider does NOT change is kept between evaluations: the grid, every source on the grid, the input its
// on-chain EMA implies, and the "parts as sampled" evaluation. A slider tick then costs one re-EMA and one script run.
let GRID = null;                                // {key, grid}
const ON_GRID = new WeakMap(), UID = new WeakMap();   // rt source entry -> {key, g, implied: Map(T0 -> array)}; -> number
let uidNext = 0, BASE = null;                   // {key, vars}
const LINES = new Map();                        // expr.evaluate's per-line memo
// raw 1e18 integers all the way through? The median of a thinned sample answers that (sorting 69k points did, slowly)
const rawUnits = a => { const k = Math.max(1, Math.floor(a.length / 512)), s = []; for (let i = 0; i < a.length; i += k) if (Number.isFinite(a[i])) s.push(a[i]); return s.length > 0 && median(s) > 1e9; };
const uidOf = d => { if (!UID.has(d)) UID.set(d, ++uidNext); return UID.get(d); };

export function evaluateOracle(store) {
  const spec = store.spec, rt = store.rt, range = resolveRange(spec);
  const compiled = compile(spec.oracle.script);
  const used = sourcesUsed(compiled);
  // The grid: the regular one, plus every moment a block-exact source the script reads has a point. `step` stays the
  // nominal spacing of the regular part (window lengths, candles); the points in between are what makes it per block.
  const exact = spec.oracle.sources.filter(s => s.name && used.has(s.name)).map(s => rt.sources[s.id]).filter(d => d && d.status === "ok" && d.exact);
  const gridKey = [range.from, range.to, range.step, ...exact.map(uidOf)].join("|");
  if (!GRID || GRID.key !== gridKey) {
    const base = makeGrid(range.from, range.to, range.step);
    let t = base;
    if (exact.length && base.length) { const u = unionGrid(base, exact); if (u.length <= 900000) t = u; }
    GRID = { key: gridKey, grid: { t, step: range.step, exact: t !== base } };
  }
  const grid = GRID.grid, t = grid.t;
  // env = what the script sees; envBase = the same readings exactly as sampled.
  // They differ only where a source's EMA is re-timed (spec source .ema.use).
  const env = {}, envBase = {}, missing = [], parts = {};
  let retimed = false;
  for (const s of spec.oracle.sources) {
    const d = rt.sources[s.id];
    if (!s.name) continue;
    if (d && d.status === "ok") {
      if (d.const !== undefined) { env[s.name] = envBase[s.name] = d.const; continue; }
      let hit = ON_GRID.get(d);
      if (!hit || hit.key !== gridKey) ON_GRID.set(d, hit = { key: gridKey, g: d.exact && !d.held ? toGridLinear(d, t) : toGrid(d, t), implied: new Map() });
      const g = hit.g, e = emaOf(s), wantImplied = e.onchain > 0 && (e.active || used.has(s.name));
      if (wantImplied && !hit.implied.has(e.onchain)) hit.implied.set(e.onchain, impliedInput(g, t, e.onchain));
      const implied = wantImplied ? hit.implied.get(e.onchain) : null;
      envBase[s.name] = g;
      if (e.active) { env[s.name] = reEma(g, t, e.onchain, e.use, implied); retimed = true; } else env[s.name] = g;
      if (used.has(s.name)) parts[s.name] = { sampled: g, used: env[s.name], implied };
    } else if (used.has(s.name)) missing.push(s.name);
  }
  const { vars, errors } = evaluate(compiled, env, grid, LINES);
  let base = null;
  if (retimed) {
    const baseKey = JSON.stringify([spec.oracle.script, gridKey, spec.oracle.sources.map(s => { const d = rt.sources[s.id]; return [s.name, d && d.status === "ok" ? (d.const !== undefined ? d.const : uidOf(d)) : null]; })]);
    if (!BASE || BASE.key !== baseKey) BASE = { key: baseKey, vars: evaluate(compiled, envBase, grid).vars };
    base = BASE.vars;
  }
  const evalErrors = errors.filter(e => !missing.some(m => e.msg.includes(`"${m}"`)));
  const names = compiled.lines.map(l => l.name);
  const pick = n => (vars[n] instanceof Float64Array ? vars[n] : null);
  let oracle = pick("oracle") || [...names].reverse().map(pick).find(Boolean) || null;
  let market = pick("market") || oracle;
  let rescaled = false;
  if (oracle && rawUnits(oracle)) {
    oracle = oracle.map(x => x / 1e18);
    rescaled = true;
  }
  if (!pick("market")) market = oracle;         // no market line: the two stay one and the same array
  else if (rawUnits(market)) market = market.map(x => x / 1e18);
  // the oracle as it would read with every part left at its sampled EMA
  let oracleBase = null;
  if (base) {
    const bp = n => (base[n] instanceof Float64Array ? base[n] : null);
    oracleBase = bp("oracle") || [...names].reverse().map(bp).find(Boolean) || null;
    if (oracleBase && rawUnits(oracleBase)) oracleBase = oracleBase.map(x => x / 1e18);
  }
  let valid0 = 0;
  if (oracle) {
    valid0 = oracle.length;
    for (let i = 0; i < oracle.length; i++)
      if (Number.isFinite(oracle[i]) && Number.isFinite(market[i])) { valid0 = i; break; }
  }
  store.setRt("oracle.data", { grid, vars, oracle, market, valid0, rescaled,
    compileErrors: compiled.errors, evalErrors, missing, parts, oracleBase });
}

// ---- smoothing ---------------------------------------------------------------------
// One source's EMA setting: `onchain` = the EMA time already inside the sampled
// reading (a pool's ma_exp_time ...), `use` = the time to simulate with instead
// (blank = leave as sampled, 0 = strip the EMA). Re-timing happens at evaluation
// time on the cached samples, so turning a knob never refetches anything.
export function emaOf(s) {
  const e = (s && s.ema) || {}, num = v => (v === null || v === undefined || v === "" ? NaN : +v);
  const onchain = num(e.onchain) > 0 ? num(e.onchain) : 0, use = num(e.use);
  return { onchain, use, active: Number.isFinite(use) && use >= 0 && use !== onchain };
}

// readings that leave their contract already EMA-smoothed (a raw getter such as
// get_virtual_price sits NEXT to a pool's EMA, not under it)
export const SMOOTHED_READING = /^(price_oracle|price|price_w|priceAsCrvusd|lp_price|ema_price)\(/;

// Walk the contracts behind every on-chain part the script reads and record each
// EMA found (server: /nlapi/ema_tree). Fills source.ema.tree / detected / times, and
// source.ema.onchain where the reading is a smoothed one and nothing was set yet.
let detecting = null;
export function detectSmoothing(store, { force = false, explicit = false } = {}) {
  if (detecting) return detecting;
  // A component that merely mounts must not send the server to the chain: before any history is there, the trees
  // arrive with the prepared pack (loadPack), or with the explicit load that asked for them.
  if (!force && !explicit && !store.rt.oracle) return Promise.resolve(false);
  const used = sourcesUsed(compile(store.spec.oracle.script));
  const todo = store.spec.oracle.sources.filter(s => s.name && used.has(s.name) && s.kind === "onchain"
    && /^0x[0-9a-fA-F]{40}$/.test(s.address || "") && (force || !(s.ema && s.ema.tree)));
  if (!todo.length) return Promise.resolve(false);
  store.setBusy("smoothing", { label: "reading the oracle contracts", done: 0, total: todo.length });
  detecting = (async () => {
    let done = 0;
    for (const s of todo) {
      try {
        const r = await api.emaTree(store.spec.chain, s.address), smoothed = SMOOTHED_READING.test(s.sig || "");
        store.update("oracle.sources", sp => {
          const x = sp.oracle.sources.find(k => k.id === s.id);
          if (!x) return;
          x.ema = { onchain: null, use: null, ...(x.ema || {}), detected: r.suggested, mixed: r.mixed, times: r.ema_times,
            tree: r.nodes.map(n => ({ address: n.address, type: n.type, label: n.label, ma: n.ma, depth: n.depth, refs: n.refs })) };
          if (smoothed && !(x.ema.onchain > 0) && r.suggested) x.ema.onchain = r.suggested;
        });
      } catch (e) { console.warn("[nl] ema tree", s.name, e); }
      store.setBusy("smoothing", { label: "reading the oracle contracts", done: ++done, total: todo.length });
    }
    store.setBusy("smoothing", null);
    detecting = null;
    evaluateOracle(store);
    return true;
  })();
  return detecting;
}

// Everything that smooths the oracle, as one list + a signature the simulators
// store with their results (so "inputs changed since this run" sees EMA edits).
export function smoothingSummary(spec) {
  const used = sourcesUsed(compile(spec.oracle.script)), items = [];
  for (const s of spec.oracle.sources) {
    if (!s.name || !used.has(s.name)) continue;
    const e = emaOf(s);
    if (e.onchain || e.active) items.push({ kind: "source", name: s.name, onchain: e.onchain, use: e.active ? e.use : e.onchain, changed: e.active });
  }
  for (const c of smoothingOf(spec.oracle.script).calls)
    items.push({ kind: "script", name: `${c.fn}(${c.target})`, use: c.value, line: c.line, changed: false });
  const text = items.map(i => i.kind === "source"
    ? `${i.name} ${i.changed ? `${i.onchain || "raw"} → ${i.use} s` : `${i.use} s`}` : `${i.name} ${i.use} s`).join(" · ");
  return { items, text: text || "no smoothing anywhere in the chain", sig: JSON.stringify(items.map(i => [i.name, i.use])) };
}

// latest on-chain value of every on-chain source + the script evaluated on them
export async function liveValues(store) {
  const spec = store.spec, rt = store.rt;
  const on = spec.oracle.sources.filter(s => s.kind === "onchain" && /^0x[0-9a-fA-F]{40}$/.test(s.address || "") && s.sig);
  let res = { values: {} };
  if (on.length) res = await api.call(spec.chain, on.map(s => ({ id: s.id, to: s.address, sig: s.sig,
    args: s.args || [], slot: s.slot || 0, rtype: s.rtype || "uint", decimals: s.raw ? 0 : (s.decimals ?? 18) })));
  const env = {};
  for (const s of spec.oracle.sources) {
    const d = rt.sources[s.id] || (rt.sources[s.id] = { status: "idle" });
    if (s.kind === "onchain") d.live = res.values[s.id];
    else if (s.kind === "const") d.live = +s.value;
    else if (d.v) d.live = lastFinite(d.v);
    if (s.name && Number.isFinite(d.live)) env[s.name] = Float64Array.of(d.live);
  }
  const { vars } = evaluate(compile(spec.oracle.script), env, { t: Float64Array.of(0), step: 1 });
  const live = {};
  for (const [k, v] of Object.entries(vars)) {
    let x = v instanceof Float64Array ? v[0] : v;
    if (x > 1e9) x /= 1e18;
    live[k] = x;
  }
  store.setRt("oracle.data", { liveVars: live, liveBlock: res.block, liveAt: res.timestamp });
  return live;
}

// ---- scenario -------------------------------------------------------------------
// The deepest falls of the market series inside windows of `span` seconds, deepest
// first, no two windows overlapping. A fall = the lowest price within `span` after
// a point, over the price at that point. The window that plays is `span` long and
// is hung on the CRASH itself (the last moment the price still stood at least half
// way between the high and the low): a quarter of it before, three quarters after.
const WORST = new WeakMap();              // market array -> Map(key -> windows)
export function worstWindows(rt, span, count = 3) {
  const { grid, market, valid0 } = rt;
  if (!grid || !market) return [];
  let memo = WORST.get(market);
  if (!memo) WORST.set(market, memo = new Map());
  const t = grid.t, n = t.length;
  const key = [span, count, valid0, n, t[0], t[n - 1]].join("|");
  if (memo.has(key)) return memo.get(key);
  // by TIME, not by index: the grid carries extra points wherever a block changed something
  const drop = new Float64Array(n).fill(NaN), low = new Int32Array(n);
  const dq = [];                          // indices, market ascending: front = window min
  let r = valid0, head = 0;
  for (let i = valid0; i < n; i++) {
    while (r < n && t[r] <= t[i] + span) {
      if (Number.isFinite(market[r])) { while (dq.length > head && market[dq[dq.length - 1]] >= market[r]) dq.pop(); dq.push(r); }
      r++;
    }
    while (head < dq.length && dq[head] < i) head++;
    if (head >= dq.length || !Number.isFinite(market[i])) continue;
    drop[i] = market[dq[head]] / market[i] - 1; low[i] = dq[head];
  }
  const out = [], taken = [], tMin = t[valid0], tMax = t[n - 1];
  const crashAt = i => { const j = low[i], mid = (market[i] + market[j]) / 2; let k = j; while (k > i && !(market[k] >= mid)) k--; return k; };
  const spanOf = i => {                   // -> [from, to] in seconds
    let a = t[crashAt(i)] - span / 4;
    if (t[low[i]] > a + span * 0.9) a = t[low[i]] + span * 0.1 - span;      // a slow grind: the low stays inside
    a = Math.max(tMin, Math.min(a, tMax - span));
    return [a, Math.min(tMax, a + span)];
  };
  while (out.length < count) {
    let bi = -1;
    for (let i = valid0; i < n; i++) {
      if (!Number.isFinite(drop[i]) || (bi >= 0 && !(drop[i] < drop[bi]))) continue;
      const [a, b] = spanOf(i);
      if (!taken.some(([x, y]) => a <= y && b >= x)) bi = i;
    }
    if (bi < 0) break;
    const [a, b] = spanOf(bi);
    taken.push([a, b]);
    // `drop` is what the window plays: its own deepest peak-to-trough fall
    let peak = -Infinity, dd = 0;
    for (let i = lowerBound(t, a); i < n && t[i] <= b; i++) { const x = market[i]; if (!Number.isFinite(x)) continue; if (x > peak) peak = x; else if (1 - x / peak > dd) dd = 1 - x / peak; }
    out.push({ t0: a, t1: b, drop: -dd, peak_t: t[crashAt(bi)], low_t: t[low[bi]] });
  }
  memo.set(key, out);
  return out;
}
export const worstWindow = (rt, span) => worstWindows(rt, span, 1)[0] || null;

// The crash picker's choice (spec.scenario.crash), resolved on the loaded history.
// It is what plays while no clip has been cut by hand.
export const CRASH_RANKS = ["worst", "2nd worst", "3rd worst"];
export const CRASH_SPANS = [{ s: 10800, label: "3 h" }, { s: 21600, label: "6 h" }, { s: 43200, label: "12 h" }, { s: 86400, label: "1 d" },
  { s: 172800, label: "2 d" }, { s: 259200, label: "3 d" }, { s: 604800, label: "7 d" }];
export function pickedCrash(spec, rt) {
  const c = spec.scenario.crash || {}, span = +c.span_s > 0 ? +c.span_s : 259200;
  const all = worstWindows(rt, span, CRASH_RANKS.length);
  if (!all.length) return null;
  const rank = Math.min(all.length - 1, Math.max(0, Math.round(+c.rank) || 0));
  return { ...all[rank], rank, span_s: span, available: all.length };
}
export const worseOf = spec => { const x = +spec.scenario.worse; return Number.isFinite(x) && x > 0 ? Math.min(10, Math.max(0.1, x)) : 1; };
function maxDrawdown(a) {
  let peak = -Infinity, dd = 0;
  for (const x of a) { if (!Number.isFinite(x)) continue; if (x > peak) peak = x; else if (peak > 0 && 1 - x / peak > dd) dd = 1 - x / peak; }
  return dd;
}

export function startPriceOf(spec, rt) {
  const sp = spec.scenario.start_price;
  if (Number.isFinite(+sp) && +sp > 0 && sp !== null && sp !== "") return +sp;
  const live = rt.liveVars && (rt.liveVars.market ?? rt.liveVars.oracle);
  if (Number.isFinite(live)) return live;
  const last = rt.market ? lastFinite(rt.market) : NaN;
  return Number.isFinite(last) ? last : 1;
}

// -> {t (s from 0), market, oracle|null, duration, label, clipsUsed}
export function buildScenario(spec, rt) {
  const sc = spec.scenario, start = startPriceOf(spec, rt);
  if (sc.mode === "linear") {
    const d = Math.max(60, (+sc.linear.duration_min || 1) * 60), end = start * (1 - (+sc.linear.drop_pct || 0) / 100);
    return { t: Float64Array.of(0, d), market: Float64Array.of(start, end), oracle: null, duration: d,
      label: `linear ${(+sc.linear.drop_pct).toFixed(1)}% over ${+sc.linear.duration_min} min` };
  }
  if (sc.mode === "draw") {
    const pts = (sc.points || []).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]) && p[1] > 0)
      .slice().sort((a, b) => a[0] - b[0]);
    if (pts.length < 2) return null;
    const t0 = pts[0][0];
    return { t: Float64Array.from(pts.map(p => p[0] - t0)), market: Float64Array.from(pts.map(p => start * p[1] / pts[0][1])),
      oracle: null, duration: pts[pts.length - 1][0] - t0, label: `drawn path, ${pts.length} points` };
  }
  // history: recorded clips, stitched level-continuous
  if (!rt.grid || !rt.market) return null;
  const gt = rt.grid.t;
  // one or the other, never a mix: the picked crash, or (by_hand) the clips cut on the timeline
  let clips = sc.by_hand ? (sc.clips || []).filter(c => c.t1 > c.t0) : [], picked = null;
  if (!clips.length) { picked = pickedCrash(spec, rt); if (picked) clips = [{ t0: picked.t0, t1: picked.t1, speed: 1, amplify: 1 }]; }
  const T = [], M = [], O = [];
  let offset = 0, level = null;
  for (const c of clips) {
    const i0 = Math.max(rt.valid0, lowerBound(gt, c.t0)), i1 = Math.min(gt.length - 1, lowerBound(gt, c.t1));
    if (i1 - i0 < 1) continue;
    const base = rt.market[i0], k = +c.amplify || 1, sp = +c.speed || 1;
    if (!Number.isFinite(base)) continue;
    const f = (level === null ? base : level) / base;
    for (let i = i0; i <= i1; i++) {
      const m = rt.market[i], o = rt.oracle[i];
      if (!Number.isFinite(m)) continue;
      T.push(offset + (gt[i] - gt[i0]) / sp);
      M.push(base * Math.pow(m / base, k) * f);
      O.push(Number.isFinite(o) ? base * Math.pow(o / base, k) * f : NaN);
    }
    offset = T[T.length - 1] + rt.grid.step / sp;
    level = M[M.length - 1];
  }
  if (T.length < 2) return null;
  // "x times worse": the deepest peak-to-trough fall of the path becomes `worse` times
  // deeper (at most -99 %). Every log-return is scaled by one exponent, so the shape,
  // the timing and the oracle's lag behind the market all stay what was recorded.
  const worse = worseOf(spec), recorded = maxDrawdown(M);
  let played = recorded;
  if (worse !== 1 && recorded > 0) {
    played = Math.min(0.99, recorded * worse);
    const k = Math.log(1 - played) / Math.log(1 - recorded), b = M[0];
    for (let i = 0; i < M.length; i++) { M[i] = b * Math.pow(M[i] / b, k); if (Number.isFinite(O[i])) O[i] = b * Math.pow(O[i] / b, k); }
  }
  const g = start / M[0];
  const hasO = O.every(Number.isFinite);
  const what = picked ? `${CRASH_RANKS[picked.rank]} ${(CRASH_SPANS.find(x => x.s === picked.span_s) || { label: Math.round(picked.span_s / 3600) + " h" }).label} crash`
    : `${clips.length} recorded clip${clips.length > 1 ? "s" : ""}`;
  return { t: Float64Array.from(T), market: Float64Array.from(M.map(x => x * g)),
    oracle: hasO ? Float64Array.from(O.map(x => x * g)) : null, duration: T[T.length - 1],
    label: what + (worse !== 1 ? ` × ${+worse.toFixed(3)}` : ""), clipsUsed: clips, picked, fall: { recorded, played, worse } };
}

const thin = (rows, max) => {
  if (rows.length <= max) return rows;
  const k = Math.ceil(rows.length / max), out = rows.filter((_, i) => i % k === 0);
  if (out[out.length - 1] !== rows[rows.length - 1]) out.push(rows[rows.length - 1]);
  return out;
};

// The modelled borrower is FIXED, not a setting: the whole market as ONE position,
// N = 4 bands, debt = the debt ceiling, and the least collateral the market lets
// that debt be opened with (max LTV). That is the worst book the parameters permit.
export const BORROWER_BANDS = 4;
export async function resolveBorrower(spec, startPrice, oracleSeed) {
  const p = spec.params;
  const cap = await api.debtcap({ collateral_usd: 1e6, start_price: startPrice, n_bands: BORROWER_BANDS,
    loan_discount_pct: p.loan_discount_pct, oracle_seed: oracleSeed, llamma_A: p.A });
  const ltv = cap.max_ltv_pct * 0.9999, debt = +p.borrow_cap;
  return { collateral_usd: debt / (ltv / 100), debt_usd: debt, max_ltv_pct: cap.max_ltv_pct, ltv_pct: ltv, n_bands: BORROWER_BANDS };
}

export function toRunParams(spec, scenario, borrower) {
  const sc = spec.scenario, p = spec.params, v = spec.venue;
  const lead = Math.max(0, +sc.lead_in_min || 0) * 60, tail = Math.max(0, +sc.tail_min || 0) * 60;
  const path = thin(Array.from(scenario.t, (x, i) => [lead + x, scenario.market[i]]), 20000);
  const useO = scenario.oracle && sc.oracle_mode === "recorded";
  const opath = useO ? thin(Array.from(scenario.t, (x, i) => [lead + x, scenario.oracle[i]]), 20000) : null;
  const start = scenario.market[0], end = scenario.market[scenario.market.length - 1];
  return {
    price_path: path, ...(opath ? { oracle_path: opath } : {}),
    crash_start_spot: start, crash_end_spot: end,
    crash_start_offset_s: lead, crash_duration_s: scenario.duration,
    horizon_min: (lead + scenario.duration + tail) / 60,
    oracle_seed: opath ? opath[0][1] : start,
    // the engine wants a half-life; pools store ma_exp_time = half-life / ln 2
    ma_time_s: (+p.ma_exp_time || 866) * Math.LN2,
    liquidation_discount_pct: +p.liquidation_discount_pct, loan_discount_pct: +p.loan_discount_pct,
    llamma_A: Math.round(+p.A), n_bands: BORROWER_BANDS,
    amm_fee_wei: Math.round(+p.fee_pct * 1e16) || null,
    // no amm_rate_wei: the run accrues NO interest. It isolates price risk, and a
    // market that does not exist yet has no borrow rate to accrue at.
    collateral_usd: borrower.collateral_usd, debt_usd: borrower.debt_usd,
    pool_type: v.pool_type, tvl_usd: +v.tvl_usd, A_raw: +v.A_raw, ss_A: +v.ss_A, n_coins: Math.round(+v.n_coins) || 2,
    ...(v.state ? { venue_state: v.state } : {}),
    discount_x_sl: true, pinned_ladder: true,
  };
}

// ---- S.L./D.L. dataset ------------------------------------------------------------
// The evaluated history as the paired format the wasm runner already reads:
// market rows [t, o, h, l, c] + the composed oracle, one row per grid step.
export function toSldlDataset(spec, rt) {
  if (!rt.grid || !rt.oracle) throw new Error("evaluate the oracle first");
  // One row per nominal grid step, whatever the grid holds in between: open = the close before, close = the last value of
  // the step, high / low = the extremes of EVERY point inside it (so a wick of a few blocks is in the candle), and the
  // oracle as it stood at the close.
  const t = rt.grid.t, step = rt.grid.step, a = rt.valid0, last = t.length - 1;
  const from = Math.ceil(t[a] / step) * step, n = Math.floor((t[last] - from) / step) + 1;
  if (!(n >= 50)) throw new Error("not enough history on the grid");
  const rows = new Float64Array(n * 5), orc = new Float64Array(n);
  let i = a, prev = NaN, prevO = NaN;
  while (i <= last && t[i] <= from - step) i++;
  for (let k = 0; k < n; k++) {
    const end = from + k * step;
    let hi = -Infinity, lo = Infinity, c = NaN, o = NaN;
    for (; i <= last && t[i] <= end; i++) {
      const m = rt.market[i], x = rt.oracle[i];
      if (Number.isFinite(m)) { c = m; if (m > hi) hi = m; if (m < lo) lo = m; }
      if (Number.isFinite(x)) o = x;
    }
    if (!Number.isFinite(c)) c = prev;
    if (!Number.isFinite(o)) o = prevO;
    const open = Number.isFinite(prev) ? prev : c;
    rows.set([end, open, Math.max(open, c, hi), Math.min(open, c, lo), c], k * 5);
    orc[k] = o; prev = c; prevO = o;
  }
  const head = (f64, count) => {
    const buf = new ArrayBuffer(8 + f64.byteLength);
    new DataView(buf).setBigUint64(0, BigInt(count), true);
    new Float64Array(buf, 8).set(f64);
    return new Uint8Array(buf);
  };
  const key = "nl-" + Date.now().toString(36);
  const label = `${spec.collateral.symbol || "collateral"} (${spec.meta.name || "custom"})`;
  return { marketBytes: head(rows, n), oracleBytes: head(orc, n),
    meta: { format: "llamma-v2-paired-f64-v1", key, label, symbol: label, pool_name: "new-llamalend composed oracle",
      market_file: key + ".market.bin", oracle_file: key + ".oracle.bin", n, from, to: from + (n - 1) * step,
      cadence_s: step, warmup_rows: 0, oracle_mode: "new-llamalend-script" } };
}

let factoryP = null;
// the engine is cached immutable, so its URLs carry the build (wasm_v)
function engineFactory(mod, v) {
  if (!factoryP) factoryP = new Promise((res, rej) => {
    if (globalThis.RefModelV2) return res(mod.versionedFactory(globalThis.RefModelV2, v));
    const s = document.createElement("script");
    s.src = "/wasm/ref_model_v2.js?v=" + v;
    s.onload = () => res(mod.versionedFactory(globalThis.RefModelV2, v));
    s.onerror = () => { factoryP = null; rej(new Error("failed to load the wasm engine")); };
    document.head.appendChild(s);
  });
  return factoryP;
}

// Runs the v2 sweep in the page. The wasm build can, rarely, freeze mid-run
// (thread-pool race in the Emscripten glue); a watchdog turns that into one
// automatic retry instead of a spinner that never ends.
export async function runSldl(spec, rt, onTick) {
  if (!globalThis.crossOriginIsolated || typeof SharedArrayBuffer === "undefined")
    throw new Error("this browser context cannot run the wasm engine (needs cross-origin isolation)");
  const payload = await api.sldlSources();
  const mod = await import("/wasm/sldl_client.js?v=" + ((payload.client && payload.client.js_v) || 0));
  const s = spec.sldl;
  for (let attempt = 1; attempt <= 2; attempt++) {
    const ds = toSldlDataset(spec, rt);
    const runner = mod.createRunner({
      fetchBin: async name => name === ds.meta.market_file ? ds.marketBytes : ds.oracleBytes,
      fetchJson: async () => ({}), factory: () => engineFactory(mod, (payload.client && payload.client.wasm_v) || 0),
      threads: Math.min(navigator.hardwareConcurrency || 8, 16) });
    const params = { mode: "table", model: "v2", a_min: +s.a_min, a_max: +s.a_max, fee_min: +s.fee_min, fee_max: +s.fee_max,
      grid: +s.grid, method: "exact", bands: +s.bands, tail_pct: +s.tail_pct, loan_days: +s.loan_days,
      oracle_hl: +spec.params.ma_exp_time || 866, source: ds.meta.key, realities: 1 };
    let last = Date.now(), timer = null;
    try {
      return await Promise.race([
        runner.run(params, ds.meta, (done, total, label) => { last = Date.now(); onTick && onTick(done, total, label, attempt); }),
        new Promise((_, rej) => { timer = setInterval(() => { if (Date.now() - last > 45000) rej(new Error("stalled")); }, 2000); }),
      ]);
    } catch (e) {
      if (String(e.message) !== "stalled" || attempt === 2) throw e.message === "stalled"
        ? new Error("the wasm engine froze twice in a row (known thread race): reload the tab and retry") : e;
    } finally { clearInterval(timer); }
  }
}
