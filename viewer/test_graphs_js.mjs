// Unit test for the Graphs tab's range curves (viewer/static/graphs.js).
// usage: node viewer/test_graphs_js.mjs
import { curve, curves, at, shareAbove, mixingCurve } from "./static/graphs.js";

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };
const near = (a, b, eps = 1e-9) => a != null && b != null && Math.abs(a - b) <= eps;

// four combos: reach 1, 0.5, 0 (not in range), 2.5; one in range without a value is left out
const order = ["AsAh", "KsKh", "QsQh", "JsJh", "TsTh"];
const reach = [1, 0.5, 0, 2.5, 1];
const eq = [0.9, 0.6, 0.99, 0.3, null];
const mu = [2, 1, 0, 1, 0];
const c = curve(reach, eq, mu, order);
check(c.pts.length === 3, `combos with reach and a value only (got ${c.pts.length})`);
check(c.pts.map((p) => p.combo).join() === "JsJh,KsKh,AsAh", "weakest first (the curve rises to the right)");
check(near(c.total, 4), "total reach");
check(near(c.pts[0].x0, 0) && near(c.pts[0].x1, 0.625) && near(c.pts[1].x1, 0.75) && near(c.pts[2].x1, 1), "x spans are shares of the range");
check(near(c.avg, (2 * 0.9 + 1 * 0.6 + 1 * 0.3) / 4), `matchup-weighted average (got ${c.avg})`);
check(near(curve(reach, eq, null, order).avg, (0.9 + 0.5 * 0.6 + 2.5 * 0.3) / 4), "reach-weighted without matchups");
check(c.median === 0.3, "median: the value at half the range (JJ covers 0 .. 62.5%)");
check(at(c, 0).combo === "JsJh" && at(c, 0.7).combo === "KsKh" && at(c, 1).combo === "AsAh", "the point covering a share");
check(near(shareAbove(c, 0.5), 0.375), "share at or above 50%");
check(curve([0, 0], [1, 1], null, null).pts.length === 0 && at({ pts: [] }, 0.5) === null, "an empty range");
const both = curves({ oop: { reach, eq, eq_matchups: mu, ev: [5, 4, 3, 2, 1], ev_matchups: null },
                      ip: { reach: [0, 0, 1, 0, 0], eq: [0, 0, 0.5, 0, 0], eq_matchups: null, ev: [0, 0, -3, 0, 0], ev_matchups: null } }, "ev", order);
check(both.OOP.pts[both.OOP.pts.length - 1].v === 5 && both.IP.pts.length === 1 && both.IP.pts[0].v === -3, "curves() picks the metric for both players");

const mixingReach = [8, 1, 1, 0.5, 0, 1];
const frequencies = [[1, .9, .5, 1/3, .5, 0], [0, .1, .5, 1/3, .5, 0], [0, 0, 0, 1/3, 0, 0]];
const mixing = mixingCurve(mixingReach, frequencies, [...order, "9s9h"]);
check(mixing.pts.length === 4 && near(mixing.total, 10.5), "mixing excludes unreachable combos and combos without a strategy");
check(near(mixing.pts[0].v, 0) && near(mixing.pts[1].v, (1/.82 - 1)/2) && near(mixing.pts[2].v, .5) && near(mixing.pts[3].v, 1),
  "mixing runs from zero for pure play to one for an equal mix of every available action");
check(near(mixing.avg, ((1/.82 - 1)/2 + .5 + .5)/10.5) && mixing.median === 0 && near(mixing.nearlyPure, 9/10.5),
  "mean, median, and nearly-pure share use the combo's weight at the node");
check(near(mixing.pts[0].x1, 8/10.5) && at(mixing, .8).combo === "KsKh", "mixing curve widths and inspection follow range weight");
check(near(mixing.pts.reduce((area, point) => area + point.v * (point.x1 - point.x0), 0), mixing.avg),
  "the area under the curve equals the range-weighted mean mixing score");
const unused = mixingCurve(mixingReach, [...frequencies, mixingReach.map(() => 0)], [...order, "9s9h"]);
check(near(unused.avg, mixing.avg * 2/3) && unused.pts.every((p, i) => near(p.v, mixing.pts[i].v * 2/3)), "normalization includes every available action, including unused options");
const reordered = mixingCurve(mixingReach, frequencies.slice().reverse(), [...order, "9s9h"]);
check(near(reordered.avg, mixing.avg), "action order does not change mixing");
const pureRange = mixingCurve([1, 1], [[1, 0], [0, 1]], order);
check(pureRange.avg === 0 && pureRange.nearlyPure === 1, "different pure actions across the range still count as pure combos");
check(mixingCurve([1], [[.99999], [0]], order).avg === 0, "frequency rounding is normalized before measuring mixing");
check(mixingCurve([1], [[1]], order).avg === 0 && mixingCurve([0], [[1]], order).avg === null, "forced actions are pure and empty ranges have no average");
for (const count of [2, 3, 4, 7]) {
  check(near(mixingCurve([1], Array.from({length: count}, () => [1/count]), order).avg, 1), `equal mixing across ${count} actions reaches 100%`);
}
check(near(mixingCurve([1], [[.5], [.5]], order).avg, 1) && near(mixingCurve([1], [[.5], [.5], [0]], order).avg, .5),
  "a 50/50 mix is maximum with two actions and halfway to maximum with three");

if (fails) { console.log(`${fails} FAILED`); process.exit(1); }
console.log("ALL PASSED");
