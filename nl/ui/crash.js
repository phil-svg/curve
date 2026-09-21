// Which recorded crash plays: the worst, 2nd or 3rd worst fall inside windows of one
// length, and how much deeper than recorded it is played. It sits over the oracle chart:
// a pick sends the chart to that window, and the bad-debt run replays it.
import { pickedCrash, worstWindows, worseOf, CRASH_RANKS, CRASH_SPANS } from "../core/pipeline.js";
import { fmtDateTime } from "../core/charts.js";
import { h, seg, field } from "./kit.js";

const WORSE = [1, 1.25, 1.5, 2, 3];
const pct = x => (Number.isFinite(x) ? (x * 100).toFixed(2).replace("-", "−") + " %" : "–");
const num = x => String(+(+x).toFixed(3));

export function mount(host, ctx) {
  const { store } = ctx;
  const root = h("div", { class: "nl-crash" });
  host.append(root);
  const sc = () => store.spec.scenario;
  const upd = fn => store.update("scenario", s => fn(s.scenario));
  // a recorded crash is the only path the page plays: a configuration from before (drawn, cut, linear) is brought back to it
  if (sc().mode !== "history" || sc().by_hand) upd(s => { s.mode = "history"; s.by_hand = false; });
  // picking a crash or a window length sends the chart to the window that plays
  const pick = fn => {
    upd(s => { s.mode = "history"; s.by_hand = false; fn(s); });
    const p = pickedCrash(store.spec, store.rt);
    if (p) store.setRt("oracle.focus", { focus: { a: p.t0, b: p.t1 } });
  };

  const ranks = h("div", { class: "nl-crash-ranks" });
  const spanSeg = seg(CRASH_SPANS.map(x => ({ value: x.s, label: x.label })), sc().crash.span_s, v => pick(s => { s.crash = { ...s.crash, span_s: +v }; }));
  const worseSeg = seg(WORSE.map(x => ({ value: x, label: x === 1 ? "as recorded" : "× " + x })), worseOf(store.spec), v => upd(s => { s.worse = +v; }));
  const worseF = field({ label: "", unit: "×", type: "number", min: 0.1, max: 10, value: num(worseOf(store.spec)), mono: true, onChange: v => upd(s => { s.worse = v; }) });
  const group = (cap, title, ...kids) => h("div", { class: "nl-crash-group", title }, h("span", { class: "nl-label" }, cap), ...kids);
  root.append(h("div", { class: "nl-crash-row" },
    group("Which crash", "the deepest falls inside windows of the chosen length; windows never overlap, so the 2nd and 3rd worst are separate events", ranks),
    group("Window", "how long a stretch of history to replay", spanSeg),
    group("Shit hits the fan", "how many times deeper than recorded the fall is played: 2 turns −8 % into −16 %. Shape, timing and the oracle's lag stay as recorded; capped at −99 %.",
      h("div", { class: "nl-row nl-tight" }, worseSeg, worseF))));

  function paint() {
    const spec = store.spec, rt = store.rt, span = +sc().crash.span_s || 259200, w = worseOf(spec);
    const all = worstWindows(rt, span, CRASH_RANKS.length), cur = pickedCrash(spec, rt);
    ranks.replaceChildren(...CRASH_RANKS.map((name, i) => {
      const win = all[i];
      return h("button", { type: "button", class: "nl-crash-rank" + (cur && cur.rank === i ? " on" : ""), disabled: !win || null,
        title: win ? `${fmtDateTime(win.t0)} → ${fmtDateTime(win.t1)} UTC` : "no further separate window of this length in the loaded history",
        onClick: () => pick(x => { x.crash = { ...x.crash, rank: i }; }) }, h("b", {}, name), h("span", { class: "nl-mono" }, win ? `${pct(win.drop)} · ${fmtDateTime(win.peak_t).slice(0, 10)}` : "none"));
    }));
    spanSeg.set(span);
    worseSeg.set(WORSE.includes(w) ? w : "");
    worseF.set(num(w));
  }

  paint();
  let t = 0;
  const off = store.on(tag => { if (tag === "spec" || tag === "scenario" || tag === "scenario.data" || tag === "oracle.data") { clearTimeout(t); t = setTimeout(paint, 0); } });
  return { destroy() { off(); clearTimeout(t); root.remove(); } };
}
