// Full client-pipeline run under Node, diffed against a Python sweep run
// with identical params.
//   node e2e_node.mjs <model:v1|v2> <source> <grid> <ref_table.json> [threads]
import { readFileSync } from "fs";
import { createRequire } from "module";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { createRunner } from "../sldl_client.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "../..");
const [model = "v2", source = "zchf", grid = "3", refPath, threads = "8"] =
  process.argv.slice(2);
const require = createRequire(import.meta.url);

const KL = {}; // source key -> kl json path (mirrors the server route)
for (const m of (await import("fs")).readdirSync(join(ROOT, "data")))
  if (m.startsWith("_ref_table_klines_") && m.endsWith(".meta.json")) {
    const meta = JSON.parse(readFileSync(join(ROOT, "data", m), "utf8"));
    KL[`kl-${meta.symbol}`] = join(ROOT, "data", m.replace(".meta.json", ".json"));
  }
const FILES = name => {
  if (name === "usd_1m.bin") return join(ROOT, "data/sldl_usd_1m.bin");
  if (name === "usd_hourly.json")
    return join(ROOT, "data/_ref_v2/crvusd_usd_hourly.json");
  if (name === "zchf_v2_market.bin")
    return join(ROOT, "data/sldl_zchf_market.bin");
  if (name === "zchf.bin") return join(ROOT, "zchf/his_klines.json.bin");
  const key = name.replace(/\.bin$/, "");
  if (KL[key]) return KL[key] + ".bin";
  throw new Error(`unknown data file ${name}`);
};

const runner = createRunner({
  fetchBin: async name => new Uint8Array(readFileSync(FILES(name))),
  fetchJson: async name => JSON.parse(readFileSync(FILES(name), "utf8")),
  factory: async m => require(join(HERE,
    m === "v2" ? "../ref_model_v2.js" : "../ref_model_v1.js")),
  threads: +threads,
});

const meta = source === "zchf" && model === "v2"
  ? JSON.parse(readFileSync(join(ROOT, "data/sldl_zchf_market.meta.json"), "utf8"))
  : source === "zchf"
    ? JSON.parse(readFileSync(join(ROOT, "zchf/his_klines.meta.json"), "utf8"))
    : JSON.parse(readFileSync(KL[source].replace(".json", ".meta.json"), "utf8"));

const params = { model, source, a_min: 110, a_max: 208, fee_min: 0.015,
  fee_max: 0.5, grid: +grid, bands: 4, tail_pct: 0.05, loan_days: 80 / 1440,
  oracle_hl: model === "v2" ? 3603 : 1200 };

const d = await runner.run(params, meta, () => {});
if (!refPath) { console.log(JSON.stringify(d, null, 1)); process.exit(0); }

const ref = JSON.parse(readFileSync(refPath, "utf8"));
const probs = [];
const key = c => `${c.A}|${c.fee_pct}`;
const refCells = Object.fromEntries(ref.cells.map(c => [key(c), c]));
if (JSON.stringify(d.grid) !== JSON.stringify(ref.grid))
  probs.push(`grid: ${JSON.stringify(d.grid)} vs ${JSON.stringify(ref.grid)}`);
for (const c of d.cells) {
  const r = refCells[key(c)];
  if (!r) { probs.push(`extra cell ${key(c)}`); continue; }
  for (const f of ["loss_pct", "max_pct", "n_sims", "transfer"])
    if (c[f] !== r[f]) probs.push(`${key(c)}.${f}: ${c[f]} vs ${r[f]}`);
}
if (d.cells.length !== ref.cells.length) probs.push("cell count");
if (ref.fee_curve && d.fee_curve) {
  if (d.fee_curve.A !== ref.fee_curve.A) probs.push("fee_curve.A");
  if (d.fee_curve.kind !== ref.fee_curve.kind) probs.push(
    `fee_curve.kind: "${d.fee_curve.kind}" vs "${ref.fee_curve.kind}"`);
  ref.fee_curve.avg_loss_pct.forEach((v, i) => {
    if (d.fee_curve.avg_loss_pct[i] !== v)
      probs.push(`fee_curve[${i}]: ${d.fee_curve.avg_loss_pct[i]} vs ${v}`);
  });
} else if (!!ref.fee_curve !== !!d.fee_curve) probs.push("fee_curve presence");
for (const k of ["n_all", "n_top", "range_size", "loan_days", "tail_pct",
                 "texp_s", "oracle_mode", "method", "add_reverse"])
  if (JSON.stringify(d.config[k]) !== JSON.stringify(ref.config[k]))
    probs.push(`config.${k}: ${d.config[k]} vs ${ref.config[k]}`);
console.log(probs.length ? probs.slice(0, 20).join("\n") : "E2E IDENTICAL");
process.exit(probs.length ? 1 : 0);
