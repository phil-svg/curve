// Server calls for the new-llamalend tab. Every history loader reports
// progress and walks FIXED, ALIGNED windows, so the server's on-disk cache
// hits on every window that lies fully in the past.
async function j(url, opts) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch (_) { /* non-JSON error page */ }
  if (!r.ok || (body && body.error)) throw new Error((body && body.error) || `HTTP ${r.status}`);
  return body;
}
const post = (url, body) => j(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function pool(items, width, fn) {
  let next = 0;
  const out = new Array(items.length);
  await Promise.all(Array.from({ length: Math.min(width, items.length) }, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i], i); }
  }));
  return out;
}

const dsCache = new Map();     // dataset key -> parsed typed arrays (35 MB bins: once)
const abiMemo = new Map(), aggMemo = new Map(), routeMemo = new Map(), exactMemo = new Map();
export const EXACT_WINDOW_S = 432000;          // the server's block-exact windows: 5 days, aligned
let sourcesPayload = null;

// Is this a server that never reads the chain (the default: it only serves prepared packs)? Asked once per page.
let modeP = null;
export const serverMode = () => modeP || (modeP = j("/nlapi/ping").then(r => ({ public: !!(r && r.public) })).catch(() => ({ public: false })));

export const api = {
  ping: () => j("/nlapi/ping"),
  token: (chain, address) => j(`/nlapi/token?chain=${encodeURIComponent(chain)}&address=${encodeURIComponent(address)}`),
  pool: (chain, address) => j(`/nlapi/pool?chain=${encodeURIComponent(chain)}&address=${encodeURIComponent(address)}`),
  call: (chain, calls, block) => post("/nlapi/call", { chain, calls, block }),
  emaTree: (chain, address) => j(`/nlapi/ema_tree?chain=${encodeURIComponent(chain)}&address=${encodeURIComponent(address)}`),
  // verified ABI of a contract, reduced to the read functions a source can call (memoised per page)
  abi(chain, address) {
    const key = chain + ":" + String(address).toLowerCase();
    if (!abiMemo.has(key)) abiMemo.set(key, j(`/nlapi/abi?chain=${encodeURIComponent(chain)}&address=${encodeURIComponent(address)}`).catch(e => { abiMemo.delete(key); throw e; }));
    return abiMemo.get(key);
  },
  aggregator(chain) {
    if (!aggMemo.has(chain)) aggMemo.set(chain, j(`/nlapi/aggregator?chain=${encodeURIComponent(chain)}`).catch(e => { aggMemo.delete(chain); throw e; }));
    return aggMemo.get(chain);
  },
  // A registered market's prepared chart data (nl_api.build_pack): one file, no RPC behind it. null while the
  // server is still preparing it (or the market is not registered).
  async pack(file) {
    const r = await fetch(`/nlapi/pack?market=${encodeURIComponent(file)}`);
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(`pack: HTTP ${r.status}`);
    return r.json();
  },
  // for every coin of a pool: the last-trade readings that price one token of it in `quote`
  coinRoutes(chain, pool, quote) {
    const key = [chain, pool, quote].join(":").toLowerCase();
    if (!routeMemo.has(key)) routeMemo.set(key, j(`/nlapi/coin_routes?chain=${encodeURIComponent(chain)}&pool=${encodeURIComponent(pool)}&quote=${encodeURIComponent(quote)}`).catch(e => { routeMemo.delete(key); throw e; }));
    return routeMemo.get(key);
  },
  markets: () => j("/markets"),
  oracles: () => j("/oracles"),
  debtcap: q => j("/debtcap?" + new URLSearchParams(q)),
  run: params => post("/run", params),
  progress: () => j("/progress"),
  async sldlSources() { return sourcesPayload || (sourcesPayload = await j("/sldl_sources")); },

  // Block-exact history of one on-chain read (/nlapi/sample_exact): its value in every block where a watched
  // contract logged something, refined in between until straight lines match the chain. how = {raw, watch}:
  // raw = a stored value that holds until its next point; watch = the contracts whose logs mark the blocks to read.
  // Windows that lie in the past are fetched once per page (and kept on the server's disk for ever).
  async sampleExact(chain, src, range, how = {}, onProgress) {
    const W = EXACT_WINDOW_S, now = Date.now() / 1000, wins = [];
    for (let w0 = Math.floor(range.from / W) * W; w0 <= range.to; w0 += W) wins.push(w0);
    let done = 0;
    const parts = await pool(wins, 3, async w0 => {
      const body = { chain, to: src.address, sig: src.sig, args: src.args || [], slot: src.slot || 0, rtype: src.rtype || "uint",
        decimals: src.raw ? 0 : (src.decimals ?? 18), from: w0, raw: !!how.raw, watch: how.watch || [] };
      const key = JSON.stringify(body), past = w0 + W < now - 900;
      let p = past ? exactMemo.get(key) : null;
      if (!p) { p = post("/nlapi/sample_exact", body); if (past) exactMemo.set(key, p.catch(e => { exactMemo.delete(key); throw e; })); }
      const r = await p;
      onProgress && onProgress(++done, wins.length);
      return r;
    });
    const t = [], v = [];
    for (const p of parts) for (let i = 0; i < p.t.length; i++) {
      if (p.v[i] === null || p.v[i] === undefined || (t.length && p.t[i] <= t[t.length - 1])) continue;   // neighbouring windows share their edge block
      t.push(p.t[i]); v.push(p.v[i]);
    }
    if (!t.length) throw new Error("no data: the call reverts over the whole range (not deployed yet, wrong signature, or no archive RPC for this chain)");
    return { t: Float64Array.from(t), v: Float64Array.from(v), exact: true, held: !!how.raw };
  },

  // Curve prices API: base priced in quote, close of each candle
  async curveSeries(chain, src, range, onProgress) {
    const unit = src.units || "hour";
    const [units, number, span] = unit === "day" ? ["day", 1, 170 * 86400]
      : unit === "15min" ? ["minute", 15, 170 * 900] : ["hour", 1, 168 * 3600];
    const wins = [];
    for (let w0 = Math.floor(range.from / span) * span; w0 <= range.to; w0 += span)
      wins.push([w0, w0 + span - 1]);
    let done = 0;
    const parts = await pool(wins, 3, async ([a, b]) => {
      const q = new URLSearchParams({ chain, pool: src.pool, base: src.base, quote: src.quote, from: a, to: b, units, number });
      const r = await j("/nlapi/curve_ohlc?" + q);
      onProgress && onProgress(++done, wins.length);
      return r.rows;
    });
    const seen = new Map();
    for (const rows of parts) for (const r of rows) seen.set(r[0], r[4]);
    const ts = [...seen.keys()].sort((a, b) => a - b);
    if (!ts.length) throw new Error("the prices API has no candles for this pool / pair");
    return { t: Float64Array.from(ts), v: Float64Array.from(ts.map(k => seen.get(k))) };
  },

  // packed local datasets from the S.L./D.L. data menu
  async datasetSeries(src, onProgress) {
    const payload = await api.sldlSources();
    const row = (payload.sources || []).find(s => s.key === src.key);
    if (!row) throw new Error(`dataset ${src.key} is not on this server`);
    const paired = row.meta && row.meta.format === "llamma-v2-paired-f64-v1";
    const col = src.column || "close";
    const ck = src.key + ":" + (paired && col === "oracle" ? "oracle" : "market");
    if (!dsCache.has(ck)) {
      dsCache.set(ck, (async () => {
        const grab = async name => {
          const r = await fetch(`/sldl_data/${name}`);
          if (!r.ok) throw new Error(`${name}: HTTP ${r.status}`);
          const size = +r.headers.get("Content-Length") || 0, reader = r.body.getReader();
          const chunks = [];
          let got = 0;
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value); got += value.length;
            if (onProgress && size) onProgress(got, size);
          }
          const u8 = new Uint8Array(got);
          let off = 0;
          for (const c of chunks) { u8.set(c, off); off += c.length; }
          return u8;
        };
        if (paired) {
          const m = await grab(row.meta.market_file);
          const rows = new Float64Array(m.buffer, 8, (m.byteLength - 8) >> 3);
          const o = col === "oracle" ? await grab(row.meta.oracle_file) : null;
          return { rows, tScale: 1, oracle: o ? new Float64Array(o.buffer, 8, (o.byteLength - 8) >> 3) : null };
        }
        const b = await grab(src.key === "zchf" ? "zchf.bin" : `${src.key}.bin`);
        return { rows: new Float64Array(b.buffer, 0, b.byteLength >> 3), tScale: 1e-3, oracle: null };
      })().catch(e => { dsCache.delete(ck); throw e; }));
    }
    const d = await dsCache.get(ck);
    const n = d.rows.length / 5, t = new Float64Array(n), v = new Float64Array(n);
    const ci = { open: 1, high: 2, low: 3, close: 4 }[col] || 4;
    for (let i = 0; i < n; i++) {
      t[i] = Math.floor(d.rows[i * 5] * d.tScale);
      v[i] = d.oracle ? d.oracle[i] : d.rows[i * 5 + ci];
    }
    return { t, v };
  },
};
