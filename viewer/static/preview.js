// Fast preview: hide the solve time.
//  1. Solve first runs a sketch: the same tree with one bet size and no raises on the turn and
//     river (viewer/refine.py SketchManager), solved to the build's target. The Study view
//     opens on it, then the full solve starts.
//  2. Each flop node comes from the sketch until the full tree's strategy there has caught up
//     with it (the server decides at each progress report), and the node on screen refreshes
//     whenever the full solve reports progress.
//  3. On the turn or later, the street's subtree is re-solved from its first decision (full bet
//     sizes, current ranges) and shown instead, marked "Refined", while the full solve continues.

import { get, post, prefs, fmt, toast } from "./api.js";

const $ = (id) => document.getElementById(id);
const P = {
  watching: false, t0: 0, timer: null,                // after Solve: wait for the sketch
  solving: false,                                     // main solve running (incl. parked for a refine)
  shown: null, refineRoot: null, refineTimer: null,   // node on screen and its refine
  events: null, refresh: null,                        // last solver event count seen
};

export const previewOn = () => prefs.get("fast-preview", true) !== false;

/* ------------------------------------------------------------------ 1. open on the sketch */

async function tick() {
  if (!P.watching) return;
  let st;
  try { st = await get("/api/progress"); } catch (e) { P.timer = setTimeout(tick, 600); return; }
  const sk = st.sketch || { state: "idle" };
  const info = $("preview-info");
  const secs = (performance.now() - P.t0) / 1000;
  let ready = null;
  if (sk.state === "running") {
    const pg = sk.progress || {};
    const pot = Number($("f-pot").value) || 0;
    const now = pg.explo != null && pot ? ` · ${fmt((100 * pg.explo) / pot, 2)}% of the pot` : "";
    const goal = pg.target != null && pot ? ` → ${fmt((100 * pg.target) / pot, 2)}%` : "";
    if (info) info.textContent = `Fast preview: sketching the flop on a simplified tree${now}${goal} · ${fmt(secs, 0)} s`;
  } else if (sk.state === "ready") {
    ready = `Flop ready in ${fmt(secs, 1)} s · full solve running in the background`;
  } else if (st.solving) {
    ready = "Solving in the background";   // no sketch (a resumed solve, or the sketch failed)
  } else if ((st.iters || 0) > 0) {
    ready = `Solved in ${fmt(secs, 1)} s`;
  }
  if (ready) {
    P.watching = false;
    if (info) info.textContent = "";
    window.dispatchEvent(new CustomEvent("preview-ready"));
    toast(ready);
    return;
  }
  P.timer = setTimeout(tick, 400);
}

/** Called right after a solve starts. */
export function startPreview() {
  if (!previewOn()) return;
  clearTimeout(P.timer);
  Object.assign(P, { watching: true, t0: performance.now() });
  P.timer = setTimeout(tick, 300);
}

/* ------------------------------------------------------------------ 2-3. live view and street refines */

/** From the status poll: keeps the badge and the live refresh in step with the main solve. */
export function previewStatus(st) {
  P.solving = !!(st && st.solving);
  // Each progress report is when the full solve's numbers (and the server's choice between
  // sketch and full tree, and refines' freshness) can change: re-read the node on screen then.
  const ev = st ? st.events : null;
  if (P.solving && previewOn() && P.events != null && ev !== P.events) liveRefresh();
  P.events = ev;
  drawBadge();
}

function liveRefresh() {
  if (!P.refresh || !P.shown || P.shown.sub) return;
  if (document.hidden || document.querySelector("dialog[open]")) return;
  if ($("view-study").classList.contains("hidden")) return;
  P.refresh();
}

/** The Study view calls this after every render. cur: the node's /api/strategy payload. */
export async function nodeShown(id, cur, { inSubgame, refresh }) {
  P.refresh = refresh;
  P.shown = { id, refined: cur && cur.refined ? cur.refined : null, sketch: cur && cur.sketch ? cur.sketch : null,
              sub: inSubgame, state: null };
  drawBadge();
  if (!previewOn() || inSubgame) return;
  const hasCard = id.split(":").some((t) => /^[2-9TJQKA][cdhs]$/.test(t));
  if (!hasCard) return;
  const build = prefs.get("build", {}) || {};
  const body = Object.assign({}, build, {
    id,
    accuracy_pct: Number(build.accuracy || $("f-accuracy").value) || 0.3,
    allin_threshold: build.allin_threshold ?? 67,
    add_allin_limit: build.add_allin_limit ?? 300,
  });
  let r;
  try { r = await post("/api/refine/view", body); } catch (e) { return; }
  if (!P.shown || P.shown.id !== id) return;
  P.shown.state = r.state;
  P.refineRoot = r.root || null;
  drawBadge();
  if (r.state === "ready" && !P.shown.refined) refresh();   // finished earlier: show it
  else if (r.state === "running") watchRefine(r.root);
}

function watchRefine(root) {
  clearTimeout(P.refineTimer);
  const poll = async () => {
    let st;
    try { st = await get("/api/refine/status"); } catch (e) { return; }
    if (P.refineRoot !== root) return;
    if (st.ready && st.ready.root === root && !st.busy) { if (P.refresh) P.refresh(); return; }
    if (!st.busy && st.error) { if (P.shown) P.shown.state = "error"; drawBadge(); return; }
    P.refineTimer = setTimeout(poll, 500);
  };
  P.refineTimer = setTimeout(poll, 400);
}

function drawBadge() {
  const b = $("live-tag");
  if (!b) return;
  const s = P.shown;
  let text = "", cls = "", title = "";
  if (s && !s.sub && previewOn()) {
    if (s.refined) {
      text = `Refined · ${s.refined.street} re-solved to ${fmt(s.refined.accuracy_pct, 2)}%`;
      cls = "ok";
      title = `This street was re-solved from its first decision in ${fmt(s.refined.seconds, 1)} s with the current ranges (opponent range fixed there). The main solve continues in the background.`;
    } else if (s.sketch) {
      text = "Sketch · full solve running";
      cls = "busy";
      title = `Flop strategy from a simplified tree (one bet size, no raises on the turn and river), solved to your target (${fmt(s.sketch.accuracy_pct, 2)}% of the pot) in ${fmt(s.sketch.seconds, 1)} s. The full tree takes over this node once its strategy here has caught up with the sketch's.`;
    } else if (s.state === "running") {
      text = "Refining this street…";
      cls = "busy";
      title = "Re-solving from this street's first decision with the current ranges; the main solve is paused meanwhile.";
    } else if (P.solving) {
      text = "Live · still solving";
      cls = "busy";
      title = "The tree is still solving in the background; this view updates as it improves.";
    }
  }
  b.textContent = text;
  b.title = title;
  b.className = `tag ${cls}`.trim();
  b.classList.toggle("hidden", !text);
}

export function initPreview() {
  const cb = $("f-preview");
  if (cb) {
    cb.checked = previewOn();
    cb.onchange = () => {
      prefs.set("fast-preview", cb.checked);
      if (!cb.checked) post("/api/refine/clear", {}).catch(() => {});
      drawBadge();
    };
  }
}
