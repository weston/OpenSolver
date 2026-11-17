// Unit test for the Graphs tab's range curves (viewer/static/graphs.js).
// usage: node viewer/test_graphs_js.mjs
import { curve, curves, at, shareAbove } from "./static/graphs.js";

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
check(c.pts.map((p) => p.combo).join() === "AsAh,KsKh,JsJh", "strongest first");
check(near(c.total, 4), "total reach");
check(near(c.pts[0].x0, 0) && near(c.pts[0].x1, 0.25) && near(c.pts[1].x1, 0.375) && near(c.pts[2].x1, 1), "x spans are shares of the range");
check(near(c.avg, (2 * 0.9 + 1 * 0.6 + 1 * 0.3) / 4), `matchup-weighted average (got ${c.avg})`);
check(near(curve(reach, eq, null, order).avg, (0.9 + 0.5 * 0.6 + 2.5 * 0.3) / 4), "reach-weighted without matchups");
check(c.median === 0.3, "median: the value at half the range (JJ covers 37.5% .. 100%)");
check(at(c, 0).combo === "AsAh" && at(c, 0.3).combo === "KsKh" && at(c, 1).combo === "JsJh", "the point covering a share");
check(near(shareAbove(c, 0.5), 0.375), "share at or above 50%");
check(curve([0, 0], [1, 1], null, null).pts.length === 0 && at({ pts: [] }, 0.5) === null, "an empty range");
const both = curves({ oop: { reach, eq, eq_matchups: mu, ev: [5, 4, 3, 2, 1], ev_matchups: null },
                      ip: { reach: [0, 0, 1, 0, 0], eq: [0, 0, 0.5, 0, 0], eq_matchups: null, ev: [0, 0, -3, 0, 0], ev_matchups: null } }, "ev", order);
check(both.OOP.pts[0].v === 5 && both.IP.pts.length === 1 && both.IP.pts[0].v === -3, "curves() picks the metric for both players");

if (fails) { console.log(`${fails} FAILED`); process.exit(1); }
console.log("ALL PASSED");
