// The oracle script: the small language a researcher writes an oracle in.
//
//   lp_reusd     = lp_oracle                       # a source, by its name
//   reusd_crvusd = min(1, reusd_feed)
//   lp_crvusd    = lp_reusd * reusd_crvusd
//   oracle       = lp_crvusd * agg                 # what LLAMMA reads
//   market       = lp_spot                         # optional: what it trades at
//
// One assignment per line, '#' comments. Values are numbers or series on the
// shared grid; every operator and function works elementwise, NaN-propagating
// (NaN = "this source had no data yet").
//
// Vyper-style integer code pastes in unchanged: 10**18 literals work, and
// `a * b // 10**18` is accepted. Sources can be switched to raw (unscaled)
// units for that; the pipeline rescales a final result that is still 1e18-big.
import { ema, asymEma, portfolioValue, portfolioValues } from "./series.js";

const FUNCS = new Set(["min", "max", "abs", "sqrt", "exp", "log", "pow", "clamp", "inv",
  "ema", "ema_hl", "asym_ema", "lag", "lp_stable", "portfolio_value", "floor"]);

export const FUNC_DOCS = [
  ["min(a, b, ...)", "elementwise minimum; min(1, feed) is the contract's min(10**18, feed)"],
  ["max(a, b, ...)", "elementwise maximum"],
  ["clamp(x, lo, hi)", "x limited to [lo, hi]"],
  ["inv(x)", "1 / x: flip a quote"],
  ["ema(x, ma_exp_time)", "Curve EMA as pools and curve_std run it: exp(-dt / ma_exp_time) weights, previous sample queued. 866 = the usual 10 min half-life"],
  ["ema_hl(x, half_life)", "the same EMA given as a half-life in seconds"],
  ["asym_ema(x, ema_time)", "StableSwapNGLPOracle's dampened virtual price: up-moves through the EMA, down-moves at once, reports min(spot, ema)"],
  ["lag(x, seconds)", "x delayed by a fixed time: a slow or stale feed"],
  ["lp_stable(vp, p, A)", "2-coin StableSwap LP price in coin 0: portfolio_value(A, p) * vp, p = pool price_oracle, A = pool A()"],
  ["portfolio_value(A, p)", "the bare lp_oracle_2 math: x + p*y on the invariant at marginal price p (D = 1)"],
  ["abs, sqrt, exp, log, pow(a, b), floor", "plain math"],
];

// ---- tokenizer ---------------------------------------------------------------
function tokenize(src, line) {
  const out = [];
  let i = 0;
  const err = msg => { const e = new Error(msg); e.line = line; throw e; };
  while (i < src.length) {
    const c = src[i];
    if (c === " " || c === "\t") { i++; continue; }
    if (c === "#") break;
    if (/[0-9.]/.test(c)) {
      let j = i;
      while (j < src.length && /[0-9_.]/.test(src[j])) j++;
      if (/[eE]/.test(src[j] || "") && /[-+0-9]/.test(src[j + 1] || "")) {
        j += 2;
        while (j < src.length && /[0-9]/.test(src[j])) j++;
      }
      const num = Number(src.slice(i, j).replace(/_/g, ""));
      if (!Number.isFinite(num)) err(`bad number "${src.slice(i, j)}"`);
      out.push({ k: "num", v: num });
      i = j;
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      let j = i;
      while (j < src.length && /[A-Za-z0-9_.]/.test(src[j])) j++;
      // `feed.price()` style: a method call on a source is just the source
      let name = src.slice(i, j);
      if (name.includes(".")) {
        name = name.split(".")[0];
        let k = j;
        while (src[k] === " ") k++;
        if (src[k] === "(") {                        // swallow "( ... )"
          let depth = 0;
          do { if (src[k] === "(") depth++; else if (src[k] === ")") depth--; k++; }
          while (k < src.length && depth > 0);
          j = k;
        }
      }
      out.push({ k: "id", v: name });
      i = j;
      continue;
    }
    const two = src.slice(i, i + 2);
    if (two === "**" || two === "//") { out.push({ k: "op", v: two }); i += 2; continue; }
    if ("+-*/(),=".includes(c)) { out.push({ k: "op", v: c }); i++; continue; }
    err(`unexpected character "${c}"`);
  }
  return out;
}

// ---- parser (precedence climbing) --------------------------------------------
function parseExpr(toks, line) {
  let p = 0;
  const err = msg => { const e = new Error(msg); e.line = line; throw e; };
  const peek = () => toks[p], next = () => toks[p++];
  const isOp = v => peek() && peek().k === "op" && peek().v === v;

  function primary() {
    const t = next();
    if (!t) err("expression ends early");
    if (t.k === "num") return { k: "num", v: t.v };
    if (t.k === "id") {
      if (isOp("(")) {
        next();
        const args = [];
        if (!isOp(")")) { do { args.push(add()); } while (isOp(",") && next()); }
        if (!isOp(")")) err(`missing ")" after ${t.v}(`);
        next();
        if (!FUNCS.has(t.v)) err(`unknown function ${t.v}()`);
        return { k: "call", fn: t.v, args };
      }
      return { k: "ref", name: t.v };
    }
    if (t.k === "op" && t.v === "(") {
      const e = add();
      if (!isOp(")")) err('missing ")"');
      next();
      return e;
    }
    if (t.k === "op" && t.v === "-") return { k: "neg", a: unary() };
    if (t.k === "op" && t.v === "+") return unary();
    err(`unexpected "${t.v}"`);
  }
  function unary() { return primary(); }
  function power() {
    const base = unary();
    if (isOp("**")) { next(); return { k: "bin", op: "**", a: base, b: power() }; }
    return base;
  }
  function mul() {
    let e = power();
    while (isOp("*") || isOp("/") || isOp("//")) {
      const op = next().v;
      e = { k: "bin", op, a: e, b: power() };
    }
    return e;
  }
  function add() {
    let e = mul();
    while (isOp("+") || isOp("-")) {
      const op = next().v;
      e = { k: "bin", op, a: e, b: mul() };
    }
    return e;
  }
  const e = add();
  if (p < toks.length) err(`unexpected "${toks[p].v}"`);
  return e;
}

export function refsOf(ast, out = new Set()) {
  if (!ast) return out;
  if (ast.k === "ref") out.add(ast.name);
  else if (ast.k === "neg") refsOf(ast.a, out);
  else if (ast.k === "bin") { refsOf(ast.a, out); refsOf(ast.b, out); }
  else if (ast.k === "call") ast.args.forEach(a => refsOf(a, out));
  return out;
}

// compile(script) -> { lines: [{name, ast, line, text}], errors: [{line, msg}] }
export function compile(script) {
  const lines = [], errors = [];
  String(script || "").split("\n").forEach((raw, idx) => {
    const line = idx + 1;
    const text = raw.replace(/#.*$/, "").trim();
    if (!text) return;
    try {
      const toks = tokenize(text, line);
      if (toks.length < 3 || toks[0].k !== "id" || toks[1].k !== "op" || toks[1].v !== "=")
        throw Object.assign(new Error('expected "name = expression"'), { line });
      if (FUNCS.has(toks[0].v))
        throw Object.assign(new Error(`"${toks[0].v}" is a function name`), { line });
      lines.push({ name: toks[0].v, ast: parseExpr(toks.slice(2), line), line, text });
    } catch (e) {
      errors.push({ line: e.line || line, msg: e.message });
    }
  });
  return { lines, errors };
}

// names the script reads that it does not define itself = the sources it needs
export function sourcesUsed(compiled) {
  const defined = new Set(), used = new Set();
  for (const l of compiled.lines) {
    for (const r of refsOf(l.ast)) if (!defined.has(r)) used.add(r);
    defined.add(l.name);
  }
  return used;
}

// ---- evaluation --------------------------------------------------------------
const isSeries = x => x instanceof Float64Array;
function map2(a, b, f) {
  if (!isSeries(a) && !isSeries(b)) return f(a, b);
  const n = isSeries(a) ? a.length : b.length, out = new Float64Array(n);
  if (isSeries(a) && isSeries(b)) for (let i = 0; i < n; i++) out[i] = f(a[i], b[i]);
  else if (isSeries(a)) for (let i = 0; i < n; i++) out[i] = f(a[i], b);
  else for (let i = 0; i < n; i++) out[i] = f(a, b[i]);
  return out;
}
function map1(a, f) {
  if (!isSeries(a)) return f(a);
  const out = new Float64Array(a.length);
  for (let i = 0; i < a.length; i++) out[i] = f(a[i]);
  return out;
}
const scalar = (x, what) => {
  if (isSeries(x)) throw new Error(`${what} must be a plain number`);
  return x;
};
const asSeries = (x, n) => isSeries(x) ? x : new Float64Array(n).fill(x);
// Vyper's // on 1e18-scaled integers floors; on already-normalised numbers
// flooring would zero everything, so small results divide exactly.
const intDiv = (a, b) => { const q = a / b; return Math.abs(q) >= 1e9 ? Math.floor(q) : q; };

function evalAst(ast, env, grid) {
  switch (ast.k) {
    case "num": return ast.v;
    case "ref": {
      if (!(ast.name in env)) throw new Error(`"${ast.name}" is not a source or an earlier line`);
      return env[ast.name];
    }
    case "neg": return map1(evalAst(ast.a, env, grid), x => -x);
    case "bin": {
      const a = evalAst(ast.a, env, grid), b = evalAst(ast.b, env, grid);
      switch (ast.op) {
        case "+": return map2(a, b, (x, y) => x + y);
        case "-": return map2(a, b, (x, y) => x - y);
        case "*": return map2(a, b, (x, y) => x * y);
        case "/": return map2(a, b, (x, y) => x / y);
        case "//": return map2(a, b, intDiv);
        case "**": return map2(a, b, (x, y) => Math.pow(x, y));
      }
      throw new Error(`operator ${ast.op}`);
    }
    case "call": {
      const A = ast.args.map(x => evalAst(x, env, grid)), n = grid.t.length;
      const need = k => { if (A.length !== k) throw new Error(`${ast.fn}() takes ${k} argument${k > 1 ? "s" : ""}`); };
      switch (ast.fn) {
        case "min": if (!A.length) need(1); return A.reduce((p, q) => map2(p, q, Math.min));
        case "max": if (!A.length) need(1); return A.reduce((p, q) => map2(p, q, Math.max));
        case "abs": need(1); return map1(A[0], Math.abs);
        case "sqrt": need(1); return map1(A[0], Math.sqrt);
        case "exp": need(1); return map1(A[0], Math.exp);
        case "log": need(1); return map1(A[0], Math.log);
        case "floor": need(1); return map1(A[0], Math.floor);
        case "inv": need(1); return map1(A[0], x => 1 / x);
        case "pow": need(2); return map2(A[0], A[1], Math.pow);
        case "clamp": need(3); return map2(map2(A[0], A[1], Math.max), A[2], Math.min);
        case "ema": need(2); return ema(asSeries(A[0], n), grid.t, scalar(A[1], "ema time"));
        case "ema_hl": need(2); return ema(asSeries(A[0], n), grid.t, scalar(A[1], "half-life") / Math.LN2);
        case "asym_ema": need(2); return asymEma(asSeries(A[0], n), grid.t, scalar(A[1], "ema time"));
        case "lag": {
          need(2);
          // by time, not by index: the grid is not evenly spaced where blocks changed something
          const x = asSeries(A[0], n), d = scalar(A[1], "lag"), T = grid.t, out = new Float64Array(n).fill(NaN);
          let j = -1;
          for (let i = 0; i < n; i++) { while (j + 1 < n && T[j + 1] <= T[i] - d) j++; if (j >= 0) out[i] = x[j]; }
          return out;
        }
        case "portfolio_value": {
          need(2);
          const Av = A[0];
          if (isSeries(A[1]) && !isSeries(Av)) return portfolioValues(Av, A[1]);
          return map1(A[1], p => portfolioValue(isSeries(Av) ? NaN : Av, p));
        }
        case "lp_stable": {
          need(3);
          const Aamp = scalar(A[2], "A");
          if (isSeries(A[1])) return map2(A[0], portfolioValues(Aamp, A[1]), (vp, pv) => vp * pv);
          return map2(A[0], A[1], (vp, p) => vp * portfolioValue(Aamp, p));
        }
      }
      throw new Error(`function ${ast.fn}`);
    }
  }
  throw new Error("bad expression");
}

// evaluate(compiled, sources, grid) -> { vars, errors }
//   sources: { name: Float64Array | number }   grid: { t: Float64Array, step }
// memo (a Map the caller keeps between calls): a line whose inputs are the very same arrays / numbers as last time, on the
// same grid, returns its last result, so a change to one source recomputes only the lines downstream of it. The memo holds
// the lines of the latest call only.
export function evaluate(compiled, sources, grid, memo) {
  const env = { ...sources }, vars = {}, errors = [], next = memo ? new Map() : null;
  for (const l of compiled.lines) {
    try {
      let v;
      if (memo) {
        const refs = [...refsOf(l.ast)], key = l.line + ":" + l.text, hit = memo.get(key);
        if (hit && hit.grid === grid && hit.deps.length === refs.length && refs.every((r, i) => hit.deps[i] === env[r])) v = hit.v;
        else v = evalAst(l.ast, env, grid);
        next.set(key, { grid, deps: refs.map(r => env[r]), v });
      } else v = evalAst(l.ast, env, grid);
      env[l.name] = vars[l.name] = v;
    } catch (e) {
      errors.push({ line: l.line, msg: e.message });
    }
  }
  if (memo) { memo.clear(); for (const [k, v] of next) memo.set(k, v); }
  return { vars, errors };
}

// ---- graph view --------------------------------------------------------------
// nodes: sources the script reads + every assigned name; edges: reads.
export function graphOf(compiled, sourceNames) {
  const nodes = new Map(), edges = [];
  const defined = new Set();
  const opLabel = ast => ast.k === "call" ? ast.fn + "()"
    : ast.k === "bin" ? ({ "*": "×", "/": "÷", "//": "÷", "+": "+", "-": "−", "**": "^" })[ast.op]
    : ast.k === "neg" ? "−" : "=";
  for (const l of compiled.lines) {
    for (const r of refsOf(l.ast)) {
      if (!defined.has(r) && !nodes.has(r))
        nodes.set(r, { id: r, kind: sourceNames.includes(r) ? "source" : "missing" });
      edges.push({ from: r, to: l.name });
    }
    nodes.set(l.name, { id: l.name, kind: l.name === "oracle" ? "oracle"
      : l.name === "market" ? "market" : "var", op: opLabel(l.ast), text: l.text });
    defined.add(l.name);
  }
  // depth = longest path from a leaf, for a left-to-right layered layout
  const depth = new Map();
  const d = id => {
    if (depth.has(id)) return depth.get(id);
    depth.set(id, 0);
    const ins = edges.filter(e => e.to === id && e.from !== id).map(e => d(e.from));
    const v = ins.length ? Math.max(...ins) + 1 : 0;
    depth.set(id, v);
    return v;
  };
  for (const id of nodes.keys()) nodes.get(id).depth = d(id);
  return { nodes: [...nodes.values()], edges };
}

// ---- the smoothing the SCRIPT itself applies -----------------------------------
// Finds every ema(x, T) / ema_hl / asym_ema / lag call and every `name = number`
// line, with the character span of the number, so a panel can offer each as a
// knob and write the new value back into the text without re-formatting it.
const TIME_FUNCS = { ema: "EMA time", ema_hl: "half-life", asym_ema: "EMA time (up-moves only)", lag: "delay" };
export function smoothingOf(script) {
  const text = String(script || ""), calls = [], consts = new Map();
  let off = 0;
  text.split("\n").forEach((raw, idx) => {
    const code = raw.replace(/#.*$/, "");
    const mc = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9][0-9_.]*(?:[eE][-+]?[0-9]+)?)\s*$/.exec(code);
    if (mc) {
      const start = off + code.indexOf(mc[2], code.indexOf("=") + 1);
      consts.set(mc[1], { name: mc[1], value: Number(mc[2].replace(/_/g, "")), start, end: start + mc[2].length, line: idx + 1 });
    }
    const re = /\b(ema_hl|asym_ema|ema|lag)\s*\(/g;
    let m;
    while ((m = re.exec(code))) {
      let depth = 1, k = m.index + m[0].length, lastComma = -1;
      for (; k < code.length && depth > 0; k++) {
        const c = code[k];
        if (c === "(") depth++;
        else if (c === ")") depth--;
        else if (c === "," && depth === 1) lastComma = k;
      }
      if (depth !== 0 || lastComma < 0) continue;
      const argRaw = code.slice(lastComma + 1, k - 1), arg = argRaw.trim();
      const start = off + lastComma + 1 + argRaw.indexOf(arg);
      const target = code.slice(m.index + m[0].length, lastComma).trim();
      const lhs = (/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/.exec(code) || [])[1] || "";
      calls.push({ fn: m[1], what: TIME_FUNCS[m[1]], target, lhs, line: idx + 1, arg, start, end: start + arg.length,
        literal: /^[0-9][0-9_.]*(?:[eE][-+]?[0-9]+)?$/.test(arg) });
    }
    off += raw.length + 1;
  });
  for (const c of calls) {
    if (c.literal) c.value = Number(c.arg.replace(/_/g, ""));
    else if (consts.has(c.arg)) { const k = consts.get(c.arg); c.value = k.value; c.via = k.name; c.start = k.start; c.end = k.end; }
    else c.value = NaN;
  }
  return { calls, consts: [...consts.values()] };
}
export function setNumberAt(script, start, end, value) {
  const s = String(script);
  return s.slice(0, start) + String(value) + s.slice(end);
}
