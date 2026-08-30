// Browser-side S.L./D.L. runner: replicates sweeps/sweep_ref_table.py (v1,
// cpp/exact branch) and sweeps/sweep_v2_table.py on top of the wasm engine
// modules in /wasm/, producing the exact table JSON renderTable() consumes.
// Environment-agnostic core (deps injected) so wasm/test/e2e_node.mjs can
// run the identical pipeline under Node.
import {
  aGrid, bestA, buildOracleV1, buildOracleV2, clampParams, feeGrid,
  klToV2Market, marketWithHeader, parseUsdBin, pyRound, spread, usdLookup,
  withHeader,
} from "./sldl_shared.js";

const EXT_FEE = 5e-4;
const DYN_MULT = 0.25;

const utc = s => {
  const d = new Date(s * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-` +
    `${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

// deps: { fetchBin(name, onFrac) -> Uint8Array,   // /sldl_data/<name>
//         fetchJson(name) -> object,
//         factory(model) -> Emscripten module factory,  // "v1" | "v2"
//         threads: int }
export function createRunner(deps) {
  const cache = new Map(); // sourceKey -> Float64Array (v1 kl rows, t ms)
  let zchfV2Bytes = null, usdTab = null, hourly = null;

  async function runEngine(model, files, argv, onLine, onErrLine) {
    const lines = [];
    let exitResolve;
    const exited = new Promise(r => (exitResolve = r));
    const mod = await (await deps.factory(model))({
      print: s => { lines.push(s); if (onLine) onLine(s); },
      printErr: s => { if (onErrLine) onErrLine(s); },
      onExit: code => exitResolve(code),
    });
    const old = Date.now() - 60000;
    for (const [name, bytes] of files) {
      if (name.endsWith(".json.bin")) {
        const twin = name.slice(0, -4);
        mod.FS.writeFile(twin, new Uint8Array(0));
        mod.FS.utime(twin, old, old);
      }
      mod.FS.writeFile(name, bytes);
    }
    mod.callMain(argv);
    const code = await exited;
    if (code !== 0) throw new Error(`engine exited ${code}`);
    return { lines, FS: mod.FS };
  }

  async function loadSource(srcKey, onFrac) {
    if (!cache.has(srcKey)) {
      const bytes = await deps.fetchBin(
        srcKey === "zchf" ? "zchf.bin" : `${srcKey}.bin`, onFrac);
      cache.set(srcKey, new Float64Array(
        bytes.buffer, bytes.byteOffset, bytes.byteLength / 8));
    }
    return cache.get(srcKey);
  }
  async function loadUsd() {
    if (!usdTab) usdTab = parseUsdBin(await deps.fetchBin("usd_1m.bin"));
    if (!hourly) {
      try { hourly = await deps.fetchJson("usd_hourly.json"); }
      catch (e) { hourly = {}; }
    }
    return { usdTab, hourly };
  }

  // params: the POST /sldl_run body; meta: the source's meta row from
  // /sldl_sources (+ client.zchf_v2_meta for v2 zchf); onTick(done, total).
  async function run(params, meta, onTick) {
    const t0 = Date.now();
    const model = params.model === "v2" ? "v2" : "v1";
    const p = clampParams({ ...params, model });
    const A = aGrid(p.a_min, p.a_max, p.grid);
    const F = feeGrid(p.fee_min, p.fee_max, p.grid);
    const R = p.realities;
    const FC_N = 15;
    const prepTicks = model === "v2" ? 1 : 3;
    const total = prepTicks + A.length * F.length * R + FC_N;
    let done = 0;
    const tick = n => { done += n; onTick(Math.min(done, total), total); };
    const frac = f => onTick(Math.min(done + f * prepTicks, total), total);

    // ---- data + engine input files ---------------------------------------
    const texp = p.oracle_hl;
    const threads = String(deps.threads);
    let files, gridArgv, srcMeta;
    if (model === "v2" && params.source === "zchf") {
      if (!zchfV2Bytes) zchfV2Bytes = await deps.fetchBin(
        "zchf_v2_market.bin", frac);
      await loadUsd();
      const mkt = new Float64Array(zchfV2Bytes.buffer,
        zchfV2Bytes.byteOffset + 8, (zchfV2Bytes.byteLength - 8) / 8);
      const orc = buildOracleV2(mkt, texp,
        i => usdTab.vals[(mkt[i * 5] - usdTab.t0) / 60]);
      files = [["/market.json.bin", zchfV2Bytes],
               ["/oracle.json.bin", withHeader(orc)]];
      // {n, from, to, span_h} of the v2 author market + the fixed labels
      // ensure_cache() hardcodes (sweep_v2_table.py)
      srcMeta = { ...meta, symbol: "ZCHF",
        pool_name: "author dataset: zchf_crvusd_1m (ZCHF/crvUSD) " +
                   "x crvUSD/USD aggregate, 1-min" };
    } else if (model === "v2") {
      const kl = await loadSource(params.source, frac);
      const mkt = klToV2Market(kl);
      const orc = buildOracleV2(mkt, texp, null);
      files = [["/market.json.bin", marketWithHeader(mkt)],
               ["/oracle.json.bin", withHeader(orc)]];
      srcMeta = meta;
    } else {
      const kl = await loadSource(params.source, f => frac(f * 0.7));
      const { usdTab: ut, hourly: hr } = await loadUsd();
      const orc = buildOracleV1(kl, texp, usdLookup(ut, hr));
      files = [["/klines.json.bin",
                new Uint8Array(kl.buffer, kl.byteOffset, kl.byteLength)],
               ["/oracle.json.bin",
                new Uint8Array(orc.buffer, 0, orc.byteLength)]];
      srcMeta = meta;
    }
    tick(prepTicks);

    // ---- grid stage ------------------------------------------------------
    const cells = [];
    let n_all = null, n_top = null;
    const onCell = line => {
      if (!line.startsWith("{")) return;
      const c = JSON.parse(line);
      if (model === "v2") {
        cells.push({ A: pyRound(c.A), fee_pct: pyRound(c.fee * 100, 4),
          loss_pct: pyRound(c.loss_pct, 5), max_pct: pyRound(c.max_pct, 5),
          n_sims: c.n_sims, secs: c.secs, transfer: c.transfer });
      } else {
        cells.push({ A: pyRound(c.A), fee_pct: c.fee_pct,
          loss_pct: pyRound(c.loss_pct, 5), n_sims: c.n_sims, secs: c.secs,
          max_pct: pyRound(c.max_pct, 5), transfer: c.transfer });
      }
      n_all = c.n_all; n_top = c.m;
      if (R === 1) tick(1);
    };
    const onErr = line => { if (line.startsWith("reality ") && R > 1) tick(1); };
    if (model === "v2") {
      const warmup = Math.ceil(10 * texp / 60);
      gridArgv = ["--market", "/market.json", "--oracle", "/oracle.json",
        "--a-list", A.join(","),
        "--fee-list", F.map(f => String(f / 100)).join(","),
        "--length", String(Math.max(2, pyRound(p.loan_days * 1440))),
        "--bands", String(p.bands), "--ext-fee", String(EXT_FEE),
        "--dyn-mult", String(DYN_MULT), "--threads", threads,
        "--warmup", String(warmup), "--tail-frac", String(p.tail_pct / 100),
        "--auto", "--oracle-hl", String(texp), "--realities", String(R)];
      await runEngine("v2", files, gridArgv, onCell, onErr);
      var v2warmup = warmup;
    } else {
      gridArgv = ["--klines", "/klines.json",
        "--a-list", A.join(","), "--fee-list", F.join(","),
        "--range-size", String(p.bands), "--loan-days", String(p.loan_days),
        "--texp", String(texp), "--ext-fee", String(EXT_FEE),
        "--auto", "--tail-frac", String(p.tail_pct / 100),
        "--realities", String(R),
        "--oracle", "/oracle.json", "--threads", threads];
      await runEngine("v1", files, gridArgv, onCell, onErr);
    }

    // ---- fee-curve stage -------------------------------------------------
    const fcFees = feeGrid(0.015, 0.5, 15);
    let fee_curve = null;
    const best_A = cells.every(c => c.max_pct != null)
      ? bestA(cells, p.bands) : null;
    if (model === "v2" && n_all && best_A != null) {
      const L3 = 3 * 1440;
      const warmup = v2warmup;
      const starts = [];
      for (let s = warmup; s < n_all - L3; s += 400) starts.push(s);
      const startsBytes = new TextEncoder().encode(JSON.stringify(starts));
      const avg = [];
      for (const f of fcFees) {
        const { FS } = await runEngine("v2",
          [...files, ["/starts.json", startsBytes]],
          ["--market", "/market.json", "--oracle", "/oracle.json",
           "--starts", "/starts.json", "--A", String(best_A),
           "--fee", String(f / 100), "--length", String(L3),
           "--bands", String(p.bands), "--ext-fee", String(EXT_FEE),
           "--dyn-mult", String(DYN_MULT), "--threads", threads,
           "--out", "/out.f64"]);
        const raw = FS.readFile("/out.f64");
        const vals = new Float64Array(raw.buffer, raw.byteOffset,
                                      raw.byteLength / 8);
        let s = 0, n = 0;
        for (const v of vals) if (v === v) { s += v; n++; }
        avg.push(pyRound((100 * s) / n, 5));
        tick(1);
      }
      fee_curve = { A: best_A, loan_days: 3,
        kind: `${starts.length.toLocaleString("en-US")} loans, ` +
              "every 400th minute",
        fee_pct: fcFees, avg_loss_pct: avg };
    } else if (model === "v1" && best_A != null) {
      const fc = [];
      await runEngine("v1", files,
        ["--klines", "/klines.json",
         "--a-list", String(best_A), "--fee-list", fcFees.join(","),
         "--range-size", String(p.bands), "--loan-days", "3",
         "--texp", String(texp), "--ext-fee", String(EXT_FEE),
         "--samples", "4000", "--n-top", "4000", "--seed", "1",
         "--oracle", "/oracle.json", "--threads", threads],
        l => { if (l.startsWith("{")) { fc.push(JSON.parse(l)); tick(1); } });
      if (fc.length === fcFees.length)
        fee_curve = { A: best_A, loan_days: 3, kind: "4,000 sampled loans",
          fee_pct: fc.map(c => c.fee_pct),
          avg_loss_pct: fc.map(c => pyRound(c.loss_pct, 5)) };
      else tick(fcFees.length - fc.length);
    }
    tick(total - done); // top up (skipped stage / short output)

    // ---- result (mirrors the sweep scripts' result dicts) ----------------
    const history = {
      base_symbol: srcMeta.symbol ?? srcMeta.base_symbol ?? params.source,
      pool_name: srcMeta.pool_name ?? srcMeta.source ?? "",
      span_h: srcMeta.span_h ?? pyRound((srcMeta.to - srcMeta.from) / 3600, 1),
      n_points: srcMeta.n,
      from_utc: utc(srcMeta.from), to_utc: utc(srcMeta.to),
    };
    const common = {
      range_size: p.bands, loan_days: p.loan_days, realities: R,
      n_all, n_top, tail_pct: p.tail_pct, texp_s: texp,
      ext_fee: EXT_FEE, client: true,
    };
    const config = model === "v2"
      ? { model: "llamma-simulator_v2 port, C++ (cpp/src/ref_model_v2.cpp)",
          model_variant: "v2", backend: "cpp", collateral: history.base_symbol,
          history, ...common, method: "exact", samples: null,
          oracle_mode: "usd-basis", dyn_mult: DYN_MULT, warmup: v2warmup,
          add_reverse: false }
      : { model: "llamma-simulator port, C++ (cpp/src/ref_model.cpp)",
          backend: "cpp", collateral: history.base_symbol, history, ...common,
          method: "exact", samples: null, oracle_mode: "usd-basis",
          add_reverse: true };
    return { mode: "table", config, grid: { A, fee_pct: F }, cells,
      fee_curve, runtime_s: pyRound((Date.now() - t0) / 1000, 1),
      generated_at: Math.floor(Date.now() / 1000) };
  }

  return { run };
}

// ---- browser wiring --------------------------------------------------------
let runner = null;
const factories = {};
function browserFactory(model) {
  const name = model === "v2" ? "RefModelV2" : "RefModelV1";
  const file = model === "v2" ? "ref_model_v2.js" : "ref_model_v1.js";
  if (!factories[model]) factories[model] = new Promise((res, rej) => {
    if (globalThis[name]) return res(globalThis[name]);
    const s = document.createElement("script");
    s.src = `/wasm/${file}`;
    s.onload = () => res(globalThis[name]);
    s.onerror = () => rej(new Error(`failed to load ${file}`));
    document.head.appendChild(s);
  });
  return factories[model];
}
async function browserFetchBin(name, onFrac) {
  const r = await fetch(`/sldl_data/${name}`);
  if (!r.ok) throw new Error(`${name}: HTTP ${r.status}`);
  const size = +r.headers.get("Content-Length") || 0;
  const reader = r.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    if (onFrac && size) onFrac(got / size);
  }
  const out = new Uint8Array(got);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}
export function sldlLocalSupported() {
  return typeof crossOriginIsolated !== "undefined" && crossOriginIsolated &&
    typeof SharedArrayBuffer !== "undefined" && "WebAssembly" in globalThis;
}
export async function sldlLocalRun(params, sourcesPayload, onTick) {
  if (!runner) runner = createRunner({
    fetchBin: browserFetchBin,
    fetchJson: async name => {
      const r = await fetch(`/sldl_data/${name}`);
      if (!r.ok) throw new Error(`${name}: HTTP ${r.status}`);
      return r.json();
    },
    factory: browserFactory,
    threads: Math.min(navigator.hardwareConcurrency || 8, 16),
  });
  const row = (sourcesPayload.sources || [])
    .find(s => s.key === params.source);
  const meta = params.model === "v2" && params.source === "zchf"
    ? sourcesPayload.client?.zchf_v2_meta
    : row?.meta;
  if (!meta) throw new Error("source metadata unavailable");
  if (params.model === "v2" && params.source === "zchf" &&
      !sourcesPayload.client?.zchf_v2)
    throw new Error("v2 zchf data not exported on the server");
  return runner.run(params, meta, onTick);
}
