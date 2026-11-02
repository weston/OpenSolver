// WSolver viewer: boot, view switching, solver polling, top bar and file dialog.
// ES modules, no build step. Talks to viewer/server.py.

import { initHands } from "./cards.js";
import { get, post, toast, fail, prefs, fmt } from "./api.js";
import { initStudy, gotoNode, showEmpty, study, applyUrlState } from "./study.js";
import { initBuild, updateProgress, treeLoadedElsewhere } from "./build.js";
import { drawSpark } from "./chart.js";
import { initLibrary } from "./library.js";
import { initResolve, openResolve } from "./resolve.js";

const $ = (id) => document.getElementById(id);

const app = { view: "study", tree: false, solving: false, rootPot: 0, series: [], lastEvents: -1 };

/* ------------------------------------------------------------------ views */

function setView(v) {
  app.view = v;
  for (const name of ["study", "build"]) $("view-" + name).classList.toggle("hidden", name !== v);
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

async function refreshRootPot() {
  try {
    const p = await get("/api/path?id=r:0");
    const n = p.nodes[0];
    app.rootPot = (n.pot || []).reduce((a, b) => a + (b || 0), 0);
  } catch (e) { app.rootPot = 0; }
}

async function poll() {
  let st;
  try { st = await get("/api/progress"); } catch (e) { setPill("offline", "Server offline"); schedule(); return; }
  const wasSolving = app.solving;
  app.solving = st.solving;
  if (st.tree && !app.tree) { app.tree = true; await refreshRootPot(); }
  if (!st.tree) app.tree = false;
  if (st.events !== app.lastEvents || st.solving) {
    app.lastEvents = st.events;
    try { app.series = (await get("/api/convergence")).points; } catch (e) { /* keep last */ }
  }
  const last = app.series[app.series.length - 1];
  const pct = last && app.rootPot ? (last.explo / app.rootPot) * 100 : null;
  if (st.dead) setPill("err", "Solver stopped working — restart from Open");
  else if (st.solving) setPill("solving", `Solving · ${pct != null ? fmt(pct, 2) + "%" : "…"} · ${last ? fmt(last.t, 0) + "s" : "0s"}`);
  else if (last) setPill("done", `Solved · ${fmt(pct, 2)}% pot`);
  else setPill("", app.tree ? "Tree ready" : "Idle");
  drawSpark($("pill-spark"), app.series, app.rootPot);
  updateProgress(st, app.series, app.rootPot);

  if (wasSolving && !st.solving) {
    toast(pct != null ? `Solve finished at ${fmt(pct, 2)}% of the pot` : "Solve finished");
    if (app.view === "study" && app.tree) study.refresh();
  }
  schedule();
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
  $("file-path").value = prefs.get("path", "tree.cfr");
  dlg.returnValue = "";
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    const path = $("file-path").value.trim();
    prefs.set("path", path);
    try {
      if (mode === "open") {
        await post("/api/load", { path });
        app.tree = true;
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
    } catch (e) { fail(e); }
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
  $("btn-resolve").onclick = () => openResolve(study.id);
  initBuild({ built: async () => { app.tree = true; await refreshRootPot(); study.reset(); } });

  const st = await get("/api/status");
  app.tree = st.tree;
  if (app.tree) await refreshRootPot();
  // #build, #study, or #study=<node id> (deep link to a node; also restores the spot on reload)
  const [head, ...rest] = (location.hash || "").slice(1).split("&");
  const [want, node] = head.split("=");
  applyUrlState(Object.fromEntries(rest.map((kv) => kv.split("="))));
  if (want === "study" && node && app.tree) {
    for (const name of ["study", "build"]) $("view-" + name).classList.toggle("hidden", name !== "study");
    document.querySelectorAll(".view-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === "study"));
    app.view = "study";
    gotoNode(node);
  } else {
    setView(want === "build" || want === "study" ? want : app.tree ? "study" : "build");
  }
  // Back/forward and edited URLs: follow #build / #study=<node> without a reload.
  addEventListener("hashchange", () => {
    const [h] = location.hash.slice(1).split("&");
    const [v, id] = h.split("=");
    if (v === "build" && app.view !== "build") setView("build");
    else if (v === "study") {
      if (app.view !== "study") setView("study");
      if (id && app.tree && id !== study.id) gotoNode(id);
    }
  });
  poll();
}

boot();
