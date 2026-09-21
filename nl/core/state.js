// The market spec (what gets saved) and the runtime
// data hanging off it (fetched series, evaluated oracle, results in flight).
import { defaultPolicy, POLICIES } from "./policy.js";

export const FORMAT = "new-llamalend/1";
const LS_KEY = "nl.spec.v2";   // v2: reUSD/sfrxUSD demo, per-part EMA, best-known A / fee / discount

// On-chain read presets: every getter an oracle stack is built from.
// slot = which 32-byte word of the return data; decimals = how it is scaled.
export const PRESETS = [
  { id: "price", label: "price()  ·  Curve oracle / aggregator", sig: "price()", args: [], slot: 0, rtype: "uint", decimals: 18 },
  { id: "price_oracle", label: "price_oracle()  ·  2-coin pool EMA", sig: "price_oracle()", args: [], slot: 0, rtype: "uint", decimals: 18 },
  { id: "price_oracle_i", label: "price_oracle(i)  ·  NG / multi-coin pool EMA", sig: "price_oracle(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18 },
  { id: "last_price_ng", label: "last_price(i)  ·  NG pool last trade (no EMA)", sig: "last_price(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18 },
  { id: "last_price_i", label: "last_prices(i)  ·  pool last trade (no EMA)", sig: "last_prices(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18 },
  { id: "get_p_i", label: "get_p(i)  ·  NG pool spot", sig: "get_p(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18 },
  { id: "vp", label: "get_virtual_price()  ·  pool", sig: "get_virtual_price()", args: [], slot: 0, rtype: "uint", decimals: 18 },
  { id: "lp_price", label: "lp_price()  ·  cryptoswap LP", sig: "lp_price()", args: [], slot: 0, rtype: "uint", decimals: 18 },
  { id: "chainlink", label: "latestRoundData()  ·  Chainlink feed", sig: "latestRoundData()", args: [], slot: 1, rtype: "int", decimals: 8 },
  { id: "latestAnswer", label: "latestAnswer()  ·  Chainlink (legacy)", sig: "latestAnswer()", args: [], slot: 0, rtype: "int", decimals: 8 },
  { id: "erc4626", label: "convertToAssets(1e18)  ·  ERC4626 vault rate", sig: "convertToAssets(uint256)", args: ["1000000000000000000"], slot: 0, rtype: "uint", decimals: 18 },
  { id: "pps", label: "pricePerShare()  ·  vault", sig: "pricePerShare()", args: [], slot: 0, rtype: "uint", decimals: 18 },
  { id: "priceAsCrvusd", label: "priceAsCrvusd()  ·  Resupply feed", sig: "priceAsCrvusd()", args: [], slot: 0, rtype: "uint", decimals: 18 },
  { id: "custom", label: "custom signature…", sig: "", args: [], slot: 0, rtype: "uint", decimals: 18 },
];

const POOL = "0xed785af60bed688baa8990cd5c4166221599a441";   // reUSD/sfrxUSD NG: coin 0 = reUSD, A = 200, ma_exp_time = 866
const BRIDGE = "0xc522a6606bba746d7960404f22a3db936b6f4f50";  // reUSD/scrvUSD NG: the pool the reUSD feed reads (priceAsCrvusd = 1 / its price_oracle)
// The external mark, as llamma-simulator_v2 (PR 9) builds it: the oracle's formula on the pools' LAST TRADED
// prices. The first default read the oracle's own EMA-smoothed legs, so "market" hugged the oracle and the
// February 2026 depeg (mark 0.976 against an oracle of 1.008) all but vanished from it.
const MARK_SOURCES = [
  { name: "p_last", kind: "onchain", address: POOL, preset: "last_price_ng", sig: "last_price(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18, raw: false, note: "LP pool: last traded price, no EMA", ema: { onchain: null, use: null } },
  { name: "bridge_last", kind: "onchain", address: BRIDGE, preset: "last_price_ng", sig: "last_price(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18, raw: false, note: "reUSD/scrvUSD pool: last traded price (crvUSD-side per reUSD is its inverse), no EMA", ema: { onchain: null, use: null } },
];
const OLD_MARK = "market       = lp_stable(vp, p, 200) * reusd_feed * agg", NEW_MARK = "market       = lp_stable(vp, p_last, 200) * inv(bridge_last) * agg";
const OLD_MARK_NOTE = "# what the LP actually trades at: no dampening, no cap", NEW_MARK_NOTE = "# the external mark: the same LP formula on the pools' LAST TRADED prices (no EMA, no dampening, no cap)";
export function defaultSpec() {
  return {
    format: FORMAT,
    meta: { name: "reUSD/sfrxUSD LP · crvUSD", author: "", notes: "", updated: 0, defaults: 4 },
    chain: "ethereum",
    collateral: { address: POOL, symbol: "reusdsfrx", name: "reUSD/sfrxUSD", decimals: 18 },
    borrowed: { address: "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e", symbol: "crvUSD", name: "Curve.Fi USD Stablecoin", decimals: 18 },
    params: {
      // current best from the parameter search: A 236, fee 0.0975 %, liquidation discount 1.36322 %
      A: 236, fee_pct: 0.0975, admin_fee_pct: 10,
      loan_discount_pct: 2, liquidation_discount_pct: 1.36322,
      borrow_cap: 2_000_000,
      // not a market parameter: the single EMA the ENGINE runs when a scenario has no
      // recorded oracle to replay (drawn / linear). Edited among the MA knobs only.
      ma_exp_time: 866,
    },
    // the monetary policy CONTRACT a market is created with: version + constructor
    // arguments per version. Documentation: neither simulation reads it.
    policy: defaultPolicy(),
    borrower: { mode: "cap", n_bands: 4, ltv_pct: null, collateral_usd: 2_100_000, debt_usd: 2_000_000 },
    venue: { pool_type: "stableswap-ng", tvl_usd: 3_000_000, A_raw: 270, ss_A: 200, n_coins: 2, state: null, note: "" },
    oracle: {
      // 5 min: an 866 s EMA is invisible on an hourly grid, and EMA timing is the point here
      // 240 d reaches back past the February 2026 reUSD depeg, the one real crash on record
      range: { days: 240, step_s: 300 },
      sources: [
        { id: "s1", name: "vp", kind: "onchain", address: POOL, preset: "vp", sig: "get_virtual_price()", args: [], slot: 0, rtype: "uint", decimals: 18, raw: false, note: "pool virtual price", ema: { onchain: null, use: null } },
        { id: "s2", name: "p", kind: "onchain", address: POOL, preset: "price_oracle_i", sig: "price_oracle(uint256)", args: [0], slot: 0, rtype: "uint", decimals: 18, raw: false, note: "pool price_oracle(0): the pool's own EMA of its last price", ema: { onchain: 866, use: null } },
        { id: "s3", name: "reusd_feed", kind: "onchain", address: "0x07ac1e016d4335fb833666ed5c43846162d2b7e8", preset: "priceAsCrvusd", sig: "priceAsCrvusd()", args: [], slot: 0, rtype: "uint", decimals: 18, raw: false, note: "reUSD in crvUSD, built from the reUSD pools' EMAs", ema: { onchain: 866, use: null } },
        { id: "s4", name: "agg", kind: "onchain", address: "0x18672b1b0c623a30089a280ed9256379fb0e4e62", preset: "price", sig: "price()", args: [], slot: 0, rtype: "uint", decimals: 18, raw: false, note: "crvUSD/USD aggregator over five pools' EMAs", ema: { onchain: 866, use: null } },
        { id: "s7", ...MARK_SOURCES[0] },
        { id: "s8", ...MARK_SOURCES[1] },
        { id: "s5", name: "ref_market", kind: "dataset", key: "pv2-reusd-sfrxusd-lp", column: "close", note: "llamma-simulator_v2 PR 9 dataset: LP market price (not read by the script; overlay it in the chart)", ema: { onchain: null, use: null } },
        { id: "s6", name: "ref_oracle", kind: "dataset", key: "pv2-reusd-sfrxusd-lp", column: "oracle", note: "llamma-simulator_v2 PR 9 dataset: its composed oracle", ema: { onchain: null, use: null } },
      ],
      script: [
        "# StableSwapNGLPOracle: portfolio value x dampened virtual price, in reUSD",
        "vp_ema_time  = 866                # the LP oracle's own deploy-time EMA on the virtual price",
        "lp_reusd     = lp_stable(asym_ema(vp, vp_ema_time), p, 200)",
        "reusd_crvusd = min(1, reusd_feed)",
        "lp_crvusd    = lp_reusd * reusd_crvusd",
        "oracle       = lp_crvusd * agg",
        "",
        NEW_MARK_NOTE,
        NEW_MARK,
      ].join("\n"),
    },
    scenario: {
      mode: "history",                      // history | draw | linear
      clips: [],                            // [{t0, t1, speed, amplify}] unix seconds
      // the crash picker: which recorded crash the single clip is (rank 0 = worst, of
      // windows this long), and how many times deeper its fall is played
      crash: { rank: 0, span_s: 259200 },      // 3 d: room for the fall and for what the oracle does after it
      worse: 1,
      // history mode only: false = the picked crash plays, true = the hand-cut clips play
      by_hand: false,
      points: [[0, 1], [1800, 0.99], [3600, 0.9], [7200, 0.86], [14400, 0.9]],   // [seconds, x start price]
      tool: "points",                       // points | freehand (both edit `points`)
      linear: { drop_pct: 10, duration_min: 60 },
      lead_in_min: 10, tail_min: 60,
      start_price: null,                    // null = the oracle's latest value
      oracle_mode: "recorded",              // recorded | ema
    },
    sldl: { a_min: 118, a_max: 354, fee_min: 0.025, fee_max: 0.29, grid: 15, bands: 4, tail_pct: 0.05, loan_days: 2 },
    results: { baddebt: null, sldl: null },
  };
}

function freshRt() {
  return {
    sources: {},          // id -> {status: idle|loading|ok|error, t, v, n, err, progress, sig, live}
    grid: null,           // {t: Float64Array, step}
    vars: {},             // script variables on the grid
    oracle: null, market: null, valid0: 0,
    evalErrors: [], compileErrors: [], rescaled: false,
    scenario: null,       // {t, market, oracle, label}
    busy: {},             // {sources|baddebt|sldl: {label, done, total}}
  };
}

function migrate(spec) {
  const d = defaultSpec();
  if (!spec || typeof spec !== "object") return d;
  const out = { ...d, ...spec };
  for (const k of ["meta", "collateral", "borrowed", "params", "borrower", "venue", "scenario", "sldl", "results"])
    out[k] = { ...d[k], ...(spec[k] || {}) };
  out.oracle = { ...d.oracle, ...(spec.oracle || {}) };
  out.oracle.range = { ...d.oracle.range, ...((spec.oracle || {}).range || {}) };
  out.oracle.sources = Array.isArray((spec.oracle || {}).sources) ? spec.oracle.sources : d.oracle.sources;
  out.scenario.linear = { ...d.scenario.linear, ...((spec.scenario || {}).linear || {}) };
  out.scenario.crash = { ...d.scenario.crash, ...((spec.scenario || {}).crash || {}) };
  // the run settings have no editor any more: every run has the same lead-in and tail, starts at the latest
  // price and replays the recorded oracle wherever the path has one
  Object.assign(out.scenario, { lead_in_min: d.scenario.lead_in_min, tail_min: d.scenario.tail_min, start_price: null, oracle_mode: "recorded" });
  // specs from before the flag: clips that were cut by hand keep playing
  if ((spec.scenario || {}).by_hand === undefined) out.scenario.by_hand = (out.scenario.clips || []).some(c => c.t1 > c.t0);
  // rates used to live in params; an LLV2 market takes a policy contract instead
  for (const k of ["rate_apr_pct", "rate_min_apr_pct", "rate_max_apr_pct"]) delete out.params[k];
  out.policy = { ...d.policy, ...(spec.policy || {}) };
  for (const P of POLICIES) out.policy[P.id] = { ...P.defaults, ...((spec.policy || {})[P.id] || {}) };
  if (!POLICIES.some(P => P.id === out.policy.version)) out.policy.version = d.policy.version;
  // the borrower is not configurable (see pipeline.resolveBorrower): whatever an
  // older export carried, it is the fixed worst-case position
  out.borrower = { ...d.borrower };
  // defaults that moved: a saved spec still on the old default follows once (meta.defaults marks it done)
  if (!((spec.meta || {}).defaults >= 2)) { if (out.sldl.grid === 8) out.sldl.grid = 15; out.meta.defaults = 2; }
  // v3: the demo's market line read the oracle's smoothed legs. A spec that still carries exactly that line gets the real mark.
  if (!((spec.meta || {}).defaults >= 3)) {
    const names = new Set(out.oracle.sources.map(x => x.name));
    if (String(out.oracle.script || "").includes(OLD_MARK) && !MARK_SOURCES.some(m => names.has(m.name))) {
      const ids = new Set(out.oracle.sources.map(x => x.id));
      out.oracle.sources = [...out.oracle.sources, ...MARK_SOURCES.map(m => { let k = 1; while (ids.has("s" + k)) k++; ids.add("s" + k); return { id: "s" + k, ...JSON.parse(JSON.stringify(m)) }; })];
      out.oracle.script = out.oracle.script.replace(OLD_MARK, NEW_MARK).replace(OLD_MARK_NOTE, NEW_MARK_NOTE);
      out.results = { ...out.results, baddebt: null, sldl: null };      // computed on the old market path
    }
    out.meta.defaults = 3;
  }
  // v4: the crash window opens at 3 d; a spec still on the old 1 d default follows once
  if (!((spec.meta || {}).defaults >= 4)) {
    if (+out.scenario.crash.span_s === 86400) { out.scenario.crash.span_s = 259200; out.results = { ...out.results, baddebt: null }; }
    out.meta.defaults = 4;
  }
  out.format = FORMAT;
  return out;
}

export class Store {
  constructor() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch (_) { /* private mode */ }
    this.spec = migrate(saved);
    this.rt = freshRt();
    this.listeners = new Set();
    this._t = null;
  }
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(tag) { for (const fn of [...this.listeners]) { try { fn(tag); } catch (e) { console.error("[nl] listener", e); } } }
  // tags: tokens params borrower venue oracle.sources oracle.script oracle.range
  //       oracle.data scenario scenario.data sldl results busy meta spec
  update(tag, fn) {
    fn(this.spec);
    this.spec.meta.updated = Math.floor(Date.now() / 1000);
    this.persist();
    this.emit(tag);
  }
  setRt(tag, patch) { Object.assign(this.rt, patch); this.emit(tag); }
  setBusy(key, val) { if (val) this.rt.busy[key] = val; else delete this.rt.busy[key]; this.emit("busy"); }
  persist() {
    clearTimeout(this._t);
    this._t = setTimeout(() => {
      try {
        // results can be megabytes; keep storage for the config
        const slim = { ...this.spec, results: { baddebt: null, sldl: null } };
        localStorage.setItem(LS_KEY, JSON.stringify(slim));
      } catch (_) { /* quota / private mode: the session still works */ }
    }, 250);
  }
  replace(spec) {
    const pub = this.rt.public;                   // what kind of server this is does not change with the market
    this.spec = migrate(spec);
    this.rt = freshRt();
    if (pub) this.rt.public = pub;
    this.persist();
    this.emit("spec");
  }
  reset() { this.replace(defaultSpec()); }
  nextSourceId() {
    let k = 1;
    const ids = new Set(this.spec.oracle.sources.map(s => s.id));
    while (ids.has("s" + k)) k++;
    return "s" + k;
  }
}
