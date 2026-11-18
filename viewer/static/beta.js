// Beta view: features still finding their shape. The first one, Runout lines: how one hand plays the
// rest of the pot on every runout, from the node open in Study (server side: viewer/hand_lines.py).
//
// A line is the hand's own actions street by street ("bet · bet · check"). From a flop node the
// picture is a map of every runout: a row per turn card, a column per river card, and in each box the
// few lines the hand plays most often on that runout, as bands of colour, wider the more often. The
// lines that matter most over the whole map get a colour of their own (the list on the right is the
// key); the rest share a grey. From a turn node, or with one turn card enlarged, the boxes are river
// cards and big enough to carry the lines and their numbers in writing.

import { get, q, prefs, showTip, hideTip, fmt } from "./api.js";
import { RANKS, SUITS, GLYPH, el, cardEl, comboEl, displayCombo, comboClass } from "./cards.js";
import { study } from "./study.js";
import { words, short, depth, cellsAt, pick, bands, lineStats, slots, shareOf, split, deck, axis } from "./runouts.js";

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pct = (f, digits = 0) => fmt(100 * f, digits) + "%";
const STREET = { 3: "flop", 4: "turn", 5: "river" };
const COLORS = 8;   // lines with a colour of their own (--line-1 .. --line-8)
const MAX_N = 5;

const B = {
  on: false,
  hand: String(prefs.get("beta-hand", "") || ""),
  player: null,                 // OOP / IP once chosen; until then the player to act at the node
  n: clamp(+prefs.get("beta-top", 3) || 3, 1, MAX_N),   // lines shown per runout
  sizes: !!prefs.get("beta-sizes", false),
  mode: "all",                  // from a flop node: every runout ("all"), or one turn enlarged ("turn")
  data: null, key: null, seq: 0,
  stats: [], slot: new Map(),   // lineStats over the runouts, and the lines with a colour (line -> 0..7)
  turn: null,                   // the enlarged turn card
  hl: null, pin: null,          // the line the picture is a heat map of (hovered / clicked in the list)
  rows: new Map(),              // line -> its row in the list
  seenPin: null,                // the Study pin already taken over as the hand
};

/* ------------------------------------------------------------------ colours */

function palette() {
  const cs = getComputedStyle(document.documentElement);
  const g = (v) => cs.getPropertyValue(v).trim();
  const line = [];
  for (let i = 1; i <= COLORS; i++) line.push(g("--line-" + i));
  return { line, other: g("--line-other"), none: g("--cell-empty"), accent: g("--accent"), dim: g("--text-3"), spade: g("--text-2"),
           suit: { s: g("--suit-s"), h: g("--suit-h"), d: g("--suit-d"), c: g("--suit-c") } };
}
function colorOf(line, pal) {
  const k = B.slot.get(line);
  return k == null ? pal.other : pal.line[k];
}
const swatchHtml = (line, pal) => `<i class="rl-sw" style="background:${colorOf(line, pal)}"></i>`;
function swatch(line, pal) {
  const i = el("i", "rl-sw");
  i.style.background = colorOf(line, pal);
  return i;
}
const cardHtml = (c) => `<span class="rl-c s-${c[1]}">${c[0]}${GLYPH[c[1]]}</span>`;

/* ------------------------------------------------------------------ loading */

export function initBeta() {
  const seg = (id, attr, set) => $(id).querySelectorAll("button").forEach((b) => (b.onclick = () => set(b.dataset[attr])));
  seg("rl-player", "p", (p) => { B.player = p; load(); });
  seg("rl-n", "n", (n) => { B.n = +n; prefs.set("beta-top", B.n); render(); });
  seg("rl-sizes", "s", (s) => { B.sizes = s === "split"; prefs.set("beta-sizes", B.sizes); load(); });
  seg("rl-mode", "m", (m) => { B.mode = m; render(); });
  const inp = $("rl-hand");
  const apply = () => { const v = inp.value.trim(); if (v !== B.hand) setHand(v); };
  inp.onkeydown = (e) => {
    if (e.key === "Enter") { apply(); inp.blur(); }
    if (e.key === "Escape") { inp.value = B.hand; inp.blur(); }
  };
  inp.onchange = apply;
  document.addEventListener("study-node", () => { if (B.on) load(); });
  let t = null;
  addEventListener("resize", () => { if (!B.on) return; clearTimeout(t); t = setTimeout(render, 120); });
}

function setHand(hand) {
  B.hand = hand;
  prefs.set("beta-hand", hand);
  load();
}

/** Show or leave the view. params: the URL's (&hand=JJ&player=IP&n=3) on a deep link. */
export function showBeta(on, params) {
  B.on = on;
  if (!on) { hideTip(); return; }
  if (params) {
    if (params.hand) {
      try { B.hand = decodeURIComponent(params.hand); } catch { B.hand = params.hand; }   // a mangled link
    }
    if (params.player === "OOP" || params.player === "IP") B.player = params.player;
    if (params.n) B.n = clamp(+params.n || 3, 1, MAX_N);
    if (params.turn) { B.turn = params.turn; B.mode = "turn"; }
    if (params.sizes) B.sizes = params.sizes === "split";
  }
  // the class pinned in Study is the hand meant, the first time it is seen here
  const pin = study.pinned;
  if (pin && pin !== B.seenPin && !(params && params.hand)) { B.hand = pin; prefs.set("beta-hand", pin); }
  B.seenPin = pin;
  B.key = null;   // the solve may have moved on since the last look
  load();
}

function syncUrl(player) {
  if (!B.on) return;
  let h = "#beta";
  if (study.loaded) {
    h += "=" + study.id + (B.hand ? "&hand=" + encodeURIComponent(B.hand) : "") + "&player=" + player;
    if (B.mode === "turn" && B.turn && B.data && depth(B.data) === 2) h += "&turn=" + B.turn;
  }
  history.replaceState(null, "", h);
}

async function load() {
  if (!B.on) return;
  const player = B.player || study.actor || "OOP";
  syncBar(player);
  syncUrl(player);
  if (!study.loaded) return message("No tree loaded", "Build and solve a spot, or open a saved tree, then come back.");
  if (!B.hand) { B.data = null; renderSide(); return message("Pick a hand", "Type one above (JJ, AKs, AhKd), or pin a hand in Study before opening this tab."); }
  const id = study.id, base = study.base;
  const key = [base, id, player, B.hand, B.sizes].join("|");
  if (key === B.key && B.data) return render();
  const seq = ++B.seq;
  $("rl-plot").classList.add("busy");
  let data;
  try {
    data = await get(`${base}/hand_lines?id=${q(id)}&player=${player}&hand=${q(B.hand)}&sizes=${B.sizes ? 1 : 0}`);
  } catch (e) {
    if (seq !== B.seq) return;
    B.data = null; B.key = null;
    renderSide();
    return message("Nothing to show here", String((e && e.message) || e));
  } finally {
    if (seq === B.seq) $("rl-plot").classList.remove("busy");
  }
  if (seq !== B.seq) return;
  const dep = depth(data);
  const stats = lineStats(dep ? cellsAt(data, dep) : []);
  Object.assign(B, { key, data, stats, slot: slots(stats, COLORS), hl: null, pin: null });
  render();
}

function syncBar(player) {
  const act = (id, attr, v) => $(id).querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset[attr] === String(v)));
  act("rl-player", "p", player);
  act("rl-n", "n", B.n);
  act("rl-sizes", "s", B.sizes ? "split" : "merged");
  act("rl-mode", "m", B.mode);
  if (document.activeElement !== $("rl-hand")) $("rl-hand").value = B.hand;
  const d = B.data, dep = d ? depth(d) : 0;
  $("rl-mode-opt").classList.toggle("hidden", dep !== 2);
  $("rl-n-label").textContent = dep === 1 ? "Lines per river card" : "Lines per runout";
}

function message(title, text) {
  hideTip();
  const plot = $("rl-plot");
  plot.textContent = "";
  const card = el("div", "empty-card");
  card.append(el("div", "empty-title", title), el("p", "", text));
  const box = el("div", "rl-empty");
  box.appendChild(card);
  plot.appendChild(box);
}

/* ------------------------------------------------------------------ render */

function render() {
  if (!B.on) return;
  const d = B.data;
  if (!d) return;
  syncBar(d.player);
  renderSpot(d);
  renderSide();
  paint();
  syncUrl(d.player);
}

/** The picture alone (also when a line is pointed at in the list, which must stay under the pointer). */
function paint() {
  const d = B.data;
  if (!d) return;
  hideTip();
  if (!d.now) {
    const inRange = d.combos.length > 0;
    return message(`${d.hand} is not here`, inRange ? `${d.player} never reaches this node with ${d.hand}.` : `The board takes every combo of ${d.hand}.`);
  }
  const dep = depth(d);
  if (dep === 0) return message("No cards to come", `What ${d.hand} does from here is on the right.`);
  if (dep === 1) return renderBig(d, d.now.cards, [], null);
  const open = B.mode === "turn" && (d.now.cards.find((t) => t.card === B.turn && t.cards && t.cards.length) || d.now.cards.find((t) => t.cards && t.cards.length));
  if (!open) return renderMap(d);
  B.turn = open.card;
  renderBig(d, open.cards, [open.card], zoomHead(d, open));
}

function renderSpot(d) {
  const box = $("rl-spot");
  box.textContent = "";
  for (const c of d.board) box.appendChild(cardEl(c, "sm"));
  box.appendChild(el("code", "", d.id));
  const combos = $("rl-combos");
  combos.textContent = "";
  const single = d.combos.length === 1 && d.hand.length === 4;
  for (const { combo, reach } of d.combos) {
    const b = el("button", "blk-combo" + (single ? " sel" : "") + (reach > 0 ? "" : " out"));
    b.appendChild(comboEl(combo, "xs"));
    if (reach > 0 && reach < 0.995) b.appendChild(el("span", "w", pct(reach)));
    b.title = `${displayCombo(combo)}: ${pct(reach)} in ${d.player}'s range here` + (single ? ". Click for the whole class" : ". Click for this combo alone");
    b.onclick = () => setHand(single ? comboClass(combo) : displayCombo(combo));
    combos.appendChild(b);
  }
}

function caption(...parts) {
  const c = el("div", "rl-cap");
  for (const p of parts) c.appendChild(typeof p === "string" ? el("span", "", p) : p);
  return c;
}

/** The caption of a heat map: the line it is of. */
function heatCaption(d, hl, pal, where) {
  return caption(swatch(hl, pal), el("b", "", words(d.lines[hl])), `how often ${d.hand} plays this line ${where}: the fuller the colour, the more often.`);
}

/** Light up the rows of the list whose lines a box shows (none: put the list back). */
function markRows(lines) {
  for (const [line, row] of B.rows) row.classList.toggle("in", !!lines && lines.includes(line));
}

/* ------------------------------------------------------------------ every runout: the turn x river map */

function renderMap(d) {
  const plot = $("rl-plot");
  plot.textContent = "";
  const hl = B.hl != null ? B.hl : B.pin;
  const pal = palette();
  const cards = deck(d.board);
  const LEFT = 44, TOP = 46, GROUP = 7;
  const groups = new Set(cards.map((c) => c[1])).size - 1;
  const W = plot.clientWidth - 40 - LEFT, H = plot.clientHeight - 62 - TOP;
  const cw = clamp(Math.floor((W - groups * GROUP) / cards.length) - 1, 8, 28);
  const ch = clamp(Math.floor((H - groups * GROUP) / cards.length) - 1, 8, Math.min(cw, 20));
  const X = axis(cards, cw, GROUP), Y = axis(cards, ch, GROUP);
  const index = new Map(cards.map((c, i) => [c, i]));

  const box = el("div", "rl-view");
  if (hl != null) box.appendChild(heatCaption(d, hl, pal, "on each runout"));
  else {
    box.appendChild(caption(el("b", "", "Every runout"),
      `A row per turn card, a column per river card. Each box holds the ${B.n === 1 ? "line" : B.n + " lines"} ${d.hand} plays most often on that runout`
      + (B.n === 1 ? "." : ", wider the more often.")));
  }

  const w = LEFT + X.end + 1, h = TOP + Y.end + 1;
  const wrap = el("div", "rl-mapw");
  const c = el("canvas"), cur = el("i", "rl-cur hidden"), rowTag = el("span", "rl-tag hidden"), colTag = el("span", "rl-tag hidden");
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  for (const e of [c, wrap]) { e.style.width = w + "px"; e.style.height = h + "px"; }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  // the two sides: the card of every row and column, and what they are
  const sans = getComputedStyle(document.body).fontFamily;
  ctx.textBaseline = "middle";
  ctx.fillStyle = pal.dim;
  ctx.font = `600 9px ${sans}`;
  ctx.textAlign = "left";
  ctx.fillText("RIVER CARD", LEFT, 7);
  ctx.save();
  ctx.translate(7, TOP);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "right";
  ctx.fillText("TURN CARD", 0, 0);
  ctx.restore();
  const fs = clamp(Math.min(cw, ch + 2) - 1, 8, 11);
  ctx.font = `600 ${fs}px ${sans}`;
  cards.forEach((card, i) => {
    ctx.fillStyle = card[1] === "s" ? pal.spade : pal.suit[card[1]];   // the spades' own grey is for chips
    ctx.textAlign = "center";
    ctx.fillText(card[0], LEFT + X.at[i] + cw / 2, TOP - 20);
    ctx.fillText(GLYPH[card[1]], LEFT + X.at[i] + cw / 2, TOP - 8);
    ctx.textAlign = "right";
    ctx.fillText(card[0] + GLYPH[card[1]], LEFT - 6, TOP + Y.at[i] + ch / 2 + 0.5);
  });

  // the boxes
  const label = hl != null ? d.lines[hl] : null;
  let max = 0;
  if (label != null) for (const { cell } of cellsAt(d, 2)) max = Math.max(max, shareOf(d.lines, cell, label));
  const cellOf = new Map();   // "turn river" -> the river's cell
  for (const t of d.now.cards) {
    const j = index.get(t.card);
    for (const r of t.cards || []) {
      const i = index.get(r.card);
      if (i == null || j == null) continue;
      cellOf.set(t.card + r.card, r);
      const x = LEFT + X.at[i], y = TOP + Y.at[j];
      ctx.fillStyle = pal.none;
      ctx.fillRect(x, y, cw, ch);
      if (label != null) {
        const v = max > 0 ? shareOf(d.lines, r, label) / max : 0;
        if (v > 0) { ctx.globalAlpha = clamp(v, 0.08, 1); ctx.fillStyle = colorOf(hl, pal); ctx.fillRect(x, y, cw, ch); ctx.globalAlpha = 1; }
        continue;
      }
      const bs = bands(r, B.n);
      const ws = split(cw - (bs.length - 1), bs.map((b) => b.share));
      let xx = x;
      bs.forEach((b, k) => {
        if (!(ws[k] > 0)) return;
        ctx.fillStyle = colorOf(b.line, pal);
        ctx.fillRect(xx, y, ws[k], ch);
        xx += ws[k] + 1;
      });
    }
  }

  const turns = new Map(d.now.cards.map((t) => [t.card, t]));
  const at = (ev) => {
    const b = c.getBoundingClientRect();
    const i = X.index(ev.clientX - b.left - LEFT), j = Y.index(ev.clientY - b.top - TOP);
    return { i, j, turn: cards[j], river: cards[i], cell: cellOf.get(cards[j] + cards[i]), label: ev.clientX - b.left < LEFT };
  };
  const tag = (e, card, left, top) => {
    e.className = `rl-tag rl-c s-${card[1]}`;
    e.textContent = card[0] + GLYPH[card[1]];
    Object.assign(e.style, { left: left + "px", top: top + "px" });
  };
  const off = () => { for (const e of [cur, rowTag, colTag]) e.classList.add("hidden"); hideTip(); markRows(null); c.style.cursor = ""; };
  c.onmousemove = (ev) => {
    const hit = at(ev), t = turns.get(hit.turn);
    if (hit.label && t && t.cards && t.cards.length) {   // a row's card: enlarge that turn
      off();
      c.style.cursor = "pointer";
      tag(rowTag, hit.turn, LEFT - 4, TOP + Y.at[hit.j] + ch / 2);
      return showTip(ev, `<div class="t">Turn ${cardHtml(hit.turn)}</div><div class="rl-tip-sub">Click to enlarge its river cards</div>`);
    }
    if (!hit.cell) return off();
    cur.classList.remove("hidden");
    Object.assign(cur.style, { left: LEFT + X.at[hit.i] - 2 + "px", top: TOP + Y.at[hit.j] - 2 + "px", width: cw + 4 + "px", height: ch + 4 + "px" });
    tag(rowTag, hit.turn, LEFT - 4, TOP + Y.at[hit.j] + ch / 2);
    tag(colTag, hit.river, LEFT + X.at[hit.i] + cw / 2, TOP - 4);
    colTag.classList.add("col");
    c.style.cursor = "pointer";
    markRows(label != null ? null : bands(hit.cell, B.n).map((b) => b.line));
    showTip(ev, tipHtml(d, [hit.turn, hit.river], hit.cell));
  };
  c.onmouseleave = off;
  c.onclick = (ev) => {
    const hit = at(ev), t = turns.get(hit.turn);
    if (hit.label && t && t.cards && t.cards.length) { B.turn = hit.turn; B.mode = "turn"; return render(); }
    if (hit.cell) openStudy(d, hit.cell);
  };
  wrap.append(c, cur, rowTag, colTag);
  box.appendChild(wrap);
  plot.appendChild(box);
}

/* ------------------------------------------------------------------ one card at a time: boxes with writing */

/** The bar over an enlarged turn: back to every runout, or straight to another turn. */
function zoomHead(d, open) {
  const head = el("div", "rl-zoom");
  const back = el("button", "rl-back", "‹ Every runout");
  back.onclick = () => { B.mode = "all"; render(); };
  head.append(back, el("span", "lbl", "Turn"));
  const strip = el("div", "rl-strip");
  for (const t of d.now.cards) {
    if (!t.cards || !t.cards.length) continue;
    const b = el("button", `rl-chip s-${t.card[1]}` + (t.card === open.card ? " sel" : ""), t.card[0] + GLYPH[t.card[1]]);
    b.onclick = () => { B.turn = t.card; render(); };
    strip.appendChild(b);
  }
  head.appendChild(strip);
  return head;
}

/** cells: the river cards of the node itself or of the enlarged turn; before: the cards dealt on the
 *  way ([] or [turn]). A column per rank, a row per suit. */
function renderBig(d, cells, before, head) {
  const plot = $("rl-plot");
  plot.textContent = "";
  const hl = B.hl != null ? B.hl : B.pin;
  const pal = palette();
  const next = STREET[d.board.length + before.length + 1];
  const box = el("div", "rl-view");
  if (head) box.appendChild(head);
  const label = hl != null ? d.lines[hl] : null;
  let max = 0;
  if (label != null) {
    for (const c of cells) max = Math.max(max, shareOf(d.lines, c, label));
    box.appendChild(heatCaption(d, hl, pal, `on each ${next} card`));
  } else {
    box.appendChild(caption(el("b", "", before.length ? `Every river on the ${before[0][0]}${GLYPH[before[0][1]]} turn` : `Every ${next} card`),
      `Each box holds the ${B.n === 1 ? "line" : B.n + " lines"} ${d.hand} plays most often on that card, and how often (%).`));
  }
  const g = el("div", "rl-grid");
  const by = new Map(cells.map((c) => [c.card, c]));
  for (const s of SUITS) {
    for (const r of RANKS) {
      const card = r + s, cell = by.get(card);
      if (!cell) { g.appendChild(el("div", "rl-card off")); continue; }
      const b = el("button", "rl-card");
      b.appendChild(el("span", `rl-card-h s-${s}`, r + GLYPH[s]));
      if (label != null) {
        const f = shareOf(d.lines, cell, label);
        const v = el("span", "rl-heat", f >= 0.005 ? pct(f) : "");
        if (f > 0) v.style.background = `color-mix(in srgb, ${colorOf(hl, pal)} ${Math.round(clamp(f / max, 0.08, 1) * 100)}%, transparent)`;
        b.appendChild(v);
      } else {
        const bs = bands(cell, B.n);
        const bar = el("span", "rl-stack");
        for (const x of bs) {
          const seg = el("i");
          seg.style.flex = String(x.f);
          seg.style.background = colorOf(x.line, pal);
          bar.appendChild(seg);
        }
        const rest = el("i", "rest");
        rest.style.flex = String(pick(cell, B.n).other);
        bar.appendChild(rest);
        b.appendChild(bar);
        for (const x of bs) {
          const row = el("span", "rl-ln");
          row.append(swatch(x.line, pal), el("span", "t", short(d.lines[x.line])), el("span", "p", fmt(100 * x.f, 0)));
          b.appendChild(row);
        }
        b.onmouseenter = () => markRows(bs.map((x) => x.line));
      }
      b.onmousemove = (ev) => showTip(ev, tipHtml(d, before.concat(card), cell));
      b.onmouseleave = () => { hideTip(); markRows(null); };
      b.onclick = () => openStudy(d, cell);
      g.appendChild(b);
    }
  }
  box.appendChild(g);
  plot.appendChild(box);
}

/* ------------------------------------------------------------------ tooltip, side panel */

function tipHtml(d, cards, cell) {
  const names = d.board.length === 3 ? ["Turn", "River"] : ["River"];
  const pal = palette();
  let h = `<div class="t">${cards.map((c, i) => `${names[i]} ${cardHtml(c)}`).join(" &nbsp; ")}</div>`;
  let sum = 0;
  cell.lines.slice(0, Math.max(B.n, 5)).forEach(([i, f], k) => {
    sum += f;
    h += `<div class="r${k < B.n ? "" : " rl-tip-more"}"><span>${swatchHtml(i, pal)}${words(d.lines[i])}</span><b>${pct(f)}</b></div>`;
  });
  if (sum < 0.995) h += `<div class="r rl-tip-other"><span>other lines</span><b>${pct(1 - sum)}</b></div>`;
  if (cell.reach < 0.995) h += `<div class="rl-tip-sub">${d.hand} gets to this ${names[cards.length - 1].toLowerCase()} ${pct(cell.reach)} of the time</div>`;
  h += `<div class="rl-tip-sub">Click to open it in Study</div>`;
  return h;
}

function openStudy(d, cell) {
  if (!cell.go) return;
  hideTip();
  location.hash = "#study=" + d.id + ":" + cell.go;
}

function lineRow(line, label, pal, ...right) {
  const row = el("div", "rl-row");
  if (line != null) row.appendChild(swatch(line, pal));
  const text = el("span", "t", words(label));
  text.title = words(label);   // a split-size line can be longer than the column
  row.appendChild(text);
  right.forEach((r, k) => row.appendChild(el("span", k ? "m" : "p", r)));
  return row;
}

function renderSide() {
  const side = $("rl-side");
  side.textContent = "";
  B.rows = new Map();
  const d = B.data;
  if (!d || !d.now) return;
  const dep = depth(d), street = STREET[d.board.length], pal = palette();

  // what the hand does on this street
  const now = el("section", "rl-sec");
  now.appendChild(el("h3", "", `${d.hand} on the ${street}`));
  for (const [i, f] of d.now.lines.slice(0, 6)) {
    const row = lineRow(null, d.lines[i], pal, pct(f));
    row.style.setProperty("--f", String(f));
    row.classList.add("meter");
    now.appendChild(row);
  }
  side.appendChild(now);

  if (dep > 0) {
    const cells = cellsAt(d, dep), what = dep === 2 ? "runouts" : "river cards";
    const sec = el("section", "rl-sec");
    sec.appendChild(el("h3", "", "Lines"));
    sec.appendChild(el("div", "hint", `Over the ${cells.length} ${what}. Point at a line to see where it is played; click to keep it.`));
    const head = el("div", "rl-row head");
    head.append(el("span", "t", "line"), el("span", "p", "top on"), el("span", "m", "avg"));
    head.title = `top on: the share of the ${what} where it is the most common line. avg: how often it is played on an average one.`;
    const list = el("div", "rl-list");
    const SHOWN = 16;
    for (const s of B.stats.slice(0, SHOWN)) {
      const row = lineRow(s.line, d.lines[s.line], pal, !s.lead ? "–" : s.lead / cells.length < 0.005 ? "<1%" : pct(s.lead / cells.length), pct(s.mean));
      row.title = `The most common line on ${s.lead} of the ${cells.length} ${what}. Played ${pct(s.mean, 1)} of the time on an average one, ${pct(s.max)} at most.`;
      row.classList.add("pick");
      row.classList.toggle("sel", B.pin === s.line);
      row.onmouseenter = () => { B.hl = s.line; paint(); };
      row.onmouseleave = () => { B.hl = null; paint(); };
      row.onclick = () => {
        B.pin = B.pin === s.line ? null : s.line;
        list.querySelectorAll(".rl-row").forEach((x) => x.classList.toggle("sel", x === row && B.pin != null));
        paint();
      };
      B.rows.set(s.line, row);
      list.appendChild(row);
    }
    sec.append(head, list);
    if (B.stats.length > SHOWN) {
      const more = el("div", "rl-row rest");
      more.append(el("i", "rl-sw other"), el("span", "t", `${B.stats.length - SHOWN} rarer lines`));
      sec.appendChild(more);
    }
    side.appendChild(sec);
  }

  // how to read it
  const key = el("section", "rl-sec");
  key.appendChild(el("h3", "", "Reading it"));
  key.appendChild(el("div", "hint", `A line is what ${d.hand} itself does, street by street: bet · bet · check is a bet on the ${street}, `
    + (d.board.length === 3 ? "a bet on the turn and a check on the river. " : "then a check on the river. ")
    + `Two letters on one street are two actions there: xc check then call, xf check then fold, xr check-raise, bc bet then call, bf bet then fold. `
    + `A dash is a street with nothing left to do.`));
  if (dep > 0) {
    key.appendChild(el("div", "hint", `The percentages are of the times ${d.hand} gets to that card: the pot has not ended before it.`
      + (B.stats.length > COLORS ? ` The ${COLORS} lines that weigh most in the picture have a colour of their own; the others are grey.` : "")));
  }
  side.appendChild(key);
}
