// Node locking (#200, part 1): set the acting player's strategy at a decision by hand, lock it,
// and re-solve the whole tree or the subtree below it. Server side: /api/lock, /api/unlock,
// /api/lock_state, /api/lock/revert (viewer/server.py).
//
// Edits are per hand class (the 169 matrix cells): suit-isomorphic combos share one strategy in
// the solver, so per-combo edits would not survive anyway.

import { get, post, toast, fail, prefs, fmt } from "./api.js";
import { el, H, blockedBy } from "./cards.js";
import { lockContext, study } from "./study.js";

const $ = (id) => document.getElementById(id);

const L = {
  ctx: null,      // lockContext() when the dialog opened
  edit: null,     // actions x 1326, the strategy being edited (rows in solver order)
  edited: new Set(),
  sel: new Set(),
  state: null,    // /api/lock_state
  blocked: null,
  drag: null,     // {on: bool} while painting a selection
};
let onChanged = async () => {};

/* ------------------------------------------------------------------ helpers */

const live = (i) => !L.blocked(H.order[i]) && (L.ctx.reach[i] || 0) > 0;

function classCombos(cls) { return H.byClass[cls].filter(live); }

/** Range-weighted frequency of each action (display order) over the given combos. */
function freqs(freq, combos) {
  const out = L.ctx.acts.map(() => 0);
  let tot = 0;
  for (const i of combos) {
    const r = L.ctx.reach[i] || 0;
    tot += r;
    L.ctx.acts.forEach((a, j) => { out[j] += r * (freq[a.k][i] || 0); });
  }
  return { f: out.map((x) => (tot > 0 ? x / tot : 0)), tot };
}

function allLive() { const v = []; for (let i = 0; i < H.order.length; i++) if (live(i)) v.push(i); return v; }

function selCombos() {
  const v = [];
  for (const cls of L.sel) v.push(...classCombos(cls));
  return v;
}

/** Scale action j so its share of the whole range is exactly `target`, changing only `combos`.
 *  Down: every changed hand plays it k times as often (g = k p). Up: every changed hand moves the
 *  same fraction t of the way to always (g = p + t (1 - p)), so hands that never played it start
 *  to. Both are linear in k / t, so the target is hit exactly. The other actions keep their
 *  proportions within each hand. */
function scaleAction(j, target, combos) {
  const a = L.ctx.acts[j].k, A = L.ctx.acts.length;
  const reach = L.ctx.reach;
  const all = allLive();
  const R = all.reduce((s, i) => s + (reach[i] || 0), 0);
  if (!combos.length || R <= 0) throw new Error("no hands to change");
  const inSet = new Set(combos);
  let cur = 0, mass = 0, room = 0;
  for (const i of all) {
    const r = reach[i] || 0, p = L.edit[a][i] || 0;
    cur += r * p;
    if (inSet.has(i)) { mass += r * p; room += r * (1 - p); }
  }
  cur /= R;
  const lo = cur - mass / R, hi = cur + room / R;
  const where = L.sel.size ? "the selected hands" : "all hands";
  if (target < lo - 1e-9 || target > hi + 1e-9) {
    throw new Error(`${L.ctx.acts[j].label} can only go from ${fmt(100 * lo, 1)}% to ${fmt(100 * hi, 1)}% by changing ${where}`);
  }
  let g;
  if (target <= cur) {
    const k = mass > 0 ? Math.max(0, 1 - ((cur - target) * R) / mass) : 1;
    g = (p) => k * p;
  } else {
    const t = room > 0 ? Math.min(1, ((target - cur) * R) / room) : 0;
    g = (p) => p + t * (1 - p);
  }
  for (const i of combos) {
    const p = L.edit[a][i] || 0, ng = Math.min(1, Math.max(0, g(p))), rest = 1 - p;
    for (let k = 0; k < A; k++) {
      const kk = L.ctx.acts[k].k;
      if (kk === a) continue;
      L.edit[kk][i] = rest > 1e-12 ? (L.edit[kk][i] || 0) * (1 - ng) / rest : (1 - ng) / (A - 1);
    }
    L.edit[a][i] = ng;
  }
  for (const cls of (L.sel.size ? L.sel : new Set(H.classes))) if (classCombos(cls).length) L.edited.add(cls);
}

/* ------------------------------------------------------------------ rendering */

let gridCells = null;

function renderGrid() {
  const g = $("lk-grid");
  if (!gridCells) {
    g.textContent = "";
    gridCells = H.classes.map((cls) => {
      const c = el("div", "lk-cell");
      const bar = el("div", "lk-bar"), lbl = el("div", "lbl", cls);
      c.append(bar, lbl);
      c.onmousedown = (ev) => { ev.preventDefault(); L.drag = { on: !L.sel.has(cls) }; toggle(cls, L.drag.on); };
      // paint only while the button is held (a scroll that moves a cell under a still pointer must not)
      c.onmouseenter = (ev) => { if (L.drag && (ev.buttons & 1)) toggle(cls, L.drag.on); };
      g.appendChild(c);
      return { cls, c, bar };
    });
  }
  for (const x of gridCells) {
    const combos = classCombos(x.cls);
    x.c.classList.toggle("blank", !combos.length);
    x.c.classList.toggle("sel", L.sel.has(x.cls));
    x.c.classList.toggle("edited", L.edited.has(x.cls));
    while (x.bar.firstChild) x.bar.firstChild.remove();
    if (!combos.length) continue;
    const { f } = freqs(L.edit, combos);
    L.ctx.acts.forEach((a, j) => {
      const sp = el("span");
      sp.style.background = a.color;
      sp.style.flexGrow = Math.max(f[j], 0);
      x.bar.appendChild(sp);
    });
  }
}

function toggle(cls, on) {
  if (!classCombos(cls).length) return;
  if (on) L.sel.add(cls); else L.sel.delete(cls);
  renderGrid();
  renderSel();
}

let lastSelKey = "";

/** Selection summary; when the selection changes, the Selected column starts from its current mix
 *  (and is left alone otherwise, so typed values are not overwritten). */
function renderSel(force = false) {
  const n = selCombos().length;
  $("lk-sel").textContent = L.sel.size ? `${L.sel.size} hands selected (${n} combos)` : "Click or drag hands to select them.";
  const key = [...L.sel].sort().join(",");
  if (!force && key === lastSelKey) return;
  lastSelKey = key;
  if (L.sel.size) {
    const { f } = freqs(L.edit, selCombos());
    document.querySelectorAll("#lk-acts input.mix").forEach((inp, j) => { inp.value = fmt(100 * f[j], 0); });
  }
}

function renderActs() {
  const tb = $("lk-acts");
  tb.textContent = "";
  const all = allLive();
  const solved = freqs(L.ctx.freq, all).f, now = freqs(L.edit, all).f;
  L.ctx.acts.forEach((a, j) => {
    const tr = el("tr");
    const name = el("td", "act");
    const sw = el("span", "sw"); sw.style.background = a.color;
    name.append(sw, el("span", null, a.label));
    if (a.sub) name.appendChild(el("small", "muted", " " + a.sub));
    const mix = el("input", "mix"); mix.type = "number"; mix.min = "0"; mix.max = "100"; mix.step = "5";
    mix.value = fmt(100 * now[j], 0);
    const tdm = el("td", "num"); tdm.append(mix, el("span", "muted", "%"));
    const sc = el("input", "scale"); sc.type = "number"; sc.min = "0"; sc.max = "100"; sc.step = "1";
    sc.placeholder = fmt(100 * now[j], 0);
    const go = el("button", "btn tiny", "Set");
    go.title = `Scale ${a.label} to this share of the whole range, proportionally across ${L.sel.size ? "the selected hands" : "all hands"}`;
    go.onclick = () => {
      const t = Number(sc.value) / 100;
      if (!(t >= 0 && t <= 1)) { info("Enter a share between 0 and 100%.", true); return; }
      try { scaleAction(j, t, L.sel.size ? selCombos() : all); info(""); refresh(); } catch (e) { info(e.message, true); }
    };
    const tds = el("td", "scale-cell"); tds.append(sc, el("span", "muted", "%"), go);
    const changed = Math.abs(now[j] - solved[j]) > 0.0005;
    tr.append(name, el("td", "num", fmt(100 * solved[j], 1) + "%"), el("td", "num" + (changed ? " changed" : ""), fmt(100 * now[j], 1) + "%"), tdm, tds);
    tb.appendChild(tr);
  });
}

function renderState() {
  const st = L.state || {};
  const lk = st.locked ? "Locked" : st.combo_locked ? "Some hands locked" : "Not locked";
  $("lk-state").textContent = lk;
  $("lk-state").className = "tag" + (st.locked || st.combo_locked ? " lock" : "");
  $("lk-unlock").classList.toggle("hidden", !(st.locked || st.combo_locked));
  $("lk-unlock-all").classList.toggle("hidden", !st.locks || Object.keys(st.locks).length < 1);
  $("lk-revert").classList.toggle("hidden", !st.undo);
  $("lk-snap").disabled = st.can_snapshot === false || !!st.undo || !st.can_solve;
  $("lk-snap").parentElement.title = st.undo ? "A copy of the unlocked solve is already kept" :
    !st.can_solve ? "This tree was opened from a file: it cannot be re-solved here" :
    st.can_snapshot === false ? "The tree already has locks: there is no unlocked solve to copy" : "";
  $("lk-after").classList.toggle("hidden", !(st.locked || st.combo_locked) || !st.can_solve);
}

function refresh() { renderGrid(); renderActs(); renderSel(true); renderState(); }

function info(html, err) { $("lk-info").innerHTML = err ? `<span class="err">${html}</span>` : html; }

/* ------------------------------------------------------------------ actions */

function setSelected() {
  if (!L.sel.size) { info("Select some hands first.", true); return; }
  const mix = [...document.querySelectorAll("#lk-acts input.mix")].map((x) => Math.max(0, Number(x.value) || 0));
  const s = mix.reduce((a, b) => a + b, 0);
  if (s <= 0) { info("The mix is all zeros.", true); return; }
  for (const i of selCombos()) L.ctx.acts.forEach((a, j) => { L.edit[a.k][i] = mix[j] / s; });
  for (const cls of L.sel) L.edited.add(cls);
  info(s !== 100 ? `Mix normalised to 100% (it added up to ${fmt(s, 0)}%).` : "");
  refresh();
}

function editedMask() {
  const m = new Array(H.order.length).fill(0);
  for (const cls of L.edited) for (const i of H.byClass[cls]) m[i] = 1;
  return m;
}

async function doLock(mode) {
  const body = { id: L.ctx.id, mode, snapshot: $("lk-snap").checked && !$("lk-snap").disabled };
  if (mode !== "current") {
    body.freq = L.edit;
    const scope = document.querySelector('input[name="lk-scope"]:checked').value;
    if (scope === "combos") {
      if (!L.edited.size) { info("Edit some hands first (or lock every hand).", true); return; }
      body.mode = "combos";
      body.combos = editedMask();
    }
  }
  info(body.snapshot ? "Keeping a copy of the unlocked solve, then locking…" : "Locking…");
  try {
    L.state = await post("/api/lock", body);
    info(mode === "current" ? "Locked as solved." : body.mode === "combos" ? `Locked ${L.edited.size} hands.` : "Locked.");
    renderState();
    await onChanged();
  } catch (e) { info(String(e.message || e), true); }
}

async function doUnlock(all) {
  try {
    const r = await post("/api/unlock", all ? { all: true } : { id: L.ctx.id });
    L.state = all ? await get("/api/lock_state?id=" + encodeURIComponent(L.ctx.id)) : r;
    info(all ? "Every lock is off; the strategies stay where they were until you solve again." : "Unlocked.");
    renderState();
    await onChanged();
  } catch (e) { fail(e); }
}

async function doRevert() {
  if (!confirm("Go back to the solve as it was before the first lock? Locks and everything solved since are dropped.")) return;
  try {
    await post("/api/lock/revert", {});
    toast("Back to the unlocked solve");
    $("lock-dialog").close();
    await onChanged();
  } catch (e) { fail(e); }
}

async function resolveTree() {
  const pct = Number($("lk-acc").value);
  if (!(pct > 0)) { info("Set a target above 0.", true); return; }
  prefs.set("lock-acc", $("lk-acc").value);
  try {
    await post("/api/solve", { accuracy: (pct / 100) * L.ctx.rootPot });
    toast("Re-solving the whole tree with the lock");
    $("lock-dialog").close();
  } catch (e) { info(String(e.message || e), true); }
}

/* ------------------------------------------------------------------ plain English (#200 part 2) */

// The model (Kimi K3 via Sail, server side: viewer/nl_lock.py) answers with a lock spec, the server
// turns it into a strategy, and it lands here as edits: nothing is locked until Lock is clicked.
const NL = { status: null, timer: null, seq: 0 };
const usd = (x) => (x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(3)}`);

function nlOut(html, err) { $("lk-nl-out").innerHTML = err ? `<span class="err">${html}</span>` : html; }
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function nlEstimate() {
  clearTimeout(NL.timer);
  const text = $("lk-nl-text").value.trim();
  if (!NL.status || !NL.status.enabled || !text) { $("lk-nl-cost").textContent = ""; return; }
  NL.timer = setTimeout(async () => {
    const seq = ++NL.seq;
    try {
      const e = await post("/api/nl/estimate", { id: L.ctx.id, text });
      if (seq === NL.seq) $("lk-nl-cost").textContent = `about ${usd(e.usd_input)} in, at most ${usd(e.usd_max)} with the answer`;
    } catch (err) { /* the estimate is a hint only */ }
  }, 500);
}

async function nlPropose() {
  const text = $("lk-nl-text").value.trim();
  if (!text) { nlOut("Describe the strategy first, e.g. “IP never raises”.", true); return; }
  $("lk-nl-go").disabled = true;
  nlOut(`Asking ${esc(NL.status.model)}…`);
  try {
    const r = await post("/api/nl/propose", { id: L.ctx.id, text });
    const cost = r.cost ? ` <span class="muted">· ${r.cost.input_tokens} + ${r.cost.output_tokens} tokens, ${usd(r.cost.usd)}</span>` : "";
    if (!r.ok) {
      nlOut(`${esc(r.error || "The model could not turn that into a lock.")}${r.explanation ? `<br>${esc(r.explanation)}` : ""}${cost}`, true);
      return;
    }
    L.edit = r.freq;
    L.edited = new Set(r.classes);
    L.sel = new Set();
    refresh();
    nlOut(`${esc(r.explanation)}${r.notes.length ? `<br><b>${r.notes.map(esc).join(" · ")}</b>` : ""}`
      + `<br>Proposed for ${r.classes.length} hand classes: compare <i>Solved</i> and <i>Locked</i>, edit if needed, then Lock.${cost}`);
  } catch (e) { nlOut(esc(e.message || e), true); }
  finally { $("lk-nl-go").disabled = !(NL.status && NL.status.enabled); }
}

async function nlInit() {
  try { NL.status = await get("/api/nl/status"); } catch (e) { NL.status = { enabled: false, reason: "Plain-English locks are not available on this server." }; }
  const on = !!NL.status.enabled;
  $("lk-nl-text").disabled = !on;
  $("lk-nl-go").disabled = !on;
  $("lk-nl-model").textContent = on ? `(${NL.status.model})` : "";
  if (!on) nlOut(esc(NL.status.reason || "Plain-English locks are off."));
}

/* ------------------------------------------------------------------ open */

// for viewer/test_lock_js.mjs
export const _test = { L, scaleAction, freqs, allLive };

export async function openLock() {
  const ctx = lockContext();
  if (!ctx) return;
  L.ctx = ctx;
  L.edit = ctx.freq.map((r) => r.map((v) => v || 0));
  L.edited = new Set();
  L.sel = new Set();
  L.blocked = blockedBy(ctx.board);
  gridCells = null;
  $("lk-title").textContent = `Lock ${ctx.actor}'s strategy at ${ctx.id}`;
  $("lk-acc").value = prefs.get("lock-acc", "0.3");
  info("");
  if (NL.status && NL.status.enabled) { nlOut(""); nlEstimate(); }
  try { L.state = await get("/api/lock_state?id=" + encodeURIComponent(ctx.id)); } catch (e) { fail(e); return; }
  refresh();
  $("lock-dialog").showModal();
}

export function initLock({ changed }) {
  onChanged = changed || onChanged;
  $("btn-lock").onclick = openLock;
  $("lk-close").onclick = () => $("lock-dialog").close();
  document.addEventListener("mouseup", () => { L.drag = null; });
  $("lk-all").onclick = () => { for (const c of H.classes) if (classCombos(c).length) L.sel.add(c); renderGrid(); renderSel(); };
  $("lk-none").onclick = () => { L.sel.clear(); renderGrid(); renderSel(); };
  $("lk-apply").onclick = setSelected;
  $("lk-reset").onclick = () => { L.edit = L.ctx.freq.map((r) => r.map((v) => v || 0)); L.edited.clear(); info(""); refresh(); };
  $("lk-lock").onclick = () => doLock("node");
  $("lk-current").onclick = () => doLock("current");
  $("lk-unlock").onclick = () => doUnlock(false);
  $("lk-unlock-all").onclick = () => doUnlock(true);
  $("lk-revert").onclick = doRevert;
  $("lk-tree").onclick = resolveTree;
  $("lk-node").onclick = () => { $("lock-dialog").close(); $("btn-further").click(); };
  $("lk-nl-go").onclick = nlPropose;
  $("lk-nl-text").addEventListener("input", nlEstimate);
  $("lk-nl-text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); nlPropose(); } });
  nlInit();
}
