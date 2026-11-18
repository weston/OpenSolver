// Unit test for the Beta view's runout lines helpers (viewer/static/runouts.js, #467).
// usage: node viewer/test_runouts_js.mjs
import { streets, words, short, depth, cellsAt, pick, bands, lineStats, slots, deck, axis, shareOf, split } from "./static/runouts.js";

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const near = (a, b) => Math.abs(a - b) < 1e-9;

// ---- reading a line label
check(same(streets("bc x -").map((s) => s.map((t) => t.a).join("")), ["bc", "x", ""]), "streets: one list of actions per street, none for '-'");
const sized = streets("b33 xra b125");
check(sized[0][0].size === 33 && !sized[0][0].allin, "streets: a bet carries its size");
check(sized[1][1].a === "r" && sized[1][1].allin && sized[1][1].size === null, "streets: 'ra' is a raise all-in");
check(sized[2][0].size === 125, "streets: a size over 100% of the pot");
check(words("b b x") === "bet · bet · check", `words: ${words("b b x")}`);
check(words("bc xf -") === "bc · xf · –", `words: two actions on a street stay shorthand (${words("bc xf -")})`);
check(words("b33 ba") === "bet 33% · bet all-in", `words: sizes (${words("b33 ba")})`);
check(words("xr") === "xr" && words("f") === "fold", "words: one street");
check(short("b - -") === "b – –", "short: a dash for a street with nothing to do");

// ---- the report's levels
const cell = (card, lines, cards) => ({ card, go: card, reach: 1, lines, ...(cards ? { cards } : {}) });
const report = {
  lines: ["b", "x", "b b", "b x", "x b", "b b b", "b b x", "b x x", "x b b"],
  now: {
    reach: 1, lines: [[0, 0.6], [1, 0.4]],
    cards: [
      cell("Ks", [[2, 0.5], [3, 0.3], [4, 0.2]], [cell("2h", [[5, 0.7], [6, 0.2], [7, 0.1]]), cell("3h", [[6, 0.5], [5, 0.25], [8, 0.25]])]),
      cell("Qs", [[3, 0.9], [2, 0.1]], [cell("2h", [[7, 1]])]),
    ],
  },
};
check(depth(report) === 2, "depth: turn and river cards below a flop node");
check(depth({ now: { cards: [cell("2h", [[0, 1]])] } }) === 1, "depth: one card to come");
check(depth({ now: { lines: [] } }) === 0 && depth({ now: null }) === 0, "depth: none (a river node, a hand that is not there)");
check(same(cellsAt(report, 1).map((c) => c.cards), [["Ks"], ["Qs"]]), "cellsAt 1: one cell per next card");
check(same(cellsAt(report, 2).map((c) => c.cards.join("")), ["Ks2h", "Ks3h", "Qs2h"]), "cellsAt 2: one cell per runout");

// ---- what a box shows
const ks2h = report.now.cards[0].cards[0];
const p = pick(ks2h, 2);
check(p.shown.length === 2 && near(p.other, 0.1), "pick: the n most common lines and the rest");
const b2 = bands(ks2h, 2);
check(same(b2.map((b) => b.line), [5, 6]) && near(b2[0].share, 0.7 / 0.9) && near(b2[0].share + b2[1].share, 1), "bands: the shown lines share the whole box, in proportion");
check(near(b2[0].f, 0.7), "bands: f stays the real frequency");
check(bands(ks2h, 5).length === 3, "bands: no more than the cell has");
check(same(bands({ lines: [] }, 3), []), "bands: an empty cell");
check(bands({ lines: [[0, 0]] }, 3)[0].share === 0, "bands: a zero frequency does not divide by zero");

// ---- which lines get a colour
const stats = lineStats(cellsAt(report, 2));
const by = new Map(stats.map((s) => [s.line, s]));
check(stats[0].line === 7 && near(stats[0].mass, 1 + 0.01), "lineStats: heaviest first (a line that is everything on one runout)");
check(by.get(5).lead === 1 && by.get(6).lead === 1 && by.get(7).lead === 1 && by.get(8).lead === 0, "lineStats: lead counts the runouts a line tops");
check(near(by.get(5).mean, (0.7 + 0.25) / 3) && near(by.get(5).max, 0.7), "lineStats: mean over all the cells, max");
check(same(stats.map((s) => s.line), [7, 5, 6, 8]), `lineStats: order by mass (${stats.map((s) => s.line)})`);
const sl = slots(stats, 2);
check(sl.get(7) === 0 && sl.get(5) === 1 && !sl.has(6), "slots: the k heaviest lines, in that order");
check(same(lineStats([]), []), "lineStats: no cells");
const tie = lineStats([{ cell: { lines: [[3, 0.5], [1, 0.5]] } }]);
check(same(tie.map((s) => s.line), [3, 1]), "lineStats: equal mass -> the one that leads comes first");

// ---- the map's rows and columns
const d = deck(["Td", "9d", "2c"]);
check(d.length === 49 && d[0] === "As" && d[12] === "2s" && d[13] === "Ah" && d[48] === "3c", "deck: 49 cards, a suit at a time, the board's left out");
check(!d.includes("Td") && !d.includes("2c"), "deck: no board card");
const ax = axis(["As", "Ks", "Ah", "Kh", "Ad"], 10, 6);
check(same(ax.at, [0, 11, 28, 39, 56]) && ax.end === 66, `axis: 1px between boxes, a gap where the suit changes (${ax.at})`);
check(ax.index(0) === 0 && ax.index(10) === 0 && ax.index(11) === 1 && ax.index(24) === -1 && ax.index(28) === 2 && ax.index(66) === 4 && ax.index(67) === -1,
  "axis: the box under a pixel, none in a gap or past the end");
check(axis([], 10, 6).end === 0 && axis([], 10, 6).index(0) === -1, "axis: no cards");

// ---- one line over the map (the legend's heat map)
check(near(shareOf(report.lines, ks2h, "b b b"), 0.7), "shareOf: a river line on a river cell");
check(near(shareOf(report.lines, report.now.cards[0], "b b b"), 0.5), "shareOf: a river line on a turn cell is its first two streets");
check(shareOf(report.lines, ks2h, "x x x") === 0 && shareOf(report.lines, { lines: [] }, "b") === 0, "shareOf: not played, or an empty cell");

// ---- whole pixels
check(same(split(10, [0.5, 0.3, 0.2]), [5, 3, 2]), "split: in proportion");
for (const ws of [[0.98, 0.01, 0.01], [1, 1, 1], [0.34, 0.33, 0.33], [5, 0, 1]]) {
  const px = split(11, ws);
  check(px.reduce((a, b) => a + b, 0) === 11 && px.every((x, i) => (ws[i] > 0 ? x >= 1 : x === 0)), `split: ${ws} -> ${px} (nothing lost, a part that weighs something shows)`);
}
const tight = split(2, [1, 1, 1]);
check(tight.reduce((a, b) => a + b, 0) === 2 && tight.every((x) => x <= 1), `split: no room for everyone (${tight})`);
check(same(split(0, [1, 2]), [0, 0]), "split: no pixels");
check(same(split(8, [0, 0]), [0, 0]), "split: nothing to show");

console.log(fails ? `${fails} FAILED` : "all runouts.js tests passed");
process.exit(fails ? 1 : 0);
