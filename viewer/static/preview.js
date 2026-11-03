// Fast preview (experiment): hide the solve time.
//  1. Solve first runs a sketch: the same tree with one bet size and no raises on the turn and
//     river (viewer/refine.py SketchManager), a few seconds even on big trees. The Study view
//     opens on it, then the full solve starts.
//  2. Flop nodes come from the sketch until the full solve's exploitability beats it, and the
//     node you look at refreshes live as the full solve improves.
//  3. On the turn or later, the street's subtree is re-solved from its first decision (full bet
//     sizes, current ranges) and shown instead, marked "Refined", while the full solve continues.

import { get, post, prefs, fmt, toast } from "./api.js";

const $ = (id) => document.getElementById(id);
const P = {
  watching: false, t0: 0, timer: null,                // after Solve: wait for the sketch
  solving: false,                                     // main solve running (incl. parked for a refine)
  shown: null, refineRoot: null, refineTimer: null,   // node on screen and its refine
  liveTimer: null, refresh: null,
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
    if (info) info.textContent = `Fast preview: sketching the flop on a simplified tree… ${fmt(secs, 0)} s`;
  } else if (sk.state === "ready") {
    ready = `Flop ready in ${fmt(secs, 1)} s · full solve running in the background`;
  } else if (st.solving && (st.iters || 0) >= 10) {
    ready = `Ready in ${fmt(secs, 1)} s · still solving in the background`;   // no sketch (e.g. not built here)
  } else if (!st.solving && secs > 1.5) {
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
  if (P.solving && previewOn() && !P.liveTimer) P.liveTimer = setInterval(liveRefresh, 3000);
  if ((!P.solving || !previewOn()) && P.liveTimer) { clearInterval(P.liveTimer); P.liveTimer = null; }
  drawBadge();
}

function liveRefresh() {
  // re-read the node on screen while the main solve improves it (not refined ones: those are done)
  if (!P.refresh || !P.shown || P.shown.refined || P.shown.sub) return;   // sketch views refresh: the full tree takes over
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
  if (P.shown.refined) return;
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
  if (r.state === "ready") refresh();       // finished earlier: show it
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
      title = `Flop strategy from a simplified tree (one bet size, no raises on the turn and river), solved to ${fmt(s.sketch.accuracy_pct, 1)}% of the pot in ${fmt(s.sketch.seconds, 1)} s. The full tree takes over once its solve is more accurate.`;
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
