// Byte-parity of the JS oracle builders vs the Python-built artifacts on
// disk, plus pyRound fixtures. Run: node wasm/test/oracle_parity.mjs
import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { buildOracleV1, buildOracleV2, parseUsdBin, pyRound, usdLookup }
  from "../sldl_shared.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const R = p => readFileSync(join(HERE, "../../", p));
const f64 = (b, off = 0) => {
  const out = new Float64Array((b.byteLength - off) / 8);
  const dv = new DataView(b.buffer, b.byteOffset + off);
  for (let i = 0; i < out.length; i++) out[i] = dv.getFloat64(i * 8, true);
  return out;
};
const diff = (a, b) => {
  if (a.length !== b.length) return `len ${a.length} vs ${b.length}`;
  let n = 0, worst = 0, at = -1;
  for (let i = 0; i < a.length; i++)
    if (a[i] !== b[i] && !(a[i] !== a[i] && b[i] !== b[i])) {
      n++; const d = Math.abs(a[i] - b[i]);
      if (d > worst) { worst = d; at = i; }
    }
  return n ? `${n} diffs, worst ${worst} at ${at}` : "IDENTICAL";
};

// pyRound fixtures (Python-verified values, incl. .5 ties)
const fix = [[102.5, 0, 102], [103.5, 0, 104], [0.12345, 4, 0.1235],
  [0.123450001, 4, 0.1235], [2.675, 2, 2.67], [117.00000000000001, 0, 117],
  [-102.5, 0, -102], [0.000125, 4, 0.0001], [1e-7, 5, 0]];
for (const [x, nd, want] of fix)
  if (pyRound(x, nd) !== want)
    throw new Error(`pyRound(${x},${nd}) = ${pyRound(x, nd)} != ${want}`);
console.log("pyRound fixtures: OK");

const usdTab = parseUsdBin(R("data/sldl_usd_1m.bin"));
const hourly = JSON.parse(R("data/_ref_v2/crvusd_usd_hourly.json"));
const lookup = usdLookup(usdTab, hourly);

// v1 zchf oracle, texp 1200 (raw f64, no header)
{
  const kl = f64(R("zchf/his_klines.json.bin"));
  const got = buildOracleV1(kl, 1200, lookup);
  const want = f64(R("zchf/his_klines_oracle_1200.json.bin"));
  console.log("v1 zchf oracle texp=1200:", diff(got, want));
}
// v2 zchf oracle, texp 3603 (header in the artifact)
{
  const mkt = f64(R("data/sldl_zchf_market.bin"), 8);
  const got = buildOracleV2(mkt, 3603, i => usdTab.vals[
    (mkt[i * 5] - usdTab.t0) / 60]);
  const want = f64(R("data/_ref_v2/oracle_5ccd79269e9e.json.bin"), 8);
  console.log("v2 zchf oracle texp=3603:", diff(got, want));
}

// v2 kl-WBTC: market conversion + aggregate-1.0 oracle vs ensure_cache_klines
{
  const { klToV2Market } = await import("../sldl_shared.js");
  const kl = f64(R("data/_ref_table_klines_WBTC_14552h.json.bin"));
  const mkt = klToV2Market(kl);
  const wantM = Float64Array.from(
    JSON.parse(R("data/_ref_v2/market_b10a5990dadd.json"))
      .flatMap(r => r.slice(0, 5)));
  console.log("v2 kl-WBTC market:", diff(mkt, wantM));
  const got = buildOracleV2(mkt, 3603, null);
  const wantO = Float64Array.from(
    JSON.parse(R("data/_ref_v2/oracle_b10a5990dadd.json")));
  console.log("v2 kl-WBTC oracle texp=3603:", diff(got, wantO));
}
