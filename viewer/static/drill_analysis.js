import { post, fmt, showTip, hideTip } from "./api.js";
import { el, parseCards, cardEl, comboEl, categoryLabel } from "./cards.js";
import { actionOrder, actionColor, frequencyBar, bb } from "./drill_controls.js";
import { defaultBoardFilters, renderBoardFilters } from "./board_filters.js";

const signed = (value, digits = 1) => `${value > 0 ? "+" : ""}${fmt(value, digits)}`;

export function initAnalysis(container, onSelectSessions) {
  const filters = el("div", "analysis-filters"), result = el("div", "analysis-result");
  const sessionButton = el("button", "btn small", "Choose sessions"); sessionButton.onclick = onSelectSessions;
  const modes = el("div", "analysis-modes"); modes.setAttribute("role", "group"); modes.setAttribute("aria-label", "Practice mode");
  let answerMode = null;
  for (const [value, label] of [["frequencies", "Frequencies"], ["play", "Normal play"]]) {
    const button = el("button", "btn small", label); button.dataset.mode = value;
    button.onclick = () => { answerMode = value; selectedNode = null; schedule(); };
    modes.append(button);
  }
  const positions = el("div", "analysis-positions"); positions.setAttribute("role", "group"); positions.setAttribute("aria-label", "Analysis position");
  let seat = "OOP";
  for (const position of ["OOP", "IP"]) {
    const button = el("button", "btn small", position); button.setAttribute("aria-pressed", String(position === seat));
    button.onclick = () => {
      seat = position; selectedNode = null;
      for (const choice of positions.children) choice.setAttribute("aria-pressed", String(choice === button));
      schedule();
    };
    positions.append(button);
  }
  const tree = el("select"), baseline = el("select"), metric = el("select");
  for (const [select, values] of [[baseline, [["played", "Played strategy"], ["original", "Original strategy"]]],
    [metric, [["gap", "Frequencies"], ["volume", "Betting volume"]]]]) {
    for (const [value, label] of values) { const option = el("option", "", label); option.value = value; select.append(option); }
  }
  tree.setAttribute("aria-label", "Matching game tree");
  baseline.setAttribute("aria-label", "Solver baseline"); metric.setAttribute("aria-label", "Tree metric");
  const simMenu = el("details", "analysis-selection"), simTitle = el("summary", "", "Simulations"), simList = el("div", "analysis-options");
  simMenu.append(simTitle, simList);
  const textureMenu = el("details", "analysis-selection analysis-texture"), textureTitle = el("summary", "", "Flop texture");
  const textureOptions = el("div", "analysis-options"), textureFields = el("div"), resetTextures = el("button", "btn small", "Reset");
  let boardFilters = defaultBoardFilters();
  function changeTextures(next) {
    boardFilters = next; simulations = null; selectedNode = null;
    const filtered = [next.high, next.middle, next.low].some(ranks => ranks.length !== 13) || next.pairing !== "both" || next.suits.length !== 3;
    textureTitle.textContent = filtered ? "Flop texture · Filtered" : "Flop texture";
    renderBoardFilters(textureFields, boardFilters, changeTextures); schedule();
  }
  resetTextures.onclick = () => changeTextures(defaultBoardFilters());
  renderBoardFilters(textureFields, boardFilters, changeTextures);
  textureOptions.append(textureFields, resetTextures); textureMenu.append(textureTitle, textureOptions);
  filters.append(sessionButton, modes, positions, tree, simMenu, textureMenu, baseline, metric);
  container.replaceChildren(filters, result);
  let sessions = [], simulations = null, selectedNode = null, request = 0, timer, latest;
  const expanded = new Set();
  let expandedInitialized = false;

  const schedule = () => { request++; clearTimeout(timer); timer = setTimeout(load, 150); };
  const choice = (parent, label, checked, change) => {
    const row = el("label", "analysis-option"), input = el("input"); input.type = "checkbox"; input.checked = checked;
    input.onchange = () => change(input.checked); row.append(input, label); parent.append(row); return input;
  };
  function selection() {
    const body = { sessions, seat, baseline: baseline.value, board_filters: boardFilters };
    if (answerMode) body.answer_mode = answerMode;
    if (tree.value) body.tree = tree.value;
    if (simulations) body.simulations = [...simulations];
    if (selectedNode) body.node = selectedNode;
    return body;
  }
  tree.onchange = () => { simulations = null; selectedNode = null; answerMode = null; expanded.clear(); expandedInitialized = false; load(); };
  baseline.onchange = schedule;
  metric.onchange = () => latest && render(latest);

  async function load() {
    clearTimeout(timer); hideTip();
    const version = ++request;
    result.setAttribute("aria-busy", "true");
    try {
      const body = selection();
      let data = await post("/api/drill/analysis", body);
      if (!data.tree && data.groups.length && body.tree) {
        delete body.tree; delete body.simulations; delete body.node;
        data = await post("/api/drill/analysis", body);
      }
      if (version !== request || !filters.isConnected) return;
      answerMode = data.answer_mode || answerMode;
      for (const button of modes.children) {
        const count = data.answer_modes?.[button.dataset.mode] || 0;
        button.textContent = `${button.dataset.mode === "play" ? "Normal play" : "Frequencies"} · ${count}`;
        button.setAttribute("aria-pressed", String(button.dataset.mode === answerMode));
        button.disabled = count === 0;
      }
      const previous = tree.value;
      tree.replaceChildren();
      for (const group of data.groups) {
        const option = el("option", "", `${group.name} · ${["Preflop", "", "", "Flop", "Turn", "River"][group.street]} · ${bb(group.pot, group.big_blind)} · ${bb(group.stack, group.big_blind)} deep · ${group.simulations.length} sims`);
        option.value = group.id; tree.append(option);
      }
      tree.value = data.tree || "";
      if (previous !== tree.value) { simulations = null; expanded.clear(); expandedInitialized = false; }
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
    if (!data.nodes.length) { result.append(el("p", "muted", sessions.length ? "No submitted decisions in this selection" : "Choose sessions to analyze")); return; }
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
        bars.append(frequencyBar(node.stats.actions, node.stats.solver, data.big_blind, "Solver"), frequencyBar(node.stats.actions, node.stats.user, data.big_blind, "You"));
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
    else detail.append(el("p", "muted", "No decisions for this selection"));
    layout.append(graph, detail); result.append(layout);
    const selected = graph.querySelector(".analysis-node.selected");
    if (selected) graph.scrollTop += selected.getBoundingClientRect().top - graph.getBoundingClientRect().top - graph.clientHeight / 3;
  }

  function renderDetail(container, stats, node, bigBlind) {
    const selected = { ...selection(), node: stats.id }, version = request;
    const samples = answerMode === "play" ? `${stats.samples} ${stats.samples === 1 ? "action" : "actions"}` : `${stats.samples} dealt hands`;
    container.append(el("h3", "", `${node.street} · ${node.seat}`), el("div", "muted", `${samples} · ${stats.decisions} nodes`));
    const frequencies = el("div", "analysis-comparison");
    for (const [label, values] of [["Solver", stats.solver], ["You", stats.user]]) frequencies.append(el("span", "", label), frequencyBar(stats.actions, values, bigBlind, label));
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
    for (const i of [1, 0]) {
      const label = i === 1 ? "Solver" : "You", amount = `${fmt(stats.volume[i], 2)} BB`;
      const track = el("div", `analysis-volume-track ${i === 1 ? "solver" : "user"}`), fill = el("i");
      fill.style.width = `${100 * stats.volume[i] / max}%`; track.append(fill);
      track.onmousemove = event => showTip(event, el("div", "t", `${label} · ${amount}`).outerHTML);
      track.onmouseleave = hideTip;
      volume.append(el("span", "", label), track, el("strong", "num", amount));
    }
    container.append(volume, el("div", "analysis-volume-delta num", `${signed(stats.volume[0] - stats.volume[1], 2)} BB · ${signed((stats.pot_volume[0] - stats.pot_volume[1]) * 100)}% pot`));
    const categories = el("div", "analysis-categories");
    for (const [kind, label] of [["made", "Made hands"], ["draws", "Draws"]]) {
      const groups = stats.categories[kind];
      if (!groups.length || (kind === "draws" && groups.every(group => group.name === "no_draw"))) continue;
      categories.append(el("h4", "", label));
      for (const group of groups.sort((a, b) => b.gap - a.gap)) {
        const row = el("details", "analysis-category"), summary = el("summary"), bars = el("div", "analysis-category-bars");
        bars.append(frequencyBar(group.actions, group.solver, bigBlind, "Solver"), frequencyBar(group.actions, group.user, bigBlind, "You"));
        summary.append(el("span", "category-chevron", "›"), el("strong", "", categoryLabel(group.name)), bars, el("span", "num", `${fmt(group.gap * 100, 1)} pp`));
        const examples = el("div", "category-examples");
        row.append(summary, examples);
        row.ontoggle = () => {
          if (row.open && !examples.childNodes.length) loadExamples(examples, { ...selected, category: `${kind}:${group.name}` }, 0, version, bigBlind);
        };
        categories.append(row);
      }
    }
    container.append(categories);
  }

  async function loadExamples(container, selection, offset, version, bigBlind) {
    container.textContent = "Loading…";
    try {
      const data = await post("/api/drill/analysis", { ...selection, offset });
      if (version !== request || !container.isConnected) return;
      const examples = data.detail.examples;
      container.replaceChildren();
      for (const hand of examples.hands) {
        const row = el("div", "category-example"), cards = el("div", "category-example-cards"), board = el("div", "board");
        board.append(...parseCards(hand.board).map(card => cardEl(card)));
        cards.append(comboEl(hand.combo, ""), board, el("span", "num", `${fmt(hand.gap * 100, 1)} pp`));
        const bars = el("div", "analysis-comparison");
        for (const [label, frequencies] of [["Solver", hand.solver], ["You", hand.user]]) bars.append(el("span", "", label), frequencyBar(hand.actions, frequencies, bigBlind, label));
        row.append(cards, bars); container.append(row);
      }
      const pager = el("div", "session-pagination");
      pager.append(el("span", "muted", `${offset + 1}–${offset + examples.hands.length} of ${examples.total} hands`));
      if (examples.total > 25) {
        for (const [label, next, disabled] of [["Previous", offset - 25, offset === 0], ["Next", offset + 25, offset + examples.hands.length >= examples.total]]) {
          const button = el("button", "btn small", label); button.disabled = disabled;
          button.onclick = () => loadExamples(container, selection, next, version, bigBlind); pager.append(button);
        }
      }
      container.append(pager);
    } catch (error) { if (version === request && container.isConnected) container.textContent = error.message; }
  }
  return { show(selected) {
    if (JSON.stringify(sessions) !== JSON.stringify(selected)) {
      sessions = selected; simulations = null; selectedNode = null; answerMode = null; expanded.clear(); expandedInitialized = false;
    }
    sessionButton.textContent = sessions.length ? `${sessions.length} sessions · Change` : "Choose sessions";
    load();
  } };
}
