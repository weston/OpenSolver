// Convergence chart (#115): exploitability as % of the pot on a log axis, against solve time.
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
  if (t < 60) return Math.round(t) + "s";
  return Math.floor(t / 60) + "m" + String(Math.round(t % 60)).padStart(2, "0");
}
function pctLabel(v) {
  if (v >= 1) return v.toFixed(0) + "%";
  if (v >= 0.1) return v.toFixed(1) + "%";
  return v.toFixed(2) + "%";
}

/** points: [{t, explo}], pot: chips, target: % of pot (optional). */
export function drawChart(svg, points, pot, target) {
  const W = 360, Hh = 200, L = 40, R = 10, T = 10, B = 24;
  svg.textContent = "";
  const defs = mk("defs", {}, svg);
  const g = mk("linearGradient", { id: "chart-grad", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  mk("stop", { offset: "0%", style: "stop-color:var(--accent);stop-opacity:.28" }, g);
  mk("stop", { offset: "100%", style: "stop-color:var(--accent);stop-opacity:0" }, g);

  const pts = (points || []).filter((p) => p.explo != null && p.explo > 0 && pot > 0)
    .map((p) => ({ t: p.t, y: (p.explo / pot) * 100 }));
  if (pts.length < 1) {
    const t = mk("text", { x: W / 2, y: Hh / 2, "text-anchor": "middle", class: "empty" }, svg);
    t.textContent = "Solve to see convergence";
    return;
  }
  const tMax = Math.max(pts[pts.length - 1].t, 0.5);
  let yMax = Math.max(...pts.map((p) => p.y)), yMin = Math.min(...pts.map((p) => p.y));
  if (target > 0) yMin = Math.min(yMin, target);
  let lo = Math.floor(Math.log10(yMin)), hi = Math.ceil(Math.log10(yMax));
  if (hi - lo < 1) hi = lo + 1;
  const x = (t) => L + (t / tMax) * (W - L - R);
  const y = (v) => T + (1 - (Math.log10(v) - lo) / (hi - lo)) * (Hh - T - B);

  const grid = mk("g", { class: "grid" }, svg), axis = mk("g", { class: "axis" }, svg);
  for (let e = lo; e <= hi; e++) {
    const v = Math.pow(10, e), yy = y(v);
    mk("line", { x1: L, x2: W - R, y1: yy, y2: yy }, grid);
    const t = mk("text", { x: L - 6, y: yy + 3, "text-anchor": "end" }, axis);
    t.textContent = pctLabel(v);
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
    t.textContent = "target " + pctLabel(target);
  }
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
  mk("path", { d: `${d}L${x(pts[pts.length - 1].t).toFixed(1)},${Hh - B}L${x(pts[0].t).toFixed(1)},${Hh - B}Z`, class: "area" }, svg);
  mk("path", { d, class: "line" }, svg);
  const last = pts[pts.length - 1];
  mk("circle", { cx: x(last.t), cy: y(last.y), r: 3.5, class: "head" }, svg);
}

/** Tiny sparkline for the top-bar pill. */
export function drawSpark(svg, points, pot) {
  svg.textContent = "";
  const pts = (points || []).filter((p) => p.explo > 0 && pot > 0);
  if (pts.length < 2) return;
  const ys = pts.map((p) => Math.log10((p.explo / pot) * 100));
  const lo = Math.min(...ys), hi = Math.max(...ys), tMax = pts[pts.length - 1].t || 1;
  const d = pts.map((p, i) => {
    const xx = (p.t / tMax) * 60, yy = hi > lo ? 2 + (1 - (ys[i] - lo) / (hi - lo)) * 14 : 9;
    return `${i ? "L" : "M"}${xx.toFixed(1)},${yy.toFixed(1)}`;
  }).join("");
  mk("path", { d }, svg);
}
