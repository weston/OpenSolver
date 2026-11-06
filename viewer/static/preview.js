// Fast preview: hide the solve time.
//  1. Solve shows a loading modal on the main solve's progress; once it is within 10x the
//     target (viewer/refine.py PREVIEW_FACTOR) the Study view opens on the still-solving tree.
//  2. The node on screen refreshes whenever the solve reports progress.
//  3. On the turn or later, the street's subtree is re-solved from its first decision (full bet
//     sizes, current ranges) and shown instead, marked "Refined", while the full solve waits.

import { get, post, prefs, fmt, toast } from "./api.js";

const $ = (id) => document.getElementById(id);
const P = {
  watching: false, t0: 0, timer: null,                // after Solve: the loading modal
  gen: 0,                                             // bumped by startPreview: an older tick chain stops (#386)
  ringBest: 0,                                        // the ring's furthest point this solve (0..1)
  solving: false,                                     // main solve running (incl. parked for a refine)
  shown: null, refineRoot: null, refineTimer: null,   // node on screen and its refine
  events: null, refresh: null,                        // last solver event count seen
};

export const previewOn = () => prefs.get("fast-preview", true) !== false;

/* ------------------------------------------------------------------ 1. the loading modal */

const RING = 326.73;   // 2 * pi * r for the modal's ring (r = 52)

function modal() { return $("solve-modal"); }

function closeModal() {
  const m = modal();
  if (m && m.open) m.close();
}

/** The ring: how far the solve's exploitability has come from its first report toward the
 *  point where the Study view opens (PREVIEW_FACTOR x the target), on a log scale - the scale
 *  exploitability falls on. Spins until the first report. It never goes back: exploitability is
 *  not monotone (threebet_mono: 15.8 -> 21.7 chips right after the average restarts at iteration
 *  33, #135), so it shows the best point so far; the status line keeps the current number. */
function drawRing(pg) {
  const ring = document.querySelector("#solve-modal .sm-ring");
  const arc = $("sm-arc");
  const pct = $("sm-pct");
  if (!ring || !arc) return;
  const { explo, first, preview_at: goal } = pg || {};
  if (!(explo > 0 && first > 0 && goal > 0)) {
    ring.classList.add("spin");
    if (pct) pct.textContent = "";
    return;
  }
  ring.classList.remove("spin");
  let f = first > goal ? Math.log(first / explo) / Math.log(first / goal) : 1;
  f = Math.max(P.ringBest, Math.min(1, f));
  P.ringBest = f;
  arc.style.strokeDashoffset = String(RING * (1 - f));
  if (pct) pct.textContent = `${Math.round(f * 100)}%`;
}

async function tick() {
  if (!P.watching) return;
  const gen = P.gen;
  let st;
  try { st = await get("/api/progress"); } catch (e) { if (gen === P.gen && P.watching) P.timer = setTimeout(tick, 600); return; }
  // dismissed ("keep solving in the background") or restarted while the request was out: this answer is
  // not for the modal any more, and must not hand off to Study (#386)
  if (!P.watching || gen !== P.gen) return;
  const pg = st.preview || {};
  const secs = (performance.now() - P.t0) / 1000;
  const status = $("sm-status");
  let ready = null;
  if (st.solving && !pg.early) {
    const pot = Number($("f-pot").value) || 0;
    drawRing(pg);
    if (status) {
      const now = pg.explo != null && pot ? `${fmt((100 * pg.explo) / pot, 2)}% of the pot` : "Starting";
      status.textContent = `${now} · ${fmt(secs, 0)} s`;
    }
  } else if (st.solving) {
    ready = `Ready in ${fmt(secs, 1)} s · still solving in the background`;
  } else {
    ready = (st.iters || 0) > 0 && /required accuracy/.test(st.solver_status || "") ? `Solved in ${fmt(secs, 1)} s` : null;
    if (!ready) { P.watching = false; closeModal(); return; }   // stopped
  }
  if (ready) {
    P.watching = false;
    drawRing({ explo: 1, first: 2, preview_at: 1 });   // full ring for the hand-off
    closeModal();
    window.dispatchEvent(new CustomEvent("preview-ready"));
    toast(ready);
    return;
  }
  P.timer = setTimeout(tick, 300);
}

/** Called right after a solve starts; on: the server follows it for fast preview (a fresh solve).
 *  Shows the solving-period modal and opens the Study view when the solve is close enough. */
export function startPreview(on) {
  clearTimeout(P.timer);
  P.gen++;
  P.watching = false;
  if (!previewOn() || !on) return;
  Object.assign(P, { watching: true, t0: performance.now(), ringBest: 0 });
  const m = modal();
  if (m) {
    drawRing(null);
    $("sm-status").textContent = "Starting";
    if (!m.open) m.showModal();
  }
  P.timer = setTimeout(tick, 200);
}

/* ------------------------------------------------------------------ 2-3. live view and street refines */

/** From the status poll: keeps the badge and the live refresh in step with the main solve. */
export function previewStatus(st) {
  P.solving = !!(st && st.solving);
  // Each progress report is when the numbers on screen and refines' freshness can change:
  // re-read the node on screen then.
  const ev = st ? st.events : null;
  if (P.solving && previewOn() && P.events != null && ev !== P.events) liveRefresh();
  P.events = ev;
  drawBadge();
}

// A live refresh rebuilds the node's action cards and matrix. Landing between a press and its
// release, it swallows the click (the button pressed is gone): during a solve about 1 press in 15
// was lost. So a refresh neither starts nor renders while a pointer is down; it carries on after
// the release, once the click has run (setTimeout), so a click's own navigation wins.
let pressed = false, deferred = false, waiters = [];
const release = () => {
  pressed = false;
  setTimeout(() => {
    const w = waiters; waiters = [];
    w.forEach((f) => f());
    if (deferred) { deferred = false; liveRefresh(); }
  }, 0);
};
// Registered only in a page: the JS unit tests import this module without a DOM (#286 review).
if (typeof document !== "undefined") {
  document.addEventListener("pointerdown", () => { pressed = true; }, true);
  document.addEventListener("pointerup", release, true);
  document.addEventListener("pointercancel", release, true);
  // a press released outside the window sends no pointerup: losing focus counts as the release
  window.addEventListener("blur", () => { if (pressed) release(); });
}
/** Resolves now, or once a pressed pointer is released and its click has run. */
export const whenReleased = () => (pressed ? new Promise((r) => waiters.push(r)) : Promise.resolve());

function liveRefresh() {
  if (!P.refresh || !P.shown || P.shown.sub) return;
  if (document.hidden || document.querySelector("dialog[open]")) return;
  if ($("view-study").classList.contains("hidden")) return;
  if (pressed) { deferred = true; return; }
  P.refresh();
}

/** The Study view calls this after every render. cur: the node's /api/strategy payload. */
export async function nodeShown(id, cur, { inSubgame, refresh }) {
  P.refresh = refresh;
  P.shown = { id, refined: cur && cur.refined ? cur.refined : null, sub: inSubgame, state: null };
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
    if (!st.busy && st.error) {
      // only on a node of that street: browsing back to the flop meanwhile must not show it there
      const s = P.shown;
      if (s && (s.id === root || s.id.startsWith(root + ":"))) { s.state = "error"; s.error = st.error; }
      drawBadge();
      return;
    }
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
    } else if (s.state === "error") {
      text = "Street re-solve failed";
      cls = "err";
      title = `Re-solving this street failed${s.error ? ": " + s.error : ""}. Showing the main solve's strategy.`;
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
  const m = modal();
  if (m) {
    const dismiss = () => {   // the solve keeps going; stay on this page
      if (!P.watching) return;
      P.watching = false;
      clearTimeout(P.timer);
      toast("Still solving in the background · open Study any time");
    };
    $("sm-close").onclick = () => { dismiss(); closeModal(); };
    m.addEventListener("cancel", dismiss);   // Esc
  }
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
