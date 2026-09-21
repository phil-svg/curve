// new-llamalend tab entry: one store, the page over the components.
import { Store } from "./core/state.js";
import { evaluateOracle, buildScenario, loadPack } from "./core/pipeline.js";
import { serverMode } from "./core/api.js";
import { h, toast } from "./ui/kit.js";

const CSS = ["base", "forms", "oracle", "ema", "flow", "crash", "baddebt", "matune", "runs", "page"];

let store = null;
export async function mount(host) {
  for (const name of CSS) {
    if (document.querySelector(`link[data-nl="${name}"]`)) continue;
    document.head.append(h("link", { rel: "stylesheet", href: `/nl/css/${name}.css?v=${Date.now()}`, dataset: { nl: name } }));
  }
  store = store || new Store();
  const ctx = { store };
  const root = h("div", { class: "nl-root" });
  host.replaceChildren(root);

  // keep the derived runtime data coherent whether or not the scenario editor is open (it
  // publishes too while mounted; the sims and the summary need the scenario either way)
  let scRaf = 0;
  store.on(tag => {
    if (tag === "spec") { try { evaluateOracle(store); } catch (e) { console.warn("[nl] evaluate", e); } }
    if (tag === "spec" || tag === "scenario" || tag === "oracle.data") {
      // a timer, not requestAnimationFrame: rAF never fires in a hidden tab, and
      // the sims must not depend on the tab being looked at while data loads
      clearTimeout(scRaf);
      scRaf = setTimeout(() => {
        let sc = null;
        try { sc = buildScenario(store.spec, store.rt); } catch (e) { console.warn("[nl] scenario", e); }
        const cur = store.rt.scenario;
        // cheap content check: an EMA knob changes the ORACLE of the very same clips,
        // and the bad-debt sim must replay the new one
        const sum = a => { let x = 0; if (a) for (let i = 0; i < a.length; i++) x += a[i] * (1 + (i % 7)); return x; };
        const same = cur && sc && cur.t.length === sc.t.length && cur.label === sc.label && cur.duration === sc.duration
          && sum(cur.market) === sum(sc.market) && sum(cur.oracle) === sum(sc.oracle);
        if (!same && (cur || sc)) store.setRt("scenario.data", { scenario: sc });
      }, 0);
    }
  });
  if (!store.rt.scenario) { const sc = buildScenario(store.spec, store.rt); if (sc) store.rt.scenario = sc; }

  const { mount: mountPage } = await import(`./page.js?v=${Date.now()}`);
  mountPage(root, ctx);
  window.__nlStore = store;                               // handy in the console
  // Open with numbers on screen, and without one RPC call: every registered market's history is prepared
  // ahead of time (one file, pipeline.loadPack). Sampling the chain is left to an explicit
  // "Load & evaluate" (a source the pack does not hold, or to be fresher than the pack).
  // a server that never reads the chain: the buttons that would ask it to are not shown (tag "mode")
  serverMode().then(m => { if (m.public) store.setRt("mode", { public: true }); });
  const fill = () => { if (!store.rt.oracle && !store.rt.busy.sources) loadPack(store).catch(e => toast("history did not load: " + (e.message || e), "error")); };
  fill();
  store.on(tag => { if (tag === "spec") setTimeout(fill, 0); });        // another market picked, or an import: its pack
  return { store };
}
