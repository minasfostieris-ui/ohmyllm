/* OhMyLLM console — dark OpenCode × Qoder hybrid front-end.
   Vanilla, no deps, fully offline. Talks to the local loopback bridge.
   Security UX is ported 1:1 from the previous console (approvals, IME guards,
   drafts, dirty editors, fail-closed expiry) and restyled. */
"use strict";

/* provider brand glyphs (simple-icons, CC0) — offline data, letter fallback */
const PROV_ICONS = {
  "openrouter": ["#94A3B8", "M16.778 1.844v1.919q-.569-.026-1.138-.032-.708-.008-1.415.037c-1.93.126-4.023.728-6.149 2.237-2.911 2.066-2.731 1.95-4.14 2.75-.396.223-1.342.574-2.185.798-.841.225-1.753.333-1.751.333v4.229s.768.108 1.61.333c.842.224 1.789.575 2.185.799 1.41.798 1.228.683 4.14 2.75 2.126 1.509 4.22 2.11 6.148 2.236.88.058 1.716.041 2.555.005v1.918l7.222-4.168-7.222-4.17v2.176c-.86.038-1.611.065-2.278.021-1.364-.09-2.417-.357-3.979-1.465-2.244-1.593-2.866-2.027-3.68-2.508.889-.518 1.449-.906 3.822-2.59 1.56-1.109 2.614-1.377 3.978-1.466.667-.044 1.418-.017 2.278.02v2.176L24 6.014Z"],
  "anthropic": ["#D97757", "M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z"],
  "openai": ["#74AA9C", "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"],
  "google": ["#8E75B2", "M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81"],
  "deepseek": ["#5786FE", "M23.748 4.651c-.254-.124-.364.113-.512.233-.051.04-.094.09-.137.137-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.155-.708-.311-.955-.65-.172-.24-.219-.509-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.094.172.187.129.323-.082.28-.18.553-.266.833-.055.179-.137.218-.328.14a5.5 5.5 0 0 1-1.737-1.179c-.857-.828-1.631-1.743-2.597-2.46a12 12 0 0 0-.689-.47c-.985-.957.13-1.743.387-1.836.27-.098.094-.433-.778-.428-.872.003-1.67.295-2.687.685a3 3 0 0 1-.465.136 9.6 9.6 0 0 0-2.883-.101c-1.885.21-3.39 1.1-4.497 2.622C.082 8.776-.231 10.854.152 13.02c.403 2.284 1.568 4.175 3.36 5.653 1.857 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.132-.284 4.994-1.86.47.234.962.328 1.78.398.629.058 1.235-.031 1.705-.129.735-.155.684-.836.418-.961-2.155-1.004-1.682-.595-2.112-.926 1.095-1.295 2.768-3.598 3.284-6.733.05-.346.115-.834.108-1.114-.004-.171.035-.238.23-.257a4.2 4.2 0 0 0 1.545-.475c1.397-.763 1.96-2.016 2.093-3.517.02-.23-.004-.467-.247-.588"],
  "mistral": ["#FA520F", "M17.143 3.429v3.428h-3.429v3.429h-3.428V6.857H6.857V3.43H3.43v13.714H0v3.428h10.286v-3.428H6.857v-3.429h3.429v3.429h3.429v-3.429h3.428v3.429h-3.428v3.428H24v-3.428h-3.43V3.429z"],
  "github": ["#e8e8e8", "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"],
  "huggingface": ["#FFD21E", "M12.025 1.13c-5.77 0-10.449 4.647-10.449 10.378 0 1.112.178 2.181.503 3.185.064-.222.203-.444.416-.577a.96.96 0 0 1 .524-.15c.293 0 .584.124.84.284.278.173.48.408.71.694.226.282.458.611.684.951v-.014c.017-.324.106-.622.264-.874s.403-.487.762-.543c.3-.047.596.06.787.203s.31.313.4.467c.15.257.212.468.233.542.01.026.653 1.552 1.657 2.54.616.605 1.01 1.223 1.082 1.912.055.537-.096 1.059-.38 1.572.637.121 1.294.187 1.967.187.657 0 1.298-.063 1.921-.178-.287-.517-.44-1.041-.384-1.581.07-.69.465-1.307 1.081-1.913 1.004-.987 1.647-2.513 1.657-2.539.021-.074.083-.285.233-.542.09-.154.208-.323.4-.467a1.08 1.08 0 0 1 .787-.203c.359.056.604.29.762.543s.247.55.265.874v.015c.225-.34.457-.67.683-.952.23-.286.432-.52.71-.694.257-.16.547-.284.84-.285a.97.97 0 0 1 .524.151c.228.143.373.388.43.625l.006.04a10.3 10.3 0 0 0 .534-3.273c0-5.731-4.678-10.378-10.449-10.378"],
  "minimax": ["#E73562", "M11.43 3.92a.86.86 0 1 0-1.718 0v14.236a1.999 1.999 0 0 1-3.997 0V9.022a.86.86 0 1 0-1.718 0v3.87a1.999 1.999 0 0 1-3.997 0V11.49a.57.57 0 0 1 1.139 0v1.404a.86.86 0 0 0 1.719 0V9.022a1.999 1.999 0 0 1 3.997 0v9.134a.86.86 0 0 0 1.719 0V3.92a1.998 1.998 0 1 1 3.996 0v11.788a.57.57 0 1 1-1.139 0zm10.572 3.105a2 2 0 0 0-1.999 1.997v7.63a.86.86 0 0 1-1.718 0V3.923a1.999 1.999 0 0 0-3.997 0v16.16a.86.86 0 0 1-1.719 0V18.08a.57.57 0 1 0-1.138 0v2a1.998 1.998 0 0 0 3.996 0V3.92a.86.86 0 0 1 1.719 0v12.73a1.999 1.999 0 0 0 3.996 0V9.023a.86.86 0 1 1 1.72 0v6.686a.57.57 0 0 0 1.138 0V9.022a2 2 0 0 0-1.998-1.997"],
  "alibaba": ["#FF6A00", "M3.996 4.517h5.291L8.01 6.324 4.153 7.506a1.668 1.668 0 0 0-1.165 1.601v5.786a1.668 1.668 0 0 0 1.165 1.6l3.857 1.183 1.277 1.807H3.996A3.996 3.996 0 0 1 0 15.487V8.513a3.996 3.996 0 0 1 3.996-3.996m16.008 0h-5.291l1.277 1.807 3.857 1.182c.715.227 1.17.889 1.165 1.601v5.786a1.668 1.668 0 0 1-1.165 1.6l-3.857 1.183-1.277 1.807h5.291A3.996 3.996 0 0 0 24 15.487V8.513a3.996 3.996 0 0 0-3.996-3.996m-4.007 8.345H8.002v-1.804h7.995Z"],
};
function _logoColor(hex) {
  const n = parseInt(hex.slice(1), 16);
  const lum = 0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255);
  return lum < 96 ? "#e8e8e8" : hex;
}

const bootData = window.__OMLLM__ || { port: 0 };
const fragment = new URLSearchParams(window.location.hash.slice(1));
const BOOT = { ...bootData, token: fragment.get("token") || "" };
// The fragment is never sent to the bridge; erase it before any navigation,
// logging, copying, or browser persistence can retain the capability token.
if (window.location.hash) history.replaceState(null, "", window.location.pathname + window.location.search);
const BASE = `http://127.0.0.1:${BOOT.port}`;
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  view: "chat", status: null, config: null, session: null,
  streaming: false, stopping: false, connected: true, everConnected: false,
  approvalQueue: [], answeringApproval: false, changingChat: false,
  drafts: new Map(), following: true, unread: 0,
  soulDirty: false, settingsDirty: false, _draft: null,
  chats: [], tabStates: {}, modelsCache: {}, openTabs: [],
  thinkNode: null, reasonBuf: "", fileNode: null, writeNode: null,
  toolEntries: new Map(),
};

/* -------------------------------------------- custom controls (task #29)
   om-listbox: themed combobox + overlay listbox replacing every native
   <select>. Mimics the select API the console already uses (.value,
   .options, setOptions, disabled, focus) and dispatches bubbling
   input/change events, so existing onchange handlers keep working.
   The popup is absolutely positioned: opening it never shifts layout. */
const _ddOpen = new Set();
class OmListbox extends HTMLElement {
  constructor() {
    super();
    this._options = [];
    this._value = "";
    this._active = -1;
    this._typeBuf = "";
    this._typeTimer = null;
    this._uid = "dd-" + Math.random().toString(36).slice(2, 9);
    this._btn = document.createElement("button");
    this._btn.type = "button";
    this._btn.className = "dd-btn";
    this._btn.setAttribute("role", "combobox");
    this._btn.setAttribute("aria-haspopup", "listbox");
    this._btn.setAttribute("aria-expanded", "false");
    this._btn.setAttribute("aria-controls", this._uid);
    this._label = document.createElement("span");
    this._label.className = "dd-label";
    const caret = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    caret.setAttribute("viewBox", "0 0 8 6");
    caret.setAttribute("aria-hidden", "true");
    caret.classList.add("dd-caret");
    const cpath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    cpath.setAttribute("d", "M1 1l3 3 3-3");
    cpath.setAttribute("fill", "none");
    cpath.setAttribute("stroke", "currentColor");
    cpath.setAttribute("stroke-width", "1.4");
    cpath.setAttribute("stroke-linecap", "round");
    caret.appendChild(cpath);
    this._btn.append(this._label, caret);
    this._pop = document.createElement("div");
    this._pop.className = "dd-pop";
    this._pop.id = this._uid;
    this._pop.setAttribute("role", "listbox");
    this._pop.hidden = true;
    // Do NOT append children to `this` in the constructor: Chromium's
    // document.createElement() rejects a custom element whose constructor
    // leaves it with children ("The result must not have children"), which
    // broke every dynamically built settings listbox. The internal nodes are
    // built detached here and attached once in connectedCallback.
    this._btn.addEventListener("click", () => { if (!this.disabled) { if (this._pop.hidden) this.open(); else this.close(); } });
    this._btn.addEventListener("keydown", (e) => this._onKey(e));
    this._pop.addEventListener("click", (e) => {
      const o = e.target.closest('[role="option"]');
      if (o) this._select(+o.dataset.i, true);
    });
    this._outside = (e) => { if (!this.contains(e.target)) this.close(); };
    document.addEventListener("click", this._outside);
  }
  connectedCallback() {
    if (this._btn.parentNode !== this) this.append(this._btn, this._pop);
    const lbl = this.getAttribute("aria-label");
    if (lbl) this._btn.setAttribute("aria-label", lbl);
    this._renderLabel();
  }
  disconnectedCallback() { document.removeEventListener("click", this._outside); this.close(); }
  /* ---- select-like API ---- */
  get type() { return "select-one"; }
  get options() { return this._options.slice(); }
  get value() { return this._value; }
  set value(v) {
    v = v == null ? "" : String(v);
    if (!this._options.some((o) => o.value === v)) v = this._options.length ? this._options[0].value : "";
    this._value = v;
    this._renderLabel();
    this._syncSelected();
  }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(d) {
    if (d) { this.setAttribute("disabled", ""); this.close(); } else this.removeAttribute("disabled");
    this._btn.disabled = !!d;
  }
  focus() { this._btn.focus(); }
  setOptions(list) {
    const keep = this._value;
    this._options = (list || []).map((o) => Array.isArray(o)
      ? { value: String(o[0]), label: String(o[1] ?? o[0]) }
      : { value: String(o.value), label: String(o.label ?? o.value) });
    this._value = this._options.some((o) => o.value === keep) ? keep : (this._options.length ? this._options[0].value : "");
    this._paint();
  }
  /* ---- popup ---- */
  open() {
    if (this.disabled || !this._pop.hidden) return;
    _ddOpen.forEach((x) => { if (x !== this) x.close(); });
    _ddOpen.add(this);
    this._pop.hidden = false;
    this._btn.setAttribute("aria-expanded", "true");
    this._active = Math.max(0, this._options.findIndex((o) => o.value === this._value));
    this._paintActive();
    // flip up / right near viewport edges so the popup never leaves screen
    const r = this._btn.getBoundingClientRect();
    const ph = Math.min(this._pop.scrollHeight, 320), pw = this._pop.offsetWidth;
    this.classList.toggle("up", r.bottom + 6 + ph > window.innerHeight - 8 && r.top > ph + 14);
    this.classList.toggle("right", r.left + pw > window.innerWidth - 8);
    const sel = this._pop.querySelector('[aria-selected="true"]');
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }
  close() {
    if (this._pop.hidden) return;
    this._pop.hidden = true;
    _ddOpen.delete(this);
    this._btn.setAttribute("aria-expanded", "false");
    this._btn.removeAttribute("aria-activedescendant");
    this.classList.remove("up", "right");
  }
  _paint() {
    this._pop.innerHTML = "";
    if (!this._options.length) {
      const d = document.createElement("div");
      d.className = "dd-empty";
      d.textContent = "no options";
      this._pop.appendChild(d);
    }
    this._options.forEach((o, i) => {
      const d = document.createElement("div");
      d.setAttribute("role", "option");
      d.id = `${this._uid}-o${i}`;
      d.dataset.i = i;
      d.setAttribute("aria-selected", String(o.value === this._value));
      d.title = o.label;
      const s = document.createElement("span");
      s.className = "dd-opt-label";
      s.textContent = o.label;
      d.appendChild(s);
      this._pop.appendChild(d);
    });
    this._renderLabel();
  }
  _renderLabel() {
    const o = this._options.find((x) => x.value === this._value);
    this._label.textContent = o ? o.label : "—";
    this._btn.title = o ? o.label : "";
  }
  _syncSelected() {
    this._pop.querySelectorAll('[role="option"]').forEach((d) => {
      const o = this._options[+d.dataset.i];
      d.setAttribute("aria-selected", String(!!o && o.value === this._value));
    });
  }
  _paintActive() {
    const opts = this._pop.querySelectorAll('[role="option"]');
    opts.forEach((d, i) => d.classList.toggle("active", i === this._active));
    const a = opts[this._active];
    if (a) { this._btn.setAttribute("aria-activedescendant", a.id); a.scrollIntoView({ block: "nearest" }); }
  }
  _move(d) {
    if (!this._options.length) return;
    this._active = (this._active + d + this._options.length) % this._options.length;
    this._paintActive();
  }
  _select(i, user) {
    const o = this._options[i];
    if (!o) return;
    const changed = o.value !== this._value;
    this._value = o.value;
    this._renderLabel();
    this._syncSelected();
    this.close();
    if (user) {
      this._btn.focus();
      if (changed) {
        this.dispatchEvent(new Event("input", { bubbles: true }));
        this.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  }
  _onKey(e) {
    if (e.isComposing || e.keyCode === 229) return;
    const isOpen = !this._pop.hidden;
    const k = e.key;
    const plain = k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (!isOpen) {
      if (["ArrowDown", "ArrowUp", "Enter", " ", "Home", "End"].includes(k)) {
        e.preventDefault();
        this.open();
        if (k === "ArrowUp" || k === "End") { this._active = this._options.length - 1; this._paintActive(); }
      } else if (plain) { e.preventDefault(); this.open(); this._typeAhead(k); }
      return;
    }
    if (k === "ArrowDown") { e.preventDefault(); this._move(1); }
    else if (k === "ArrowUp") { e.preventDefault(); this._move(-1); }
    else if (k === "Home") { e.preventDefault(); this._active = 0; this._paintActive(); }
    else if (k === "End") { e.preventDefault(); this._active = this._options.length - 1; this._paintActive(); }
    else if (k === "Enter" || k === " ") { e.preventDefault(); this._select(this._active, true); }
    else if (k === "Escape") { e.preventDefault(); e.stopPropagation(); this.close(); }
    else if (k === "Tab") { this.close(); }
    else if (plain) { e.preventDefault(); this._typeAhead(k); }
  }
  _typeAhead(ch) {
    clearTimeout(this._typeTimer);
    this._typeBuf += ch.toLowerCase();
    this._typeTimer = setTimeout(() => { this._typeBuf = ""; }, 700);
    const start = this._typeBuf.length === 1 ? this._active + 1 : 0;
    for (let n = 0; n < this._options.length; n++) {
      const i = (start + n) % this._options.length;
      if (this._options[i].label.toLowerCase().startsWith(this._typeBuf)) { this._active = i; this._paintActive(); return; }
    }
  }
}
if (!customElements.get("om-listbox")) customElements.define("om-listbox", OmListbox);
window.addEventListener("resize", () => _ddOpen.forEach((x) => x.close()));

/* themed number stepper: −/+ with press-and-hold repeat. The input keeps
   its id/data-key, so dirty tracking and programmatic fill behave as before;
   native spin buttons are suppressed in CSS. */
function makeStepper(inp) {
  const wrap = document.createElement("div");
  wrap.className = "stepper";
  const minus = document.createElement("button");
  const plus = document.createElement("button");
  minus.type = plus.type = "button";
  minus.tabIndex = plus.tabIndex = -1;
  minus.textContent = "−";
  plus.textContent = "+";
  minus.setAttribute("aria-label", "Decrease value");
  plus.setAttribute("aria-label", "Increase value");
  const stepOf = () => (inp.step === "any" ? 0.1 : parseFloat(inp.step) || 1);
  const clamp = (v) => {
    if (inp.min !== "" && v < parseFloat(inp.min)) v = parseFloat(inp.min);
    if (inp.max !== "" && v > parseFloat(inp.max)) v = parseFloat(inp.max);
    return Math.round(v * 1000) / 1000;
  };
  const syncBtns = () => {
    const v = parseFloat(inp.value);
    minus.disabled = inp.disabled || (inp.min !== "" && Number.isFinite(v) && v <= parseFloat(inp.min));
    plus.disabled = inp.disabled || (inp.max !== "" && Number.isFinite(v) && v >= parseFloat(inp.max));
  };
  const step = (dir) => {
    if (inp.disabled) return;
    const cur = parseFloat(inp.value);
    const next = clamp((Number.isFinite(cur) ? cur : 0) + dir * stepOf());
    if (String(next) !== inp.value) {
      inp.value = String(next);
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    }
    syncBtns();
  };
  const wire = (btn, dir) => {
    let holdT = null, repT = null;
    const stop = () => { clearTimeout(holdT); clearInterval(repT); holdT = repT = null; };
    btn.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || btn.disabled) return;
      e.preventDefault(); // keep focus in the input; no text selection
      step(dir);
      inp.focus();
      holdT = setTimeout(() => { repT = setInterval(() => step(dir), 80); }, 450);
    });
    ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, stop));
    // keyboard activation (Enter/Space) arrives as click with detail 0;
    // pointer clicks already stepped on pointerdown
    btn.addEventListener("click", (e) => { if (e.detail === 0) { step(dir); inp.focus(); } });
  };
  wire(minus, -1);
  wire(plus, 1);
  inp.addEventListener("input", syncBtns);
  wrap.append(minus, inp, plus);
  syncBtns();
  return wrap;
}

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", "X-OMLLM-Token": BOOT.token, ...(opts.headers || {}) };
  const res = await fetch(BASE + path, { ...opts, headers });
  if (res.status === 401) { const e = new Error("bridge token rejected — restart the GUI"); e.status = 401; throw e; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || `HTTP ${res.status}`); e.status = res.status; throw e; }
  return data;
}
const get = (p, opts) => api(p, opts);
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
const patch = (p, body) => api(p, { method: "PATCH", body: JSON.stringify(body || {}) });
const del = (p) => api(p, { method: "DELETE" });
function debounce(f, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; }

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

/* ------------------------------------------------------------- markdown */
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
const KEYWORDS = /\b(fn|def|class|const|let|var|use|mod|pub|struct|enum|impl|if|else|elif|for|while|return|import|from|async|await|try|except|catch|throw|new|match|switch|case|break|continue|self|this|null|None|nil|true|false|True|False)\b/g;
function hl(code, lang) {
  if (/^(json|toml|ini|yaml)$/.test(lang)) return esc(code);
  let out = esc(code);
  out = out.replace(/(&quot;.*?&quot;|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, '<span class="str">$1</span>');
  out = out.replace(/(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)/g, (m) => (m.includes('class="') ? m : `<span class="cmt">${m}</span>`));
  out = out.replace(KEYWORDS, '<span class="kw">$1</span>');
  out = out.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="num">$1</span>');
  return out;
}
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, (m, c) => `<code>${c}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|\W)\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
function md(src) {
  const blocks = [];
  const text = String(src).replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => `\u0000B${blocks.push({ lang: lang || "text", code }) - 1}\u0000`);
  const lines = text.split("\n");
  let html = "", list = null, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bm = line.match(/^\u0000B(\d+)\u0000$/);
    if (bm) {
      flushPara(); flushList();
      const b = blocks[+bm[1]];
      html += `<pre class="codeblock"><code>${hl(b.code, b.lang)}</code><button class="btn small copy-code" type="button" data-code="${esc(b.code)}">copy</button></pre>`;
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)/);
    if (h) { flushPara(); flushList(); html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; continue; }
    const ul = line.match(/^[-*]\s+(.*)/), ol = line.match(/^\d+[.)]\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const want = ul ? "ul" : "ol";
      if (list !== want) { flushList(); html += `<${want}>`; list = want; }
      html += `<li>${inline((ul || ol)[1])}</li>`; continue;
    }
    if (/^>\s?/.test(line)) { flushPara(); flushList(); html += `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`; continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    para.push(line);
  }
  flushPara(); flushList();
  return html;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".copy-code");
  if (!b) return;
  navigator.clipboard.writeText(b.dataset.code).then(() => toast("copied")).catch(() => toast("Copy unavailable — select the text and copy manually", "err"));
});

/* ------------------------------------------------- modals (stacked, LIFO) */
const modalStack = [];
function focusables(m) {
  return Array.from(m.querySelectorAll('button, input, textarea, select, [tabindex="0"]'))
    .filter((x) => !x.disabled && !x.hidden && !x.closest("[hidden]") && x.getClientRects().length);
}
function openModal(sel, focusSel) {
  const m = typeof sel === "string" ? $(sel) : sel;
  if (!m || modalStack.some((e) => e.el === m)) return;
  modalStack.push({ el: m, prev: document.activeElement });
  m.style.zIndex = 60 + modalStack.length * 2;
  const scrim = $("#scrim");
  scrim.hidden = false;
  scrim.style.zIndex = 59 + modalStack.length * 2;
  m.hidden = false;
  (focusSel ? m.querySelector(focusSel) : focusables(m)[0])?.focus();
}
function closeModal(sel) {
  const m = typeof sel === "string" ? $(sel) : sel;
  const i = modalStack.findIndex((e) => e.el === m);
  if (i < 0) { m.hidden = true; return; }
  const [entry] = modalStack.splice(i, 1);
  m.hidden = true;
  const top = modalStack[modalStack.length - 1];
  const scrim = $("#scrim");
  if (top) { top.el.style.zIndex = 60 + modalStack.length * 2; scrim.style.zIndex = 59 + modalStack.length * 2; scrim.hidden = false; }
  else scrim.hidden = true;
  // restore focus to whatever opened this modal, in LIFO order
  const prev = entry.prev;
  if (prev && document.contains(prev) && prev.getClientRects().length && !prev.closest("[hidden]")) prev.focus();
  else if (top) focusables(top.el)[0]?.focus();
}
function dismissModal(m) {
  if (m.id === "approval-modal") return answerApproval("deny"); // fail-closed
  if (m.id === "palette-modal") return closeModal(m);
  const cancel = m.querySelector(".modal-cancel");
  if (cancel) cancel.click(); else closeModal(m);
}
$("#scrim").onclick = () => { const top = modalStack[modalStack.length - 1]; if (top) dismissModal(top.el); };
document.addEventListener("keydown", (e) => {
  const top = modalStack[modalStack.length - 1];
  if (!top || e.isComposing || e.keyCode === 229) return;
  const m = top.el;
  if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); dismissModal(m); return; }
  if (e.key === "Tab") {
    const items = focusables(m);
    const i = items.indexOf(document.activeElement);
    if (items.length && (i < 0 || (e.shiftKey ? i === 0 : i === items.length - 1))) {
      e.preventDefault(); items[e.shiftKey ? items.length - 1 : 0].focus();
    }
  }
}, true);

function promptModal(title, hint, secret, def) {
  return new Promise((resolve) => {
    const inp = $("#pm-input");
    $("#pm-title").textContent = title;
    const hintEl = $("#pm-hint");
    hintEl.innerHTML = "";
    if (hint) {
      const span = document.createElement("span");
      span.textContent = hint + "  ";
      hintEl.appendChild(span);
      const link = hint.match(/https:\/\/[^\s)]+/);
      if (link) { const a = document.createElement("a"); a.href = link[0]; a.target = "_blank"; a.rel = "noopener"; a.textContent = "open"; hintEl.appendChild(a); }
    }
    inp.type = secret ? "password" : "text";
    inp.value = def || "";
    openModal("#prompt-modal", "#pm-input");
    const done = (val) => { closeModal("#prompt-modal"); inp.value = ""; $("#pm-ok").onclick = null; $("#pm-cancel").onclick = null; inp.onkeydown = null; resolve(val); };
    $("#pm-ok").onclick = () => done(inp.value.trim());
    $("#pm-cancel").onclick = () => done(null);
    inp.onkeydown = (e) => { if (e.isComposing || e.keyCode === 229) return; if (e.key === "Enter") { e.preventDefault(); done(inp.value.trim()); } };
  });
}
function confirmModal(title, body, okLabel = "Confirm") {
  return new Promise((resolve) => {
    $("#cf-title").textContent = title;
    $("#cf-body").textContent = body;
    $("#cf-ok").textContent = okLabel;
    openModal("#confirm-modal", "#cf-cancel");
    const done = (val) => { closeModal("#confirm-modal"); $("#cf-ok").onclick = null; $("#cf-cancel").onclick = null; resolve(val); };
    $("#cf-ok").onclick = () => done(true);
    $("#cf-cancel").onclick = () => done(false);
  });
}

/* ----------------------------------------------------------------- chat */
function clearStream() { $("#stream").innerHTML = ""; state.following = true; state.unread = 0; $("#jump-latest").hidden = true; state.toolEntries.clear(); }
const TEACH = [
  ["@file:path", "attach a file as context"],
  ["@diff · @staged", "review working-tree or staged changes"],
  ["/steer note", "redirect a turn while it is running"],
  ["/mode name", "change the permission mode (you only)"],
  ["/skill:name", "cast a saved skill into this chat"],
  ["/remember fact", "write a durable memory to soul.md"],
];
function teachHtml() { return `<ul class="teach">${TEACH.map(([c, d]) => `<li><code>${esc(c)}</code><span>${esc(d)}</span></li>`).join("")}</ul>`; }
function emptyState() {
  clearStream();
  $("#stream").innerHTML = `
    <div class="stream-empty">
      <span class="hero-glyph"><svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="12" fill="none" stroke="currentColor" stroke-width="2.2"/><circle cx="16" cy="16" r="4.5" fill="currentColor"/></svg></span>
      <h2>Local agent, <em>your rules.</em></h2>
      <p>Tools are permission-gated, memory lives in a plain soul.md you own, and every shell command asks first. Start with a real command:</p>
      ${teachHtml()}
    </div>`;
}
function addMsg(role, content, extraClass = "") {
  if (document.querySelector(".stream-empty")) clearStream();
  const el = document.createElement("div");
  el.className = `msg ${role} ${extraClass}`;
  el.innerHTML = `<div class="who">${role === "user" ? "you" : role === "assistant" ? "agent" : "note"}</div><div class="content md"></div>`;
  el.querySelector(".content").innerHTML = role === "user" ? userHtml(content) : md(content);
  $("#stream").appendChild(el);
  scrollBottom();
  return el.querySelector(".content");
}
/* Approval artifacts in persisted history are not agent prose: the runtime
   journals an empty assistant placeholder for pure tool-call turns and
   "[asked to run X]" / "[attempted X]" echoes for denied/blocked calls. The
   placeholder renders no row at all; the echo renders a single decided
   approval card, so every approval request owns exactly one visible row,
   live or replayed. */
function approvalEcho(text) {
  return /^\[(asked to run|attempted) ([^\]]+)\]$/.exec((text || "").trim());
}
function replayAssistant(m) {
  const text = Array.isArray(m.content)
    ? m.content.filter((p) => p && p.type === "text").map((p) => p.text || "").join("")
    : String(m.content ?? "");
  if (!text.trim()) return; // empty tool-call placeholder: no row
  const echo = approvalEcho(text);
  if (echo) {
    const denied = echo[1] === "asked to run";
    const card = document.createElement("div");
    card.className = "app-card decided";
    card.innerHTML = `<div class="head"><span class="tag">approval</span><b>${esc(echo[2])}</b><span class="muted small">${denied ? "denied" : "blocked"}</span></div>
      <div class="state">${denied ? "denied by you" : "blocked by policy"}</div>`;
    $("#stream").appendChild(card);
    return;
  }
  addMsg("assistant", text);
}
function userHtml(content) {
  if (Array.isArray(content)) {
    const txt = content.filter((p) => p && p.type === "text").map((p) => inline(p.text || "")).join("<br>");
    const imgs = content.filter((p) => p && p.type === "image_url" && String((p.image_url || {}).url || "").startsWith("data:"))
      .map((p) => `<img class="msg-img" src="${p.image_url.url}" alt="attached image" loading="lazy">`).join("");
    return (txt || "") + (imgs ? `<div class="msg-imgs">${imgs}</div>` : "");
  }
  return inline(content);
}
function addEvline(tag, text, cls = "", dur) {
  const el = document.createElement("div");
  el.className = `evline ${cls}`;
  el.innerHTML = `<span class="tag">${esc(tag)}</span><span class="txt"></span>` + (dur ? `<span class="dur">${esc(dur)}</span>` : "");
  el.querySelector(".txt").textContent = text;
  $("#stream").appendChild(el);
  scrollBottom();
  return el;
}
function scrollBottom(force) {
  const s = $("#stream");
  // follow only while at the bottom; reading up must not be yanked back
  if (force || state.following) {
    state.following = true; state.unread = 0;
    s.scrollTop = s.scrollHeight; $("#jump-latest").hidden = true;
  } else {
    $("#jump-latest").hidden = false;
    $("#jump-latest").textContent = `↓ Latest · ${++state.unread} updates`;
  }
}
$("#stream").addEventListener("scroll", () => {
  const s = $("#stream");
  state.following = s.scrollHeight - s.scrollTop - s.clientHeight < 48;
  if (state.following) { state.unread = 0; $("#jump-latest").hidden = true; }
});
$("#jump-latest").onclick = () => scrollBottom(true);

let liveNode = null, liveText = "";
function ensureLive() { if (!liveNode) liveNode = addMsg("assistant", ""); return liveNode; }
function updateLive(chunk) {
  // an empty chunk must not birth a placeholder row: a turn that resolves
  // into a tool approval would otherwise leave an empty AGENT label behind
  if (!liveNode && !chunk) return;
  liveText += chunk; ensureLive().innerHTML = md(liveText); scrollBottom();
}
function sealLive() {
  // a placeholder that never received content is removed, not sealed
  if (liveNode && !liveNode.textContent.trim()) liveNode.closest(".msg").remove();
  liveNode = null; liveText = ""; sealThinkLive();
}
/* playful activity verbs — a GUI mirror of ohmyllm/verbs.py (the canonical
   TUI list), extended with a few console-exclusive ones. Time-windowed so
   the ticker and any renderer agree without extra state (task #30). */
const VERBS = [
  "pondering", "ruminating", "untangling", "connecting dots", "consulting the oracle",
  "crunching", "squinting", "following the white rabbit", "feeding the hamster", "rubber-ducking",
  "reading the fine print", "overthinking", "marinating", "negotiating with the tokens",
  "doodling", "counting r's in strawberry", "herding cats", "sharpening pencils",
  "brewing", "buffering dramatically", "asking the void", "tidying the desk",
];
const VERB_MS = 3500;
function thinkVerb() {
  const w = Math.floor(Date.now() / VERB_MS);
  // parentheses matter: `>>> 0 % n` parsed as `>>> (0 % n)` and indexed
  // far past the array — the source of the "undefined (step 1)" row
  const pick = (x) => VERBS[(Math.imul(x + 1, 2654435761) >>> 0) % VERBS.length];
  let v = pick(w);
  if (v === pick(w - 1)) v = pick(w + 1);
  return v || "thinking";
}
setInterval(() => {
  const v = thinkVerb();
  const n = state.thinkNode;
  if (n && n !== "sealed" && n.sm) n.sm.textContent = "💭 " + v + "…";
  const chip = $("#run-verb");
  if (chip && !chip.hidden) chip.textContent = v + "…";
}, 1000);
function setRunVerb(on) {
  const chip = $("#run-verb");
  if (!chip) return;
  chip.hidden = !on;
  if (on) chip.textContent = thinkVerb() + "…";
}
function liveThink(t) {
  state.reasonBuf = (state.reasonBuf || "") + t;
  if (state.config && state.config.agent && state.config.agent.thinking_visible === false) {
    const k = Math.max(1, Math.round(state.reasonBuf.length / 100) / 10);
    const label = `💭 thinking · hidden · ${k}k chars`;
    if (!state.thinkNode || state.thinkNode === "sealed") state.thinkNode = { hidden: true, el: addEvline("think", label, "dim") };
    else if (state.thinkNode.el) state.thinkNode.el.querySelector(".txt").textContent = label;
    scrollBottom(); return;
  }
  if (!state.thinkNode || state.thinkNode === "sealed") {
    const det = document.createElement("details"); det.open = true; det.className = "think-details";
    const sm = document.createElement("summary"); sm.textContent = "💭 " + thinkVerb() + "…";
    const pre = document.createElement("pre"); pre.className = "think-body";
    det.append(sm, pre); $("#stream").append(det);
    state.thinkNode = { det, sm, pre };
  }
  state.thinkNode.pre.textContent += t;
  scrollBottom();
}
function sealThinkLive() {
  if (state.thinkNode && state.thinkNode !== "sealed") {
    if (state.thinkNode.hidden) { state.thinkNode = "sealed"; return; }
    if (!state.thinkNode.pre.textContent.trim()) { state.thinkNode.det.remove(); state.thinkNode = null; }
    else { state.thinkNode.det.open = false; state.thinkNode.sm.textContent = "💭 thinking process"; state.thinkNode = "sealed"; }
  }
}
const ACTION = {
  write_file: ["write", "📝"], patch_file: ["edit", "✏️"], run_command: ["exec", "⚡"],
  read_file: ["read", "📖"], list_dir: ["ls", "📂"], glob: ["find", "🔎"], grep: ["find", "🔎"],
  web_fetch: ["net", "🌐"], web_search: ["net", "🌐"], soul_write: ["memory", "🧠"],
  soul_remember: ["memory", "🧠"], soul_forget: ["memory", "🧠"], soul_append: ["memory", "🧠"],
};
const actionOf = (n) => ACTION[n] || ["tool", "🔧"];
function firstLine(s) { const x = String(s || "").trim().split("\n")[0]; return x.length > 140 ? x.slice(0, 140) + "…" : x; }
function shortArgs(a) { try { const s = JSON.stringify(a || {}); return s.length > 90 ? s.slice(0, 90) + "…" : s; } catch { return ""; } }
function planSummary(d) {
  const a = d.args || {};
  switch (d.name) {
    case "run_command": return `⚡ run_command ${firstLine(a.command || "")}`;
    case "write_file": return `📝 write_file ${a.path || "?"} · ${String(a.content || "").length} chars${a.append ? " (append)" : ""}`;
    case "patch_file": return `✏️ patch_file ${a.path || "?"} · −${String(a.old_text || "").split("\n").length} +${String(a.new_text || "").split("\n").length} lines`;
    case "read_file": return `📖 read_file ${a.path || "?"}`;
    default: return `→ ${d.name} ${shortArgs(a)}`;
  }
}
/* tool dock: chronological inline collapsible entries, monospace output only */
function toolEntry(d) {
  const det = document.createElement("details"); det.className = "tool-entry";
  const sm = document.createElement("summary");
  sm.innerHTML = `<span class="tag">${esc((d.level || "low").toLowerCase())}/L${d.risk ?? "?"}</span><span class="txt"></span>`;
  sm.querySelector(".txt").textContent = planSummary(d);
  const args = document.createElement("pre"); args.textContent = JSON.stringify(d.args || {}, null, 1);
  const out = document.createElement("pre"); out.hidden = true;
  det.append(sm, args, out);
  $("#stream").appendChild(det);
  scrollBottom();
  const entry = { det, sm, out, tag: sm.querySelector(".tag") };
  state.toolEntries.set(d.name, entry);
  return entry;
}
function toolResult(d) {
  const entry = state.toolEntries.get(d.name) || toolEntry({ name: d.name, args: {} });
  const cls = d.ok ? "ok" : "err";
  entry.det.classList.add(cls);
  entry.tag.textContent = d.blocked ? "blocked" : d.denied ? "denied" : d.ok ? "ok" : "fail";
  const f = d.file;
  const isWrite = f && (f.created != null || f.added != null || f.removed != null || f.changed != null);
  const head = f && d.ok && isWrite
    ? (f.created ? `created ${f.path} +${f.added || 0}` : `edited ${f.path} -${f.removed || 0} ~${f.changed || 0} +${f.added || 0}`)
    : f && d.ok && f.media ? `read ${f.media}` : "";
  entry.out.hidden = false;
  entry.out.textContent = (head ? head + "\n" : "") + String(d.output ?? "");
  if (d.duration_ms != null) entry.sm.querySelector(".txt").textContent += ` · ${Math.round(d.duration_ms)} ms`;
  scrollBottom();
}
function liveFileProgress(d) {
  const name = d.name || d.tool || "tool";
  let partial;
  if (d.content_tail) {
    let raw = d.content_tail;
    const cm = /^((?:[^"\\]|\\.)*)"/.exec(raw);
    if (cm) raw = cm[1];
    partial = raw.replace(/\\n/g, "\n").replace(/\\"/g, '"');
  } else {
    const m = /"content"\s*:\s*"((?:[^"\\]|\\.)*)$/.exec(d.tail || "");
    if (!m) return;
    partial = m[1].replace(/\\n/g, "\n").replace(/\\"/g, '"');
  }
  const lines = partial.split("\n").slice(-12).join("\n");
  if (!state.fileNode || state.fileNode.name !== name) {
    const det = document.createElement("details"); det.open = true; det.className = "think-details";
    const sm = document.createElement("summary"); sm.textContent = `${actionOf(name)[1]} ${name === "patch_file" ? "patching" : "writing"} ${name}…`;
    const pre = document.createElement("pre"); pre.className = "think-body";
    det.append(sm, pre); $("#stream").append(det);
    state.fileNode = { name, det, pre };
  }
  state.fileNode.pre.textContent = lines;
  scrollBottom();
}
function liveThinkProgress(rawName, chars) {
  const name = rawName || "thinking";
  const k = ((chars / 1000) | 0) || 1;
  if (state.writeNode && state.writeNode.name === name) state.writeNode.el.querySelector(".txt").textContent = `${actionOf(name)[1]} ${name} … ${k}k chars`;
  else state.writeNode = { name, el: addEvline(actionOf(name)[0], `${actionOf(name)[1]} ${name} … ${k}k chars`, "dim") };
}

/* session loading with abort + stale-guard against fetch races */
let loadSeq = 0, loadAbort = null;
async function loadSessionIntoView(sid) {
  if (state.streaming) { toast("Stop the current turn before switching chats", "warn"); return false; }
  const seq = ++loadSeq;
  loadAbort?.abort();
  loadAbort = new AbortController();
  let sess;
  try { sess = await get(`/api/chats/${sid}`, { signal: loadAbort.signal }); }
  catch (e) { if (seq === loadSeq && e.name !== "AbortError") toast(`chat load failed: ${e.message}`, "err"); return false; }
  if (seq !== loadSeq) return false; // a newer switch won the race
  state.drafts.set(state.session, $("#input").value);
  state.session = sid;
  if (state.tabStates[sid] === "done") delete state.tabStates[sid]; // viewed → clear
  $("#input").value = state.drafts.get(sid) || ""; autoGrow();
  clearStream();
  (sess.messages || []).forEach((m) => {
    if (m.role === "user") addMsg("user", m.content);
    else if (m.role === "assistant") replayAssistant(m);
    else if (m.role === "tool") toolResult({ name: m.name || "tool", ok: true, output: String(m.content).slice(0, 4000) });
  });
  if (!sess.messages?.length) emptyState();
  renderTabs();
  return true;
}

/* SSE reader over fetch (POST streaming). Never auto-replays. */
async function sendToAgent(text, memoryDirective) {
  let res;
  try {
    res = await fetch(`${BASE}/api/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-OMLLM-Token": BOOT.token },
      body: JSON.stringify({ text, memory_directive: !!memoryDirective, session: state.session || undefined }),
    });
  } catch (e) { const err = new Error(`bridge unreachable: ${e.message}`); err.partial = false; throw err; }
  if (res.status === 202) { toast("queued into the running turn"); return true; }
  if (!res.ok) { const d = await res.json().catch(() => ({})); toast(d.error || "request failed", "err"); return false; }
  let partial = false;
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const ev = (frame.match(/^event: (.*)$/m) || [])[1] || "message";
      const dl = (frame.match(/^data: (.*)$/m) || [])[1] || "{}";
      if (ev !== "ping") partial = true;
      handleEvent(ev, JSON.parse(dl));
      if (ev === "done") return true;
    }
  }
  const err = new Error("Connection ended before completion."); err.partial = partial; throw err;
}

function handleEvent(ev, d) {
  switch (ev) {
    case "token": updateLive(d.text || ""); break;
    case "assistant_note": {
      const nt = (d.text || "").trim();
      // approval echoes belong to the approval card, never a second row
      if (nt && !approvalEcho(nt) && nt !== liveText.trim()) addMsg("systemish", nt, "systemish");
      sealLive();
      break;
    }
    case "tool_plan": {
      sealLive();
      toolEntry(d);
      break;
    }
    case "tool_result": {
      if (state.fileNode) { state.fileNode.det.remove(); state.fileNode = null; }
      toolResult(d);
      break;
    }
    case "memory_activity": toast(`memory: ${d.tool || d.name || "update"}`, d.ok ? "" : "err"); break;
    case "tool_stream": {
      // the runtime may name the tool via name/tool/label — never render
      // "undefined" in a live row (task #30)
      const nm = d.name || d.tool || d.label || "tool";
      liveThinkProgress(nm, d.chars || 0);
      liveFileProgress({ ...d, name: nm });
      break;
    }
    case "compaction": addEvline("ctx", `🗜 compacted ${d.before}→${d.after} messages`, "dim"); break;
    case "interjection": addMsg("user", d.text || ""); break;
    case "ultrathink_review": liveText = ""; addEvline("ultra", "🌀 reviewing draft, re-deriving, rewriting…", "dim"); break;
    case "agentloop_audit": liveText = ""; addEvline("ultra", "🔁 auditing completion — evidence required before the answer…", "dim"); break;
    case "retry": addEvline("retry", d.reason || "", "warn"); break;
    case "thinking": {
      state.reasonBuf = ""; state.thinkNode = null;
      const step = d.step ?? d.label ?? d.name ?? "?";
      addEvline("think", `💭 ${thinkVerb()} (step ${step})`, "dim");
      break;
    }
    case "thinking_text": liveThink(d.text || ""); break;
    case "error": sealLive(); addEvline("error", d.message || "", "err"); break;
    case "approval_request": {
      // Two events share this name: the runtime's informational echo
      // ({id, name, call_id, risk}) and the bridge's decidable request
      // ({id, tool|name, args, reasons, ...}). Only the decidable payload can
      // be answered — the echo carries call_id and no args, and queuing it
      // would stack a phantom modal whose id the approval endpoint does not
      // know. Discriminate by shape, not by which key names the tool, so the
      // guard holds whether the bridge ships "tool", "name", or both.
      const tool = d && (d.tool || d.name);
      if (d && d.id && tool && d.args && !d.call_id) { queueApproval(d); pollState(); }
      break;
    }
    case "ping": break;
    case "done": {
      sealLive();
      setRunVerb(false);
      if (d.session) { state.session = d.session; addTab(d.session); }
      state.streaming = false;
      $("#cancel").hidden = true; $("#send").disabled = false;
      if (d.error) addEvline("error", d.error, "err");
      const rtext = state.thinkNode ? "" : (d.reasoning || state.reasonBuf || "");
      if (rtext.trim()) {
        const det = document.createElement("details"); det.className = "think-details";
        const sm = document.createElement("summary"); sm.textContent = "💭 thinking process";
        const bd = document.createElement("pre"); bd.className = "think-body"; bd.textContent = rtext;
        det.append(sm, bd); $("#stream").append(det); scrollBottom();
      }
      state.reasonBuf = "";
      const bits = [d.model || "", d.usage?.eval_count ? `${d.usage.eval_count} tok` : "", d.ttft_s ? `ttft ${d.ttft_s.toFixed(1)}s` : "", d.tokens_per_s ? `${d.tokens_per_s.toFixed(1)} tok/s` : "", d.steps ? `${d.steps} step${d.steps > 1 ? "s" : ""}` : "", d.calls?.length ? `${d.calls.length} tool call${d.calls.length > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ");
      if (bits) addEvline("turn", bits, "ok");
      hideSteerChip();
      refreshStatus(); pollState(); renderSidebar();
      break;
    }
  }
}

/* ------------------------------------------------------------ approvals */
function denyRuleOf(reasons) {
  const r = (reasons || []).find((x) => /blocklist rule|denylist|deny rule/i.test(x));
  return r || "";
}
function queueApproval(d) {
  // normalize: the bridge may name the tool via "tool" or "name"; every
  // renderer and the shell once/deny restriction below key off d.tool.
  d = { ...d, tool: d.tool || d.name };
  // exactly one card per request id, even if the bridge re-emits it
  if (state.approvalQueue.some((x) => x.id === d.id)
      || Array.from(document.querySelectorAll(".app-card")).some((c) => c.dataset.aid === d.id)) return;
  sealLive(); // no unfilled placeholder may sit above the card
  state.approvalQueue.push(d);
  // inline card stays in the stream chronologically; the modal decides
  const card = document.createElement("div");
  card.className = "app-card"; card.dataset.aid = d.id;
  const rule = denyRuleOf(d.reasons);
  card.innerHTML = `
    <div class="head"><span class="tag">approval</span><b>${esc(d.tool)}</b><span class="muted small">${esc((d.level || "low").toUpperCase())} · risk ${d.risk ?? "?"}</span></div>
    ${rule ? `<div class="rule">matched deny rule — ${esc(rule)}</div>` : ""}
    <div class="reasons">${esc((d.reasons || []).join(" · "))}</div>
    <div class="acts">
      <button class="btn small ok" type="button" data-dec="approve">Allow once</button>
      <button class="btn small" type="button" data-dec="session" ${d.tool === "run_command" ? "hidden" : ""}>Allow session</button>
      <button class="btn small" type="button" data-dec="always" ${d.tool === "run_command" ? "hidden" : ""}>Permanent rule</button>
      <button class="btn small no" type="button" data-dec="deny">Deny</button>
    </div>
    <div class="state">waiting for your decision…</div>`;
  $("#stream").appendChild(card);
  scrollBottom();
  if (state.approvalQueue.length === 1) showApproval(d);
}
$("#stream").addEventListener("click", (e) => {
  const b = e.target.closest(".app-card button[data-dec]");
  if (b) answerApproval(b.dataset.dec);
});
function showApproval(d) {
  $("#ap-risk").textContent = `${(d.level || "low").toUpperCase()} · risk ${d.risk ?? "?"}`;
  $("#ap-risk").className = `risk-badge ${d.level === "critical" || d.level === "high" ? "exec" : d.level === "medium" ? "write" : "read"}`;
  $("#ap-tool").textContent = d.tool;
  $("#ap-mode").textContent = ` — mode: ${d.mode}`;
  $("#ap-args").textContent = JSON.stringify(d.args, null, 1);
  $("#ap-reasons").textContent = (d.reasons || []).join(" · ");
  const rule = denyRuleOf(d.reasons);
  const ruleEl = $("#ap-rule");
  ruleEl.hidden = !rule;
  ruleEl.textContent = rule ? `matched deny rule — ${rule}` : "";
  const prev = $("#ap-preview");
  const previewText = d.preview || "";
  if (previewText) {
    prev.hidden = false;
    prev.innerHTML = previewText.split("\n").map((l) => {
      const e = esc(l);
      if (l.startsWith("+")) return `<span class="add">${e}</span>`;
      if (l.startsWith("-")) return `<span class="del">${e}</span>`;
      if (l.startsWith("@@")) return `<span class="hunk">${e}</span>`;
      return e;
    }).join("\n");
  } else prev.hidden = true;
  const shell = d.tool === "run_command";
  // shell consent is one-shot only — never session, never permanent
  $("#ap-session").hidden = shell; $("#ap-always").hidden = shell;
  openModal("#approval-modal", "#ap-deny");
  $("#run-state").textContent = "Waiting for approval";
}
function markCard(aid, cls, text) {
  const card = document.querySelector(`.app-card[data-aid="${aid}"]`);
  if (!card) return;
  card.classList.add(cls);
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  card.querySelector(".state").textContent = text;
}
function closeApproval() {
  closeModal("#approval-modal");
  state.approvalQueue.shift();
  if (state.approvalQueue.length) showApproval(state.approvalQueue[0]);
}
async function answerApproval(decision) {
  const d = state.approvalQueue[0];
  if (!d || state.answeringApproval) return; // duplicate-decision guard
  if (d.tool === "run_command" && ["session", "always"].includes(decision)) return;
  state.answeringApproval = true;
  $$("#approval-modal button").forEach((b) => (b.disabled = true));
  try {
    await post(`/api/approval/${d.id}`, { decision });
    markCard(d.id, "decided", decision === "deny" ? "denied by you" : `allowed · ${decision}`);
    closeApproval();
  } catch (e) {
    if (e.status === 404) {
      // fail-closed: the bridge no longer holds this approval → treat as denied
      markCard(d.id, "expired", "expired — denied");
      toast("Approval expired — denied (fail-closed)", "warn");
      closeApproval();
    } else {
      toast(`Approval not confirmed: ${e.message}`, "err");
    }
  } finally {
    state.answeringApproval = false;
    $$("#approval-modal button").forEach((b) => (b.disabled = false));
    $("#run-state").textContent = state.streaming ? "Working" : "Ready";
  }
}
[["ap-allow", "approve"], ["ap-session", "session"], ["ap-always", "always"], ["ap-deny", "deny"]].forEach(([id, dec]) => {
  document.getElementById(id).onclick = () => answerApproval(dec);
});/* ------------------------------------------------------- composer basics */
const input = $("#input"), composer = $("#composer");
const slashHint = $("#slash-hint"), atHint = $("#at-hint");
function autoGrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 240) + "px"; }
function fmtTok(n) { n = Number(n) || 0; if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k"; return String(n); }
function relTime(t) {
  if (!t) return "";
  const d = new Date(t); if (isNaN(d.getTime())) return "";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return Math.max(1, Math.floor(s / 60)) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 7 * 86400) return Math.floor(s / 86400) + "d ago";
  return d.toLocaleDateString();
}
const GLYPH_COLORS = ["#7ac79f", "#58a6e8", "#e2a63d", "#c792ea", "#e0685c", "#8bd5ca", "#f78c6c", "#a6da95"];
function glyphColor(id) { let h = 0; for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return GLYPH_COLORS[h % GLYPH_COLORS.length]; }

/* ---------------------------------------------------- slash / @ completion */
const SLASHES = [
  { cmd: "/help", desc: "help & keyboard map" },
  { cmd: "/new", desc: "start a new chat" },
  { cmd: "/mode", desc: "permission mode…", arg: true },
  { cmd: "/model", desc: "set model…", arg: true },
  { cmd: "/models", desc: "browse models" },
  { cmd: "/backend", desc: "choose backend…", arg: true },
  { cmd: "/think", desc: "reasoning effort off…max", arg: true },
  { cmd: "/ultrathink", desc: "toggle deep reasoning" },
  { cmd: "/tone", desc: "agent voice…", arg: true },
  { cmd: "/agentloop", desc: "toggle relentless completion" },
  { cmd: "/chat", desc: "op mode: chat (no writes)" },
  { cmd: "/plan", desc: "op mode: plan (no writes)" },
  { cmd: "/build", desc: "op mode: build (full agent)" },
  { cmd: "/remember", desc: "write a durable memory", arg: true },
  { cmd: "/steer", desc: "redirect the running turn", arg: true },
  { cmd: "/skill:", desc: "cast a saved skill", arg: true },
  { cmd: "/soul", desc: "edit soul.md" },
  { cmd: "/doctor", desc: "run health checks" },
  { cmd: "/settings", desc: "open settings" },
  { cmd: "/providers", desc: "connect cloud providers" },
  { cmd: "/market", desc: "model market" },
  { cmd: "/export", desc: "export this chat" },
  { cmd: "/cancel", desc: "stop the current turn" },
  { cmd: "/quit", desc: "shut down the bridge" },
];
const REFS = ["file:", "folder:", "diff", "staged", "git:"];
const REF_DESC = { "file:": "attach a file", "folder:": "attach a folder", "diff": "working-tree changes", "staged": "staged changes", "git:": "git ref / log" };
let slashMatches = [], slashSel = 0, atMatches = [], atSel = 0;
function computeHints() {
  const v = input.value;
  if (v.startsWith("/") && !v.includes(" ")) {
    const head = v.toLowerCase();
    slashMatches = SLASHES.filter((s) => s.cmd.startsWith(head));
    slashSel = 0;
  } else { slashMatches = []; }
  const am = v.match(/@([\w:]*)$/);
  if (am) { atMatches = REFS.filter((r) => r.startsWith(am[1].toLowerCase())); atSel = 0; }
  else { atMatches = []; }
  paintHints();
}
function paintHints() {
  if (slashMatches.length) {
    slashHint.innerHTML = slashMatches.map((s, i) => `<div role="option" aria-selected="${i === slashSel}" class="${i === slashSel ? "sel" : ""}" data-i="${i}"><b>${esc(s.cmd)}</b><span>${esc(s.desc)}</span></div>`).join("");
    slashHint.hidden = false;
  } else slashHint.hidden = true;
  if (atMatches.length) {
    atHint.innerHTML = atMatches.map((r, i) => `<div role="option" aria-selected="${i === atSel}" class="${i === atSel ? "sel" : ""}" data-i="${i}"><b>@${esc(r)}</b><span>${esc(REF_DESC[r] || "")}</span></div>`).join("");
    atHint.hidden = false;
  } else atHint.hidden = true;
}
function hideHints() { slashHint.hidden = true; atHint.hidden = true; }
function cycleHint(dir) {
  if (!slashHint.hidden && slashMatches.length) { slashSel = (slashSel + dir + slashMatches.length) % slashMatches.length; paintHints(); }
  else if (!atHint.hidden && atMatches.length) { atSel = (atSel + dir + atMatches.length) % atMatches.length; paintHints(); }
}
function completeSlash(cmd) {
  const meta = SLASHES.find((s) => s.cmd === cmd);
  input.value = cmd + (meta && meta.arg ? " " : "");
  input.focus(); autoGrow(); computeHints();
}
function completeAt(ref) {
  const m = input.value.match(/@([\w:]*)$/); if (!m) return;
  input.value = input.value.slice(0, m.index) + "@" + ref + (ref.endsWith(":") ? "" : " ");
  input.focus(); autoGrow(); computeHints();
}
slashHint.addEventListener("click", (e) => { const d = e.target.closest("[data-i]"); if (d) completeSlash(slashMatches[+d.dataset.i].cmd); });
atHint.addEventListener("click", (e) => { const d = e.target.closest("[data-i]"); if (d) completeAt(atMatches[+d.dataset.i]); });

let composing = false;
input.addEventListener("compositionstart", () => { composing = true; });
input.addEventListener("compositionend", () => { composing = false; });
input.addEventListener("input", () => { autoGrow(); computeHints(); state.drafts.set(state.session, input.value); });
input.addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229 || composing) return; // IME guard
  const hintOpen = !slashHint.hidden || !atHint.hidden;
  if (e.key === "ArrowDown" && hintOpen) { e.preventDefault(); cycleHint(1); return; }
  if (e.key === "ArrowUp" && hintOpen) { e.preventDefault(); cycleHint(-1); return; }
  if (e.key === "Tab" && hintOpen) {
    e.preventDefault();
    if (!slashHint.hidden) completeSlash(slashMatches[slashSel].cmd); else completeAt(atMatches[atSel]);
    return;
  }
  if (e.key === "Escape" && hintOpen) { e.preventDefault(); hideHints(); return; }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    // exact-match rule: a fully typed command sends as-is, never autocorrected
    if (!slashHint.hidden && slashMatches.length && input.value.trim() !== slashMatches[slashSel].cmd) { completeSlash(slashMatches[slashSel].cmd); return; }
    if (!atHint.hidden && atMatches.length) { completeAt(atMatches[atSel]); return; }
    submit();
  }
});
composer.addEventListener("submit", (e) => { e.preventDefault(); if (composing) return; submit(); });

/* ------------------------------------------------------------- submitting */
function restoreDraft(text) { input.value = text; autoGrow(); state.drafts.set(state.session, text); input.focus(); }
async function submit() {
  const text = input.value.trim();
  hideHints(); // complete synchronously before any async work
  if (!text) return;
  if (text.startsWith("/")) { await handleSlash(text); return; }
  if (state.streaming) { await steerOrQueue(text); return; }
  await sendNormal(text, false);
}
async function sendNormal(text, memoryDirective) {
  state._draft = text;
  input.value = ""; autoGrow();
  const userNode = addMsg("user", text).closest(".msg");
  state.streaming = true; state.stopping = false;
  $("#send").disabled = true; $("#cancel").hidden = false; $("#cancel").disabled = false;
  $("#run-state").textContent = "Working";
  setRunVerb(true);
  renderTabs();
  let accepted = false, partial = false;
  try {
    accepted = await sendToAgent(text, memoryDirective);
  } catch (e) {
    sealLive();
    if (e.partial) {
      partial = true;
      addEvline("error", "stream interrupted — the partial reply above is kept; nothing was replayed", "warn");
      toast("Connection dropped mid-reply — partial output kept, send again to continue", "warn");
    } else {
      toast(e.message, "err");
    }
  } finally {
    state.streaming = false; state.stopping = false;
    $("#send").disabled = false; $("#cancel").hidden = true;
    setRunVerb(false);
    if (!state.approvalQueue.length) $("#run-state").textContent = "Ready";
    renderTabs();
  }
  if (!accepted && !partial) { userNode.remove(); restoreDraft(text); } // rejected → draft survives
  else { refreshStatus(); renderSidebar(); }
}
let steerTimer = null;
function showSteerChip(text) { const c = $("#steer-chip"); c.textContent = text; c.hidden = false; clearTimeout(steerTimer); steerTimer = setTimeout(hideSteerChip, 6000); }
function hideSteerChip() { $("#steer-chip").hidden = true; clearTimeout(steerTimer); }
async function steerOrQueue(text) {
  if (!text) { toast("nothing to steer", "warn"); return; }
  let fallback = false, notice409 = false;
  try {
    const r = await post("/api/steer", { text });
    if (r && r.queued) { showSteerChip(`steer queued · position ${r.position}`); input.value = ""; autoGrow(); return; }
    fallback = true;
  } catch (e) {
    if (e.status === 400) { toast(e.message || "steer rejected (cap/queue full)", "err"); return; } // keep draft
    if (e.status === 409 || e.status === 404) { fallback = true; notice409 = e.status === 409; }
    else { toast(e.message, "err"); return; }
  }
  if (!fallback) return;
  if (state.streaming) {
    input.value = ""; autoGrow();
    const ok = await sendToAgent(text).catch(() => false); // 202 interjection
    if (ok) showSteerChip("queued into the running turn"); else toast("steer fallback failed", "err");
  } else {
    if (notice409) toast("no active turn — sent as a normal message", "warn");
    await sendNormal(text, false);
  }
}

/* ---------------------------------------------------------- slash commands */
async function setOpMode(mode) {
  try {
    try { await post("/api/mode", { mode }); }
    catch (e) { if (e.status === 400) await patch("/api/config", { agent: { op_mode: mode } }); else throw e; }
    toast(`op mode: ${mode}`); refreshStatus();
  } catch (e) { toast(e.message, "err"); }
}
async function handleSlash(text) {
  const sp = text.indexOf(" ");
  const cmd = (sp < 0 ? text : text.slice(0, sp)).toLowerCase();
  const arg = sp < 0 ? "" : text.slice(sp + 1).trim();
  const keep = () => { input.value = text; autoGrow(); };
  const clear = () => { input.value = ""; autoGrow(); };
  try {
    switch (cmd) {
      case "/help": clear(); switchView("help"); return;
      case "/new": clear(); newChat(); return;
      case "/mode": clear(); setModeUi(arg || null); return;
      case "/models": clear(); switchView("models"); return;
      case "/model":
        clear();
        if (arg) { await post("/api/model", { backend: state.status ? state.status.backend : "ollama", model: arg }); toast(`model: ${arg}`); refreshStatus(); }
        else switchView("models");
        return;
      case "/backend":
        clear();
        if (arg) { await post("/api/backend", { name: arg }); toast(`backend: ${arg}`); refreshStatus(); }
        else switchView("backend");
        return;
      case "/think": {
        clear();
        const levels = ["off", "low", "medium", "high", "xhigh", "max"];
        if (!levels.includes(arg)) { toast("usage: /think off|low|medium|high|xhigh|max", "warn"); keep(); return; }
        await patch("/api/config", { generation: { effort: arg } }); toast(`effort: ${arg}`); refreshStatus(); return;
      }
      case "/ultrathink": {
        clear();
        const cur = state.config && state.config.generation ? state.config.generation.ultrathink : false;
        await patch("/api/config", { generation: { ultrathink: !cur } }); toast(`ultrathink: ${!cur}`); refreshStatus(); return;
      }
      case "/tone":
        clear();
        if (!arg) { toast("usage: /tone default|ruthless|chill|friendly|professional|terse", "warn"); keep(); return; }
        await patch("/api/config", { agent: { tone: arg } }); toast(`tone: ${arg}`); refreshStatus(); return;
      case "/agentloop": {
        clear();
        const cur = state.config && state.config.agent ? state.config.agent.agent_loop : false;
        await patch("/api/config", { agent: { agent_loop: !cur } }); toast(`agent loop: ${!cur}`); refreshStatus(); return;
      }
      case "/chat": case "/plan": case "/build": clear(); setOpMode(cmd.slice(1)); return;
      case "/remember":
        if (!arg) { toast("usage: /remember <fact>", "warn"); keep(); return; }
        clear(); await sendNormal(arg, true); return;
      case "/steer": clear(); await steerOrQueue(arg); return;
      case "/soul": clear(); switchView("memory"); return;
      case "/doctor": clear(); switchView("doctor"); return;
      case "/settings": clear(); switchView("settings"); return;
      case "/providers": clear(); switchView("providers"); return;
      case "/market": clear(); switchView("market"); return;
      case "/export":
        clear();
        if (!state.session) { toast("no active chat to export", "warn"); return; }
        { const r = await post(`/api/chats/${state.session}/export`, {}); toast(`exported → ${r.path}`); }
        return;
      case "/cancel": clear(); $("#cancel").click(); return;
      case "/quit":
        clear();
        if (await confirmModal("Shut down?", "This stops the local bridge and ejects the model.", "Shut down")) { await post("/api/shutdown", {}); toast("bridge shutting down"); }
        return;
      default:
        if (cmd.startsWith("/skill:")) {
          clear();
          const name = cmd.slice(7);
          if (!state.session) { toast("start a chat first", "warn"); return; }
          await post("/api/skills/cast", { name, session: state.session }); toast(`cast skill: ${name}`); renderSidebar(); return;
        }
        toast(`unknown command ${cmd} — try /help`, "err"); keep();
    }
  } catch (e) { toast(e.message, "err"); keep(); }
}

/* ----------------------------------------------------------- tabs & state
   The tab strip shows state.openTabs (an explicit, closable list), never
   the whole session history. It scrolls horizontally with edge fades, the
   "+" button stays pinned outside it, and the active tab auto-scrolls into
   view — no tab is ever unreachable (task #30). */
function addTab(id) {
  if (id && !state.openTabs.includes(id)) state.openTabs.push(id);
}
function pruneTab(id) {
  state.openTabs = state.openTabs.filter((t) => t !== id);
}
function updateTabFades() {
  const t = $("#tabs"), w = $("#tabs-wrap");
  if (!t || !w) return;
  w.classList.toggle("can-l", t.scrollLeft > 2);
  w.classList.toggle("can-r", t.scrollLeft + t.clientWidth < t.scrollWidth - 2);
}
function renderTabs() {
  const tabs = $("#tabs");
  const keepScroll = tabs.scrollLeft;
  tabs.innerHTML = "";
  state.openTabs.forEach((id) => {
    const c = state.chats.find((x) => x.id === id) || { id, title: "New chat" };
    const b = document.createElement("div");
    b.className = "tab" + (id === state.session ? " active" : "");
    b.dataset.sid = id;
    b.setAttribute("role", "button");
    b.tabIndex = 0;
    b.title = c.title || "New chat";
    const st = state.tabStates[id] || "idle";
    const glyph = document.createElement("span"); glyph.className = "glyph"; glyph.style.background = glyphColor(id);
    glyph.textContent = (c.title || "?").trim().charAt(0).toUpperCase() || "?";
    const t = document.createElement("span"); t.className = "t"; t.textContent = c.title || "New chat";
    const dot = document.createElement("span"); dot.className = "dot " + st; dot.setAttribute("aria-label", st);
    const x = document.createElement("button");
    x.type = "button"; x.className = "tab-close"; x.tabIndex = -1;
    x.setAttribute("aria-label", `Close tab ${c.title || "New chat"}`);
    x.title = "Close tab (session stays in Chats)";
    x.innerHTML = '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 2l6 6M8 2l-6 6"/></svg>';
    x.onclick = (e) => { e.stopPropagation(); closeTab(id); };
    b.append(glyph, t, dot, x);
    b.onclick = () => openChat(id);
    b.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openChat(id); }
      else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); closeTab(id); }
    };
    // middle-click closes, matching editor tab conventions
    b.onauxclick = (e) => { if (e.button === 1) { e.preventDefault(); closeTab(id); } };
    tabs.appendChild(b);
  });
  tabs.scrollLeft = keepScroll;
  updateTabFades();
  const act = tabs.querySelector(".tab.active");
  if (act) act.scrollIntoView({ block: "nearest", inline: "nearest" });
}
async function closeTab(id) {
  if (!state.openTabs.includes(id)) return;
  const idx = state.openTabs.indexOf(id);
  pruneTab(id);
  if (state.session === id) {
    const next = state.openTabs[Math.min(idx, state.openTabs.length - 1)];
    if (next) await openChat(next);
    else {
      if (state.streaming) { toast("Stop the current turn before closing the last tab", "warn"); addTab(id); renderTabs(); return; }
      state.session = null;
      clearStream(); emptyState();
    }
  }
  renderTabs();
}
$("#tabs").addEventListener("scroll", updateTabFades);
// vertical wheel becomes horizontal scroll over the strip
$("#tabs").addEventListener("wheel", (e) => {
  const t = $("#tabs");
  if (t.scrollWidth <= t.clientWidth) return;
  if (Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
    e.preventDefault();
    t.scrollLeft += e.deltaY;
  }
}, { passive: false });
window.addEventListener("resize", updateTabFades);
let polling = false;
async function pollState() {
  if (polling) return; polling = true;
  try {
    let s = null;
    try { s = await get("/api/state"); } catch (e) { if (e.status !== 404) throw e; }
    if (s && s.state) {
      const sid = s.session_id;
      if (sid) {
        if (s.state === "working") state.tabStates[sid] = "working";
        else if (s.state === "blocked") state.tabStates[sid] = "blocked";
        else if (s.state === "error") state.tabStates[sid] = "error";
        else if (s.state === "done") {
          state._lastDoneSeq = state._lastDoneSeq || {};
          if ((state._lastDoneSeq[sid] || 0) < (s.seq || 0)) {
            state._lastDoneSeq[sid] = s.seq || 0;
            if (sid !== state.session) state.tabStates[sid] = "done"; // unread only
          }
        } else if (s.state === "idle" && state.tabStates[sid] !== "done") delete state.tabStates[sid];
      }
      if (!state.streaming) $("#run-state").textContent = s.state === "blocked" ? "Waiting for approval" : s.state === "working" ? "Working" : "Ready";
    } else if (state.session) {
      // fallback until /api/state lands: derive from status + approval queue
      const busy = (state.status && state.status.busy) || state.streaming;
      if (state.approvalQueue.length) state.tabStates[state.session] = "blocked";
      else if (busy) state.tabStates[state.session] = "working";
      else if (state.tabStates[state.session] !== "done") delete state.tabStates[state.session];
    }
    renderTabs();
  } catch (e) { /* offline — keep last known dots */ }
  finally { polling = false; }
}
async function openChat(id) {
  if (state.view !== "chat") await switchView("chat");
  const ok = await loadSessionIntoView(id);
  if (ok) { addTab(id); renderTabs(); closeSidebarNarrow(); }
}

/* --------------------------------------------------------------- sidebar */
async function renderSidebar() {
  const q = $("#sb-search").value.trim();
  let rows = [];
  try {
    if (q) {
      try {
        const r = await get(`/api/search?q=${encodeURIComponent(q)}&limit=30`);
        rows = (r.results || []).map((x) => ({ id: x.session_id, title: x.title, updated: x.updated_iso, preview: x.snippet, pinned: false }));
      } catch (e) {
        if (e.status === 404) { const r = await get(`/api/chats?q=${encodeURIComponent(q)}`); rows = r.chats || []; }
        else throw e;
      }
    } else { const r = await get("/api/chats"); rows = r.chats || []; }
  } catch (e) { rows = q ? [] : state.chats; }
  if (!q) state.chats = rows;
  renderTabs();
  const list = $("#sb-list"), hero = $("#sb-hero");
  list.innerHTML = "";
  if (!rows.length) {
    if (!q) {
      hero.hidden = false;
      hero.innerHTML = `<span class="hero-glyph"><svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="12" fill="none" stroke="currentColor" stroke-width="2.2"/><circle cx="16" cy="16" r="4.5" fill="currentColor"/></svg></span>
        <h2>No chats yet.</h2><p>Everything stays on this machine. Try a real command:</p>${teachHtml()}`;
    } else { hero.hidden = false; hero.innerHTML = `<p class="muted">No chats match “${esc(q)}”.</p>`; }
    return;
  }
  hero.hidden = true;
  rows.forEach((c) => {
    const row = document.createElement("div");
    row.className = "sb-row" + (c.id === state.session ? " active" : "");
    row.tabIndex = 0;
    row.innerHTML = `<div class="sb-main">
      <div class="top"><span class="title">${esc(c.title || "New chat")}</span>${c.pinned ? '<span class="pin" aria-label="Pinned">●</span>' : ""}</div>
      <div class="meta"><p class="prev">${esc(c.preview || "")}</p><time>${esc(relTime(c.updated))}</time></div>
    </div>
    <span class="sb-acts" role="group" aria-label="Chat actions">
      <button type="button" data-act="pin" class="${c.pinned ? "pinned" : ""}" aria-label="Pin chat" title="${c.pinned ? "Unpin" : "Pin"}"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2h4l-1 4 3 3v1H4V9l3-3-1-4zM8 10v4"/></svg></button>
      <button type="button" data-act="export" aria-label="Export chat" title="Export"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2v8M5 7l3 3 3-3M3 12h10"/></svg></button>
      <button type="button" data-act="rename" aria-label="Rename chat" title="Rename"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M11.5 2.5l2 2L6 12H4v-2l7.5-7.5zM10 4l2 2"/></svg></button>
      <button type="button" data-act="del" class="del" aria-label="Delete chat" title="Delete"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4h10M6 4V3h4v1M5 4l1 9h4l1-9M7 6v5M9 6v5"/></svg></button>
    </span>`;
    row.addEventListener("click", (e) => { if (!e.target.closest(".sb-acts")) openChat(c.id); });
    row.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.target.closest(".sb-acts")) openChat(c.id); });
    row.querySelector(".sb-acts").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-act]"); if (!b) return;
      e.stopPropagation();
      const act = b.dataset.act;
      try {
        if (act === "pin") { await patch(`/api/chats/${c.id}`, { pinned: !c.pinned }); renderSidebar(); }
        else if (act === "export") { const r = await post(`/api/chats/${c.id}/export`, {}); toast(`exported → ${r.path}`); }
        else if (act === "rename") { const t = await promptModal("Rename chat", "New title", false, c.title || ""); if (t) { await patch(`/api/chats/${c.id}`, { title: t }); renderSidebar(); } }
        else if (act === "del") {
          if (await confirmModal("Delete chat?", "It is moved to trash and exported first.", "Delete")) {
            await del(`/api/chats/${c.id}`);
            pruneTab(c.id);
            if (state.session === c.id) { state.session = null; emptyState(); }
            renderSidebar();
          }
        }
      } catch (err) { toast(err.message, "err"); }
    });
    list.appendChild(row);
  });
}
$("#sb-search").addEventListener("input", debounce(() => renderSidebar(), 250));

/* ---------------------------------------------------------------- status */
function syncBackendSelect(cur) {
  const sel = $("#sel-backend");
  if (!sel.options.length) sel.setOptions(["ollama", "lmstudio", "mock", "cloud"].map((b) => [b, b]));
  if (!sel.contains(document.activeElement)) sel.value = cur || "ollama";
}
async function loadModels(backend, selectModel, force) {
  try {
    let data = force ? null : state.modelsCache[backend];
    if (!data) { data = await get(`/api/models?backend=${encodeURIComponent(backend)}`); state.modelsCache[backend] = data; }
    const sel = $("#sel-model");
    const cur = selectModel || sel.value;
    sel.setOptions((data.models || []).map((m) => [m.name, m.name]));
    if (cur && sel.options.some((o) => o.value === cur)) sel.value = cur;
    else if (data.default) sel.value = data.default;
    return data;
  } catch (e) { return null; }
}
async function refreshStatus() {
  try {
    const s = await get("/api/status");
    state.status = s; state.connected = true; state.everConnected = true;
    $("#boot-error").hidden = true;
    $("#conn-dot").classList.remove("off");
    const used = s.ctx_used || 0, win = s.ctx_window || s.num_ctx || 0;
    $("#ctx-badge").textContent = `◍ ${fmtTok(used)}/${fmtTok(win)}`;
    $("#ctx-badge").title = `context ${used} / ${win} tokens` + (s.tok_s ? ` · ${s.tok_s.toFixed(1)} tok/s` : "");
    const chip = $("#chip-mode");
    chip.textContent = `⦿ ${s.mode}`;
    chip.className = `badge mode mode-${s.mode}`;
    $("#version").textContent = "v" + (s.version || "");
    if (!$("#sel-opmode").contains(document.activeElement)) $("#sel-opmode").value = s.op_mode || "build";
    syncBackendSelect(s.backend);
    if (!$("#sel-effort").contains(document.activeElement)) $("#sel-effort").value = s.effort || "off";
    await loadModels(s.backend, s.model);
    if (!state.streaming && !state.approvalQueue.length) $("#run-state").textContent = "Ready";
    try { state.config = await get("/api/config"); } catch (e) { /* keep last config */ }
  } catch (e) {
    state.connected = false;
    $("#conn-dot").classList.add("off");
    if (!state.everConnected) { $("#boot-error").hidden = false; $("#boot-error-msg").textContent = e.message || "Cannot reach the local bridge."; }
    $("#run-state").textContent = "Disconnected";
  }
}

/* ------------------------------------------------------- permission mode */
const MODE_DESC = {
  strict: "No modifying tools. No shell, no network, no file writes. Memory writes only when you explicitly ask.",
  safe: "Read-only access inside the workspace. No shell, no network, no writes. soul.md memory tools are allowed.",
  sandboxed: "Default agentic mode. Reads/writes limited to the workspace; writes ask for confirmation (per-session option); only a small allowlist of safe commands; no network by default.",
  dangerous: "Broader filesystem and shell access. Destructive operations still ask. Network needs explicit approval. High risk.",
  full_access: "No routine prompts. Enabled only by typing the confirmation phrase. Catastrophic commands remain blocked unless you override. Everything is logged.",
};
function ensureModeModal() {
  let m = $("#mode-modal");
  if (m) return m;
  m = document.createElement("div");
  m.id = "mode-modal"; m.className = "modal"; m.hidden = true;
  m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true"); m.setAttribute("aria-label", "Permission mode");
  m.innerHTML = `<header><h3>Permission mode</h3></header><div class="ap-body"></div><footer><button class="btn modal-cancel" type="button">Cancel</button></footer>`;
  m.querySelector(".modal-cancel").onclick = () => closeModal(m);
  document.body.appendChild(m);
  return m;
}
function setModeUi(preset) {
  if (preset && MODE_DESC[preset]) { chooseMode(preset); return; }
  const m = ensureModeModal();
  const body = m.querySelector(".ap-body");
  body.innerHTML = "";
  Object.keys(MODE_DESC).forEach((mode) => {
    const opt = document.createElement("div");
    opt.className = "opt" + (state.status && state.status.mode === mode ? " sel" : "");
    opt.tabIndex = 0;
    opt.innerHTML = `<div><b>${esc(mode)}</b><div class="d">${esc(MODE_DESC[mode])}</div></div>`;
    opt.onclick = () => chooseMode(mode);
    opt.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chooseMode(mode); } };
    body.appendChild(opt);
  });
  openModal(m, ".opt");
}
async function chooseMode(mode) {
  try {
    let phrase;
    if (mode === "full_access") {
      phrase = await promptModal("Confirm FULL ACCESS", "Type the exact phrase: I ACCEPT FULL ACCESS", false, "");
      if (phrase !== "I ACCEPT FULL ACCESS") { toast("phrase mismatch — mode unchanged", "warn"); return; }
    }
    await post("/api/mode", { mode, phrase });
    closeModal("#mode-modal");
    toast(`permission mode: ${mode}`);
    refreshStatus();
  } catch (e) { toast(e.message, "err"); }
}

/* ----------------------------------------------------------- view routing */
async function switchView(view) {
  closeMenus();
  if (view === state.view) return;
  if ((state.view === "settings" && state.settingsDirty) || (state.view === "memory" && state.soulDirty)) {
    const ok = await confirmModal("Unsaved edits", "You have unsaved edits. They are kept until you save or discard them.", "Leave");
    if (!ok) return;
  }
  state.view = view;
  $$(".rail-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => { v.hidden = true; });
  const el = $("#view-" + view);
  if (el) el.hidden = false;
  if (view === "models") renderModels();
  else if (view === "providers") renderProviders();
  else if (view === "market") renderMarket();
  else if (view === "backend") renderBackends();
  else if (view === "memory") renderSoul();
  else if (view === "doctor") renderDoctor();
  else if (view === "settings") renderSettings();
  else if (view === "help") renderHelp();
  else if (view === "chat") input.focus();
}
async function newChat() {
  if (state.streaming) { toast("Stop the current turn before starting a new chat", "warn"); return; }
  if (state.changingChat) return;
  state.changingChat = true;
  try {
    const sess = await post("/api/chats", { first_message: "" });
    state.drafts.set(state.session, input.value);
    state.session = sess.id;
    addTab(sess.id);
    input.value = state.drafts.get(sess.id) || ""; autoGrow();
    emptyState();
    await renderSidebar();
    if (state.view !== "chat") await switchView("chat");
    input.focus();
  } catch (e) { toast(e.message, "err"); }
  finally { state.changingChat = false; }
}
$("#cancel").onclick = async () => {
  if (state.stopping) return;
  state.stopping = true;
  $("#run-state").textContent = "Stopping…";
  $("#cancel").disabled = true;
  try { await post("/api/cancel", {}); } catch (e) { toast(e.message, "err"); }
  setTimeout(() => { state.stopping = false; $("#cancel").disabled = false; }, 1500);
};/* ---------------------------------------------------------- models panel */
let modelsData = null, modelsBackend = "";
function humanBytes(n) {
  if (n == null) return "—";
  if (n >= 2 ** 30) return (n / 2 ** 30).toFixed(1) + " GB";
  if (n >= 2 ** 20) return Math.round(n / 2 ** 20) + " MB";
  return Math.round(n / 2 ** 10) + " KB";
}
const familyOf = (name) => String(name).split(/[:\-]/)[0];
async function renderModels(force) {
  const selB = $("#model-backend");
  if (!selB.options.length) selB.setOptions([["", "active backend"], ["ollama", "ollama"], ["lmstudio", "lmstudio"], ["mock", "mock"], ["cloud", "cloud"]]);
  modelsBackend = selB.value || (state.status ? state.status.backend : "ollama");
  $("#model-detail").textContent = "loading models…";
  let data = force ? null : state.modelsCache[modelsBackend];
  if (!data) {
    try { data = await get(`/api/models?backend=${encodeURIComponent(modelsBackend)}`); state.modelsCache[modelsBackend] = data; }
    catch (e) { data = null; }
  }
  modelsData = data;
  if (!data) { $("#models-table tbody").innerHTML = ""; $("#model-detail").textContent = "could not list models — is the backend running?"; return; }
  paintModels();
}
function paintModels() {
  const data = modelsData; if (!data) return;
  const q = $("#model-search").value.trim().toLowerCase();
  const sort = $("#model-sort").value;
  const byName = (a, b) => a.name.localeCompare(b.name);
  const cmp = {
    name: byName,
    size: (a, b) => (b.size_bytes || 0) - (a.size_bytes || 0) || byName(a, b),
    params: (a, b) => (b.params_b || 0) - (a.params_b || 0) || byName(a, b),
    quant: (a, b) => String(a.quant || "").localeCompare(String(b.quant || "")) || byName(a, b),
    family: (a, b) => familyOf(a.name).localeCompare(familyOf(b.name)) || byName(a, b),
    thinking: (a, b) => (b.thinking ? 1 : 0) - (a.thinking ? 1 : 0) || byName(a, b),
    vision: (a, b) => (b.vision === true ? 1 : 0) - (a.vision === true ? 1 : 0) || byName(a, b),
  }[sort] || byName;
  const rows = (data.models || []).filter((m) => !q || m.name.toLowerCase().includes(q)).sort(cmp);
  const tb = $("#models-table tbody"); tb.innerHTML = "";
  const fitCls = { fits: "fit-ok", tight: "fit-tight", no: "fit-over" };
  rows.forEach((m) => {
    const tr = document.createElement("tr");
    if (m.name === data.default) tr.className = "active";
    tr.innerHTML = `
      <td class="mono">${esc(m.name)}${m.loaded ? ' <span class="muted small">loaded</span>' : ""}</td>
      <td>${esc(m.size || humanBytes(m.size_bytes))} · ${esc(m.quant || "?")}</td>
      <td>${m.params_b ? esc(m.params_b) + "B" : "?"}</td>
      <td>${m.ctx_len || "?"}</td>
      <td>${humanBytes(m.vram_min)}</td>
      <td class="${fitCls[m.fit] || "muted"}">${esc(m.fit || "?")}</td>
      <td>${m.tool_capable === true ? "✓" : m.tool_capable === false ? "weak" : "?"}</td>
      <td>${m.vision === true ? "✓" : m.vision === false ? "✗" : "?"}</td>
      <td>${m.thinking ? "✓" : "—"}</td>
      <td class="acts"></td>`;
    const acts = tr.querySelector(".acts");
    const use = document.createElement("button");
    use.type = "button"; use.className = "btn small"; use.textContent = m.name === data.default ? "active" : "use";
    use.disabled = m.name === data.default;
    use.setAttribute("aria-label", `Use model ${m.name}`);
    use.onclick = async (e) => {
      e.stopPropagation();
      try { await post("/api/model", { backend: modelsBackend, model: m.name }); toast(`model: ${m.name}`); refreshStatus(); renderModels(true); }
      catch (err) { toast(err.message, "err"); }
    };
    const probe = document.createElement("button");
    probe.type = "button"; probe.className = "btn small"; probe.textContent = "probe";
    probe.title = "Probe vision support"; probe.setAttribute("aria-label", `Probe vision support of ${m.name}`);
    probe.onclick = async (e) => {
      e.stopPropagation(); probe.disabled = true;
      try { const r = await post("/api/vision/probe", { backend: modelsBackend, model: m.name }); m.vision = r.vision; paintModels(); toast(`vision: ${r.vision}`); }
      catch (err) { toast(err.message, "err"); }
      finally { probe.disabled = false; }
    };
    acts.append(use, probe);
    tr.onclick = () => {
      const bits = [];
      if (m.why_weak) bits.push("tool support: " + m.why_weak);
      (m.warnings || []).forEach((w) => bits.push("⚠ " + w));
      (m.notes || []).forEach((n) => bits.push("· " + n));
      if (m.vram_comfortable) bits.push(`comfortable VRAM ≈ ${humanBytes(m.vram_comfortable)}`);
      $("#model-detail").textContent = m.name + " — " + (bits.length ? bits.join("  ") : "no warnings");
    };
    tb.appendChild(tr);
  });
  $("#model-detail").textContent = rows.length
    ? `${rows.length} model(s) · backend ${data.backend}${data.running ? "" : " (not running)"} — click a row for fit details`
    : (q ? `no models match “${q}”` : "no models found — pull one or start the backend");
}
async function pullModel(name) {
  const bar = $("#pull-bar"); bar.hidden = false; bar.value = 0;
  $("#model-detail").textContent = `pulling ${name}…`;
  try {
    const res = await fetch(`${BASE}/api/pull`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-OMLLM-Token": BOOT.token },
      body: JSON.stringify({ backend: modelsBackend || "ollama", name }),
    });
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || `HTTP ${res.status}`); }
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = (frame.match(/^event: (.*)$/m) || [])[1] || "message";
        const d = JSON.parse((frame.match(/^data: (.*)$/m) || [])[1] || "{}");
        if (ev === "progress") {
          const pct = Math.round((d.pct || 0) * 100);
          bar.value = pct;
          $("#model-detail").textContent = `pulling ${name} — ${d.status || ""} ${pct}%`;
        } else if (ev === "done") {
          if (d.ok) { toast(`pulled ${d.name || name}`); delete state.modelsCache[modelsBackend]; renderModels(true); }
          else { toast(d.error || "pull failed", "err"); $("#model-detail").textContent = d.error || "pull failed"; }
          return;
        }
      }
    }
  } catch (e) { toast(e.message, "err"); $("#model-detail").textContent = e.message; }
  finally { bar.hidden = true; }
}
$("#model-search").addEventListener("input", debounce(() => paintModels(), 200));
$("#model-sort").addEventListener("change", () => paintModels());
$("#model-backend").addEventListener("change", () => renderModels());
$("#models-refresh").onclick = () => renderModels(true);
$("#models-pull").onclick = async () => {
  const name = await promptModal("Pull a model", "Ollama model name, e.g. qwen2.5:7b", false, "");
  if (name) pullModel(name);
};

/* ------------------------------------------------------- providers panel */
let provData = null;
async function renderProviders() {
  $("#prov-list").innerHTML = '<p class="muted">loading providers…</p>';
  try { provData = await get("/api/providers"); } catch (e) { $("#prov-list").innerHTML = ""; toast(e.message, "err"); return; }
  paintProviders();
}
function provGlyph(p) {
  const icon = PROV_ICONS[p.pid];
  if (icon) return `<span class="prov-glyph"><svg viewBox="0 0 24 24" fill="${_logoColor(icon[0])}" aria-hidden="true"><path d="${icon[1]}"/></svg></span>`;
  return `<span class="prov-glyph">${esc((p.name || "?").charAt(0).toUpperCase())}</span>`;
}
function paintProviders() {
  if (!provData) return;
  const q = $("#prov-search").value.trim().toLowerCase();
  const list = $("#prov-list"); list.innerHTML = "";
  const rows = (provData.providers || []).filter((p) => !q || (p.name + " " + p.pid + " " + (p.blurb || "")).toLowerCase().includes(q));
  const groups = [
    ["recommended", rows.filter((p) => p.recommended)],
    ["popular", rows.filter((p) => p.popular && !p.recommended)],
    ["all providers", rows.filter((p) => !p.popular && !p.recommended && !p.pid.startsWith("custom-"))],
    ["custom endpoints", rows.filter((p) => p.pid.startsWith("custom-"))],
  ];
  groups.forEach(([label, ps]) => {
    if (!ps.length) return;
    const h = document.createElement("div"); h.className = "prov-group"; h.textContent = label; list.appendChild(h);
    ps.forEach((p) => {
      const row = document.createElement("div"); row.className = "prov-row";
      row.innerHTML = `${provGlyph(p)}<span class="prov-name">${esc(p.name)}</span><span class="prov-blurb">${esc(p.blurb || "")}</span>
        ${p.active ? '<span class="pb active">active</span>' : ""}${p.has_key ? `<span class="pb ok">${esc(p.masked || "key set")}</span>` : ""}${p.recommended ? '<span class="pb rec">rec</span>' : ""}
        <span class="prov-acts"></span>`;
      const acts = row.querySelector(".prov-acts");
      const mk = (label2, fn, cls = "btn small") => {
        const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label2;
        b.setAttribute("aria-label", `${label2} ${p.name}`);
        b.onclick = (e) => { e.stopPropagation(); fn(); };
        acts.appendChild(b);
      };
      if (!p.has_key) mk("connect", () => provConnect(p));
      else {
        if (!p.active) mk("use", () => provUse(p));
        mk("re-key", () => provConnect(p));
        mk("forget", () => provForget(p), "btn small danger");
      }
      list.appendChild(row);
    });
  });
  const foot = document.createElement("div"); foot.className = "prov-foot";
  const add = document.createElement("button"); add.type = "button"; add.className = "btn small";
  add.textContent = "+ custom OpenAI-compatible endpoint"; add.onclick = provAddCustom;
  foot.appendChild(add); list.appendChild(foot);
}
async function provConnect(p) {
  const hint = p.signup_url ? `Get a key: ${p.signup_url}` : "Paste the API key — stored locally in owner-only config.toml.";
  const key = await promptModal(`Connect ${p.name}`, hint, true, "");
  if (!key) return;
  try {
    let r = await post("/api/providers/key", { pid: p.pid, key, probe: true });
    if (r.models_ok === false) {
      // gateway without a /models listing — ask for ids, re-sending the key
      // (an empty key would DELETE it)
      const models = await promptModal(`${p.name}: model ids`, "No /models listing on this gateway — enter comma-separated model ids.", false, "");
      if (models) r = await post("/api/providers/key", { pid: p.pid, key, models });
    }
    provData = r; paintProviders();
    toast(`${p.name} key saved`);
  } catch (e) { toast(e.message, "err"); }
}
async function provUse(p) {
  try { provData = await post("/api/providers/use", { pid: p.pid }); paintProviders(); toast(`using ${p.name} (cloud backend)`); refreshStatus(); }
  catch (e) { toast(e.message, "err"); }
}
async function provForget(p) {
  if (!await confirmModal(`Forget ${p.name}?`, "The stored API key is removed from config.toml.", "Forget")) return;
  try { provData = await post("/api/providers/forget", { pid: p.pid }); paintProviders(); toast(`${p.name} key removed`); refreshStatus(); }
  catch (e) { toast(e.message, "err"); }
}
async function provAddCustom() {
  const name = await promptModal("Custom provider", "Display name", false, "");
  if (!name) return;
  const base = await promptModal("Base URL", "OpenAI-compatible base, e.g. https://host/v1", false, "");
  if (!base) return;
  const models = await promptModal("Model ids", "Comma-separated model ids (optional)", false, "");
  try { provData = await post("/api/providers/custom", { name, base_url: base, models: models || "" }); paintProviders(); toast(`added ${name}`); }
  catch (e) { toast(e.message, "err"); }
}
$("#prov-search").addEventListener("input", debounce(() => paintProviders(), 200));

/* ---------------------------------------------------------- market panel */
let marketData = null, marketSection = "", marketTimer = null;
async function renderMarket() {
  $("#market-grid").innerHTML = '<p class="muted">loading catalog…</p>';
  try { marketData = await get("/api/market"); } catch (e) { $("#market-grid").innerHTML = ""; toast(e.message, "err"); return; }
  paintMarket();
  pollMarket(); // surface any in-flight download
}
function paintMarket() {
  if (!marketData) return;
  const entries = marketData.entries || [];
  const sections = [...new Set(entries.map((e) => e.section))];
  if (marketSection && !sections.includes(marketSection)) marketSection = "";
  const head = $("#market-head");
  head.innerHTML = `<span>GPU: ${esc(marketData.gpu || "unknown")}</span><span>VRAM: ${humanBytes(marketData.vram_bytes)}</span><span class="market-seg"></span>`;
  const seg = head.querySelector(".market-seg");
  [["all", ""], ...sections.map((s) => [s, s])].forEach(([label, val]) => {
    const n = val ? entries.filter((e) => e.section === val).length : entries.length;
    const b = document.createElement("button"); b.type = "button";
    b.className = "chip" + (marketSection === val ? " active" : "");
    b.innerHTML = `${esc(label)}<span class="cnt">${n}</span>`;
    b.onclick = () => { marketSection = val; paintMarket(); };
    seg.appendChild(b);
  });
  const grid = $("#market-grid"); grid.innerHTML = "";
  const fitCls = { fits: "ok", tight: "warn", no: "bad", unknown: "dim" };
  entries.filter((e) => !marketSection || e.section === marketSection).forEach((e) => {
    const card = document.createElement("div"); card.className = "market-card"; card.tabIndex = 0;
    const pct = marketData.vram_bytes ? Math.min(100, Math.round(((e.size_gb * 2 ** 30) / marketData.vram_bytes) * 100)) : 0;
    card.innerHTML = `
      <div class="mc-top"><span class="mc-title" title="${esc(e.title)}">${esc(e.title)}</span><span class="mc-size">${esc(e.size_gb)} GB</span></div>
      <div class="mc-fit ${fitCls[e.fit] || "dim"}">${esc(e.fit_label || e.fit || "?")}</div>
      <div class="mc-blurb">${esc(e.blurb || e.short || "")}</div>
      <div class="mc-bar"><i class="${fitCls[e.fit] || "dim"}" style="width:${pct}%"></i></div>
      <div class="mc-foot"><span class="mc-repo" title="${esc(e.repo || "")}">${esc(e.repo || "")}</span><span class="mc-dl">download →</span></div>`;
    const go = () => marketDownload(e);
    card.onclick = go;
    card.onkeydown = (ev) => { if (ev.key === "Enter" && !ev.isComposing) go(); };
    grid.appendChild(card);
  });
}
async function marketDownload(e) {
  const warn = e.fit === "no" ? " It does NOT fit your VRAM — expect CPU offload or failure." : "";
  if (!await confirmModal(`Download ${e.title}?`, `${e.size_gb} GB from ${e.repo || "the catalog"}.${warn} ${e.fit_label || ""}`, "Download")) return;
  try {
    await post("/api/market/download", { mid: e.mid });
    $("#market-dl").hidden = false;
    $("#market-dl-label").textContent = `downloading ${e.title}…`;
    $("#market-bar-fill").style.width = "0%";
    pollMarket();
  } catch (err) { toast(err.message, "err"); }
}
async function pollMarket() {
  clearTimeout(marketTimer);
  let p;
  try { p = await get("/api/market/progress"); } catch (e) { return; }
  if (p.active) {
    $("#market-dl").hidden = false;
    const pct = p.total ? Math.min(100, (p.received / p.total) * 100) : 0;
    $("#market-bar-fill").style.width = pct.toFixed(1) + "%";
    $("#market-dl-label").textContent = `${p.title} — ${humanBytes(p.received)} / ${humanBytes(p.total)} · ${p.mbps} MB/s · eta ${p.eta_s}s`;
    marketTimer = setTimeout(pollMarket, 1000);
  } else if (p.done) {
    $("#market-bar-fill").style.width = "100%";
    $("#market-dl-label").textContent = `${p.title} — done`;
    toast(`downloaded ${p.title}`);
    setTimeout(() => { $("#market-dl").hidden = true; }, 4000);
    renderModels(true);
  } else if (p.error) {
    $("#market-dl-label").textContent = `${p.title || "download"} — ${p.error}`;
    toast(p.error, "err");
  } else { $("#market-dl").hidden = true; }
}
$("#market-dl-cancel").onclick = async () => {
  try { await post("/api/market/cancel", {}); toast("download cancelling…"); } catch (e) { toast(e.message, "err"); }
};

/* --------------------------------------------------------- backend panel */
const BACKEND_INFO = {
  ollama: "Local Ollama server — GGUF models with GPU offload.",
  lmstudio: "LM Studio desktop app — local OpenAI-compatible server.",
  mock: "Built-in scripted backend for demos and tests. No GPU, no network.",
  cloud: "Cloud providers (OpenRouter, OpenAI, Anthropic, …) — connect a key in Providers.",
};
async function renderBackends() {
  const wrap = $("#backend-cards"); wrap.innerHTML = '<p class="muted">probing backends…</p>';
  let data = {};
  try { data = await get("/api/backends"); } catch (e) { toast(e.message, "err"); }
  wrap.innerHTML = "";
  const cur = state.status ? state.status.backend : "";
  ["ollama", "lmstudio", "mock", "cloud"].forEach((name) => {
    const b = data[name] || {};
    const card = document.createElement("div"); card.className = "be-card-ui" + (name === cur ? " sel" : "");
    card.innerHTML = `<h3><span class="pulse ${b.running ? "on" : "off"}"></span>${esc(name)}${name === cur ? ' <span class="pb active">default</span>' : ""}</h3>
      <div class="be-row">${esc(BACKEND_INFO[name])}</div>
      <div class="be-row mono small">${b.installed ? "installed" : "not installed"} · ${b.running ? "running" : "stopped"}${b.health ? " · " + esc(b.health) : ""}</div>
      <div class="be-acts"></div>`;
    const acts = card.querySelector(".be-acts");
    const set = document.createElement("button"); set.type = "button"; set.className = "btn small";
    set.textContent = "Set default"; set.disabled = name === cur;
    set.onclick = async () => {
      try { await post("/api/backend", { name }); toast(`backend: ${name}`); await refreshStatus(); renderBackends(); }
      catch (e) { toast(e.message, "err"); }
    };
    const start = document.createElement("button"); start.type = "button"; start.className = "btn small";
    start.textContent = b.running ? "Check" : "Start";
    start.onclick = async () => {
      start.disabled = true;
      try {
        const r = await post("/api/backend", { name, start: true });
        const ins = $("#backend-instructions");
        if (r.instructions) { ins.hidden = false; ins.textContent = r.instructions; } else ins.hidden = true;
        toast(r.message || (r.ok ? `${name} ready` : `${name} not ready`), r.ok ? "" : "warn");
        renderBackends();
      } catch (e) { toast(e.message, "err"); }
      finally { start.disabled = false; }
    };
    acts.append(set, start);
    wrap.appendChild(card);
  });
}

/* ------------------------------------------------------------ soul panel */
let soulSaved = "";
async function renderSoul() {
  if (!state.config) { try { state.config = await get("/api/config"); } catch (e) { /* keep null */ } }
  let s;
  try { s = await get("/api/soul"); } catch (e) { toast(e.message, "err"); return; }
  $("#soul-path").textContent = s.path || "";
  $("#mem-writes").checked = !!(state.config && state.config.memory && state.config.memory.agent_writes);
  if (!state.soulDirty) { $("#soul-editor").value = s.text || ""; soulSaved = s.text || ""; }
  const rec = $("#soul-recent"); rec.innerHTML = "";
  (s.recent || []).slice(0, 8).forEach((r) => {
    const div = document.createElement("div");
    const op = r.op || r.action || r.kind || "change";
    const txt = r.text || r.note || r.summary || (r.section ? "section: " + r.section : JSON.stringify(r));
    const b = document.createElement("b"); b.textContent = op;
    div.append(b, document.createTextNode(" " + String(txt).slice(0, 160)));
    rec.appendChild(div);
  });
  if (!(s.recent || []).length) rec.innerHTML = '<span class="muted">no memory changes yet</span>';
}
$("#soul-editor").addEventListener("input", () => { state.soulDirty = $("#soul-editor").value !== soulSaved; });
$("#soul-save").onclick = async () => {
  const text = $("#soul-editor").value;
  try {
    await post("/api/soul", { action: "write", text });
    soulSaved = text; state.soulDirty = false;
    toast("soul.md saved"); renderSoul();
  } catch (e) { toast(`save failed — edits kept: ${e.message}`, "err"); }
};
$("#soul-revert").onclick = async () => {
  if (!await confirmModal("Revert soul.md?", "Restores the latest backup. Current unsaved edits are discarded.", "Revert")) return;
  try {
    const r = await post("/api/soul", { action: "revert" });
    $("#soul-editor").value = r.text || ""; soulSaved = r.text || ""; state.soulDirty = false;
    toast("reverted to backup"); renderSoul();
  } catch (e) { toast(e.message, "err"); }
};
$("#soul-clear").onclick = async () => {
  if (!await confirmModal("Clear soul.md?", "All durable memories are removed (a backup is kept).", "Clear")) return;
  try {
    const r = await post("/api/soul", { action: "clear" });
    $("#soul-editor").value = r.text || ""; soulSaved = r.text || ""; state.soulDirty = false;
    toast("soul.md cleared"); renderSoul();
  } catch (e) { toast(e.message, "err"); }
};
$("#mem-writes").onchange = async () => {
  const want = $("#mem-writes").checked;
  try {
    await post("/api/config/memory", { agent_writes: want });
    state.config = await get("/api/config");
    toast(`agent memory writes: ${want ? "on" : "off"}`);
  } catch (e) { $("#mem-writes").checked = !want; toast(e.message, "err"); }
};

/* ---------------------------------------------------------- doctor panel */
async function renderDoctor() {
  const list = $("#doctor-list"); list.innerHTML = '<li class="muted">running checks…</li>';
  let d;
  try { d = await get("/api/doctor"); } catch (e) { list.innerHTML = ""; toast(e.message, "err"); return; }
  list.innerHTML = "";
  (d.checks || []).forEach((c) => {
    const li = document.createElement("li");
    const st = c.status === "info" ? "skip" : c.status;
    li.innerHTML = `<span class="st ${esc(st)}">${esc(c.status)}</span><span class="lbl"></span>${c.fix ? '<span class="hint"></span>' : ""}`;
    li.querySelector(".lbl").textContent = c.label + (c.detail ? " — " + c.detail : "");
    if (c.fix) li.querySelector(".hint").textContent = c.fix;
    list.appendChild(li);
  });
  const sum = Object.entries(d.summary || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
  if (sum) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="st skip">sum</span><span class="lbl"></span>';
    li.querySelector(".lbl").textContent = sum;
    list.appendChild(li);
  }
}
$("#doctor-run").onclick = () => renderDoctor();

/* -------------------------------------------------------- settings panel */
/* Editable: generation / agent / voice / ui only — the bridge 403s any
   attempt to PATCH protected security sections, and the UI never tries. */
const SET_FIELDS = {
  generation: [
    ["temperature", "num", "0–2 · higher = more random"],
    ["top_p", "num", "nucleus sampling cutoff (0–1)"],
    ["top_k", "int", "top-k sampling"],
    ["repeat_penalty", "num", "repetition penalty (0–2)"],
    ["num_ctx", "int", "context window (tokens)"],
    ["num_gpu", "int", "GPU layers (−1 = all)"],
    ["num_batch", "int", "batch size"],
    ["max_tokens", "int", "max reply tokens"],
    ["seed", "int", "RNG seed (0 = random)"],
    ["threads", "int", "CPU threads (0 = auto)"],
    ["timeout", "num", "request timeout (seconds)"],
    ["streaming", "bool", "stream tokens as they arrive"],
    ["effort", "enum:off,low,medium,high,xhigh,max", "reasoning effort ladder"],
    ["ultrathink", "bool", "deep multi-pass reasoning"],
    ["thinking_budget", "int", "reasoning token cap (0 = server default)"],
    ["kv_cache", "enum:f16,bf16,q8_0,q4_0,q4_1,iq4_nl,fp8,fp8_e4m3,auto", "K/V cache quantization"],
  ],
  agent: [
    ["max_steps", "int", "tool steps per turn (1–64)"],
    ["max_parse_retries", "int", "retries on malformed tool JSON (0–5)"],
    ["tool_timeout", "num", "per-tool timeout (seconds)"],
    ["native_tools", "enum:auto,force,off", "native tool-calling API"],
    ["react_fallback", "bool", "ReAct fallback for weak models"],
    ["compact", "bool", "auto-compact long chats"],
    ["compact_ratio", "num", "compact above this ctx ratio (0.2–0.95)"],
    ["thinking_visible", "bool", "show reasoning text in chat"],
    ["agent_loop", "bool", "relentless completion audit"],
    ["tone", "text", "default · ruthless · chill · friendly · professional · terse"],
    ["op_mode", "enum:chat,plan,build", "chat/plan block writes & shell · build = full agent"],
  ],
  voice: [
    ["language", "text", "STT language hint (auto = detect)"],
    ["engine", "enum:auto,sensevoice,parakeet,whisper,whisperx", "STT engine override"],
  ],
  ui: [
    ["theme", "text", "stored preference — this console ships dark only"],
    ["timestamps", "bool", "show message timestamps"],
    ["show_tool_panels", "bool", "expand tool panels by default"],
    ["confirm_quit", "bool", "confirm before shutting down"],
  ],
};
const RO_ROWS = [
  ["permission_mode", "Permission mode", "read-only · change via the mode badge (Ctrl+P), phrase-gated"],
  ["workspace", "Workspace", "read-only · set at launch"],
  ["generation.thinking", "Thinking (derived)", "read-only · mirrors effort ≠ off"],
  ["memory.enabled", "Memory enabled", "read-only · dedicated memory control"],
  ["memory.agent_writes", "Agent memory writes", "read-only · toggle in the Memory view"],
  ["memory.require_explicit_save", "Explicit-save only", "read-only · dedicated memory control"],
  ["safety.deny", "Deny globs (hard floor)", "read-only · edit config.toml — wins in every mode"],
  ["safety.command_denylist", "Command blocklist", "read-only · edit config.toml"],
  ["safety.catastrophic_override", "Catastrophic override", "read-only · edit config.toml"],
  ["guard.enabled", "Guard (System-1 flagger)", "read-only · edit config.toml"],
  ["ollama.host", "Ollama host", "read-only · edit config.toml"],
  ["lmstudio.base_url", "LM Studio URL", "read-only · edit config.toml"],
  ["mail.address", "Mail address", "read-only · oml mail setup"],
  ["mail.password", "Mail password (masked)", "read-only · oml mail setup"],
];
let settingsSnapshot = null;
function cfgGet(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function settingsValues() {
  const out = {};
  $$("#settings-form [data-key]").forEach((el) => { out[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value; });
  return out;
}
function settingsDirtyNow() {
  if (!settingsSnapshot) return false;
  const v = settingsValues();
  return Object.keys(settingsSnapshot).some((k) => String(v[k]) !== String(settingsSnapshot[k]));
}
function renderSettings() {
  // dirty edits survive navigation — never rebuild over them
  if (state.settingsDirty && $("#settings-form").children.length) return;
  const cfg = state.config;
  const form = $("#settings-form"); form.innerHTML = "";
  if (!cfg) { form.innerHTML = '<p class="muted">config unavailable — is the bridge connected?</p>'; return; }
  const snap = {};
  Object.entries(SET_FIELDS).forEach(([section, fields]) => {
    const sec = document.createElement("div"); sec.className = "set-row section";
    sec.innerHTML = `<label>${esc(section)}</label><span></span>`;
    form.appendChild(sec);
    fields.forEach(([key, type, hint]) => {
      const path = `${section}.${key}`;
      const val = cfgGet(cfg, path);
      const row = document.createElement("div"); row.className = "set-row";
      const id = `set-${section}-${key}`;
      row.innerHTML = `<label for="${id}">${esc(key)}<small class="muted">${esc(hint)}</small></label>`;
      let el, holder = null;
      if (type === "bool") { el = document.createElement("input"); el.type = "checkbox"; el.checked = !!val; }
      else if (type.startsWith("enum:")) {
        el = document.createElement("om-listbox");
        el.className = "dd";
        el.setOptions(type.slice(5).split(",").map((o) => [o, o]));
        el.value = String(val == null ? "" : val);
        el.setAttribute("aria-label", key);
      } else {
        el = document.createElement("input");
        el.type = type === "text" ? "text" : "number";
        if (type === "num") el.step = "any";
        el.value = val == null ? "" : String(val);
        if (el.type === "number") holder = makeStepper(el);
      }
      el.id = id; el.dataset.key = path; el.dataset.type = type;
      el.addEventListener("input", () => { state.settingsDirty = settingsDirtyNow(); });
      el.addEventListener("change", () => { state.settingsDirty = settingsDirtyNow(); });
      row.appendChild(holder || el);
      form.appendChild(row);
      snap[path] = el.type === "checkbox" ? el.checked : el.value;
    });
  });
  const sec = document.createElement("div"); sec.className = "set-row section";
  sec.innerHTML = "<label>read-only · dedicated controls</label><span></span>";
  form.appendChild(sec);
  RO_ROWS.forEach(([path, label, hint]) => {
    let val = cfgGet(cfg, path);
    if (Array.isArray(val)) val = val.join(", ") || "(none)";
    else if (val && typeof val === "object") val = JSON.stringify(val);
    const row = document.createElement("div"); row.className = "set-row";
    const id = `ro-${path.replace(/\./g, "-")}`;
    row.innerHTML = `<label for="${id}">${esc(label)}<small class="muted">${esc(hint)}</small></label>`;
    const el = document.createElement("input"); el.type = "text"; el.id = id; el.disabled = true;
    el.value = val == null ? "" : String(val);
    el.setAttribute("aria-label", `${label} (read-only)`);
    row.appendChild(el); form.appendChild(row);
  });
  settingsSnapshot = snap;
  state.settingsDirty = false;
}
$("#settings-save").onclick = async () => {
  const values = settingsValues();
  const body = {};
  for (const [path, raw] of Object.entries(values)) {
    const [section, key] = path.split(".");
    const el = $(`#settings-form [data-key="${path}"]`);
    const type = el ? el.dataset.type : "text";
    let v = raw;
    if (type === "bool") v = !!el.checked;
    else if (type === "int") { v = parseInt(raw, 10); if (!Number.isFinite(v)) { toast(`${key}: enter an integer`, "err"); return; } }
    else if (type === "num") { v = parseFloat(raw); if (!Number.isFinite(v)) { toast(`${key}: enter a number`, "err"); return; } }
    (body[section] = body[section] || {})[key] = v;
  }
  const btn = $("#settings-save"); btn.disabled = true;
  try {
    const r = await patch("/api/config", body);
    state.config = r.config || state.config;
    settingsSnapshot = { ...values };
    // edits typed DURING the save are still dirty — never clobber them
    state.settingsDirty = settingsDirtyNow();
    toast("settings saved");
    if (!state.settingsDirty) renderSettings();
    refreshStatus();
  } catch (e) { toast(`save failed — edits kept: ${e.message}`, "err"); }
  finally { btn.disabled = false; }
};

/* ------------------------------------------------------------ help panel */
function renderHelp() {
  const keys = [
    ["Enter", "send · Shift+Enter = newline"],
    ["Esc", "close hints / modal · stop the running turn"],
    ["Ctrl+K", "command palette"],
    ["Ctrl+N", "new chat"],
    ["Ctrl+P", "permission mode"],
    ["Ctrl+M", "models"],
    ["Ctrl+B", "backend"],
    ["Ctrl+S", "memory · soul.md"],
    ["Ctrl+D", "doctor"],
    ["Ctrl+G", "settings"],
    ["Ctrl+H", "this help"],
    ["Ctrl+O", "focus chat search"],
    ["Ctrl+E (hold)", "voice input — release to transcribe"],
    ["Ctrl+Enter", "save soul.md (Memory view)"],
    ["Tab / ↑ ↓", "complete slash & @ hints"],
  ];
  const refs = [
    ["@file:path", "attach a file's contents as context"],
    ["@folder:path", "attach a folder listing"],
    ["@diff", "working-tree changes"],
    ["@staged", "staged changes"],
    ["@git:ref", "git ref / log context"],
  ];
  $("#help-body").innerHTML = `
    <h3>Keyboard</h3>
    <table>${keys.map(([k, d]) => `<tr><td><kbd>${esc(k)}</kbd></td><td>${esc(d)}</td></tr>`).join("")}</table>
    <h3>Context references</h3>
    <table>${refs.map(([k, d]) => `<tr><td><code>${esc(k)}</code></td><td>${esc(d)}</td></tr>`).join("")}</table>
    <h3>Slash commands</h3>
    <table>${SLASHES.map((s) => `<tr><td><code>${esc(s.cmd)}</code></td><td>${esc(s.desc)}</td></tr>`).join("")}</table>
    <h3>Operation modes</h3>
    <p>The composer's <b>chat / plan / build</b> selector is separate from the permission mode: chat and plan block write and shell tools entirely; build is the full agent.</p>
    <h3>Steering a running turn</h3>
    <p>While a turn is busy, sending a message queues it as guidance (<code>/api/steer</code>) — a chip shows the queue position. <code>/steer note</code> does the same. If no turn is active, the message is sent normally and you get a notice. Steering never changes mode, tools, or approvals.</p>
    <h3>Permission modes</h3>
    <table>${Object.entries(MODE_DESC).map(([m, d]) => `<tr><td><b>${esc(m)}</b></td><td>${esc(d)}</td></tr>`).join("")}</table>
    <h3>Shell consent</h3>
    <p><code>run_command</code> approvals offer <b>Allow once</b> or <b>Deny</b> only — never session-wide, never permanent. In sandboxed and dangerous modes every shell command asks fresh. In full_access, routine commands are auto-approved by policy (the mode itself requires typing the exact confirmation phrase); catastrophic commands stay blocked unless <code>safety.catastrophic_override</code> is set. <code>safety.deny</code> globs are a hard floor in every mode — a matched rule is named on the approval card. Expired approvals render as <b>expired — denied</b> (fail-closed).</p>
    <h3>Where your data lives</h3>
    <p>The bridge binds 127.0.0.1 only. The GUI capability token arrives in the URL fragment, is erased immediately, and never leaves this machine. Chats, soul.md, the audit log, and config live under your local OMLLM home with owner-only permissions. Cloud providers are contacted only when you connect a key and select one; keys are stored locally in config.toml and shown masked. This console loads no CDNs, no external fonts, and no telemetry — everything is offline.</p>`;
}/* ------------------------------------------------------- command palette */
const PALETTE = [
  ...SLASHES.map((s) => ({ label: s.cmd, hint: s.desc, cmd: s.cmd, arg: !!s.arg })),
  { label: "view: chat", hint: "back to the conversation", view: "chat" },
  { label: "view: models", hint: "browse & pull models", view: "models" },
  { label: "view: providers", hint: "connect cloud providers", view: "providers" },
  { label: "view: market", hint: "model market", view: "market" },
  { label: "view: backend", hint: "backend status", view: "backend" },
  { label: "view: memory", hint: "soul.md editor", view: "memory" },
  { label: "view: doctor", hint: "health checks", view: "doctor" },
  { label: "view: settings", hint: "generation / agent / voice / ui", view: "settings" },
  { label: "view: help", hint: "keyboard & security notes", view: "help" },
];
let palSel = 0, palMatches = [];
function openPalette() {
  $("#palette-input").value = "";
  paintPalette("");
  openModal("#palette-modal", "#palette-input");
}
function paintPalette(q) {
  const needle = q.trim().toLowerCase();
  palMatches = PALETTE.filter((c) => !needle || c.label.toLowerCase().includes(needle) || (c.hint || "").toLowerCase().includes(needle));
  palSel = 0;
  const ul = $("#palette-list"); ul.innerHTML = "";
  palMatches.forEach((c, i) => {
    const li = document.createElement("li");
    li.className = i === palSel ? "sel" : "";
    li.innerHTML = `<code class="pal-cmd">${esc(c.label)}</code><small>${esc(c.hint || "")}</small>`;
    li.onclick = () => runPalette(c);
    ul.appendChild(li);
  });
  if (!palMatches.length) ul.innerHTML = '<li class="pal-empty">no matching command</li>';
}
function repaintPaletteSel() {
  $$("#palette-list li").forEach((li, i) => li.classList.toggle("sel", i === palSel));
  const sel = $("#palette-list li.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
function runPalette(c) {
  closeModal("#palette-modal");
  if (c.view) { switchView(c.view); return; }
  input.value = c.cmd + (c.arg ? " " : "");
  autoGrow();
  if (c.arg) { input.focus(); computeHints(); }
  else submit();
}
$("#palette-input").addEventListener("input", () => paintPalette($("#palette-input").value));
$("#palette-input").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return; // IME guard
  if (e.key === "ArrowDown") { e.preventDefault(); palSel = Math.min(palSel + 1, Math.max(0, palMatches.length - 1)); repaintPaletteSel(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palSel = Math.max(palSel - 1, 0); repaintPaletteSel(); }
  else if (e.key === "Enter") { e.preventDefault(); if (palMatches[palSel]) runPalette(palMatches[palSel]); }
});

/* ----------------------------------------------------------------- voice */
let talking = false, talkTimer = null;
async function talkStart() {
  if (talking) return;
  talking = true;
  input.classList.add("mic-live");
  $("#run-state").textContent = "Listening… release Ctrl+E";
  try { await post("/api/voice/record", { action: "start" }); }
  catch (e) {
    talking = false; input.classList.remove("mic-live");
    toast(e.message, "err");
    $("#run-state").textContent = state.streaming ? "Working" : "Ready";
    return;
  }
  talkTimer = setTimeout(() => { if (talking) talkStop(); }, 30000);
}
async function talkStop() {
  if (!talking) return;
  talking = false; clearTimeout(talkTimer);
  input.classList.remove("mic-live");
  $("#run-state").textContent = "Transcribing…";
  try {
    const r = await post("/api/voice/record", { action: "stop" });
    if (r.text) {
      input.value += (input.value && !input.value.endsWith(" ") ? " " : "") + r.text;
      autoGrow(); computeHints(); input.focus();
      state.drafts.set(state.session, input.value);
    } else if (r.note) toast(r.note, "warn");
  } catch (e) { toast(e.message, "err"); }
  $("#run-state").textContent = state.streaming ? "Working" : "Ready";
}

/* ------------------------------------------------------------ global keys */
document.addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return; // IME guard on every hotkey path
  if (modalStack.length) return; // the modal capture handler owns keys while stacked
  if (e.defaultPrevented) return;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "e") { e.preventDefault(); if (!e.repeat) talkStart(); return; }
  if (!mod) {
    if (e.key === "Escape") {
      if (!$("#plus-menu").hidden) { closeMenus(); return; }
      if (!slashHint.hidden || !atHint.hidden) { hideHints(); return; }
      if (state.streaming && !state.stopping) { e.preventDefault(); $("#cancel").click(); }
    }
    return;
  }
  const k = e.key.toLowerCase();
  const views = { m: "models", b: "backend", s: "memory", d: "doctor", g: "settings", h: "help" };
  if (views[k]) { e.preventDefault(); switchView(views[k]); }
  else if (k === "p") { e.preventDefault(); setModeUi(null); }
  else if (k === "n") { e.preventDefault(); newChat(); }
  else if (k === "k") { e.preventDefault(); openPalette(); }
  else if (k === "q") { e.preventDefault(); handleSlash("/quit"); }
  else if (k === "o") { e.preventDefault(); openSidebarNarrow(); $("#sb-search").focus(); }
  else if (k === "enter" && state.view === "memory") { e.preventDefault(); $("#soul-save").click(); }
});
document.addEventListener("keyup", (e) => {
  if (talking && (e.key === "Control" || e.key === "Meta" || e.key.toLowerCase() === "e")) talkStop();
});
window.addEventListener("blur", () => { if (talking) talkStop(); });

/* leaving with unsaved work warns — drafts, dirty editors, running turn */
window.addEventListener("beforeunload", (e) => {
  const hasDraft = input.value.trim() || Array.from(state.drafts.values()).some((v) => v && v.trim());
  if (state.soulDirty || state.settingsDirty || state.streaming || hasDraft) { e.preventDefault(); e.returnValue = ""; }
});

/* ----------------------------------------------------------------- menus */
function closeMenus() {
  $("#plus-menu").hidden = true;
  $("#btn-plus").setAttribute("aria-expanded", "false");
}
function toggleMenu(btnSel, menuSel) {
  const m = $(menuSel), wasHidden = m.hidden;
  closeMenus();
  m.hidden = !wasHidden;
  $(btnSel).setAttribute("aria-expanded", String(wasHidden));
}
$("#btn-plus").onclick = (e) => { e.stopPropagation(); toggleMenu("#btn-plus", "#plus-menu"); };
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) closeMenus(); });
/* persistent left rail replaces the old popup view menu (task #29) */
$("#rail").addEventListener("click", (e) => {
  const b = e.target.closest("[data-view]");
  if (b) switchView(b.dataset.view);
});
$("#rail-palette").onclick = () => openPalette();
$("#plus-menu").addEventListener("click", (e) => {
  const b = e.target.closest("[data-ins]");
  if (b) {
    closeMenus();
    input.value += b.dataset.ins;
    input.focus(); autoGrow(); computeHints();
    state.drafts.set(state.session, input.value);
  }
});
$("#plus-skills").onclick = async () => {
  closeMenus();
  try {
    const r = await get("/api/skills");
    const names = (r.skills || []).map((s) => s.name);
    if (!names.length) { toast("no skills saved yet", "warn"); return; }
    const name = await promptModal("Cast a skill", names.join(", "), false, names[0]);
    if (name) { input.value = `/skill:${name}`; autoGrow(); submit(); }
  } catch (e) { toast(e.message, "err"); }
};

/* -------------------------------------------------------- sidebar toggle */
const narrowMQ = window.matchMedia("(max-width: 900px)");
function sidebarIsOpen() {
  const sb = $("#sidebar");
  return narrowMQ.matches ? sb.classList.contains("open") : !sb.classList.contains("closed");
}
function setSidebar(open) {
  const sb = $("#sidebar");
  if (narrowMQ.matches) { sb.classList.toggle("open", open); sb.classList.remove("closed"); }
  else { sb.classList.toggle("closed", !open); sb.classList.remove("open"); }
  $("#btn-sidebar").setAttribute("aria-pressed", String(open));
}
function closeSidebarNarrow() { if (narrowMQ.matches) setSidebar(false); }
function openSidebarNarrow() { if (narrowMQ.matches) setSidebar(true); }
$("#btn-sidebar").onclick = () => setSidebar(!sidebarIsOpen());
narrowMQ.addEventListener("change", () => setSidebar(!narrowMQ.matches));

/* ------------------------------------------------------ composer selectors */
$("#sel-backend").onchange = async () => {
  const name = $("#sel-backend").value;
  try { await post("/api/backend", { name }); toast(`backend: ${name}`); await refreshStatus(); loadModels(name, null, true); }
  catch (e) { toast(e.message, "err"); refreshStatus(); }
};
$("#sel-model").onchange = async () => {
  const model = $("#sel-model").value; if (!model) return;
  const backend = $("#sel-backend").value || (state.status && state.status.backend);
  try { await post("/api/model", { backend, model }); toast(`model: ${model}`); refreshStatus(); }
  catch (e) { toast(e.message, "err"); }
};
$("#sel-effort").onchange = async () => {
  const effort = $("#sel-effort").value;
  try { await patch("/api/config", { generation: { effort } }); toast(`effort: ${effort}`); }
  catch (e) { toast(e.message, "err"); refreshStatus(); }
};
$("#sel-opmode").onchange = () => setOpMode($("#sel-opmode").value);
$("#chip-mode").onclick = () => setModeUi(null);
$("#ctx-badge").onclick = () => switchView("settings");
$("#btn-new").onclick = () => newChat();
$("#boot-retry").onclick = async () => {
  $("#boot-error").hidden = true;
  $("#run-state").textContent = "Connecting…";
  await refreshStatus();
  if (state.connected) renderSidebar();
};

/* ------------------------------------------------------------------ boot */
async function boot() {
  // prompt/confirm cancel buttons participate in Escape/scrim dismissal
  $("#pm-cancel").classList.add("modal-cancel");
  $("#cf-cancel").classList.add("modal-cancel");
  // static listbox option sets (the hosts ship empty in index.html)
  $("#sel-opmode").setOptions([["chat", "chat"], ["plan", "plan"], ["build", "build"]]);
  $("#sel-effort").setOptions(["off", "low", "medium", "high", "xhigh", "max"].map((v) => [v, v]));
  $("#model-sort").setOptions(["name", "size", "params", "quant", "family", "thinking", "vision"].map((v) => [v, v]));
  $$(".rail-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === "chat"));
  setSidebar(!narrowMQ.matches);
  await refreshStatus();
  await renderSidebar();
  const active = state.status && state.status.session;
  if (active && state.chats.some((c) => c.id === active)) { addTab(active); await loadSessionIntoView(active); }
  else emptyState();
  setInterval(refreshStatus, 5000);
  setInterval(pollState, 2000);
  pollState();
  input.focus();
}
boot();