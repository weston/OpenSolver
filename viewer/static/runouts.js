// Runout lines (Study's Beta side tab): the parts with no page in them. Reading a line label, choosing what a
// runout's box shows, which lines get a colour, and where the boxes of the turn x river map go. The report is /api/hand_lines
// (viewer/hand_lines.py): {lines: [label], now: {reach, lines: [[line, freq]], put, put_seen, call, call_seen, cards: [{card, ...}]}}.

const TOKEN = /([xbcfr])(a|\d+)?/g;
const WORD = { x: "check", b: "bet", c: "call", f: "fold", r: "raise" };

/** "bc x -" -> one list per street of {a, size, allin}: a is x b c f r, size the bet in % of the pot
 *  (split sizes only), allin whether it is a shove. A street with nothing to do is []. */
export function streets(label) {
  return label.split(" ").map((s) => {
    const out = [];
    if (s !== "-") for (const m of s.matchAll(TOKEN)) out.push({ a: m[1], allin: m[2] === "a", size: m[2] && m[2] !== "a" ? +m[2] : null });
    return out;
  });
}

const one = (t) => WORD[t.a] + (t.allin ? " all-in" : t.size != null ? ` ${t.size}%` : "");
const letter = (t) => t.a + (t.allin ? "-all-in" : t.size != null ? t.size : "");

/** A line in words, street by street: "bet · bet · xc". One action is spelled out, several on a street
 *  are the usual shorthand (xc check-call, xf check-fold, xr check-raise, bc bet-call, bf bet-fold). */
export function words(label) {
  return streets(label).map((st) => (!st.length ? "–" : st.length === 1 ? one(st[0]) : st.map(letter).join(""))).join(" · ");
}

/** The compact spelling for a small cell: "b b xc". */
export function short(label) {
  return label.replace(/-/g, "–");
}

/** How many levels of cards the report goes down: 0 (none to come), 1 or 2. */
export function depth(report) {
  const turns = (report.now && report.now.cards) || [];
  if (!turns.length) return 0;
  return turns.some((t) => t.cards && t.cards.length) ? 2 : 1;
}

/** The cells of one level of cards: [{cards: ["Ks"], cell}] for level 1 (the next card),
 *  [{cards: ["Ks", "2h"], cell}] for level 2 (the two next cards). */
export function cellsAt(report, level) {
  const out = [];
  for (const t of (report.now && report.now.cards) || []) {
    if (level === 1) out.push({ cards: [t.card], cell: t });
    else for (const r of t.cards || []) out.push({ cards: [t.card, r.card], cell: r });
  }
  return out;
}

/** What a cell shows: its n most common lines, and the share of everything else. */
export function pick(cell, n) {
  const shown = cell.lines.slice(0, n);
  let sum = 0;
  for (const [, f] of shown) sum += f;
  return { shown, other: Math.max(0, 1 - sum) };
}

/** What a box draws: the cell's n most common lines, and the part of the box each gets. The parts are
 *  in proportion to each other and fill the box (share sums to 1); f is the line's real frequency. */
export function bands(cell, n) {
  const shown = cell.lines.slice(0, n);
  let sum = 0;
  for (const [, f] of shown) sum += f;
  return shown.map(([line, f]) => ({ line, f, share: sum > 0 ? f / sum : 0 }));
}

/** The lines played over cells (of cellsAt): {line, mean, max, lead, mass}. lead: on how many of the
 *  cells it is the most common line; mean / max: its frequency over the cells, each counted once;
 *  mass: the sum of its squared frequencies, which is large for a line that dominates some runouts
 *  and for one that is played a little everywhere, in that order. Heaviest first. */
export function lineStats(cells) {
  const by = new Map();
  const at = (i) => { if (!by.has(i)) by.set(i, { line: i, mean: 0, max: 0, lead: 0, mass: 0 }); return by.get(i); };
  for (const { cell } of cells) {
    cell.lines.forEach(([i, f], k) => {
      const s = at(i);
      if (k === 0) s.lead++;
      s.mean += f;
      s.mass += f * f;
      if (f > s.max) s.max = f;
    });
  }
  const out = [...by.values()];
  for (const s of out) s.mean /= cells.length || 1;
  return out.sort((a, b) => b.mass - a.mass || b.lead - a.lead || a.line - b.line);
}

/** Which lines get a colour of their own: the k heaviest of lineStats, line -> 0..k-1. It does not
 *  depend on how many lines a box shows, so a line keeps its colour when that changes. */
export function slots(stats, k) {
  return new Map(stats.slice(0, k).map((s, i) => [s.line, i]));
}

const RANKS = "AKQJT98765432", SUITS = "shdc";

/** The cards not on `board`, a suit at a time (spades A..2, hearts, ...). With no board, all 52: the
 *  rows and the columns of the turn x river map, where a card that cannot come keeps its place (every
 *  suit is a block of 13) and is left empty. */
export function deck(board = []) {
  const out = [];
  for (const s of SUITS) for (const r of RANKS) if (!board.includes(r + s)) out.push(r + s);
  return out;
}

/** Where the boxes of one side of the map go: `size` px each, `sep` px apart (0: touching), `gap` px
 *  more where the suit changes. {at: [px], size, end}; index(px) is the box under a pixel, or -1. */
export function axis(cards, size, gap, sep = 1) {
  const at = [];
  let x = 0;
  cards.forEach((c, i) => {
    if (i && c[1] !== cards[i - 1][1]) x += gap;
    at.push(x);
    x += size + sep;
  });
  const end = Math.max(0, x - sep);
  const index = (px) => {
    for (let i = 0; i < at.length; i++) if (px >= at[i] && px < at[i] + size + sep) return i;
    return -1;
  };
  return { at, size, end, index };
}

/** How often a cell's hand is on a line that starts like `label` does, as far as the cell's own lines
 *  go: a river line asked of a turn cell is the share of its first two streets there. */
export function shareOf(lines, cell, label) {
  if (!cell.lines.length) return 0;
  const n = lines[cell.lines[0][0]].split(" ").length;
  const want = label.split(" ").slice(0, n).join(" ");
  let f = 0;
  for (const [k, v] of cell.lines) if (lines[k] === want) f += v;
  return f;
}

/** Split `size` pixels between parts weighing `ws`, whole pixels, nothing lost: the band widths of a
 *  box. In proportion; then a part that weighs something and came out under `min` pixels takes them
 *  from the widest part, while one has pixels to spare. */
export function split(size, ws, min = 1) {
  const total = ws.reduce((a, b) => a + b, 0);
  if (!(total > 0) || !(size > 0)) return ws.map(() => 0);
  const out = [];
  let acc = 0, used = 0;
  for (const w of ws) {
    acc += w;
    const px = Math.round((size * acc) / total) - used;
    out.push(px);
    used += px;
  }
  ws.forEach((w, i) => {
    while (w > 0 && out[i] < min) {
      let j = -1;
      out.forEach((px, k) => { if (k !== i && px > min && (j < 0 || px > out[j])) j = k; });
      if (j < 0) break;
      out[j]--;
      out[i]++;
    }
  });
  return out;
}

/** The parts of a column `size` pixels tall, part k the fraction fs[k] of it: whole pixels each, bottom
 *  first, so that the top of each is where it belongs (the column's own top too). A part that is
 *  something shows: it has `min` pixels, taken from the tallest when the column is full. */
export function stack(size, fs, min = 1) {
  const room = Math.max(0, Math.round(size));
  const out = [];
  let acc = 0, used = 0;
  for (const f of fs) {
    acc += f > 0 ? f : 0;
    const px = clampTo(Math.round(acc * size), room) - used;
    out.push(px);
    used += px;
  }
  fs.forEach((f, i) => {
    while (f > 0 && out[i] < min) {
      if (used < room) used++;
      else {
        let j = -1;
        out.forEach((px, k) => { if (k !== i && px > min && (j < 0 || px > out[j])) j = k; });
        if (j < 0) break;
        out[j]--;
      }
      out[i]++;
    }
  });
  return out;
}
const clampTo = (v, hi) => Math.max(0, Math.min(hi, v));

/** A cell's chips by the action that put them in. part: "all"; "call", the chips of its calls;
 *  "bet", the rest, which went in as bets and raises (a raise whole). seen: the figure for the times
 *  the hand sees the cell's card, not for every pot. null for a part when the report does not tell
 *  the calls apart (a solver from before it did). */
export function chipsOf(cell, part = "all", seen = false) {
  const put = seen ? cell.put_seen : cell.put, call = seen ? cell.call_seen : cell.call;
  if (part === "all") return put;
  if (call == null || put == null) return null;
  return part === "call" ? call : Math.max(0, put - call);
}

/** The least and the most the hand puts in (chipsOf, for a part of the chips or all of them) over
 *  cells (of cellsAt): {min, max}, each {put, cards}; null with no cells, or when the part is not
 *  told. The first of equals, in the order given. */
export function putRange(cells, part = "all") {
  let min = null, max = null;
  for (const { cards, cell } of cells) {
    const put = chipsOf(cell, part);
    if (put == null || !(put >= 0)) continue;
    if (!min || put < min.put) min = { put, cards };
    if (!max || put > max.put) max = { put, cards };
  }
  return min ? { min, max } : null;
}

/** How full a box's colour is for a value v on a scale that ends at max: in proportion, 0 for
 *  nothing, and never under `floor` for something (a box with a little in it must not read as empty). */
export function strength(v, max, floor = 0) {
  if (!(v > 0) || !(max > 0)) return 0;
  return Math.max(floor, Math.min(1, v / max));
}
