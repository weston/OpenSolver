// Study side panel, Beta tab: features still finding their shape. The first one, Runout lines: how
// the hand pinned in the matrix plays the rest of the pot on every runout, from the node being
// viewed (server side: viewer/hand_lines.py).
//
// A line is the hand's own actions street by street ("bet · bet · check"). From a flop node the
// picture is a map of every runout: a row per turn card, a column per river card, and in each box the
// few lines the hand plays most often on that runout, as bands of colour, taller the more often. The
// lines that matter most over the whole map get a colour of their own (the list under the map is the
// key); the rest share a grey. From a turn node, or with one turn card enlarged, it is a bar per
// river card.
//
// The hand is never typed: it is the class clicked in the matrix, or one of its combos clicked here.

import { get, q, prefs, showTip, hideTip, fmt } from "./api.js";
import { H, RANKS, SUITS, GLYPH, el, comboEl, displayCombo, comboClass, blockedBy } from "./cards.js";
import { sideCache } from "./sidecache.js";
import { words, depth, cellsAt, bands, lineStats, slots, shareOf, split, deck, axis } from "./runouts.js";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pct = (f, digits = 0) => fmt(100 * f, digits) + "%";
const STREET = { 3: "flop", 4: "turn", 5: "river" };
const COLORS = 8;   // lines with a colour of their own (--line-1 .. --line-8)
const MAX_N = 5;
const LISTED = 12;  // lines in the list; the rest are counted
const CAP = 32, GAP = 8, HEAD = 26, CARD_H = 13;   // the picture's fixed heights (style.css: .rl-cap, .rl-plot, .rl-zoom, .rl-card-h)

const B = {
  n: clamp(+prefs.get("beta-top", 3) || 3, 1, MAX_N),   // lines shown per runout
  sizes: !!prefs.get("beta-sizes", false),               // each bet size its own line
  combo: null,                  // one combo of the pinned class, when narrowed to it
  mode: "all",                  // from a flop node: every runout ("all"), or one turn enlarged ("turn")
  turn: null,                   // the enlarged turn card
  hl: null, pin: null,          // the line the picture is a heat map of (hovered / clicked in the list)
  ctx: null,                    // what Study shows (renderRunouts)
  want: null,                   // the key of the report the panel is to show
  err: null,                    // why the last ask failed
  data: null,                   // the report drawn
  stats: [], slot: new Map(),   // lineStats over the runouts, and the lines with a colour (line -> 0..7)
  reach: new Map(),             // "base|id|player" -> Map(combo -> reach): which combos get here at all
  rows: new Map(),              // line -> its row in the list
  el: null,                     // the panel's parts
  size: "",                     // the room the picture was drawn for
  watching: false,              // the panel's size and the theme are being watched
};

const cache = sideCache(get, (e) => { B.err = e; });

/** Forget what was read from the tree (a solve ended, a lock, a re-solve): the next render asks again. */
export function forgetRunouts() {
  cache.invalidate();
  B.reach.clear();
}

/* ------------------------------------------------------------------ colours */

function palette() {
  const cs = getComputedStyle(document.documentElement);
  const g = (v) => cs.getPropertyValue(v).trim();
  const line = [];
  for (let i = 1; i <= COLORS; i++) line.push(g("--line-" + i));
  return { line, other: g("--line-other"), none: g("--cell-empty"), dim: g("--text-3"), text: g("--text"), spade: g("--text-2"),
           suit: { s: g("--suit-s"), h: g("--suit-h"), d: g("--suit-d"), c: g("--suit-c") } };
}
/** A line's colour for the canvas (a value), and for the page (the variable: it follows the theme). */
function colorOf(line, pal) {
  const k = B.slot.get(line);
  return k == null ? pal.other : pal.line[k];
}
function cssColor(line) {
  const k = B.slot.get(line);
  return k == null ? "var(--line-other)" : `var(--line-${k + 1})`;
}
const swatchHtml = (line) => `<i class="rl-sw" style="background:${cssColor(line)}"></i>`;
function swatch(line) {
  const i = el("i", "rl-sw");
  i.style.background = cssColor(line);
  return i;
}
const cardHtml = (c) => `<span class="rl-c s-${c[1]}">${c[0]}${GLYPH[c[1]]}</span>`;
const cardTag = (c) => el("span", `rl-c s-${c[1]}`, c[0] + GLYPH[c[1]]);

/* ------------------------------------------------------------------ the panel */

/** The panel's parts, built once: an empty state, and the hand, its combos, the picture, the options,
 *  the list of lines, what the hand does on this street, and how to read it. */
function ensure(box) {
  if (B.el && B.el.box === box && B.el.main.parentNode === box) return B.el;
  box.textContent = "";
  box.dataset.key = "";
  const empty = el("div", "side-empty", "Click a hand in the matrix to see the lines it takes on every runout from here.");
  const main = el("div", "rl-main hidden");
  const head = el("div", "hand-head rl-head"), combos = el("div", "blk-combos rl-combos"), opts = el("div", "rl-opts");
  const plot = el("div", "rl-plot"), legend = el("section", "rl-sec"), now = el("section", "rl-sec"), key = el("section", "rl-sec");

  const seg = (label, title, items, pick) => {
    const o = el("div", "rl-opt");
    o.title = title;
    const s = el("div", "seg small");
    for (const [v, text, tip] of items) {
      const b = el("button", "", text);
      b.dataset.v = v;
      if (tip) b.title = tip;
      b.onclick = () => pick(v);
      s.appendChild(b);
    }
    o.append(el("span", "", label), s);
    opts.appendChild(o);
    return s;
  };
  const nSeg = seg("Lines", "How many lines each runout shows: its most common ones", [1, 2, 3, 4, 5].map((n) => [String(n), String(n)]), (v) => {
    B.n = +v;
    prefs.set("beta-top", B.n);
    syncOpts();
    paint();
  });
  const sSeg = seg("Bet sizes", "", [["merged", "Merged", "A bet is a bet, whatever its size"], ["split", "Split", "Each bet size is its own line"]], (v) => {
    B.sizes = v === "split";
    prefs.set("beta-sizes", B.sizes);
    syncOpts();
    again();
  });

  main.append(head, combos, plot, opts, legend, now, key);
  box.append(empty, main);
  B.el = { box, empty, main, head, combos, opts, plot, legend, now, key, nSeg, sSeg };
  B.data = null;
  B.want = null;
  if (!B.watching) {
    B.watching = true;
    // the picture is drawn for the room it has: the panel is dragged wider, the window resized, a tab shown
    if (window.ResizeObserver) new ResizeObserver(() => { if (B.data && sizeKey() !== B.size) paint(); }).observe(box);
    // the canvas holds colours, not variables
    new MutationObserver(() => { if (B.data) paint(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (B.data) paint(); });
  }
  syncOpts();
  return B.el;
}

const sizeKey = () => `${B.el.plot.clientWidth}x${B.el.box.clientHeight}`;

function syncOpts() {
  const act = (s, v) => s.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.v === String(v)));
  act(B.el.nSeg, B.n);
  act(B.el.sSeg, B.sizes ? "split" : "merged");
}

/** Draw for what Study shows now, as last told. */
function again() {
  if (B.ctx && B.el) renderRunouts(B.el.box, B.ctx);
}

/** The Beta tab of Study's side panel. ctx: {base, id, board, player, cls, current(), goto(id)}: the
 *  API root, the node viewed and its board, the player whose hands the matrix shows, the class pinned
 *  in it (or null), whether Study still shows what it did when it asked, and how to go to a node.
 *  Study calls this on every render of the side panel: the report is asked for once per
 *  (node, player, hand, sizes) and the panel is redrawn only when that changes. */
export async function renderRunouts(box, ctx) {
  B.ctx = ctx;
  const E = ensure(box);
  const cls = ctx.cls;
  E.empty.classList.toggle("hidden", !!cls);
  E.main.classList.toggle("hidden", !cls);
  if (!cls) {
    B.want = null;
    hideTip();
    return;
  }
  // one combo, until another class is clicked or the board takes one of its cards
  if (B.combo && (comboClass(B.combo) !== cls || blockedBy(ctx.board)(B.combo))) B.combo = null;
  const hand = B.combo || cls;
  const key = [ctx.base, ctx.id, ctx.player, hand, B.sizes ? 1 : 0].join("|");
  const fresh = B.want !== key;
  B.want = key;
  if (fresh) drawHead();
  if (box.dataset.key === key) { E.main.classList.remove("busy"); return; }
  E.main.classList.add("busy");
  if (!B.data) message("Reading the tree…", "");
  B.err = null;
  const ent = await cache.load("lines", key,
    `${ctx.base}/hand_lines?id=${q(ctx.id)}&player=${ctx.player}&hand=${q(hand)}&sizes=${B.sizes ? 1 : 0}`,
    (data) => {
      const dep = depth(data);
      const stats = lineStats(dep ? cellsAt(data, dep) : []);
      return { data, stats, slot: slots(stats, COLORS) };
    });
  if (B.want !== key) return;        // another hand or node was asked for meanwhile: that one draws
  E.main.classList.remove("busy");
  if (!ctx.current()) return;
  if (!ent) {
    if (!B.err) return;              // dropped by a refresh, which asks again
    Object.assign(B, { data: null, stats: [], slot: new Map(), hl: null, pin: null });
    box.dataset.key = key;
    for (const s of [E.legend, E.now, E.key]) s.textContent = "";
    return message("Nothing to show here", String(B.err.message || B.err));
  }
  box.dataset.key = key;
  Object.assign(B, { data: ent.data, stats: ent.stats, slot: ent.slot, hl: null, pin: null });
  const rk = [ctx.base, ctx.id, ctx.player].join("|");
  if (!B.reach.has(rk)) B.reach.set(rk, new Map());
  for (const c of ent.data.combos) B.reach.get(rk).set(displayCombo(c.combo), c.reach);
  drawHead();
  drawNow();
  drawLegend();
  drawKey();
  paint();
}

/** The hand and whose it is; its combos, to click for one alone. */
function drawHead() {
  const E = B.el, ctx = B.ctx, cls = ctx.cls;
  E.head.textContent = "";
  const name = el("div", "cls", cls);
  if (B.combo) { name.textContent = ""; name.append(cardTag(B.combo.slice(0, 2)), cardTag(B.combo.slice(2, 4))); }
  E.head.append(name, el("div", "meta", `${ctx.player}'s lines from the ${STREET[ctx.board.length] || "node"} on`));
  E.combos.textContent = "";
  const all = el("button", "blk-combo rl-all" + (B.combo ? "" : " sel"), "All");
  all.title = `Every combo of ${cls}, weighted by how often each is here`;
  all.onclick = () => { B.combo = null; again(); };
  E.combos.appendChild(all);
  const blocked = blockedBy(ctx.board);
  const reach = B.reach.get([ctx.base, ctx.id, ctx.player].join("|"));
  for (const i of H.byClass[cls] || []) {
    const c = displayCombo(H.order[i]);
    if (blocked(c)) continue;
    const r = reach ? reach.get(c) : null;
    const b = el("button", "blk-combo" + (B.combo === c ? " sel" : "") + (r === 0 ? " out" : ""));
    b.appendChild(comboEl(c, "xs"));
    b.title = (r == null ? c : `${c}: ${pct(r)} in ${ctx.player}'s range here`) + (B.combo === c ? ". Click for the whole class" : ". Click for this combo alone");
    b.onclick = () => { B.combo = B.combo === c ? null : c; again(); };
    E.combos.appendChild(b);
  }
}

/** A note in place of the picture. */
function message(title, text) {
  hideTip();
  const plot = B.el.plot;
  plot.textContent = "";
  plot.style.minHeight = "";
  const m = el("div", "rl-note");
  m.append(el("b", "", title), el("span", "", text));
  plot.appendChild(m);
}

/** The picture alone (also when a line is pointed at in the list, which must stay under the pointer). */
function paint() {
  const d = B.data;
  if (!d) return;
  hideTip();
  B.size = sizeKey();
  B.el.plot.style.minHeight = "";
  if (!d.now) {
    return message(`${d.hand} is not here`, d.combos.length ? `${d.player} never gets to this node with ${d.hand}.` : `The board takes every combo of ${d.hand}.`);
  }
  const dep = depth(d);
  if (dep === 0) return message("No cards to come", `What ${d.hand} does from here is below.`);
  if (!(B.el.plot.clientWidth > 0)) return;   // out of sight: drawn when shown (ensure's observer)
  if (dep === 1) return renderBars(d, d.now.cards, [], null, 0);
  const open = B.mode === "turn" && (d.now.cards.find((t) => t.card === B.turn && t.cards && t.cards.length) || d.now.cards.find((t) => t.cards && t.cards.length));
  if (!open) return renderMap(d);
  B.turn = open.card;
  renderBars(d, open.cards, [open.card], zoomHead(d, open), CAP + GAP + mapDims(d).h);
}

function caption(...parts) {
  const c = el("div", "rl-cap");
  for (const p of parts) c.appendChild(typeof p === "string" ? el("span", "", p) : p);
  return c;
}

/** The caption of a heat map: the line it is of. */
function heatCaption(d, hl, where) {
  const c = el("div", "rl-cap");
  const s = el("span");
  s.append(swatch(hl), el("b", "", words(d.lines[hl])), ` how often ${d.hand} plays it ${where}`);
  c.appendChild(s);
  return c;
}

const nLines = () => (B.n === 1 ? "line" : B.n + " lines");

/** Light up the rows of the list whose lines a box shows (none: put the list back). */
function markRows(lines) {
  for (const [line, row] of B.rows) row.classList.toggle("in", !!lines && lines.includes(line));
}

/* ------------------------------------------------------------------ every runout: the turn x river map */

/** Where the map's boxes go in the room the panel has. Columns as wide as the panel allows (to the
 *  device pixel: there are 49 of them), touching while they are narrow; rows about as tall, never
 *  under 8px (the bands split a box's height) nor more than the panel is tall. */
function mapDims(d) {
  const E = B.el, cards = deck(d.board);
  const dpr = window.devicePixelRatio || 1;
  const LEFT = 15, GROUP = 4;
  const groups = new Set(cards.map((c) => c[1])).size - 1;
  const n = cards.length || 1;
  const col = clamp(Math.floor(((E.plot.clientWidth - LEFT - groups * GROUP) / n) * dpr) / dpr, 3, 22);
  const sep = col >= 9 ? 1 : 0;
  const TOP = sep ? 30 : 16;
  // what is left of the panel under the caption: the whole map in sight without scrolling
  const above = E.plot.getBoundingClientRect().top - E.box.getBoundingClientRect().top + E.box.scrollTop + CAP + GAP;
  const room = Math.floor((E.box.clientHeight - above - 8 - TOP - groups * GROUP) / n);
  // a row is at least tall enough for its bands to be told apart, when the panel is tall enough for 49 of them
  const row = clamp(Math.min(Math.max(Math.round(col), 3 * B.n + 3), room), 8, 15);
  const rsep = row < 11 ? 2 : 1;   // rows that touch read as one striped field
  const X = axis(cards, col - sep, GROUP, sep), Y = axis(cards, row - rsep, GROUP, rsep);
  return { cards, LEFT, TOP, sep, cw: col - sep, ch: row - rsep, X, Y, w: LEFT + X.end, h: TOP + Y.end };
}

function renderMap(d) {
  const plot = B.el.plot;
  plot.textContent = "";
  const hl = B.hl != null ? B.hl : B.pin;
  const pal = palette();
  const { cards, LEFT, TOP, sep, cw, ch, X, Y, w, h } = mapDims(d);
  const index = new Map(cards.map((c, i) => [c, i]));
  plot.appendChild(hl != null ? heatCaption(d, hl, "on each runout: the fuller the colour, the more often.")
    : caption(el("b", "", "Every runout"), ` Turn cards down, river cards across. Each box holds the ${nLines()} ${d.hand} plays most often there.`));

  const wrap = el("div", "rl-mapw");
  const c = el("canvas"), rowCur = el("i", "rl-rowcur hidden"), cur = el("i", "rl-cur hidden");
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  for (const e of [c, wrap]) { e.style.width = w + "px"; e.style.height = h + "px"; }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  // the two sides: a rank per row (and per column, when there is room), in its suit's colour
  const sans = getComputedStyle(document.body).fontFamily;
  const ink = (s) => (s === "s" ? pal.spade : pal.suit[s]);   // the spades' own grey is for chips
  ctx.textBaseline = "middle";
  cards.forEach((card, i) => {
    ctx.fillStyle = ink(card[1]);
    ctx.font = `700 ${clamp(ch + 1, 8, 10)}px ${sans}`;
    ctx.textAlign = "right";
    ctx.fillText(card[0], LEFT - 5, TOP + Y.at[i] + ch / 2 + 0.5);
    if (sep) {
      ctx.font = `700 ${clamp(cw, 8, 10)}px ${sans}`;
      ctx.textAlign = "center";
      ctx.fillText(card[0], LEFT + X.at[i] + cw / 2, TOP - 7);
    }
  });
  ctx.font = `700 11px ${sans}`;
  ctx.textAlign = "center";
  for (const s of SUITS) {
    const of = cards.map((card, i) => (card[1] === s ? i : -1)).filter((i) => i >= 0);
    if (!of.length) continue;
    ctx.fillStyle = ink(s);
    ctx.fillText(GLYPH[s], LEFT + (X.at[of[0]] + X.at[of[of.length - 1]] + cw) / 2, sep ? TOP - 21 : TOP - 8);
  }

  // the boxes: the most common line at the bottom, the next ones on top of it
  const label = hl != null ? d.lines[hl] : null;
  let max = 0;
  if (label != null) for (const { cell } of cellsAt(d, 2)) max = Math.max(max, shareOf(d.lines, cell, label));
  const unit = Math.max(1, Math.round(dpr)), gap = ch >= 11 ? 1 : 0;
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
      const hs = split((ch - gap * (bs.length - 1)) * unit, bs.map((b) => b.share));
      let yy = y + ch;
      bs.forEach((b, k) => {
        const hh = hs[k] / unit;
        if (!(hh > 0)) return;
        yy -= hh;
        ctx.fillStyle = colorOf(b.line, pal);
        ctx.fillRect(x, yy, cw, hh);
        yy -= gap;
      });
    }
  }

  const turns = new Map(d.now.cards.map((t) => [t.card, t]));
  const at = (ev) => {
    const b = c.getBoundingClientRect();
    const i = X.index(ev.clientX - b.left - LEFT), j = Y.index(ev.clientY - b.top - TOP);
    const t = j >= 0 ? turns.get(cards[j]) : null;
    return { i, j, turn: t && t.cards && t.cards.length ? cards[j] : null, river: cards[i], cell: i >= 0 && j >= 0 ? cellOf.get(cards[j] + cards[i]) : null };
  };
  const off = () => { for (const e of [cur, rowCur]) e.classList.add("hidden"); hideTip(); markRows(null); c.style.cursor = ""; };
  c.onmousemove = (ev) => {
    const hit = at(ev);
    if (!hit.turn) return off();
    c.style.cursor = "pointer";
    rowCur.classList.remove("hidden");
    Object.assign(rowCur.style, { left: LEFT - 2 + "px", top: TOP + Y.at[hit.j] - 2 + "px", width: X.end + 4 + "px", height: ch + 4 + "px" });
    cur.classList.toggle("hidden", !hit.cell);
    if (!hit.cell) {
      markRows(null);
      return showTip(ev, `<div class="t">Turn ${cardHtml(hit.turn)}</div><div class="rl-tip-sub">Click to enlarge this turn</div>`);
    }
    Object.assign(cur.style, { left: LEFT + X.at[hit.i] - 2 + "px", top: TOP + Y.at[hit.j] - 2 + "px", width: cw + 4 + "px", height: ch + 4 + "px" });
    markRows(label != null ? null : bands(hit.cell, B.n).map((b) => b.line));
    showTip(ev, tipHtml(d, [hit.turn, hit.river], hit.cell, "Click to enlarge this turn"));
  };
  c.onmouseleave = off;
  c.onclick = (ev) => {
    const hit = at(ev);
    if (!hit.turn) return;
    B.turn = hit.turn;
    B.mode = "turn";
    paint();
  };
  wrap.append(c, rowCur, cur);
  plot.appendChild(wrap);
}

/* ------------------------------------------------------------------ one card at a time: a bar per river card */

/** The bar over an enlarged turn: back to every runout, or on to the next turn. */
function zoomHead(d, open) {
  const head = el("div", "rl-zoom");
  const back = el("button", "rl-back", "‹ Every runout");
  back.onclick = () => { B.mode = "all"; paint(); };
  const turns = d.now.cards.filter((t) => t.cards && t.cards.length);
  const k = turns.indexOf(open);
  const step = (by, text, title) => {
    const b = el("button", "rl-back rl-step", text);
    b.title = title;
    b.onclick = () => { B.turn = turns[(k + by + turns.length) % turns.length].card; paint(); };
    return b;
  };
  head.append(back, el("span", "sp"), el("span", "lbl", "Turn"), step(-1, "‹", "The turn card before"), cardTag(open.card), step(1, "›", "The next turn card"));
  return head;
}

/** cells: the river cards of the node itself or of the enlarged turn; before: the cards dealt on the
 *  way ([] or [turn]). A column per rank, a row per suit; `fill` is the height to take (the map's, so
 *  that nothing under the picture moves), or 0. */
function renderBars(d, cells, before, head, fill) {
  const plot = B.el.plot;
  plot.textContent = "";
  const hl = B.hl != null ? B.hl : B.pin;
  const next = STREET[d.board.length + before.length + 1];
  if (head) plot.appendChild(head);
  const label = hl != null ? d.lines[hl] : null;
  if (label != null) plot.appendChild(heatCaption(d, hl, `on each ${next} card.`));
  else {
    plot.appendChild(caption(el("b", "", before.length ? `Every river on the ${before[0][0]}${GLYPH[before[0][1]]} turn` : `Every ${next} card`),
      ` Each bar holds the ${nLines()} ${d.hand} plays most often on that card.`));
  }
  const ROWS = SUITS.length, ROW_GAP = 6;
  const used = (head ? HEAD + GAP : 0) + CAP + GAP + (ROWS - 1) * ROW_GAP + ROWS * (CARD_H + 2);
  const bar = fill ? clamp(Math.floor((fill - used) / ROWS), 30, 140) : 56;
  const g = el("div", "rl-grid");
  g.style.rowGap = ROW_GAP + "px";
  const by = new Map(cells.map((c) => [c.card, c]));
  for (const s of SUITS) {
    for (const r of RANKS) {
      const card = r + s, cell = by.get(card);
      const b = el(cell ? "button" : "div", "rl-card" + (cell ? "" : " off"));
      b.appendChild(el("span", `rl-card-h s-${s}`, r));
      const stack = el("span", "rl-bar");
      stack.style.height = bar + "px";
      b.appendChild(stack);
      g.appendChild(b);
      if (!cell) continue;
      const seg = (line, f) => {
        const i = el("i");
        i.style.flexBasis = (100 * f).toFixed(2) + "%";
        i.style.background = cssColor(line);
        stack.appendChild(i);
      };
      let shown = null;
      if (label != null) {
        const f = shareOf(d.lines, cell, label);
        if (f > 0) seg(hl, f);
      } else {
        const bs = bands(cell, B.n);
        for (const x of bs) seg(x.line, x.f);
        shown = bs.map((x) => x.line);
      }
      b.onmouseenter = () => markRows(shown);
      b.onmousemove = (ev) => showTip(ev, tipHtml(d, before.concat(card), cell, "Click to go to it in the tree"));
      b.onmouseleave = () => { hideTip(); markRows(null); };
      b.onclick = () => { if (cell.go) { hideTip(); B.ctx.goto(d.id + ":" + cell.go); } };
    }
  }
  plot.appendChild(g);
  if (fill) plot.style.minHeight = fill + "px";
}

/* ------------------------------------------------------------------ tooltip, the lists */

function tipHtml(d, cards, cell, click) {
  const names = d.board.length === 3 ? ["Turn", "River"] : ["River"];
  let h = `<div class="t">${cards.map((c, i) => `${names[i]} ${cardHtml(c)}`).join(" &nbsp; ")}</div>`;
  let sum = 0;
  cell.lines.slice(0, Math.max(B.n, 5)).forEach(([i, f], k) => {
    sum += f;
    h += `<div class="r${k < B.n ? "" : " rl-tip-more"}"><span>${swatchHtml(i)}${words(d.lines[i])}</span><b>${pct(f)}</b></div>`;
  });
  if (sum < 0.995) h += `<div class="r rl-tip-other"><span>other lines</span><b>${pct(1 - sum)}</b></div>`;
  if (cell.reach < 0.995) h += `<div class="rl-tip-sub">${d.hand} gets to this ${names[cards.length - 1].toLowerCase()} ${pct(cell.reach)} of the time</div>`;
  h += `<div class="rl-tip-sub">${click}</div>`;
  return h;
}

function lineRow(line, label, ...right) {
  const row = el("div", "rl-row");
  if (line != null) row.appendChild(swatch(line));
  const text = el("span", "t", words(label));
  text.title = words(label);   // a split-size line can be longer than the column
  row.appendChild(text);
  right.forEach((r, k) => row.appendChild(el("span", k ? "m" : "p", r)));
  return row;
}

/** The lines over the runouts, heaviest first: the key to the picture's colours. Pointing at one
 *  turns the picture into a map of that line. */
function drawLegend() {
  const sec = B.el.legend, d = B.data;
  sec.textContent = "";
  B.rows = new Map();
  if (!d || !d.now) return;
  const dep = depth(d);
  if (!dep) return;
  const cells = cellsAt(d, dep), what = dep === 2 ? "runouts" : "river cards";
  sec.appendChild(el("h3", "", "Lines"));
  sec.appendChild(el("div", "hint", `Over the ${cells.length} ${what}. Point at a line to see where it is played; click to keep it.`));
  const head = el("div", "rl-row head");
  head.append(el("span", "t", "line"), el("span", "p", "top on"), el("span", "m", "avg"));
  head.title = `top on: the share of the ${what} where it is the most common line. avg: how often it is played on an average one.`;
  const list = el("div", "rl-list");
  for (const s of B.stats.slice(0, LISTED)) {
    const row = lineRow(s.line, d.lines[s.line], !s.lead ? "–" : s.lead / cells.length < 0.005 ? "<1%" : pct(s.lead / cells.length), pct(s.mean));
    row.title = `The most common line on ${s.lead} of the ${cells.length} ${what}. Played ${pct(s.mean, 1)} of the time on an average one, ${pct(s.max)} at most.`;
    row.classList.add("pick");
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
  if (B.stats.length > LISTED) {
    const more = el("div", "rl-row rest");
    more.append(el("i", "rl-sw other"), el("span", "t", `${B.stats.length - LISTED} rarer lines`));
    sec.appendChild(more);
  }
}

/** What the hand does on this street. */
function drawNow() {
  const sec = B.el.now, d = B.data;
  sec.textContent = "";
  if (!d || !d.now) return;
  sec.appendChild(el("h3", "", `${d.hand} on the ${STREET[d.board.length]}`));
  for (const [i, f] of d.now.lines.slice(0, 6)) {
    const row = lineRow(null, d.lines[i], pct(f));
    row.style.setProperty("--f", String(f));
    row.classList.add("meter");
    sec.appendChild(row);
  }
}

/** How to read it. */
function drawKey() {
  const sec = B.el.key, d = B.data;
  sec.textContent = "";
  if (!d || !d.now) return;
  const dep = depth(d), street = STREET[d.board.length];
  sec.appendChild(el("h3", "", "Reading it"));
  sec.appendChild(el("div", "hint", (dep === 0 ? `A line is what ${d.hand} itself does from here. `
    : `A line is what ${d.hand} itself does, street by street: ` + (dep === 2 ? `bet · bet · check is a bet on the ${street}, a bet on the turn and a check on the river. `
      : `bet · check is a bet on the ${street}, then a check on the river. `))
    + `Two letters on one street are two actions there: xc check then call, xf check then fold, xr check-raise, bc bet then call, bf bet then fold. `
    + `A dash is a street with nothing left to do.`));
  if (dep > 0) {
    sec.appendChild(el("div", "hint", `The percentages are of the times ${d.hand} gets to that card: the pot has not ended before it.`
      + (B.stats.length > COLORS ? ` The ${COLORS} lines that weigh most in the picture have a colour of their own; the others are grey.` : "")));
  }
}
