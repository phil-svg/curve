// Per-cell parity of the wasm ref_model (v1) vs native ground truth
// (wasm/test/native_ref_v1.json, generated with pinned --threads 8).
//   node parity_v1.mjs [threads]
import { readFileSync } from "fs";
import { createRequire } from "module";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const HERE = dirname(fileURLToPath(import.meta.url));
const [threads = "8"] = process.argv.slice(2);
const require = createRequire(import.meta.url);
const factory = require(join(HERE, "../ref_model_v1.js"));

const REF = JSON.parse(readFileSync(join(HERE, "native_ref_v1.json"), "utf8"));
const kl = readFileSync(join(HERE, "../../zchf/his_klines.json.bin"));
const orc = readFileSync(
  join(HERE, "../../zchf/his_klines_oracle_1200.json.bin"));

const lines = [];
let exitResolve;
const exited = new Promise(r => (exitResolve = r));
const mod = await factory({
  print: s => lines.push(s),
  printErr: () => {},
  onExit: code => exitResolve(code),
});
// v1 freshness check: .bin must be newer than the .json twin
mod.FS.writeFile("/klines.json", new Uint8Array(0));
mod.FS.writeFile("/oracle.json", new Uint8Array(0));
const old = Date.now() - 60000;
mod.FS.utime("/klines.json", old, old);
mod.FS.utime("/oracle.json", old, old);
mod.FS.writeFile("/klines.json.bin", kl);
mod.FS.writeFile("/oracle.json.bin", orc);

// same argv as the native reference, with paths swapped to the FS copies
const argv = REF.argv.map(s => s
  .replace("zchf/his_klines_oracle_1200.json", "/oracle.json")
  .replace("zchf/his_klines.json", "/klines.json"));
const ti = argv.indexOf("--threads");
argv[ti + 1] = String(threads);

const t0 = process.hrtime.bigint();
mod.callMain(argv);
await exited;
const secs = Number(process.hrtime.bigint() - t0) / 1e9;

const cells = lines.filter(l => l.startsWith("{")).map(JSON.parse);
let maxd = 0;
for (const c of cells) {
  const ref = REF.cells[`${c.A}|${c.fee_pct}`];
  maxd = Math.max(maxd, Math.abs(c.loss_pct - ref[0]),
                  Math.abs(c.max_pct - ref[1]));
}
console.log(JSON.stringify({ threads: +threads, secs: +secs.toFixed(2),
  cells: cells.length, maxDiff: maxd }));
process.exit(maxd === 0 && cells.length === 225 ? 0 : 1);
