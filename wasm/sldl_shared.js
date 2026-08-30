// Shared pure helpers for the browser-side S.L./D.L. runner. This file is
// the ONLY client-side home of grids / rounding / clamps / oracle recipes —
// it mirrors sweeps/sweep_ref_table.py + sweeps/sweep_v2_table.py (and the
// author's build_oracle). Any change to those scripts must be reflected
// here and re-checked with wasm/test/*.mjs.

// ---- CPython-exact rounding -----------------------------------------------
// round(x, nd) with banker's rounding on the EXACT value of the double,
// like CPython (which rounds the exact decimal expansion). Implemented via
// BigInt: x = m * 2^e exactly; round(x * 10^nd) half-to-even; back through
// parseFloat (correctly rounded per the JS spec).
export function pyRound(x, nd = 0) {
  if (!Number.isFinite(x)) return x;
  if (x === 0) return 0;
  const buf = new DataView(new ArrayBuffer(8));
  buf.setFloat64(0, x, true);
  const bits = buf.getBigUint64(0, true);
  const neg = bits >> 63n;
  const rawExp = Number((bits >> 52n) & 0x7ffn);
  const frac = bits & 0xfffffffffffffn;
  let m, e; // |x| = m * 2^e
  if (rawExp === 0) { m = frac; e = -1074; }
  else { m = frac | 0x10000000000000n; e = rawExp - 1075; }
  // want q = round(m * 2^e * 10^nd), ties to even
  let num = m * 10n ** BigInt(Math.max(nd, 0));
  let den = 10n ** BigInt(Math.max(-nd, 0));
  if (e >= 0) num <<= BigInt(e);
  else den <<= BigInt(-e);
  let q = num / den;
  const rem2 = (num - q * den) * 2n;
  if (rem2 > den || (rem2 === den && (q & 1n) === 1n)) q += 1n;
  let out;
  if (nd <= 0) {
    out = Number(q * 10n ** BigInt(-nd)); // exact for our magnitudes
  } else {
    let s = q.toString();
    if (s.length <= nd) s = "0".repeat(nd - s.length + 1) + s;
    out = parseFloat(s.slice(0, -nd) + "." + s.slice(-nd));
  }
  return neg ? -out : out;
}

// ---- grids (sweep_sl_dl.spread + the sweep scripts' set/round/sort) -------
export function spread(lo, hi, n) {
  if (n <= 1) return [lo];
  return Array.from({ length: n }, (_, i) => lo + ((hi - lo) * i) / (n - 1));
}
export const aGrid = (lo, hi, n) =>
  [...new Set(spread(lo, hi, n).map(v => pyRound(v)))].sort((a, b) => a - b);
export const feeGrid = (lo, hi, n) =>
  [...new Set(spread(lo, hi, n).map(v => pyRound(v, 4)))].sort((a, b) => a - b);

// ---- discount coefficient + best A (sweep_ref_table._disc_coeff) ----------
export function discCoeff(A, bands) {
  let s = 0;
  for (let k = 0; k < bands; k++) s += Math.pow((A - 1) / A, k + 0.5);
  return s / bands;
}
export function bestA(cells, bands) {
  let best = null, bestV = Infinity;
  for (const c of cells) {
    const v = 1 - (1 - c.max_pct / 100) * discCoeff(c.A, bands);
    if (v < bestV) { bestV = v; best = c.A; } // strict < = Python min tie-break
  }
  return best;
}

// ---- parameter clamps (ui_server.py num(), v2/v1 table blocks) ------------
function num(v, dflt, lo, hi, asInt = false) {
  let x = parseFloat(v);
  if (!Number.isFinite(x)) x = dflt;
  x = Math.max(lo, Math.min(hi, x));
  return asInt ? pyRound(x) : x;
}
export function clampParams(p) {
  const aLo = num(p.a_min, 100, 2, 1000, true);
  const aHi = num(p.a_max, 180, 2, 1000, true);
  const fLo = num(p.fee_min, 0.05, 0.001, 5.0);
  const fHi = num(p.fee_max, 0.5, 0.001, 5.0);
  return {
    a_min: Math.min(aLo, aHi), a_max: Math.max(aLo, aHi),
    fee_min: Math.min(fLo, fHi), fee_max: Math.max(fLo, fHi),
    grid: num(p.grid, 10, 2, 16, true),
    bands: num(p.bands, 4, 1, 50, true),
    tail_pct: num(p.tail_pct, 0.05, 0.001, 50.0),
    loan_days: num(p.loan_days, 80 / 1440, 10 / 1440, 14.0),
    oracle_hl: num(p.oracle_hl, p.model === "v2" ? 3603 : 1200, 30, 86400),
    realities: num(p.realities, 1, 1, 100, true),
  };
}

// ---- crvUSD/USD lookup (sweep_ref_table._crvusd_usd_lookup) ---------------
// minuteTable: {t0, vals: Float64Array} from data/sldl_usd_1m.bin (NaN = no
// row); hourly: plain object {sec: price} from crvusd_usd_hourly.json.
export function usdLookup(minuteTable, hourly) {
  const hoursSorted = Object.keys(hourly || {}).map(Number).sort((a, b) => a - b);
  const { t0, vals } = minuteTable || { t0: 0, vals: new Float64Array(0) };
  return ts => {
    const m = Math.floor(ts / 60) * 60;
    const i = (m - t0) / 60;
    if (i >= 0 && i < vals.length) {
      const v = vals[i];
      if (v === v) return v;
    }
    const h = Math.floor(ts / 3600) * 3600;
    if (hourly && Object.prototype.hasOwnProperty.call(hourly, h))
      return hourly[h];
    // forward-fill from the latest earlier hour; 1.0 if nothing at all
    let lo = 0, hi = hoursSorted.length;
    while (lo < hi) { const mid = (lo + hi) >> 1;
      if (hoursSorted[mid] <= h) lo = mid + 1; else hi = mid; }
    return lo > 0 ? hourly[hoursSorted[lo - 1]] : 1.0;
  };
}

// ---- oracle builders ------------------------------------------------------
// All three follow the author's EMA: seeded at the first open, decay
// 2^(-dt/half_life), ema = ema*decay + mid*(1-decay) — op order preserved.

// v1 usd-basis (sweep_ref_table.build_usd_oracle): klines rows are 5 f64
// per row with t in MILLISECONDS; returns raw f64 array (v1 sidecar = no
// header).
export function buildOracleV1(kl, halfLife, lookup) {
  const n = kl.length / 5;
  const out = new Float64Array(n);
  let ema = kl[1];
  let prev = Math.floor(kl[0] / 1000);
  for (let i = 0; i < n; i++) {
    const ts = Math.floor(kl[i * 5] / 1000);
    const mid = (kl[i * 5 + 2] + kl[i * 5 + 3]) / 2;
    const decay = halfLife ? Math.pow(2, -(ts - prev) / halfLife) : 0.0;
    ema = ema * decay + mid * (1 - decay);
    out[i] = ema * lookup(ts);
    prev = ts;
  }
  return out;
}

// v2 (ensure_cache_klines / build_oracle): market rows are 5 f64 per row
// with t in SECONDS; usdAt(i, ts) returns the aggregate (1.0 for kl
// sources). Returns raw f64 array; caller adds the uint64-n header.
export function buildOracleV2(market, halfLife, usdAt) {
  const n = market.length / 5;
  const out = new Float64Array(n);
  let ema = market[1];
  let prev = market[0];
  for (let i = 0; i < n; i++) {
    const ts = market[i * 5];
    const mid = (market[i * 5 + 2] + market[i * 5 + 3]) / 2;
    const decay = halfLife ? Math.pow(2, -(ts - prev) / halfLife) : 0.0;
    ema = ema * decay + mid * (1 - decay);
    out[i] = ema * (usdAt ? usdAt(i, ts) : 1.0);
    prev = ts;
  }
  return out;
}

// v1 klines (t ms, no header) -> v2 market sidecar payload (t s) as one
// Float64Array; caller prepends the uint64-n header when writing.
export function klToV2Market(kl) {
  const out = new Float64Array(kl.length);
  for (let i = 0; i < kl.length; i += 5) {
    out[i] = Math.floor(kl[i] / 1000);
    out[i + 1] = kl[i + 1]; out[i + 2] = kl[i + 2];
    out[i + 3] = kl[i + 3]; out[i + 4] = kl[i + 4];
  }
  return out;
}

// uint64-n header + f64 payload (v2 oracle sidecar format)
export function withHeader(f64) {
  const buf = new ArrayBuffer(8 + f64.length * 8);
  new DataView(buf).setBigUint64(0, BigInt(f64.length), true);
  new Float64Array(buf, 8).set(f64);
  return new Uint8Array(buf);
}
// v2 market sidecar: header counts ROWS (n), payload is n*5 doubles
export function marketWithHeader(f64rows5) {
  const buf = new ArrayBuffer(8 + f64rows5.length * 8);
  new DataView(buf).setBigUint64(0, BigInt(f64rows5.length / 5), true);
  new Float64Array(buf, 8).set(f64rows5);
  return new Uint8Array(buf);
}
export function parseUsdBin(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const t0 = Number(dv.getBigUint64(0, true));
  const n = Number(dv.getBigUint64(8, true));
  // copy to an aligned buffer (byteOffset+16 may be unaligned)
  const vals = new Float64Array(n);
  for (let i = 0; i < n; i++) vals[i] = dv.getFloat64(16 + i * 8, true);
  return { t0, vals };
}
