// Series math for the new-llamalend tab. A "series" is {t: Float64Array
// (unix seconds, ascending), v: Float64Array}. Everything the oracle script
// computes runs on ONE shared time grid, so functions here take plain
// Float64Arrays plus the grid's t where time matters.

export function makeGrid(from, to, step) {
  const n = Math.max(0, Math.floor((to - from) / step) + 1);
  const t = new Float64Array(n);
  for (let i = 0; i < n; i++) t[i] = from + i * step;
  return t;
}

// Forward-fill a source onto the grid: the value in force at each grid time
// is the last sample at or before it (what an on-chain reader would see).
// NaN before the source's first sample.
export function toGrid(src, gridT) {
  const out = new Float64Array(gridT.length).fill(NaN);
  const st = src.t, sv = src.v, n = st.length;
  let j = -1;
  for (let i = 0; i < gridT.length; i++) {
    while (j + 1 < n && st[j + 1] <= gridT[i]) j++;
    if (j >= 0) out[i] = sv[j];
  }
  return out;
}

// A block-exact smooth reading (an EMA, a vault rate) is given by points that straight lines join within tolerance:
// on the grid it is read off those lines. NaN before its first point, held after its last.
export function toGridLinear(src, gridT) {
  const out = new Float64Array(gridT.length).fill(NaN);
  const st = src.t, sv = src.v, n = st.length;
  let j = -1;
  for (let i = 0; i < gridT.length; i++) {
    const x = gridT[i];
    while (j + 1 < n && st[j + 1] <= x) j++;
    if (j < 0) continue;
    out[i] = j + 1 < n && st[j + 1] > st[j] ? sv[j] + (sv[j + 1] - sv[j]) * (x - st[j]) / (st[j + 1] - st[j]) : sv[j];
  }
  return out;
}
// The grid of a block-exact evaluation: the regular grid plus every time one of the series has a point, and one
// second before each point of a HELD series (so its step is a step, not a ramp to the previous grid time).
export function unionGrid(base, series) {
  let n = base.length;
  const a = base[0], b = base[base.length - 1];
  for (const s of series) n += s.t.length * (s.held ? 2 : 1);
  const all = new Float64Array(n);
  all.set(base);
  let k = base.length;
  for (const s of series) for (let i = 0; i < s.t.length; i++) {
    const x = s.t[i];
    if (x < a || x > b) continue;
    all[k++] = x;
    if (s.held && x - 1 > a) all[k++] = x - 1;
  }
  const sorted = all.subarray(0, k).sort();
  let m = 0;
  for (let i = 0; i < k; i++) if (!m || sorted[i] !== sorted[m - 1]) sorted[m++] = sorted[i];
  return sorted.slice(0, m);
}

// Curve's EMA (curve_std/ema.vy and the pools' price_oracle): the value
// QUEUED at the previous update is what gets blended in, weighted by
// exp(-dt / ema_time). ema_time is the pools' ma_exp_time (= half-life / ln 2).
export function ema(x, t, emaTime) {
  const n = x.length, out = new Float64Array(n);
  if (!n) return out;
  const T = Math.max(1e-9, +emaTime || 0);
  let prev = NaN, queued = NaN;
  for (let i = 0; i < n; i++) {
    const xi = x[i];
    if (Number.isNaN(prev)) {                 // not seeded yet
      if (!Number.isNaN(xi)) { prev = queued = xi; }
      out[i] = prev;
      continue;
    }
    const mul = Math.exp(-(t[i] - t[i - 1]) / T);
    prev = prev * mul + queued * (1 - mul);
    if (!Number.isNaN(xi)) queued = xi;
    out[i] = prev;
  }
  return out;
}

// StableSwapNGLPOracle's dampened virtual price: upside through the EMA,
// downside passed through at once (the EMA snaps down to spot), and the
// reported value is min(spot, ema).
export function asymEma(x, t, emaTime) {
  const n = x.length, out = new Float64Array(n);
  if (!n) return out;
  const T = Math.max(1e-9, +emaTime || 0);
  let prev = NaN, queued = NaN;
  for (let i = 0; i < n; i++) {
    const spot = x[i];
    if (Number.isNaN(prev)) {
      if (!Number.isNaN(spot)) prev = queued = spot;
      out[i] = prev;
      continue;
    }
    const mul = Math.exp(-(t[i] - t[i - 1]) / T);
    let cur = prev * mul + queued * (1 - mul);
    if (!Number.isNaN(spot)) {
      if (spot < cur) { cur = spot; queued = spot; }   // snap down
      else queued = spot;                                // smooth up
    }
    prev = cur;
    out[i] = Number.isNaN(spot) ? cur : Math.min(spot, cur);
  }
  return out;
}

// curve_std/stableswap/lp_oracle_2.vy in floats. StableSwap n=2, D=1:
// find the point on the invariant whose marginal price -dx/dy equals p and
// return the portfolio value x + p*y in x-units. A is the pool's A()
// (A_eff = A / n^(n-1) = A / 2, exactly the contract's _scaled_A_raw).
function xFromY(Ae, y) {
  const b1 = 4 * Ae * (y - 1) + 1;
  return (-b1 + Math.sqrt(b1 * b1 + 4 * Ae / y)) / (8 * Ae);
}
function pFromY(Ae, y) {
  const x = xFromY(Ae, y), a = 4 * Ae * x;
  return (a + 1 / (4 * y * y)) / (a + 1 / (4 * x * y));
}
// -> [value, d value / d p]. The slope is y itself: along the invariant dx = -p dy, so d(x + p y)/dp = y.
function pvAndSlope(A, p) {
  if (!(p > 0) || !(A > 0)) return [NaN, NaN];
  if (p < 1) { const [v, d] = pvAndSlope(A, 1 / p); return [p * v, v - d / p]; }      // symmetry branch
  const Ae = A / 2;
  let lo = 1e-9, hi = 0.5;                              // p(lo) > p >= p(hi)=1
  for (let k = 0; k < 80; k++) {
    const mid = 0.5 * (lo + hi);
    if (pFromY(Ae, mid) > p) lo = mid; else hi = mid;
    if (hi - lo < 1e-15) break;
  }
  const y = 0.5 * (lo + hi);
  return [xFromY(Ae, y) + p * y, y];
}
export function portfolioValue(A, p) { return pvAndSlope(A, p)[0]; }
// The same over a whole series. A long one (a block-exact grid has 200k points, and an MA slider asks many times a
// second) is solved exactly at 2049 prices between its extremes and read off cubic Hermite pieces through those values
// AND slopes: the error is of the order 1e-12, the 50-step bisection runs 2049 times instead of once per point.
export function portfolioValues(A, p) {
  const n = p.length, out = new Float64Array(n);
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) { const x = p[i]; if (x > 0 && x < Infinity) { if (x < lo) lo = x; if (x > hi) hi = x; } }
  if (n < 8192 || !(hi > lo)) { for (let i = 0; i < n; i++) out[i] = portfolioValue(A, p[i]); return out; }
  const N = 2048, h = (hi - lo) / N, V = new Float64Array(N + 1), D = new Float64Array(N + 1);
  for (let k = 0; k <= N; k++) { const r = pvAndSlope(A, lo + k * h); V[k] = r[0]; D[k] = r[1]; }
  for (let i = 0; i < n; i++) {
    const x = p[i];
    if (!(x > 0 && x < Infinity)) { out[i] = NaN; continue; }
    const u = (x - lo) / h, k = Math.min(N - 1, Math.floor(u)), s = u - k, s2 = s * s, s3 = s2 * s;
    out[i] = (2 * s3 - 3 * s2 + 1) * V[k] + (s3 - 2 * s2 + s) * h * D[k] + (3 * s2 - 2 * s3) * V[k + 1] + (s3 - s2) * h * D[k + 1];
  }
  return out;
}

// largest-triangle-free min/max decimation: what a pixel column can show
export function minMaxBuckets(t, v, i0, i1, buckets) {
  const n = Math.max(0, i1 - i0);
  const B = Math.max(1, Math.min(buckets | 0, n));
  const bt = new Float64Array(B), lo = new Float64Array(B), hi = new Float64Array(B),
        last = new Float64Array(B);
  for (let b = 0; b < B; b++) {
    const a = i0 + Math.floor((b * n) / B), z = i0 + Math.floor(((b + 1) * n) / B);
    let mn = Infinity, mx = -Infinity, l = NaN;
    for (let i = a; i < Math.max(z, a + 1) && i < i1; i++) {
      const x = v[i];
      if (Number.isNaN(x)) continue;
      if (x < mn) mn = x;
      if (x > mx) mx = x;
      l = x;
    }
    bt[b] = t[Math.min(i1 - 1, a)];
    lo[b] = mn === Infinity ? NaN : mn;
    hi[b] = mx === -Infinity ? NaN : mx;
    last[b] = l;
  }
  return { t: bt, lo, hi, last };
}

// first index with t[i] >= x
export function lowerBound(t, x) {
  let a = 0, b = t.length;
  while (a < b) { const m = (a + b) >> 1; if (t[m] < x) a = m + 1; else b = m; }
  return a;
}

// Ramer-Douglas-Peucker on [[x, y], ...] with a relative tolerance on y and
// x normalised to the span - turns a recorded window into a few drag points.
export function simplify(points, eps) {
  if (points.length <= 2) return points.slice();
  const x0 = points[0][0], xs = (points[points.length - 1][0] - x0) || 1;
  let yMin = Infinity, yMax = -Infinity;
  for (const p of points) { if (p[1] < yMin) yMin = p[1]; if (p[1] > yMax) yMax = p[1]; }
  const ys = (yMax - yMin) || 1;
  const keep = new Uint8Array(points.length);
  keep[0] = keep[points.length - 1] = 1;
  const stack = [[0, points.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const ax = (points[a][0] - x0) / xs, ay = (points[a][1] - yMin) / ys;
    const bx = (points[b][0] - x0) / xs, by = (points[b][1] - yMin) / ys;
    const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
    let worst = -1, wi = -1;
    for (let i = a + 1; i < b; i++) {
      const px = (points[i][0] - x0) / xs, py = (points[i][1] - yMin) / ys;
      const d = Math.abs(dy * (px - ax) - dx * (py - ay)) / len;
      if (d > worst) { worst = d; wi = i; }
    }
    if (worst > eps && wi > 0) { keep[wi] = 1; stack.push([a, wi], [wi, b]); }
  }
  return points.filter((_, i) => keep[i]);
}

// piecewise-linear y(x) through sorted [[x, y], ...], clamped at the ends
export function interp(points, x) {
  const n = points.length;
  if (!n) return NaN;
  if (x <= points[0][0]) return points[0][1];
  if (x >= points[n - 1][0]) return points[n - 1][1];
  let a = 0, b = n - 1;
  while (b - a > 1) { const m = (a + b) >> 1; if (points[m][0] <= x) a = m; else b = m; }
  const [xa, ya] = points[a], [xb, yb] = points[b];
  return xb === xa ? ya : ya + ((yb - ya) * (x - xa)) / (xb - xa);
}

export function median(arr) {
  const a = Array.from(arr).filter(x => Number.isFinite(x)).sort((p, q) => p - q);
  return a.length ? a[a.length >> 1] : NaN;
}

export function lastFinite(arr) {
  for (let i = arr.length - 1; i >= 0; i--) if (Number.isFinite(arr[i])) return arr[i];
  return NaN;
}
export function firstFiniteIndex(arr) {
  for (let i = 0; i < arr.length; i++) if (Number.isFinite(arr[i])) return i;
  return -1;
}

// ---- re-timing an EMA that is already baked into a sampled value ---------------
// A pool's price_oracle, an aggregator's price ... arrive EMA-smoothed with the
// contract's own time T0. Curve's recursion  y_i = y_(i-1)*m + x*(1-m),
// m = exp(-dt/T0)  can be run backwards on the sampled grid: the input the
// contract must have seen over each step is  x = (y_i - m*y_(i-1)) / (1-m).
// impliedInput() returns that; reEma() feeds it through a different time T1
// (0 = no smoothing at all). T1 === T0 reproduces y exactly. With no T0 (a raw
// reading) reEma() is a plain Curve EMA.
export function impliedInput(y, t, T0) {
  const n = y.length, out = new Float64Array(n);
  if (!(T0 > 0)) { out.set(y); return out; }
  let prev = NaN;
  for (let i = 0; i < n; i++) {
    const yi = y[i];
    if (Number.isNaN(yi)) { out[i] = NaN; continue; }
    if (Number.isNaN(prev)) { out[i] = yi; prev = yi; continue; }
    const m = Math.exp(-(t[i] - t[i - 1]) / T0);
    let x = (yi - m * prev) / (1 - m);
    // a step far shorter than T0 divides by ~0 and turns sampling jitter into
    // spikes: keep the implied input within a sane band of the reading itself
    const lo = yi * 0.5, hi = yi * 1.5;
    if (x < lo) x = lo; else if (x > hi) x = hi;
    out[i] = x; prev = yi;
  }
  return out;
}
// implied: impliedInput(y, t, T0) when the caller already holds it (an MA slider asks for a new T1 many times a second)
export function reEma(y, t, T0, T1, implied) {
  if (!(T0 > 0)) return T1 > 0 ? ema(y, t, T1) : y;
  const x = implied || impliedInput(y, t, T0), n = y.length, out = new Float64Array(n);
  if (!(T1 > 0)) return x;
  let z = NaN;
  for (let i = 0; i < n; i++) {
    if (Number.isNaN(x[i])) { out[i] = z; continue; }
    if (Number.isNaN(z)) { z = y[i]; out[i] = z; continue; }
    const m = Math.exp(-(t[i] - t[i - 1]) / T1);
    z = z * m + x[i] * (1 - m);
    out[i] = z;
  }
  return out;
}
