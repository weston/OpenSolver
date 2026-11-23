// Build tab, Preflop mode (#492): the form of a heads-up preflop tree. The server checks the fields
// with tools/preflop.py (the tool the command line uses) and answers with the tree in words, so this
// module only reads and fills the form; what it computes is in preflop.js.

import { get, post, prefs } from "./api.js";
import { el } from "./cards.js";
import { memoryNote, fmtMemory, streetsBet, flopsLabel, nextEstimate, treeSummary } from "./preflop.js";

const $ = (id) => document.getElementById(id);

// form field -> input (text fields: sent as typed, the tool reads them)
const TEXT = {
  stack: "pf-stack", ante: "pf-ante", allin_threshold: "pf-allin-threshold",
  open: "pf-open", iso: "pf-iso", threebet: "pf-threebet", limp_raise: "pf-limp-raise", fourbet: "pf-fourbet", fivebet: "pf-fivebet",
  postflop_allin_threshold: "pf-post-threshold", postflop_allin_pot: "pf-post-allin",
  flop_bet: "pf-flop-bet", turn_bet: "pf-turn-bet", river_bet: "pf-river-bet", raise: "pf-raise", donk: "pf-donk",
};
const DEPTH_WORDS = { none: "No street (checked down)", flop: "The flop", turn: "The turn", river: "The river" };

const editors = {};
let defaults = null;       // GET /api/preflop/defaults: the tool's defaults, the flop subsets, the machine's memory
let onChange = () => {};
let seq = 0, timer = null;
// The memory estimates on show: { sig (the form without its number of flops), machine (MB), cells
// (choice of flops -> { mb, flops }), running, error }. They stay while only the number of flops changes.
let mem = null, memRun = 0;
const memSig = (f) => JSON.stringify({ ...f, flops: null });

/** True once the form holds its defaults (they come from the server: the tool's own). */
export function preflopReady() { return defaults != null; }

export function readPreflopForm() {
  const f = {};
  for (const [k, id] of Object.entries(TEXT)) f[k] = $(id).value.trim();
  f.limp = $("pf-limp").checked;
  f.depth = $("pf-depth").value;
  f.flops = $("pf-flops").value;
  const cap = $("pf-cap").value.trim();
  f.raise_cap = cap === "" ? null : Number(cap);
  f.range_bb = editors.BB.value();
  f.range_sb = editors.SB.value();
  if (f.raise_cap == null) delete f.raise_cap;
  return f;
}

function apply(saved) {
  const d = defaults.form, f = saved && typeof saved === "object" ? saved : {};
  const pick = (k) => (f[k] != null && typeof f[k] !== "object" ? f[k] : d[k]);
  for (const [k, id] of Object.entries(TEXT)) $(id).value = String(pick(k));
  $("pf-limp").checked = typeof f.limp === "boolean" ? f.limp : d.limp;
  $("pf-depth").value = defaults.depths.includes(f.depth) ? f.depth : d.depth;
  $("pf-flops").value = defaults.flops.includes(String(f.flops)) ? String(f.flops) : d.flops;
  $("pf-cap").value = Number.isInteger(f.raise_cap) ? f.raise_cap : d.raise_cap;
  editors.BB.setText(typeof f.range_bb === "string" && f.range_bb ? f.range_bb : d.range_bb);
  editors.SB.setText(typeof f.range_sb === "string" && f.range_sb ? f.range_sb : d.range_sb);
  syncDepth();
}

/** The size fields of the streets that are checked down are not used: shown as such. */
function syncDepth() {
  const on = streetsBet($("pf-depth").value);
  $("pf-flop-bet").disabled = !on.flop;
  $("pf-turn-bet").disabled = !on.turn;
  $("pf-river-bet").disabled = !on.river;
  for (const id of ["pf-raise", "pf-donk", "pf-cap", "pf-post-threshold", "pf-post-allin"]) $(id).disabled = !on.flop;
}

function changed() {
  if (!defaults) return;
  const f = readPreflopForm();
  prefs.set("preflop", f);
  syncDepth();
  if (mem && mem.sig !== memSig(f)) { mem = null; memRun++; }   // another tree: its estimates are not these
  renderMem();
  $("pf-tree").classList.add("stale");
  clearTimeout(timer);
  timer = setTimeout(describe, 300);
  onChange();
}

/** The tree in words under the sizes, or why these settings make no tree. */
async function describe() {
  const mine = ++seq, box = $("pf-tree");
  let d = null, err = null;
  try { d = await post("/api/preflop/describe", readPreflopForm()); } catch (e) { err = String(e.message || e); }
  if (mine !== seq) return;   // a newer answer is on its way
  box.classList.remove("stale");
  box.textContent = "";
  if (err) { box.appendChild(el("div", "err", err)); return; }
  box.appendChild(el("div", "pf-sum", treeSummary(d)));
  const rows = el("div", "pf-rows");
  rows.append(el("span", "pf-h", "Action"), el("span", "pf-h pf-in", "In, SB / BB"), el("span", "pf-h", "Then"));
  for (const r of d.rows) {
    const act = el("span", "pf-act", r.what === "start" ? "Blinds posted" : r.what);
    act.style.paddingLeft = `${r.depth * 16}px`;
    act.title = r.id;
    rows.append(act, el("span", "pf-in", `${r.in[0]} / ${r.in[1]}`), el("span", `pf-then ${r.kind}`, r.then));
  }
  box.appendChild(rows);
}

/** The memory line (the chosen number of flops against the machine's memory) and under it what is
 *  known for the other numbers; a click on one chooses it. */
function renderMem() {
  const out = $("pf-mem-text"), row = $("pf-mem-row"), chosen = $("pf-flops").value;
  const own = mem && mem.cells[chosen];
  const note = own ? memoryNote(own.mb, mem.machine, own.flops) : null;
  out.textContent = note ? note.text : mem && mem.error ? mem.error : mem && mem.running ? "Asking the solver…" : "";
  out.classList.toggle("warn", note ? !note.fits : !!(mem && mem.error));
  row.textContent = "";
  $("pf-estimate").disabled = !!(mem && mem.running);
  if (!mem) return;
  for (const c of defaults.flops) {
    const e = mem.cells[c];
    if (!e) continue;
    const over = mem.machine > 0 && e.mb > mem.machine;
    const b = el("button", `pf-mem-cell${c === chosen ? " active" : ""}${over ? " warn" : ""}`);
    b.type = "button";
    b.title = over ? "More than this machine's memory" : c === chosen ? "The number of flops chosen" : "Take this number of flops";
    b.append(el("span", null, flopsLabel(c)), el("b", "num", fmtMemory(e.mb)));
    b.onclick = () => { $("pf-flops").value = c; changed(); };
    row.appendChild(b);
  }
  if (mem.running) row.appendChild(el("span", "hint", "…"));
}

/** An estimate the server gave for this form ({mb, memory_mb, flops}: Build asks for one too). */
export function noteEstimate(form, e) {
  const sig = memSig(form);
  if (!defaults || sig !== memSig(readPreflopForm())) return;   // the form changed while the server worked
  if (!mem || mem.sig !== sig) { mem = { sig, machine: 0, cells: {}, running: false, error: null }; memRun++; }
  mem.machine = e.memory_mb || 0;
  mem.cells[form.flops] = { mb: e.mb, flops: e.flops };
  mem.error = null;
  renderMem();
}

/** Ask the solver what the tree takes: for the chosen number of flops first, then for the other
 *  numbers, so that the row under the button shows what fewer or more flops would cost. */
async function estimate() {
  if (!defaults) return;   // the form is not filled in yet
  const form = readPreflopForm(), sig = memSig(form);
  if (!mem || mem.sig !== sig) mem = { sig, machine: 0, cells: {}, running: false, error: null };
  const run = ++memRun, mine = mem;
  mine.running = true;
  mine.error = null;
  renderMem();
  const known = () => Object.fromEntries(Object.entries(mine.cells).map(([c, e]) => [c, e.mb]));
  try {
    for (let c; (c = nextEstimate(defaults.flops, $("pf-flops").value, known(), mine.machine)) != null;) {
      const e = await post("/api/preflop/estimate", { ...form, flops: c });
      if (run !== memRun || mem !== mine) return;   // the form is another one now, or a newer run took over
      mine.machine = e.memory_mb || 0;
      mine.cells[c] = { mb: e.mb, flops: e.flops };
      renderMem();
    }
  } catch (e) {
    if (run === memRun && mem === mine) mine.error = String(e.message || e);
  } finally {
    if (run === memRun && mem === mine) { mine.running = false; renderMem(); }
  }
}

/** Set the form up. RangeEditor is the Build tab's (passed in: build.js imports this module). */
export async function initPreflopForm(RangeEditor, changedCb) {
  onChange = changedCb || onChange;
  editors.BB = new RangeEditor(document.querySelector('.editor[data-seat="BB"]'), "OOP", changed, "Big blind");
  editors.SB = new RangeEditor(document.querySelector('.editor[data-seat="SB"]'), "IP", changed, "Small blind (button)");
  for (const id of ["pf-form", "pf-post"]) {
    $(id).addEventListener("input", changed);
    $(id).addEventListener("change", changed);
  }
  $("pf-estimate").onclick = estimate;
  try {
    const d = await get("/api/preflop/defaults");
    $("pf-flops").replaceChildren(...d.flops.map((c) => { const o = el("option", null, flopsLabel(c)); o.value = c; return o; }));
    $("pf-depth").replaceChildren(...d.depths.map((c) => { const o = el("option", null, DEPTH_WORDS[c] || c); o.value = c; return o; }));
    defaults = d;
    apply(prefs.get("preflop", null));
    describe();
    onChange();
  } catch (e) {
    $("pf-tree").replaceChildren(el("div", "err", `The preflop form could not be set up: ${e.message || e}`));
  }
  return defaults;
}
