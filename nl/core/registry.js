// The markets this tab knows. A market is a spec file under nl/markets/, listed in
// nl/markets/index.json, and it gets there by pull request: the page has no way to
// type a token in (the server reads a market's whole price history from the chain,
// so which contracts it reads is decided in review, not by whoever opens the page).
export const REPO = "https://github.com/phil-svg/curve";
export const DIR = "nl/markets";
export const TEMPLATE = "reusd-sfrxusd-lp.json";

let listP = null;
async function j(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}
// -> [{file, spec}], in the order of index.json; a file that does not load is left out
export function markets() {
  if (!listP) listP = (async () => {
    const idx = await j(`/${DIR}/index.json`);
    const files = (idx.markets || []).filter(f => /^[a-z0-9][a-z0-9-]*\.json$/.test(f));
    const out = await Promise.all(files.map(f => j(`/${DIR}/${f}`).then(spec => ({ file: f, spec })).catch(e => { console.warn("[nl] market", f, e); return null; })));
    return out.filter(Boolean);
  })().catch(e => { listP = null; throw e; });
  return listP;
}
const low = x => String(x || "").toLowerCase();
export const tokensKey = spec => [spec.chain, low((spec.collateral || {}).address), low((spec.borrowed || {}).address)].join("|");
// the registered market with these tokens on this chain, or null
export async function marketOf(spec) {
  const k = tokensKey(spec);
  return (await markets()).find(m => tokensKey(m.spec) === k) || null;
}

// What someone who wants another market copies and follows.
export const HOW_TO = `How to add a market to the new-llamalend tab

Markets are added by pull request. The page itself has no field for a token or a contract address: a market's whole price history is read from the chain ahead of time, so which contracts are read is decided in review.

1. Fork ${REPO} and create a branch.
2. Copy ${DIR}/${TEMPLATE} to ${DIR}/<collateral>-<borrowed>-<chain>.json (lower case, a-z, 0-9 and "-" only).
3. In your copy, set:
     meta.name            how the market is listed
     chain                ethereum | arbitrum | optimism | base | fraxtal | sonic
     collateral           address, symbol, name, decimals (an LP token: the pool / LP token address)
     borrowed             address, symbol, name, decimals of the token that is lent
     params               A, fee_pct, loan_discount_pct, liquidation_discount_pct, borrow_cap (what LendFactory.create takes)
     oracle.sources       one entry per on-chain read: name, address, sig, args, decimals
     oracle.script        the lines that turn the sources into "oracle", and "market" (the same formula on last traded prices, no EMA)
   Leave the rest as it is: missing fields take their defaults.
4. Add your file name to the "markets" list in ${DIR}/index.json.
5. Open a pull request titled "new-llamalend market: <collateral>/<borrowed> (<chain>)". For every address in the file, give its block explorer link in the description so it can be checked.
6. After the merge the market is in the picker at the top of the tab. Only the JSON file goes through GitHub: its charts appear once the maintainer has prepared its price history.
`;
