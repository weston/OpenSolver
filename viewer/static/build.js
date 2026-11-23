// Build view (#113): two 13x13 range editors (OOP left, IP right) painted with a weight slider,
// a card-button board picker, the bet-size form, and the solve panel with the convergence chart.
// A switch at the top (#492) chooses the tree: Postflop (one board, this file's form) or Preflop
// (heads up from the blinds, preflop_build.js's form); the range editors' tools and the solve panel
// are the same for both.

import { H, RANKS, SUITS, GLYPH, el, cardEl } from "./cards.js";
import { startPreview, previewOn } from "./preview.js";
import { parseRange, formatRange, rangeStats } from "./ranges.js";
import { get, post, toast, fail, prefs, fmt, fmtChips, fmtEvPct } from "./api.js";
import { drawChart } from "./chart.js";
import { openLibrary } from "./library.js";
import { STREETS, renderSizes, readSizes, validateSizes as validateSizeTable } from "./sizes.js";
import { initPreflopForm, readPreflopForm, preflopReady, noteEstimate } from "./preflop_build.js";
import { bb100ToChips, chipsToBb100, fmtMemory } from "./preflop.js";

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
  /** title: what the player is called here (the Preflop form's blinds); the position by default. */
  constructor(root, player, onChange, title = null) {
    this.player = player;
    this.onChange = onChange;
    this.w = new Float32Array(1326);
    root.textContent = "";

    const head = el("div", "editor-head");
    head.append(el("span", `badge ${player.toLowerCase()}`, player), el("h2", null, title || (player === "OOP" ? "Out of position" : "In position")));
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
      kind: "range", mode: "load", onLoad: (text, name) => { this.setText(text.trim()); this.source = name; toast(`Loaded “${name}” into ${title ? `the ${title.toLowerCase()}` : player}`); },
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
  Object.assign(f, readSizes(sizesBody()));
  if (pioExtra.length) f.pio_extra = pioExtra;
  return f;
}

/* PioViewer settings the form has no field for (#317), kept from an imported tree file and sent
 * with every build: merge similar bets, street caps, forced bets, unified re-raises, hand edits. */
let pioExtra = [];

function describePioExtra(lines) {
  const kv = Object.fromEntries(lines.map((l) => { const m = l.match(/^#([^#]+)#(.*)$/); return m ? [m[1], m[2]] : ["", ""]; }));
  const out = [];
  if (/^true$/i.test(kv.MergeSimilarBets || "")) out.push(`merge similar bets (${kv.MergeSimilarBetsThreshold || 12}%)`);
  if (/^true$/i.test(kv.CapEnabled || "") && kv.CapPerStreet) out.push(`street caps ${kv.CapPerStreet.split(/\\n/).join("/")}`);
  if (/^true$/i.test(kv.ForceOOPBet || "")) out.push("OOP must bet the flop");
  if (/^true$/i.test(kv.ForceIPBet || "")) out.push("IP must bet the flop");
  if (/^true$/i.test(kv.UseUnifiedRaiseAfterRaise || "")) out.push(`re-raises ${kv.UnifiedRaiseAfterRaise || ""}`);
  const edits = lines.filter((l) => /^#(Removed|Extra)Lines/.test(l))
    .reduce((n, l) => n + l.replace(/^#[^#]+#/, "").split(/\\+0/).filter((p) => p.trim()).length, 0);
  if (edits) out.push(`${edits} hand-edited line${edits === 1 ? "" : "s"}`);
  return out.length ? out.join(", ") : `${lines.length} PioViewer setting${lines.length === 1 ? "" : "s"}`;
}

function renderPioExtra() {
  $("pio-extra").classList.toggle("hidden", !pioExtra.length);
  $("pio-extra-text").textContent = pioExtra.length ? `Also from PioViewer: ${describePioExtra(pioExtra)}.` : "";
}

async function importPio(file) {
  try {
    const text = await file.text();
    const r = await post("/api/pio_import", { text });
    applyForm(r.form);
    saveForm();
    const how = r.matches ? `the same ${r.file_lines} lines PioViewer generated`
      : r.file_lines ? `${r.lines} lines (the file has ${r.file_lines}: not an exact match)` : `${r.lines} lines`;
    toast(`Imported ${file.name}: builds ${how}.`);
  } catch (e) { fail(e); }
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
  board = f && typeof f.board === "string" ? cleanBoard(f.board) : [...DEFAULTS.board];
  if (f && f.accuracy != null && Number.isFinite(Number(f.accuracy))) {
    $("f-accuracy").value = f.accuracy;
    prefs.set("accuracy", $("f-accuracy").value);  // 3. as typing does: an opened spot survives a reload (#384)
  }
  if (f) {
    const set = (id, v) => { if (v != null && Number.isFinite(v)) $(id).value = v; };
    set("f-pot", f.pot); set("f-stack", f.stack);
    set("f-allin-threshold", f.allin_threshold); set("f-allin-limit", f.add_allin_limit);
  }
  renderSizes(sizesBody(), STREETS, f || {});
  pioExtra = f && Array.isArray(f.pio_extra) ? f.pio_extra.map(String) : [];
  renderPioExtra();
  drawBoard();
  updateSpr();
}

function updateSpr() {
  const p = Number($("f-pot").value), s = Number($("f-stack").value);
  $("f-spr").textContent = p > 0 && s > 0 ? (s / p).toFixed(2) : "—";
}

function validateSizes() { return validateSizeTable(sizesBody()); }

/* An empty number input reads as 0, and the server takes 0 literally: a 0 all-in threshold turns
 * every bet into a shove, a 0 "add all-in below" never adds one, a 0 pot builds a tree with no
 * bets. So each of these must hold before a build (the target, before a solve). */
const NUMBER_FIELDS = {
  "f-pot": ["Pot (a whole number of chips, at least 1)", (v) => Number.isInteger(v) && v >= 1],
  "f-stack": ["Effective stack (a whole number of chips, at least 1)", (v) => Number.isInteger(v) && v >= 1],
  "f-allin-threshold": ["All-in threshold (above 0)", (v) => v > 0],
  "f-allin-limit": ["Add all-in below (0 or more)", (v) => v >= 0],
  "f-accuracy": ["Target exploitability (above 0)", (v) => v > 0],
  "pf-accuracy": ["Target exploitability (above 0 bb/100)", (v) => v > 0],
};
function checkNumber(id) {
  const raw = $(id).value.trim();
  const good = raw !== "" && Number.isFinite(Number(raw)) && NUMBER_FIELDS[id][1](Number(raw));
  $(id).classList.toggle("invalid", !good);
  return good;
}
/** Names of the invalid fields among ids (marked in the form), empty when all are fine. */
function badNumbers(ids) { return ids.filter((id) => !checkNumber(id)).map((id) => NUMBER_FIELDS[id][0]); }

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
      const n = Array.isArray(c.locks) ? c.locks.length : 0;
      toast(`Opened spot “${name}”${n ? ` · ${plural(n, "node lock")}, set again when you build it` : ""}`);
    },
  });
}

const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** Save the form as a spot. The node locks set on the tree built from this very form go with it
 *  (#200); building the spot again sets them again. */
async function saveSpot() {
  const suggest = spot.name || (board.length ? `Spots/${board.join("")}` : "");
  const content = spotContent();
  if (treeReady && builtSig === sig(readForm())) {
    try {
      const r = await get("/api/lock/export");
      if (r.locks && r.locks.length) content.locks = r.locks;
    } catch (e) { toast(`The node locks could not be read, so the spot is saved without them: ${e.message || e}`, "err"); }
  }
  // the dialog's preview lists the locks that go with the spot
  openLibrary({ kind: "config", mode: "save", content, suggest, onLoad: (_, name) => setSpot(name) });
}

/** The saved spot's node locks when the form is that spot, unchanged; else null. */
async function savedSpotLocks() {
  if (!spot.name || spot.sig !== sig(spotForm())) return null;
  try {
    const r = await get(`/api/library/get?kind=config&name=${encodeURIComponent(spot.name)}`);
    const locks = r.content && r.content.locks;
    return Array.isArray(locks) && locks.length ? locks : null;
  } catch (e) { return null; }  // renamed or deleted since: build without
}
/* ------------------------------------------------------------------ postflop or preflop (#492) */

let mode = "postflop";   // which form is shown; the tree in the solver can be of the other kind
const PREFLOP_SIG = "preflop:";
/** The shown form as a signature: what `builtSig` holds for the tree built from it. */
function formSig() {
  if (mode !== "preflop") return sig(readForm());
  return preflopReady() ? PREFLOP_SIG + sig(readPreflopForm()) : "";
}

function setMode(m) {
  mode = m === "preflop" ? "preflop" : "postflop";
  document.querySelector(".build").dataset.mode = mode;
  document.querySelectorAll("#build-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  prefs.set("build-mode", mode);
  if (!busy) info("");   // what the line said was about the other form
  markDirty();
  syncResume();
}

/** "Solve" continues the tree in the solver to the target shown, so it needs a tree of the kind the
 *  form is set to: a preflop tree's target is in bb/100, a postflop tree's in % of its pot. */
function syncResume() {
  const btn = $("btn-resume");
  const other = treeReady && (treeBB > 0) !== (mode === "preflop");
  btn.disabled = busy || !treeReady || other;
  btn.title = other
    ? `The tree in the solver is a ${treeBB > 0 ? "preflop" : "postflop"} tree: switch to ${treeBB > 0 ? "Preflop" : "Postflop"} to solve it further`
    : "Solve the current tree, continuing from where it stopped";
}

function markDirty() {
  const tag = $("build-state");
  if (!builtSig) { tag.textContent = "not built"; tag.className = "tag"; return; }
  const same = builtSig === formSig();
  const other = builtSig.startsWith(PREFLOP_SIG) !== (mode === "preflop");
  tag.textContent = same ? "built" : other ? `a ${mode === "preflop" ? "postflop" : "preflop"} tree is built` : "changed — rebuild";
  tag.className = same ? "tag ok" : "tag";
}

/* ------------------------------------------------------------------ build & solve */

let busy = false;
let treeReady = false, rootPotNow = 0;
let treeBB = 0;   // chips in a big blind of the tree in the solver when it is a preflop tree, else 0
function setBusy(b, label) {
  busy = b;
  $("btn-build").disabled = b;
  $("btn-solve").disabled = b;
  syncResume();
  if (label) info(label);
}
/** A status line: html for our own messages; an error is plain text (it can echo server or file input, #384). */
function info(html, err = false) {
  const box = $("build-info");
  if (!err) { box.innerHTML = html; return; }
  const s = document.createElement("span");
  s.className = "err";
  s.textContent = html;
  box.replaceChildren(s);
}

/** A saved/imported board: 0-5 distinct valid cards, else none (#384). */
function cleanBoard(text) {
  const cards = typeof text === "string" ? text.replace(/\s+/g, "").match(/../g) || [] : [];
  const ok = cards.length <= 5 && cards.every((c) => /^[2-9TJQKA][cdhs]$/.test(c)) && new Set(cards).size === cards.length;
  return ok ? cards : [];
}

function memLine(text) {
  const m = /([\d.]+)\s*(MB|GB|KB)/i.exec(text || "");
  return m ? `${m[1]} ${m[2].toUpperCase()}` : "";
}

async function build() {
  if (!validateSizes()) { info("Fix the highlighted sizes: 33 (% pot), 2e (geometric), 3x (raises only), a (all-in).", true); return false; }
  if (board.length < 3) { info("Pick at least a flop (3 cards).", true); return false; }
  const bad = badNumbers(["f-pot", "f-stack", "f-allin-threshold", "f-allin-limit"]);
  if (bad.length) { info(`Fix ${bad.join(", ")}.`, true); return false; }
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
    treeBB = 0;
    rootPotNow = f.pot;   // the target of a Resume right after this build (before a poll sets it)
    const total = /TOTAL\D*(\d+)/.exec(b.node_count);
    let locked = "";
    const locks = await savedSpotLocks();
    if (locks) {
      const r = await post("/api/lock/apply", { locks });
      locked = ` · ${plural(r.applied.length, "node lock")} from the spot`;
      if (r.skipped.length) {
        locked += `, ${r.skipped.length} skipped`;
        toast(`${plural(r.skipped.length, "lock")} of the spot did not fit the tree: ${r.skipped.map((x) => `${x.id} (${x.error})`).join("; ")}`, "err");
      }
    }
    info(`Built: ${total ? Number(total[1]).toLocaleString() + " nodes" : "ok"}${est ? ` · ${est}` : ""}${locked}`);
    markDirty();
    onBuilt();
    return true;
  } catch (e) {
    info(String(e.message || e), true);
    return false;
  } finally { setBusy(false); }
}

/** Build the heads-up preflop tree of the Preflop form (#492). The server checks the form and the
 *  tree's memory before it touches the solver, so a refused build leaves the tree that is there. */
async function buildPreflop() {
  if (!preflopReady()) { info("The preflop form is not set up yet.", true); return false; }
  const f = readPreflopForm();
  // One request: the server checks the form and the tree's memory in a solver of its own, then
  // builds. (An estimate asked for here first could be ended by the Estimate button's, #493.)
  setBusy(true, "Checking the tree and its memory, then building…");
  try {
    const b = await post("/api/preflop/build", f);
    noteEstimate(f, b);
    builtSig = PREFLOP_SIG + sig(f);
    treeReady = true;
    treeBB = b.chips;     // the target of a Solve right after this build (before a poll sets it)
    rootPotNow = 0;
    const total = /TOTAL\D*(\d+)/.exec(b.node_count);
    info(`Built: ${total ? Number(total[1]).toLocaleString() + " nodes" : "ok"} · ${Number(b.flops).toLocaleString()} flops · about ${fmtMemory(b.mb)}`);
    markDirty();
    onBuilt({ bb: b.chips });
    return true;
  } catch (e) {
    info(String(e.message || e), true);
    return false;
  } finally { setBusy(false); }
}

/** Solve the tree that is in the solver now; a stopped solve continues where it left off. A preflop
 *  tree's target is in bb/100, a postflop tree's in % of its pot: both become chips for the solver. */
async function solve() {
  const pre = treeBB > 0;
  const bad = badNumbers([pre ? "pf-accuracy" : "f-accuracy"]);
  if (bad.length) { info(`Fix ${bad.join(", ")}.`, true); return; }
  const pot = rootPotNow || Number($("f-pot").value);
  const accuracy = pre ? bb100ToChips(Number($("pf-accuracy").value), treeBB) : (Number($("f-accuracy").value) / 100) * pot;
  if (solveStarting) return;  // a double click sent two solves; the second failed with "solver is running" (#386)
  solveStarting = true;
  try {
    // no fast preview before the flop: it re-solves streets of one board, a preflop tree has many
    const r = await post("/api/solve", { accuracy, preview: !pre && previewOn() });
    if (!r.preview) toast("Solving…");
    startPreview(!!r.preview);
  } catch (e) { fail(e); } finally { solveStarting = false; }
}
let solveStarting = false;

/** The Study view's board picker: the same tree (ranges, sizes, everything in the form) on other
 *  root cards, rebuilt and solved like Build & solve. False if a build or solve start is busy. */
export async function rebuildWithBoard(cards) {
  if (busy) return false;
  board = [...cards];
  boardChanged();
  toast(`Rebuilding on ${cards.join(" ")}…`);
  if (!(await build())) {   // the Build panel shows why; say it here too (the Study view is open)
    toast($("build-info").textContent || "The build failed", "err");
    return true;
  }
  rootPotNow = Number($("f-pot").value);
  await solve();
  return true;
}

/** Always rebuild from the form (a fresh tree), then solve it. */
async function buildAndSolve() {
  if (mode === "preflop") {
    if (badNumbers(["pf-accuracy"]).length) { info(`Fix ${NUMBER_FIELDS["pf-accuracy"][0]}.`, true); return; }
    if (await buildPreflop()) await solve();
    return;
  }
  if (!(await build())) return;
  rootPotNow = Number($("f-pot").value);
  await solve();
}

/* ------------------------------------------------------------------ live progress */

/** bb: chips in a big blind when the tree in the solver is a preflop tree (its exploitability is
 *  then shown in bb/100), else 0. EVs are a share of the root's pot for both kinds (#459). */
export function updateProgress(st, series, rootPot, bb = 0) {
  const solving = st && st.solving;
  treeReady = !!(st && st.tree);
  if (rootPot) rootPotNow = rootPot;
  treeBB = treeReady ? bb : 0;
  $("btn-stop").classList.toggle("hidden", !solving);
  $("btn-solve").classList.toggle("hidden", !!solving);
  $("btn-resume").classList.toggle("hidden", !!solving);
  syncResume();
  const pre = treeBB > 0;
  const pot = rootPot || Number($("f-pot").value) || 1;
  if (pre) drawChart($("chart"), series, treeBB, Number($("pf-accuracy").value), "bb/100");
  else drawChart($("chart"), series, pot, Number($("f-accuracy").value));
  const last = series && series.length ? series[series.length - 1] : null;
  const box = $("solve-stats");
  box.textContent = "";
  const kv = (k, v) => { const d = el("div", "kv"); d.append(el("div", "k", k), el("div", "v num", v)); box.appendChild(d); };
  kv("Exploitability", !last ? "—" : pre ? fmt(chipsToBb100(last.explo, treeBB), 2) + " bb/100" : fmt((last.explo / pot) * 100, 3) + "%");
  kv("Time", last ? fmt(last.t, 1) + "s" : "—");
  kv(pre ? "EV BB (OOP)" : "EV OOP", last ? fmtEvPct(last.ev_oop, pot) : "—");
  kv(pre ? "EV SB (IP)" : "EV IP", last ? fmtEvPct(last.ev_ip, pot) : "—");
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
  let acc = prefs.get("accuracy", null);
  if (acc != null && !Number.isFinite(Number(acc))) acc = null;  // a damaged pref, not "[object Object]" (#392)
  if (acc) $("f-accuracy").value = acc;

  $("spot-open").onclick = openSpot;
  $("spot-save").onclick = saveSpot;
  $("pio-import").onclick = () => $("pio-import-file").click();
  $("pio-import-file").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) importPio(f); };
  $("pio-extra-clear").onclick = () => { pioExtra = []; renderPioExtra(); saveForm(); };
  $("btn-build").onclick = () => { if (!busy) (mode === "preflop" ? buildPreflop : build)(); };
  $("btn-solve").onclick = () => { if (!busy) buildAndSolve(); };
  $("btn-resume").onclick = () => { if (!busy && treeReady && (treeBB > 0) === (mode === "preflop")) solve(); };
  document.querySelectorAll("#build-mode button").forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
  const pfAcc = prefs.get("preflop-accuracy", null);
  if (pfAcc != null && Number.isFinite(Number(pfAcc)) && Number(pfAcc) > 0) $("pf-accuracy").value = pfAcc;
  $("pf-accuracy").addEventListener("input", () => prefs.set("preflop-accuracy", $("pf-accuracy").value));
  initPreflopForm(RangeEditor, markDirty);   // its defaults come from the server; the form is usable when they are in
  for (const id of Object.keys(NUMBER_FIELDS))   // a marked field clears once it is fixed
    $(id).addEventListener("input", () => { if ($(id).classList.contains("invalid")) checkNumber(id); });
  $("btn-stop").onclick = async () => { try { await post("/api/stop"); } catch (e) { fail(e); } };
  loadForm();
  validateSizes();
  setMode(prefs.get("build-mode", "postflop"));
}

export function treeLoadedElsewhere() { builtSig = null; markDirty(); }

// The Build form as a tree config for the job queue (#196): everything but the board, which each job sets.
export function currentTree() {
  if (!validateSizes()) throw new Error("Fix the highlighted sizes on the Build tab first.");
  const bad = badNumbers(["f-pot", "f-stack", "f-allin-threshold", "f-allin-limit"]);
  if (bad.length) throw new Error(`Fix ${bad.join(", ")} on the Build tab first.`);
  const f = readForm();
  if (!f.range_oop || !f.range_ip) throw new Error("Set both ranges on the Build tab first.");
  return { ...f, board: "" };
}
