// ui/oracle.js: the oracle workbench of the new-llamalend tab.
//
//   mount(host, ctx, opts) -> { destroy() }
//   opts.part     range | sources | script | graph | chart   (omitted / "all" = all five, stacked)
//   opts.height   canvas height of the chart part (the graph viewport when part === "graph")
//   opts.rows     visible rows of the script editor
//   opts.card     false = render the part without its card frame (the design supplies one)
//   opts.title    replaces the card title of a single part
//   opts.coins    chart part: one thin chart per coin of the collateral's pool above the main chart
//   opts.below    chart part: an element placed right under the chart
//   opts.above    chart part: an element placed over the charts (the crash picker)
//
// Every part talks to the others through the store only, so a design may put
// the chart in one pane and the script in another, or mount a part twice.
import { h, esc, field, selectField, select, seg, button, card, progress, toast, shortAddr } from "./kit.js";
import { PRESETS } from "../core/state.js";
import { api, serverMode } from "../core/api.js";
import { loadSources, evaluateOracle, liveValues, resolveRange, emaOf, packSeries, SMOOTHED_READING, HELD_READING } from "../core/pipeline.js";
import { compile, evaluate, sourcesUsed, refsOf, graphOf, FUNC_DOCS } from "../core/expr.js";
import { toGrid, toGridLinear, lastFinite } from "../core/series.js";
import { lineChart, fmtNum, fmtDateTime } from "../core/charts.js";

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const ADDR_RE = /^0x[0-9a-fA-F]{40}$/;
const PART_ORDER = ["range", "sources", "script", "graph", "chart"];
const TITLES = { range: "History range", sources: "Oracle sources", script: "Oracle script",
  graph: "Dependency graph", chart: "Oracle history" };
const KINDS = [
  { value: "onchain", label: "on-chain read" },
  { value: "curve", label: "Curve pool price" },
  { value: "dataset", label: "local dataset" },
  { value: "const", label: "constant" },
  { value: "upload", label: "CSV upload" },
];
const PAIRED = "llamma-v2-paired-f64-v1";
const TEMPLATE_CAP = 14;

// ---- formatting ----------------------------------------------------------------
const nInt = n => Math.round(n).toLocaleString("en-US");
const dateOf = ts => fmtDateTime(ts).slice(0, 10);
export function fmtVal(x) {
  if (!Number.isFinite(x)) return "–";
  const a = Math.abs(x);
  if (a >= 1e12) return x.toExponential(4);
  if (a >= 1e4) return Math.round(x).toLocaleString("en-US");
  return fmtNum(x, 6);
}
const fmtPct = (x, d = 3) => Number.isFinite(x) ? (x > 0 ? "+" : "") + (x * 100).toFixed(d) + "%" : "–";
const stepLabel = s => s % 86400 === 0 ? s / 86400 + " d" : s % 3600 === 0 ? s / 3600 + " h" : s % 60 === 0 ? s / 60 + " min" : s + " s";
const setText = (el, text) => { if (el.textContent !== text) el.textContent = text; };
const setSelect = (sel, v) => { const s = String(v ?? ""); if (document.activeElement !== sel && sel.value !== s) sel.value = s; };

// ---- names -----------------------------------------------------------------------
const funcNameMemo = new Map();
// a name the script language reserves for a function ("min = 1" does not compile)
function isFuncName(name) {
  if (!funcNameMemo.has(name)) funcNameMemo.set(name, NAME_RE.test(name) && compile(name + " = 1").errors.length > 0);
  return funcNameMemo.get(name);
}
const isReservedName = name => name === "oracle" || name === "market" || isFuncName(name);

// label -> identifier body: lowercase, every other character run becomes "_"
export function toIdent(label, prefix = "s") {
  let s = String(label ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  if (s.length > 28) s = s.slice(0, 28).replace(/_+$/, "");
  if (!s) return "";
  if (/^[0-9]/.test(s)) s = prefix + "_" + s;
  return s;
}
export function uniqueName(base, taken) {
  let name = base, k = 2;
  while (taken.has(name) || isReservedName(name)) name = `${base}_${k++}`;
  return name;
}
export function nameProblem(name, sources, selfId) {
  if (!name) return "A source needs a name: the script refers to it by this identifier.";
  if (!NAME_RE.test(name)) return "Use letters, digits and _ only, not starting with a digit: the script reads the name as an identifier.";
  if (name === "oracle" || name === "market") return `"${name}" is an output name of the script. Pick another name for the source.`;
  if (isFuncName(name)) return `"${name}" is a script function. Pick another name.`;
  if (sources.some(s => s.id !== selfId && s.name === name)) return `Another source is already called "${name}". Names must be unique.`;
  return "";
}
const codeOf = script => String(script || "").split("\n").map(l => l.replace(/#.*$/, "")).join("\n");
const mentions = (script, name) => new RegExp(`(?<![A-Za-z0-9_.])${name}(?![A-Za-z0-9_])`).test(codeOf(script));
// rename an identifier in the code part of every line; comments stay as written
export function renameInScript(script, from, to) {
  const re = new RegExp(`(?<![A-Za-z0-9_.])${from}(?![A-Za-z0-9_])`, "g");
  let count = 0;
  const out = String(script || "").split("\n").map(line => {
    const k = line.indexOf("#"), code = k < 0 ? line : line.slice(0, k), rest = k < 0 ? "" : line.slice(k);
    return code.replace(re, () => { count++; return to; }) + rest;
  }).join("\n");
  return { script: out, count };
}

// ---- on-chain read fields ------------------------------------------------------------
const presetFields = id => {
  const p = PRESETS.find(x => x.id === id) || PRESETS[0];
  return { preset: p.id, sig: p.sig, args: [...p.args], slot: p.slot, rtype: p.rtype, decimals: p.decimals };
};
export function parseArgs(text) {
  const s = String(text ?? "").trim().replace(/^\[/, "").replace(/\]$/, "");
  if (!s) return [];
  return s.split(",").map(x => x.trim().replace(/^["']|["']$/g, "")).filter(x => x !== "")
    .map(x => /^-?\d{1,15}$/.test(x) ? Number(x) : x);
}
const showArgs = a => (Array.isArray(a) ? a : []).join(", ");
const sigArity = sig => {
  const m = /^[A-Za-z_][A-Za-z0-9_]*\((.*)\)$/.exec(String(sig || "").replace(/\s+/g, ""));
  return m ? (m[1] ? m[1].split(",").length : 0) : null;
};

function kindDefaults(kind, ctx = {}) {
  if (kind === "onchain") return { address: "", ...presetFields("price"), raw: false };
  if (kind === "curve") return { pool: "", base: "", quote: "", units: "hour" };
  if (kind === "dataset") return { key: ctx.datasetKey || "", column: "close" };
  if (kind === "const") return { value: 1 };
  return { rows: [] };
}
const BASE_NAME = { onchain: "feed", curve: "pool_px", dataset: "data", const: "k", upload: "csv" };

function summaryOf(s) {
  if (s.kind === "onchain") return `${shortAddr(s.address) || "no address"} · ${s.sig || "no signature"}` +
    `${(s.args || []).length ? " [" + showArgs(s.args) + "]" : ""}${s.raw ? " · raw integer units" : ""}`;
  if (s.kind === "curve") return `pool ${shortAddr(s.pool) || "?"} · ${shortAddr(s.base) || "?"} priced in ${shortAddr(s.quote) || "?"} · ${s.units || "hour"} candles`;
  if (s.kind === "dataset") return `dataset ${s.key || "?"} · ${s.column || "close"}`;
  if (s.kind === "const") return `constant ${s.value}`;
  if (s.kind === "upload") return `${nInt((s.rows || []).length)} uploaded rows`;
  return s.kind || "";
}

// what still keeps this source from loading ("" = ready)
function configProblem(s, datasets) {
  if (s.kind === "onchain") {
    if (!s.address) return "Needs a contract address.";
    if (!ADDR_RE.test(s.address)) return "The address must be 0x followed by 40 hex characters.";
    if (!s.sig) return "Needs a function signature, for example price() or price_oracle(uint256).";
    const n = sigArity(s.sig);
    if (n === null) return "The signature must look like name(type, ...), for example price_oracle(uint256).";
    if (n !== (s.args || []).length) return `${s.sig} takes ${n} argument${n === 1 ? "" : "s"}, ${(s.args || []).length} given.`;
    return "";
  }
  if (s.kind === "curve") {
    for (const [k, label] of [["pool", "pool"], ["base", "base token"], ["quote", "quote token"]]) {
      if (!s[k]) return `Needs the ${label} address. "Inspect pool" lists the coins to click.`;
      if (!ADDR_RE.test(s[k])) return `The ${label} address must be 0x followed by 40 hex characters.`;
    }
    if (s.base.toLowerCase() === s.quote.toLowerCase()) return "Base and quote are the same token: the price would be 1.";
    return "";
  }
  if (s.kind === "dataset") {
    if (!s.key) return "Pick a dataset.";
    if (datasets && !datasets.some(d => d.key === s.key)) return `Dataset "${s.key}" is not on this server.`;
    return "";
  }
  if (s.kind === "const") return Number.isFinite(+s.value) && s.value !== "" && s.value !== null ? "" : "Needs a numeric value.";
  if (s.kind === "upload") return (s.rows || []).length ? "" : "No rows yet: paste a CSV below and press Parse.";
  return `Unknown source kind "${s.kind}".`;
}

// ---- CSV ---------------------------------------------------------------------------------
// unix seconds / milliseconds / micro / nano, or ISO 8601 (no zone = UTC) -> unix seconds
export function parseTime(raw) {
  const s = String(raw ?? "").trim().replace(/^"|"$/g, "").trim();
  if (!s) return NaN;
  if (/^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(s)) {
    let x = Number(s);
    if (x > 1e17) x /= 1e9; else if (x > 1e14) x /= 1e6; else if (x > 1e11) x /= 1e3;
    return x >= 1e8 && x < 1e11 ? Math.round(x) : NaN;
  }
  const m = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?)?\s*(Z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)?$/i.exec(s);
  if (!m) return NaN;
  const [mo, d, hh, mi, ss] = [+m[2], +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0)];
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || hh > 23 || mi > 59 || ss > 59) return NaN;
  let t = Date.UTC(+m[1], mo - 1, d, hh, mi, ss) / 1000;
  const z = m[7];
  if (z && /^[+-]/.test(z)) {
    const digits = z.slice(1).replace(":", "");
    t -= (z[0] === "-" ? -1 : 1) * ((+digits.slice(0, 2)) * 3600 + (+(digits.slice(2, 4) || 0)) * 60);
  }
  return Number.isFinite(t) ? t : NaN;
}
function parseCsvValue(raw, decimalComma) {
  let s = String(raw ?? "").trim().replace(/^"|"$/g, "").replace(/[_\s]/g, "");
  if (decimalComma && /^[-+]?\d+,\d+([eE][-+]?\d+)?$/.test(s)) s = s.replace(",", ".");
  if (!s) return NaN;
  const x = Number(s);
  return Number.isFinite(x) ? x : NaN;
}
function splitCsvLine(line, delim) {
  if (delim === null) {                                   // whitespace-separated
    const tok = line.trim().split(/\s+/);
    if (tok.length >= 3 && /^\d{4}[-/]\d/.test(tok[0]) && /^\d{1,2}:\d{2}/.test(tok[1])) {
      const take = /^(z|utc|gmt)$/i.test(tok[2]) && tok.length >= 4 ? 3 : 2;
      return [tok.slice(0, take).join(" "), ...tok.slice(take)];
    }
    return tok;
  }
  if (!line.includes('"')) return line.split(delim);
  const out = [];
  let cur = "", quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === '"') { if (quoted && line[i + 1] === '"') { cur += '"'; i++; } else quoted = !quoted; }
    else if (c === delim && !quoted) { out.push(cur); cur = ""; }
    else cur += c;
  }
  out.push(cur);
  return out;
}
// parseCsv(text) -> { rows: [[unix_s, value], ...] ascending, one per timestamp (the last
//   wins), lines, skipped, errors: [first few], header: [names] | null, column, delimiter }
export function parseCsv(text) {
  const lines = String(text ?? "").replace(/^﻿/, "").split(/\r\n|\n|\r/);
  const body = [];
  lines.forEach((l, i) => { if (l.trim() && !l.trim().startsWith("#")) body.push([l, i + 1]); });
  const probe = body.slice(0, 25).map(x => x[0]);
  const most = ch => probe.filter(l => l.includes(ch)).length >= Math.max(1, Math.ceil(probe.length * 0.6));
  const delim = !probe.length ? "," : most("\t") ? "\t" : most(";") ? ";" : most(",") ? "," : null;
  const decimalComma = delim !== ",";
  const out = { rows: [], lines: body.length, skipped: 0, errors: [], header: null, column: "", delimiter: delim === null ? "whitespace" : delim };
  let col = 1, first = true;
  const seen = new Map();
  for (const [line, no] of body) {
    const f = splitCsvLine(line, delim).map(x => x.trim());
    const t = parseTime(f[0]);
    if (first) {
      first = false;
      if (!Number.isFinite(t)) {                            // a header row: pick the value column by name
        out.header = f.map(x => x.replace(/^"|"$/g, ""));
        const k = out.header.findIndex((name, i) => i > 0 && /^(value|price|close|px|rate|v|p)$/i.test(name));
        col = k > 0 ? k : 1;
        out.column = out.header[col] || "";
        continue;
      }
    }
    const fail = why => { out.skipped++; if (out.errors.length < 4) out.errors.push(`line ${no}: ${why}`); };
    if (!Number.isFinite(t)) { fail(`cannot read "${f[0].slice(0, 32)}" as a time (use unix seconds, unix milliseconds or an ISO date, UTC)`); continue; }
    if (f.length <= col) { fail("no value column"); continue; }
    const v = parseCsvValue(f[col], decimalComma);
    if (!Number.isFinite(v)) { fail(`cannot read "${f[col].slice(0, 32)}" as a number`); continue; }
    seen.set(t, v);
  }
  out.rows = [...seen.entries()].sort((a, b) => a[0] - b[0]);
  return out;
}

// ---- template builders (pure) --------------------------------------------------------------------
// what to read on a node of the /oracles snapshot; null = nothing priced to read
function readingOf(node, addr) {
  const label = String(node.label || "").trim(), meta = node.meta || {};
  switch (node.type) {
    case "pool":
      if (node.ema_1e18 === undefined || node.ema_1e18 === null) return null;
      return { name: toIdent(label || meta.symbol, "p") || "p_" + addr.slice(2, 6), preset: "price_oracle", lookup: true,
        what: `pool ${label || meta.symbol || shortAddr(addr)}` };
    case "agg":
      if (node.price_1e18 === undefined || node.price_1e18 === null) return null;
      return { name: "agg", preset: "price", what: "stablecoin price aggregator" };
    case "wrapper":
      if (node.price_1e18 === undefined || node.price_1e18 === null) return null;
      return { name: toIdent(label, "w") || "w_" + addr.slice(2, 6), preset: "price", what: `oracle contract ${label || shortAddr(addr)}` };
    case "vault":
      return { name: toIdent(label || meta.symbol, "v") || "v_" + addr.slice(2, 6), preset: "erc4626",
        what: `ERC4626 vault ${label || meta.symbol || shortAddr(addr)}: assets per share` };
    case "chainlink": {
      if (/sequencer|uptime/i.test(label + " " + (meta.description || ""))) return null;   // a 0/1 flag, not a price
      const dec = Number.isFinite(+meta.decimals) && meta.decimals !== null ? +meta.decimals : 8;
      return { name: toIdent(label || meta.description, "cl") || "cl_" + addr.slice(2, 6), preset: "chainlink",
        fields: { decimals: dec }, what: `Chainlink feed ${label || meta.description || shortAddr(addr)}` };
    }
    default: return null;                                   // unknown, adapter, chainlink-agg
  }
}
// marketDrafts(market) -> { drafts: [{src, type, label, what, lookup}], skipped, ignored }
// root first, then the contracts it reaches, nearest first (breadth-first over refs)
export function marketDrafts(market, cap = TEMPLATE_CAP) {
  const nodes = {};
  for (const [a, n] of Object.entries((market && market.nodes) || {})) nodes[a.toLowerCase()] = n || {};
  const root = String((market && market.root) || "").toLowerCase();
  const order = [], seen = new Set([root]), queue = [root];
  while (queue.length) {
    const a = queue.shift(), n = nodes[a];
    if (!n) continue;
    if (a !== root) order.push(a);
    for (const r of Object.values(n.refs || {})) {
      const k = String(r || "").toLowerCase();
      if (k && !seen.has(k)) { seen.add(k); queue.push(k); }
    }
  }
  for (const a of Object.keys(nodes)) if (!seen.has(a)) { seen.add(a); order.push(a); }
  const drafts = [], taken = new Set();
  let skipped = 0, ignored = 0;
  const add = (address, node, r) => {
    const name = uniqueName(r.name, taken);
    taken.add(name);
    drafts.push({ type: r.type || node.type || "unknown", label: node.label || "", what: r.what, lookup: !!r.lookup,
      src: { name, kind: "onchain", address, ...presetFields(r.preset), ...(r.fields || {}), raw: false, note: r.what } });
  };
  if (ADDR_RE.test(root)) {
    const n = nodes[root] || {};
    add(root, n, { type: "root", name: "root", preset: "price", what: `the market's oracle contract${n.label ? " " + n.label : ""}: what LLAMMA reads` });
  }
  for (const a of order) {
    if (!ADDR_RE.test(a)) { ignored++; continue; }
    const r = readingOf(nodes[a], a);
    if (!r) { ignored++; continue; }
    if (drafts.length >= cap) { skipped++; continue; }
    add(a, nodes[a], r);
  }
  return { drafts, skipped, ignored };
}
function applyPoolLookup(draft, info) {
  Object.assign(draft.src, presetFields(info.price_oracle_takes_index ? "price_oracle_i" : "price_oracle"));
  if (info.price_oracle_takes_index && info.n_coins > 2)
    draft.src.note = `${draft.what}: price_oracle(0), coin 1 in coin 0; change the argument for the other coins`;
}
function guessPoolSig(draft, node) {
  const n = +((node.meta || {}).n_coins) || 0;
  Object.assign(draft.src, presetFields(n > 2 ? "price_oracle_i" : "price_oracle"));
  draft.src.note = `${draft.what}: pool lookup failed, signature guessed. Check it against the pool.`;
}
const readText = src => `${src.sig}${(src.args || []).length ? " [" + showArgs(src.args) + "]" : ""}` +
  `${src.slot ? ", word " + src.slot : ""}${src.decimals !== 18 ? ", " + src.decimals + " decimals" : ""}`;
function marketScript(market, chain, drafts) {
  const out = [`# ${market.market || "market"} (${market.group || "lending"}, ${chain}): the deployed oracle contract, read as it is`,
    `oracle = ${drafts[0].src.name}`];
  const rest = drafts.slice(1);
  if (rest.length) {
    const w = Math.max(...rest.map(d => d.src.name.length));
    out.push("", "# its building blocks are in the source list; name one in a line and Load fetches it:");
    for (const d of rest) out.push(`#   ${d.src.name.padEnd(w)}  ${d.what}, ${readText(d.src)}`);
  }
  return out.join("\n");
}

// the StableSwap-NG LP oracle from api.pool(): A and the script lines
export function lpPlan(info, coinIndex, emaTime) {
  if (!info) return { error: "Inspect the pool first." };
  if (info.family !== "stableswap")
    return { error: "This is a cryptoswap pool. lp_stable() models the 2-coin StableSwap invariant only; read lp_price() with an on-chain source instead." };
  if (info.n_coins !== 2)
    return { error: `This pool has ${info.n_coins} coins. lp_stable() is the 2-coin lp_oracle math; it does not apply here.` };
  const rawA = info.A_precise !== null && info.A_precise !== undefined ? info.A_precise / 100 : info.A;
  if (!(rawA > 0)) return { error: "The pool reports no amplification A." };
  const A = +(+rawA).toFixed(2), T = +emaTime > 0 ? +emaTime : 866, tail = coinIndex === 1 ? " / {p}" : "";
  return { A, T, takesIndex: !!info.price_oracle_takes_index,
    oracleExpr: `lp_stable(asym_ema({vp}, ${T}), {p}, ${A})${tail}`, marketExpr: `lp_stable({vp}, {p}, ${A})${tail}` };
}
const fillExpr = (expr, vp, p) => expr.replace(/\{vp\}/g, vp).replace(/\{p\}/g, p);

async function mapLimit(items, width, fn) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(width, items.length) }, async () => {
    while (next < items.length) { const i = next++; await fn(items[i], i); }
  }));
}

// ---- store helpers shared by the parts ---------------------------------------------------------------
// Live values are a snapshot of the sources at one block. When the script or the source list
// changes, the script side of that snapshot is recomputed here from the same source values
// (pipeline.liveValues does the identical evaluation after its read), so the graph never
// shows a live number that belongs to an older version of a line.
function refreshLiveVars(store) {
  const rt = store.rt;
  if (!rt.liveVars) return;
  const env = {};
  for (const s of store.spec.oracle.sources) {
    const d = rt.sources[s.id], x = s.kind === "const" ? +s.value : d ? d.live : NaN;
    if (s.name && Number.isFinite(x)) env[s.name] = Float64Array.of(x);
  }
  const { vars } = evaluate(compile(store.spec.oracle.script), env, { t: Float64Array.of(0), step: 1 });
  const live = {};
  for (const [k, v] of Object.entries(vars)) {
    let x = v instanceof Float64Array ? v[0] : v;
    if (x > 1e9) x /= 1e18;
    live[k] = x;
  }
  rt.liveVars = live;                                      // published by the evaluation that follows
}
function evaluateSafe(store) {
  try { refreshLiveVars(store); evaluateOracle(store); }
  catch (e) { toast("The script could not be evaluated: " + (e && e.message ? e.message : e), "error"); }
}
// A loaded series whose settings changed keeps showing until the next Load; the flag sits on
// the runtime entry, which loadSources replaces, so it is visible to every mounted part.
function flagStale(store, id) {
  const d = store.rt.sources[id];
  if (d && d.status === "ok" && !d.stale) { d.stale = true; return true; }
  return false;
}
function usedBy(store) { return sourcesUsed(compile(store.spec.oracle.script)); }
function missingOf(store, used) {
  const rt = store.rt;
  if (Array.isArray(rt.missing)) return rt.missing;
  return store.spec.oracle.sources.filter(s => s.name && used.has(s.name) && !(rt.sources[s.id] && rt.sources[s.id].status === "ok")).map(s => s.name);
}
async function loadWithFeedback(store, { force = false, all = false } = {}) {
  if (store.rt.busy.sources) return;
  const used = usedBy(store);
  const todo = store.spec.oracle.sources.filter(s => s.name && (all || used.has(s.name)));
  if (!todo.length) {
    evaluateSafe(store);
    toast(store.spec.oracle.sources.length
      ? "Nothing to load: the script reads none of the sources. Use a source name in the script, or tick \"also load sources the script does not read\"."
      : "Nothing to load: add a source first.", "error");
    return;
  }
  try {
    await loadSources(store, { force, all });
    let cleared = false;
    for (const s of todo) { const d = store.rt.sources[s.id]; if (d && d.stale && d.status === "ok") { delete d.stale; cleared = true; } }
    if (cleared) store.emit("oracle.data");
    const failed = todo.filter(s => (store.rt.sources[s.id] || {}).status === "error");
    if (failed.length) toast(`${failed.length} of ${todo.length} sources failed to load (${failed.map(s => s.name).join(", ")}). The source list shows the reason for each.`, "error");
    else if (!store.rt.oracle) toast("The sources loaded, but the script produced no series. Check the messages under the script.", "error");
  } catch (e) {
    store.setBusy("sources", null);
    toast("Loading failed: " + (e && e.message ? e.message : e), "error");
  }
}
function frameScheduler(fn) {
  let id = 0, dead = false;
  const run = () => { id = 0; if (!dead) fn(); };
  const schedule = () => { if (!id && !dead) id = requestAnimationFrame(run); };
  schedule.cancel = () => { dead = true; if (id) cancelAnimationFrame(id); id = 0; };
  return schedule;
}
const checkbox = (label, checked, onChange, title) => {
  const input = h("input", { type: "checkbox", checked: checked ? true : null });
  input.addEventListener("change", () => onChange(input.checked));
  const el = h("label", { class: "nl-oracle-check", title: title || null }, input, h("span", {}, label));
  el.input = input;
  el.set = v => { input.checked = !!v; };
  return el;
};

// =====================================================================================================
// part: range
// =====================================================================================================
const DAYS = [30, 90, 180, 365, 730];
const STEPS = [[300, "5 min"], [900, "15 min"], [3600, "1 h"], [14400, "4 h"], [86400, "1 d"]];

function rangePart(store) {
  let custom = !DAYS.includes(+store.spec.oracle.range.days), loadAll = false, dead = false;

  const changeRange = fn => {
    store.update("oracle.range", s => fn(s.oracle.range));
    for (const s of store.spec.oracle.sources) if (s.kind !== "const") flagStale(store, s.id);
    evaluateSafe(store);                                   // regrids what is loaded; fetching waits for Load
  };
  const rolling = (r, days) => { r.days = days; delete r.from; delete r.to; };

  const daysSeg = seg([...DAYS.map(d => ({ value: d, label: d + " d" })), { value: "custom", label: "custom", hint: "any number of days" }],
    custom ? "custom" : +store.spec.oracle.range.days, v => {
      if (v === "custom") { custom = true; paint(); customF.input.focus(); customF.input.select(); return; }
      custom = false;
      changeRange(r => rolling(r, +v));
    });
  const customF = field({ label: "length", type: "number", unit: "d", min: 0.1, max: 3650, value: store.spec.oracle.range.days,
    hint: "history length in days, counted back from the last completed grid step",
    onChange: x => changeRange(r => rolling(r, x)) });
  customF.classList.add("nl-oracle-custom");
  const stepSeg = seg(STEPS.map(([v, l]) => ({ value: v, label: l })), +store.spec.oracle.range.step_s,
    v => changeRange(r => { r.step_s = +v; }));

  const cost = h("div", { class: "nl-note nl-oracle-cost", hidden: true });
  const fixedText = h("span", {});
  const fixed = h("div", { class: "nl-oracle-hint", hidden: true }, fixedText,
    button("Use a rolling window", { small: true, onClick: () => changeRange(r => rolling(r, +r.days || 365)) }));

  const loadBtn = button("Load & evaluate", { kind: "primary", onClick: () => loadWithFeedback(store, { all: loadAll }),
    title: "sample every source the script reads over this range, then evaluate the script on one shared grid" });
  const reloadBtn = button("Reload", { onClick: () => loadWithFeedback(store, { force: true, all: loadAll }),
    title: "fetch every source again, also the ones already loaded in this session (the server still answers past windows from its disk cache)" });
  const liveBtn = button("Live values", { onClick: runLive,
    title: "read every on-chain source at the latest block and evaluate the script on those values" });
  const allBox = checkbox("also load sources the script does not read", loadAll, v => { loadAll = v; },
    "off: Load fetches only what the script references. On: every source is fetched, so any of them can be overlaid in the chart.");
  const prog = progress();
  const state = h("div", { class: "nl-oracle-state" });
  const stale = h("div", { class: "nl-oracle-hint", hidden: true },
    "The range or a source changed after the last load. Press Load & evaluate to fetch what is missing.");
  // on a server that never reads the chain (rt.public) there is nothing these could do
  const actions = h("div", { class: "nl-oracle-actions", hidden: !!store.rt.public }, loadBtn, reloadBtn, liveBtn, allBox);

  async function runLive() {
    if (store.rt.busy.live) return;
    store.setBusy("live", { label: "reading live values", done: 0, total: 1 });
    try {
      await liveValues(store);
      const rt = store.rt;
      toast(rt.liveBlock ? `Live values read at block ${nInt(rt.liveBlock)}.` : "Live values evaluated (no on-chain source to read).", "good");
    } catch (e) {
      toast("Live values failed: " + (e && e.message ? e.message : e) + ". Check the server's RPC for this chain.", "error");
    } finally { store.setBusy("live", null); }
  }

  function paint() {
    if (dead) return;
    const r = store.spec.oracle.range, rr = resolveRange(store.spec);
    if (!DAYS.includes(+r.days)) custom = true;
    daysSeg.set(custom ? "custom" : +r.days);
    stepSeg.set(+r.step_s);
    customF.hidden = !custom;
    customF.set(r.days);
    const n = Math.max(0, Math.floor((rr.to - rr.from) / rr.step) + 1);
    const used = usedBy(store);
    const onchain = store.spec.oracle.sources.filter(s => s.kind === "onchain" && used.has(s.name)).length;
    cost.hidden = true;                                    // on-chain history is block-exact and read where blocks changed something: the grid step no longer sets its cost
    fixed.hidden = !(r.from && r.to);
    if (!fixed.hidden) setText(fixedText, `This spec pins a fixed window (${fmtDateTime(rr.from)} .. ${fmtDateTime(rr.to)} UTC). Picking a length switches back to a window that ends now.`);
    paintState();
  }

  function paintState() {
    if (dead) return;
    const rt = store.rt, spec = store.spec, busy = rt.busy.sources, live = rt.busy.live;
    loadBtn.disabled = reloadBtn.disabled = !!busy;
    liveBtn.disabled = !!busy || !!live;
    setText(liveBtn, live ? "Reading…" : "Live values");
    for (const s of [daysSeg, stepSeg]) s.querySelectorAll("button").forEach(b => { b.disabled = !!busy; });
    customF.input.disabled = !!busy;
    if (busy) {
      let part = 0;
      for (const s of spec.oracle.sources) { const d = rt.sources[s.id]; if (d && d.status === "loading") part += d.progress || 0; }
      prog.set(busy.total ? Math.min(1, (busy.done + part) / busy.total) : 0, `${busy.label}: ${busy.done} / ${busy.total}`);
    } else prog.set(null);

    const used = usedBy(store), lines = [];
    let ok = 0, err = 0, edited = 0, staleUsed = false;
    for (const s of spec.oracle.sources) {
      const d = rt.sources[s.id];
      if (d && d.status === "ok") { ok++; if (d.stale) { edited++; if (used.has(s.name)) staleUsed = true; } }
      else if (d && d.status === "error") err++;
    }
    if (err) lines.push(`${err} source${err > 1 ? "s" : ""} failed to load: see the source list under "Build or change the oracle".`);
    state.replaceChildren(...lines.map(l => h("div", {}, l)));
    stale.hidden = !staleUsed || !!busy;
  }

  paint();
  return {
    body: [
      h("div", { class: "nl-oracle-range-row" },
        h("div", { class: "nl-oracle-ctl" }, h("span", { class: "nl-label" }, "history length"), daysSeg),
        customF,
        h("div", { class: "nl-oracle-ctl" }, h("span", { class: "nl-label" }, "grid step"), stepSeg)),
      cost, fixed,
      actions,
      prog, stale, state,
    ],
    onTag(tag) {
      if (tag === "oracle.range" || tag === "oracle.sources" || tag === "oracle.script") paint();
      else if (tag === "busy" || tag === "oracle.data") paintState();
      else if (tag === "mode") actions.hidden = !!store.rt.public;
    },
    destroy() { dead = true; },
  };
}

// =====================================================================================================
// part: sources
// =====================================================================================================
// opts.collapsed: rows start as one-line summaries
function sourcesPart(store, opts = {}) {
  let dead = false, used = usedBy(store), pendingFocus = null, panel = null;
  let datasets = null, datasetsErr = "", datasetsAsked = false;
  const rows = new Map();                                  // "id:kind" -> row
  const stash = new Map();                                 // "id:kind" -> fields, so a kind switch can be undone
  const poolMemo = new Map();                              // "chain:address" -> Promise(pool info)

  const fetchPool = (chain, address) => {
    const key = chain + ":" + address.toLowerCase();
    if (!poolMemo.has(key)) poolMemo.set(key, api.pool(chain, address).catch(e => { poolMemo.delete(key); throw e; }));
    return poolMemo.get(key);
  };
  function askDatasets() {
    if (datasetsAsked) return;
    datasetsAsked = true;
    api.sldlSources().then(p => { datasets = (p && p.sources) || []; datasetsErr = ""; })
      .catch(e => { datasets = null; datasetsErr = String(e && e.message ? e.message : e); datasetsAsked = false; })
      .finally(() => { if (!dead) for (const r of rows.values()) r.sync(); });
  }

  // ---- list edits ------------------------------------------------------------------------------
  function addSource(kind) {
    let newId = null;
    store.update("oracle.sources", spec => {
      const taken = new Set(spec.oracle.sources.map(s => s.name));
      newId = store.nextSourceId();
      spec.oracle.sources.push({ id: newId, name: uniqueName(BASE_NAME[kind] || "src", taken), kind,
        ...kindDefaults(kind, { datasetKey: datasets && datasets[0] ? datasets[0].key : "" }), note: "" });
    });
    const row = [...rows.values()].find(r => r.id === newId);
    if (row) { row.expand(true); row.el.scrollIntoView({ block: "nearest" }); row.focusName(); }
  }
  function duplicateSource(id) {
    store.update("oracle.sources", spec => {
      const i = spec.oracle.sources.findIndex(x => x.id === id);
      if (i < 0) return;
      const copy = JSON.parse(JSON.stringify(spec.oracle.sources[i]));
      copy.id = store.nextSourceId();
      copy.name = uniqueName(copy.name || "src", new Set(spec.oracle.sources.map(s => s.name)));
      spec.oracle.sources.splice(i + 1, 0, copy);
    });
  }
  function deleteSource(id) {
    store.update("oracle.sources", spec => { spec.oracle.sources = spec.oracle.sources.filter(x => x.id !== id); });
    delete store.rt.sources[id];
    for (const k of [...stash.keys()]) if (k.startsWith(id + ":")) stash.delete(k);
    evaluateSafe(store);
  }
  function changeKind(id, to) {
    pendingFocus = id;
    store.update("oracle.sources", spec => {
      const i = spec.oracle.sources.findIndex(x => x.id === id);
      if (i < 0 || spec.oracle.sources[i].kind === to) return;
      const { id: _id, name, kind, note, ...rest } = spec.oracle.sources[i];
      stash.set(id + ":" + kind, rest);
      const fields = stash.get(id + ":" + to) || kindDefaults(to, { datasetKey: datasets && datasets[0] ? datasets[0].key : "" });
      spec.oracle.sources[i] = { id, name, kind: to, ...JSON.parse(JSON.stringify(fields)), note: note || "" };
    });
    delete store.rt.sources[id];
    evaluateSafe(store);
  }
  function renameSource(id, to) {
    let renamed = 0, from = "";
    store.update("oracle.sources", spec => {
      const s = spec.oracle.sources.find(x => x.id === id);
      if (!s) return;
      from = s.name;
      s.name = to;
      const script = spec.oracle.script || "";
      // keep the script wired: only when the old name really was this source and the new one is free
      if (from && NAME_RE.test(from) && sourcesUsed(compile(script)).has(from) && !mentions(script, to)
          && !spec.oracle.sources.some(x => x.id !== id && x.name === from)) {
        const r = renameInScript(script, from, to);
        if (r.count) { spec.oracle.script = r.script; renamed = r.count; }
      }
    });
    if (renamed) {
      store.emit("oracle.script");
      toast(`Renamed ${from} to ${to} in the script (${renamed} place${renamed > 1 ? "s" : ""}).`);
    }
    evaluateSafe(store);
  }

  // ---- one source row ----------------------------------------------------------------------------
  function makeRow(src, expanded) {
    const id = src.id, kind = src.kind, syncers = [];
    let rowDead = false, confirmTimer = null, patchSig = "", open = !!expanded;
    const get = () => store.spec.oracle.sources.find(s => s.id === id);
    const edit = (fn, o = {}) => {
      store.update("oracle.sources", spec => { const s = spec.oracle.sources.find(x => x.id === id); if (s) fn(s, spec); });
      if (o.stale !== false && flagStale(store, id)) store.emit("oracle.data");
    };
    const bindField = (f, read) => { syncers.push(s => f.set(read(s))); return f; };
    const bindSelect = (f, read) => { syncers.push(s => setSelect(f.input, read(s))); return f; };

    // head
    const toggle = h("button", { type: "button", class: "nl-oracle-src-toggle", "aria-expanded": String(open), title: "show or hide the settings of this source",
      onClick: () => { expand(!open); paintHead(); } });
    const nameIn = h("input", { class: "nl-input nl-mono nl-oracle-src-nameinput", type: "text", value: src.name || "", placeholder: "name",
      spellcheck: "false", autocomplete: "off", autocapitalize: "off", "aria-label": "source name", title: "the identifier the script uses for this source" });
    const nameErr = h("div", { class: "nl-err", hidden: true });
    const checkName = () => {
      const e = nameProblem(nameIn.value.trim(), store.spec.oracle.sources, id);
      nameIn.classList.toggle("nl-bad", !!e);
      nameErr.hidden = !e;
      setText(nameErr, e);
      return e;
    };
    const commitName = () => {
      if (checkName()) return;
      const v = nameIn.value.trim(), cur = get();
      if (cur && cur.name !== v) renameSource(id, v);
    };
    nameIn.addEventListener("input", checkName);
    nameIn.addEventListener("change", commitName);
    nameIn.addEventListener("keydown", e => {
      if (e.key === "Enter") { commitName(); nameIn.blur(); }
      else if (e.key === "Escape") { const cur = get(); nameIn.value = cur ? cur.name || "" : ""; checkName(); nameIn.blur(); }
    });
    const kindSel = select(KINDS, kind, v => changeKind(id, v), "nl-oracle-src-kind");
    kindSel.setAttribute("aria-label", "source kind");
    const statusEl = h("span", { class: "nl-badge nl-oracle-status" }, "idle");
    const spanEl = h("span", { class: "nl-oracle-src-span nl-mono" });
    const usedEl = h("span", { class: "nl-badge nl-oracle-used" });
    const liveEl = h("span", { class: "nl-oracle-live nl-mono" });
    const dupBtn = button("Duplicate", { small: true, kind: "ghost", title: "copy this source under a new name", onClick: () => duplicateSource(id) });
    const delBtn = button("Delete", { small: true, kind: "danger", title: "remove this source", onClick: () => {
      if (delBtn.dataset.armed) { clearTimeout(confirmTimer); deleteSource(id); return; }
      delBtn.dataset.armed = "1";
      delBtn.textContent = "Confirm delete";
      delBtn.classList.add("nl-oracle-armed");
      confirmTimer = setTimeout(() => { delete delBtn.dataset.armed; delBtn.textContent = "Delete"; delBtn.classList.remove("nl-oracle-armed"); }, 3000);
    } });
    const head = h("div", { class: "nl-oracle-src-head" }, toggle, nameIn, kindSel,
      h("div", { class: "nl-oracle-src-chips" }, statusEl, usedEl, spanEl, liveEl),
      h("div", { class: "nl-oracle-src-actions" }, dupBtn, delBtn));
    const summaryEl = h("div", { class: "nl-oracle-src-summary nl-mono" });
    const warnEl = h("div", { class: "nl-oracle-src-warn", hidden: true });
    const errEl = h("div", { class: "nl-err nl-oracle-src-err", hidden: true });

    // body, by kind
    const grid = h("div", { class: "nl-grid" });
    const extras = [];
    let afterSync = () => {};

    if (kind === "onchain") {
      const presetOptions = PRESETS.map(p => ({ value: p.id, label: p.label }));
      const decF = bindField(field({ label: "decimals", type: "number", min: 0, max: 77, value: src.decimals ?? 18,
        hint: "the integer is divided by 10^decimals; ignored while \"raw integer units\" is on",
        onChange: x => edit(s => { s.decimals = Math.round(x); }) }), s => s.decimals ?? 18);
      grid.append(
        bindField(field({ label: "contract address", mono: true, wide: true, value: src.address || "", placeholder: "0x…",
          onChange: v => edit(s => { s.address = v; }) }), s => s.address || ""),
        bindSelect(selectField({ label: "preset", options: presetOptions, value: PRESETS.some(p => p.id === src.preset) ? src.preset : "custom",
          hint: "fills signature, arguments, return word, type and decimals; all of them stay editable",
          onChange: v => edit(s => { s.preset = v; if (v !== "custom") Object.assign(s, presetFields(v)); }) }),
          s => PRESETS.some(p => p.id === s.preset) ? s.preset : "custom"),
        bindField(field({ label: "signature", mono: true, value: src.sig || "", placeholder: "price_oracle(uint256)",
          hint: "canonical ABI signature; argument types may be uint, int, address or bool",
          onChange: v => edit(s => {
            s.sig = v.replace(/\s+/g, "");
            const p = PRESETS.find(x => x.id === s.preset);
            if (!p || p.id === "custom" || p.sig !== s.sig) s.preset = "custom";
          }) }), s => s.sig || ""),
        bindField(field({ label: "arguments", mono: true, value: showArgs(src.args), placeholder: "none",
          hint: "comma-separated; integers (also 1e18 or 10**18), addresses, true / false",
          onChange: v => edit(s => { s.args = parseArgs(v); }) }), s => showArgs(s.args)),
        bindField(field({ label: "return word", type: "number", min: 0, max: 63, value: src.slot ?? 0,
          hint: "which 32-byte word of the return data holds the value; latestRoundData() answers in word 1",
          onChange: x => edit(s => { s.slot = Math.round(x); }) }), s => s.slot ?? 0),
        bindSelect(selectField({ label: "type", options: [{ value: "uint", label: "uint256" }, { value: "int", label: "int256 (signed)" }],
          value: src.rtype || "uint", onChange: v => edit(s => { s.rtype = v; }) }), s => s.rtype || "uint"),
        decF);
      const rawBox = checkbox("raw integer units", !!src.raw, v => edit(s => { s.raw = v; }),
        "keep the on-chain integer as it is (no division by 10^decimals), so Vyper-style lines such as a * b // 10**18 paste unchanged");
      syncers.push(s => { if (document.activeElement !== rawBox.input) rawBox.set(s.raw); decF.classList.toggle("nl-oracle-off", !!s.raw); });
      extras.push(rawBox);

      // the contract's verified ABI (Sourcify, then Blockscout): pick the function instead of typing its signature
      let abi = null, abiFor = "", abiBusy = false, abiErr = "";
      const abiNote = h("span", { class: "nl-note" }), fnSel = h("select", { class: "nl-input nl-select nl-mono", "aria-label": "function of the contract" });
      const outSel = h("select", { class: "nl-input nl-select nl-mono", "aria-label": "which return value", hidden: true });
      const abiBox = h("div", { class: "nl-oracle-abi nl-wide" }, h("div", { class: "nl-oracle-abi-h" }, h("span", { class: "nl-label" }, "contract"), abiNote),
        h("div", { class: "nl-oracle-abi-pick" }, fnSel, outSel));
      const numeric = o => /^u?int/.test(o.type);
      const bestWord = fn => {
        const named = fn.outputs.findIndex(o => numeric(o) && /^(answer|price|rate|value|result)$/i.test(o.name));
        if (named >= 0) return named;
        if (fn.name === "latestRoundData" && fn.outputs[1] && numeric(fn.outputs[1])) return 1;
        return Math.max(0, fn.outputs.findIndex(numeric));
      };
      const applyFn = (fn, word) => {
        const k = word === undefined ? bestWord(fn) : word;
        edit(s => {
          const old = Array.isArray(s.args) ? s.args : [];
          s.sig = fn.sig; s.slot = k; s.rtype = /^int/.test((fn.outputs[k] || {}).type || "") ? "int" : "uint";
          s.args = fn.inputs.map((inp, i) => (old[i] !== undefined && old[i] !== "" ? old[i] : inp.type === "address" ? "" : 0));
          const p = PRESETS.find(x => x.sig === s.sig && x.slot === s.slot && JSON.stringify(x.args) === JSON.stringify(s.args));
          s.preset = p ? p.id : "custom";
        });
        // how the integer is scaled: the contract's own decimals() when it has one (Chainlink answers in 8)
        const addr = String((get() || {}).address || "");
        if (abi && abi.functions.some(f => f.sig === "decimals()") && fn.sig !== "decimals()")
          api.call(store.spec.chain, [{ id: "d", to: addr, sig: "decimals()", decimals: 0 }]).then(r => {
            const d = r && r.values ? +r.values.d : NaN, cur = get();
            if (!rowDead && cur && cur.sig === fn.sig && Number.isInteger(d) && d >= 0 && d <= 36 && (cur.decimals ?? 18) !== d) edit(x => { x.decimals = d; });
          }).catch(() => {});
      };
      fnSel.addEventListener("change", () => { const fn = abi && abi.functions.find(f => f.sig === fnSel.value); if (fn) applyFn(fn); });
      outSel.addEventListener("change", () => { const fn = abi && abi.functions.find(f => f.sig === fnSel.value); if (fn) applyFn(fn, +outSel.value); });
      const label = fn => `${fn.name}(${fn.inputs.map(i => `${i.type}${i.name ? " " + i.name : ""}`).join(", ")}) → ${fn.outputs.map(o => o.type).join(", ")}`;
      function paintAbi() {
        const cur = get() || {}, addr = String(cur.address || "").trim();
        const ready = abi && abiFor === addr.toLowerCase();
        setText(abiNote, !ADDR_RE.test(addr) ? "enter the contract address and its functions are listed here"
          : abiBusy ? "reading the verified ABI…"
          : abiErr ? `the ABI could not be read (${abiErr}): type the signature by hand or pick a preset`
          : !ready ? ""
          : !abi.verified ? "not verified on Sourcify or Blockscout: type the signature by hand, or pick a preset"
          : `${abi.name || "contract"} · verified on ${abi.source === "sourcify" ? "Sourcify" : "Blockscout"}${abi.proxy_of ? " · proxy of " + shortAddr(abi.proxy_of) : ""} · ${abi.functions.length} read function${abi.functions.length === 1 ? "" : "s"} a source can call`);
        const fns = ready && abi.verified ? abi.functions : [];
        fnSel.hidden = !fns.length;
        if (fns.length && document.activeElement !== fnSel) {
          const known = fns.some(f => f.sig === cur.sig);
          fnSel.replaceChildren(h("option", { value: "" }, known ? "choose a function…" : cur.sig ? `${cur.sig}: not in this ABI, choose a function…` : "choose a function…"),
            ...fns.map(f => h("option", { value: f.sig }, label(f))));
          fnSel.value = known ? cur.sig : "";
        }
        const fn = fns.find(f => f.sig === cur.sig), words = fn ? fn.outputs.map((o, i) => [o, i]).filter(([o]) => numeric(o)) : [];
        outSel.hidden = words.length < 2;
        if (words.length >= 2 && document.activeElement !== outSel) {
          outSel.replaceChildren(...words.map(([o, i]) => h("option", { value: i }, `return value ${i}: ${o.name || "unnamed"} (${o.type})`)));
          outSel.value = String(cur.slot ?? 0);
        }
      }
      async function readAbi() {
        const addr = String((get() || {}).address || "").trim();
        if (!ADDR_RE.test(addr) || abiBusy || abiFor === addr.toLowerCase()) return paintAbi();
        abiBusy = true; abiErr = ""; paintAbi();
        try { abi = await api.abi(store.spec.chain, addr); abiFor = addr.toLowerCase(); }
        catch (e) { abi = null; abiFor = addr.toLowerCase(); abiErr = String(e && e.message ? e.message : e).slice(0, 120); }
        abiBusy = false;
        if (!rowDead) { paintAbi(); if (abiFor !== String((get() || {}).address || "").trim().toLowerCase()) readAbi(); }
      }
      grid.insertBefore(abiBox, grid.children[1] || null);        // address, then what the contract offers, then the details
      afterSync = () => { if (open) readAbi(); else paintAbi(); };
    }

    if (kind === "curve") {
      let info = null, infoErr = "", infoBusy = false, infoFor = "";
      const box = h("div", { class: "nl-oracle-pool", hidden: true });
      const inspectBtn = button("Inspect pool", { small: true, title: "read the pool's coins and state, then click a coin to make it base or quote", onClick: inspect });
      grid.append(
        bindField(field({ label: "pool address", mono: true, wide: true, value: src.pool || "", placeholder: "0x…",
          onChange: v => edit(s => { s.pool = v; }) }), s => s.pool || ""),
        bindField(field({ label: "base token", mono: true, value: src.base || "", placeholder: "0x…", hint: "the token being priced",
          onChange: v => edit(s => { s.base = v; }) }), s => s.base || ""),
        bindField(field({ label: "quote token", mono: true, value: src.quote || "", placeholder: "0x…", hint: "the token the price is expressed in",
          onChange: v => edit(s => { s.quote = v; }) }), s => s.quote || ""),
        bindSelect(selectField({ label: "candles", options: [{ value: "15min", label: "15 min" }, { value: "hour", label: "1 hour" }, { value: "day", label: "1 day" }],
          value: src.units || "hour", hint: "candle size requested from the Curve prices API; the close of each candle is used",
          onChange: v => edit(s => { s.units = v; }) }), s => s.units || "hour"));
      async function inspect() {
        const s = get();
        if (!s || infoBusy) return;
        const addr = String(s.pool || "").trim();
        if (!ADDR_RE.test(addr)) { info = null; infoErr = "Enter the pool address first: 0x followed by 40 hex characters."; paintPool(); return; }
        infoBusy = true; infoErr = ""; inspectBtn.disabled = true; paintPool();
        try { info = await fetchPool(store.spec.chain, addr); infoFor = addr.toLowerCase(); }
        catch (e) {
          info = null;
          infoErr = `Could not read a pool at ${shortAddr(addr)} on ${store.spec.chain}: ${e && e.message ? e.message : e}`;
          if (!rowDead) toast(infoErr, "error");
        }
        if (rowDead) return;
        infoBusy = false; inspectBtn.disabled = false; paintPool();
      }
      const pick = (role, address) => edit(s => {
        const other = role === "base" ? "quote" : "base", prev = s[role];
        if (String(s[other] || "").toLowerCase() === address.toLowerCase()) s[other] = prev || "";
        s[role] = address;
      });
      function paintPool() {
        const s = get() || {};
        box.hidden = !(info || infoErr || infoBusy);
        if (box.hidden) return;
        if (infoBusy) { box.replaceChildren(h("div", { class: "nl-note" }, "Reading the pool…")); return; }
        if (!info) { box.replaceChildren(h("div", { class: "nl-err" }, infoErr)); return; }
        const is = (role, c) => String(s[role] || "").toLowerCase() === String(c.address).toLowerCase();
        const stale = infoFor !== String(s.pool || "").trim().toLowerCase();
        const A = info.A_precise !== null && info.A_precise !== undefined && info.family === "stableswap" ? info.A_precise / 100 : info.A;
        box.replaceChildren(...[
          h("div", { class: "nl-oracle-pool-h" }, h("b", {}, info.symbol || info.name || "pool"),
            h("span", { class: "nl-note" }, [info.family, `${info.n_coins} coins`, Number.isFinite(+A) && A !== null ? "A " + +(+A).toFixed(2) : "",
              Number.isFinite(info.virtual_price) ? "virtual price " + fmtNum(info.virtual_price, 6) : "",
              (info.price_oracle || []).length ? "price_oracle " + info.price_oracle.map(x => fmtNum(x, 6)).join(", ") : ""].filter(Boolean).join(" · "))),
          stale ? h("div", { class: "nl-oracle-src-warn" }, "The pool address changed since this was read. Press Inspect pool again.") : null,
          ...info.coins.map((c, i) => h("div", { class: "nl-oracle-coin" },
            h("span", { class: "nl-oracle-coin-sym" }, `${i} · ${c.symbol || "?"}`),
            h("span", { class: "nl-mono nl-dim", title: c.address }, shortAddr(c.address)),
            h("span", { class: "nl-mono nl-oracle-coin-bal", title: "pool balance" }, c.balance === null || c.balance === undefined ? "–" : fmtNum(c.balance)),
            h("span", { class: "nl-seg nl-oracle-coin-pick" },
              h("button", { type: "button", class: is("base", c) ? "on" : "", title: "price this token", onClick: () => pick("base", c.address) }, "base"),
              h("button", { type: "button", class: is("quote", c) ? "on" : "", title: "express the price in this token", onClick: () => pick("quote", c.address) }, "quote")))),
          h("div", { class: "nl-note" }, "The series is the base token priced in the quote token, from the Curve prices API candles of this pool.")].filter(Boolean));
      }
      afterSync = paintPool;
      extras.push(h("div", { class: "nl-row nl-tight" }, inspectBtn), box);
    }

    if (kind === "dataset") {
      askDatasets();
      const keySel = select([], "", v => edit(s => {
        s.key = v;
        const row = (datasets || []).find(d => d.key === v);
        if (s.column === "oracle" && !(row && row.meta && row.meta.format === PAIRED)) s.column = "close";
      }));
      const colSel = select([], "", v => edit(s => { s.column = v; }));
      keySel.addEventListener("blur", () => sync());        // a list that arrived while the menu was open
      const dsNote = h("div", { class: "nl-note" });
      let keySig = "", colSig = "";
      const fill = (sel, options, value) => {
        if (document.activeElement === sel && sel.options.length) return false;   // never under the user's cursor; the next sync retries
        sel.replaceChildren(...options.map(o => h("option", { value: o.value }, o.label)));
        sel.value = value;
        return true;
      };
      syncers.push(s => {
        const list = datasets || [], known = list.some(d => d.key === s.key);
        const opts = list.map(d => ({ value: d.key, label: `${d.label || d.key}${Number.isFinite(+d.days) && d.days !== null ? " · " + nInt(+d.days) + " d" : ""}` }));
        if (!known) opts.unshift({ value: s.key || "", label: s.key ? `${s.key}${datasets ? " (not on this server)" : ""}` : "choose a dataset…" });
        const ks = JSON.stringify(opts);
        if (ks !== keySig) { if (fill(keySel, opts, s.key || "")) keySig = ks; } else setSelect(keySel, s.key || "");
        const row = list.find(d => d.key === s.key), paired = !!(row && row.meta && row.meta.format === PAIRED);
        const cols = ["close", "open", "high", "low", ...(paired || s.column === "oracle" ? ["oracle"] : [])].map(c => ({ value: c, label: c }));
        const cs = JSON.stringify(cols);
        if (cs !== colSig) { if (fill(colSel, cols, s.column || "close")) colSig = cs; } else setSelect(colSel, s.column || "close");
        setText(dsNote, datasetsErr ? `The dataset list could not be read (${datasetsErr}). Is the server's S.L./D.L. data in place?`
          : !datasets ? "Reading the dataset list…"
          : row ? `${row.label || row.key}: ${Number.isFinite(+row.days) && row.days !== null ? nInt(+row.days) + " days of candles" : "span unknown"}${paired ? ", paired with its recorded oracle (column \"oracle\")" : ""}. The whole file is fetched once and cached in the page.`
          : "Local candle files from the S.L./D.L. data menu.");
      });
      grid.append(
        h("label", { class: "nl-field nl-wide" }, h("span", { class: "nl-label" }, "dataset"), h("span", { class: "nl-inwrap" }, keySel)),
        h("label", { class: "nl-field" }, h("span", { class: "nl-label" }, "column"), h("span", { class: "nl-inwrap" }, colSel)));
      extras.push(dsNote);
    }

    if (kind === "const") {
      grid.append(bindField(field({ label: "value", type: "number", value: src.value ?? 1, hint: "a fixed number on the whole grid, for example a peg or a haircut",
        onChange: x => edit(s => { s.value = x; }) }), s => s.value ?? ""));
    }

    if (kind === "upload") {
      const ta = h("textarea", { class: "nl-input nl-oracle-csv", rows: 5, spellcheck: "false", "aria-label": "CSV text",
        placeholder: "timestamp,value\n1726560000,1.0012\n1726563600000,1.0015\n2024-09-17 12:00,1.0019" });
      const msg = h("div", { class: "nl-note" });
      const stored = h("div", { class: "nl-oracle-summary nl-mono" });
      const ingest = (text, origin) => {
        const r = parseCsv(text);
        if (!r.rows.length) {
          msg.className = "nl-err";
          setText(msg, `No rows could be read from the ${origin}. ${r.errors[0] || "Expected one \"timestamp,value\" pair per line."}`);
          return;
        }
        edit(s => { s.rows = r.rows; }, { stale: false });
        delete store.rt.sources[id];                       // the next Load takes the new rows, whatever their count
        evaluateSafe(store);
        msg.className = r.skipped ? "nl-oracle-src-warn" : "nl-note";
        setText(msg, `Read ${nInt(r.rows.length)} rows from the ${origin}` + (r.column ? `, column "${r.column}"` : "") +
          (r.skipped ? `; ${nInt(r.skipped)} line${r.skipped > 1 ? "s" : ""} skipped (${r.errors.join("; ")})` : "") +
          `. Press Load & evaluate to use them.` + (r.rows.length > 50000 ? " Series this long may not fit the browser's saved state; export the spec to keep them." : ""));
      };
      const fileIn = h("input", { type: "file", accept: ".csv,.tsv,.txt,text/csv,text/plain" });
      fileIn.addEventListener("change", () => {
        const f = fileIn.files && fileIn.files[0];
        if (!f) return;
        const rd = new FileReader();
        rd.onload = () => { if (!rowDead) ingest(String(rd.result || ""), `file ${f.name}`); fileIn.value = ""; };
        rd.onerror = () => { if (!rowDead) { msg.className = "nl-err"; setText(msg, `The file ${f.name} could not be read.`); } };
        rd.readAsText(f);
      });
      syncers.push(s => {
        const rws = s.rows || [];
        if (!rws.length) { setText(stored, "no rows stored"); return; }
        let lo = Infinity, hi = -Infinity;
        for (const r of rws) { if (r[1] < lo) lo = r[1]; if (r[1] > hi) hi = r[1]; }
        setText(stored, `${nInt(rws.length)} rows stored · ${fmtDateTime(rws[0][0])} .. ${fmtDateTime(rws[rws.length - 1][0])} UTC · values ${fmtVal(lo)} .. ${fmtVal(hi)}`);
      });
      extras.push(
        h("div", { class: "nl-note" }, "One \"timestamp,value\" pair per line; a header row is fine. Timestamps may be unix seconds, unix milliseconds or ISO dates (read as UTC). Comma, semicolon, tab or spaces separate the columns."),
        ta,
        h("div", { class: "nl-row nl-tight" },
          button("Parse pasted text", { small: true, onClick: () => ingest(ta.value, "pasted text") }),
          h("label", { class: "nl-btn nl-small nl-oracle-filebtn", title: "read a .csv file from disk instead of pasting" }, "Read a CSV file", fileIn),
          button("Clear rows", { small: true, kind: "ghost", onClick: () => { edit(s => { s.rows = []; }, { stale: false }); delete store.rt.sources[id]; evaluateSafe(store); ta.value = ""; msg.className = "nl-note"; setText(msg, ""); } })),
        msg, stored);
    }

    if (!grid.childNodes.length && !extras.length) extras.push(h("div", { class: "nl-err" }, `Unknown source kind "${kind}". Pick a kind above.`));
    grid.append(bindField(field({ label: "note", wide: true, value: src.note || "", placeholder: "what this source is",
      onChange: v => edit(s => { s.note = v; }, { stale: false }) }), s => s.note || ""));
    const bodyEl = h("div", { class: "nl-oracle-src-body", hidden: !open }, grid, ...extras);
    const el = h("div", { class: "nl-oracle-src nl-oracle-src-" + kind }, head, summaryEl, nameErr, warnEl, errEl, bodyEl);

    function expand(v) {
      open = !!v;
      if (open && kind === "onchain") queueMicrotask(() => { if (!rowDead) afterSync(); });
      bodyEl.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      summaryEl.hidden = open;
      el.classList.toggle("nl-oracle-src-open", open);
    }
    function sync() {
      const s = get();
      if (!s || rowDead) return;
      if (document.activeElement !== nameIn && !nameIn.classList.contains("nl-bad")) nameIn.value = s.name || "";
      setSelect(kindSel, s.kind);
      for (const fn of syncers) fn(s);
      setText(summaryEl, summaryOf(s) + (s.note ? "  ·  " + s.note : ""));
      const problem = configProblem(s, datasets);
      warnEl.hidden = !problem;
      setText(warnEl, problem);
      afterSync();
      patch();
    }
    function patch() {
      const s = get();
      if (!s || rowDead) return;
      const d = store.rt.sources[id] || { status: "idle" }, isUsed = !!s.name && used.has(s.name);
      let cls = "", text = "idle", title = isUsed ? "not loaded yet: press Load & evaluate" : "the script does not read this source, so Load skips it", span = "", err = "";
      if (d.status === "loading") { cls = "nl-warn"; text = `loading ${Math.round((d.progress || 0) * 100)}%`; title = "sampling the series"; }
      else if (d.status === "ok") {
        cls = d.stale ? "nl-warn" : "nl-good";
        text = (d.const !== undefined ? "ok: constant" : `ok: ${nInt(d.n || 0)} points`) + (d.stale ? " · stale" : "");
        title = d.stale ? "the range or this source's settings changed after the series was loaded: press Load & evaluate to fetch it again" : "loaded";
        if (d.t && d.t.length) span = `${dateOf(d.t[0])} .. ${dateOf(d.t[d.t.length - 1])}`;
      } else if (d.status === "error") { cls = "nl-bad"; text = "error"; title = "loading failed"; err = d.err || "loading failed"; }
      const live = Number.isFinite(d.live) ? "live " + fmtVal(d.live) : "";
      const sig = [cls, text, span, err, live, isUsed, title].join("|");
      if (sig === patchSig) return;
      patchSig = sig;
      statusEl.className = "nl-badge nl-oracle-status" + (cls ? " " + cls : "");
      setText(statusEl, text);
      statusEl.title = title;
      setText(spanEl, span);
      spanEl.hidden = !span;
      setText(liveEl, live);
      liveEl.hidden = !live;
      liveEl.title = live && store.rt.liveBlock ? `at block ${nInt(store.rt.liveBlock)}` : "";
      usedEl.className = "nl-badge nl-oracle-used" + (isUsed ? " nl-oracle-used-yes" : "");
      setText(usedEl, isUsed ? "read by script" : "not read");
      usedEl.title = isUsed ? "the script references this name, so Load fetches it" : "the script does not reference this name";
      el.classList.toggle("nl-oracle-src-used", isUsed);
      errEl.hidden = !err;
      setText(errEl, err ? `Load failed: ${err}` : "");
    }
    expand(open);
    return { id, el, sync, patch, expand, isOpen: () => open, focusName: () => { nameIn.focus(); nameIn.select(); }, focusKind: () => kindSel.focus(),
      destroy() { rowDead = true; clearTimeout(confirmTimer); } };
  }

  // ---- list ---------------------------------------------------------------------------------------
  const listEl = h("div", { class: "nl-oracle-srclist" });
  const emptyEl = h("div", { class: "nl-oracle-empty", hidden: true },
    h("b", {}, "No sources yet"),
    h("div", {}, "A source is one series the script can name: an on-chain read sampled over time, a Curve pool price, a local candle dataset, a constant or a pasted CSV. Add one below, or start from a template."));
  const countEl = h("span", { class: "nl-badge" });
  const foldBtn = button("Collapse all", { small: true, kind: "ghost", onClick: () => {
    const anyOpen = [...rows.values()].some(r => r.isOpen());
    for (const r of rows.values()) r.expand(!anyOpen);
    paintHead();
  } });
  const tplBtn = button("Templates", { small: true, title: "build the source list from an existing market or from a StableSwap-NG pool", onClick: () => togglePanel() });
  const panelHost = h("div", { class: "nl-oracle-tplhost", hidden: true });
  const addRow = h("div", { class: "nl-oracle-add" }, h("span", { class: "nl-label" }, "add a source"),
    ...KINDS.map(k => button(k.label, { small: true, kind: "ghost", onClick: () => addSource(k.value) })));

  function paintHead() {
    const list = store.spec.oracle.sources, n = list.filter(s => s.name && used.has(s.name)).length;
    setText(countEl, `${list.length} source${list.length === 1 ? "" : "s"} · ${n} read by the script`);
    foldBtn.hidden = list.length < 2;
    setText(foldBtn, [...rows.values()].some(r => r.isOpen()) ? "Collapse all" : "Expand all");
  }
  function reconcile() {
    const list = store.spec.oracle.sources, keyOf = s => s.id + ":" + s.kind;
    const want = new Set(list.map(keyOf));
    for (const [key, row] of [...rows]) if (!want.has(key)) { row.destroy(); row.el.remove(); rows.delete(key); }
    const fresh = list.filter(s => !rows.has(keyOf(s))).length;
    let cursor = listEl.firstChild;
    const done = new Set();
    for (const s of list) {
      const key = keyOf(s);
      if (done.has(key)) continue;                          // a malformed spec with a repeated id
      done.add(key);
      let row = rows.get(key);
      if (!row) { row = makeRow(s, !opts.collapsed && fresh <= 4); rows.set(key, row); }
      if (row.el === cursor) cursor = cursor.nextSibling;
      else listEl.insertBefore(row.el, cursor);
      row.sync();
    }
    emptyEl.hidden = list.length > 0;
    paintHead();
    if (pendingFocus) {
      const row = [...rows.values()].find(r => r.id === pendingFocus);
      pendingFocus = null;
      if (row) { row.expand(true); row.focusKind(); }
    }
  }
  const patchAll = frameScheduler(() => { for (const r of rows.values()) r.patch(); });

  // ---- templates ---------------------------------------------------------------------------------------
  function togglePanel(force) {
    const show = force === undefined ? panelHost.hidden : force;
    if (show && !panel) { panel = templatePanel(); panelHost.append(panel.el); }
    panelHost.hidden = !show;
    tplBtn.classList.toggle("nl-oracle-on", show);
    if (show && panel) { panel.shown(); panel.repaint(); }
  }

  // writes the drafts into the spec; replace = start the source list (and script) over
  function commitSources(drafts, { replace, script, chain }) {
    if (replace) {
      store.update("oracle.sources", spec => { spec.oracle.sources = []; });   // every mounted list drops its rows first
      for (const k of Object.keys(store.rt.sources)) delete store.rt.sources[k];
      Object.assign(store.rt, { liveVars: null, liveBlock: null, liveAt: null });
      stash.clear();
    }
    const chainChanged = !!chain && chain !== store.spec.chain;
    store.update("oracle.sources", spec => {
      if (chainChanged) spec.chain = chain;
      const taken = new Set(spec.oracle.sources.map(s => s.name));
      for (const d of drafts) {
        const name = uniqueName(d.src.name, taken);
        taken.add(name);
        d.src = { ...d.src, name };
        spec.oracle.sources.push({ id: store.nextSourceId(), ...JSON.parse(JSON.stringify(d.src)) });
      }
      if (script) spec.oracle.script = script(spec.oracle.script || "", drafts);
    });
    if (script) store.emit("oracle.script");
    if (chainChanged) store.emit("tokens");
    evaluateSafe(store);
  }

  function templatePanel() {
    let mode = "market", panelDead = false;
    const el = h("div", { class: "nl-oracle-tpl" });

    // (1) from an existing market -----------------------------------------------------------------
    const marketPane = (() => {
      let payload = null, selKey = "", chainFilter = "", query = "", busy = false, asked = false;
      const status = h("div", { class: "nl-note" }, "Reading the oracle snapshot of the Curve lending markets…");
      const chainSel = select([{ value: "", label: "all chains" }], "", v => { chainFilter = v; paintList(); });
      chainSel.setAttribute("aria-label", "chain");
      const search = h("input", { class: "nl-input", type: "text", placeholder: "filter: wstETH, sDOLA, 0x…", spellcheck: "false", autocomplete: "off", "aria-label": "filter markets" });
      search.addEventListener("input", () => { query = search.value.trim().toLowerCase(); paintList(); });
      const listBox = h("div", { class: "nl-oracle-mlist", role: "listbox", "aria-label": "markets" });
      const preview = h("div", { class: "nl-oracle-tpl-preview" });
      const prog = progress();
      const replaceBtn = button("Replace sources and script", { kind: "primary", onClick: () => apply(true),
        title: "the source list becomes this market's oracle stack and the script becomes  oracle = root" });
      const appendBtn = button("Append sources only", { onClick: () => apply(false), title: "add these sources to the current list; the script stays as it is" });
      const controls = h("div", { class: "nl-oracle-tpl-controls", hidden: true }, chainSel, search);
      const actions = h("div", { class: "nl-row nl-tight", hidden: true }, replaceBtn, appendBtn);
      const pane = h("div", { class: "nl-oracle-tpl-pane" },
        h("div", { class: "nl-note" }, "Takes the oracle contract of a live Curve lending market and the contracts it reads: pools, aggregators, wrappers, vaults and Chainlink feeds become on-chain sources, the market's own oracle is called root, and the script starts as  oracle = root.  Then rewire it from the parts."),
        status, controls, listBox, preview, actions, prog);

      const marketsOf = () => Object.entries((payload && payload.markets) || {});
      function paintList() {
        const items = marketsOf().filter(([key, m]) => (!chainFilter || m.chain === chainFilter) &&
          (!query || `${m.market || ""} ${m.group || ""} ${m.chain || ""} ${key} ${m.root || ""}`.toLowerCase().includes(query)))
          .sort((a, b) => String(a[1].market || "").localeCompare(String(b[1].market || "")));
        if (!items.some(([k]) => k === selKey)) selKey = "";
        listBox.replaceChildren(...(items.length ? items.map(([key, m]) =>
          h("button", { type: "button", role: "option", class: "nl-oracle-mitem" + (key === selKey ? " on" : ""), "aria-selected": String(key === selKey),
            onClick: () => { selKey = key; paintList(); } },
            h("b", {}, m.market || key), h("span", { class: "nl-note" }, `${m.chain || ""} · ${m.group || ""} · ${Object.keys(m.nodes || {}).length} contracts`)))
          : [h("div", { class: "nl-note nl-oracle-mnone" }, payload ? "No market matches this filter." : "")]));
        paintPreview();
      }
      function paintPreview() {
        const m = payload && payload.markets[selKey];
        actions.hidden = !m;
        if (!m) { preview.replaceChildren(payload ? h("div", { class: "nl-note" }, "Pick a market to see the sources it would create.") : ""); return; }
        const { drafts, skipped, ignored } = marketDrafts(m, TEMPLATE_CAP);
        const chain = m.chain || selKey.split(":")[0], other = chain !== store.spec.chain;
        appendBtn.disabled = busy || other;
        replaceBtn.disabled = busy || !drafts.length;
        appendBtn.title = other ? `this market is on ${chain}; sources of two chains cannot be mixed, so only Replace is possible`
          : "add these sources to the current list; the script stays as it is";
        preview.replaceChildren(...[
          h("div", { class: "nl-oracle-tablewrap" }, h("table", { class: "nl-oracle-table" },
            h("thead", {}, h("tr", {}, h("th", {}, "name"), h("th", {}, "reads"), h("th", {}, "contract"), h("th", {}, "what it is"))),
            h("tbody", {}, drafts.map(d => h("tr", {},
              h("td", { class: "nl-mono" }, d.src.name),
              h("td", { class: "nl-mono" }, d.lookup ? "price_oracle, signature checked on apply" : readText(d.src)),
              h("td", { class: "nl-mono", title: d.src.address }, shortAddr(d.src.address)),
              h("td", {}, d.what)))))),
          h("div", { class: "nl-note" }, `${drafts.length} source${drafts.length === 1 ? "" : "s"}` +
            (skipped ? `; ${skipped} more readable contract${skipped > 1 ? "s" : ""} left out (limit ${TEMPLATE_CAP}, nearest to the root first)` : "") +
            (ignored ? `; ${ignored} contract${ignored > 1 ? "s" : ""} without a price to read (tokens, adapters, feed implementations) skipped` : "") + "."),
          other ? h("div", { class: "nl-oracle-hint" }, `This market lives on ${chain}, the workbench is set to ${store.spec.chain}. Replace also switches the workbench chain to ${chain}, because every on-chain source is read on one chain.`) : null].filter(Boolean));
      }
      async function apply(replace) {
        const m = payload && payload.markets[selKey];
        if (!m || busy) return;
        const chain = m.chain || selKey.split(":")[0];
        const nodes = {};
        for (const [a, n] of Object.entries(m.nodes || {})) nodes[a.toLowerCase()] = n || {};
        const { drafts } = marketDrafts(m, TEMPLATE_CAP);
        const pools = drafts.filter(d => d.lookup);
        busy = true; paintPreview();
        let done = 0, guessed = 0;
        const label = () => `checking the price_oracle signature of each pool: ${done} / ${pools.length}`;
        if (pools.length) prog.set(0, label());
        await mapLimit(pools, 3, async d => {
          try { applyPoolLookup(d, await fetchPool(chain, d.src.address)); }
          catch (_) { guessed++; guessPoolSig(d, nodes[d.src.address.toLowerCase()] || {}); }
          done++;
          if (!panelDead) prog.set(done / pools.length, label());
        });
        if (panelDead) return;
        prog.set(null);
        busy = false;
        commitSources(drafts, { replace, chain: replace ? chain : null, script: replace ? (_old, ds) => marketScript(m, chain, ds) : null });
        toast(`${drafts.length} sources created from ${m.market || selKey}.` +
          (replace ? " The script reads root only: press Load & evaluate, then rewire." : "") +
          (guessed ? ` ${guessed} pool lookup${guessed > 1 ? "s" : ""} failed; those signatures are guesses.` : ""), guessed ? "info" : "good");
        togglePanel(false);
        paintPreview();
      }
      async function ask() {
        if (asked) return;
        asked = true;
        try { payload = await api.oracles(); }
        catch (e) {
          asked = false;
          if (panelDead) return;
          status.className = "nl-err";
          setText(status, `The oracle snapshot could not be read (${e && e.message ? e.message : e}). It is written by fetchers/fetch_oracles.py on the server.`);
          return;
        }
        if (panelDead) return;
        const chains = [...new Set(marketsOf().map(([, m]) => m.chain).filter(Boolean))].sort();
        chainFilter = chains.includes(store.spec.chain) ? store.spec.chain : "";
        chainSel.replaceChildren(h("option", { value: "" }, "all chains"), ...chains.map(c => h("option", { value: c }, c)));
        chainSel.value = chainFilter;
        status.className = "nl-note";
        setText(status, `${marketsOf().length} markets in the snapshot${payload.fetched_at_utc ? " of " + String(payload.fetched_at_utc).slice(0, 16).replace("T", " ") + " UTC" : ""}.`);
        controls.hidden = false;
        paintList();
      }
      return { el: pane, shown: ask, repaint: () => { if (payload) paintPreview(); } };
    })();

    // (2) StableSwap-NG LP oracle from a pool ------------------------------------------------------------
    const lpPane = (() => {
      let info = null, infoFor = "", busy = false, coin = 0, emaTime = 866, typed = false;
      const collateral = () => { const a = (store.spec.collateral && store.spec.collateral.address) || ""; return ADDR_RE.test(a) ? a : ""; };
      let addr = collateral();
      const addrF = field({ label: "pool address", mono: true, wide: true, value: addr, placeholder: "0x…",
        hint: "a 2-coin StableSwap-NG pool; for an LP-token market this is the collateral itself",
        onChange: v => { addr = v; typed = true; paint(); } });
      addrF.input.addEventListener("input", () => { addr = addrF.input.value.trim(); typed = true; paint(); });
      const coinSeg = seg([{ value: 0, label: "coin 0" }, { value: 1, label: "coin 1" }], coin, v => { coin = +v; paint(); });
      const emaF = field({ label: "ema_time", type: "number", unit: "s", min: 1, max: 1e7, value: emaTime,
        hint: "EMA time of the LP oracle's dampened virtual price (the oracle contract's setting, not the pool's ma_exp_time). 866 s is a 10 min half-life.",
        onChange: x => { emaTime = x; paint(); } });
      const inspectBtn = button("Inspect pool", { onClick: inspect });
      const out = h("div", { class: "nl-oracle-tpl-preview" });
      const addBtn = button("Add to sources and script", { kind: "primary", onClick: () => apply(false),
        title: "adds vp and p to the source list and appends the lp line to the script" });
      const replaceBtn = button("Replace sources and script", { onClick: () => apply(true),
        title: "the source list becomes vp and p, the script becomes this LP oracle with a market line" });
      const actions = h("div", { class: "nl-row nl-tight" }, inspectBtn, addBtn, replaceBtn);
      const pane = h("div", { class: "nl-oracle-tpl-pane", hidden: true },
        h("div", { class: "nl-note" }, "Rebuilds StableSwapNGLPOracle for a 2-coin StableSwap-NG pool: the LP token is worth portfolio_value(A, price_oracle) times a virtual price whose rises pass through an EMA while falls count at once. Adds the sources vp and p and the line  lp = lp_stable(asym_ema(vp, ema_time), p, A)."),
        h("div", { class: "nl-grid" }, addrF,
          h("div", { class: "nl-field" }, h("span", { class: "nl-label" }, "price the LP token in"), coinSeg), emaF),
        actions, out);

      async function inspect() {
        if (busy) return;
        if (!ADDR_RE.test(addr)) { info = null; out.replaceChildren(h("div", { class: "nl-err" }, "Enter the pool address first: 0x followed by 40 hex characters.")); return; }
        busy = true; paint();
        try { info = await fetchPool(store.spec.chain, addr); infoFor = addr.toLowerCase(); }
        catch (e) {
          info = null; infoFor = "";
          const text = `Could not read a pool at ${shortAddr(addr)} on ${store.spec.chain}: ${e && e.message ? e.message : e}`;
          if (!panelDead) { toast(text, "error"); busy = false; paint(); out.replaceChildren(h("div", { class: "nl-err" }, text)); }
          return;
        }
        if (panelDead) return;
        busy = false; paint();
      }
      const current = () => info && infoFor === addr.toLowerCase() ? info : null;
      function paint() {
        const inf = current(), plan = inf ? lpPlan(inf, coin, emaTime) : null;
        inspectBtn.disabled = busy;
        setText(inspectBtn, busy ? "Reading the pool…" : "Inspect pool");
        addBtn.disabled = replaceBtn.disabled = busy || !plan || !!plan.error;
        coinSeg.querySelectorAll("button").forEach((b, i) => {
          setText(b, inf && inf.coins[i] ? `coin ${i} · ${inf.coins[i].symbol || "?"}` : `coin ${i}`);
        });
        if (!inf) { out.replaceChildren(h("div", { class: "nl-note" }, busy ? "" : "Press Inspect pool to read its coins and amplification.")); return; }
        if (plan.error) { out.replaceChildren(h("div", { class: "nl-err" }, plan.error)); return; }
        out.replaceChildren(
          h("div", { class: "nl-oracle-pool-h" }, h("b", {}, inf.symbol || inf.name || "pool"),
            h("span", { class: "nl-note" }, [inf.coins.map(c => c.symbol || "?").join(" / "), `A ${plan.A}`,
              Number.isFinite(inf.virtual_price) ? "virtual price " + fmtNum(inf.virtual_price, 6) : "",
              (inf.price_oracle || []).length ? "price_oracle " + fmtNum(inf.price_oracle[0], 6) : "",
              inf.ma_exp_time ? "pool ma_exp_time " + inf.ma_exp_time + " s" : ""].filter(Boolean).join(" · "))),
          h("pre", { class: "nl-oracle-pre nl-mono" }, [
            `vp = ${shortAddr(inf.address || addr)} get_virtual_price()`,
            `p  = ${shortAddr(inf.address || addr)} ${plan.takesIndex ? "price_oracle(uint256) [0]" : "price_oracle()"}`,
            `lp = ${fillExpr(plan.oracleExpr, "vp", "p")}`].join("\n")),
          h("div", { class: "nl-note" }, `lp is priced in coin ${coin} (${(inf.coins[coin] || {}).symbol || "?"}). Multiply it by a feed of that coin in the borrowed token when the market borrows something else.` +
            (plan.takesIndex ? "" : " This pool answers price_oracle() without an index, so p uses that signature.")));
      }
      function apply(replace) {
        const inf = current(), plan = inf ? lpPlan(inf, coin, emaTime) : null;
        if (!plan || plan.error) return;
        const pool = inf.address || addr, sym = inf.symbol || inf.name || shortAddr(pool), coinSym = (inf.coins[coin] || {}).symbol || "coin " + coin;
        const same = (s, d) => s.kind === "onchain" && String(s.address || "").toLowerCase() === pool.toLowerCase() &&
          s.sig === d.sig && JSON.stringify(s.args || []) === JSON.stringify(d.args) && !s.raw;
        const mk = (name, preset, note) => ({ name, kind: "onchain", address: pool, ...presetFields(preset), raw: false, note });
        const want = [mk("vp", "vp", `${sym} virtual price`), mk("p", plan.takesIndex ? "price_oracle_i" : "price_oracle", `${sym} price_oracle${plan.takesIndex ? "(0)" : "()"}: coin 1 in coin 0`)];
        // a source that already reads exactly this is reused instead of doubled
        const existing = replace ? [] : store.spec.oracle.sources;
        const names = want.map(d => { const hit = existing.find(s => same(s, d)); return hit ? hit.name : null; });
        const drafts = want.map((src, i) => ({ src, keep: !names[i] })).filter(d => d.keep);
        commitSources(drafts, { replace, chain: null, script: (old, ds) => {
          let k = 0;
          const [vp, p] = want.map((_, i) => names[i] || ds[k++].src.name);
          const head = `# StableSwap-NG LP oracle of ${sym}, priced in coin ${coin} (${coinSym}): A = ${plan.A}, ema_time = ${plan.T} s`;
          if (replace) return [head, `lp     = ${fillExpr(plan.oracleExpr, vp, p)}`, "oracle = lp", "",
            "# what the LP token trades at: no dampening of the virtual price", `market = ${fillExpr(plan.marketExpr, vp, p)}`].join("\n");
          const compiled = compile(old), assigned = new Set(compiled.lines.map(l => l.name));
          const taken = new Set([...assigned, ...store.spec.oracle.sources.map(s => s.name)]);
          const lp = uniqueName("lp", taken);
          const lines = [old.replace(/\s+$/, ""), old.trim() ? "" : null, head, `${lp} = ${fillExpr(plan.oracleExpr, vp, p)}`,
            assigned.has("oracle") ? null : `oracle = ${lp}`].filter(x => x !== null);
          return lines.join("\n");
        } });
        toast(replace ? `LP oracle of ${sym} written. Press Load & evaluate.` : `Added the LP oracle of ${sym} to the sources and the script. Press Load & evaluate.`, "good");
        togglePanel(false);
      }
      paint();
      return { el: pane, shown: () => {}, repaint: () => { if (!typed && collateral() !== addr) { addr = collateral(); addrF.set(addr); } paint(); } };
    })();

    const tabs = seg([{ value: "market", label: "From an existing market" }, { value: "lp", label: "StableSwap-NG LP oracle from a pool" }], mode, v => {
      mode = v;
      marketPane.el.hidden = v !== "market";
      lpPane.el.hidden = v !== "lp";
      if (v === "market") marketPane.shown();
    });
    el.append(h("div", { class: "nl-oracle-tpl-h" }, tabs, button("Close", { small: true, kind: "ghost", onClick: () => togglePanel(false) })),
      marketPane.el, lpPane.el);
    el.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); togglePanel(false); tplBtn.focus(); } });
    return { el, shown: () => { if (mode === "market") marketPane.shown(); }, repaint: () => { marketPane.repaint(); lpPane.repaint(); },
      destroy() { panelDead = true; } };
  }

  // ---- helper strip: what the script still needs, and what usually belongs in a crvUSD market --------------
  // (1) a pasted Vyper oracle names its contracts: `lp_oracle.price()`. Every such name that is not a source yet
  //     gets a row: paste the address, the function is looked up in the contract's ABI by its name.
  // (2) the crvUSD/USD aggregator the live markets of this chain read, when no source points at it yet.
  // (3) a script in Vyper integer units (10**18) reading sources that are scaled to decimals: mixed units.
  const helpEl = h("div", { class: "nl-oracle-help", hidden: true });
  const typed = new Map();                                 // missing name -> address typed so far
  let helpSig = "", agg = null, aggChain = "";
  const usesRaw = code => /10\s*\*\*\s*18|\b1e18\b/.test(code);
  const methodOf = (code, name) => { const m = new RegExp(`(?<![A-Za-z0-9_.])${name}\\.([A-Za-z_][A-Za-z0-9_]*)\\s*\\(`).exec(code); return m ? m[1] : ""; };
  async function createFromScript(name, method, address, btn) {
    if (!ADDR_RE.test(address)) return toast(`Paste the address of ${name} first: 0x followed by 40 hex characters.`, "error");
    btn.disabled = true;
    let fn = null, info = null;
    try { info = await api.abi(store.spec.chain, address); fn = info.functions.find(f => f.name === method && !f.inputs.length) || info.functions.find(f => f.name === method) || null; }
    catch (_) { /* unverified or offline: fall back to the name the script uses */ }
    if (dead) return;
    const raw = usesRaw(codeOf(store.spec.oracle.script)), sig = fn ? fn.sig : `${method || "price"}()`;
    const k = fn ? Math.max(0, fn.name === "latestRoundData" ? 1 : fn.outputs.findIndex(o => /^u?int/.test(o.type))) : 0;
    const p = PRESETS.find(x => x.sig === sig && x.slot === k && !x.args.length);
    store.update("oracle.sources", spec => {
      spec.oracle.sources.push({ id: store.nextSourceId(), name, kind: "onchain", address, preset: p ? p.id : "custom", sig,
        args: fn ? fn.inputs.map(i => (i.type === "address" ? "" : 0)) : [], slot: k, rtype: fn && /^int/.test((fn.outputs[k] || {}).type || "") ? "int" : "uint",
        decimals: 18, raw, note: info && info.name ? info.name : "", ema: { onchain: null, use: null } });
    });
    typed.delete(name);
    evaluateSafe(store);
    liveValues(store).catch(() => {});                      // the new source's reading now, so the lines show numbers at once
    toast(fn ? `${name} = ${info.name || shortAddr(address)} · ${sig}${raw ? " · raw integer units, as the script computes in 10**18" : ""}`
      : `${name} created with ${sig}: the ABI of ${shortAddr(address)} is not verified, so check the signature.`, fn ? "good" : "info");
  }
  function addAggregator(multiply) {
    if (!agg || !agg.address) return;
    const raw = usesRaw(codeOf(store.spec.oracle.script));
    let name = "";
    store.update("oracle.sources", spec => {
      name = uniqueName("agg", new Set([...spec.oracle.sources.map(x => x.name), ...compile(spec.oracle.script).lines.map(l => l.name)]));
      spec.oracle.sources.push({ id: store.nextSourceId(), name, kind: "onchain", address: agg.address, ...presetFields("price"), raw,
        note: "crvUSD/USD aggregator: what one crvUSD is worth in dollars", ema: { onchain: null, use: null } });
      if (!multiply) return;
      const lines = String(spec.oracle.script || "").split("\n"), tail = raw ? ` * ${name} // 10**18` : ` * ${name}`;
      const at = lines.findIndex(l => /^\s*oracle\s*=/.test(l.replace(/#.*$/, "")));
      if (at >= 0) {
        const hash = lines[at].indexOf("#"), code = hash < 0 ? lines[at] : lines[at].slice(0, hash), rest = hash < 0 ? "" : "  " + lines[at].slice(hash);
        const m = /^(\s*oracle\s*=\s*)(.*?)\s*$/.exec(code), simple = /^[A-Za-z_][A-Za-z0-9_.]*(\(\))?$/.test(m[2]);
        lines[at] = `${m[1]}${simple ? m[2] : "(" + m[2] + ")"}${tail}${rest}`;
      } else {
        const last = compile(spec.oracle.script).lines.slice(-1)[0];
        if (last) lines.push(`oracle = ${last.name}${tail}`);
      }
      spec.oracle.script = lines.join("\n");
    });
    if (multiply) store.emit("oracle.script");
    evaluateSafe(store);
    liveValues(store).catch(() => {});
    toast(multiply ? `${name} added and the oracle line multiplied by it. Press Load & evaluate.` : `${name} added: multiply the crvUSD-priced line by it in the script.`, "good");
  }
  function paintHelp() {
    const spec = store.spec, code = codeOf(spec.oracle.script), names = new Set(spec.oracle.sources.map(x => x.name));
    const assigned = new Set(compile(spec.oracle.script).lines.map(l => l.name));
    const missing = [...used].filter(n => NAME_RE.test(n) && !names.has(n) && !assigned.has(n) && !isReservedName(n));
    const hasAgg = agg && agg.address && spec.oracle.sources.some(x => String(x.address || "").toLowerCase() === agg.address);
    const mixed = usesRaw(code) ? spec.oracle.sources.filter(x => x.kind === "onchain" && x.name && used.has(x.name) && !x.raw) : [];
    const sig = JSON.stringify([missing, missing.map(n => methodOf(code, n)), agg && agg.address, hasAgg, agg && agg.price, mixed.map(x => x.id), spec.chain]);
    if (aggChain !== spec.chain) {
      aggChain = spec.chain; agg = null;
      const chainNow = spec.chain;
      serverMode().then(m => (m.public ? null : api.aggregator(chainNow))).then(r => { if (!dead && aggChain === spec.chain) { agg = r; paintHelp(); } }).catch(() => {});
    }
    if (sig === helpSig) return;
    helpSig = sig;
    const items = missing.map(name => {
      const method = methodOf(code, name), looksAgg = agg && agg.address && /agg/i.test(name);
      if (!typed.has(name) && looksAgg) typed.set(name, agg.checksum || agg.address);
      const input = h("input", { class: "nl-input nl-mono", type: "text", placeholder: "0x… contract address", spellcheck: "false", autocomplete: "off", value: typed.get(name) || "", "aria-label": `address of ${name}` });
      input.addEventListener("input", () => typed.set(name, input.value.trim()));
      const btn = button("Create the source", { small: true, kind: "primary", onClick: () => createFromScript(name, method, input.value.trim(), btn) });
      input.addEventListener("keydown", e => { if (e.key === "Enter") btn.click(); });
      return h("div", { class: "nl-oracle-help-item" }, h("div", {}, "The script reads ", h("b", { class: "nl-mono" }, method ? `${name}.${method}()` : name), ", and no source is called ", h("b", { class: "nl-mono" }, name), " yet. ",
        h("span", { class: "nl-note" }, looksAgg ? "Filled in: the crvUSD/USD aggregator the live markets of this chain read." : method ? `Paste the contract's address: ${method}() is looked up in its verified ABI.` : "Paste the contract's address, then pick its function.")),
        h("div", { class: "nl-oracle-help-act" }, input, btn));
    });
    if (agg && agg.address && !hasAgg && !missing.some(n => /agg/i.test(n))) items.push(h("div", { class: "nl-oracle-help-item" },
      h("div", {}, h("b", {}, "Priced in crvUSD? "), "A pool against crvUSD, or a feed such as priceAsCrvusd(), says how many crvUSD the collateral is worth. The live markets multiply that by the crvUSD/USD aggregator ",
        h("span", { class: "nl-mono", title: agg.checksum || agg.address }, shortAddr(agg.checksum || agg.address)), Number.isFinite(agg.price) ? ` (price() = ${fmtNum(agg.price, 6)} now, read by ${agg.used_by_markets} markets here)` : "",
        ", so a crvUSD that drifts off $1 does not move the loans."),
      h("div", { class: "nl-oracle-help-act" }, button("Add it as a source", { small: true, onClick: () => addAggregator(false) }),
        button("Add it and multiply the oracle line by it", { small: true, kind: "primary", onClick: () => addAggregator(true) }))));
    if (mixed.length) items.push(h("div", { class: "nl-oracle-help-item nl-oracle-help-warn" },
      h("div", {}, h("b", {}, "Mixed units. "), `The script computes in Vyper integer units (10**18), but ${mixed.map(x => x.name).join(", ")} ${mixed.length > 1 ? "are" : "is"} divided by 10^decimals first, so a line such as min(10**18, feed) never caps anything.`),
      h("div", { class: "nl-oracle-help-act" }, button("Switch them to raw integer units", { small: true, kind: "primary", onClick: () => {
        store.update("oracle.sources", sp => { for (const x of sp.oracle.sources) if (mixed.some(q => q.id === x.id)) x.raw = true; });
        for (const x of mixed) flagStale(store, x.id);
        evaluateSafe(store);
      } }))));
    helpEl.hidden = !items.length;
    helpEl.replaceChildren(...items);
  }

  reconcile();
  paintHelp();
  return {
    head: [countEl, foldBtn, tplBtn],
    body: [panelHost, helpEl, emptyEl, listEl, addRow],
    onTag(tag) {
      if (tag === "oracle.sources") { used = usedBy(store); reconcile(); paintHelp(); }
      else if (tag === "oracle.script") { used = usedBy(store); paintHead(); for (const r of rows.values()) r.patch(); paintHelp(); }
      else if (tag === "oracle.data") patchAll();
      else if (tag === "tokens") { if (panel && !panelHost.hidden) panel.repaint(); paintHelp(); }
    },
    destroy() {
      dead = true;
      patchAll.cancel();
      if (panel) panel.destroy();
      for (const r of rows.values()) r.destroy();
      rows.clear();
    },
  };
}

// =====================================================================================================
// part: script
// =====================================================================================================
function scriptPart(store, opts) {
  let dead = false, timer = null, dirty = false, gutterSig = "", chipSig = "";
  const ta = h("textarea", { class: "nl-input nl-oracle-code-ta", rows: Math.max(4, +opts.rows || 12), wrap: "off", spellcheck: "false",
    autocomplete: "off", autocapitalize: "off", "aria-label": "oracle script" });
  ta.value = store.spec.oracle.script || "";
  const gutterIn = h("div", { class: "nl-oracle-gutter-in" });
  const gutter = h("div", { class: "nl-oracle-gutter nl-mono", "aria-hidden": "true" }, gutterIn);
  const msgs = h("div", { class: "nl-oracle-msgs" });
  const chips = h("div", { class: "nl-oracle-names" });

  const commit = () => {
    clearTimeout(timer);
    timer = null;
    if (!dirty || dead) return;
    dirty = false;
    const v = ta.value;
    if (v !== store.spec.oracle.script) store.update("oracle.script", s => { s.oracle.script = v; });
    evaluateSafe(store);                                   // local and cheap: no fetching
  };
  const touched = () => { dirty = true; clearTimeout(timer); timer = setTimeout(commit, 400); paintGutter(); };
  ta.addEventListener("input", touched);
  ta.addEventListener("blur", commit);
  ta.addEventListener("scroll", () => { gutterIn.scrollTop = ta.scrollTop; });
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); dirty = true; commit(); }
  });

  function errorLines() {
    const compiled = compile(store.spec.oracle.script);
    const firstDef = new Map();
    for (const l of compiled.lines) if (!firstDef.has(l.name)) firstDef.set(l.name, l.line);
    // a line that only fails because an earlier line failed adds nothing: hide the echo
    const evalErrs = (store.rt.evalErrors || []).filter(e => {
      const m = /^"([^"]+)" is not a source or an earlier line$/.exec(e.msg || "");
      return !(m && firstDef.has(m[1]) && firstDef.get(m[1]) < e.line);
    });
    return { compiled, compileErrs: compiled.errors, evalErrs };
  }
  function paintGutter() {
    const n = ta.value.split("\n").length, bad = new Set();
    if (!dirty) { const e = errorLines(); for (const x of [...e.compileErrs, ...e.evalErrs]) bad.add(x.line); }   // marks wait for the evaluation
    const sig = n + ":" + [...bad].join(",");
    if (sig !== gutterSig) {
      gutterSig = sig;
      const kids = [];
      for (let i = 1; i <= n; i++) kids.push(h("span", { class: bad.has(i) ? "nl-oracle-gl nl-oracle-gl-err" : "nl-oracle-gl" }, String(i)));
      gutterIn.replaceChildren(...kids);
      gutter.style.width = `calc(${Math.max(2, String(n).length)}ch + 16px)`;
    }
    gutterIn.scrollTop = ta.scrollTop;
  }
  function selectLine(line) {
    const lines = ta.value.split("\n");
    let a = 0;
    for (let i = 0; i < line - 1 && i < lines.length; i++) a += lines[i].length + 1;
    ta.focus();
    ta.setSelectionRange(a, a + (lines[line - 1] || "").length);
  }
  function insert(text) {
    const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a;
    ta.setRangeText(text, a, b, "end");
    ta.focus();
    touched();
  }
  function paintMessages() {
    const rt = store.rt, { compiled, compileErrs, evalErrs } = errorLines();
    const used = sourcesUsed(compiled), missing = missingOf(store, used), out = [];
    const errRow = (e, what) => h("div", { class: "nl-oracle-msg nl-err" },
      h("button", { type: "button", class: "nl-oracle-lineref", title: "select this line", onClick: () => selectLine(e.line) }, `line ${e.line}`),
      h("span", {}, `${what}: ${e.msg}`));
    for (const e of compileErrs) out.push(errRow(e, "does not parse"));
    for (const e of evalErrs) out.push(errRow(e, "cannot be evaluated"));
    if (missing.length) out.push(h("div", { class: "nl-oracle-msg nl-oracle-hint" },
      `Not loaded yet: ${missing.join(", ")}. Press Load & evaluate in the range controls to fetch ${missing.length > 1 ? "them" : "it"}; until then every line that depends on ${missing.length > 1 ? "them" : "it"} stays empty.`));
    if (rt.rescaled) out.push(h("div", { class: "nl-oracle-msg nl-note" },
      "The result was above 1e9, so it was taken as a raw 1e18 integer and divided by 1e18 (raw-unit script)."));
    const names = compiled.lines.map(l => l.name);
    if (compiled.lines.length && !names.includes("oracle")) out.push(h("div", { class: "nl-oracle-msg nl-oracle-hint" },
      `No line assigns oracle, so the last assignment (${names[names.length - 1]}) is used as the oracle. Name it oracle to be explicit.`));
    if (!compiled.lines.length && !compileErrs.length) out.push(h("div", { class: "nl-oracle-msg nl-oracle-hint" },
      "The script is empty. Write  oracle = <source name>  to begin."));
    if (!compileErrs.length && !evalErrs.length && !missing.length && rt.oracle && rt.valid0 < rt.oracle.length) {
      const lo = lastFinite(rt.oracle), lm = rt.market ? lastFinite(rt.market) : NaN;
      out.push(h("div", { class: "nl-oracle-msg nl-ok" }, `Evaluates on ${nInt(rt.oracle.length - rt.valid0)} grid points: latest oracle ${fmtVal(lo)}` +
        (rt.market && rt.market !== rt.oracle ? `, market ${fmtVal(lm)}.` : "; no market line, so market = oracle.")));
    }
    msgs.replaceChildren(...out);
    paintGutter();
  }
  function paintNames() {
    const list = store.spec.oracle.sources.filter(s => s.name), used = usedBy(store);
    const sig = list.map(s => s.name + (used.has(s.name) ? "+" : "-")).join("|");
    if (sig === chipSig) return;
    chipSig = sig;
    chips.replaceChildren(h("span", { class: "nl-label" }, list.length ? "sources (click to insert)" : "no sources defined yet"),
      ...list.map(s => h("button", { type: "button", class: "nl-oracle-name" + (used.has(s.name) ? " nl-oracle-name-used" : ""),
        title: summaryOf(s) + (s.note ? " · " + s.note : ""), onClick: () => insert(s.name) }, s.name)));
  }
  function syncText(force) {
    const v = store.spec.oracle.script || "";
    if (ta.value === v) return;
    if (force || (!dirty && document.activeElement !== ta)) { ta.value = v; dirty = false; clearTimeout(timer); gutterSig = ""; }
  }

  const ref = h("details", { class: "nl-oracle-ref" },
    h("summary", {}, "Function reference"),
    h("div", { class: "nl-oracle-ref-b" },
      h("div", { class: "nl-note" }, "One assignment per line, # starts a comment. Values are numbers or series on the shared grid; operators + - * / ** and Vyper's // work elementwise, and a value that is missing (NaN) stays missing. 10**18 literals and feed.price() spellings paste unchanged."),
      ...FUNC_DOCS.map(([sig, doc]) => h("div", { class: "nl-oracle-ref-row" }, h("code", { class: "nl-mono" }, sig), h("span", {}, doc)))));

  paintNames();
  paintMessages();
  return {
    body: [
      h("div", { class: "nl-note", title: "A formula, not a program: names, numbers, + − * / // ** and the functions in the reference. It is parsed and computed by this page over the loaded series and cannot call a contract, reach the network or touch the page. A Vyper oracle pastes as it is: name.method() reads the source called name." },
        "oracle = what LLAMMA reads · market = what it really trades at (optional) · a formula, not code: nothing here can execute"),
      h("div", { class: "nl-oracle-code" }, gutter, ta),
      chips, msgs, ref,
    ],
    onTag(tag) {
      if (tag === "oracle.script") { syncText(false); paintNames(); paintMessages(); }
      else if (tag === "oracle.sources") { paintNames(); paintMessages(); }
      else if (tag === "oracle.data") paintMessages();
    },
    destroy(discard) {
      if (dirty && !discard) commit();                     // a design switch must not eat the last keystrokes
      dead = true;
      clearTimeout(timer);
    },
  };
}

// =====================================================================================================
// part: graph
// =====================================================================================================
const G = { w: 176, h: 46, gx: 58, gy: 14, pad: 10 };
const clip = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s; };

function graphPart(store, opts, single) {
  const viewport = h("div", { class: "nl-oracle-graph" });
  const maxH = +opts.graphHeight || (single ? +opts.height : 0);
  if (maxH > 0) viewport.style.maxHeight = maxH + "px";
  const legend = h("div", { class: "nl-oracle-legend" },
    ...[["source", "source"], ["var", "script line"], ["oracle", "oracle: what LLAMMA reads"], ["market", "market: what it trades at"], ["missing", "unknown name"]]
      .map(([k, label]) => h("span", { class: "nl-oracle-key nl-oracle-key-" + k }, h("i", {}), label)));
  const note = h("div", { class: "nl-note", hidden: true });
  let lastSig = "";

  function valueOfVar(rt, name) {
    const live = rt.liveVars && rt.liveVars[name];
    if (Number.isFinite(live)) return { v: live, live: true };
    const arr = name === "oracle" && rt.oracle ? rt.oracle : name === "market" && rt.market ? rt.market : rt.vars && rt.vars[name];
    if (arr instanceof Float64Array) return { v: lastFinite(arr), live: false };
    return { v: typeof arr === "number" ? arr : NaN, live: false };
  }
  function valueOfSource(rt, s) {
    const d = rt.sources[s.id];
    if (!d) return { v: NaN, live: false, status: "idle" };
    if (Number.isFinite(d.live)) return { v: d.live, live: true, status: d.status || "idle" };
    if (d.const !== undefined) return { v: d.const, live: false, status: d.status };
    return { v: d.v ? lastFinite(d.v) : NaN, live: false, status: d.status || "idle" };
  }

  function paint() {
    const spec = store.spec, rt = store.rt;
    const compiled = compile(spec.oracle.script);
    const byName = new Map(spec.oracle.sources.filter(s => s.name).map(s => [s.name, s]));
    const g = graphOf(compiled, [...byName.keys()]);
    const inGraph = new Set(g.nodes.map(n => n.id));
    const idle = [...byName.keys()].filter(n => !inGraph.has(n));
    note.hidden = !idle.length;
    setText(note, idle.length ? `Not read by the script: ${idle.join(", ")}.` : "");
    legend.hidden = !g.nodes.length;
    if (!g.nodes.length) {
      lastSig = "";
      viewport.replaceChildren(h("div", { class: "nl-oracle-empty" }, h("b", {}, "Nothing to draw yet"),
        h("div", {}, compiled.errors.length ? "No line of the script parses. Fix the errors shown under the script."
          : "The script has no assignment. Write  oracle = <source name>  and the graph shows what feeds the oracle.")));
      return;
    }
    // layers by depth, ordered by the mean position of their neighbours to keep edges short
    const edges = [], seenEdge = new Set();
    for (const e of g.edges) {
      const k = e.from + ">" + e.to;
      if (e.from === e.to || seenEdge.has(k) || !inGraph.has(e.from) || !inGraph.has(e.to)) continue;
      seenEdge.add(k);
      edges.push(e);
    }
    const layers = [];
    for (const n of g.nodes) (layers[n.depth] || (layers[n.depth] = [])).push(n);
    const cols = layers.filter(Boolean), pos = new Map();
    const place = col => col.forEach((n, i) => pos.set(n.id, (i + 0.5) / col.length));
    const order = (col, pick) => {
      const key = new Map(col.map((n, i) => {
        const ps = edges.filter(e => pick(e) === n.id).map(e => pos.get(pick(e) === e.to ? e.from : e.to)).filter(Number.isFinite);
        return [n.id, ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : (i + 0.5) / col.length];
      }));
      col.sort((a, b) => key.get(a.id) - key.get(b.id));
      place(col);
    };
    cols.forEach(place);
    for (let i = 1; i < cols.length; i++) order(cols[i], e => e.to);
    for (let i = cols.length - 2; i >= 0; i--) order(cols[i], e => e.from);
    for (let i = 1; i < cols.length; i++) order(cols[i], e => e.to);

    const colH = n => n * G.h + (n - 1) * G.gy, tall = Math.max(...cols.map(c => colH(c.length)));
    const W = G.pad * 2 + cols.length * G.w + (cols.length - 1) * G.gx, H = G.pad * 2 + tall;
    const at = new Map();
    cols.forEach((col, ci) => col.forEach((n, i) => at.set(n.id, {
      x: G.pad + ci * (G.w + G.gx), y: G.pad + (tall - colH(col.length)) / 2 + i * (G.h + G.gy) })));

    const kindOf = new Map(g.nodes.map(n => [n.id, n.kind]));
    const svg = [`<svg class="nl-oracle-g" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="dependency graph of the oracle script" style="width:100%;min-width:${Math.round(Math.min(W, Math.max(280, W * 0.78)))}px;max-width:${W}px;height:auto">`];
    const incoming = new Map();
    for (const e of edges) (incoming.get(e.to) || incoming.set(e.to, []).get(e.to)).push(e);
    for (const [to, list] of incoming) {
      list.sort((a, b) => at.get(a.from).y - at.get(b.from).y);
      list.forEach((e, k) => {
        const a = at.get(e.from), b = at.get(to);
        const x1 = a.x + G.w, y1 = a.y + G.h / 2, x2 = b.x - 6, y2 = b.y + (G.h * (k + 1)) / (list.length + 1);
        const dx = Math.max(22, (x2 - x1) * 0.5), bad = kindOf.get(e.from) === "missing" ? " nl-oracle-g-edge-missing" : "";
        svg.push(`<path class="nl-oracle-g-edge${bad}" d="M${x1} ${y1.toFixed(1)} C${(x1 + dx).toFixed(1)} ${y1.toFixed(1)} ${(x2 - dx).toFixed(1)} ${y2.toFixed(1)} ${x2} ${y2.toFixed(1)}"/>`,
          `<path class="nl-oracle-g-arrow${bad}" d="M${x2 + 6} ${y2.toFixed(1)} l-7 -3.6 v7.2 z"/>`);
      });
    }
    for (const n of g.nodes) {
      const p = at.get(n.id), src = n.kind === "source" ? byName.get(n.id) : null;
      const val = src ? valueOfSource(rt, src) : n.kind === "missing" ? { v: NaN, live: false } : valueOfVar(rt, n.id);
      const shown = Number.isFinite(val.v) ? fmtVal(val.v) : "";
      const sub = src ? (src.kind === "onchain" ? (src.sig || "on-chain read") : (KINDS.find(k => k.value === src.kind) || {}).label || src.kind)
        : n.kind === "missing" ? "not a source, not assigned" : (n.op || "=");
      const tip = src ? `${n.id}: ${summaryOf(src)}${src.note ? " · " + src.note : ""}`
        : n.kind === "missing" ? `${n.id} is read by the script but is neither a source nor assigned on an earlier line` : n.text || n.id;
      svg.push(`<g class="nl-oracle-g-node nl-oracle-g-${n.kind}" transform="translate(${p.x} ${p.y.toFixed(1)})">`,
        `<title>${esc(tip)}${shown ? esc(` · ${val.live ? "live" : "last"} ${shown}`) : ""}</title>`,
        `<rect width="${G.w}" height="${G.h}" rx="7"/>`,
        `<text class="nl-oracle-g-name" x="11" y="19">${esc(clip(n.id, src ? 19 : 21))}</text>`,
        `<text class="nl-oracle-g-sub" x="11" y="36">${esc(clip(sub, shown ? 25 - Math.min(14, shown.length + 5) : 25))}</text>`);
      if (shown) svg.push(`<text class="nl-oracle-g-val${val.live ? " nl-oracle-g-val-live" : ""}" x="${G.w - 10}" y="36" text-anchor="end">${esc((val.live ? "live " : "") + shown)}</text>`);
      if (src) svg.push(`<circle class="nl-oracle-g-dot nl-oracle-g-dot-${esc(val.status || "idle")}" cx="${G.w - 12}" cy="14" r="3.5"/>`);
      svg.push("</g>");
    }
    svg.push("</svg>");
    const markup = svg.join("");
    if (markup === lastSig) return;
    lastSig = markup;
    viewport.innerHTML = markup;
  }
  const schedule = frameScheduler(paint);
  paint();
  return {
    body: [viewport, legend, note],
    onTag(tag) { if (tag === "oracle.script" || tag === "oracle.sources" || tag === "oracle.data") schedule(); },
    destroy() { schedule.cancel(); },
  };
}

// =====================================================================================================
// part: chart
// =====================================================================================================
// The oracle is the one white, heavy line and the market keeps its colour of the whole tool (--nl-c2, sky blue); every
// part that can be switched on gets a colour of its own from this list (no white, no blue), dashed once the list has gone round.
const OVERLAY_COLORS = ["#f0883e", "#34d399", "#f472b6", "#c084fc", "#fbbf24", "#f85149", "#a3e635"];
const COIN_COLORS = ["#2dd4bf", "#c084fc", "#fbbf24", "#f472b6"];

// what a reading IS, in words: "sfrxUSD in reUSD · pool EMA" instead of a bare source name
const poolMemo = new Map(), whatMemo = new Map();          // "chain:address" -> Promise(pool | null); reading -> text
const poolOf = (chain, addr) => {
  const key = chain + ":" + addr;
  if (!poolMemo.has(key)) poolMemo.set(key, api.pool(chain, addr).catch(() => null));
  return poolMemo.get(key);
};
// pack = the market's prepared file (store.rt.pack): it carries these words, so a page load asks nobody
export async function readingText(chain, src, pack) {
  const known = pack && pack.texts && pack.texts[JSON.stringify([chain, src.address, src.sig, src.args || []]).toLowerCase()];
  if (known) return known;
  const addr = String(src.address || "").toLowerCase(), fn = String(src.sig || "").split("(")[0];
  const info = ADDR_RE.test(addr) ? await poolOf(chain, addr) : null;
  if (info && (info.coins || []).length > 1) {
    const k = Math.max(0, Math.round(+(src.args || [])[0]) || 0), base = info.coins[0].symbol, coin = (info.coins[k + 1] || info.coins[1]).symbol;
    if (/^price_oracle$/.test(fn)) return `${coin} in ${base} · pool EMA`;
    if (/^(last_price|last_prices)$/.test(fn)) return `${coin} in ${base} · last trade`;
    if (fn === "get_p") return `${coin} in ${base} · pool spot`;
    if (fn === "get_virtual_price") return `${info.symbol || "pool"} virtual price`;
    if (fn === "lp_price") return `${info.symbol || "pool"} LP price`;
    return `${fn}() of ${info.symbol || info.name || shortAddr(addr)}`;
  }
  const agg = await api.aggregator(chain).catch(() => null);
  if (agg && String(agg.address || "").toLowerCase() === addr && fn === "price") return "crvUSD in USD · aggregator";
  const abi = ADDR_RE.test(addr) ? await api.abi(chain, addr).catch(() => null) : null;
  return `${fn}() of ${(abi && abi.name) || shortAddr(addr)}`;
}
// one on-chain reading over a range, block-exact (api.sampleExact keeps past windows for the page, the server for ever)
const sampleOnce = (chain, r, range) => api.sampleExact(chain, { address: r.to, sig: r.sig, args: r.args, slot: r.slot, rtype: r.rtype, decimals: r.decimals, raw: false },
  range, { raw: HELD_READING.test(r.sig), watch: [r.to] });
const onGrid = (d, t) => (d.exact && !d.held ? toGridLinear(d, t) : toGrid(d, t));

function chartPart(store, opts) {
  let chipSig = "", last = null, refocus = null;
  const picked = new Map();                                // overlay key -> colour slot
  const regrid = new Map();                                // source id -> { d, grid, arr }
  const chartHost = h("div", { class: "nl-oracle-chart" });
  // opts.coins: one thin chart per coin of the collateral's pool above the main one, on the same time axis (zoom, pan and cursor shared)
  let coinCharts = [], coinSig = "", coinView = null;
  const coinsHost = h("div", { class: "nl-oracle-ccs", hidden: true });
  const others = me => [chart, ...coinCharts.map(c => c.chart)].filter(c => c !== me);
  const linkOf = () => ({ onView(v) { coinView = v; for (const c of others(this.me)) c.setView(v ? v.a : 0, v ? v.b : 0); }, onHover(x) { for (const c of others(this.me)) c.setHover(x); } });
  const mainLink = linkOf();
  const chart = lineChart(chartHost, { height: Math.max(140, +opts.height || 280), xMode: "time", yFmt: v => fmtNum(v, 5), empty: "no oracle history yet", zoom: !!opts.zoom,
    onView: v => mainLink.onView(v), onHover: x => mainLink.onHover(x) });
  mainLink.me = chart;
  const kpis = h("div", { class: "nl-kpis nl-oracle-kpis" });
  const chips = h("div", { class: "nl-oracle-chips" });
  const emptyText = h("div", {}), emptyTitle = h("b", {});
  const loadBtn = button("Load & evaluate", { kind: "primary", onClick: () => loadWithFeedback(store) });
  const empty = h("div", { class: "nl-oracle-empty", hidden: true }, emptyTitle, emptyText, h("div", {}, loadBtn));
  const tools = h("div", { class: "nl-oracle-charttools" }, chips);
  // chartFirst: the chart leads and the figures follow it (the hero chart at the top of the page)
  // opts.above / opts.below: elements the page wants right over the charts (the crash picker) and right under them (the MA sliders)
  const full = h("div", { class: "nl-oracle-chartwrap" }, opts.chartFirst ? [opts.above || null, coinsHost, tools, chartHost, kpis, opts.below || null] : [kpis, opts.above || null, coinsHost, tools, chartHost, opts.below || null]);
  const hasData = rt => !!(rt.grid && rt.oracle && rt.grid.t.length === rt.oracle.length && rt.valid0 < rt.oracle.length - 1);

  function overlayItems() {
    const rt = store.rt, n = rt.grid ? rt.grid.t.length : 0, out = [];
    const texts = new Map(compile(store.spec.oracle.script).lines.map(l => [l.name, l.text]));
    for (const [name, v] of Object.entries(rt.vars || {})) {
      if (!(v instanceof Float64Array) || v.length !== n || name === "oracle" || name === "market") continue;
      out.push({ key: "v:" + name, label: name, title: texts.get(name) || name });
    }
    for (const s of store.spec.oracle.sources) {
      const d = rt.sources[s.id];
      if (s.name && d && d.status === "ok") out.push({ key: "s:" + s.id, label: s.name, what: whatOf(s), source: true, title: `source · ${summaryOf(s)}` });
    }
    return out;
  }
  // words for a source, at once where they need no lookup, filled in (and the chips repainted) once the contract has answered
  function whatOf(src) {
    if (src.kind === "dataset") return `dataset ${src.key || "?"} · ${src.column || "close"}`;
    if (src.kind === "const") return `constant ${src.value}`;
    if (src.kind === "curve") return "prices API candles";
    if (src.kind === "upload") return "pasted CSV";
    const key = JSON.stringify([store.spec.chain, src.address, src.sig, src.args]).toLowerCase();
    if (!whatMemo.has(key)) {
      whatMemo.set(key, "");
      readingText(store.spec.chain, src, store.rt.pack).then(text => { whatMemo.set(key, text || ""); chipSig = ""; last = null; schedule(); }).catch(() => {});
    }
    return whatMemo.get(key);
  }
  function overlayArray(key) {
    const rt = store.rt, g = rt.grid;
    if (key.startsWith("v:")) { const v = rt.vars && rt.vars[key.slice(2)]; return v instanceof Float64Array && v.length === g.t.length ? v : null; }
    const id = key.slice(2), d = rt.sources[id];
    if (!d || d.status !== "ok") return null;
    // a part the script reads is drawn as the script sees it: with its MA time as set on the sliders
    const src = store.spec.oracle.sources.find(x => x.id === id), part = src && rt.parts && rt.parts[src.name];
    if (part && part.used && part.used.length === g.t.length) return part.used;
    return rawArray(id);
  }
  // a loaded source on the grid, exactly as sampled
  function rawArray(id) {
    const g = store.rt.grid, d = store.rt.sources[id], hit = regrid.get(id);
    if (hit && hit.d === d && hit.grid === g) return hit.arr;
    const arr = d.const !== undefined ? new Float64Array(g.t.length).fill(d.const) : onGrid(d, g.t);
    regrid.set(id, { d, grid: g, arr });
    return arr;
  }
  function togglePick(key) {
    if (picked.has(key)) picked.delete(key);
    else { let slot = 0; const taken = new Set(picked.values()); while (taken.has(slot)) slot++; picked.set(key, slot); }
    last = null;
    refocus = key;
    paint();
    refocus = null;
  }
  function paintChips(items) {
    for (const k of [...picked.keys()]) if (!items.some(i => i.key === k)) picked.delete(k);
    const sig = items.map(i => `${i.key}=${i.label}=${i.what || ""}=${picked.has(i.key) ? picked.get(i.key) : ""}`).join("|");
    if (sig === chipSig) return;
    chipSig = sig;
    chips.replaceChildren(...items.map(i => {
        const on = picked.has(i.key);
        const b = h("button", { type: "button", class: "nl-oracle-chip" + (i.source ? " nl-oracle-chip-source" : ""), "aria-pressed": String(on), title: i.title,
          onClick: () => togglePick(i.key) }, h("i", {}), h("b", {}, i.label), i.what ? h("span", { class: "nl-oracle-chip-what" }, i.what) : null);
        if (on) { b.style.setProperty("--nl-oracle-chip", OVERLAY_COLORS[picked.get(i.key) % OVERLAY_COLORS.length]); if (picked.get(i.key) >= OVERLAY_COLORS.length) b.classList.add("nl-oracle-chip-dash"); }
        if (i.key === refocus) queueMicrotask(() => { if (b.isConnected) b.focus(); });
        return b;
      }));
  }
  const kpi = (value, label, o = {}) => h("div", { class: "nl-kpi" + (o.cls ? " " + o.cls : ""), title: o.title || null }, h("b", {}, value), h("span", {}, label));
  function paintKpis() {
    const rt = store.rt, o = rt.oracle, m = rt.market || o, t = rt.grid.t, a = rt.valid0, n = o.length, own = m !== o;
    if (opts.figures === "none") { kpis.hidden = true; return; }
    if (opts.figures === "line") {                         // one line of figures instead of six tiles
      let gap = NaN, wide = NaN;
      if (own) {
        for (let i = n - 1; i >= a; i--) { const x = o[i] / m[i] - 1; if (Number.isFinite(x)) { gap = x; break; } }
        for (let i = a; i < n; i++) { const x = o[i] / m[i] - 1; if (Number.isFinite(x) && !(Math.abs(x) <= Math.abs(wide))) wide = x; }
      }
      const bit = (k, v) => h("span", {}, k + " ", h("b", { class: "nl-mono" }, v));
      kpis.className = "nl-oracle-figures";
      kpis.replaceChildren(...[bit("oracle", fmtVal(lastFinite(o))), own ? bit("market", fmtVal(lastFinite(m))) : null, own ? bit("gap now", fmtPct(gap)) : null, own ? bit("widest gap", fmtPct(wide)) : null,
        h("span", { class: "nl-note" }, `${nInt(n - a)} points · ${stepLabel(rt.grid.step)} · ${dateOf(t[a])} .. ${dateOf(t[n - 1])}`)].filter(Boolean));
      return;
    }
    let gapNow = NaN, widest = NaN, widestAt = NaN;
    if (own) {
      for (let i = n - 1; i >= a; i--) { const x = o[i] / m[i] - 1; if (Number.isFinite(x)) { gapNow = x; break; } }
      for (let i = a; i < n; i++) { const x = o[i] / m[i] - 1; if (Number.isFinite(x) && !(Math.abs(x) <= Math.abs(widest))) { widest = x; widestAt = t[i]; } }
    }
    const noMarket = "the script has no market line, so market = oracle and there is no gap";
    kpis.replaceChildren(
      kpi(fmtVal(lastFinite(o)), "latest oracle", { title: "last finite value of oracle on the grid" }),
      kpi(fmtVal(lastFinite(m)), own ? "latest market" : "latest market (= oracle)", { title: own ? "last finite value of market on the grid" : noMarket }),
      kpi(own ? fmtPct(gapNow) : "–", "gap now", { title: own ? "oracle / market − 1 at the last grid point where both exist" : noMarket }),
      kpi(own ? fmtPct(widest) : "–", "widest gap", { title: own ? (Number.isFinite(widestAt) ? `largest |oracle / market − 1| of the range, at ${fmtDateTime(widestAt)} UTC` : "no point where both exist") : noMarket }),
      kpi(nInt(n - a), `points · ${stepLabel(rt.grid.step)} grid`, { title: "grid points from the first one where oracle and market both exist" }),
      kpi(`${dateOf(t[a])} .. ${dateOf(t[n - 1])}`, "date range (UTC)", { cls: "nl-oracle-kpi-wide", title: `${fmtDateTime(t[a])} .. ${fmtDateTime(t[n - 1])} UTC` }));
  }
  function paintEmpty() {
    const rt = store.rt, compiled = compile(store.spec.oracle.script), used = sourcesUsed(compiled), missing = missingOf(store, used);
    const busy = !!rt.busy.sources;
    loadBtn.disabled = busy;
    let title = "No oracle history yet", text = "";
    if (busy) { title = "Loading the sources…"; text = "The chart fills in as soon as every source has arrived and the script has been evaluated."; }
    else if (!compiled.lines.length && compiled.errors.length) text = `The script does not parse (line ${compiled.errors[0].line}: ${compiled.errors[0].msg}). Fix it in the script editor first.`;
    else if (!compiled.lines.length) text = "The script is empty. Write  oracle = <source name>  in the script editor, then press Load & evaluate.";
    else if (!rt.grid || missing.length) text = `Press Load & evaluate: ${missing.length ? "the sources the script reads (" + missing.join(", ") + ") are" : "every source the script reads is"} sampled over the chosen range, then the script is evaluated on one shared grid and drawn here.`;
    else if ((rt.evalErrors || []).length) text = `The script fails while it is evaluated (line ${rt.evalErrors[0].line}: ${rt.evalErrors[0].msg}). The messages under the script editor list every problem.`;
    else text = "The script was evaluated, but oracle and market never have a value at the same time on this grid. Check that the sources cover the range (their first and last dates are in the source list), or press Reload.";
    if (rt.public && !busy) { title = "The history of this market is being prepared"; text = "It is built ahead of time and appears here once it has been published."; }
    loadBtn.hidden = !!rt.public;
    setText(emptyTitle, title);
    setText(emptyText, text);
  }
  function paint() {
    const rt = store.rt, ok = hasData(rt);
    full.hidden = !ok;
    empty.hidden = ok;
    if (!ok) { last = null; chipSig = ""; paintEmpty(); return; }
    const items = overlayItems();
    paintChips(items);
    const keys = [...picked.keys()].join(",");
    if (last && last.grid === rt.grid && last.oracle === rt.oracle && last.market === rt.market && last.valid0 === rt.valid0 && last.keys === keys
        && [...picked.keys()].every(k => last.arrs.get(k) === overlayArray(k))) return;
    const a = rt.valid0, arrs = new Map();
    // the oracle is THE line of this chart: white, heavy, drawn over everything else, with a dark edge where it crosses the others
    let series = [{ name: "collat oracle", v: rt.oracle.subarray(a), color: "--nl-fg", width: 3, top: true, halo: 1.5 }];
    if (rt.market && rt.market !== rt.oracle) series.push({ name: "collat spot", v: rt.market.subarray(a), color: "--nl-c2", width: 1.5 });
    for (const [key, slot] of picked) {
      const arr = overlayArray(key), item = items.find(i => i.key === key);
      arrs.set(key, arr);
      if (arr && item) series.push({ name: esc(item.label + (item.what ? ": " + item.what : "")), v: arr.subarray(a), color: OVERLAY_COLORS[slot % OVERLAY_COLORS.length], width: 1.3,
        dash: slot >= OVERLAY_COLORS.length ? [5, 3] : undefined });
    }
    chart.setData({ t: rt.grid.t.subarray(a), series });
    paintKpis();
    if (opts.coins) paintCoins();
    last = { grid: rt.grid, oracle: rt.oracle, market: rt.market, valid0: rt.valid0, keys, arrs };
  }
  // ---- the coins of the collateral's pool: what one token of each is worth in the borrowed token --------------
  // Last traded prices only, read from the pools the server routes through (/nlapi/coin_routes). A reading the
  // oracle sources already hold is taken from them; the rest is sampled once and cached on the server.
  function clearCoins() { for (const c of coinCharts) c.chart.destroy(); coinCharts = []; coinsHost.replaceChildren(); coinsHost.hidden = true; }
  function paintCoins() {
    const sp = store.spec, rt = store.rt, g = rt.grid;
    const sig = [sp.chain, sp.collateral.address, sp.borrowed.address, g.t[0], g.t[g.t.length - 1], g.step, rt.valid0].join("|").toLowerCase();
    if (sig === coinSig) return;
    coinSig = sig;
    loadCoins(sig).catch(e => { console.warn("[nl] coin charts", e); if (sig === coinSig) clearCoins(); });
  }
  async function loadCoins(sig) {
    const sp = store.spec, rt = store.rt, g = rt.grid, a = rt.valid0, chain = sp.chain, n = g.t.length;
    if (!ADDR_RE.test(sp.collateral.address || "") || !ADDR_RE.test(sp.borrowed.address || "")) return clearCoins();
    // From the prepared pack when there is one (no RPC on a page load). Without a pack the chain is only asked once
    // the researcher has loaded history by hand.
    const pack = rt.pack, byHand = Object.values(rt.sources).some(d => d && d.status === "ok" && d.exact && !d.fromPack);
    if (!(pack && pack.routes) && !byHand) return clearCoins();
    const routes = pack && pack.routes ? pack.routes
      : await api.coinRoutes(chain, sp.collateral.address, sp.borrowed.address);   // no Curve pool behind the collateral: rejects, and there is nothing to show
    const coins = (routes.coins || []).filter(c => Array.isArray(c.hops) && c.hops.length);
    if (sig !== coinSig) return;
    if (!coins.length) return clearCoins();
    const range = { from: g.t[0], to: g.t[n - 1], step: g.step };
    const sameArgs = (p, q) => JSON.stringify((p || []).map(String)) === JSON.stringify((q || []).map(String));
    const series = r => {
      if (!r) return Promise.resolve(null);
      const own = sp.oracle.sources.find(x => x.kind === "onchain" && !x.raw && String(x.address || "").toLowerCase() === r.to && String(x.sig || "").replace(/\s+/g, "") === r.sig
        && sameArgs(x.args, r.args) && (x.slot || 0) === r.slot && (x.decimals ?? 18) === r.decimals && (rt.sources[x.id] || {}).status === "ok");
      if (own) return Promise.resolve(rawArray(own.id));
      const packed = packSeries(pack, r);
      if (packed) return Promise.resolve(onGrid({ t: packed.t, v: packed.v, exact: true, held: !!packed.held }, g.t));
      if (!byHand) return Promise.resolve(null);
      return sampleOnce(chain, r, range).then(d => onGrid(d, g.t));
    };
    const built = await Promise.all(coins.map(async coin => {
      const v = new Float64Array(n).fill(1);
      for (const hop of coin.hops) {
        const [num, den, rn, rd] = await Promise.all([series(hop.num), series(hop.den), series(hop.rate_num), series(hop.rate_den)]);
        for (let i = 0; i < n; i++) v[i] *= ((num ? num[i] : 1) / (den ? den[i] : 1)) * ((rn ? rn[i] : 1) / (rd ? rd[i] : 1));
      }
      return { coin, v };
    }));
    if (sig !== coinSig) return;
    clearCoins();
    coinsHost.hidden = false;
    const quote = sp.borrowed.symbol || "the borrowed token";
    built.forEach(({ coin, v }, k) => {
      const plot = h("div", {}), link = linkOf();
      coinsHost.append(h("div", { class: "nl-oracle-cc" }, h("div", { class: "nl-oracle-cc-h" }, h("i", { style: { background: COIN_COLORS[k % COIN_COLORS.length] } }),
        h("b", {}, coin.symbol || shortAddr(coin.address)), ` in ${quote}`, h("span", { class: "nl-note" }, `last trade · ${coin.hops.map(x => x.name || shortAddr(x.pool)).join(" → ")}`)), plot));
      const ch = lineChart(plot, { height: 120, xMode: "time", yFmt: x => fmtNum(x, 5), zoom: !!opts.zoom, onView: x => link.onView(x), onHover: x => link.onHover(x) });
      link.me = ch;
      ch.setData({ t: g.t.subarray(a), series: [{ name: `${esc(coin.symbol || "coin")} in ${esc(quote)}`, v: v.subarray(a), color: COIN_COLORS[k % COIN_COLORS.length], width: 1.4, legend: false }] });
      coinCharts.push({ chart: ch });
    });
    if (coinView) for (const c of coinCharts) c.chart.setView(coinView.a, coinView.b);
  }
  // another component asks for a window (the MA sliders: the crash they are judged on)
  function focus() {
    const f = store.rt.focus;
    if (!f || !(f.b > f.a) || !hasData(store.rt)) return;
    chart.setView(f.a, f.b);
    coinView = chart.view;
    for (const c of coinCharts) c.chart.setView(coinView ? coinView.a : 0, coinView ? coinView.b : 0);
  }
  const schedule = frameScheduler(paint);
  paint();
  return {
    head: [],
    body: [empty, full],
    onTag(tag) {
      if (tag === "oracle.data" || tag === "oracle.sources" || tag === "oracle.script" || tag === "busy" || tag === "mode") schedule();
      else if (tag === "oracle.focus") focus();
    },
    destroy() { schedule.cancel(); clearCoins(); coinSig = "x"; chart.destroy(); regrid.clear(); },
  };
}

// =====================================================================================================
// mount
// =====================================================================================================
const PARTS = { range: rangePart, sources: sourcesPart, script: scriptPart, graph: graphPart, chart: chartPart };

export function mount(host, ctx, opts = {}) {
  const store = ctx.store;
  const which = !opts.part || opts.part === "all" ? "all" : String(opts.part);
  const names = which === "all" ? PART_ORDER : PART_ORDER.includes(which) ? [which] : [];
  const root = h("div", { class: `nl-oracle nl-oracle-is-${names.length ? which : "unknown"}` });
  host.append(root);
  if (!names.length) {
    root.append(h("div", { class: "nl-err" }, `oracle: unknown part "${which}". Use range, sources, script, graph, chart or all.`));
    return { destroy() { root.remove(); } };
  }
  let parts = [];
  const build = () => {
    parts = names.map(name => {
      let p;
      try { p = PARTS[name](store, opts, names.length === 1); }
      catch (e) {                                           // one broken part must not take the pane down
        console.error("[nl] oracle " + name, e);
        p = { body: [h("div", { class: "nl-err" }, `The ${name} part failed to start: ${e && e.message ? e.message : e}`)], onTag() {}, destroy() {} };
      }
      const title = names.length === 1 && opts.title ? opts.title : TITLES[name];
      const head = p.head && p.head.length ? h("span", { class: "nl-oracle-headtools" }, p.head) : null;
      p.el = opts.card === false
        ? h("div", { class: `nl-oracle-part nl-oracle-bare nl-oracle-part-${name}` }, head ? h("div", { class: "nl-oracle-barehead" }, head) : null, p.body)
        : card([h("span", { class: "nl-oracle-title" }, title), head], p.body);
      p.el.classList.add("nl-oracle-part", `nl-oracle-part-${name}`);
      return p;
    });
    root.replaceChildren(...parts.map(p => p.el));
  };
  const teardown = discard => { for (const p of parts) { try { p.destroy(discard); } catch (e) { console.error("[nl] oracle destroy", e); } } parts = []; };
  build();
  const off = store.on(tag => {
    if (tag === "spec") { teardown(true); build(); return; } // whole spec replaced: start over, pending edits belong to the old one
    for (const p of parts) p.onTag(tag);
  });
  return { destroy() { off(); teardown(false); root.remove(); } };
}
