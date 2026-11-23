// Build tab, Preflop mode (#492): the form of a heads-up preflop tree. The server checks the fields
// with tools/preflop.py (the tool the command line uses) and answers with the tree in words, so this
// module only reads and fills the form; what it computes is in preflop.js.

import { get, post, prefs } from "./api.js";
import { el } from "./cards.js";
import { memoryNote, streetsBet, flopsLabel, treeSummary } from "./preflop.js";

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
let rev = 0;               // counts the form's changes: an estimate of an older form is not shown

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
  rev++;
  prefs.set("preflop", readPreflopForm());
  syncDepth();
  showEstimate(null);
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

/** Show a memory estimate ({mb, memory_mb, flops}) in the form, or clear it (null). */
export function showEstimate(e, err = null) {
  const out = $("pf-mem-text");
  const note = e ? memoryNote(e.mb, e.memory_mb, e.flops) : null;
  out.textContent = err || (note ? note.text : "");
  out.classList.toggle("warn", !!err || !!(note && !note.fits));
}

async function estimate() {
  const btn = $("pf-estimate"), mine = rev;
  btn.disabled = true;
  showEstimate(null);
  $("pf-mem-text").textContent = "Asking the solver…";
  try {
    const e = await post("/api/preflop/estimate", readPreflopForm());
    if (mine === rev) showEstimate(e);
  } catch (e) {
    if (mine === rev) showEstimate(null, String(e.message || e));
  } finally { btn.disabled = false; }
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
