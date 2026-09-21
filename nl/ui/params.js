// Plain market parameters (LLV2) and the liquidation venue. parts: market | venue
// (the modelled borrower is fixed, see pipeline.resolveBorrower: it has no editor)
import { api } from "../core/api.js";
import { fmtNum, fmtUsd } from "../core/charts.js";
import { h, field, selectField, button, card, toast } from "./kit.js";

// Controller.max_borrowable in floats (mid-band phase), as the Bad-Debt-Sim tab labels markets
export function maxLtv(A, N, loanDiscPct) {
  if (!(A >= 2) || !(N >= 1)) return NaN;
  const r = (A - 1) / A, G = A * (1 - Math.pow(r, N)) / (Math.sqrt(A / (A - 1)) * N);
  return Math.max(0, (1 - loanDiscPct / 100) * G * (1 - 1 / A / 2) * (1 - 1e-4)) * 100;
}

export function mount(host, ctx, opts = {}) {
  const { store } = ctx, part = opts.part || "all";
  const root = h("div", { class: "nl-params nl-stack" });
  host.append(root);
  let readout, venueBox, fields = [];

  const P = (key, o) => {
    const f = field({ type: "number", value: store.spec.params[key], ...o, onChange: v => store.update("params", s => { s.params[key] = v; }) });
    fields.push(() => f.set(store.spec.params[key]));
    return f;
  };

  function paintReadout() {
    if (!readout) return;
    const p = store.spec.params, N = 4;   // the modelled borrower always uses 4 bands
    const ltv = maxLtv(p.A, N, p.loan_discount_pct);
    readout.replaceChildren(h("span", {}, "max LTV ", h("b", { class: "nl-mono" }, Number.isFinite(ltv) ? ltv.toFixed(2) + " %" : "–")));
  }

  function marketCard() {
    readout = h("div", { class: "nl-figures" });
    return card("Parameters",
      h("div", { class: "nl-grid" },
        P("A", { label: "A (amplification)", hint: "LLAMMA A: band width = 1/A. Higher A = narrower bands, higher max LTV, faster soft-liquidation.", min: 2, max: 10000 }),
        P("fee_pct", { label: "AMM fee", unit: "%", hint: "LLAMMA swap fee charged to soft-liquidation arbitrage.", min: 0, max: 50 }),
        P("admin_fee_pct", { label: "Admin fee share", unit: "%", hint: "Share of interest that goes to the DAO (LLV2 admin_percentage).", min: 0, max: 100 }),
        P("loan_discount_pct", { label: "Loan discount", unit: "%", hint: "Haircut applied when a loan is opened: sets the max LTV.", min: 0, max: 99 }),
        P("liquidation_discount_pct", { label: "Liquidation discount", unit: "%", hint: "Haircut in the health formula: the margin a hard-liquidator earns. Must be below the loan discount.", min: 0, max: 99 }),
        P("borrow_cap", { label: "Debt ceiling", unit: store.spec.borrowed.symbol || "", hint: "borrow_cap: the most debt the market permits (set by the admin with configure_lend after creation). The bad-debt sim models the whole ceiling as one position.", min: 0 })
        ),
      readout);
  }

  function paintVenue() {
    if (!venueBox) return;
    const v = store.spec.venue, crypto = v.pool_type === "cryptoswap";
    const V = (key, o) => field({ type: "number", value: v[key], ...o, onChange: x => store.update("venue", s => { s.venue[key] = x; s.venue.state = null; }) });
    const tpl = h("select", { class: "nl-input nl-select" }, h("option", { value: "" }, "copy a live market's venue pool…"));
    api.markets().then(mk => {
      for (const g of ["LLV2", "LLV1"]) (mk.groups[g] || []).forEach((m, i) => { if (m.venue) tpl.append(h("option", { value: `${g}#${i}` },
        `${m.venue.name} · ${m.venue.pool_type} · ${fmtUsd(m.venue.pair_tvl_usd)} (${m.collateral.symbol} ${g})`)); });
    }).catch(() => {});
    tpl.addEventListener("change", async () => {
      if (!tpl.value) return;
      const mk = await api.markets(), [g, i] = tpl.value.split("#"), m = mk.groups[g][+i];
      store.update("venue", s => { Object.assign(s.venue, { pool_type: m.venue.pool_type, tvl_usd: Math.round(m.venue.pair_tvl_usd), n_coins: m.venue.n_coins || 2,
        state: m.venue.state || null, note: `${m.venue.name} (${m.venue.pool})`, ...(m.venue.pool_type === "cryptoswap" ? { A_raw: m.venue.A } : { ss_A: m.venue.A }) }); });
      toast(`venue copied from ${m.venue.name}${m.venue.state ? ", including its live pool state" : ""}`, "good");
    });
    venueBox.replaceChildren(...[h("div", { class: "nl-grid" },
      selectField({ label: "Pool type", value: v.pool_type, options: [{ value: "stableswap-ng", label: "stableswap-NG" }, { value: "stableswap", label: "stableswap" }, { value: "cryptoswap", label: "cryptoswap" }],
        onChange: x => store.update("venue", s => { s.venue.pool_type = x; s.venue.state = null; }) }),
      V("tvl_usd", { label: "Pair TVL", unit: "USD", min: 1000, hint: "Depth liquidators sell the collateral into. Thin venues are where bad debt comes from." }),
      crypto ? V("A_raw", { label: "A (cryptoswap, raw)", min: 1 }) : V("ss_A", { label: "A (stableswap)", min: 1 }),
      V("n_coins", { label: "Coins", min: 2, max: 3 }),
      h("label", { class: "nl-field nl-wide" }, h("span", { class: "nl-label" }, "Template"), tpl)),
      v.state ? h("div", { class: "nl-note" }, `Real pool state of ${v.note}; editing a field drops it for a balanced pool.`) : null,
      v.state ? button("Drop the real state", { small: true, kind: "ghost", onClick: () => store.update("venue", s => { s.venue.state = null; s.venue.note = ""; }) }) : null].filter(Boolean));
  }
  function venueCard() { venueBox = h("div", { class: "nl-stack" }); return card("Liquidation venue", venueBox); }

  function build() {
    fields = []; readout = venueBox = null;
    const kids = [];
    if (part === "all" || part === "market") kids.push(marketCard());
    if (part === "all" || part === "venue") kids.push(venueCard());
    root.replaceChildren(...kids);
    paintReadout(); paintVenue();
  }
  build();
  const off = store.on(tag => {
    if (tag === "spec") return build();
    if (tag === "params") { fields.forEach(f => f()); paintReadout(); }
    if (tag === "venue") paintVenue();
    if (tag === "tokens") build();
  });
  return { destroy() { off(); root.remove(); } };
}
