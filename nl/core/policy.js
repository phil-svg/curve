// Monetary policies of curve-stablecoin, as the contracts compute them, in
// floats. Rates are % APR here; on-chain they are per second (APR / 31 536 000).
//
// Five of the six share ONE curve, a hyperbola through three points:
//     rate(u) = r0 * (r_minf + A / (u_inf - u)) + shift
//     u_inf  = (beta-1)*u0 / ((beta-1)*u0 - (1-u0)*(1-alpha))
//     A      = (1-alpha) * u_inf * (u_inf - u0) / u0
//     r_minf = alpha - A / u_inf
// so that rate(0) = alpha*r0, rate(u0) = r0, rate(1) = beta*r0. They differ only
// in where the base rate r0 comes from. Semilog is the older, separate shape.
const YEAR = 365 * 86400;

export const POLICIES = [
  { id: "HyperbolicMP", label: "HyperbolicMP (v2) · fixed target rate", file: "mpolicies/v2/HyperbolicMP.vy", curve: "hyperbolic", shift: true,
    base: { key: "target_rate_apr_pct", label: "Target rate", hint: "The fixed base rate at the target utilisation. Set at deployment, adjustable by the factory admin. Contract bounds: about 1 % to 150 % APR.", min: 1, max: 150 },
    about: "For like-kind lend markets that want a static base rate. The rate at target utilisation is a number fixed in the policy; the factory admin can change it later with set_parameters.",
    defaults: { target_utilization_pct: 80, target_rate_apr_pct: 5.28, low_ratio: 0.3, high_ratio: 10, rate_shift_apr_pct: 0 } },
  { id: "HyperbolicDynamicMP", label: "HyperbolicDynamicMP (v2) · rate calculator", file: "mpolicies/v2/HyperbolicDynamicMP.vy", curve: "hyperbolic", shift: true, calculator: true,
    base: { key: "assumed_rate_apr_pct", label: "Calculator rate (assumed)", hint: "On-chain the base rate is read live from an external rate calculator (the yield of the collateral, e.g. sfrxUSD) and clamped to about 1 % .. 150 % APR. Type the rate you want to draw the curve for.", min: 1, max: 150 },
    about: "For yield-bearing collateral in like-kind markets: the base rate follows an external rate calculator contract, so borrowing costs track what the collateral earns. Used by the live sDOLA, sfrxUSD and syrupUSDC LLV2 markets.",
    defaults: { target_utilization_pct: 90, assumed_rate_apr_pct: 4.7, low_ratio: 0.5, high_ratio: 5, rate_shift_apr_pct: 0, rate_calculator: "" } },
  { id: "EMAMonetaryPolicy", label: "EMAMonetaryPolicy · EMA of a rate calculator", file: "mpolicies/EMAMonetaryPolicy.vy", curve: "hyperbolic", shift: true, calculator: true,
    base: { key: "assumed_rate_apr_pct", label: "EMA of calculator rate (assumed)", hint: "On-chain: an EMA (TEXP = 40 000 s) of the external rate calculator's rate, clamped to about 1 % .. 150 % APR.", min: 1, max: 150 },
    about: "The predecessor of HyperbolicDynamicMP: same curve, but the calculator's rate is smoothed with a 40 000 s EMA before it is used as the base rate.",
    defaults: { target_utilization_pct: 90, assumed_rate_apr_pct: 4.7, low_ratio: 0.5, high_ratio: 5, rate_shift_apr_pct: 0, rate_calculator: "" } },
  { id: "KinkedMonetaryPolicy", label: "KinkedMonetaryPolicy · fixed base rate, no shift", file: "mpolicies/KinkedMonetaryPolicy.vy", curve: "hyperbolic", shift: false,
    base: { key: "base_rate_apr_pct", label: "Base rate", hint: "The rate at target utilisation, given to the constructor as an APR.", min: 0.01, max: 1000 },
    about: "For non-yielding collateral (e.g. WBTC/crvUSD) where the rate depends on utilisation alone. Same curve, a fixed base rate, no flat shift.",
    defaults: { target_utilization_pct: 85, base_rate_apr_pct: 8, low_ratio: 0.5, high_ratio: 3 } },
  { id: "SecondaryMonetaryPolicy", label: "SecondaryMonetaryPolicy · follows the crvUSD mint rate", file: "mpolicies/SecondaryMonetaryPolicy.vy", curve: "hyperbolic", shift: true,
    base: { key: "assumed_rate_apr_pct", label: "crvUSD mint-market rate (assumed)", hint: "On-chain the base rate is AMM.rate() of a reference crvUSD mint market. Type the rate you want to draw the curve for.", min: 0, max: 1000 },
    about: "Ties a lend market to crvUSD's own borrow rate: the base rate is read from a reference mint market's AMM, then bent by utilisation.",
    defaults: { target_utilization_pct: 85, assumed_rate_apr_pct: 6, low_ratio: 0.35, high_ratio: 1.5, rate_shift_apr_pct: 0 } },
  { id: "SemilogMonetaryPolicy", label: "SemilogMonetaryPolicy · min / max (LLV1 lending)", file: "mpolicies/SemilogMonetaryPolicy.vy", curve: "semilog",
    about: "The LLV1 one-way-lending policy: log(rate) is linear in utilisation, rate = min * (max / min) ^ u. Contract bounds: min at least 0.1 % APR, max at most 1000 % APR.",
    defaults: { min_rate_apr_pct: 0.5, max_rate_apr_pct: 40 } },
];
export const policyOf = id => POLICIES.find(p => p.id === id) || POLICIES[0];

export function defaultPolicy() {
  const out = { version: "HyperbolicMP" };
  for (const p of POLICIES) out[p.id] = { ...p.defaults };
  return out;
}

export function hyperbola(u0, alpha, beta) {
  const num = (beta - 1) * u0, sub = (1 - u0) * (1 - alpha);
  const u_inf = num / (num - sub), A = (1 - alpha) * u_inf * (u_inf - u0) / u0;
  return { u_inf, A, r_minf: alpha - A / u_inf };
}

// what the constructor's asserts would reject, in words
export function policyProblems(id, v) {
  const P = policyOf(id), out = [];
  if (P.curve === "semilog") {
    if (!(v.min_rate_apr_pct >= 0.1)) out.push("min rate must be at least 0.1 % APR");
    if (!(v.max_rate_apr_pct <= 1000)) out.push("max rate must be at most 1000 % APR");
    if (!(v.min_rate_apr_pct <= v.max_rate_apr_pct)) out.push("min rate must not exceed max rate");
    return out;
  }
  const u0 = v.target_utilization_pct / 100, a = v.low_ratio, b = v.high_ratio, r0 = v[P.base.key];
  if (!(u0 >= 0.01 && u0 <= 0.99)) out.push("target utilisation must be 1 % .. 99 %");
  if (!(a >= 0.01)) out.push("low ratio must be at least 0.01");
  if (!(a < 1)) out.push("low ratio must be below 1 (the rate at 0 % is below the base rate)");
  if (!(b > 1)) out.push("high ratio must be above 1 (the rate at 100 % is above the base rate)");
  if (!(b <= 100)) out.push("high ratio must be at most 100");
  if (!(r0 >= P.base.min && r0 <= P.base.max)) out.push(`${P.base.label.toLowerCase()} must be ${P.base.min} % .. ${P.base.max} % APR`);
  if (!out.length) {
    const h = hyperbola(u0, a, b);
    if (!((b - 1) * u0 >= (1 - u0) * (1 - a))) out.push("invalid curve: (high-1)*u0 must be at least (1-u0)*(1-low)");
    else if (!(h.u_inf > 1)) out.push("invalid curve: the asymptote u_inf must lie above 100 % utilisation");
  }
  return out;
}

// borrow rate in % APR at utilisation u (0..1)
export function policyRate(id, v, u) {
  const P = policyOf(id);
  u = Math.min(1, Math.max(0, u));
  if (P.curve === "semilog") return v.min_rate_apr_pct * Math.pow(v.max_rate_apr_pct / v.min_rate_apr_pct, u);
  const h = hyperbola(v.target_utilization_pct / 100, v.low_ratio, v.high_ratio);
  const r = v[P.base.key] * (h.r_minf + h.A / (h.u_inf - u)) + (P.shift ? +v.rate_shift_apr_pct || 0 : 0);
  return Math.max(0, r);
}
export const aprToPerSecond1e18 = apr => Math.round((apr / 100 / YEAR) * 1e18);
