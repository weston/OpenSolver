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

export function mixingCurve(reach, frequencies, order, sortValues = null) {
  const values = new Array(reach.length).fill(null);
  const strategies = new Array(reach.length);
  let nearlyPure = 0;
  for (let i = 0; i < reach.length; i++) {
    if (!(reach[i] > 0)) continue;
    const weights = frequencies.map((row) => row[i]);
    const total = weights.reduce((sum, p) => sum + p, 0);
    if (!(total > 0)) continue;
    const strategy = weights.map((p) => p / total);
    const effectiveActions = 1 / strategy.reduce((sum, p) => sum + p * p, 0);
    values[i] = strategy.length > 1 ? Math.min(1, Math.max(0, (effectiveActions - 1) / (strategy.length - 1))) : 0;
    strategies[i] = strategy;
    if (Math.max(...strategy) >= 0.9 - 1e-9) nearlyPure += reach[i];
  }
  const result = curve(reach, values, null, order);
  for (const point of result.pts) {
    point.strategy = strategies[point.i];
    point.sortValue = sortValues && Number.isFinite(sortValues[point.i]) ? sortValues[point.i] : null;
  }
  if (sortValues) {
    result.pts.sort((a, b) => (a.sortValue ?? Infinity) - (b.sortValue ?? Infinity) || a.v - b.v || a.i - b.i);
    let weight = 0;
    for (const point of result.pts) {
      point.x0 = weight / result.total;
      weight += point.r;
      point.x1 = weight / result.total;
    }
  }
  return { ...result, nearlyPure: result.total > 0 ? nearlyPure / result.total : null };
}

export function mixingPlot(profile, raw) {
  if (!raw) return { pts: profile.pts, lo: 0, hi: 1, area: profile.avg };
  const pts = profile.pts.filter(point => point.sortValue != null);
  const lo = pts[0]?.sortValue ?? 0, hi = pts.at(-1)?.sortValue ?? 0;
  const span = hi - lo;
  let area = 0;
  const positioned = pts.map((point, i) => {
    const x = span > 0 ? (point.sortValue - lo) / span : 0.5;
    if (i > 0 && span > 0) {
      const previous = pts[i - 1];
      area += (point.v + previous.v) / 2 * (point.sortValue - previous.sortValue) / span;
    }
    return { ...point, x };
  });
  return { pts: positioned, lo, hi, area: span > 0 ? area : null };
}

export function equityVolume(data, equity, binCount = 10) {
  const own = data.node.pot[data.actor === "OOP" ? 0 : 1];
  const sizes = data.children.map(child => {
    const token = child.id.slice(child.id.lastIndexOf(":") + 1);
    return token.startsWith("b") ? Number(token.slice(1)) - own : 0;
  });
  const bins = Array.from({ length: binCount }, (_, i) => ({
    lo: i / binCount, hi: (i + 1) / binCount, reach: 0, volume: 0, actions: sizes.map(() => 0),
  }));
  let reach = 0, volume = 0, aggressive = 0, weightedEquity = 0;
  for (let i = 0; i < data.reach.length; i++) {
    const weight = data.reach[i], eq = equity[i];
    const total = data.freq.reduce((sum, row) => sum + row[i], 0);
    if (!(weight > 0) || !Number.isFinite(eq) || !(total > 0)) continue;
    const bin = bins[Math.min(binCount - 1, Math.max(0, Math.floor(eq * binCount)))];
    reach += weight;
    bin.reach += weight;
    for (let k = 0; k < sizes.length; k++) {
      if (!(sizes[k] > 0)) continue;
      const frequency = data.freq[k][i] / total;
      const amount = weight * frequency * sizes[k];
      bin.actions[k] += amount;
      bin.volume += amount;
      volume += amount;
      aggressive += weight * frequency;
      weightedEquity += amount * eq;
    }
  }
  return { bins, sizes, reach, volume, average: reach > 0 ? volume / reach : 0,
    frequency: reach > 0 ? aggressive / reach : 0, equity: volume > 0 ? weightedEquity / volume : null };
}
