import { el } from "./cards.js";
import { fmt, fmtEvPct } from "./api.js";
import { equityVolume } from "./graphs.js";

let view = null;

function svgEl(tag, attrs, parent, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text != null) element.textContent = text;
  parent.appendChild(element);
  return element;
}

export function renderVolume(box, data, actions, equity) {
  if (view?.box !== box) {
    view?.observer.disconnect();
    view = { box, data: null, width: 0, selected: 0 };
    view.observer = new ResizeObserver(() => {
      const width = Math.round(box.clientWidth);
      if (width > 0 && width !== view.width) draw();
    });
    view.observer.observe(box);
  }
  if (view.data === data && view.equity === equity && view.width === Math.round(box.clientWidth)) return;
  if (view.data !== data || view.equity !== equity) {
    view.data = data;
    view.equity = equity;
    view.actions = actions;
    view.profile = data?.freq ? equityVolume(data, equity) : null;
    view.selected = 0;
  }
  draw();
}

function draw() {
  const { box, data, profile } = view;
  const width = Math.round(box.clientWidth);
  if (!width) return;
  view.width = width;
  box.replaceChildren();
  if (!profile?.reach) {
    box.appendChild(el("div", "side-empty", "No strategy at this node."));
    return;
  }
  const actions = view.actions.filter(action => profile.sizes[action.k] > 0).slice().reverse();
  const pot = data.node.pot.reduce((sum, value) => sum + value, 0);
  const share = volume => profile.volume > 0 ? volume / profile.volume : 0;
  const head = el("div", "cats-head");
  head.append(el("span", `badge ${data.actor.toLowerCase()}`, data.actor), el("strong", "", "Bet / raise volume"));
  const stats = el("div", "volume-stats");
  for (const [label, value] of [["Avg. volume (% pot)", fmtEvPct(profile.average, pot, 1)],
    ["Bet / raise frequency", `${fmt(profile.frequency * 100, 1)}%`],
    ["Volume-weighted equity", profile.equity == null ? "—" : `${fmt(profile.equity * 100, 1)}%`]]) {
    const stat = el("div");
    stat.append(el("b", "num", value), el("span", "", label));
    stats.appendChild(stat);
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const height = Math.round(Math.min(340, Math.max(245, width * 0.5)));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "gr-chart volume-chart");
  svg.setAttribute("role", "slider");
  svg.setAttribute("aria-label", "Bet and raise volume by equity band");
  svg.setAttribute("aria-valuemin", "0");
  svg.setAttribute("aria-valuemax", "9");
  svg.setAttribute("tabindex", "0");
  const detail = el("div", "volume-detail");
  box.append(head, stats, svg, detail);

  const left = 40, right = width - 12, top = 28, bottom = height - 42;
  const barWidth = (right - left) / profile.bins.length;
  const maximum = Math.max(0.1, Math.ceil(Math.max(...profile.bins.map(bin => share(bin.volume))) * 10) / 10);
  const y = value => bottom - value / maximum * (bottom - top);
  const grid = svgEl("g", { class: "grid" }, svg);
  const axis = svgEl("g", { class: "axis" }, svg);
  svgEl("text", { x: left, y: 12, class: "axis-title" }, axis, "Share of bet / raise volume");
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    const value = fraction * maximum;
    svgEl("line", { x1: left, x2: right, y1: y(value), y2: y(value) }, grid);
    svgEl("text", { x: left - 8, y: y(value) + 4, "text-anchor": "end" }, axis, `${fmt(value * 100, maximum < 0.4 ? 1 : 0)}%`);
  }
  for (let i = 0; i <= 10; i += right - left < 300 ? 5 : 2) {
    svgEl("text", { x: left + i * barWidth, y: bottom + 17, "text-anchor": i === 0 ? "start" : i === 10 ? "end" : "middle" }, axis, `${i * 10}%`);
  }
  svgEl("text", { x: (left + right) / 2, y: height - 2, "text-anchor": "middle", class: "axis-title" }, axis, "Equity");
  const highlight = svgEl("rect", { y: top, width: barWidth, height: bottom - top, class: "volume-selection" }, svg);
  for (const [i, bin] of profile.bins.entries()) {
    const group = svgEl("g", { class: "volume-bin", "data-bin": i, "data-share": share(bin.volume) }, svg);
    let stacked = 0;
    for (const action of actions) {
      const amount = share(bin.actions[action.k]);
      svgEl("rect", { x: left + i * barWidth + 2, y: y(stacked + amount), width: barWidth - 4,
        height: y(stacked) - y(stacked + amount), fill: action.color, "data-action": action.k }, group);
      stacked += amount;
    }
  }
  const hit = svgEl("rect", { x: left, y: top, width: right - left, height: bottom - top, class: "gr-hit" }, svg);
  function select(index) {
    view.selected = Math.max(0, Math.min(profile.bins.length - 1, index));
    const bin = profile.bins[view.selected];
    const band = `${Math.round(bin.lo * 100)}–${Math.round(bin.hi * 100)}% equity`;
    const percentage = `${fmt(share(bin.volume) * 100, 1)}%`;
    svg.setAttribute("aria-valuenow", String(view.selected));
    svg.setAttribute("aria-valuetext", `${band}, ${percentage} of volume`);
    highlight.setAttribute("x", left + view.selected * barWidth);
    const heading = el("div", "volume-band");
    heading.append(el("strong", "", band), el("span", "num", `${percentage} of volume`));
    const table = el("table", "act-table");
    const header = el("tr");
    header.append(el("th", "", "Action"), el("th", "", "Volume share"));
    const thead = el("thead");
    thead.appendChild(header);
    const body = el("tbody");
    for (const action of actions) {
      const row = el("tr"), label = el("td");
      const swatch = el("span", "sw");
      swatch.style.background = action.color;
      label.append(swatch, document.createTextNode(action.label));
      row.append(label, el("td", "num", `${fmt(share(bin.actions[action.k]) * 100, 1)}%`));
      body.appendChild(row);
    }
    table.append(thead, body);
    detail.replaceChildren(heading, table);
  }
  hit.onpointermove = event => {
    const bounds = svg.getBoundingClientRect();
    select(Math.floor(((event.clientX - bounds.left) * width / bounds.width - left) / barWidth));
  };
  hit.onpointerdown = event => { svg.focus({ preventScroll: true }); hit.onpointermove(event); };
  svg.onkeydown = event => {
    const index = { Home: 0, End: 9, ArrowLeft: view.selected - 1, ArrowRight: view.selected + 1 }[event.key];
    if (index == null) return;
    event.preventDefault();
    event.stopPropagation();
    select(index);
  };
  select(view.selected);
}
