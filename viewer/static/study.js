// Study view (#112): browse a solved tree. The layout is fixed: board + action line on top,
// action strip and 13x13 matrix in the middle, details on the right. Navigating only swaps
// data inside these regions, so nothing moves while you click through a line.

import { H, RANKS, SUITS, GLYPH, el, cardEl, comboEl, parseCards, blockedBy } from "./cards.js";
import { get, q, fail, showTip, hideTip, fmt, fmtChips } from "./api.js";

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
  cats: null,        // {key, data} cache for the Categories tab (#154)
  catsPlayer: null,  // OOP/IP override in that tab; null = the actor
  catHl: null,       // Set of classes highlighted by a hovered category row
  catPin: null,      // {name, classes} pinned by clicking a category row
  side: "hand",
  seq: 0,            // navigation sequence, drops stale responses
  loaded: false,
  base: "/api",      // "/api/resolve" while browsing a re-solved subtree (#117)
  sub: null,         // {origin, root, label} in subtree mode
};

/* ------------------------------------------------------------------ actions */

const COL = { fold: "#5b8def", check: "#4cc38a", call: "#4cc38a", allin: "#9b51e0" };
const BET_STOPS = [[0.15, [246, 196, 69]], [0.5, [243, 146, 55]], [1.0, [232, 83, 74]], [2.0, [194, 54, 110]]];

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
function actionsOf(data, pi) {
  const node = data.node;
  const acts = data.children.map((c, k) => ({ k, id: c.id, ...describe(node, lastTok(c.id), pi, S.path) }));
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
    if (S.pinned && !H.byClass[S.pinned]) S.pinned = null;
    // a pinned/hovered category is board-dependent: it does not survive navigation
    S.catPin = null; S.catHl = null;
    if (S.mode !== "strategy") await loadModeData();
    if (seq !== S.seq) return;
    render();
    if (!S.sub && !document.getElementById("view-study").classList.contains("hidden")) history.replaceState(null, "", "#study=" + id + urlState());
  } catch (e) {
    if (seq === S.seq) fail(e);
  }
}

async function loadModeData() {
  if (!S.range) S.range = await get(`${S.base}/range?id=` + q(S.id));
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
    b.appendChild(cardEl(c, "lg"));
  });
  for (let i = cards.length; i < 5 && cards.length >= 3; i++) {
    if (i === 3 || i === 4) b.appendChild(el("span", "gap"));
    const ph = el("span", "pc lg placeholder");
    ph.appendChild(el("span", "r", i === 3 ? "T" : "R"));
    ph.title = i === 3 ? "turn" : "river";
    b.appendChild(ph);
  }
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

function evColor(v, scale) {
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
    note.textContent = S.decId !== S.id ? `Last decision: ${d.actor} at ${S.decId} — pick a card to continue` : "";
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
          const e = evd.ev[i], mu = evd.matchups ? evd.matchups[i] : 1, rw = w[i] || 0;
          if (e == null || !(mu > 0) || rw <= 0) continue;
          num += rw * mu * e; den += rw * mu;
        }
        const v = den > 0 ? num / den : null;
        color = v == null ? "var(--surface-3)" : evColor(v, pot * 0.75);
        text = v == null ? "" : fmtChips(v);
      } else {
        text = cw.w < 0.995 ? Math.round(cw.w * 100) + "" : "";
      }
      setSegments(x.fill, [[color, 1]]);
      x.fill.style.height = (Math.min(1, cw.w) * 100).toFixed(1) + "%";
      x.c.classList.toggle("on-fill", cw.w > 0.62);
      x.val.textContent = text;
    }
    note.textContent = S.mode === "ev" ? `${pl} EV per class, in chips (pot ${fmtChips(pot)})` : `${pl} range at this node — bar height = weight`;
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
  for (const k of ["hand", "node", "ranges", "cats"]) $("side-" + k).classList.toggle("hidden", k !== S.side);
  document.querySelectorAll(".side-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.side === S.side));
  if (S.side === "hand") renderHand();
  else if (S.side === "node") renderNodePanel();
  else if (S.side === "ranges") renderRangesPanel();
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
    const c4 = el("td", "ev num", w > 0 && ev != null ? fmtChips(ev) : "—");
    tr.append(c1, c2, c3, c4);
    if (w <= 0) tr.style.opacity = ".4";
    t.appendChild(tr);
  }
  box.appendChild(t);
}

async function renderNodePanel() {
  const box = $("side-node");
  const n = S.path[S.path.length - 1];
  const seq = S.seq;
  box.textContent = "";
  const grid = el("div", "kv-grid");
  const kv = (k, v) => { const d = el("div", "kv"); d.append(el("div", "k", k), el("div", "v num", v)); grid.appendChild(d); };
  const pot = potOf(n);
  kv("Pot", fmtChips(pot));
  kv("Behind", S.eff ? fmtChips(S.eff - Math.max(n.pot[0], n.pot[1])) : "—");
  const evBox = el("div", "kv-grid");
  box.append(el("h3", null, "Spot"), grid, el("h3", null, "Range EV at this node"), evBox);
  if (isDec(n) && S.cur.freq) {
    const acts = actionsOf(S.cur, S.path.length - 1);
    const o = overallFreqs(S.cur, acts);
    box.appendChild(el("h3", null, `${actorOf(n)} actions`));
    const t = el("table", "act-table");
    t.innerHTML = "<tr><th>Action</th><th>Freq</th><th>Combos</th></tr>";
    acts.forEach((a, j) => {
      const tr = el("tr");
      const c = el("td");
      const sw = el("span", "sw"); sw.style.background = a.color;
      c.append(sw, document.createTextNode(`${a.label} ${a.sub}`.trim()));
      tr.append(c, el("td", "num", fmt(o.freqs[j] * 100) + "%"), el("td", "num", fmt(o.combos[j])));
      t.appendChild(tr);
    });
    box.appendChild(t);
  }
  try {
    for (const pl of ["OOP", "IP"]) {
      if (!S.ev[pl]) S.ev[pl] = await get(`${S.base}/ev?id=${q(S.id)}&player=${pl}`);
    }
    if (!S.range) S.range = await get(`${S.base}/range?id=` + q(S.id));
    if (seq !== S.seq || S.side !== "node") return;
    for (const pl of ["OOP", "IP"]) {
      const e = S.ev[pl], w = pl === "OOP" ? S.range.oop : S.range.ip;
      let num = 0, den = 0;
      for (let i = 0; i < 1326; i++) {
        const mu = e.matchups ? e.matchups[i] : 1;
        if (e.ev[i] == null || !(mu > 0) || !(w[i] > 0)) continue;
        num += w[i] * mu * e.ev[i]; den += w[i] * mu;
      }
      const d = el("div", "kv");
      d.append(el("div", "k", pl), el("div", "v num", den > 0 ? fmtChips(num / den) : "—"));
      if (den > 0 && pot > 0) d.appendChild(el("div", "muted", fmt((num / den / pot) * 100) + "% of pot"));
      evBox.appendChild(d);
    }
  } catch (e) { fail(e); }
}

async function renderRangesPanel() {
  const box = $("side-ranges");
  const seq = S.seq;
  try {
    if (!S.range) S.range = await get(`${S.base}/range?id=` + q(S.id));
  } catch (e) { fail(e); return; }
  if (seq !== S.seq || S.side !== "ranges") return;
  box.textContent = "";
  const blocked = blockedBy(boardOf(S.path[S.path.length - 1]));
  for (const pl of ["OOP", "IP"]) {
    const w = pl === "OOP" ? S.range.oop : S.range.ip;
    let combos = 0;
    for (let i = 0; i < 1326; i++) if (!blocked(H.order[i])) combos += w[i] || 0;
    const head = el("div", "range-head");
    head.append(el("span", `badge ${pl.toLowerCase()}`, pl), el("span", "muted num", `${fmt(combos)} combos`));
    box.appendChild(head);
    const mm = el("div", "mini-matrix");
    for (const cls of H.classes) {
      const cw = classWeight(w, cls, blocked);
      const c = el("div");
      const f = el("i");
      f.style.height = (Math.min(1, cw.w) * 100).toFixed(1) + "%";
      f.style.background = `var(--${pl.toLowerCase()})`;
      c.appendChild(f);
      c.title = `${cls}: ${fmt(cw.w * 100, 0)}%`;
      mm.appendChild(c);
    }
    box.appendChild(mm);
  }
}

/* ---- categories tab (#154): made-hand and draw breakdown of the range at hand ---- */

const DRAW_LABEL = {
  no_draw: "No draw", bdfd_1card: "Backdoor FD (1 card)", bdfd_2card: "Backdoor FD (2 cards)",
  "4out_straight_draw": "4-out straight draw", "8out_straight_draw": "8-out straight draw",
  flush_draw: "Flush draw", combo_draw: "Combo draw",
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
    box.textContent = "";
    box.appendChild(el("div", "side-empty", "Categories need a flop (or later) decision — preflop nodes have no board."));
    return;
  }
  const pl = S.catsPlayer || d.actor;
  const key = `${S.base}|${S.decId}|${pl}`;
  if (!S.cats || S.cats.key !== key) {
    let data;
    try { data = await get(`${S.base}/categories?id=${q(S.decId)}&player=${pl}`); } catch (e) { fail(e); return; }
    S.cats = { key, data };
  }
  if (seq !== S.seq || S.side !== "cats") return;
  const data = S.cats.data;
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
  const section = (title, rows) => {
    box.appendChild(el("h3", null, title));
    rows.forEach((c) => box.appendChild(catRow(c, acts, box)));
  };
  section("Made hands", data.made);
  const draws = data.draws.filter((c) => c.name !== "no_draw" || data.draws.length > 1);
  if (draws.length) section("Draws", draws);
  box.appendChild(el("div", "hint cats-note", "Hover a row to light up its hands in the matrix; click to pin."));
}

function catRow(c, acts, box) {
  const row = el("div", "cat-row");
  row.dataset.cat = c.name;
  if (S.catPin && S.catPin.name === c.name) row.classList.add("sel");
  const top = el("div", "cat-top");
  top.append(el("span", "cat-name", prettyCat(c.name)),
             el("span", "cat-share num", `${fmt(c.combos, c.combos < 10 ? 1 : 0)} c · ${fmt(c.pct * 100, c.pct < 0.1 ? 1 : 0)}%`),
             el("span", "cat-ev num", c.ev != null ? fmtChips(c.ev) : "—"));
  top.title = "Combos and share of the range; EV in chips (reach- and matchup-weighted)";
  row.appendChild(top);
  if (c.strategy) {
    const bar = el("div", "mini-bar cat-bar");
    const tip = `<div class="t">${prettyCat(c.name)}</div>` +
      acts.map((a) => `<div class="r"><span>${`${a.label} ${a.sub}`.trim()}</span><b>${fmt((c.strategy[a.k] || 0) * 100)}%</b></div>`).join("");
    acts.forEach((a) => {
      const f = c.strategy[a.k] || 0;
      if (f <= 0.0005) return;
      const sp = el("span");
      sp.style.background = a.color;
      sp.style.width = (f * 100).toFixed(1) + "%";
      bar.appendChild(sp);
    });
    row.appendChild(bar);
    const mix = el("div", "cat-mix");
    acts.forEach((a) => {
      const f = c.strategy[a.k] || 0;
      if (f <= 0.0005) return;
      const itm = el("span", "cat-mix-item");
      const sw = el("span", "sw");
      sw.style.background = a.color;
      itm.append(sw, el("span", null, `${a.label} ${a.sub}`.trim()), el("b", null, fmt(f * 100, f < 0.1 ? 1 : 0) + "%"));
      mix.appendChild(itm);
    });
    row.appendChild(mix);
    row.onmousemove = (ev) => showTip(ev, tip);
  }
  row.onmouseenter = () => { S.catHl = new Set(c.classes); if (!S.catPin) markCatHl(); };
  row.onmouseleave = () => { S.catHl = null; hideTip(); if (!S.catPin) markCatHl(); };
  row.onclick = () => {
    S.catPin = S.catPin && S.catPin.name === c.name ? null : { name: c.name, classes: new Set(c.classes) };
    markCatHl();
    box.querySelectorAll(".cat-row").forEach((r) => r.classList.toggle("sel", !!S.catPin && S.catPin.name === r.dataset.cat));
  };
  return row;
}

/** Up one node: the parent (deals are steps too); at a re-solved subtree's root, back to the original. */
function goUp() {
  if (S.sub && S.id === S.sub.root) { exitSubgame(); return; }
  if (S.path.length > 1) gotoNode(S.path[S.path.length - 2].id);
}

/* ------------------------------------------------------------------ wiring */

export function initStudy() {
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
  return s;
}

export function applyUrlState(params) {
  if (["strategy", "range", "ev"].includes(params.mode)) S.mode = params.mode;
  if (["OOP", "IP"].includes(params.player)) S.player = params.player;
  if (["hand", "node", "ranges", "cats"].includes(params.side)) S.side = params.side;
  if (params.pin && H.byClass[params.pin]) S.pinned = params.pin;
  document.querySelectorAll("#view-seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === S.mode));
}

export function showEmpty() {
  S.loaded = false;
  $("empty-state").classList.remove("hidden");
  for (const id of ["board", "path", "potinfo", "node-title", "strip", "matrix-note"]) $(id).textContent = "";
  $("matrix").textContent = "";
  cells = null;
  $("side-hand").textContent = "";
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

export const study = {
  get loaded() { return S.loaded; },
  get id() { return S.id; },
  get inSubgame() { return !!S.sub; },
  refresh() { return gotoNode(S.loaded ? S.id : "r:0"); },
  reset() { S.pinned = null; S.decId = null; S.dec = null; S.base = "/api"; S.sub = null; return gotoNode("r:0"); },
};
