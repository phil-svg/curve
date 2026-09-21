// Which market. Markets are files in nl/markets/, added by pull request (core/registry.js),
// so the page has nothing to set one up with: it picks one, and everything below plays with it.
//   opts.part "pick"   the picker, and "Reset": back to the market as its file has it (what is
//                      changed on the page, parameters or MA times, lives in this browser only)
//   opts.part "howto"  the pull-request instructions, to copy
import { markets, marketOf, HOW_TO, REPO, DIR } from "../core/registry.js";
import { h, button, toast } from "./kit.js";

export function mount(host, ctx, opts = {}) {
  const { store } = ctx;
  const root = h("div", { class: "nl-market" });
  host.append(root);
  if (opts.part === "howto") {
    root.append(h("div", { class: "nl-row nl-tight nl-market-tools" },
      button("Copy", { small: true, onClick: async () => { try { await navigator.clipboard.writeText(HOW_TO); toast("copied", "good"); } catch (_) { toast("clipboard blocked: select the text instead", "error"); } } }),
      h("a", { class: "nl-btn nl-small nl-ghost", href: `${REPO}/tree/main/${DIR}`, target: "_blank", rel: "noopener" }, "Open the folder on GitHub")),
      h("pre", { class: "nl-tokens-howto nl-mono" }, HOW_TO));
    return { destroy() { root.remove(); } };
  }
  let list = [], alive = true;
  const sel = h("select", { class: "nl-input nl-select", "aria-label": "market" });
  const use = file => { const m = list.find(x => x.file === file); if (m) store.replace(JSON.parse(JSON.stringify(m.spec))); return m; };
  sel.addEventListener("change", () => use(sel.value));
  const reset = button("Reset", { small: true, kind: "ghost", title: "back to the market as its file has it", onClick: () => { if (use(sel.value)) toast("back to the market's own values", "good"); } });
  root.append(h("div", { class: "nl-row" }, h("div", { class: "nl-grow" }, sel), reset));
  async function paint() {
    try { list = await markets(); } catch (e) { list = []; console.warn("[nl] markets", e); }
    const cur = await marketOf(store.spec).catch(() => null);
    if (!alive) return;
    if (!cur && list.length) return void use(list[0].file);          // a configuration from before markets came by pull request
    sel.replaceChildren(...list.map(m => h("option", { value: m.file, selected: cur && cur.file === m.file ? true : null }, (m.spec.meta && m.spec.meta.name) || m.file)));
  }
  paint();
  const off = store.on(tag => { if (tag === "spec") paint(); });
  return { destroy() { alive = false; off(); root.remove(); } };
}
