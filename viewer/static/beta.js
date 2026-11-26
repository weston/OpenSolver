// Study side panel, Beta tab: features still finding their shape. The first one, Runout lines: how
// the hand pinned in the matrix plays the rest of the pot on every runout, from the node being
// viewed (server side: viewer/hand_lines.py).
//
// A line is the hand's own actions street by street ("bet · bet · check"). From a flop node the
// picture is a map of every runout: a row per turn card, a column per river card, 52 by 52 (a runout
// that cannot be, a card of the board among them, keeps its box, empty), in 16 outlined blocks, the turns of
// one suit against the rivers of one suit. A box is the line the hand plays most often on that
// runout, in that line's colour (how often is in the tooltip); or, asked for more lines than one, the
// few most common as bands of colour, taller the more often. The lines that matter most over the whole
// map get a colour of their own (the list under the map is the key); the rest share a grey. From a
// turn node, or with one turn card enlarged, it is a bar per river card. A flop node can also be shown
// by its turn cards alone: a bar per turn card, the lines as far as the turn (the rivers averaged).
//
// The second view, Money in, is the same map with a column in each box: the taller, the more chips the
// hand puts into the pot on that runout, on average, from the node to the end of the pot. The chips
// are told apart by the action that put them in: the ones bet or raised at the bottom, the ones called
// on top of them, in two colours, a full box being the most the hand puts in over the runouts (one
// scale: the two compare). Either can be shown alone, from the bottom of the box, on its own scale.
// A report that does not tell them apart (an older solver) is one colour, fuller the more.
//
// Nothing moves under the pointer: what changes the picture's height is over it, and the picture is
// rebuilt at the height it had. The panel scrolls as one piece: nothing in it stays behind.
//
// The hand is never typed: it is the class clicked in the matrix, or one of its combos clicked here.

import { get, q, prefs, showTip, hideTip, fmt } from "./api.js";
import { H, RANKS, SUITS, GLYPH, el, comboEl, displayCombo, comboClass, blockedBy } from "./cards.js";
import { sideCache } from "./sidecache.js";
import { words, depth, cellsAt, bands, lineStats, slots, shareOf, split, deck, axis, putRange, chipsOf, stack, strength } from "./runouts.js";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pct = (f, digits = 0) => fmt(100 * f, digits) + "%";
const STREET = { 3: "flop", 4: "turn", 5: "river" };
const COLORS = 8;   // lines with a colour of their own (--line-1 .. --line-8)
const MAX_N = 5;
const LISTED = 12;  // lines in the list; the rest are counted
const FRAME = 1;     // the outline of a block of the map, px
const ONE_ROW = 680; // the panel width that holds every control in one row (the widest: Money in, with its three parts and what a full box is)
const CAP = 32, GAP = 8, HEAD = 26, CARD_H = 13;   // the picture's fixed heights (style.css: .rl-cap, .rl-plot, .rl-zoom, .rl-card-h)
const DETAIL = 3;   // lines where there is room for them (a bar, the tooltip), however few a box shows
const VIEWS = ["lines", "money"];
// The chips of the Money in view, by the action that put them in. color: the variable of the part's
// colour; name, does, by: the part in words; most, least: the rows of the key.
const PARTS = {
  all: { color: "--money", name: "Money in", does: "puts into the pot", by: "", most: "Most on", least: "Least on" },
  bet: { color: "--money-bet", name: "Bets and raises", does: "bets and raises", by: " by betting and raising", most: "Most bet on", least: "Least bet on" },
  call: { color: "--money-call", name: "Calls", does: "calls", by: " by calling", most: "Most called on", least: "Least called on" },
};

const B = {
  view: VIEWS.includes(prefs.get("beta-view", "lines")) ? prefs.get("beta-view", "lines") : "lines",
  n: clamp(+prefs.get("beta-lines", 1) || 1, 1, MAX_N), // lines a box of the map shows: 1, the most common, shaded by how often
  sizes: !!prefs.get("beta-sizes", false),               // each bet size its own line
  part: ["bet", "call"].includes(prefs.get("beta-money", "all")) ? prefs.get("beta-money", "all") : "all",   // the chips Money in shows
  combo: null,                  // one combo of the pinned class, when narrowed to it
  cards: prefs.get("beta-cards", "runouts") === "turns" ? "turns" : "runouts",   // from a flop node: the picture is of every runout, or of the turn cards alone
  mode: "all",                  // from a flop node: that picture ("all"), or one turn enlarged ("turn")
  turn: null,                   // the enlarged turn card
  hl: null, pin: null,          // the line the picture is a heat map of (hovered / clicked in the list)
  ctx: null,                    // what Study shows (renderRunouts)
  want: null,                   // the key of the report the panel is to show
  err: null,                    // why the last ask failed
  data: null,                   // the report drawn
  by: {},                       // level of cards (1: the next card, 2: the two next) -> {stats, slot, put, parts} over its cells
  stats: [], slot: new Map(),   // of the level the picture is of: lineStats, and the lines with a colour (line -> 0..7)
  put: null,                    // putRange over its cells: the least and the most the hand puts in
  parts: {},                    // the same of the chips bet or raised, and of the chips called: {bet, call}
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
  if (B.el) B.el.box.dataset.key = "";
}

/* ------------------------------------------------------------------ colours */

function palette() {
  const cs = getComputedStyle(document.documentElement);
  const g = (v) => cs.getPropertyValue(v).trim();
  const line = [];
  for (let i = 1; i <= COLORS; i++) line.push(g("--line-" + i));
  return { line, other: g("--line-other"), frame: g("--rl-frame"), money: g("--money"), bet: g("--money-bet"), call: g("--money-call"), none: g("--cell-empty"), blank: g("--rl-blank"), dim: g("--text-3"), text: g("--text"), spade: g("--text-2"),
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
const dotHtml = (part) => `<i class="rl-dot" style="background:var(${PARTS[part].color})"></i>`;
function dot(part) {
  const i = el("i", "rl-dot");
  i.style.background = `var(${PARTS[part].color})`;
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
  // over the picture, one row of a fixed height: what sets the picture's own height (the lines in a
  // box), so that it stays under the pointer when clicked; in its place the Money in view's scale
  const top = el("div", "rl-top"), scale = el("div", "rl-scale");
  // with it, what the picture is: of the lines or of the chips; from a flop node, of every runout or
  // of the turn cards alone. Beside the row in a wide panel, over it in a narrow one (fit())
  const pick = el("div", "rl-pick"), ctl = el("div", "rl-ctl");
  const views = el("div", "seg small rl-views");
  for (const [v, text, tip] of [["lines", "Lines", "The line the hand takes on each runout"], ["money", "Money in", "How much the hand puts into the pot on each runout"]]) {
    const b = el("button", "", text);
    b.dataset.v = v;
    b.title = tip;
    b.onclick = () => {
      if (B.view === v) return;
      B.view = v;
      prefs.set("beta-view", v);
      B.hl = B.pin = null;
      syncOpts();
      drawNow();
      drawLegend();
      drawKey();
      paint();
    };
    views.appendChild(b);
  }
  const plot = el("div", "rl-plot"), legend = el("section", "rl-sec"), now = el("section", "rl-sec"), key = el("section", "rl-sec");

  const seg = (into, label, title, items, pick) => {
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
    into.appendChild(o);
    return s;
  };
  const nSeg = seg(top, "Lines", "How many lines a box of the map shows. 1: the most common, in its colour", [1, 2, 3, 4, 5].map((n) => [String(n), String(n)]), (v) => {
    B.n = +v;
    prefs.set("beta-lines", B.n);
    syncOpts();
    drawKey();
    paint();
  });
  const pSeg = seg(top, "", "The chips the picture shows, by the action that put them in: both parts together, or one alone on its own scale",
    [["all", "Both", "Every chip the hand puts in: its bets and raises, and its calls on top of them"], ["bet", "Bets & raises", "The chips it bets or raises (a raise whole: the part that matches the bet it answers too)"],
     ["call", "Calls", "The chips it calls with"]], setPart);
  pSeg.parentNode.classList.add("rl-part");
  const cSeg = seg(pick, "", "From a flop node: what the picture is of",
    [["runouts", "Runouts", "Every runout: a box per turn and river card"], ["turns", "Turn only", "A bar per turn card: what the hand does as far as the turn, whatever the river"]], setCards);
  cSeg.parentNode.classList.add("rl-lvl");
  const sSeg = seg(opts, "Bet sizes", "", [["merged", "Merged", "A bet is a bet, whatever its size"], ["split", "Split", "Each bet size is its own line"]], (v) => {
    B.sizes = v === "split";
    prefs.set("beta-sizes", B.sizes);
    syncOpts();
    again();
  });

  top.appendChild(scale);
  pick.prepend(views);
  ctl.append(pick, top);
  main.append(head, combos, ctl, plot, opts, legend, now, key);
  box.append(empty, main);
  B.el = { box, empty, main, head, combos, ctl, pick, top, scale, opts, plot, legend, now, key, nSeg, sSeg, pSeg, cSeg, views };
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

/** One row of controls where the panel is wide enough for all of them in either view, two where it
 *  is not: by the panel's width alone, so that the picture is at the same place whatever is chosen. */
function fit() {
  const w = B.el.plot.clientWidth;
  if (w > 0) B.el.ctl.classList.toggle("row", w >= ONE_ROW);
}

function syncOpts() {
  const act = (s, v) => s.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.v === String(v)));
  act(B.el.nSeg, B.n);
  act(B.el.sSeg, B.sizes ? "split" : "merged");
  act(B.el.views, B.view);
  act(B.el.pSeg, part());
  act(B.el.cSeg, B.cards);
  // the turn cards alone are a choice where there are rivers under them
  B.el.pick.classList.toggle("flat", !B.data || depth(B.data) < 2);
  // no choice of chips with a report that does not tell the calls apart, nor with no picture to show them on
  B.el.top.classList.toggle("whole", !told() || !!(B.data && !depth(B.data)));
  B.el.top.classList.toggle("money", B.view !== "lines");  // the scale in place of the lines in a box
  B.el.opts.classList.toggle("off", B.view !== "lines");   // they are about lines
}

/** Whether the report tells the chips called from the ones bet (not one of a solver from before it
 *  did). Until a report is in, it is taken to. */
const told = () => !B.data || !B.data.now || B.data.now.call != null;
/** The chips the Money in view shows: "all", "bet" or "call". */
const part = () => (told() ? B.part : "all");

/** What a full box or bar is, in chips: the most the hand puts in over the runouts. Both parts together
 *  share that scale (they compare); one alone fills the box at its own most. */
function full() {
  const r = part() === "all" ? B.put : B.parts[part()];
  return r ? r.max.put : 0;
}

/** The level of cards the picture is of: 2, the runouts (a turn and a river card); 1, the next card
 *  alone (a turn node's rivers; a flop node's turn cards, asked for alone and none of them enlarged). */
function level(d = B.data) {
  const dep = d ? depth(d) : 0;
  return dep === 2 && B.cards === "turns" && B.mode !== "turn" ? 1 : dep;
}
/** Whether the picture is the map (boxes), not bars. */
const mapped = () => level() === 2 && B.mode !== "turn";
const mark = () => (mapped() ? "box" : "bar");

/** Take the lines, their colours and the chips of the level the picture is of. */
function relevel() {
  const lv = B.by[level()] || { stats: [], slot: new Map(), put: null, parts: {} };
  Object.assign(B, { stats: lv.stats, slot: lv.slot, put: lv.put, parts: lv.parts });
}

/** Everything again, for another level of cards: its lines are other lines. */
function levelled() {
  B.hl = B.pin = null;
  relevel();
  syncOpts();
  drawLegend();
  drawKey();
  paint();
}

function setCards(v) {
  if (B.cards === v && B.mode === "all") return;
  B.cards = v;
  B.mode = "all";
  prefs.set("beta-cards", v);
  levelled();
}

/** Enlarge a turn card, or back to the picture it was clicked in. With every runout shown the lines
 *  stay the same ones (the rivers'): the picture alone is redrawn. */
function zoom(turn) {
  B.mode = turn ? "turn" : "all";
  if (turn) B.turn = turn;
  if (B.cards === "turns") levelled();
  else paint();
}

function setPart(v) {
  if (B.part === v) return;
  B.part = v;
  prefs.set("beta-money", v);
  syncOpts();
  drawLegend();
  paint();
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
      const by = {};
      for (let lv = 1; lv <= depth(data); lv++) {
        const cells = cellsAt(data, lv), stats = lineStats(cells);
        by[lv] = { stats, slot: slots(stats, COLORS), put: putRange(cells), parts: { bet: putRange(cells, "bet"), call: putRange(cells, "call") } };
      }
      return { data, by };
    });
  if (B.want !== key) return;        // another hand or node was asked for meanwhile: that one draws
  E.main.classList.remove("busy");
  if (!ctx.current()) return;
  if (!ent) {
    if (!B.err) return;              // dropped by a refresh, which asks again
    Object.assign(B, { data: null, by: {}, hl: null, pin: null });
    relevel();
    box.dataset.key = key;
    syncOpts();
    for (const s of [E.scale, E.legend, E.now, E.key]) s.textContent = "";
    return message("Nothing to show here", String(B.err.message || B.err));
  }
  box.dataset.key = key;
  Object.assign(B, { data: ent.data, by: ent.by, hl: null, pin: null });
  relevel();
  syncOpts();
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
  E.head.append(name, el("div", "meta", `${ctx.player}, from the ${STREET[ctx.board.length] || "node"} on`));
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

/** The picture alone (also when a line is pointed at in the list, which must stay under the pointer).
 *  It is built anew: meanwhile the panel must not get shorter, or one scrolled down to the list
 *  jumps back up. */
function paint() {
  const d = B.data;
  if (!d) return;
  hideTip();
  B.size = sizeKey();
  fit();
  drawScale();
  const { plot, box } = B.el;
  const at = box.scrollTop;
  plot.style.minHeight = plot.offsetHeight + "px";
  const fill = draw(d);
  plot.style.minHeight = fill ? fill + "px" : "";
  if (box.scrollTop !== at) box.scrollTop = at;
}

/** Draws the picture; returns the height it is to take (the map's, for one turn card enlarged), or nothing. */
function draw(d) {
  if (!d.now) {
    return message(`${d.hand} is not here`, d.combos.length ? `${d.player} never gets to this node with ${d.hand}.` : `The board takes every combo of ${d.hand}.`);
  }
  const dep = depth(d);
  if (dep === 0) {
    return d.cut && d.board.length < 5
      ? message(`No ${STREET[d.board.length + 1]} in this tree`, `It was saved without it. What ${d.hand} does on the ${STREET[d.board.length]} is below.`)
      : d.board.length < 5 ? message(`Nothing after the ${STREET[d.board.length]} for ${d.hand}`, `Its pot always ends here: a fold, or all the chips in. What it does is below.`)
      : message("No cards to come", `What ${d.hand} does from here is below.`);
  }
  if (!(B.el.plot.clientWidth > 0)) return null;   // out of sight: drawn when shown (ensure's observer)
  if (dep === 1) return renderBars(d, d.now.cards, [], null, 0);
  const open = B.mode === "turn" && (d.now.cards.find((t) => t.card === B.turn && t.cards && t.cards.length) || d.now.cards.find((t) => t.cards && t.cards.length));
  const fill = CAP + GAP + mapDims(d).h;   // the map's height, whatever is shown: nothing under the picture moves
  if (open) {
    B.turn = open.card;
    return renderBars(d, open.cards, [open.card], zoomHead(d, open), fill);
  }
  return B.cards === "turns" ? renderBars(d, d.now.cards, [], null, fill) : renderMap(d);
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

const money = () => B.view === "money";
/** The line the picture is a heat map of, if any (the Lines view's list). */
const heat = () => (money() ? null : B.hl != null ? B.hl : B.pin);
/** Chips as a share of the pot at the node (the unit money in is told in); chips, when it is empty. */
const ofPot = (d, v, digits = 0) => (d.pot > 0 ? pct(v / d.pot, digits) : fmt(v, 1));
const chips = (v) => fmt(v, v < 100 ? 1 : 0);

/** Light up the rows of the list whose lines a box shows (none: put the list back). */
function markRows(lines) {
  for (const [line, row] of B.rows) row.classList.toggle("in", !!lines && lines.includes(line));
}

/* ------------------------------------------------------------------ every runout: the turn x river map */

/** Where the map's boxes go in the room the panel has: 52 by 52, the cards that cannot come too.
 *  Columns as wide as the panel allows (to the device pixel); rows about as tall, never under 8px
 *  (a rank is written beside each; bands split a box's height) nor more than the panel is tall. */
function mapDims(d) {
  const E = B.el, cards = deck();
  const dpr = window.devicePixelRatio || 1;
  // a suit's turns by a suit's rivers are a block of 13 by 13, with an outline of its own: EDGE from
  // the boxes to the outside of it (the line and the room inside it), BETWEEN two outlines
  const EDGE = FRAME + 2, BETWEEN = 3, GROUP = 2 * EDGE + BETWEEN;
  const LEFT = 14 + EDGE;
  const groups = SUITS.length - 1;
  const n = cards.length;
  const col = clamp(Math.floor(((E.plot.clientWidth - LEFT - EDGE - groups * GROUP) / n) * dpr) / dpr, 3, 22);
  const wide = col >= 9;             // room for a rank over each column
  const striped = !money() && B.n > 1;
  // a box of one colour is set off by a hairline all round; bands touch their neighbours' sides
  const sep = wide ? 1 : striped ? 0 : 1 / dpr;
  const TOP = (wide ? 30 : 16) + EDGE;
  // what is left of the panel under the caption: the whole map in sight without scrolling
  // (measured from the row over the picture, whatever the panel's scroll)
  const lead = parseFloat(getComputedStyle(E.main).rowGap) || 0;
  const above = E.top.getBoundingClientRect().bottom - E.box.getBoundingClientRect().top + E.box.scrollTop + lead + CAP + GAP;
  const room = Math.floor((E.box.clientHeight - above - 8 - TOP - EDGE - groups * GROUP) / n);
  // a row is at least tall enough for its bands to be told apart, when the panel is tall enough for 52 of them
  const row = clamp(Math.min(Math.max(Math.round(col), striped ? 3 * B.n + 3 : 0), room), 8, 15);
  const rsep = !striped ? sep : row < 11 ? 2 : 1;   // rows of bands that touch read as one striped field
  const X = axis(cards, col - sep, GROUP, sep), Y = axis(cards, row - rsep, GROUP, rsep);
  return { cards, LEFT, TOP, EDGE, wide, cw: col - sep, ch: row - rsep, X, Y, w: LEFT + X.end + EDGE, h: TOP + Y.end + EDGE };
}

function renderMap(d) {
  const plot = B.el.plot;
  plot.textContent = "";
  const hl = heat();
  const pal = palette();
  const { cards, LEFT, TOP, EDGE, wide, cw, ch, X, Y, w, h } = mapDims(d);
  const index = new Map(cards.map((c, i) => [c, i]));
  plot.appendChild(hl != null ? heatCaption(d, hl, "on each runout: the fuller the colour, the more often.")
    : money() && told() && part() === "all" ? caption(el("b", "", "Money in"), ` Turn cards down, river cards across. A box fills with what ${d.hand} puts in there: `, dot("bet"), "bets and raises, ", dot("call"), "calls.")
    : money() ? caption(el("b", "", PARTS[part()].name), ` Turn cards down, river cards across. The fuller a box, the more ${d.hand} ${PARTS[part()].does} on that runout.`)
    : B.n === 1 ? caption(el("b", "", "Every runout"), ` Turn cards down, river cards across. A box is the colour of ${d.hand}'s most common line there.`)
    : caption(el("b", "", "Every runout"), ` Turn cards down, river cards across. Each box holds the ${B.n} lines ${d.hand} plays most often there.`));

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
  // a card that cannot come (on the board, or in the one combo asked for) keeps its row and its column, empty
  const turns = new Map(d.now.cards.map((t) => [t.card, t]));
  cards.forEach((card, i) => {
    ctx.fillStyle = ink(card[1]);
    ctx.globalAlpha = turns.has(card) ? 1 : 0.3;
    ctx.font = `700 ${clamp(ch + 1, 8, 10)}px ${sans}`;
    ctx.textAlign = "right";
    ctx.fillText(card[0], LEFT - EDGE - 4, TOP + Y.at[i] + ch / 2 + 0.5);
    if (wide) {
      ctx.font = `700 ${clamp(cw, 8, 10)}px ${sans}`;
      ctx.textAlign = "center";
      ctx.fillText(card[0], LEFT + X.at[i] + cw / 2, TOP - EDGE - 7);
    }
  });
  ctx.globalAlpha = 1;
  ctx.font = `700 11px ${sans}`;
  ctx.textAlign = "center";
  for (const s of SUITS) {
    const of = cards.map((card, i) => (card[1] === s ? i : -1)).filter((i) => i >= 0);
    if (!of.length) continue;
    ctx.fillStyle = ink(s);
    ctx.fillText(GLYPH[s], LEFT + (X.at[of[0]] + X.at[of[of.length - 1]] + cw) / 2, TOP - EDGE - (wide ? 21 : 8));
  }
  // the 16 blocks, each in its outline: the turns of one suit by the rivers of one suit
  ctx.fillStyle = pal.frame;
  for (const [y0, y1] of Y.spans) {
    for (const [x0, x1] of X.spans) {
      const x = LEFT + x0 - EDGE, y = TOP + y0 - EDGE, bw = x1 - x0 + 2 * EDGE, bh = y1 - y0 + 2 * EDGE;
      for (const [a, b, c2, d2] of [[x, y, bw, FRAME], [x, y + bh - FRAME, bw, FRAME], [x, y, FRAME, bh], [x + bw - FRAME, y, FRAME, bh]]) ctx.fillRect(a, b, c2, d2);
    }
  }

  // the boxes. One colour: the chips put in or the line pointed at, the fuller the more; or the most
  // common line, in full. Bands: the most common line at the bottom, the next ones on top of it.
  const label = hl != null ? d.lines[hl] : null;
  let max = 0;
  if (label != null) for (const { cell } of cellsAt(d, 2)) max = Math.max(max, shareOf(d.lines, cell, label));
  const tint = (x, y, color, a) => {
    if (!(a > 0)) return;
    ctx.globalAlpha = a;
    ctx.fillStyle = color;
    ctx.fillRect(x, y, cw, ch);
    ctx.globalAlpha = 1;
  };
  const unit = Math.max(1, Math.round(dpr)), gap = ch >= 11 ? 1 : 0;
  const top = full();   // Money in: a full box
  const cellOf = new Map();   // "turn river" -> the river's cell
  // every box of the 52 by 52 is there: empty where the runout cannot be (the same card twice, a
  // card of the board, a card of the one combo asked for), then the others over them
  ctx.fillStyle = pal.blank;
  for (const y of Y.at) for (const x of X.at) ctx.fillRect(LEFT + x, TOP + y, cw, ch);
  for (const t of d.now.cards) {
    const j = index.get(t.card);
    for (const r of t.cards || []) {
      const i = index.get(r.card);
      if (i == null || j == null) continue;
      cellOf.set(t.card + r.card, r);
      const x = LEFT + X.at[i], y = TOP + Y.at[j];
      ctx.fillStyle = pal.none;
      ctx.fillRect(x, y, cw, ch);
      if (money() && told()) {       // a column: the bets and raises, the calls on top of them; or one of the two
        const ks = part() === "all" ? ["bet", "call"] : [part()];
        let yy = y + ch;
        stack(ch * unit, ks.map((k) => strength(chipsOf(r, k), top))).forEach((px, k) => {
          if (!px) return;
          yy -= px / unit;
          ctx.fillStyle = pal[ks[k]];
          ctx.fillRect(x, yy, cw, px / unit);
        });
        continue;
      }
      if (money()) { tint(x, y, pal.money, strength(r.put, top, 0.06)); continue; }
      if (label != null) { tint(x, y, colorOf(hl, pal), strength(shareOf(d.lines, r, label), max, 0.08)); continue; }
      if (B.n === 1) {
        if (r.lines.length) tint(x, y, colorOf(r.lines[0][0], pal), 1);
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
    markRows(label != null || money() ? null : bands(hit.cell, B.n).map((b) => b.line));
    showTip(ev, tipHtml(d, [hit.turn, hit.river], hit.cell, "Click to enlarge this turn"));
  };
  c.onmouseleave = off;
  c.onclick = (ev) => {
    const hit = at(ev);
    if (!hit.turn) return;
    zoom(hit.turn);
  };
  wrap.append(c, rowCur, cur);
  plot.appendChild(wrap);
  return 0;
}

/* ------------------------------------------------------------------ one card at a time: a bar per river card */

/** The bar over an enlarged turn: back to every runout, or on to the next turn. */
function zoomHead(d, open) {
  const head = el("div", "rl-zoom");
  const back = el("button", "rl-back", B.cards === "turns" ? "‹ Every turn" : "‹ Every runout");
  back.onclick = () => zoom(null);
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

/** The caption of the bars that hold both parts of the chips: which colour is which. */
function barKey(d, top, deep) {
  const s = el("span");
  // (what a full bar is, is over the picture; said here too where there is room for it)
  s.append(` What ${d.hand} puts in ${deep ? "when that card comes, over its rivers" : "on that card"}: `, dot("bet"), "bets and raises, ", dot("call"), deep ? "calls." : `calls. A full bar is ${ofPot(d, top)}${d.pot > 0 ? " of the pot" : " chips"}.`);
  return s;
}

/** cells: the next cards of the node itself (a turn node's rivers; a flop node's turns, shown alone)
 *  or the rivers of the enlarged turn; before: the cards dealt on the way ([] or [turn]). A column
 *  per rank, a row per suit; `fill` is the height to take (the map's, so that nothing under the
 *  picture moves), or 0. A river goes to its node in the tree on a click, a turn is enlarged. */
function renderBars(d, cells, before, head, fill) {
  const plot = B.el.plot;
  plot.textContent = "";
  const hl = heat();
  const next = STREET[d.board.length + before.length + 1];
  const label = hl != null ? d.lines[hl] : null;
  const shown = Math.max(B.n, DETAIL);
  const top = full();   // the scale of every bar: the most over all the runouts
  const deep = cells.some((c) => c.cards && c.cards.length);   // turn cards with rivers under them
  const click = deep ? "Click to see its rivers" : "Click to go to it in the tree";
  if (label != null) plot.appendChild(heatCaption(d, hl, `on each ${next} card.`));
  else {
    plot.appendChild(caption(el("b", "", before.length ? `Every river on the ${before[0][0]}${GLYPH[before[0][1]]} turn` : `Every ${next} card`),
      !money() ? ` Each bar holds the ${shown} lines ${d.hand} plays most often ${deep ? "as far as that card, whatever the river" : "on that card"}.`
        : part() === "all" && told() ? barKey(d, top, deep)
        : ` The taller a bar, the more ${d.hand} ${PARTS[part()].does} ${deep ? "when that card comes, over its rivers." : `on that card. A full bar is ${ofPot(d, top)}${d.pot > 0 ? " of the pot" : " chips"}.`}`));
  }
  const ROWS = SUITS.length, ROW_GAP = 6;
  const used = (head ? HEAD + GAP : 0) + CAP + GAP + (ROWS - 1) * ROW_GAP + ROWS * (CARD_H + 2);
  const bar = fill ? clamp(Math.floor((fill - used) / ROWS), 30, 140) : 56;
  const pic = el("div", "rl-pic"), g = el("div", "rl-grid");
  g.style.rowGap = ROW_GAP + "px";
  if (head) pic.appendChild(head);
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
      const seg = (color, f) => {
        const i = el("i");
        i.style.flexBasis = (100 * f).toFixed(2) + "%";
        i.style.background = color;
        stack.appendChild(i);
      };
      let lit = null;
      if (money()) {
        // all of them: the two parts, one on the other (a report that does not tell them: one bar)
        for (const k of part() !== "all" ? [part()] : told() ? ["bet", "call"] : ["all"]) {
          const f = strength(chipsOf(cell, k), top);
          if (f > 0) seg(`var(${PARTS[k].color})`, f);
        }
      } else if (label != null) {
        const f = shareOf(d.lines, cell, label);
        if (f > 0) seg(cssColor(hl), f);
      } else {
        const bs = bands(cell, shown);
        for (const x of bs) seg(cssColor(x.line), x.f);
        lit = bs.map((x) => x.line);
      }
      b.onmouseenter = () => markRows(lit);
      const rivers = !!(cell.cards && cell.cards.length);
      if (deep && !rivers) b.classList.add("end");   // a turn with no river to see
      b.onmousemove = (ev) => showTip(ev, tipHtml(d, before.concat(card), cell, deep && !rivers ? "" : click, deep));
      b.onmouseleave = () => { hideTip(); markRows(null); };
      b.onclick = () => {
        hideTip();
        if (rivers) zoom(card);
        else if (!deep && cell.go) B.ctx.goto(d.id + ":" + cell.go);
      };
    }
  }
  pic.appendChild(g);
  plot.appendChild(pic);
  return fill;
}

/* ------------------------------------------------------------------ tooltip, the lists */

/** deep: the cell is a turn card with its rivers under it (its chips are an average over them). */
function tipHtml(d, cards, cell, click, deep = false) {
  const names = d.board.length === 3 ? ["Turn", "River"] : ["River"];
  let h = `<div class="t">${cards.map((c, i) => `${names[i]} ${cardHtml(c)}`).join(" &nbsp; ")}</div>`;
  const last = names[cards.length - 1].toLowerCase();
  if (money()) {
    const p = part();
    h += `<div class="r${p === "all" ? "" : " rl-tip-more"}"><span>${d.hand} puts in, on average${deep ? " over its rivers" : ""}</span><b>${ofPot(d, cell.put)}</b></div>`;
    // the two parts of it, the one the picture shows in full ink
    if (told()) {
      for (const k of ["bet", "call"]) {
        h += `<div class="r${p === "all" || p === k ? "" : " rl-tip-more"}"><span>${dotHtml(k)}${PARTS[k].by.trim()}</span><b>${ofPot(d, chipsOf(cell, k))}</b></div>`;
      }
    }
    if (cell.reach < 0.995 && cell.lines.length) {
      h += `<div class="r rl-tip-more"><span>${p === "all" ? "" : PARTS[p].name.toLowerCase() + ", "}when it sees this ${last} (${pct(cell.reach)} of the time)</span><b>${ofPot(d, chipsOf(cell, p, true))}</b></div>`;
    }
    if (d.pot > 0) h += `<div class="rl-tip-sub">${chips(cell.put)} chips: ${ofPot(d, cell.put)} of the ${fmt(d.pot, 0)} in the pot now</div>`;
    if (cell.lines.length) h += `<div class="rl-tip-sub">Its lines there</div>`;
  }
  const shown = money() ? DETAIL : Math.max(B.n, DETAIL);
  let sum = 0;
  cell.lines.slice(0, money() ? DETAIL : Math.max(shown, 5)).forEach(([i, f], k) => {
    sum += f;
    h += `<div class="r${k < shown && !money() ? "" : " rl-tip-more"}"><span>${swatchHtml(i)}${words(d.lines[i])}</span><b>${pct(f)}</b></div>`;
  });
  // a runout the hand never plays (its pot always ends first) has no lines: only what it has put in
  if (!cell.lines.length) return h + `<div class="rl-tip-sub">${d.hand} has nothing left to do by this ${last}: its pot always ends first (a fold, or all the chips in)</div>`;
  if (sum < 0.995 && !money()) h += `<div class="r rl-tip-other"><span>other lines</span><b>${pct(1 - sum)}</b></div>`;
  if (cell.reach < 0.995 && !money()) h += `<div class="rl-tip-sub">${d.hand} gets to this ${last} ${pct(cell.reach)} of the time</div>`;
  if (click) h += `<div class="rl-tip-sub">${click}</div>`;
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
  drawScale();
  if (!d || !d.now) return;
  const dep = depth(d);
  if (!dep) return;
  const cells = cellsAt(d, level()), what = over(d);
  if (money()) return drawMoney(sec, d, what);
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

/** What the picture is drawn over: the runouts, or the cards of the one street dealt below the node. */
function over(d) {
  return level(d) === 2 ? "runouts" : `${STREET[d.board.length + 1]} cards`;
}

/** Where the chips are counted to: the end of the pot, or of the last street the tree has (a save
 *  without the later streets). */
function until(d) {
  return d.cut ? `the end of the ${STREET[d.board.length + depth(d)]}` : "the pot ends";
}

/** The Money in view's scale, over the picture: from nothing put in to the most over the runouts. */
function drawScale() {
  const s = B.el.scale, d = B.data, r = B.put;
  s.textContent = "";
  if (!d || !d.now || !r || !depth(d)) return;
  const most = `${ofPot(d, full())}${d.pot > 0 ? " of the pot" : " chips"}`;
  s.classList.toggle("full", told());
  if (told()) {      // columns: what a full box is
    s.title = `A ${mark()} is empty for nothing and full for the most ${d.hand} ${PARTS[part()].does} over the ${over(d)}: ${most}`;
    s.append(el("span", "", `full ${mark()}: ${most}`));
    return;
  }
  s.title = `${mapped() ? "The colour of a box" : "The height of a bar"}: empty for nothing put in, full for the most ${d.hand} puts in over the ${over(d)}`;
  s.append(el("span", "", "0"), el("i"), el("span", "", most));
}

/** The key to the Money in view: the average, and where the hand puts in the most and the least. */
function drawMoney(sec, d, what) {
  sec.appendChild(el("h3", "", "Money in"));
  const r = B.put;
  if (!r) return;
  const unit = d.pot > 0 ? "of the pot" : "chips";
  const hero = el("div", "rl-hero");
  hero.append(el("b", "", ofPot(d, d.now.put)),
    el("span", "", `${unit}: what ${d.hand} puts in from here${d.cut ? " to " + until(d) : ""} on average, over the ${what}. ${chips(d.now.put)} of the ${fmt(d.behind, 0)} chips ${d.player} has behind; the pot is ${fmt(d.pot, 0)} now.`));
  sec.appendChild(hero);
  const p = part(), range = p === "all" ? r : B.parts[p];
  const row = (k, name, v, cards = []) => {
    const x = el("div", "rl-row"), on = el("span", "t");
    on.append(name, ...cards.map(cardTag));
    x.append(k === "all" && told() ? el("i", "rl-dot") : dot(k), on, el("span", "p", ofPot(d, v)), el("span", "m", chips(v)));
    return x;
  };
  const head = el("div", "rl-row head");
  head.append(el("span", "t", ""), el("span", "p", d.pot > 0 ? "of pot" : ""), el("span", "m", "chips"));
  const list = el("div", "rl-list rl-money");
  list.appendChild(head);
  // the two parts of the average; a click shows one alone on the picture, another all of them again
  if (told()) {
    for (const k of ["bet", "call"]) {
      const x = row(k, PARTS[k].name, chipsOf(d.now, k));
      x.classList.add("pick");
      x.classList.toggle("sel", p === k);
      x.title = `What ${d.hand} puts in${PARTS[k].by}, on average over the ${what}. Click to see ${p === k ? "both again" : "these alone"} on the picture`;
      x.onclick = () => setPart(p === k ? "all" : k);
      list.appendChild(x);
    }
  }
  if (range) list.append(row(p, PARTS[p].most + " ", range.max.put, range.max.cards), row(p, PARTS[p].least + " ", range.min.put, range.min.cards));
  sec.appendChild(list);
}

/** What the hand does on this street. */
function drawNow() {
  const sec = B.el.now, d = B.data;
  sec.textContent = "";
  if (!d || !d.now) return;
  sec.appendChild(el("h3", "", `${d.hand} on the ${STREET[d.board.length]}`));
  if (money() && !depth(d)) {
    sec.appendChild(el("div", "hint", `From here${d.cut ? " to " + until(d) : ""} ${d.hand} puts in ${ofPot(d, d.now.put)}${d.pot > 0 ? " of the pot" : ""} on average (${chips(d.now.put)} chips)`
      + (told() ? `: ${ofPot(d, chipsOf(d.now, "bet"))} by betting and raising, ${ofPot(d, chipsOf(d.now, "call"))} by calling.` : ".")));
  }
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
  const dep = depth(d), lv = level(d), street = STREET[d.board.length];
  const turns = dep === 2 && lv === 1;   // a flop node by its turn cards alone
  sec.appendChild(el("h3", "", "Reading it"));
  if (money()) {
    sec.appendChild(el("div", "hint", `Money in is every chip ${d.hand} puts into the pot from this node ${d.cut ? "to " + until(d) : "until the pot ends"}: calls, and bets and raises `
      + `whether or not they are called. It is an average over the ways ${d.hand} and the other player play it.`));
    if (told()) {
      sec.appendChild(el("div", "hint", `A chip counts by the action that puts it in: a call's chips are calls, a bet's or a raise's are bets and raises. A raise counts whole, `
        + `the part that matches the bet it answers too. ` + (dep > 0 ? `Both together, a ${mark()} is full for the most ${d.hand} puts in over the ${over(d)}: one scale, so the two colours compare, with each other and from ${mark()} to ${mark()}. `
          + `Alone, each fills a ${mark()} at its own most; the figure over the picture says what a full ${mark()} is.` : "")));
    }
    if (d.cut) {
      sec.appendChild(el("div", "hint warn", `This tree was saved without the ${STREET[d.board.length + dep + 1]}: what ${d.hand} puts in after the ${STREET[d.board.length + dep]} is not counted.`));
    }
    if (turns) {
      sec.appendChild(el("div", "hint", `A turn card's figure is the average over the rivers that can follow it${d.cut ? "" : ", to the end of the pot"}: what the row of that turn holds in the map of every runout, in one bar.`));
    }
    if (dep > 0) {
      sec.appendChild(el("div", "hint", `A ${lv === 2 ? "runout" : "card"} counts all of them, the pots that ended before the card came too (a fold, an all-in): those at what `
        + `${d.hand} had put in by then. The tooltip also gives the figure for the times it does get to the card.`));
    }
    return;
  }
  sec.appendChild(el("div", "hint", (dep === 0 ? `A line is what ${d.hand} itself does from here. `
    : `A line is what ${d.hand} itself does, street by street: ` + (lv === 2 ? `bet · bet · check is a bet on the ${street}, a bet on the turn and a check on the river. `
      : `bet · check is a bet on the ${street}, then a check on the ${STREET[d.board.length + 1]}. `))
    + `Two letters on one street are two actions there: xc check then call, xf check then fold, xr check-raise, bc bet then call, bf bet then fold. `
    + `A dash is a street with nothing left to do.`));
  if (dep > 0) {
    sec.appendChild(el("div", "hint", (mapped() && B.n === 1 ? `A box of the map is the colour of the line played most often on that runout; point at it for how often. ` : "")
      + (turns ? `A bar is a turn card: the lines stop at the turn, whatever the river brings. Click a bar for the rivers of that turn. ` : "")
      + `The percentages are of the times ${d.hand} gets to that card: the pot has not ended before it.`
      + (B.stats.length > COLORS ? ` The ${COLORS} lines that weigh most in the picture have a colour of their own; the others are grey.` : "")));
  }
}
