// WSolver viewer: boot, view switching, solver polling, top bar and file dialog.
// ES modules, no build step. Talks to viewer/server.py.

import { initHands } from "./cards.js";
import { get, post, toast, fail, prefs, fmt } from "./api.js";
import { initStudy, gotoNode, showEmpty, study, applyUrlState, setReportBatch } from "./study.js";
import { initBuild, updateProgress, treeLoadedElsewhere } from "./build.js";
import { drawSpark } from "./chart.js";
import { bigBlindOf, chipsToBb100 } from "./preflop.js";
import { initLibrary } from "./library.js";
import { initResolve, openSizes } from "./resolve.js";
import { initFurther, pollFurther } from "./further.js";
import { initLock } from "./lock.js";
import { initJobs, showJobs, openReport } from "./jobs.js";
import { initPreview, previewStatus } from "./preview.js";
import { initDrill, showDrill, drillHere } from "./drill.js";
import { initSettings } from "./settings.js";

const $ = (id) => document.getElementById(id);

const app = { view: "study", tree: false, solving: false, rootPot: 0, bb: 0, series: [], lastEvents: -1, loadedPath: null, loadedBatch: null };

/* ------------------------------------------------------------------ views */

const VIEWS = ["study", "build", "jobs", "drill", "settings"];

function setView(v) {
  app.view = v;
  for (const name of VIEWS) $("view-" + name).classList.toggle("hidden", name !== v);
  showJobs(v === "jobs");
  showDrill(v === "drill");
  document.querySelectorAll(".view-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  if (!location.hash.startsWith("#" + v)) history.replaceState(null, "", "#" + v);
  if (v === "study") {
    if (app.tree) study.refresh(); else showEmpty();
  }
}

/* ------------------------------------------------------------------ theme */

function toggleTheme() {
  const root = document.documentElement;
  const cur = root.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  root.dataset.theme = cur === "light" ? "dark" : "light";
  try { localStorage.setItem("ws-theme", root.dataset.theme); } catch (e) { /* private mode */ }
}

/* ------------------------------------------------------------------ polling */

/** A tree was built, loaded or opened from a job: drop the old tree's convergence. The server clears
 *  its series too, but its event counter doesn't change, so the poll would keep ours (#384). */
function freshTree() {
  app.series = [];
  app.lastEvents = -1;
  drawSpark($("pill-spark"), app.series, app.rootPot);
}

async function refreshRootPot() {
  try {
    const p = await get("/api/path?id=r:0");
    const n = p.nodes[0];
    app.rootPot = (n.pot || []).reduce((a, b) => a + (b || 0), 0);
    app.bb = bigBlindOf(n);   // a preflop tree (#492): its exploitability is told in bb/100
  } catch (e) { app.rootPot = 0; app.bb = 0; }
}

/** The main solve's exploitability in the tree's own unit: % of the root's pot, bb/100 for a preflop tree. */
function exploText(explo, unit = "%") {
  if (app.bb > 0) return `${fmt(chipsToBb100(explo, app.bb), 2)} bb/100`;
  return app.rootPot ? `${fmt((explo / app.rootPot) * 100, 2)}${unit}` : null;
}

async function poll() {
  try {
    await pollOnce();
  } catch (e) {
    console.warn("poll", e);  // #384: one bad poll used to stop polling for good
  }
  schedule();
}

async function pollOnce() {
  let st;
  try { st = await get("/api/progress"); } catch (e) { setPill("offline", "Server offline"); return; }
  const wasSolving = app.solving;
  app.solving = st.solving;
  $("drill-lock-seat").classList.toggle("hidden", !st.drill_variant);
  $("drill-lock-seat").textContent = st.drill_variant ? `Hero ${st.drill_variant.hero} · Drill locks` : "";
  $("btn-drill-exploit").classList.toggle("hidden", !st.drill_variant);
  $("btn-drill-exploit").disabled = st.solving || !!app.exploitBusy;
  app.drillHero = st.drill_variant?.hero;
  if (st.tree && !app.tree) { app.tree = true; await refreshRootPot(); }
  if (!st.tree) app.tree = false;
  if (st.events !== app.lastEvents || st.solving) {
    app.lastEvents = st.events;
    try { app.series = (await get("/api/convergence")).points; } catch (e) { /* keep last */ }
  }
  const last = app.series[app.series.length - 1];
  const pct = last ? exploText(last.explo) : null;
  // Solve further (#153): the subtree solve has its own scale (the node's pot, not the root's)
  const pt = st.partial && st.partial.point, ppot = st.partial && st.partial.pot;
  const ppct = pt && ppot ? (pt.explo / ppot) * 100 : null;
  if (st.dead) setPill("err", "Solver stopped working — restart from Open");
  else if (st.suspended) setPill("solving", `Refining a street · ${pct != null ? pct + " · " : ""}main solve paused`);
  else if (st.solving && st.partial) setPill("solving", `Solving further · ${ppct != null ? fmt(ppct, 2) + "%" : "…"} · ${pt ? fmt(pt.t, 0) + "s" : "0s"}`);
  else if (st.solving) setPill("solving", `Solving · ${pct != null ? pct : "…"} · ${last ? fmt(last.t, 0) + "s" : "0s"}`);
  else if (last) setPill("done", `Solved · ${exploText(last.explo, "% pot") || "—"}`);
  else setPill("", app.tree ? "Tree ready" : "Idle");
  drawSpark($("pill-spark"), app.series, app.rootPot);
  updateProgress(st, app.series, app.rootPot, app.bb);
  pollFurther(st, app.series);
  // the open save's job batch, for Study's Report button (#331); asked again only when the file changes
  if ((st.loaded_path || null) !== app.loadedPath) {
    app.loadedPath = st.loaded_path || null;
    let job = null;
    if (app.loadedPath) { try { job = await get("/api/jobs/loaded"); } catch (e) { job = null; } }
    app.loadedBatch = job && job.batch ? job.batch : null;
    setReportBatch(app.loadedBatch);
  }
  previewStatus(st);

  if (wasSolving && !st.solving) {
    toast(st.partial && st.partial.done && ppct != null
      ? `Solve further finished at ${fmt(ppct, 2)}% of the node's pot`
      : pct != null ? `Solve finished at ${exploText(last.explo, "% of the pot")}` : "Solve finished");
    if (app.view === "study" && app.tree) study.refresh();
  }
}
let timer = null;
function schedule() { clearTimeout(timer); timer = setTimeout(poll, app.solving ? 900 : 2500); }

function setPill(kind, text) {
  const p = $("solve-pill");
  p.className = `solve-pill ${kind}`.trim();
  $("pill-text").textContent = text;
}

/* ------------------------------------------------------------------ open / save */

function fileDialog(mode) {
  const dlg = $("file-dialog");
  $("file-title").textContent = mode === "open" ? "Open a saved tree" : "Save tree";
  $("file-ok").textContent = mode === "open" ? "Open" : "Save";
  $("save-opts").classList.toggle("hidden", mode === "open");
  $("save-hint").classList.toggle("hidden", mode === "open");
  const lastPath = prefs.get("path", "tree.cfr");
  $("file-path").value = typeof lastPath === "string" ? lastPath : "tree.cfr";  // a damaged pref (#392)
  dlg.returnValue = "";
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    if (app.fileBusy) { toast("A load or save is still running", "err"); return; }  // (#386)
    app.fileBusy = true;
    const path = $("file-path").value.trim();
    prefs.set("path", path);
    try {
      if (mode === "open") {
        await post("/api/load", { path });
        app.tree = true;
        freshTree();
        treeLoadedElsewhere();
        await refreshRootPot();
        toast("Opened " + path + " — later streets load as you browse");
        setView("study");
        study.reset();
      } else {
        const m = document.querySelector('input[name="savemode"]:checked').value;
        await post("/api/save", { path, no_rivers: m === "no_rivers", no_turns: m === "no_turns" });
        toast("Saved " + path);
      }
    } catch (e) { fail(e); } finally { app.fileBusy = false; }
  };
  dlg.showModal();
  $("file-path").select();
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  document.querySelectorAll(".view-tab").forEach((b) => (b.onclick = () => setView(b.dataset.view)));
  document.querySelectorAll("[data-goto]").forEach((b) => (b.onclick = () => setView(b.dataset.goto)));
  $("btn-theme").onclick = toggleTheme;
  $("btn-open").onclick = () => fileDialog("open");
  $("empty-open").onclick = () => fileDialog("open");
  $("btn-save").onclick = () => { if (!app.tree) toast("Nothing to save yet — build a tree first"); else fileDialog("save"); };
  $("solve-pill").onclick = () => setView("build");

  try {
    initHands((await get("/api/hand_order")).order);
  } catch (e) { fail("Cannot reach the viewer server: " + e.message); return; }
  initStudy();
  initLibrary();
  initResolve();
  initFurther();
  initLock({ changed: () => study.refresh() });
  initPreview();
  initSettings();
  initDrill({ opened: async () => {
    app.tree = true;
    freshTree();
    treeLoadedElsewhere();
    await refreshRootPot();
    setView("study");
    study.reset();
    await pollOnce();
  } });
  $("btn-drill-exploit").onclick = async () => {
    app.exploitBusy = true;
    $("btn-drill-exploit").disabled = true;
    try {
      await post("/api/best_response", { player: app.drillHero === "OOP" ? "IP" : "OOP" });
      freshTree();
      await study.refresh();
    } catch (e) { fail(e); }
    finally { app.exploitBusy = false; await pollOnce(); }
  };
  $("btn-drill").onclick = () => { drillHere(study.id); setView("drill"); };
  // Fast preview: the strategy is readable now (the solve carries on). From a Build view left
  // alone since Solve, open the Study view; anywhere else, stay there and say so.
  window.addEventListener("preview-ready", (e) => {
    app.tree = true;
    const d = e.detail || {};
    if (app.view === "study") { study.refresh(); return; }   // open already: start this node's refine
    if (app.view === "build" && d.untouched && !document.querySelector("dialog[open]")) {
      setView("study");
      study.reset();
    } else if (d.solving) {
      toast(`Readable after ${fmt(d.secs, 1)} s · open Study any time`);
    }
  });
  $("btn-resolve").onclick = () => openSizes(study.id);
  $("btn-report").onclick = async () => {
    if (!app.loadedBatch) return;
    const line = study.id;
    setView("jobs");
    try { await openReport(app.loadedBatch, line); } catch (e) { fail(e); }
  };
  // info.bb: a preflop tree was built (its big blind in chips), known before the root is asked for
  initBuild({ built: async (info) => { app.tree = true; app.bb = (info && info.bb) || 0; freshTree(); await refreshRootPot(); study.reset(); } });
  initJobs({ opened: async () => {
    app.tree = true;
    freshTree();
    treeLoadedElsewhere();
    await refreshRootPot();
    setView("study");
    study.reset();
  } });

  const st = await get("/api/status");
  app.tree = st.tree;
  if (app.tree) await refreshRootPot();
  // #build, #study, or #study=<node id> (deep link to a node; also restores the spot on reload)
  const [head, ...rest] = (location.hash || "").slice(1).split("&");
  const [want, node] = head.split("=");
  applyUrlState(Object.fromEntries(rest.map((kv) => kv.split("="))));
  if (want === "study" && node && app.tree) {
    for (const name of VIEWS) $("view-" + name).classList.toggle("hidden", name !== "study");
    document.querySelectorAll(".view-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === "study"));
    app.view = "study";
    gotoNode(node);
  } else {
    setView(VIEWS.includes(want) ? want : app.tree ? "study" : "build");
  }
  // Back/forward and edited URLs: follow #build / #study=<node> without a reload.
  addEventListener("hashchange", () => {
    const [h] = location.hash.slice(1).split("&");
    const [v, id] = h.split("=");
    if (VIEWS.includes(v) && v !== "study" && app.view !== v) setView(v);
    else if (v === "study") {
      if (app.view !== "study") setView("study");
      if (id && app.tree && id !== study.id) gotoNode(id);
    }
  });
  poll();
}

boot();
