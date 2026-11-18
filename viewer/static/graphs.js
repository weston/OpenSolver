// Range distributions for the Graphs tab: each player's combos sorted by equity or EV and laid
// out along the share of the range they make up. Pure arithmetic over an /api/graphs answer
// (no DOM), so viewer/test_graphs_js.mjs can check it.

/**
 * One player's curve: combos with reach and a value, weakest first, so the curve rises to the right (owner). Each point covers
 * [x0, x1) of the range (reach-weighted shares, 0..1) at value v. `avg` is the matchup-weighted
 * average, the same range figure the study top bar and Pio report; `median` the value at half the range.
 */
export function curve(reach, values, matchups, order) {
  const pts = [];
  let total = 0, num = 0, den = 0;
  for (let i = 0; i < reach.length; i++) {
    const r = reach[i] || 0, v = values[i];
    if (r <= 0 || v == null) continue;
    pts.push({ i, combo: order ? order[i] : null, r, v });
    total += r;
    const m = matchups ? matchups[i] || 0 : r;
    if (m > 0) { num += m * v; den += m; }
  }
  pts.sort((a, b) => a.v - b.v || a.i - b.i);
  let x = 0;
  for (const p of pts) {
    p.x0 = total > 0 ? x / total : 0;
    x += p.r;
    p.x1 = total > 0 ? x / total : 0;
  }
  return { pts, total, avg: den > 0 ? num / den : null, median: at({ pts }, 0.5)?.v ?? null };
}

/** The point covering share x (0..1) of the range, or null for an empty curve. */
export function at(c, x) {
  const p = c.pts;
  if (!p.length) return null;
  let lo = 0, hi = p.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (p[mid].x1 <= x) lo = mid + 1; else hi = mid;
  }
  return p[lo];
}

/** Both players' curves for one metric ("eq" or "ev") of an /api/graphs answer. */
export function curves(data, metric, order) {
  const one = (d) => curve(d.reach, metric === "eq" ? d.eq : d.ev, metric === "eq" ? d.eq_matchups : d.ev_matchups, order);
  return { OOP: one(data.oop), IP: one(data.ip) };
}

/** Share of the range at or above a value (e.g. how much of the range has 50%+ equity). */
export function shareAbove(c, v) {
  if (!(c.total > 0)) return 0;
  let s = 0;
  for (const p of c.pts) if (p.v >= v) s += p.r;
  return s / c.total;
}
