// Study view (#112): browse a solved tree. The layout is fixed: board + action line on top,
// action strip and 13x13 matrix in the middle, details on the right. Navigating only swaps
// data inside these regions, so nothing moves while you click through a line.

import { H, RANKS, SUITS, GLYPH, el, cardEl, comboEl, parseCards, blockedBy, displayCombo } from "./cards.js";
import { prepare as prepareBlockers, comboReport, shareChanges, cardEffects } from "./blockers.js";
import { curves as rangeCurves, at as rangeAt, shareAbove } from "./graphs.js";
import { sideCache } from "./sidecache.js";
import { nodeShown, whenReleased } from "./preview.js";
import { get, q, fail, toast, showTip, hideTip, fmt, fmtChips, fmtEvPct, prefs } from "./api.js";
import { rebuildWithBoard } from "./build.js";
import { syncFurther } from "./further.js";

const $ = (id) => document.getElementById(id);

const S = {
  id: "r:0",
  path: [],          // node blocks root..current (from /api/path)
  eff: 0,            // effective stack
  cur: null,         // /api/strategy response for the current node (decision or not)
  dec: null,         // strategy data shown in the matrix (current decision or last one above it)
  decId: null,
  mode: "strategy",  // strategy | range | ev
  player: null,      // for range / ev; null = the actor
  range: null,       // /api/range of the current node
  ev: {},            // player -> /api/ev of the current node
  pinned: null,      // class pinned by click
  hover: null,
  catsPlayer: null,  // OOP/IP override in that tab; null = the actor
  catHl: null,       // Set of classes highlighted by a hovered category row
  catPin: null,      // {name, classes} pinned by clicking a category row
  blkCombo: null,    // the hand the Blockers tab shows ("AsKd"), or null
  blkFor: null,      // the player it was picked for (it only applies at that player's decisions)
  blkMetric: null,   // what the card ranking sorts by
  blkNoBoardRanks: prefs.get("blk-no-board-ranks", false),  // card grid: leave out cards that pair the board
  blkView: prefs.get("blk-view", "made"),  // range shift by "made" category, "draw" or hand "classes"
  blkRemovedOpen: false,  // the blocked-combos list is unfolded
  grMetric: prefs.get("graph-metric", "eq") === "ev" ? "ev" : "eq",
  side: "hand",
  seq: 0,            // navigation sequence, drops stale responses
  loaded: false,
  base: "/api",      // "/api/resolve" while browsing a re-solved subtree (#117)
  sub: null,         // {origin, root, label} in subtree mode
};

/* ------------------------------------------------------------------ actions */

const COL = { fold: "#5b8def", check: "#4cc38a", call: "#4cc38a", allin: "#9b51e0" };
const BET_STOPS = [[0.15, [246, 196, 69]], [0.5, [243, 146, 55]], [1.0, [232, 83, 74]], [2.0, [194, 54, 110]]];

/** The Study colour of a root action token (f, c, b<amount>) in a pot of `pot` chips; for
 *  the job report (jobs.js), which has no tree to ask. */
export function tokenColor(tok, pot) {
  if (tok === "f") return COL.fold;
  if (tok === "c") return COL.check;
  const m = /^[br](\d+)$/.exec(tok);
  return m && pot > 0 ? betColor(Number(m[1]) / pot) : "#8892a6";
}

function betColor(f) {
  if (f <= BET_STOPS[0][0]) return rgb(BET_STOPS[0][1]);
  for (let i = 1; i < BET_STOPS.length; i++) {
    const [x1, c1] = BET_STOPS[i];
    const [x0, c0] = BET_STOPS[i - 1];
    if (f <= x1) {
      const t = (f - x0) / (x1 - x0);
      return rgb(c0.map((v, k) => v + (c1[k] - v) * t));
    }
  }
  return rgb(BET_STOPS[BET_STOPS.length - 1][1]);
}
const rgb = (c) => `rgb(${c.map((v) => Math.round(v)).join(",")})`;

const pctLabel = (f) => `${Math.round(f * 100)}%`;
const isDec = (n) => n && (n.type === "OOP_DEC" || n.type === "IP_DEC");
const actorOf = (n) => (n.type === "OOP_DEC" ? "OOP" : "IP");
const lastTok = (id) => id.slice(id.lastIndexOf(":") + 1);
const potOf = (n) => (n.pot || []).reduce((a, b) => a + (b || 0), 0);
const lockOf = (n) => {
  const f = (n && n.flags || "").split(/\s+/);
  return f.includes("LOCKED") ? "node" : f.includes("COMBO_LOCKED") ? "combos" : null;
};

/** Street-start investment at node index i of the path (both players matched there). */
function streetBase(path, i) {
  const len = (n) => parseCards(n.board).length;
  let j = i;
  while (j > 0 && len(path[j - 1]) === len(path[i])) j--;
  return Math.min(path[j].pot[0] || 0, path[j].pot[1] || 0);
}

/** Describe the action leading from `parent` (path index pi) via token `tok`. */
function describe(parent, tok, pi, path) {
  const p = parent.pot || [0, 0, 0];
  const a = parent.type === "OOP_DEC" ? 0 : 1;
  const own = p[a] || 0, facing = Math.max(p[0] || 0, p[1] || 0), pot = potOf(parent);
  if (tok === "f") return { kind: "fold", label: "Fold", sub: "", color: COL.fold, frac: -1 };
  if (tok === "c") {
    if (facing > own) return { kind: "call", label: "Call", sub: fmtChips(facing - own), color: COL.call, frac: 0 };
    return { kind: "check", label: "Check", sub: "", color: COL.check, frac: 0 };
  }
  if (tok[0] === "b") {
    const amt = Number(tok.slice(1));
    const base = path ? streetBase(path, pi) : 0;
    const allin = S.eff > 0 && amt >= S.eff - 1e-9;
    if (facing > own) {
      const potAfterCall = pot + (facing - own);
      const frac = (amt - facing) / potAfterCall;
      const mult = facing - base > 0 ? (amt - base) / (facing - base) : 0;
      // Headline size is always % of the pot (owner): a raise's size is its increment over
      // the call, as a fraction of the pot after calling; chips and the multiple go below.
      return {
        kind: allin ? "allin" : "raise", label: allin ? "All-in" : `Raise ${pctLabel(frac)}`,
        sub: allin ? fmtChips(amt - base) : `to ${fmtChips(amt - base)} · ${mult.toFixed(1)}x`,
        color: allin ? COL.allin : betColor(frac), frac: 10 + frac,
      };
    }
    const frac = (amt - own) / pot;
    return {
      kind: allin ? "allin" : "bet", label: allin ? "All-in" : `Bet ${pctLabel(frac)}`,
      sub: fmtChips(amt - own), color: allin ? COL.allin : betColor(frac), frac,
    };
  }
  return { kind: "other", label: tok, sub: "", color: "#8892a6", frac: 99 };
}

/** Actions at a decision node in display order (owner, #141): most aggressive on the left,
 *  most passive on the right: all-in, bets/raises large -> small, check/call, fold. */
function actionsOf(data, pi, path = S.path) {
  const node = data.node;
  const acts = data.children.map((c, k) => ({ k, id: c.id, ...describe(node, lastTok(c.id), pi, path) }));
  const rank = (x) => (x.kind === "fold" ? -2 : x.kind === "check" || x.kind === "call" ? -1 : x.kind === "allin" ? 1e9 : x.frac);
  return acts.sort((x, y) => rank(y) - rank(x));
}

/* ------------------------------------------------------------------ data */

function boardOf(n) { return parseCards(n && n.board); }

function classWeight(w, cls, blocked) {
  let s = 0, n = 0;
  for (const i of H.byClass[cls]) {
    if (blocked(H.order[i])) continue;
    n++; s += w[i] || 0;
  }
  return { w: n ? s / n : 0, combos: s, n };
}

function overallFreqs(d, acts) {
  let tot = 0;
  const f = acts.map(() => 0);
  d.reach.forEach((w, i) => {
    if (!w) return;
    tot += w;
    acts.forEach((a, j) => { f[j] += w * (d.freq[a.k][i] || 0); });
  });
  return { freqs: f.map((x) => (tot > 0 ? x / tot : 0)), combos: f, total: tot };
}

export async function gotoNode(id) {
  const seq = ++S.seq;
  S.target = id;   // the latest navigation's node (a live refresh must not override a click in flight)
  try {
    const p = await get(`${S.base}/path?id=` + q(id));
    const node = p.nodes[p.nodes.length - 1];
    const cur = await get(`${S.base}/strategy?id=` + q(id));
    // The matrix keeps showing a decision: this one, or the closest one above it.
    let dec = null, decId = null;
    if (isDec(node) && cur.freq) { dec = cur; decId = id; } else {
      for (let i = p.nodes.length - 2; i >= 0; i--) {
        if (isDec(p.nodes[i])) {
          decId = p.nodes[i].id;
          dec = decId === S.decId && S.dec ? S.dec : await get(`${S.base}/strategy?id=` + q(decId));
          break;
        }
      }
    }
    if (seq !== S.seq) return;
    Object.assign(S, { id, path: p.nodes, eff: p.eff_stack || 0, cur, dec, decId, range: null, ev: {}, loaded: true });
    if (S.pinned && !Object.prototype.hasOwnProperty.call(H.byClass, S.pinned)) S.pinned = null;
    // a pinned/hovered category is board-dependent: it does not survive navigation
    S.catPin = null; S.catHl = null;
    if (S.mode !== "strategy") await loadModeData();
    await whenReleased();   // never rebuild the cards under a press (preview.js)
    if (seq !== S.seq) return;
    render();
    nodeShown(id, cur, { inSubgame: !!S.sub, refresh: () => { if (S.loaded && S.id === id && S.target === id) { invalidate(); gotoNode(id); } } });
    if (!S.sub && !document.getElementById("view-study").classList.contains("hidden")) history.replaceState(null, "", "#study=" + id + urlState());
  } catch (e) {
    if (seq === S.seq) fail(e);
  }
}

/** The current node's ranges, fetched once. A late answer for a node left meanwhile is dropped: assigning
 *  S.range after the await used to put node A's ranges on node B (#369). */
async function loadRange() {
  if (S.range) return S.range;
  const id = S.id, base = S.base;
  const r = await get(`${base}/range?id=` + q(id));
  if (S.id === id && S.base === base) S.range = r;
  return r;
}

/** Forget what was read from the tree: the decision above a deal/end node and the categories. A
 *  refresh (solve finished, a lock, a refine, a new board) must not reuse them (#369). */
function invalidate() {
  S.dec = null;
  S.decId = null;
  side.invalidate();
  for (const k of ["side-cats", "side-blockers", "side-graphs"]) {
    const box = $(k);
    if (box) box.dataset.key = "";
  }
}

/** The Cats / Blockers / Graphs tabs' data: one ask per node at a time, none cached across a refresh. */
const side = sideCache(get, fail);
const sideData = (slot, key, url, build) => side.load(slot, key, url, build);

async function loadModeData() {
  await loadRange();
  if (S.mode === "ev") {
    const pl = viewPlayer();
    if (!S.ev[pl]) S.ev[pl] = await get(`${S.base}/ev?id=${q(S.id)}&player=${pl}`);
  }
}

function viewPlayer() {
  if (S.player) return S.player;
  const n = S.path[S.path.length - 1];
  if (isDec(n)) return actorOf(n);
  return S.dec ? S.dec.actor : "OOP";
}

/* ------------------------------------------------------------------ rendering */

function render() {
  $("empty-state").classList.add("hidden");
  renderBoard();
  renderPath();
  renderPotInfo();
  renderTitle();
  renderStrip();
  renderMatrix();
  renderSide();
}

function renderBoard() {
  const b = $("board");
  b.textContent = "";
  const cards = boardOf(S.path[S.path.length - 1]);
  cards.forEach((c, i) => {
    if (i === 3 || i === 4) b.appendChild(el("span", "gap"));
    const ce = cardEl(c, "lg");
    if (!S.sub) {   // click a card to change it (not inside a re-solved subtree)
      ce.classList.add("pickable");
      const root = boardOf(S.path[0]).length;
      ce.title = i < root ? `Change the ${root === 3 ? "flop" : "board"}: rebuild and solve on other cards`
                          : `Change the ${i === 3 ? "turn" : "river"} card`;
      ce.onclick = () => openBoardPicker(i);
    }
    b.appendChild(ce);
  });
  for (let i = cards.length; i < 5 && cards.length >= 3; i++) {
    if (i === 3 || i === 4) b.appendChild(el("span", "gap"));
    const ph = el("span", "pc lg placeholder");
    ph.appendChild(el("span", "r", i === 3 ? "T" : "R"));
    ph.title = i === 3 ? "turn" : "river";
    b.appendChild(ph);
  }
}

/* ------------------------------------------------------------------ board picker */

/** Click on board card i. A root card (the flop, on a flop tree): pick new root cards and the same
 *  tree is rebuilt and solved on them (build.js rebuildWithBoard; fast preview as usual). A dealt
 *  card (turn / river): pick another card of the same deal; the view moves to that branch, keeping
 *  the actions after it where the new branch has them. */
async function openBoardPicker(i) {
  const dlg = $("card-dialog");
  if (!dlg || S.sub || !S.path.length) return;
  const root = boardOf(S.path[0]);
  const shown = boardOf(S.path[S.path.length - 1]);
  const deck = $("cd-deck");
  const apply = $("cd-apply");
  let pickable = null, picked, onPick;
  if (i < root.length) {
    picked = [...root];
    const n = root.length;
    $("cd-title").textContent = n === 3 ? "Change the flop" : "Change the board";
    $("cd-hint").textContent = `Pick ${n} cards. The same tree (ranges, bet sizes, settings) is rebuilt on them and solved again.`;
    apply.classList.remove("hidden");
    const same = () => picked.length === n && picked.every((c) => root.includes(c));
    const sync = () => { apply.disabled = picked.length !== n || same(); };
    onPick = (card) => {
      const k = picked.indexOf(card);
      if (k >= 0) picked.splice(k, 1);
      else if (picked.length < n) picked.push(card);
      else { picked.pop(); picked.push(card); }   // full: replace the last pick
      draw();
      sync();
    };
    apply.onclick = async () => {
      dlg.close();
      if (!(await rebuildWithBoard(picked))) toast("Busy: try again in a moment");
    };
    sync();
  } else {
    const chance = S.path.find((n) => n.type === "SPLIT_NODE" && boardOf(n).length === i);
    if (!chance) return;
    let kids;
    try { kids = (await get(`${S.base}/strategy?id=` + q(chance.id))).children || []; } catch (e) { fail(e); return; }
    pickable = new Map(kids.map((c) => [lastTok(c.id), c.id]));
    picked = [shown[i]];
    $("cd-title").textContent = `Change the ${i === 3 ? "turn" : "river"}`;
    $("cd-hint").textContent = "Same tree, another card: the view stays on this line where the new branch has it.";
    apply.classList.add("hidden");
    onPick = (card) => { dlg.close(); switchDealt(chance, card); };
  }
  function draw() {
    deck.textContent = "";
    for (const s of SUITS) for (const r of RANKS) {
      const card = r + s;
      const b = el("button");
      b.style.background = `var(--suit-${s})`;
      b.append(el("span", null, r), el("span", null, GLYPH[s]));
      b.title = card;
      b.classList.toggle("picked", picked.includes(card));
      if (pickable && !pickable.has(card)) b.disabled = true;
      else b.onclick = () => onPick(card);
      deck.appendChild(b);
    }
  }
  draw();
  $("cd-cancel").onclick = () => dlg.close();
  if (!dlg.open) dlg.showModal();
}

/** Move to the same line under another card of the deal at `chance`: drop trailing actions the new
 *  branch does not have (a later card equal to the new one, a size that is not there, ...). */
async function switchDealt(chance, card) {
  const base = chance.id.split(":");
  const rest = S.path[S.path.length - 1].id.split(":").slice(base.length + 1);
  const toks = [...base, card, ...rest];
  while (toks.length > base.length + 1) {
    try { await get(`${S.base}/node?id=` + q(toks.join(":"))); break; } catch (e) { toks.pop(); }
  }
  gotoNode(toks.join(":"));
}

const STREET = ["", "", "", "FLOP", "TURN", "RIVER"];

function renderPath() {
  const box = $("path");
  box.textContent = "";
  const P = S.path;
  let lastLen = -1;
  // Back: up one node (same as the left-arrow key), disabled at the root.
  const back = el("button", "path-back", "‹");
  back.title = "Up one node (←)";
  back.disabled = P.length <= 1 && !S.sub;
  back.onclick = goUp;
  box.appendChild(back);
  if (S.sub) {
    const back = el("button", "step origin");
    back.append(el("span", null, "↩ Original"), el("span", "sub", S.sub.label));
    back.title = "Leave the re-solved subtree and return to " + S.sub.origin;
    back.onclick = exitSubgame;
    box.appendChild(back);
  }
  const mark = (n) => {
    const cards = boardOf(n);
    if (cards.length === lastLen) return;
    lastLen = cards.length;
    const m = el("div", "street-mark", STREET[cards.length] || "PREFLOP");
    if (cards.length >= 4) m.appendChild(cardEl(cards[cards.length - 1], "sm"));
    box.appendChild(m);
  };
  // In a re-solved subtree, nodes above the subgame root are the backend's forced-check
  // wrapper (IP roots), not real actions: skip them.
  const first = S.sub ? Math.max(0, P.findIndex((p) => p.id === S.sub.root)) : 0;
  for (let i = first; i < P.length - 1; i++) {
    const n = P[i];
    if (!isDec(n)) continue; // deals show up as street marks
    mark(n);
    const d = describe(n, lastTok(P[i + 1].id), i, P);
    const b = el("button", "step");
    const bar = el("span", "bar"); bar.style.background = d.color;
    b.append(bar, el("span", "who", actorOf(n)), el("span", null, d.label));
    if (d.sub) b.appendChild(el("span", "sub", d.sub));
    b.title = "Go to this decision";
    if (lockOf(n)) { b.classList.add("locked"); b.title += ` (${actorOf(n)}'s strategy here is locked)`; }
    b.onclick = () => gotoNode(n.id);
    box.appendChild(b);
  }
  const cur = P[P.length - 1];
  mark(cur);
  const now = el("div", "step now");
  if (isDec(cur)) now.textContent = `${actorOf(cur)} to act`;
  else if (cur.type === "SPLIT_NODE") now.textContent = `Deal ${boardOf(cur).length === 3 ? "turn" : "river"}`;
  else if (cur.type === "END_NODE") now.textContent = endText(cur);
  else now.textContent = cur.type;
  box.appendChild(now);
  requestAnimationFrame(() => { box.scrollLeft = box.scrollWidth; });
}

function endText(n) {
  return lastTok(n.id) === "f" ? "Fold — hand over" : "Showdown";
}

function renderPotInfo() {
  const n = S.path[S.path.length - 1];
  const pot = potOf(n);
  const behind = S.eff ? S.eff - Math.max(n.pot[0] || 0, n.pot[1] || 0) : null;
  const box = $("potinfo");
  box.textContent = "";
  const kv = (k, v) => { const d = el("div", "kv"); d.append(el("span", "k", k), el("span", "v num", v)); box.appendChild(d); };
  kv("Pot", fmtChips(pot));
  if (behind != null) kv("Behind", fmtChips(behind));
  if (behind != null && pot > 0) kv("SPR", (behind / pot).toFixed(2));
  const evs = {};
  for (const pl of ["OOP", "IP"]) {
    const d = el("div", "kv");
    const k = el("span", "k");
    k.append(el("span", `who ${pl.toLowerCase()}`, pl), document.createTextNode(" EV"));
    evs[pl] = el("span", "v num", "…");
    d.append(k, evs[pl]);
    d.title = `${pl}'s range EV as % of the pot (matchup-weighted, like Pio's)`;
    box.appendChild(d);
  }
  fillRangeEvs(evs, pot);
}

/** Each range's EV at this node as % of the pot, filled in once /api/ev answers ("—" where there is none,
 *  e.g. at a fold or showdown). */
async function fillRangeEvs(evs, pot) {
  const seq = S.seq, id = S.id;
  try {
    for (const pl of ["OOP", "IP"]) {
      if (!S.ev[pl]) S.ev[pl] = await get(`${S.base}/ev?id=${q(id)}&player=${pl}`);
    }
    await loadRange();
  } catch (e) {
    if (seq === S.seq) for (const pl of ["OOP", "IP"]) evs[pl].textContent = "—";
    return;
  }
  if (seq !== S.seq || S.id !== id) return;
  for (const pl of ["OOP", "IP"]) {
    const e = S.ev[pl], w = pl === "OOP" ? S.range.oop : S.range.ip;
    let num = 0, den = 0;
    for (let i = 0; i < 1326; i++) {
      // matchups already include the hand's reach (see the matrix's EV mode)
      const mu = e.matchups ? e.matchups[i] : w[i];
      if (e.ev[i] == null || !(mu > 0) || !(w[i] > 0)) continue;
      num += mu * e.ev[i]; den += mu;
    }
    evs[pl].textContent = den > 0 ? fmtEvPct(num / den, pot) : "—";
  }
}

function renderTitle() {
  const n = S.path[S.path.length - 1];
  const t = $("node-title");
  t.textContent = "";
  if (isDec(n)) {
    const a = actorOf(n);
    t.append(el("span", `badge ${a.toLowerCase()}`, a), el("span", null, "to act"));
    const p = n.pot, own = p[a === "OOP" ? 0 : 1], facing = Math.max(p[0], p[1]);
    t.appendChild(el("span", "sub", facing > own ? `facing ${fmtChips(facing - own)} into ${fmtChips(potOf(n) - (facing - own))}` : "first to act or checked to"));
  } else if (n.type === "SPLIT_NODE") {
    t.append(el("span", "badge neutral", "DEAL"), el("span", null, `Choose the ${boardOf(n).length === 3 ? "turn" : "river"}`));
  } else if (n.type === "END_NODE") {
    t.append(el("span", "badge neutral", "END"), el("span", null, endText(n)));
  } else {
    t.append(el("span", "badge neutral", n.type));
  }
  // re-solving is offered on decisions of the original tree
  $("btn-resolve").classList.toggle("hidden", !(isDec(n) && !S.sub && S.cur && S.cur.freq));
  syncFurther(isDec(n) && !S.sub && S.cur && S.cur.freq);
  $("btn-lock").classList.toggle("hidden", !(isDec(n) && !S.sub && S.cur && S.cur.freq));
  $("btn-report").classList.toggle("hidden", !(reportBatch && isDec(n) && !S.sub));
  const lk = isDec(n) ? lockOf(n) : null;
  $("lock-tag").classList.toggle("hidden", !lk);
  $("lock-tag").textContent = lk === "combos" ? "Some hands locked" : "Locked";
  $("subgame-tag").classList.toggle("hidden", !S.sub);
  // player toggle only matters for range / EV
  const seg = $("player-seg");
  seg.classList.toggle("hidden", S.mode === "strategy");
  seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.player === viewPlayer()));
}

function renderStrip() {
  const box = $("strip");
  box.textContent = "";
  const n = S.path[S.path.length - 1];
  if (isDec(n) && S.cur.freq) {
    const acts = actionsOf(S.cur, S.path.length - 1);
    const o = overallFreqs(S.cur, acts);
    const bar = el("div", "freqbar");
    acts.forEach((a, j) => {
      const sp = el("span");
      sp.style.background = a.color;
      sp.style.flexGrow = Math.max(o.freqs[j], 0.0001);
      sp.onmousemove = (ev) => showTip(ev, `<div class="t">${a.label} ${a.sub}</div><div class="r"><span>Frequency</span><b>${fmt(o.freqs[j] * 100)}%</b></div><div class="r"><span>Combos</span><b>${fmt(o.combos[j])}</b></div>`);
      sp.onmouseleave = hideTip;
      bar.appendChild(sp);
    });
    box.appendChild(bar);
    const row = el("div", "actions");
    acts.forEach((a, j) => {
      const b = el("button", "action");
      b.style.setProperty("--c", a.color);
      b.style.setProperty("--f", (o.freqs[j] * 100).toFixed(1) + "%");
      const lbl = el("div", "lbl", a.label);
      if (a.sub) lbl.appendChild(el("small", null, a.sub));
      b.append(lbl, el("div", "pct num", fmt(o.freqs[j] * 100) + "%"), el("div", "cmb num", fmt(o.combos[j]) + " c"));
      b.onclick = () => gotoNode(a.id);
      row.appendChild(b);
    });
    box.appendChild(row);
    return;
  }
  if (n.type === "SPLIT_NODE") {
    const kids = new Map(S.cur.children.map((c) => [lastTok(c.id), c.id]));
    const wrap = el("div", "deal");
    const lab = el("div", "deal-label");
    lab.append(el("b", null, boardOf(n).length === 3 ? "Turn" : "River"), el("span", null, `${kids.size} cards`));
    wrap.appendChild(lab);
    const deck = el("div", "mini-deck");
    for (const s of SUITS) for (const r of RANKS) {
      const card = r + s;
      const b = el("button");
      b.style.background = `var(--suit-${s})`;
      b.append(el("span", null, r), el("span", null, GLYPH[s]));
      b.title = card;
      if (kids.has(card)) b.onclick = () => gotoNode(kids.get(card));
      else b.disabled = true;
      deck.appendChild(b);
    }
    wrap.appendChild(deck);
    box.appendChild(wrap);
    return;
  }
  const msg = el("div", "strip-msg");
  msg.appendChild(el("span", null, n.type === "END_NODE" ? endText(n) : "No decision here"));
  box.appendChild(msg);
}

/* ---- the matrix: 169 persistent cells, updated in place ---- */

let cells = null;

function ensureCells() {
  if (cells) return;
  const m = $("matrix");
  m.textContent = "";
  cells = H.classes.map((cls) => {
    const c = el("div", "cell");
    const fill = el("div", "fill"), lbl = el("div", "lbl", cls), val = el("div", "val");
    c.append(fill, lbl, val);
    c.onmouseenter = () => { S.hover = cls; if (!S.pinned) renderSide(); };
    c.onmouseleave = () => { S.hover = null; };
    c.onclick = () => { S.pinned = S.pinned === cls ? null : cls; markSel(); renderSide(); syncUrl(); };
    m.appendChild(c);
    return { cls, c, fill, val };
  });
}

function setSegments(fill, parts) {
  while (fill.children.length > parts.length) fill.lastChild.remove();
  while (fill.children.length < parts.length) fill.appendChild(el("span"));
  parts.forEach(([color, grow], j) => {
    const sp = fill.children[j];
    sp.style.background = color;
    sp.style.flexGrow = grow;
  });
}

export function evColor(v, scale) {
  const t = Math.max(-1, Math.min(1, scale > 0 ? v / scale : 0));
  // red (losing) .. slate (zero) .. green (winning)
  const neg = [224, 82, 82], mid = [110, 120, 138], pos = [66, 190, 128];
  const c = t < 0 ? mid.map((m, k) => m + (neg[k] - m) * -t) : mid.map((m, k) => m + (pos[k] - m) * t);
  return rgb(c);
}

function renderMatrix() {
  const m = $("matrix");
  const note = $("matrix-note");
  const n = S.path[S.path.length - 1];
  if (S.mode === "strategy" && S.dec && S.dec.raw) {
    cells = null; m.className = "matrix raw"; m.textContent = S.dec.raw; note.textContent = ""; return;
  }
  if (m.classList.contains("raw")) { m.className = "matrix"; cells = null; }
  ensureCells();
  m.classList.toggle("stale", S.mode === "strategy" && S.decId !== S.id);

  if (S.mode === "strategy") {
    const d = S.dec;
    if (!d || !d.freq) { cells.forEach((x) => { x.c.classList.add("blank"); x.fill.style.height = "0"; x.val.textContent = ""; }); note.textContent = ""; return; }
    const pi = S.path.findIndex((p) => p.id === S.decId);
    const acts = actionsOf(d, pi);
    const blocked = blockedBy(boardOf(d.node));
    for (const x of cells) {
      const cw = classWeight(d.reach, x.cls, blocked);
      if (cw.combos <= 0) { x.c.classList.add("blank"); x.c.classList.remove("on-fill"); x.fill.style.height = "0"; x.val.textContent = ""; continue; }
      x.c.classList.remove("blank");
      const agg = acts.map((a) => {
        let s = 0;
        for (const i of H.byClass[x.cls]) s += (d.reach[i] || 0) * (d.freq[a.k][i] || 0);
        return s / cw.combos;
      });
      setSegments(x.fill, acts.map((a, j) => [a.color, Math.max(agg[j], 0)]));
      x.fill.style.height = (Math.min(1, cw.w) * 100).toFixed(1) + "%";
      x.c.classList.toggle("on-fill", cw.w > 0.62);
      x.val.textContent = "";
    }
    // only a deal continues below; a fold or showdown ends the hand
    const more = n && n.type === "SPLIT_NODE" ? " — pick a card to continue" : "";
    note.textContent = S.decId !== S.id ? `Last decision: ${d.actor} at ${S.decId}${more}` : "";
  } else {
    const pl = viewPlayer();
    const w = pl === "OOP" ? S.range.oop : S.range.ip;
    const blocked = blockedBy(boardOf(n));
    const evd = S.mode === "ev" ? S.ev[pl] : null;
    const pot = potOf(n) || 1;
    for (const x of cells) {
      const cw = classWeight(w, x.cls, blocked);
      if (cw.combos <= 0) { x.c.classList.add("blank"); x.c.classList.remove("on-fill"); x.fill.style.height = "0"; x.val.textContent = ""; continue; }
      x.c.classList.remove("blank");
      let color = `var(--${pl.toLowerCase()})`, text = "";
      if (evd) {
        let num = 0, den = 0;
        for (const i of H.byClass[x.cls]) {
          // matchups = own reach * compatible opponent mass: already the full weight
          const e = evd.ev[i], rw = w[i] || 0, mu = evd.matchups ? evd.matchups[i] : rw;
          if (e == null || !(mu > 0) || rw <= 0) continue;
          num += mu * e; den += mu;
        }
        const v = den > 0 ? num / den : null;
        color = v == null ? "var(--surface-3)" : evColor(v, pot * 0.75);
        text = v == null ? "" : fmtEvPct(v, pot, 0);
      } else {
        text = cw.w < 0.995 ? Math.round(cw.w * 100) + "" : "";
      }
      setSegments(x.fill, [[color, 1]]);
      x.fill.style.height = (Math.min(1, cw.w) * 100).toFixed(1) + "%";
      x.c.classList.toggle("on-fill", cw.w > 0.62);
      x.val.textContent = text;
    }
    note.textContent = S.mode === "ev" ? `${pl} EV per class, % of pot` : `${pl} range at this node — bar height = weight`;
  }
  markSel();
  markCatHl();
}

function markSel() {
  if (!cells) return;
  for (const x of cells) x.c.classList.toggle("sel", x.cls === S.pinned);
}

/* ---- side panel ---- */

function renderSide() {
  for (const k of ["hand", "cats", "blockers", "graphs"]) $("side-" + k).classList.toggle("hidden", k !== S.side);
  document.querySelectorAll(".side-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.side === S.side));
  if (S.side === "hand") renderHand();
  else if (S.side === "blockers") renderBlockersPanel();
  else if (S.side === "graphs") renderGraphsPanel();
  else renderCatsPanel();
}

function renderHand() {
  const box = $("side-hand");
  box.textContent = "";
  const cls = S.pinned || S.hover;
  const d = S.dec;
  if (!cls || !d || !d.freq) {
    box.appendChild(el("div", "side-empty", "Hover a hand in the matrix to see its combos.\nClick to pin it."));
    return;
  }
  const pi = S.path.findIndex((p) => p.id === S.decId);
  const acts = actionsOf(d, pi);
  const blocked = blockedBy(boardOf(d.node));
  const cw = classWeight(d.reach, cls, blocked);
  const head = el("div", "hand-head");
  head.appendChild(el("div", "cls", cls));
  const meta = el("div", "meta");
  meta.innerHTML = `${d.actor} · weight <b>${fmt(cw.w * 100, 0)}%</b><br>${fmt(cw.combos, 2)} of ${cw.n} combos${S.pinned ? " · pinned" : ""}`;
  head.appendChild(meta);
  box.appendChild(head);

  if (cw.combos > 0) {
    box.appendChild(el("h3", null, "Strategy"));
    const mix = el("div", "mix");
    acts.forEach((a) => {
      let s = 0;
      for (const i of H.byClass[cls]) s += (d.reach[i] || 0) * (d.freq[a.k][i] || 0);
      const r = el("div", "mix-row");
      const sw = el("span", "sw"); sw.style.background = a.color;
      r.append(sw, el("span", null, `${a.label} ${a.sub}`.trim()), el("span", "v num", fmt((s / cw.combos) * 100) + "%"));
      mix.appendChild(r);
    });
    box.appendChild(mix);
  }

  box.appendChild(el("h3", null, "Combos"));
  const t = el("table", "combos");
  t.insertAdjacentHTML("beforeend", '<tr><th></th><th class="w">Weight</th><th></th><th class="ev" title="% of the pot">EV</th></tr>');
  const handPot = potOf(d.node);
  for (const i of H.byClass[cls]) {
    const combo = H.order[i];
    if (blocked(combo)) continue;
    const tr = el("tr");
    const c1 = el("td"); c1.appendChild(comboEl(combo, "xs"));
    const w = d.reach[i] || 0;
    const c2 = el("td", "w num", fmt(w * 100, 0) + "%");
    const c3 = el("td", "bar-cell");
    const mb = el("div", "mini-bar");
    if (w > 0) acts.forEach((a) => { const sp = el("span"); sp.style.background = a.color; sp.style.width = ((d.freq[a.k][i] || 0) * 100).toFixed(1) + "%"; mb.appendChild(sp); });
    c3.appendChild(mb);
    c3.onmousemove = (ev) => showTip(ev, `<div class="t">${combo}</div>` + acts.map((a) => `<div class="r"><span>${a.label} ${a.sub}</span><b>${fmt((d.freq[a.k][i] || 0) * 100)}%</b></div>`).join(""));
    c3.onmouseleave = hideTip;
    const ev = d.ev && d.ev.ev ? d.ev.ev[i] : null;
    const c4 = el("td", "ev num", w > 0 && ev != null ? fmtEvPct(ev, handPot, 0) : "—");
    tr.append(c1, c2, c3, c4);
    if (w <= 0) tr.style.opacity = ".4";
    tr.classList.add("pick");
    tr.title = "Click: what this hand blocks (Blockers tab)";
    tr.onclick = () => selectBlockerCombo(combo, d.actor);
    t.appendChild(tr);
  }
  box.appendChild(t);
}

/* ---- categories tab (#154): made-hand and draw breakdown of the range at hand ---- */

const DRAW_LABEL = {
  no_draw: "No draw", bdfd_1card: "Backdoor FD (1 card)", bdfd_2card: "Backdoor FD (2 cards)",
  "4out_straight_draw": "4-out straight draw", "8out_straight_draw": "8-out straight draw",
  flush_draw: "Flush draw", combo_draw: "Combo draw",
};

/** Short names for the Cats table (the row's tooltip keeps the full one). */
const DRAW_SHORT = {
  bdfd_1card: "BDFD (1 card)", bdfd_2card: "BDFD (2 cards)",
  "4out_straight_draw": "4-out straight", "8out_straight_draw": "8-out straight",
};

const prettyCat = (name) => DRAW_LABEL[name] || name.replace(/[-_]/g, " ").replace(/^\w/, (c) => c.toUpperCase());

/** Apply the category hover/pin highlight to the matrix cells. */
function markCatHl() {
  if (!cells) return;
  const set = S.catPin ? S.catPin.classes : S.catHl;
  for (const x of cells) x.c.classList.toggle("cat-hl", !!(set && set.has(x.cls)));
  $("matrix").classList.toggle("catmode", !!S.catPin);
}

async function renderCatsPanel() {
  const box = $("side-cats");
  const seq = S.seq;
  const d = S.dec;
  if (!S.decId || !d || !d.freq) {
    box.dataset.key = "";
    box.textContent = "";
    box.appendChild(el("div", "side-empty", "Categories need a flop (or later) decision — preflop nodes have no board."));
    return;
  }
  const pl = S.catsPlayer || d.actor;
  const key = `${S.base}|${S.decId}|${pl}`;
  const ent = await sideData("cats", key, `${S.base}/categories?id=${q(S.decId)}&player=${pl}`, (data) => ({ key, data }));
  if (!ent) return;
  if (seq !== S.seq || S.side !== "cats") return;
  // renderSide runs on every matrix hover; rebuilding the list then would reset its scroll
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  const data = ent.data;
  box.textContent = "";

  const head = el("div", "cats-head");
  head.append(el("span", `badge ${pl.toLowerCase()}`, pl), el("span", "muted num", `${fmt(data.total)} combos`));
  const seg = el("div", "seg small");
  for (const p of ["OOP", "IP"]) {
    const b = el("button", p === pl ? "active" : "", p);
    b.onclick = () => { S.catsPlayer = p; renderCatsPanel(); };
    seg.appendChild(b);
  }
  head.appendChild(seg);
  box.appendChild(head);

  if (!data.made.length) {
    box.appendChild(el("div", "side-empty", "No board at this node — categories start on the flop."));
    return;
  }
  const pi = S.path.findIndex((p) => p.id === S.decId);
  const acts = actionsOf(d, pi);
  if (pl !== data.actor) {
    box.appendChild(el("div", "hint cats-note", `${pl} is not to act here — shares and EVs only, no strategy.`));
  }
  const pot = potOf(d.node);
  const strat = pl === data.actor;
  const t = el("table", "cats-tbl");
  const hr = el("tr");
  hr.append(el("th", "n"), el("th", "num", "Combos"), el("th", "num", "Range"));
  const evh = el("th", "num", "EV");
  evh.title = "EV as % of the pot (reach- and matchup-weighted)";
  hr.appendChild(evh);
  if (strat) {
    for (const a of acts) {
      const th = el("th", "act");
      const sw = el("i"); sw.style.background = a.color;
      th.append(sw, el("span", null, a.label));
      th.title = `How often each category takes ${a.label}`;
      hr.appendChild(th);
    }
  }
  const thead = el("thead");
  thead.appendChild(hr);
  t.appendChild(thead);
  const ncol = 4 + (strat ? acts.length : 0);
  const section = (title, rows) => {
    const tb = el("tbody");
    const sr = el("tr", "sec");
    const sc = el("td", null, title);
    sc.colSpan = ncol;
    sr.appendChild(sc);
    tb.appendChild(sr);
    rows.forEach((c) => tb.appendChild(catRow(c, strat ? acts : [], box, pot)));
    t.appendChild(tb);
  };
  section("Made hands", data.made);
  const draws = data.draws.filter((c) => c.name !== "no_draw" || data.draws.length > 1);
  if (draws.length) section("Draws", draws);
  const wrap = el("div", "cats-wrap");
  wrap.appendChild(t);
  box.appendChild(wrap);
  box.appendChild(el("div", "hint cats-note", "Hover a row to light up its hands in the matrix; click to pin."));
}

/** One category: combos, share of the range, EV (% of pot), then how often it takes each action. */
function catRow(c, acts, box, pot) {
  const row = el("tr", "cat-row");
  row.dataset.cat = c.name;
  if (S.catPin && S.catPin.name === c.name) row.classList.add("sel");
  const name = prettyCat(c.name);
  const short = DRAW_SHORT[c.name] || name;
  const share = fmt(c.pct * 100, c.pct < 0.1 ? 1 : 0) + "%";
  const ev = c.ev != null ? fmtEvPct(c.ev, pot, 0) : "—";
  row.append(el("td", "n", short), el("td", "num muted", fmt(c.combos, c.combos < 10 ? 1 : 0)),
             el("td", "num", share), el("td", "num ev", ev));
  let tip = `<div class="t">${name}</div><div class="r"><span>Combos</span><b>${fmt(c.combos, 1)} (${share} of the range)</b></div>` +
    `<div class="r"><span>EV</span><b>${ev} of the pot</b></div>`;
  if (c.strategy) {
    for (const a of acts) {
      const f = c.strategy[a.k] || 0;
      const td = el("td", "num act");
      if (f > 0.0005) td.append(fmt(f * 100, f < 0.1 ? 1 : 0), el("small", null, "%"));
      if (f > 0.0005) td.style.background = `color-mix(in srgb, ${a.color} ${(6 + f * 50).toFixed(0)}%, transparent)`;
      row.appendChild(td);
      tip += `<div class="r"><span>${a.label}</span><b>${fmt(f * 100)}%</b></div>`;
    }
  }
  row.onmousemove = (ev) => showTip(ev, tip);
  row.onmouseenter = () => { S.catHl = new Set(c.classes); if (!S.catPin) markCatHl(); };
  row.onmouseleave = () => { S.catHl = null; hideTip(); if (!S.catPin) markCatHl(); };
  row.onclick = () => {
    S.catPin = S.catPin && S.catPin.name === c.name ? null : { name: c.name, classes: new Set(c.classes) };
    markCatHl();
    box.querySelectorAll(".cat-row").forEach((r) => r.classList.toggle("sel", !!S.catPin && S.catPin.name === r.dataset.cat));
  };
  return row;
}

/* ---- blockers tab: what holding one combo (or one card) removes from villain's range ---- */

/** A change in percentage points, signed; one decimal below 10 points, and 0.0 when it rounds to nothing. */
function pp(x) {
  if (x == null) return "—";
  const d = Math.abs(x) < 0.1 ? 1 : 0, t = fmt(Math.abs(x) * 100, d);
  return Number(t) === 0 ? fmt(0, d) : `${x > 0 ? "+" : "−"}${t}`;
}
const pctOf = (x, d = 1) => (x == null ? "—" : fmt(x * 100, d) + "%");

/** A diverging tint for a change: green for more, red for less, alpha by |x| / scale. */
function deltaTint(x, scale) {
  if (x == null || !(scale > 0)) return "transparent";
  const a = Math.min(1, Math.abs(x) / scale) * 0.85;
  return x >= 0 ? `rgba(76,195,138,${a.toFixed(3)})` : `rgba(232,83,74,${a.toFixed(3)})`;
}

/** The combo the Blockers tab shows, if it is the current actor's (a combo picked for OOP does not
 *  carry over to an IP decision) and a real combo. */
function blockerHero(data) {
  if (S.blkCombo && S.blkFor == null) S.blkFor = data.actor;   // from a link: the actor it was shared at
  if (!S.blkCombo || S.blkFor !== data.actor) return null;
  const i = H.index.get(S.blkCombo);
  return i == null ? null : i;
}

/** Villain's actions after one actor action, labelled like the strip (the child is one step past
 *  the decision at path index pi). */
function responseActs(r, pi) {
  const path = S.path.slice(0, pi + 1).concat([r.node]);
  return actionsOf({ node: r.node, children: r.children }, pi + 1, path);
}

/** Metric choices for the card ranking: villain's equity, then each villain action after each actor
 *  action, named by player and sized in % of pot ("OOP Bet 100% → IP Fold"). */
function blockerMetrics(P, acts, pi, data) {
  const out = [{ key: "eq", label: `${data.villain} eq`, get: (s) => s.eq }];
  P.responses.forEach((r, k) => {
    const a = acts.find((x) => x.id === r.id);
    const head = a ? a.label : lastTok(r.id);
    for (const va of responseActs(r, pi)) {
      out.push({ key: `r:${r.id}:${va.k}`, label: `${data.actor} ${head} → ${data.villain} ${va.label}`,
                 get: (s) => s.responses[k].freq[va.k], kind: va.kind });
    }
  });
  return out;
}

/** Default ranking metric: villain's fold after the first bet that gets an answer, else equity. */
function defaultMetric(metrics) {
  return (metrics.find((m) => m.kind === "fold") || metrics[0]).key;
}

async function renderBlockersPanel() {
  const box = $("side-blockers");
  const seq = S.seq;
  const d = S.dec;
  if (!S.decId || !d || !d.freq) {
    box.dataset.key = "";
    box.textContent = "";
    box.appendChild(el("div", "side-empty", "Blockers need a decision node — pick one in the action line."));
    return;
  }
  const key = `${S.base}|${S.decId}`;
  const ent = await sideData("blk", key, `${S.base}/blockers?id=${q(S.decId)}`,
                            (data) => ({ key, data, P: prepareBlockers(data, H.order) }));
  if (!ent) return;
  if (seq !== S.seq || S.side !== "blockers") return;
  const { data, P } = ent;
  const pi = S.path.findIndex((p) => p.id === S.decId);
  const acts = actionsOf(d, pi);
  const metrics = blockerMetrics(P, acts, pi, data);
  if (!metrics.some((m) => m.key === S.blkMetric)) S.blkMetric = defaultMetric(metrics);
  const hero = blockerHero(data);
  // renderSide runs on every matrix hover: rebuild only when what is shown changes (keeps the scroll)
  const vkey = `${key}|${hero}|${S.blkMetric}|${S.pinned || ""}|${S.blkNoBoardRanks}|${S.blkView}`;
  if (box.dataset.key === vkey) return;
  box.dataset.key = vkey;
  box.textContent = "";

  box.appendChild(blockerPicker(data, P, hero));
  if (hero != null) {
    const rep = comboReport(P, hero);
    if (rep.blockedByBoard) {
      box.appendChild(el("div", "side-empty", `${displayCombo(H.order[hero])} uses a board card.`));
    } else {
      renderBlockerSummary(box, data, hero, rep);
      renderResponses(box, data, P, rep, acts, pi);
      renderRangeShift(box, data, rep);
      renderRemoved(box, data, rep);
    }
  }
  renderCardEffects(box, P, metrics, hero);
}

/** A player's name in their colour, so every line says who acts without "you" / "villain". */
const who = (p) => el("span", `who ${p.toLowerCase()}`, p);

/** A signed change in points, coloured by direction (none when it rounds to 0). */
function deltaEl(x, cls = "blk-d") {
  const t = pp(x), e = el("span", cls, t);
  if (x != null && /^[+−]/.test(t)) e.classList.add(x > 0 ? "up" : "down");
  return e;
}

/** Pick the hand: type it, or click one of the pinned class's combos. */
function blockerPicker(data, P, hero) {
  const wrap = el("div", "blk-pick");
  const field = el("label", "blk-field");
  const inp = el("input");
  inp.type = "text";
  inp.placeholder = hero == null ? "Pick a hand, e.g. AsKd" : "";
  inp.value = hero != null ? displayCombo(H.order[hero]) : "";
  inp.spellcheck = false;
  inp.autocomplete = "off";
  const apply = (loud) => {
    const t = parseCards(inp.value);
    const i = t.length === 2 ? H.index.get(t[0] + t[1]) : undefined;
    if (i == null) { if (loud) toast("Type two cards, e.g. AsKd", "err"); return; }
    if (i !== hero) selectBlockerCombo(H.order[i], data.actor);
  };
  inp.onkeydown = (e) => {
    if (e.key === "Enter") apply(true);
    if (e.key === "Escape") { inp.value = hero != null ? displayCombo(H.order[hero]) : ""; inp.blur(); }
  };
  inp.onchange = () => apply(false);
  field.append(el("span", `badge ${data.actor.toLowerCase()}`, data.actor), inp);
  if (hero != null) {
    const clr = el("button", "blk-x", "×");
    clr.title = "Clear the hand";
    clr.onclick = (e) => { e.preventDefault(); S.blkCombo = null; renderSide(); syncUrl(); };
    field.appendChild(clr);
  }
  wrap.appendChild(field);
  const cls = S.pinned;
  if (cls) {
    const blocked = blockedBy(boardOf(data.node));
    const list = el("div", "blk-combos");
    for (const i of H.byClass[cls]) {
      const combo = H.order[i];
      if (blocked(combo)) continue;
      const b = el("button", "blk-combo" + (i === hero ? " sel" : ""));
      b.appendChild(comboEl(combo, "xs"));
      const w = data.hero_reach[i] || 0;
      if (w <= 0) b.classList.add("out");
      else if (w < 0.995) b.appendChild(el("span", "w", fmt(w * 100, 0) + "%"));
      b.title = `${displayCombo(combo)}: ${fmt(w * 100, 0)}% in ${data.actor}'s range`;
      b.onclick = () => selectBlockerCombo(combo, data.actor);
      list.appendChild(b);
    }
    wrap.appendChild(list);
  } else if (hero == null) {
    wrap.appendChild(el("div", "hint", "Or pin a class in the matrix, or click a combo in the Hand tab."));
  }
  return wrap;
}

function selectBlockerCombo(combo, actor) {
  S.blkCombo = displayCombo(combo);
  S.blkFor = actor;
  S.side = "blockers";
  renderSide();
  syncUrl();
}

/** Three numbers: how much of villain's range the hand blocks, villain's eq before → after, the hand's eq. */
function renderBlockerSummary(box, data, hero, rep) {
  const vil = data.villain;
  const removed = rep.before.mass - rep.after.mass;
  const share = rep.before.mass > 0 ? removed / rep.before.mass : 0;
  const row = el("div", "blk-sum");
  const stat = (value, label, title) => {
    const s = el("div", "blk-stat");
    s.append(value, label);
    if (title) s.title = title;
    row.appendChild(s);
    return s;
  };
  const l1 = el("span", "k");
  l1.append(who(vil), document.createTextNode(" blocked"));
  stat(el("b", "num", pctOf(share)), l1, `${pctOf(share)} of ${vil}'s range: ${fmt(removed, 2)} of ${fmt(rep.before.mass, 1)} combos`);
  const v2 = el("b", "num", pctOf(rep.after.eq));
  if (rep.before.eq != null && rep.after.eq != null) v2.appendChild(deltaEl(rep.after.eq - rep.before.eq));
  const l2 = el("span", "k");
  l2.append(who(vil), document.createTextNode(" eq"));
  stat(v2, l2, `${vil} eq ${pctOf(rep.before.eq)} vs the whole range → ${pctOf(rep.after.eq)} given the hand`);
  const l3 = el("span", "k");
  l3.append(comboEl(H.order[hero], "xs"), document.createTextNode(" eq"));
  stat(el("b", "num", rep.heroReach > 0 || rep.heroEq != null ? pctOf(rep.heroEq) : "—"), l3,
       rep.heroReach > 0 ? `vs ${vil}'s range` : `not in ${data.actor}'s range here`);
  box.appendChild(row);
}

/** Villain's next action after each actor action: a stacked bar given the hand (ticks: the whole
 *  range), and a chip per action with its change. */
function renderResponses(box, data, P, rep, acts, pi) {
  const vil = data.villain;
  const shown = P.responses.filter((r) => acts.some((a) => a.id === r.id));
  const h = el("h3", "blk-h");
  h.append(who(vil), document.createTextNode(" responds"));
  box.appendChild(h);
  if (!shown.length) {
    box.appendChild(el("div", "hint", `${vil} does not act right after any action here (a fold, a showdown or a new card comes first).`));
    return;
  }
  const list = el("div", "blk-resps");
  for (const r of shown) {
    const k = P.responses.indexOf(r);
    const a = acts.find((x) => x.id === r.id);
    const vas = responseActs(r, pi);
    const f0 = (va) => rep.before.responses[k].freq[va.k] || 0;
    const f1 = (va) => rep.after.responses[k].freq[va.k] || 0;
    const lab = el("div", "blk-resp-a");
    const sw = el("span", "sw"); sw.style.background = a.color;
    lab.append(sw, who(data.actor), document.createTextNode(" " + a.label));
    const bar = el("div", "blk-bar");
    let acc = 0;
    for (const va of vas) {
      if (f1(va) > 0) {
        const seg = el("i");
        seg.style.width = (f1(va) * 100).toFixed(2) + "%";
        seg.style.background = va.color;
        bar.appendChild(seg);
      }
      acc += f0(va);
      if (acc > 0.001 && acc < 0.999) {
        const t = el("b", "tick");
        t.style.left = (acc * 100).toFixed(2) + "%";
        bar.appendChild(t);
      }
    }
    const tip = `<div class="t">${data.actor} ${a.label} → ${vil}</div>` + vas.map((va) =>
      `<div class="r"><span>${va.label}</span><b>${pctOf(f0(va))} → ${pctOf(f1(va))}</b></div>`).join("") +
      `<div class="r"><span class="muted">whole range → given the hand</span></div>`;
    bar.onmousemove = (ev) => showTip(ev, tip);
    bar.onmouseleave = hideTip;
    const chips = el("div", "blk-chips");
    for (const va of vas) {
      const c = el("span", "blk-chip");
      const s2 = el("span", "sw"); s2.style.background = va.color;
      c.append(s2, document.createTextNode(va.label + " "), el("b", "num", pctOf(f1(va))), deltaEl(f1(va) - f0(va)));
      chips.appendChild(c);
    }
    list.append(lab, bar, el("span"), chips);
  }
  box.appendChild(list);
}

/** What the hand thins out of villain's range and what it leaves thicker: one diverging bar per
 *  made-hand category, draw or hand class, biggest moves first. */
function renderRangeShift(box, data, rep) {
  const vil = data.villain;
  const views = data.cats ? [["made", "Made"], ["draw", "Draws"], ["classes", "Classes"]] : [["classes", "Classes"]];
  const view = views.some(([k]) => k === S.blkView) ? S.blkView : views[0][0];
  const head = el("div", "blk-sec");
  const h = el("h3", "blk-h");
  h.append(who(vil), document.createTextNode(" range shifts"));
  head.appendChild(h);
  if (views.length > 1) {
    const seg = el("div", "seg small");
    for (const [k, label] of views) {
      const b = el("button", k === view ? "active" : "", label);
      b.onclick = () => { S.blkView = k; prefs.set("blk-view", k); renderSide(); };
      seg.appendChild(b);
    }
    head.appendChild(seg);
  }
  box.appendChild(head);
  let rows, name;
  if (view === "classes") {
    rows = shareChanges(rep.before, rep.after, (s) => s.classes);
    name = (k) => k;
  } else {
    const names = view === "made" ? data.cats.made_names : data.cats.draw_names;
    rows = shareChanges(rep.before, rep.after, (s) => s[view]);
    if (view === "draw") rows = rows.filter((x) => names[x.key] !== "no_draw");
    name = (k) => prettyCat(names[k] || String(k));
  }
  rows = rows.filter((x) => Math.abs(x.delta) >= 0.0005).slice(0, 7);
  if (!rows.length) { box.appendChild(el("div", "hint", "No visible change.")); return; }
  const scale = Math.max(...rows.map((x) => Math.abs(x.delta)));
  const list = el("div", "blk-shift");
  for (const x of rows) {
    const bar = el("div", "blk-div");
    const fill = el("i", x.delta >= 0 ? "up" : "down");
    fill.style.width = (Math.abs(x.delta) / scale * 50).toFixed(2) + "%";
    bar.appendChild(fill);
    const row = el("div", "blk-shift-row");
    row.append(el("span", "n", name(x.key)), bar, deltaEl(x.delta));
    const tip = `<div class="t">${name(x.key)}</div><div class="r"><span>Share of ${vil}'s range</span><b>${pctOf(x.before)} → ${pctOf(x.after)}</b></div>`;
    row.onmousemove = (ev) => showTip(ev, tip);
    row.onmouseleave = hideTip;
    list.appendChild(row);
  }
  box.appendChild(list);
}

/** The villain combos the hand rules out, folded away: one line per class. */
function renderRemoved(box, data, rep) {
  const vil = data.villain;
  const det = el("details", "blk-removed");
  det.open = S.blkRemovedOpen;
  det.ontoggle = () => { S.blkRemovedOpen = det.open; };
  const sum = el("summary");
  const removed = rep.before.mass - rep.after.mass;
  sum.append(el("span", null, "Blocked combos"), el("span", "muted num", fmt(removed, removed < 10 ? 2 : 1)));
  det.appendChild(sum);
  const byCls = new Map();
  for (const x of rep.removed) {
    const g = byCls.get(x.cls) || { cls: x.cls, w: 0, combos: [] };
    g.w += x.w; g.combos.push(x);
    byCls.set(x.cls, g);
  }
  const groups = [...byCls.values()].sort((a, b) => b.w - a.w);
  if (!groups.length) det.appendChild(el("div", "hint", `None: ${vil} has no combo with these cards.`));
  for (const g of groups) {
    const row = el("div", "blk-rm-row");
    const list = el("span", "blk-rm-list");
    for (const x of g.combos) {
      const s = comboEl(x.combo, "xs");
      s.title = `${displayCombo(x.combo)}: ${fmt(x.w * 100, 0)}% in ${vil}'s range`;
      if (x.w < 0.995) s.classList.add("part");
      list.appendChild(s);
    }
    row.append(el("span", "c", g.cls), list, el("span", "muted num", fmt(g.w, 2)));
    det.appendChild(row);
  }
  box.appendChild(det);
}

/** Every single card, shaded by how much holding it moves the chosen number. */
function renderCardEffects(box, P, metrics, hero) {
  const m = metrics.find((x) => x.key === S.blkMetric) || metrics[0];
  box.appendChild(el("h3", "blk-h", "Single cards"));
  const tools = el("div", "blk-tools");
  const sel = el("select", "blk-metric");
  for (const x of metrics) {
    const o = el("option", null, x.label);
    o.value = x.key;
    if (x.key === m.key) o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = () => { S.blkMetric = sel.value; renderSide(); };
  // a K on the board: leave out every K (they pair the board), so the grid shows the other ranks
  const boardRanks = new Set([...P.board].map((c) => c[0]));
  const lbl = el("label", "blk-toggle");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = S.blkNoBoardRanks;
  cb.onchange = () => { S.blkNoBoardRanks = cb.checked; prefs.set("blk-no-board-ranks", cb.checked); renderSide(); };
  lbl.append(cb, document.createTextNode("No board ranks"));
  lbl.title = "Leave out every card of a rank on the board (a K on the board leaves out all four kings)";
  tools.append(sel, lbl);
  box.appendChild(tools);
  const excluded = (card) => S.blkNoBoardRanks && boardRanks.has(card[0]);
  const rows = cardEffects(P, m.get).filter((r) => !excluded(r.card));
  const ranks = [...RANKS].filter((rk) => !(S.blkNoBoardRanks && boardRanks.has(rk)));
  const held = hero != null ? P.cards[hero] : [];
  const scale = Math.max(1e-9, ...rows.map((r) => Math.abs(r.delta ?? 0)));
  const byCard = new Map(rows.map((r) => [r.card, r]));
  const tip = (r) => `<div class="t">${r.card}</div><div class="r"><span>${m.label}</span><b>${pctOf(r.before)} → ${pctOf(r.after)}</b></div>` +
    `<div class="r"><span>${P.data.villain} range blocked</span><b>${pctOf(r.removedPct)}</b></div>`;
  const grid = el("div", "blk-cards");
  grid.style.gridTemplateColumns = `repeat(${ranks.length}, 1fr)`;
  for (const s of "shdc") {
    for (const rk of ranks) {
      const card = rk + s;
      const r = byCard.get(card);
      const c = el("div", "blk-card" + (held.includes(card) ? " held" : ""));
      if (!r) { c.classList.add("on-board"); c.title = `${card} is on the board`; c.appendChild(cardEl(card, "xs")); grid.appendChild(c); continue; }
      c.style.background = deltaTint(r.delta, scale);
      c.appendChild(cardEl(card, "xs"));
      c.appendChild(el("span", "num", pp(r.delta)));
      c.onmousemove = (ev) => showTip(ev, tip(r));
      c.onmouseleave = hideTip;
      grid.appendChild(c);
    }
  }
  box.appendChild(grid);
  box.appendChild(el("div", "hint cats-note", `Change in points when ${P.data.actor} holds the card (${pctOf(rows.length ? rows[0].before : null)} with none).`));
}

/* ---- graphs tab: each player's range sorted by equity or EV ---- */

const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs, parent) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
};

/** A "nice" tick step (1, 2 or 5 x 10^k) giving about n intervals over span. */
function niceTick(span, n = 4) {
  const raw = span / n, p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}

async function renderGraphsPanel() {
  const box = $("side-graphs");
  const seq = S.seq;
  const key = `${S.base}|${S.id}`;
  const ent = await sideData("gr", key, `${S.base}/graphs?id=${q(S.id)}`, (data) => ({ key, data }));
  if (!ent) return;
  if (seq !== S.seq || S.side !== "graphs") return;
  const vkey = `${key}|${S.grMetric}`;
  if (box.dataset.key === vkey) return;
  box.dataset.key = vkey;
  box.textContent = "";
  const data = ent.data;
  const metric = S.grMetric;

  const head = el("div", "cats-head");
  head.appendChild(el("span", "gr-title", metric === "eq" ? "eq distribution" : "EV distribution (% of pot)"));
  const seg = el("div", "seg small");
  for (const [k, label] of [["eq", "eq"], ["ev", "EV"]]) {
    const b = el("button", k === metric ? "active" : "", label);
    b.onclick = () => { S.grMetric = k; prefs.set("graph-metric", k); renderGraphsPanel(); };
    seg.appendChild(b);
  }
  head.appendChild(seg);
  box.appendChild(head);
  if (data.id !== S.id) box.appendChild(el("div", "hint cats-note", `At ${data.id}: the hand has ended here, so these are the last node's values.`));

  const pot = potOf(data.node) || 0;
  const cs = rangeCurves(data, metric, H.order);
  if (metric === "ev") {
    // EVs as fractions of the pot, like every EV in the viewer
    for (const c of Object.values(cs)) {
      for (const p of c.pts) p.v = pot > 0 ? p.v / pot : null;
      c.avg = c.avg != null && pot > 0 ? c.avg / pot : null;
      c.median = c.median != null && pot > 0 ? c.median / pot : null;
      c.pts = c.pts.filter((p) => p.v != null);
    }
  }
  if (!cs.OOP.pts.length && !cs.IP.pts.length) {
    box.appendChild(el("div", "side-empty", "No values here: neither range reaches this node."));
    return;
  }
  const legend = el("div", "gr-legend");
  for (const pl of ["OOP", "IP"]) {
    const it = el("span", "gr-key");
    const sw = el("span", "gr-sw"); sw.style.background = `var(--${pl.toLowerCase()})`;
    it.append(sw, document.createTextNode(pl));
    legend.appendChild(it);
  }
  box.appendChild(legend);
  const svg = svgEl("svg", { viewBox: "0 0 360 244", class: "gr-chart", role: "img",
    "aria-label": `${metric === "eq" ? "eq" : "EV (% of pot)"} of each combo, weakest first, for OOP and IP` });
  box.appendChild(svg);
  drawRangeGraph(svg, cs, metric);

  // the numbers behind the picture
  const t = el("table", "act-table gr-stats");
  t.insertAdjacentHTML("beforeend", metric === "eq"
    ? "<tr><th></th><th>Avg eq</th><th>Median</th><th>≥ 50%</th><th>Combos</th></tr>"
    : "<tr><th></th><th>Avg EV</th><th>Median</th><th>Combos</th></tr>");
  for (const pl of ["OOP", "IP"]) {
    const c = cs[pl];
    const tr = el("tr");
    const k = el("td");
    const sw = el("span", "sw"); sw.style.background = `var(--${pl.toLowerCase()})`;
    k.append(sw, document.createTextNode(pl));
    tr.append(k, el("td", "num", pctOf(c.avg)), el("td", "num", pctOf(c.median)),
              ...(metric === "eq" ? [el("td", "num", pctOf(shareAbove(c, 0.5), 0))] : []),
              el("td", "num", fmt(c.total, 1)));
    t.appendChild(tr);
  }
  box.appendChild(t);
  box.appendChild(el("div", "hint cats-note", metric === "eq"
    ? "Each combo's eq against the other range here, weakest first along the share of the range. Averages are matchup-weighted, like Pio's."
    : "Each combo's EV as % of the pot here, lowest first along the share of the range. Averages are matchup-weighted, like the Node tab's."));
}

function drawRangeGraph(svg, cs, metric) {
  const W = 360, Hh = 244, L = 42, R = 12, T = 10, B = 34;
  const all = [...cs.OOP.pts, ...cs.IP.pts].map((p) => p.v);
  let lo, hi, step;
  if (metric === "eq") { lo = 0; hi = 1; step = 0.25; } else {
    const mn = Math.min(0, ...all), mx = Math.max(0, ...all);
    step = niceTick(mx - mn || 1);
    lo = Math.floor(mn / step + 1e-9) * step;   // a range with no losing hand starts at 0, not a step below
    hi = Math.ceil(mx / step - 1e-9) * step;
    if (hi <= lo) hi = lo + step;
  }
  // enough decimals that no two ticks read the same (a narrow EV range has steps under 1% of the pot)
  const axisDigits = step * 100 >= 1 - 1e-9 ? 0 : step * 100 >= 0.1 - 1e-9 ? 1 : 2;
  const x = (f) => L + f * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (Hh - T - B);
  const grid = svgEl("g", { class: "grid" }, svg), axis = svgEl("g", { class: "axis" }, svg);
  for (let i = 0, n = Math.round((hi - lo) / step); i <= n; i++) {
    // from the index, not by adding the step (that drifts), and 0 exactly so it never prints "-0%"
    const v0 = lo + i * step, v = Math.abs(v0) < step / 2 ? 0 : v0;
    const yy = y(v);
    svgEl("line", { x1: L, x2: W - R, y1: yy, y2: yy, class: Math.abs(v) < 1e-9 && metric === "ev" ? "zero" : "" }, grid);
    const tx = svgEl("text", { x: L - 6, y: yy + 3, "text-anchor": "end" }, axis);
    tx.textContent = fmt(v * 100, axisDigits) + "%";
  }
  for (const f of [0, 0.25, 0.5, 0.75, 1]) {
    const tx = svgEl("text", { x: x(f), y: Hh - B + 14, "text-anchor": f === 0 ? "start" : f === 1 ? "end" : "middle" }, axis);
    tx.textContent = Math.round(f * 100) + "%";
  }
  const xl = svgEl("text", { x: (L + W - R) / 2, y: Hh - 3, "text-anchor": "middle", class: "axis-title" }, axis);
  xl.textContent = "share of range, weakest first";

  for (const pl of ["IP", "OOP"]) {
    const pts = cs[pl].pts;
    if (!pts.length) continue;
    let d = `M${x(pts[0].x0).toFixed(1)},${y(pts[0].v).toFixed(1)}`;
    for (const p of pts) d += `V${y(p.v).toFixed(1)}H${x(p.x1).toFixed(1)}`;
    svgEl("path", { d, class: "gr-line", style: `stroke:var(--${pl.toLowerCase()})` }, svg);
  }

  // hover: a crosshair, a dot on each curve and the combos there
  const cross = svgEl("line", { y1: T, y2: Hh - B, class: "gr-cross", visibility: "hidden" }, svg);
  const dots = {};
  for (const pl of ["OOP", "IP"]) dots[pl] = svgEl("circle", { r: 4, class: "gr-dot", style: `fill:var(--${pl.toLowerCase()})`, visibility: "hidden" }, svg);
  const hit = svgEl("rect", { x: L, y: T, width: W - L - R, height: Hh - T - B, class: "gr-hit" }, svg);
  const fmtV = (v) => pctOf(v);
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect();
    const sx = ((ev.clientX - r.left) / r.width) * W;
    const f = Math.min(1, Math.max(0, (sx - L) / (W - L - R)));
    cross.setAttribute("x1", x(f)); cross.setAttribute("x2", x(f)); cross.setAttribute("visibility", "visible");
    let html = `<div class="t">${Math.round(f * 100)}% of the range below</div>`;
    for (const pl of ["OOP", "IP"]) {
      const p = rangeAt(cs[pl], f);
      if (!p) { dots[pl].setAttribute("visibility", "hidden"); continue; }
      dots[pl].setAttribute("cx", x(f)); dots[pl].setAttribute("cy", y(p.v)); dots[pl].setAttribute("visibility", "visible");
      html += `<div class="r"><span>${pl} ${displayCombo(p.combo)}</span><b>${metric === "eq" ? "eq" : "EV"} ${fmtV(p.v)}</b></div>`;
    }
    showTip(ev, html);
  });
  hit.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden");
    for (const pl of ["OOP", "IP"]) dots[pl].setAttribute("visibility", "hidden");
    hideTip();
  });
}

/** Up one node: the parent (deals are steps too); at a re-solved subtree's root, back to the original. */
function goUp() {
  if (S.sub && S.id === S.sub.root) { exitSubgame(); return; }
  if (S.path.length > 1) gotoNode(S.path[S.path.length - 2].id);
}

/* ------------------------------------------------------------------ wiring */

// The job batch the open save belongs to (#331): its aggregate report can be run at the current node.
let reportBatch = null;
export function setReportBatch(name) {
  reportBatch = name || null;
  const n = S.path && S.path[S.path.length - 1];
  $("btn-report").classList.toggle("hidden", !(reportBatch && n && isDec(n) && !S.sub));
}

/** The side panel's width: drag its left edge (double-click resets); remembered per browser. */
function initSideResize() {
  const study = document.querySelector("#view-study .study") || document.querySelector(".study");
  const side = study && study.querySelector(".side");
  if (!side) return;
  const MIN = 300;
  const maxW = () => Math.max(MIN, Math.min(960, window.innerWidth - 480));   // leave the matrix room
  let width = null;
  const apply = (w) => {
    if (w == null) { study.style.removeProperty("--side-w"); width = null; return; }
    width = Math.round(Math.min(maxW(), Math.max(MIN, w)));
    study.style.setProperty("--side-w", width + "px");
  };
  apply(prefs.get("side-width", null));
  const h = el("div", "side-resize");
  h.title = "Drag to resize the panel; double-click to reset";
  side.prepend(h);
  h.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    h.setPointerCapture(e.pointerId);
    h.classList.add("drag");
    document.body.classList.add("col-resizing");
    const right = side.getBoundingClientRect().right;
    const move = (ev) => apply(right - ev.clientX);
    const up = () => {
      h.removeEventListener("pointermove", move);
      h.removeEventListener("pointerup", up);
      h.removeEventListener("pointercancel", up);
      h.classList.remove("drag");
      document.body.classList.remove("col-resizing");
      prefs.set("side-width", width);
    };
    h.addEventListener("pointermove", move);
    h.addEventListener("pointerup", up);
    h.addEventListener("pointercancel", up);
  });
  h.addEventListener("dblclick", () => { apply(null); prefs.set("side-width", null); });
  window.addEventListener("resize", () => { if (width != null) apply(width); });   // keep it within a smaller window
}

export function initStudy() {
  initSideResize();
  $("view-seg").querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      S.mode = b.dataset.mode;
      $("view-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      if (!S.loaded) return;
      try { if (S.mode !== "strategy") await loadModeData(); } catch (e) { fail(e); }
      renderTitle(); renderMatrix(); syncUrl();
    };
  });
  $("player-seg").querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      S.player = b.dataset.player;
      if (!S.loaded) return;
      try { await loadModeData(); } catch (e) { fail(e); }
      renderTitle(); renderMatrix(); syncUrl();
    };
  });
  document.querySelectorAll(".side-tabs button").forEach((b) => {
    b.onclick = () => { S.side = b.dataset.side; if (S.loaded) { renderSide(); syncUrl(); } };
  });
  document.addEventListener("keydown", (e) => {
    if (document.getElementById("view-study").classList.contains("hidden")) return;
    if (e.target.closest("input, textarea, dialog")) return;
    if (e.key === "ArrowLeft" || e.key === "Backspace") {
      e.preventDefault();
      goUp();
    } else if (e.key === "Escape") {
      S.pinned = null; S.catPin = null; S.catHl = null;
      markSel(); markCatHl(); renderSide();
    }
  });
}

function syncUrl() {
  if (S.loaded && !S.sub && !document.getElementById("view-study").classList.contains("hidden")) {
    history.replaceState(null, "", "#study=" + S.id + urlState());
  }
}

/** View state carried in the URL after the node id: &mode=ev&player=IP&side=node&pin=AKs */
function urlState() {
  let s = "";
  if (S.mode !== "strategy") s += "&mode=" + S.mode;
  if (S.player) s += "&player=" + S.player;
  if (S.side !== "hand") s += "&side=" + S.side;
  if (S.pinned) s += "&pin=" + S.pinned;
  if (S.blkCombo) s += "&blk=" + S.blkCombo;
  return s;
}

export function applyUrlState(params) {
  if (["strategy", "range", "ev"].includes(params.mode)) S.mode = params.mode;
  if (["OOP", "IP"].includes(params.player)) S.player = params.player;
  if (["hand", "cats", "blockers", "graphs"].includes(params.side)) S.side = params.side;
  if (params.blk && H.index.has(params.blk)) { S.blkCombo = displayCombo(H.order[H.index.get(params.blk)]); S.blkFor = null; }
  if (params.pin && Object.prototype.hasOwnProperty.call(H.byClass, params.pin)) S.pinned = params.pin;
  document.querySelectorAll("#view-seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === S.mode));
}

export function showEmpty() {
  S.loaded = false;
  $("empty-state").classList.remove("hidden");
  for (const id of ["board", "path", "potinfo", "node-title", "strip", "matrix-note"]) $(id).textContent = "";
  $("matrix").textContent = "";
  cells = null;
  $("side-hand").textContent = "";
  $("side-blockers").textContent = "";
  $("side-graphs").textContent = "";
}

/** Browse a re-solved subtree (#117) through the /api/resolve endpoints. */
export function enterSubgame({ origin, root, label }) {
  S.base = "/api/resolve";
  S.sub = { origin, root, label };
  S.dec = null; S.decId = null; S.pinned = null;
  return gotoNode(root);
}

export function exitSubgame() {
  const origin = S.sub ? S.sub.origin : "r:0";
  S.base = "/api";
  S.sub = null;
  S.dec = null; S.decId = null;
  return gotoNode(origin);
}

/** The current decision for the lock editor (#200): ids, actions in display order (label, colour,
 *  row index k into freq), the strategy and the actor's reach, all in 1326 order. */
export function lockContext() {
  const n = S.path[S.path.length - 1];
  if (!S.loaded || S.sub || !isDec(n) || !S.cur || !S.cur.freq) return null;
  return {
    id: S.id, actor: actorOf(n), lock: lockOf(n), acts: actionsOf(S.cur, S.path.length - 1),
    freq: S.cur.freq, reach: S.cur.reach, board: boardOf(n), rootPot: potOf(S.path[0]), pot: potOf(n),
    refined: !!S.cur.refined,  // shown from the fast preview's refined street, not the main tree
    actions: S.cur.children.map((c) => lastTok(c.id)),  // the freq rows' actions, in row order
  };
}

export const study = {
  get loaded() { return S.loaded; },
  get id() { return S.id; },
  get pot() { return S.path.length ? potOf(S.path[S.path.length - 1]) : 0; },
  get inSubgame() { return !!S.sub; },
  refresh() { invalidate(); return gotoNode(S.loaded ? S.id : "r:0"); },
  reset() { S.pinned = null; invalidate(); S.base = "/api"; S.sub = null; return gotoNode("r:0"); },
};
