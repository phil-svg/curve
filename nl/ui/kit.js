// Shared UI kit: tiny DOM helpers + the form primitives every component
// uses, so the five designs restyle ONE vocabulary of classes.
export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export const debounce = (fn, ms = 250) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// "0,05" and "1_000" and "1e6" are all numbers a researcher types
export function parseNum(s) {
  if (typeof s === "number") return s;
  const x = Number(String(s).trim().replace(/_/g, "").replace(/\s/g, "").replace(/,(?=\d{1,2}$)/, ".").replace(/,/g, ""));
  return Number.isFinite(x) ? x : NaN;
}

// field({label, hint, value, type: "number"|"text", unit, onChange, min, max, step, wide, placeholder, mono})
export function field(o) {
  const input = h("input", { class: "nl-input" + (o.mono ? " nl-mono" : ""), type: "text", inputmode: o.type === "number" ? "decimal" : null,
    value: o.value ?? "", placeholder: o.placeholder || "", spellcheck: "false", autocomplete: "off" });
  const commit = () => {
    if (o.type === "number") {
      let x = parseNum(input.value);
      if (!Number.isFinite(x)) { input.classList.add("nl-bad"); return; }
      if (o.min !== undefined) x = Math.max(o.min, x);
      if (o.max !== undefined) x = Math.min(o.max, x);
      input.classList.remove("nl-bad");
      o.onChange && o.onChange(x);
    } else o.onChange && o.onChange(input.value.trim());
  };
  input.addEventListener("change", commit);
  input.addEventListener("keydown", e => { if (e.key === "Enter") { commit(); input.blur(); } });
  const el = h("label", { class: "nl-field" + (o.wide ? " nl-wide" : ""), title: o.hint || null },
    h("span", { class: "nl-label" }, o.label, o.hint ? h("i", { class: "nl-q" }, "?") : null),
    h("span", { class: "nl-inwrap" }, input, o.unit ? h("em", { class: "nl-unit" }, o.unit) : null));
  el.input = input;
  el.set = v => { if (document.activeElement !== input) input.value = v ?? ""; };
  return el;
}

export function select(options, value, onChange, cls = "") {
  const el = h("select", { class: "nl-input nl-select " + cls },
    options.map(o => h("option", { value: o.value, selected: String(o.value) === String(value) }, o.label)));
  el.addEventListener("change", () => onChange(el.value));
  return el;
}
export function selectField(o) {
  const s = select(o.options, o.value, o.onChange);
  const el = h("label", { class: "nl-field" + (o.wide ? " nl-wide" : ""), title: o.hint || null },
    h("span", { class: "nl-label" }, o.label, o.hint ? h("i", { class: "nl-q" }, "?") : null), h("span", { class: "nl-inwrap" }, s));
  el.input = s;
  return el;
}

export function seg(options, value, onChange) {
  const el = h("div", { class: "nl-seg", role: "tablist" });
  const paint = v => el.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === String(v)));
  for (const o of options) el.append(h("button", { type: "button", dataset: { v: o.value }, title: o.hint || null,
    onClick: () => { paint(o.value); onChange(o.value); } }, o.label));
  paint(value);
  el.set = paint;
  return el;
}

export function button(label, o = {}) {
  return h("button", { type: "button", class: "nl-btn" + (o.kind ? " nl-" + o.kind : "") + (o.small ? " nl-small" : ""),
    title: o.title || null, disabled: o.disabled || null, onClick: o.onClick }, label);
}

export function card(title, ...kids) {
  return h("section", { class: "nl-card" }, title ? h("header", { class: "nl-card-h" }, title) : null, h("div", { class: "nl-card-b" }, kids));
}

export function progress() {
  const bar = h("i"), lab = h("span"), el = h("div", { class: "nl-progress", hidden: true }, h("div", { class: "nl-progress-track" }, bar), lab);
  el.set = (frac, label) => {
    el.hidden = frac === null;
    if (frac !== null) { bar.style.width = Math.round(Math.max(0, Math.min(1, frac)) * 100) + "%"; lab.textContent = label || ""; }
  };
  return el;
}

let toastHost = null;
export function toast(msg, kind = "info") {
  if (!toastHost || !toastHost.isConnected) { toastHost = h("div", { class: "nl-toasts" }); (document.querySelector(".nl-root") || document.body).append(toastHost); }
  const t = h("div", { class: "nl-toast nl-" + kind }, msg);
  toastHost.append(t);
  setTimeout(() => t.classList.add("out"), 3600);
  setTimeout(() => t.remove(), 4200);
}

// Token logo with a fallback chain: Curve's asset repo, TrustWallet (needs
// the EIP-55 address the token lookup returns), then a lettered disc.
export function tokenLogo(tok, chain, size = 28) {
  const wrap = h("span", { class: "nl-logo", style: { width: size + "px", height: size + "px", fontSize: Math.round(size * 0.42) + "px" } });
  const letter = () => { wrap.textContent = ((tok && tok.symbol) || "?").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "?"; wrap.classList.add("nl-logo-txt"); };
  const addr = tok && tok.address ? String(tok.address).toLowerCase() : "";
  if (!/^0x[0-9a-f]{40}$/.test(addr)) { letter(); return wrap; }
  const dir = !chain || chain === "ethereum" ? "assets" : "assets-" + chain;
  const urls = [`https://cdn.jsdelivr.net/gh/curvefi/curve-assets/images/${dir}/${addr}.png`];
  if (tok.checksum) urls.push(`https://cdn.jsdelivr.net/gh/trustwallet/assets@master/blockchains/${chain === "ethereum" || !chain ? "ethereum" : chain}/assets/${tok.checksum}/logo.png`);
  const img = h("img", { alt: "", crossorigin: "anonymous", width: size, height: size });
  let k = 0;
  img.addEventListener("error", () => { k++; if (k < urls.length) img.src = urls[k]; else { img.remove(); letter(); } });
  img.src = urls[0];
  wrap.append(img);
  return wrap;
}

export const shortAddr = a => a ? a.slice(0, 6) + "…" + a.slice(-4) : "";

