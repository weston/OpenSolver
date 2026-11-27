// Fast preview: shorten the wait for a solve, without a dialog in the way.
//  1. After Solve nothing blocks the page. A line on the Build view shows how far the main solve
//     is from a readable strategy: within 10x the target (viewer/refine.py PREVIEW_FACTOR). At
//     that point the Study view opens by itself, but only if the Build view was left alone since
//     Solve. The Study view is open all the time; until then its badge says the strategy is early.
//  2. The node on screen refreshes whenever the solve reports progress.
//  3. On the turn or later, the street's subtree is re-solved from its first decision (full bet
//     sizes, current ranges) and shown instead, marked "Refined", while the full solve waits.

import { get, post, prefs, fmt } from "./api.js";

const $ = (id) => document.getElementById(id);
const P = {
  watching: false, t0: 0, timer: null,                // after Solve: the wait for a readable strategy
  gen: 0,                                             // bumped by startPreview: an older tick chain stops (#386)
  best: 0,                                            // the bar's furthest point this solve (0..1)
  pg: null,                                           // the wait's latest numbers (/api/progress "preview")
  touched: false,                                     // the Build view was used or left since Solve
  solving: false,                                     // main solve running (incl. parked for a refine)
  shown: null, refineRoot: null, refineTimer: null,   // node on screen and its refine
  events: null, refresh: null,                        // last solver event count seen
};

export const previewOn = () => prefs.get("fast-preview", true) !== false;

/* ------------------------------------------------------------------ 1. the wait for a readable strategy */

const potNow = () => Number($("f-pot").value) || 0;
const pctOfPot = (chips) => `${fmt((100 * chips) / potNow(), 2)}%`;

/** How far the solve's exploitability has come from its first report toward the point where the
 *  strategy counts as readable (PREVIEW_FACTOR x the target), on a log scale - the scale
 *  exploitability falls on. It never goes back: exploitability is not monotone (threebet_mono:
 *  15.8 -> 21.7 chips right after the average restarts at iteration 33, #135), so the bar shows
 *  the best point so far; the text keeps the current number. Null until the first report. */
function waitFraction(pg) {
  const { explo, first, preview_at: goal } = pg || {};
  if (!(explo > 0 && first > 0 && goal > 0)) return null;
  const f = first > goal ? Math.log(first / explo) / Math.log(first / goal) : 1;
  P.best = Math.max(P.best, Math.min(1, f));
  return P.best;
}

/** The Build view's line under the Solve buttons: a thin bar and the numbers. Empty when no wait. */
function drawWait() {
  const box = $("preview-info");
  if (!box) return;
  if (!P.watching) { box.textContent = ""; return; }
  const pg = P.pg || {};
  const f = waitFraction(pg);
  let bar = box.querySelector(".pv-bar"), text = box.querySelector(".pv-text");
  if (!bar) {
    box.textContent = "";
    bar = document.createElement("div"); bar.className = "pv-bar"; bar.appendChild(document.createElement("i"));
    text = document.createElement("div"); text.className = "pv-text";
    box.append(bar, text);
  }
  bar.classList.toggle("wait", f == null);
  bar.firstChild.style.width = f == null ? "" : `${Math.round(f * 100)}%`;
  const secs = fmt((performance.now() - P.t0) / 1000, 0);
  text.textContent = pg.explo != null && pg.preview_at && potNow()
    ? `Now ${pctOfPot(pg.explo)} of the pot · Browse opens at ${pctOfPot(pg.preview_at)} · ${secs} s`
    : `Starting · ${secs} s`;
}

function endWait() {
  P.watching = false;
  clearTimeout(P.timer);
  drawWait();
  drawBadge();
}

async function tick() {
  if (!P.watching) return;
  const gen = P.gen;
  if ($("view-build").classList.contains("hidden")) P.touched = true;   // left the Build view
  let st;
  try { st = await get("/api/progress"); } catch (e) { if (gen === P.gen && P.watching) P.timer = setTimeout(tick, 600); return; }
  if (!P.watching || gen !== P.gen) return;   // restarted while the request was out (#386)
  const pg = st.preview || {};
  if (st.solving && !pg.early) {
    P.pg = pg;
    drawWait();
    drawBadge();
    P.timer = setTimeout(tick, 400);
    return;
  }
  const solved = !st.solving && (st.iters || 0) > 0 && /required accuracy/.test(st.solver_status || "");
  const ready = st.solving || solved;   // else: stopped before the strategy was readable
  const detail = { untouched: !P.touched, secs: (performance.now() - P.t0) / 1000, solving: !!st.solving };
  endWait();
  if (ready) window.dispatchEvent(new CustomEvent("preview-ready", { detail }));
}

/** Called right after a solve starts; on: the server follows it for fast preview (a fresh solve).
 *  Starts the Build view's progress line; "preview-ready" fires when the strategy is readable. */
export function startPreview(on) {
  clearTimeout(P.timer);
  P.gen++;
  P.watching = false;
  if (previewOn() && on) Object.assign(P, { watching: true, t0: performance.now(), best: 0, pg: null, touched: false });
  drawWait();
  drawBadge();
  if (P.watching) P.timer = setTimeout(tick, 200);
}

/* ------------------------------------------------------------------ 2-3. live view and street refines */

/** From the status poll: keeps the badge and the live refresh in step with the main solve. */
export function previewStatus(st) {
  P.solving = !!(st && st.solving);
  // Each progress report is when the numbers on screen and refines' freshness can change:
  // re-read the node on screen then.
  const ev = st ? st.events : null;
  if (P.solving && (previewOn() || P.shown?.smart) && P.events != null && ev !== P.events) liveRefresh();
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
  const smart = !!cur?.refined?.smart_turn || (cur?.node?.flags || "").split(/\s+/).includes("SMART_TURN");
  const shown = { id, smart, refined: cur?.refined, sub: inSubgame, state: null };
  P.shown = shown;
  clearTimeout(P.refineTimer);
  P.refineRoot = null;
  drawBadge();
  if ((!previewOn() && !smart) || inSubgame) return;
  // not yet readable: a refine now would park the main solve to re-solve from ranges that are
  // still far off. app.js refreshes the node at "preview-ready", which comes back here.
  if (P.watching && !smart) return;
  const hasCard = id.split(":").some((t) => /^[2-9TJQKA][cdhs]$/.test(t));
  if (!hasCard) return;
  const build = prefs.get("build", {}) || {};
  const body = smart ? { id } : Object.assign({}, build, {
    id,
    accuracy_pct: Number(build.accuracy || $("f-accuracy").value) || 0.3,
    allin_threshold: build.allin_threshold ?? 67,
    add_allin_limit: build.add_allin_limit ?? 300,
  });
  let r;
  try { r = await post("/api/refine/view", body); } catch (e) { return; }
  if (P.shown !== shown) return;
  P.shown.state = r.state;
  P.refineRoot = r.root || null;
  drawBadge();
  if (r.state === "ready" && !P.shown.refined) refresh();   // finished earlier: show it
  else if (r.state === "running") watchRefine(r.root, shown);
}

function watchRefine(root, shown) {
  clearTimeout(P.refineTimer);
  const poll = async () => {
    let st;
    try { st = await get("/api/refine/status"); } catch (e) { return; }
    if (P.refineRoot !== root || P.shown !== shown) return;
    if (st.ready && st.ready.root === root && !st.busy) { if (P.refresh) P.refresh(); return; }
    if (!st.busy && !st.error) return;
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
  if (s && !s.sub && !s.smart && previewOn()) {
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
    } else if (P.watching) {
      const pg = P.pg || {};
      const known = pg.explo != null && pg.preview_at && potNow();
      text = known ? `Early · ${pctOfPot(pg.explo)} of the pot` : "Early · solve starting";
      cls = "busy";
      title = "The solve has only just started, so this strategy is still far from settled. The view updates as it improves"
        + (known ? `; it is readable from about ${pctOfPot(pg.preview_at)} of the pot.` : ".");
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
  // Using the Build view during the wait (editing the next spot, say) keeps you there: the
  // Study view then does not open by itself.
  const build = $("view-build");
  if (build) for (const ev of ["pointerdown", "keydown"]) build.addEventListener(ev, () => { P.touched = true; }, true);
  const cb = $("f-preview");
  if (cb) {
    cb.checked = previewOn();
    cb.onchange = () => {
      prefs.set("fast-preview", cb.checked);
      if (!cb.checked && !P.shown?.smart) post("/api/refine/clear", {}).catch(() => {});
      drawBadge();
    };
  }
}
