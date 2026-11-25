// Convergence chart (#115): exploitability as % of the pot on a linear axis (owner preference,
// #141), against solve time.
// Plain SVG; redrawn from the full series on each update (a few hundred points at most).

const NS = "http://www.w3.org/2000/svg";
const mk = (tag, attrs, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
};

function niceTime(t) {
  if (t < 1) return t.toFixed(1) + "s";
  if (t < 59.5) return Math.round(t) + "s";   // 59.5 s and up round to a minute
  const s = Math.round(t);   // round first: 119.7 s is 2m00, not 1m60
  return Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0");
}
function pctLabel(v, step, unit = "%") {
  const d = step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : 3;
  return v.toFixed(d) + unit;
}

/** A "nice" tick step (1, 2 or 5 x 10^k) giving about `n` intervals up to `max`. */
function niceStep(max, n = 4) {
  const raw = max / n, p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}

/** points: [{t, explo}], pot: chips, target: % of pot (optional). With a unit the scale is another one:
 *  a preflop tree (#492) passes its big blind as `pot` and "bb/100", and the axis counts bb/100. */
export function drawChart(svg, points, pot, target, unit = null) {
  const W = 360, Hh = 200, L = 40, R = 10, T = 10, B = 24;
  svg.textContent = "";
  const defs = mk("defs", {}, svg);
  const gradient = `${svg.id || "chart"}-grad`;
  const g = mk("linearGradient", { id: gradient, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  mk("stop", { offset: "0%", style: "stop-color:var(--accent);stop-opacity:.28" }, g);
  mk("stop", { offset: "100%", style: "stop-color:var(--accent);stop-opacity:0" }, g);

  const pts = (points || []).filter((p) => p.explo != null && p.explo >= 0 && pot > 0)
    .map((p) => ({ t: p.t, y: (p.explo / pot) * 100 }));
  if (pts.length < 1) {
    const t = mk("text", { x: W / 2, y: Hh / 2, "text-anchor": "middle", class: "empty" }, svg);
    t.textContent = "Solve to see convergence";
    return;
  }
  const tMax = Math.max(pts[pts.length - 1].t, 0.5);
  const yTop = Math.max(...pts.map((p) => p.y), target > 0 ? target : 0, 0.01);
  const step = niceStep(yTop);
  const yMax = Math.ceil(yTop / step - 1e-9) * step;
  const x = (t) => L + (t / tMax) * (W - L - R);
  const y = (v) => T + (1 - v / yMax) * (Hh - T - B);

  const grid = mk("g", { class: "grid" }, svg), axis = mk("g", { class: "axis" }, svg);
  for (let v = 0; v <= yMax + step / 2; v += step) {
    const yy = y(v);
    mk("line", { x1: L, x2: W - R, y1: yy, y2: yy }, grid);
    const t = mk("text", { x: L - 6, y: yy + 3, "text-anchor": "end" }, axis);
    t.textContent = pctLabel(v, step, unit ? "" : "%");
  }
  for (let i = 0; i <= 4; i++) {
    const tt = (tMax * i) / 4;
    const t = mk("text", { x: x(tt), y: Hh - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, axis);
    t.textContent = niceTime(tt);
  }
  if (target > 0) {
    const yy = y(target);
    mk("line", { x1: L, x2: W - R, y1: yy, y2: yy, class: "target" }, svg);
    const t = mk("text", { x: W - R, y: yy - 4, "text-anchor": "end", class: "target-lbl" }, svg);
    t.textContent = "target " + pctLabel(target, target >= 1 ? 1 : target >= 0.1 ? 0.1 : 0.01, unit ? ` ${unit}` : "%");
  }
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
  mk("path", { d: `${d}L${x(pts[pts.length - 1].t).toFixed(1)},${Hh - B}L${x(pts[0].t).toFixed(1)},${Hh - B}Z`, class: "area", fill: `url(#${gradient})` }, svg);
  mk("path", { d, class: "line" }, svg);
  const last = pts[pts.length - 1];
  mk("circle", { cx: x(last.t), cy: y(last.y), r: 3.5, class: "head" }, svg);
}

/** Tiny sparkline for the top-bar pill. */
export function drawSpark(svg, points, pot) {
  svg.textContent = "";
  const pts = (points || []).filter((p) => p.explo > 0 && pot > 0);
  if (pts.length < 2) return;
  const ys = pts.map((p) => (p.explo / pot) * 100);
  const lo = 0, hi = Math.max(...ys), tMax = pts[pts.length - 1].t || 1;
  const d = pts.map((p, i) => {
    const xx = (p.t / tMax) * 60, yy = hi > lo ? 2 + (1 - (ys[i] - lo) / (hi - lo)) * 14 : 9;
    return `${i ? "L" : "M"}${xx.toFixed(1)},${yy.toFixed(1)}`;
  }).join("");
  mk("path", { d }, svg);
}
