// Build view (#113): two 13x13 range editors (OOP left, IP right) painted with a weight slider,
// a card-button board picker, the bet-size form, and the solve panel with the convergence chart.

import { H, RANKS, SUITS, GLYPH, el, cardEl } from "./cards.js";
import { parseRange, formatRange, rangeStats } from "./ranges.js";
import { get, post, toast, fail, prefs, fmt, fmtChips } from "./api.js";
import { drawChart } from "./chart.js";
import { openLibrary } from "./library.js";
import { STREETS, renderSizes, readSizes, validateSizes as validateSizeTable } from "./sizes.js";

const $ = (id) => document.getElementById(id);

const DEFAULTS = {
  OOP: "AA,KK,QQ,JJ,TT,99,88,AK,AQ,AJ,AT,KQ,KJ,QJ,JT,T9s,98s,87s,76s,A5s,A4s",
  IP: "AA,KK,QQ,JJ,TT,99,88,77,66,AK,AQ,AJ,AT,A9s,A8s,KQ,KJ,KTs,QJ,QTs,JTs,T9s,98s,87s",
  board: ["Qs", "8d", "7s"],
};

let weight = 1;          // current paint weight (0..1)
const editors = {};
let board = [];
let builtSig = null;     // config signature of the tree currently in the solver
let onBuilt = () => {};
let spot = { name: null, sig: null };  // the library spot the form came from (#116)

/* ------------------------------------------------------------------ range editor */

class RangeEditor {
  constructor(root, player, onChange) {
    this.player = player;
    this.onChange = onChange;
    this.w = new Float32Array(1326);
    root.textContent = "";

    const head = el("div", "editor-head");
    head.append(el("span", `badge ${player.toLowerCase()}`, player), el("h2", null, player === "OOP" ? "Out of position" : "In position"));
    this.stats = el("div", "stats");
    head.appendChild(this.stats);
    const tools = el("div", "editor-tools");
    const tb = (label, title, fn) => { const b = el("button", "btn small ghost", label); b.title = title; b.onclick = fn; tools.appendChild(b); };
    tb("Fill", "Set every hand to the click weight", () => { this.w.fill(weight); this.changed(); });
    tb("Clear", "Remove every hand", () => { this.w.fill(0); this.changed(); });
    tb("Copy", "Copy the range text", () => {
      navigator.clipboard && navigator.clipboard.writeText(this.text.value).then(() => toast("Range copied"), () => {});
    });
    tb("Load…", "Load a saved range", () => openLibrary({
      kind: "range", mode: "load", onLoad: (text, name) => { this.setText(text.trim()); this.source = name; toast(`Loaded “${name}” into ${player}`); },
    }));
    tb("Save…", "Save this range to the library", () => openLibrary({
      kind: "range", mode: "save", content: this.value(), suggest: this.source || "",
      onLoad: (_, name) => { this.source = name; },
    }));
    root.appendChild(head);

    this.grid = el("div", "rgrid");
    this.cells = H.classes.map((cls) => {
      const c = el("div", "cell");
      const fill = el("div", "fill"), lbl = el("div", "lbl", cls), val = el("div", "val");
      fill.appendChild(el("span"));
      fill.firstChild.style.background = `var(--${player.toLowerCase()})`;
      fill.firstChild.style.flexGrow = 1;
      c.append(fill, lbl, val);
      c.dataset.cls = cls;
      this.grid.appendChild(c);
      return { cls, c, fill, val };
    });
    // paint: press sets the target (toggle off if the cell already has the click weight), drag applies it
    let target = null;
    const apply = (cls) => { for (const i of H.byClass[cls]) this.w[i] = target; this.changed(true); };
    this.grid.addEventListener("pointerdown", (e) => {
      const c = e.target.closest(".cell");
      if (!c) return;
      e.preventDefault();
      const cls = c.dataset.cls;
      const cur = this.classAvg(cls);
      target = Math.abs(cur - weight) < 1e-4 && weight > 0 ? 0 : weight;
      apply(cls);
      this.grid.setPointerCapture(e.pointerId);
    });
    this.grid.addEventListener("pointermove", (e) => {
      if (target == null) return;
      const c = document.elementFromPoint(e.clientX, e.clientY);
      const cell = c && c.closest && c.closest(".rgrid .cell");
      if (cell && cell.parentElement === this.grid && Math.abs(this.classAvg(cell.dataset.cls) - target) > 1e-4) apply(cell.dataset.cls);
    });
    const end = () => { if (target != null) { target = null; this.changed(); } };
    this.grid.addEventListener("pointerup", end);
    this.grid.addEventListener("pointercancel", end);
    root.appendChild(this.grid);

    this.text = el("textarea", "rtext");
    this.text.spellcheck = false;
    this.text.placeholder = "AA,KK,AKs,AQo:0.5,…";
    this.err = el("div", "rerr");
    let t = null;
    this.text.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => this.setText(this.text.value, false), 250);
    });
    const foot = el("div", "editor-foot");
    foot.append(tools, this.err);
    root.append(this.text, foot);
  }

  classAvg(cls) {
    const idx = H.byClass[cls];
    let s = 0;
    for (const i of idx) s += this.w[i];
    return s / idx.length;
  }

  setText(text, rewrite = true) {
    try {
      this.w = parseRange(text);
      this.text.classList.remove("invalid");
      this.err.textContent = "";
      this.draw();
      if (rewrite) this.text.value = formatRange(this.w);
      this.onChange();
    } catch (e) {
      this.text.classList.add("invalid");
      this.err.textContent = e.message;
    }
  }

  changed(live = false) {
    this.draw();
    this.text.value = formatRange(this.w);
    this.text.classList.remove("invalid");
    this.err.textContent = "";
    if (!live) this.onChange();
  }

  draw() {
    for (const x of this.cells) {
      const a = this.classAvg(x.cls);
      x.fill.style.height = (a * 100).toFixed(1) + "%";
      x.c.classList.toggle("on-fill", a > 0.62);
      x.c.classList.toggle("blank", a === 0);
      x.val.textContent = a > 0 && a < 0.995 ? Math.round(a * 100) : "";
    }
    const st = rangeStats(this.w);
    this.stats.innerHTML = `<span><b>${fmt(st.combos, 0)}</b> combos</span><span><b>${fmt(st.pct)}%</b></span>`;
  }

  value() { return formatRange(this.w); }
}

/* ------------------------------------------------------------------ board picker */

function drawBoard() {
  const slots = $("board-slots");
  slots.textContent = "";
  for (let i = 0; i < 5; i++) {
    if (i === 3 || i === 4) slots.appendChild(el("span", "sep"));
    if (board[i]) {
      const c = cardEl(board[i], "lg");
      c.title = "Remove " + board[i];
      c.onclick = () => { board.splice(i, 1); boardChanged(); };
      slots.appendChild(c);
    } else {
      const ph = el("span", "pc lg placeholder");
      ph.appendChild(el("span", "r", i < 3 ? "F" : i === 3 ? "T" : "R"));
      ph.title = i < 3 ? "flop" : i === 3 ? "turn (optional)" : "river (optional)";
      slots.appendChild(ph);
    }
  }
  document.querySelectorAll("#deck button").forEach((b) => {
    const picked = board.includes(b.dataset.card);
    b.classList.toggle("picked", picked);
    b.classList.toggle("used", !picked && board.length >= 5);
  });
}

function buildDeck() {
  const deck = $("deck");
  deck.textContent = "";
  for (const s of SUITS) for (const r of RANKS) {
    const card = r + s;
    const b = el("button");
    b.dataset.card = card;
    b.style.background = `var(--suit-${s})`;
    b.append(el("span", null, r), el("small", null, GLYPH[s]));
    b.title = card;
    b.onclick = () => {
      const i = board.indexOf(card);
      if (i >= 0) board.splice(i, 1);
      else if (board.length < 5) board.push(card);
      boardChanged();
    };
    deck.appendChild(b);
  }
}

function boardChanged() { drawBoard(); saveForm(); }

/* ------------------------------------------------------------------ form */

const sizesBody = () => $("sizes-body");

function readForm() {
  const f = {
    range_oop: editors.OOP.value(), range_ip: editors.IP.value(), board: board.join(""),
    pot: Number($("f-pot").value), stack: Number($("f-stack").value),
    allin_threshold: Number($("f-allin-threshold").value), add_allin_limit: Number($("f-allin-limit").value),
  };
  return Object.assign(f, readSizes(sizesBody()));
}

function saveForm() {
  const f = readForm();
  prefs.set("build", f);
  updateSpr();
  markDirty();
  markSpot();
}

function loadForm() {
  applyForm(prefs.get("build", null));
  const sp = prefs.get("spot", null);
  if (sp && sp.sig === sig(spotForm())) spot = sp;
  markSpot();
}

/** Put a saved config (or null for the defaults) into the form. */
function applyForm(f) {
  editors.OOP.setText(f && f.range_oop != null ? f.range_oop : DEFAULTS.OOP);
  editors.IP.setText(f && f.range_ip != null ? f.range_ip : DEFAULTS.IP);
  board = f && typeof f.board === "string" ? f.board.match(/../g) || [] : [...DEFAULTS.board];
  if (f && f.accuracy != null && Number.isFinite(Number(f.accuracy))) $("f-accuracy").value = f.accuracy;
  if (f) {
    const set = (id, v) => { if (v != null && Number.isFinite(v)) $(id).value = v; };
    set("f-pot", f.pot); set("f-stack", f.stack);
    set("f-allin-threshold", f.allin_threshold); set("f-allin-limit", f.add_allin_limit);
  }
  renderSizes(sizesBody(), STREETS, f || {});
  drawBoard();
  updateSpr();
}

function updateSpr() {
  const p = Number($("f-pot").value), s = Number($("f-stack").value);
  $("f-spr").textContent = p > 0 && s > 0 ? (s / p).toFixed(2) : "—";
}

function validateSizes() { return validateSizeTable(sizesBody()); }

const sig = (f) => JSON.stringify(f);

/* ------------------------------------------------------------------ spots (library configs, #116) */

function spotContent() { return { schema: 1, ...readForm(), accuracy: Number($("f-accuracy").value) }; }
// Spot "edited" state also tracks the target accuracy, which is not part of the tree (review of #127).
function spotForm() { return { ...readForm(), accuracy: $("f-accuracy").value }; }

function markSpot() {
  $("spot-name").textContent = spot.name || "Unsaved";
  $("spot-dirty").classList.toggle("hidden", !spot.name || spot.sig === sig(spotForm()));
}

function setSpot(name) {
  spot = { name, sig: sig(spotForm()) };
  prefs.set("spot", spot);
  markSpot();
}

function openSpot() {
  openLibrary({
    kind: "config", mode: "load",
    onLoad: (c, name) => {
      applyForm(c);
      validateSizes();
      prefs.set("build", readForm());
      setSpot(name);
      markDirty();
      toast(`Opened spot “${name}”`);
    },
  });
}

function saveSpot() {
  const suggest = spot.name || (board.length ? `Spots/${board.join("")}` : "");
  openLibrary({ kind: "config", mode: "save", content: spotContent(), suggest, onLoad: (_, name) => setSpot(name) });
}
function markDirty() {
  const tag = $("build-state");
  if (!builtSig) { tag.textContent = "not built"; tag.className = "tag"; return; }
  const same = builtSig === sig(readForm());
  tag.textContent = same ? "built" : "changed — rebuild";
  tag.className = same ? "tag ok" : "tag";
}

/* ------------------------------------------------------------------ build & solve */

let busy = false;
let treeReady = false, rootPotNow = 0;
function setBusy(b, label) {
  busy = b;
  $("btn-build").disabled = b;
  $("btn-solve").disabled = b;
  $("btn-resume").disabled = b || !treeReady;
  if (label) info(label);
}
function info(html, err = false) { $("build-info").innerHTML = err ? `<span class="err">${html}</span>` : html; }

function memLine(text) {
  const m = /([\d.]+)\s*(MB|GB|KB)/i.exec(text || "");
  return m ? `${m[1]} ${m[2].toUpperCase()}` : "";
}

async function build() {
  if (!validateSizes()) { info("Fix the highlighted sizes: 33 (% pot), 2e (geometric), 3x (raises only), a (all-in).", true); return false; }
  if (board.length < 3) { info("Pick at least a flop (3 cards).", true); return false; }
  const f = readForm();
  setBusy(true, "Generating the tree…");
  try {
    const g = await post("/api/generate", f);
    let est = "";
    try { est = memLine((await get("/api/estimate")).text); } catch (e) { /* estimate is optional */ }
    info(`${g.lines} lines${est ? ` · about ${est}` : ""} — building…`);
    const b = await post("/api/build");
    builtSig = sig(f);
    treeReady = true;
    const total = /TOTAL\D*(\d+)/.exec(b.node_count);
    info(`Built: ${total ? Number(total[1]).toLocaleString() + " nodes" : "ok"}${est ? ` · ${est}` : ""}`);
    markDirty();
    onBuilt();
    return true;
  } catch (e) {
    info(String(e.message || e), true);
    return false;
  } finally { setBusy(false); }
}

/** Solve the tree that is in the solver now; a stopped solve continues where it left off. */
async function solve() {
  const pot = rootPotNow || Number($("f-pot").value);
  const pct = Number($("f-accuracy").value);
  try {
    await post("/api/solve", { accuracy: (pct / 100) * pot });
    toast("Solving…");
  } catch (e) { fail(e); }
}

/** Always rebuild from the form (a fresh tree), then solve it. */
async function buildAndSolve() {
  if (!(await build())) return;
  rootPotNow = Number($("f-pot").value);
  await solve();
}

/* ------------------------------------------------------------------ live progress */

export function updateProgress(st, series, rootPot) {
  const solving = st && st.solving;
  treeReady = !!(st && st.tree);
  if (rootPot) rootPotNow = rootPot;
  $("btn-stop").classList.toggle("hidden", !solving);
  $("btn-solve").classList.toggle("hidden", !!solving);
  $("btn-resume").classList.toggle("hidden", !!solving);
  $("btn-resume").disabled = busy || !treeReady;
  const pot = rootPot || Number($("f-pot").value) || 1;
  drawChart($("chart"), series, pot, Number($("f-accuracy").value));
  const last = series && series.length ? series[series.length - 1] : null;
  const box = $("solve-stats");
  box.textContent = "";
  const kv = (k, v) => { const d = el("div", "kv"); d.append(el("div", "k", k), el("div", "v num", v)); box.appendChild(d); };
  kv("Exploitability", last ? fmt((last.explo / pot) * 100, 3) + "%" : "—");
  kv("Time", last ? fmt(last.t, 1) + "s" : "—");
  kv("EV OOP", last ? fmtChips(last.ev_oop) : "—");
  kv("EV IP", last ? fmtChips(last.ev_ip) : "—");
  $("chart-legend").textContent = st && st.iters ? `${st.iters} iterations` : "";
}

/* ------------------------------------------------------------------ init */

export function initBuild({ built }) {
  onBuilt = built || onBuilt;
  editors.OOP = new RangeEditor(document.querySelector('.editor[data-player="OOP"]'), "OOP", saveForm);
  editors.IP = new RangeEditor(document.querySelector('.editor[data-player="IP"]'), "IP", saveForm);
  buildDeck();

  const slider = $("weight");
  const setW = (v) => {
    weight = v / 100;
    slider.value = v;
    slider.style.setProperty("--p", v + "%");
    $("weight-out").textContent = v + "%";
    document.querySelectorAll(".presets button").forEach((b) => b.classList.toggle("active", Number(b.dataset.w) === Number(v)));
  };
  slider.oninput = () => setW(Number(slider.value));
  document.querySelectorAll(".presets button").forEach((b) => (b.onclick = () => setW(Number(b.dataset.w))));
  setW(100);

  $("board-clear").onclick = () => { board = []; boardChanged(); };
  $("board-random").onclick = () => {
    const deck = [];
    for (const r of RANKS) for (const s of SUITS) deck.push(r + s);
    board = [];
    while (board.length < 3) { const c = deck[Math.floor(Math.random() * deck.length)]; if (!board.includes(c)) board.push(c); }
    boardChanged();
  };
  document.querySelector(".tree-form").addEventListener("input", () => { validateSizes(); saveForm(); });
  $("f-accuracy").addEventListener("input", () => { prefs.set("accuracy", $("f-accuracy").value); markSpot(); });
  const acc = prefs.get("accuracy", null);
  if (acc) $("f-accuracy").value = acc;

  $("spot-open").onclick = openSpot;
  $("spot-save").onclick = saveSpot;
  $("btn-build").onclick = () => { if (!busy) build(); };
  $("btn-solve").onclick = () => { if (!busy) buildAndSolve(); };
  $("btn-resume").onclick = () => { if (!busy && treeReady) solve(); };
  $("btn-stop").onclick = async () => { try { await post("/api/stop"); } catch (e) { fail(e); } };
  loadForm();
  validateSizes();
  markDirty();
}

export function treeLoadedElsewhere() { builtSig = null; markDirty(); }
