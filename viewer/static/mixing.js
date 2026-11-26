import { H, el, comboEl, displayCombo } from "./cards.js";
import { fmt } from "./api.js";
import { mixingCurve, mixingPlot, at } from "./graphs.js";

let view = null;

function svgEl(tag, attrs, parent, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text != null) element.textContent = text;
  parent.appendChild(element);
  return element;
}

export function renderMixing(box, data, actions, sortBy = "mixing", sortValues = null) {
  if (view?.box !== box) {
    view?.observer.disconnect();
    view = { box, data: null, actions: [], width: 0, position: 0.5 };
    view.observer = new ResizeObserver(() => {
      const width = Math.round(box.clientWidth);
      if (width > 0 && width !== view.width) draw();
    });
    view.observer.observe(box);
  }
  if (view.data === data && view.sortBy === sortBy && view.sortValues === sortValues && view.width === Math.round(box.clientWidth)) return;
  if (view.data !== data || view.sortBy !== sortBy || view.sortValues !== sortValues) {
    view.data = data;
    view.actions = actions;
    view.sortBy = sortBy;
    view.sortValues = sortValues;
    view.profile = data?.freq ? mixingCurve(data.reach, data.freq, H.order, sortValues) : null;
    view.position = 0.5;
  }
  draw();
}

function draw() {
  const { box, data, profile, actions, sortBy } = view;
  const raw = sortBy.endsWith("-raw"), metric = sortBy.split("-")[0];
  const plot = profile ? mixingPlot(profile, raw) : null;
  const width = Math.round(box.clientWidth);
  if (!width) return;
  view.width = width;
  box.replaceChildren();
  if (!plot?.pts.length) {
    box.appendChild(el("div", "side-empty", "No strategy at this node."));
    return;
  }

  const head = el("div", "cats-head mixing-head");
  head.append(el("span", `badge ${data.actor.toLowerCase()}`, data.actor), el("strong", "", "Mixing profile"),
    el("span", "muted num", `${fmt(profile.total, 1)} combos`));
  const stats = el("div", "mixing-stats");
  for (const [label, value] of [["Mean mixing", `${fmt(profile.avg * 100, 1)}%`], ["Median mixing", `${fmt(profile.median * 100, 1)}%`],
    ["≥90% one action", `${fmt(profile.nearlyPure * 100, 1)}%`]]) {
    const stat = el("div");
    stat.append(el("b", "num", value), el("span", "", label));
    stats.appendChild(stat);
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const pot = data.node.pot.reduce((sum, value) => sum + value, 0);
  const scale = metric === "eq" ? 100 : pot > 0 ? 100 / pot : 0;
  const axisValue = fraction => raw ? (plot.lo + fraction * (plot.hi - plot.lo)) * scale : fraction * 100;
  const axisLabel = raw ? metric === "eq" ? "Equity (%)" : "EV (% pot)"
    : { mixing: "Range weight · pure → mixed", eq: "Equity percentile · range weight", ev: "EV percentile · range weight" }[metric];
  const height = Math.round(Math.min(340, Math.max(245, width * 0.5)));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "gr-chart mixing-chart");
  svg.setAttribute("role", "slider");
  svg.setAttribute("aria-label", `Combo mixing by ${axisLabel}`);
  svg.dataset.sort = sortBy;
  svg.setAttribute("aria-valuemin", String(axisValue(0)));
  svg.setAttribute("aria-valuemax", String(axisValue(1)));
  svg.setAttribute("tabindex", "0");
  const detail = el("div", "mixing-detail");
  box.append(head, stats, svg, detail);

  const left = 40, right = width - 12, top = 28, bottom = height - 42;
  const x = (fraction) => left + fraction * (right - left);
  const y = (value) => bottom - value * (bottom - top);
  const grid = svgEl("g", { class: "grid" }, svg);
  const axis = svgEl("g", { class: "axis" }, svg);
  svgEl("text", { x: left, y: 12, class: "axis-title" }, axis, "Mixing");
  svgEl("text", { x: right, y: 12, "text-anchor": "end", class: "mixing-area-label" }, svg,
    `${raw ? "Normalized area" : "Area"}: ${plot.area == null ? "—" : `${fmt(plot.area * 100, 1)}%`}`);
  for (const value of [0, 0.25, 0.5, 0.75, 1]) {
    svgEl("line", { x1: left, x2: right, y1: y(value), y2: y(value) }, grid);
    svgEl("text", { x: left - 8, y: y(value) + 4, "text-anchor": "end" }, axis, `${value * 100}%`);
  }
  const ticks = raw && plot.lo === plot.hi ? [0.5] : raw && right - left < 300 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const tickDigits = raw ? Math.max(1, Math.min(4, Math.ceil(-Math.log10((plot.hi - plot.lo) * scale / 4 || 1)))) : 0;
  for (const fraction of ticks) {
    svgEl("text", { x: x(fraction), y: bottom + 17, "text-anchor": fraction === 0 ? "start" : fraction === 1 ? "end" : "middle" },
      axis, `${fmt(axisValue(fraction), tickDigits)}%`);
  }
  svgEl("text", { x: (left + right) / 2, y: height - 2, "text-anchor": "middle", class: "axis-title" }, axis, axisLabel);

  const start = raw ? x(plot.pts[0].x) : left;
  let path = `M${start},${y(plot.pts[0].v)}`;
  for (const point of plot.pts) path += raw ? `L${x(point.x)},${y(point.v)}` : `V${y(point.v)}H${x(point.x1)}`;
  svgEl("path", { d: `${path}V${bottom}H${start}Z`, class: "mixing-area" }, svg);
  svgEl("path", { d: path, class: "gr-line mixing-line" }, svg);
  const cross = svgEl("line", { y1: top, y2: bottom, class: "gr-cross" }, svg);
  const dot = svgEl("circle", { r: 4, class: "gr-dot mixing-dot" }, svg);
  const hit = svgEl("rect", { x: left, y: top, width: right - left, height: bottom - top, class: "gr-hit" }, svg);

  let selected = null;
  function select(fraction, point = null) {
    view.position = Math.min(1, Math.max(0, fraction));
    point ??= raw ? plot.pts.reduce((best, candidate) => Math.abs(candidate.x - view.position) < Math.abs(best.x - view.position) ? candidate : best)
      : at(profile, view.position);
    if (raw) view.position = point.x;
    const name = displayCombo(point.combo);
    cross.setAttribute("x1", x(view.position));
    cross.setAttribute("x2", x(view.position));
    dot.setAttribute("cx", x(view.position));
    dot.setAttribute("cy", y(point.v));
    svg.setAttribute("aria-valuenow", fmt(axisValue(view.position), 1));
    svg.setAttribute("aria-valuetext", `${name}, ${fmt(point.v * 100, 1)}% mixing`);
    if (selected === point) return;
    selected = point;
    detail.dataset.combo = point.combo;
    const hand = el("div", "mixing-hand");
    const score = el("span", "mixing-score");
    score.append(el("b", "num", `${fmt(point.v * 100, 1)}%`), el("span", "muted", "mixing"));
    if (sortBy !== "mixing") {
      const value = point.sortValue == null ? null : point.sortValue * scale;
      score.appendChild(el("span", "muted mixing-sort-value", `${metric === "eq" ? "Equity" : "EV"} ${fmt(value, 1)}${value == null ? "" : metric === "eq" ? "%" : "% pot"}`));
    }
    hand.append(comboEl(point.combo), score);
    const bar = el("div", "mini-bar mixing-strategy");
    const legend = el("div", "mixing-actions");
    for (const action of actions) {
      const frequency = point.strategy[action.k];
      const segment = el("span");
      segment.style.width = `${frequency * 100}%`;
      segment.style.background = action.color;
      bar.appendChild(segment);
      const item = el("span", "mixing-action");
      const swatch = el("i");
      swatch.style.background = action.color;
      item.append(swatch, el("span", "", action.label), el("b", "num", `${fmt(frequency * 100, 1)}%`));
      legend.appendChild(item);
    }
    detail.replaceChildren(hand, bar, legend);
  }
  hit.onpointermove = (event) => {
    const bounds = svg.getBoundingClientRect();
    const px = (event.clientX - bounds.left) * width / bounds.width;
    const py = (event.clientY - bounds.top) * height / bounds.height;
    const distance = point => (x(point.x) - px) ** 2 + (y(point.v) - py) ** 2;
    const nearest = raw ? plot.pts.reduce((best, point) => distance(point) < distance(best) ? point : best) : null;
    select((px - left) / (right - left), nearest);
  };
  hit.onpointerdown = (event) => { svg.focus({ preventScroll: true }); hit.onpointermove(event); };
  svg.onkeydown = (event) => {
    const position = { ArrowLeft: view.position - 0.01, ArrowRight: view.position + 0.01, Home: 0, End: 1 }[event.key];
    if (position == null) return;
    event.preventDefault();
    event.stopPropagation();
    if (raw) {
      const index = { Home: 0, End: plot.pts.length - 1, ArrowLeft: plot.pts.indexOf(selected) - 1, ArrowRight: plot.pts.indexOf(selected) + 1 }[event.key];
      const point = plot.pts[Math.max(0, Math.min(plot.pts.length - 1, index))];
      select(point.x, point);
    } else select(position);
  };
  select(view.position);
}
