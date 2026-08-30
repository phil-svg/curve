// Per-cell parity of the production wasm ref_model_v2 vs the native ground
// truth recorded in dev/wasm-poc/native_ref.json (225 cells).
//   node parity_v2.mjs [threads]
import { readFileSync } from "fs";
import { createRequire } from "module";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const HERE = dirname(fileURLToPath(import.meta.url));
const [threads = "8"] = process.argv.slice(2);
const require = createRequire(import.meta.url);
const factory = require(join(HERE, "../ref_model_v2.js"));

const REF = JSON.parse(
  readFileSync(join(HERE, "../../dev/wasm-poc/native_ref.json"), "utf8"));
const mkt = readFileSync(
  join(HERE, "../../data/_ref_v2/market_5ccd79269e9e.json.bin"));
const orc = readFileSync(
  join(HERE, "../../data/_ref_v2/oracle_5ccd79269e9e.json.bin"));

const lines = [];
let exitResolve;
const exited = new Promise(r => (exitResolve = r));
const mod = await factory({
  print: s => lines.push(s),
  printErr: () => {},
  onExit: code => exitResolve(code),
});
mod.FS.writeFile("/market.json", new Uint8Array(0));
mod.FS.writeFile("/oracle.json", new Uint8Array(0));
const old = Date.now() - 60000;
mod.FS.utime("/market.json", old, old);
mod.FS.utime("/oracle.json", old, old);
mod.FS.writeFile("/market.json.bin", mkt);
mod.FS.writeFile("/oracle.json.bin", orc);

const t0 = process.hrtime.bigint();
mod.callMain(["--market", "/market.json", "--oracle", "/oracle.json",
  "--a-list", REF.a_list, "--fee-list", REF.fee_list,
  "--length", "80", "--bands", "4", "--ext-fee", "0.0005",
  "--dyn-mult", "0.25", "--warmup", "601", "--tail-frac", "0.0005",
  "--auto", "--oracle-hl", "3603", "--threads", String(threads)]);
await exited;
const secs = Number(process.hrtime.bigint() - t0) / 1e9;

const cells = lines.filter(l => l.startsWith("{")).map(JSON.parse);
let maxd = 0;
for (const c of cells)
  maxd = Math.max(maxd, Math.abs(c.loss_pct - REF.cells[`${c.A}|${c.fee}`]));
console.log(JSON.stringify({ threads: +threads, secs: +secs.toFixed(2),
  cells: cells.length, maxDiff: maxd }));
process.exit(maxd === 0 && cells.length === 225 ? 0 : 1);
