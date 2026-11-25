import { post, fmt } from "./api.js";
import { el, parseCards, cardEl } from "./cards.js";
import { actionOrder, actionColor, frequencyBar, bb } from "./drill_controls.js";

const signed = (value, digits = 1) => `${value > 0 ? "+" : ""}${fmt(value, digits)}`;

export function openAnalysis(container) {
  const filters = el("div", "analysis-filters"), result = el("div", "analysis-result");
  const sessionMenu = el("details", "analysis-selection analysis-sessions"), sessionTitle = el("summary", "", "Sessions");
  sessionMenu.open = true;
  const sessionList = el("div", "analysis-options");
  sessionMenu.append(sessionTitle, sessionList);
  const tree = el("select"), seat = el("select"), baseline = el("select"), metric = el("select");
  for (const [select, values] of [[seat, [["both", "Both positions"], ["OOP", "OOP"], ["IP", "IP"]]],
    [baseline, [["played", "Played strategy"], ["original", "Original strategy"]]],
    [metric, [["gap", "Frequency gap"], ["volume", "Betting volume"]]]]) {
    for (const [value, label] of values) { const option = el("option", "", label); option.value = value; select.append(option); }
  }
  tree.setAttribute("aria-label", "Matching game tree");
  seat.setAttribute("aria-label", "Position"); baseline.setAttribute("aria-label", "Solver baseline"); metric.setAttribute("aria-label", "Tree metric");
  const simMenu = el("details", "analysis-selection"), simTitle = el("summary", "", "Simulations"), simList = el("div", "analysis-options");
  simMenu.append(simTitle, simList);
  filters.append(sessionMenu, tree, simMenu, seat, baseline, metric);
  container.replaceChildren(filters, result);
  let sessions = new Set(), simulations = null, selectedNode = null, request = 0, timer, latest, offset = 0;
  const expanded = new Set();
  let expandedInitialized = false;

  const schedule = () => { request++; clearTimeout(timer); timer = setTimeout(load, 150); };
  const choice = (parent, label, checked, change) => {
    const row = el("label", "analysis-option"), input = el("input"); input.type = "checkbox"; input.checked = checked;
    input.onchange = () => change(input.checked); row.append(input, label); parent.append(row); return input;
  };
  async function loadSessions() {
    try {
      const data = await post("/api/drill/sessions", { offset });
      sessionList.querySelector("button")?.remove();
      for (const session of data.sessions) {
        if (offset === 0) sessions.add(session.id);
        const input = choice(sessionList, el("span", "", `${new Date(session.started * 1000).toLocaleString()} · ${session.settings.spot || "Current tree"} · ${session.hands} hands`), sessions.has(session.id), checked => {
          checked ? sessions.add(session.id) : sessions.delete(session.id);
          sessionList.querySelectorAll("input").forEach(input => { input.disabled = sessions.size >= 100 && !input.checked; });
          simulations = null; schedule();
        });
        input.disabled = sessions.size >= 100 && !input.checked;
      }
      if (data.sessions.length === 50) {
        const more = el("button", "btn small", "More sessions"); more.onclick = () => { offset += 50; loadSessions(); }; sessionList.append(more);
      }
      await load();
    } catch (error) { result.textContent = error.message; }
  }
  tree.onchange = () => { simulations = null; selectedNode = null; expanded.clear(); expandedInitialized = false; load(); };
  seat.onchange = baseline.onchange = schedule;
  metric.onchange = () => latest && render(latest);

  async function load() {
    const version = ++request;
    result.setAttribute("aria-busy", "true");
    try {
      const body = { sessions: [...sessions], seat: seat.value, baseline: baseline.value };
      if (tree.value) body.tree = tree.value;
      if (simulations) body.simulations = [...simulations];
      if (selectedNode) body.node = selectedNode;
      let data = await post("/api/drill/analysis", body);
      if (!data.tree && data.groups.length && body.tree) {
        delete body.tree; delete body.simulations; delete body.node;
        data = await post("/api/drill/analysis", body);
      }
      if (version !== request || !filters.isConnected) return;
      const previous = tree.value;
      tree.replaceChildren();
      for (const group of data.groups) {
        const option = el("option", "", `${group.name} · ${["Preflop", "", "", "Flop", "Turn", "River"][group.street]} · ${bb(group.pot, group.big_blind)} · ${bb(group.stack, group.big_blind)} deep · ${group.simulations.length} sims`);
        option.value = group.id; tree.append(option);
      }
      tree.value = data.tree || "";
      if (previous !== tree.value) { simulations = null; expanded.clear(); expandedInitialized = false; }
      sessionTitle.textContent = `Sessions · ${sessions.size} selected`;
      simList.replaceChildren();
      const group = data.groups.find(group => group.id === data.tree);
      if (group) {
        if (!simulations) simulations = new Set(group.simulations.map(sim => sim.id));
        choice(simList, el("strong", "", "All simulations"), simulations.size === group.simulations.length, checked => {
          simulations = new Set(checked ? group.simulations.map(sim => sim.id) : []); schedule();
        });
        for (const sim of group.simulations) {
          const label = el("span", "analysis-board"); label.append(...parseCards(sim.board).map(card => cardEl(card, "xs")), el("span", "muted", `${sim.decisions} nodes`));
          choice(simList, label, simulations.has(sim.id), checked => { checked ? simulations.add(sim.id) : simulations.delete(sim.id); schedule(); });
        }
      }
      simTitle.textContent = `Simulations · ${simulations?.size || 0}`;
      selectedNode = data.detail?.id || null;
      latest = data;
      render(data);
    } catch (error) { if (version === request) result.textContent = error.message; }
    finally { if (version === request) result.removeAttribute("aria-busy"); }
  }

  function render(data) {
    result.replaceChildren();
    if (!data.nodes.length) { result.append(el("p", "muted", "No submitted decisions in this selection")); return; }
    result.append(el("div", "analysis-summary muted", `${data.sessions} sessions · ${data.simulations} simulations · ${data.hands} hands · ${data.decisions} nodes`));
    const layout = el("div", "analysis-layout"), graph = el("div", "analysis-tree"), detail = el("div", "analysis-detail");
    const byId = new Map(data.nodes.map(node => [node.id, { ...node }])), children = new Map();
    for (const node of byId.values()) {
      if (!node.chance) continue;
      const previous = byId.get(node.parent);
      if (previous && !previous.stats) {
        node.incoming = previous.incoming;
        node.parent = previous.parent;
        byId.delete(previous.id);
      }
    }
    for (const node of byId.values()) { if (!children.has(node.parent)) children.set(node.parent, []); children.get(node.parent).push(node); }
    const observed = new Set();
    for (const node of [...byId.values()].filter(node => node.stats)) {
      for (let current = node; current; current = byId.get(current.parent)) observed.add(current.id);
    }
    if (!expandedInitialized) { for (const id of observed) expanded.add(id); expandedInitialized = true; }
    for (let current = byId.get(selectedNode); current?.parent; current = byId.get(current.parent)) expanded.add(current.parent);
    function branch(node) {
      const wrap = el("div", "analysis-branch"), row = el("div", "analysis-node-row");
      const descendants = (children.get(node.id) || []).sort((a, b) => (a.incoming?.amount || 0) - (b.incoming?.amount || 0));
      const toggle = el("button", "analysis-expand"), button = el("button", "analysis-node");
      const isOpen = expanded.has(node.id);
      toggle.textContent = descendants.length ? isOpen ? "−" : "+" : "·";
      toggle.disabled = !descendants.length;
      toggle.setAttribute("aria-label", `${isOpen ? "Collapse" : "Expand"} ${node.incoming?.label || node.street}`);
      toggle.setAttribute("aria-expanded", String(isOpen));
      button.classList.toggle("selected", node.id === selectedNode);
      button.append(el("span", "analysis-line", node.incoming?.label || node.street));
      if (node.stats || descendants.length) button.append(el("span", "muted", `${node.street} · ${node.seat}`));
      if (node.stats) {
        const value = metric.value === "gap" ? node.stats.gap * 100 : node.stats.volume[0] - node.stats.volume[1];
        const severity = metric.value === "gap" ? node.stats.gap / 0.25 : Math.abs(node.stats.pot_volume[0] - node.stats.pot_volume[1]) / 0.25;
        button.style.setProperty("--deviation", Math.min(1, severity));
        button.classList.add("measured");
        button.append(el("strong", "num", metric.value === "gap" ? `${fmt(value, 1)} pp` : `${signed(value, 2)} BB`));
        const bars = el("span", "analysis-mini-bars");
        bars.append(frequencyBar(node.stats.actions, node.stats.user, data.big_blind), frequencyBar(node.stats.actions, node.stats.solver, data.big_blind));
        button.append(bars);
        button.onclick = () => { selectedNode = node.id; load(); };
      } else { button.append(el("span", "muted", "—")); button.disabled = true; }
      const subtree = el("div", "analysis-branches");
      const fill = () => { subtree.replaceChildren(...descendants.map(branch)); };
      if (isOpen) fill();
      subtree.hidden = !isOpen;
      toggle.onclick = () => {
        if (subtree.hidden) { expanded.add(node.id); fill(); } else expanded.delete(node.id);
        subtree.hidden = !subtree.hidden;
        toggle.textContent = subtree.hidden ? "+" : "−";
        toggle.setAttribute("aria-expanded", String(!subtree.hidden));
      };
      row.append(toggle, button); wrap.append(row, subtree); return wrap;
    }
    graph.append(...(children.get(null) || []).map(branch));
    if (data.detail) renderDetail(detail, data.detail, byId.get(data.detail.id), data.big_blind);
    else detail.append(el("p", "muted", "No decisions for this position"));
    layout.append(graph, detail); result.append(layout);
    const selected = graph.querySelector(".analysis-node.selected");
    if (selected) graph.scrollTop += selected.getBoundingClientRect().top - graph.getBoundingClientRect().top - graph.clientHeight / 3;
  }

  function renderDetail(container, stats, node, bigBlind) {
    container.append(el("h3", "", `${node.street} · ${node.seat}`), el("div", "muted", `${stats.samples} dealt hands · ${stats.decisions} nodes`));
    const frequencies = el("div", "analysis-comparison");
    for (const [label, values] of [["You", stats.user], ["Solver", stats.solver]]) frequencies.append(el("span", "", label), frequencyBar(stats.actions, values, bigBlind));
    container.append(frequencies);
    const deltas = el("div", "analysis-deltas");
    for (const i of actionOrder(stats.actions)) {
      const row = el("div", "analysis-action-delta"), action = stats.actions[i];
      const label = el("span", "", action.label); label.style.borderLeftColor = actionColor(action);
      row.append(label, el("strong", "num", `${signed((stats.user[i] - stats.solver[i]) * 100)} pp`));
      deltas.append(row);
    }
    container.append(deltas, el("h4", "", "Betting volume / decision"));
    const volume = el("div", "analysis-volume");
    const max = Math.max(...stats.volume, 0.0001);
    for (let i = 0; i < 2; i++) {
      const track = el("div", "analysis-volume-track"), fill = el("i"); fill.style.width = `${100 * stats.volume[i] / max}%`; track.append(fill);
      volume.append(el("span", "", i === 0 ? "You" : "Solver"), track, el("strong", "num", `${fmt(stats.volume[i], 2)} BB`));
    }
    container.append(volume, el("div", "analysis-volume-delta num", `${signed(stats.volume[0] - stats.volume[1], 2)} BB · ${signed((stats.pot_volume[0] - stats.pot_volume[1]) * 100)}% pot`));
    const classes = el("div", "analysis-classes");
    container.append(el("h4", "", "Dealt hand classes"));
    for (const group of stats.classes.sort((a, b) => b.gap - a.gap)) {
      const row = el("div", "analysis-class"), bars = el("div", "analysis-class-bars");
      bars.append(frequencyBar(group.actions, group.user, bigBlind), frequencyBar(group.actions, group.solver, bigBlind));
      row.append(el("strong", "", group.name), bars, el("span", "num", `${fmt(group.gap * 100, 1)} pp`));
      classes.append(row);
    }
    container.append(classes);
  }
  loadSessions();
}
