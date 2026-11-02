// Solve further (#153): continue solving the loaded tree from the current decision node down,
// on the main solver process (POST /api/solve_node -> UPI `go_partial`). Progress comes from the
// server's `partial` state (routed out of the whole-tree convergence series); done -> the study
// view refreshes itself in app.js's poll loop.

import { el, } from "./cards.js";
import { get, post, toast, fail, prefs, fmt, fmtChips } from "./api.js";
import { study } from "./study.js";

const $ = (id) => document.getElementById(id);

const TITLE_OK = "Continue solving the tree from this node down (changes its strategies in memory)";
const TITLE_FILE = "This tree was opened from a file — saved trees have no solver state to continue from";
const TITLE_BUSY = "The solver is busy — stop the current run first";

let fromFile = false;   // /api/progress: tree came from load_tree
let solving = false;    // /api/progress: solver is running (partial or whole)
let lastShow = false;   // last visibility state handed to syncFurther()
let lastPartial = null; // last st.partial seen by the poll (final state survives dialog closes)

const sf = { node: null, pot: 0, running: false, root: false, startExplo: null, target: 0 };

/* ------------------------------------------------------------------ the node header button */

/** Called by study.js's render(): the button sits next to Re-solve… and shares its visibility. */
export function syncFurther(show) {
  lastShow = !!show;
  const b = $("btn-further");
  b.classList.toggle("hidden", !lastShow);
  b.disabled = fromFile || solving;
  b.title = fromFile ? TITLE_FILE : solving ? TITLE_BUSY : TITLE_OK;
}

/* ------------------------------------------------------------------ dialog */

function setStatus(html) { $("sf-status").innerHTML = html; }

function setRunning(running) {
  sf.running = running;
  $("sf-go").classList.toggle("hidden", running);
  $("sf-stop").classList.toggle("hidden", !running);
  $("sf-accuracy").disabled = running;
}

function openDialog() {
  if (fromFile) { toast(TITLE_FILE, "err"); return; }
  if (!study.loaded) return;
  sf.node = study.id;
  sf.pot = study.pot;
  sf.root = sf.node === "r:0" || sf.node === "r";
  sf.startExplo = null;
  $("sf-context").textContent = "";
  $("sf-context").append(
    el("span", null, "Node"), el("b", null, sf.node),
    el("span", null, "· pot"), el("b", null, fmtChips(sf.pot) + " chips"),
    el("span", null, sf.root ? "· the tree root — continues the whole solve" : ""));
  const acc = prefs.get("sf-accuracy", null);
  if (acc != null) $("sf-accuracy").value = acc;
  $("sf-progress").classList.add("hidden");
  const isMine = lastPartial && !solving && lastPartial.node === sf.node && lastPartial.done;
  if (isMine) paintDone(lastPartial);  // just finished this node and closed the dialog meanwhile
  setRunning(false);
  $("sf-dialog").showModal();
}

async function start() {
  const pct = Number($("sf-accuracy").value);
  if (!Number.isFinite(pct) || pct <= 0) { fail(new Error("Target must be a positive % of the pot")); return; }
  prefs.set("sf-accuracy", pct);
  const target = (pct / 100) * sf.pot;
  sf.target = target;
  sf.startExplo = null;
  try {
    await post("/api/solve_node", { id: sf.node, accuracy: target });
  } catch (e) { fail(e); return; }
  $("sf-bar").classList.add("indet");
  $("sf-bar-fill").style.width = "0";
  $("sf-progress").classList.remove("hidden");
  setStatus("Starting…");
  setRunning(true);
  paint();  // immediate feedback rather than waiting for the next poll
}

async function stop() {
  setStatus("Stopping…");
  try { await post("/api/stop"); } catch (e) { fail(e); }
}

function bar() {
  const fill = $("sf-bar-fill");
  if (sf.startExplo == null || sf.startExplo <= sf.target) return;
  $("sf-bar").classList.remove("indet");
  // log-scale progress: first measured exploitability -> target
  const span = Math.log(sf.startExplo / sf.target);
  const left = Math.max(0, Math.log((sf.lastExplo ?? sf.startExplo) / sf.target));
  fill.style.width = span > 0 ? (Math.max(2, Math.min(100, 100 * (1 - left / span)))).toFixed(1) + "%" : "100%";
}

function paintDone(partial) {
  $("sf-progress").classList.remove("hidden");
  $("sf-bar").classList.remove("indet");
  $("sf-bar-fill").style.width = "100%";
  const pt = partial && partial.point;
  if (partial && pt && partial.pot > 0) {
    const pct = (pt.explo / partial.pot) * 100;
    setStatus(`Done — exploitable for <b>${fmtChips(pt.explo)}</b> chips (<b>${fmt(pct, 3)}%</b> of this pot)`
      + (partial.stopped ? ` · ${partial.stopped}` : ""));
  } else if (partial && partial.stopped) {
    $("sf-bar-fill").style.width = "100%";
    setStatus(`Stopped (${partial.stopped}) before the first report.`);
  } else {
    $("sf-bar-fill").style.width = "100%";
    setStatus("Solve finished — reopen a node to see the new strategies.");
  }
}

/** Repaint from the latest poll: `st` is /api/progress, `series` the whole-tree points. */
function paint(st, series) {
  if (!sf.running) return;
  const partial = (st && st.partial) || null;
  if (sf.root) {
    // root == whole-tree solve: progress comes from the convergence series
    const last = series && series.length ? series[series.length - 1] : null;
    if (last) {
      if (sf.startExplo == null) sf.startExplo = last.explo;
      sf.lastExplo = last.explo;
      const pct = sf.pot ? (last.explo / sf.pot) * 100 : 0;
      setStatus(`Exploitable for <b>${fmtChips(last.explo)}</b> chips (<b>${fmt(pct, 3)}%</b> of the pot)`
        + ` · target ${fmt((100 * sf.target / sf.pot), 3)}% · ${fmt(last.t, 1)}s`);
      bar();
    }
    if (st && !st.solving) finish(null);
    return;
  }
  if (!partial || partial.node !== sf.node) {
    if (st && !st.solving && (st.solver_status || "").startsWith("stopped")) finish(partial);
    else setStatus("Iterating…");
    return;
  }
  const pt = partial.point;
  if (pt) {
    if (sf.startExplo == null) sf.startExplo = pt.explo;
    sf.lastExplo = pt.explo;
    const pct = partial.pot ? (pt.explo / partial.pot) * 100 : 0;
    setStatus(`Exploitable for <b>${fmtChips(pt.explo)}</b> chips (<b>${fmt(pct, 3)}%</b> of this pot)`
      + ` · target ${fmt((100 * partial.accuracy / partial.pot), 3)}% · ${fmt(pt.t, 1)}s`);
    bar();
  } else {
    setStatus("Iterating…");
  }
  if (partial.done) finish(partial);
}

function finish(partial) {
  setRunning(false);
  sf.running = false;
  paintDone(partial || (lastPartial && lastPartial.node === sf.node ? lastPartial : null));
  if (partial) toast("Solve further finished — the node's strategies were refreshed");
}

/* ------------------------------------------------------------------ poll hook (app.js) */

export function pollFurther(st, series) {
  fromFile = !!(st && st.loaded_file);
  solving = !!(st && st.solving);
  if (st && st.partial) lastPartial = st.partial;
  syncFurther(lastShow);
  if ($("sf-dialog").open) paint(st, series);
}

/* ------------------------------------------------------------------ wiring */

export function initFurther() {
  $("btn-further").onclick = openDialog;
  $("sf-go").onclick = start;
  $("sf-stop").onclick = stop;
  $("sf-close").onclick = () => $("sf-dialog").close();
  $("sf-accuracy").addEventListener("input", () => prefs.set("sf-accuracy", $("sf-accuracy").value));
  // while browsing elsewhere the dialog's node id is stale: reopen shows the current node
  document.getElementById("sf-dialog").addEventListener("close", () => { sf.node = null; });
}

export async function refreshNow() {  // used by tests/headless checks
  const st = await get("/api/progress");
  pollFurther(st, null);
}
