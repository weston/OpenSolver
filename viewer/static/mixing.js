import { H, el, comboEl, displayCombo } from "./cards.js";
import { fmt } from "./api.js";
import { mixingCurve, at } from "./graphs.js";

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
  const width = Math.round(box.clientWidth);
  if (!width) return;
  view.width = width;
  box.replaceChildren();
  if (!profile?.pts.length) {
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
  const height = Math.round(Math.min(340, Math.max(245, width * 0.5)));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "gr-chart mixing-chart");
  svg.setAttribute("role", "slider");
  svg.setAttribute("aria-label", `Combo mixing by share of range, ordered by ${sortBy === "eq" ? "equity" : sortBy === "ev" ? "EV" : "mixing"}`);
  svg.dataset.sort = sortBy;
  svg.setAttribute("aria-valuemin", "0");
  svg.setAttribute("aria-valuemax", "100");
  svg.setAttribute("tabindex", "0");
  const detail = el("div", "mixing-detail");
  box.append(head, stats, svg, detail);

  const left = 40, right = width - 12, top = 28, bottom = height - 42;
  const x = (fraction) => left + fraction * (right - left);
  const y = (value) => bottom - value * (bottom - top);
  const grid = svgEl("g", { class: "grid" }, svg);
  const axis = svgEl("g", { class: "axis" }, svg);
  svgEl("text", { x: left, y: 12, class: "axis-title" }, axis, "Mixing");
  svgEl("text", { x: right, y: 12, "text-anchor": "end", class: "mixing-area-label" }, svg, `Area: ${fmt(profile.avg * 100, 1)}%`);
  for (const value of [0, 0.25, 0.5, 0.75, 1]) {
    svgEl("line", { x1: left, x2: right, y1: y(value), y2: y(value) }, grid);
    svgEl("text", { x: left - 8, y: y(value) + 4, "text-anchor": "end" }, axis, `${value * 100}%`);
  }
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    svgEl("text", { x: x(fraction), y: bottom + 17, "text-anchor": fraction === 0 ? "start" : fraction === 1 ? "end" : "middle" },
      axis, `${fraction * 100}%`);
  }
  const direction = { mixing: "pure → mixed", eq: "equity low → high", ev: "EV low → high" }[sortBy];
  svgEl("text", { x: (left + right) / 2, y: height - 2, "text-anchor": "middle", class: "axis-title" }, axis, `Range weight · ${direction}`);

  let path = `M${left},${y(profile.pts[0].v)}`;
  for (const point of profile.pts) path += `V${y(point.v)}H${x(point.x1)}`;
  svgEl("path", { d: `${path}V${bottom}H${left}Z`, class: "mixing-area" }, svg);
  svgEl("path", { d: path, class: "gr-line mixing-line" }, svg);
  const cross = svgEl("line", { y1: top, y2: bottom, class: "gr-cross" }, svg);
  const dot = svgEl("circle", { r: 4, class: "gr-dot mixing-dot" }, svg);
  const hit = svgEl("rect", { x: left, y: top, width: right - left, height: bottom - top, class: "gr-hit" }, svg);

  let selected = null;
  function select(fraction) {
    view.position = Math.min(1, Math.max(0, fraction));
    const point = at(profile, view.position);
    const name = displayCombo(point.combo);
    cross.setAttribute("x1", x(view.position));
    cross.setAttribute("x2", x(view.position));
    dot.setAttribute("cx", x(view.position));
    dot.setAttribute("cy", y(point.v));
    svg.setAttribute("aria-valuenow", fmt(view.position * 100, 1));
    svg.setAttribute("aria-valuetext", `${name}, ${fmt(point.v * 100, 1)}% mixing`);
    if (selected === point) return;
    selected = point;
    detail.dataset.combo = point.combo;
    const hand = el("div", "mixing-hand");
    const score = el("span", "mixing-score");
    score.append(el("b", "num", `${fmt(point.v * 100, 1)}%`), el("span", "muted", "mixing"));
    if (sortBy !== "mixing") {
      const pot = data.node.pot.reduce((sum, value) => sum + value, 0);
      const value = point.sortValue == null ? null : sortBy === "eq" ? point.sortValue * 100 : pot > 0 ? point.sortValue / pot * 100 : null;
      score.appendChild(el("span", "muted mixing-sort-value", `${sortBy === "eq" ? "Equity" : "EV"} ${fmt(value, 1)}${value == null ? "" : sortBy === "eq" ? "%" : "% pot"}`));
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
    select(((event.clientX - bounds.left) * width / bounds.width - left) / (right - left));
  };
  hit.onpointerdown = (event) => { svg.focus({ preventScroll: true }); hit.onpointermove(event); };
  svg.onkeydown = (event) => {
    const position = { ArrowLeft: view.position - 0.01, ArrowRight: view.position + 0.01, Home: 0, End: 1 }[event.key];
    if (position == null) return;
    event.preventDefault();
    event.stopPropagation();
    select(position);
  };
  select(view.position);
}
