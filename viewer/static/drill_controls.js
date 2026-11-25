import { fmt, showTip, hideTip } from "./api.js";
import { el, comboEl } from "./cards.js";

export function bb(amount, bigBlind = 100) { return `${fmt(amount / bigBlind, 2)} BB`; }

export function actionOrder(actions) {
  const rank = (action) => action.kind === "fold" ? 0 : ["check", "call"].includes(action.kind) ? 1 : 2;
  return actions.map((_, i) => i).sort((a, b) => rank(actions[a]) - rank(actions[b]) || (actions[a].amount || 0) - (actions[b].amount || 0));
}

export function actionHeader(actions, bigBlind, disabled = [], onToggle = null) {
  const header = el("div", "drill-action-header");
  header.classList.toggle("range-options", !!onToggle);
  if (onToggle) { header.setAttribute("role", "group"); header.setAttribute("aria-label", "Range options"); }
  const heading = el("div", "range-options-heading");
  heading.append(el("strong", "", onToggle ? "Range options" : "Hand"));
  if (onToggle) heading.append(el("span", "", "Entire range"));
  header.append(heading);
  const columns = el("div", "mix-actions");
  for (const index of actionOrder(actions)) {
    const action = actions[index], cell = el("div", "action-heading");
    cell.style.borderTopColor = actionColor(action);
    cell.append(el("strong", "", action.label));
    if (action.amount != null) cell.append(el("span", "num", bb(action.amount, bigBlind)));
    if (onToggle && ["bet", "raise"].includes(action.kind)) {
      const toggle = el("button", "size-toggle");
      toggle.type = "button";
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-label", `${action.label} for the entire range`);
      toggle.setAttribute("aria-checked", String(!disabled.includes(action.id)));
      toggle.onclick = () => onToggle(action.id);
      cell.append(toggle);
    }
    columns.append(cell);
  }
  header.append(columns);
  return header;
}

export function actionColor(action) {
  if (action.kind === "fold") return "#5865dc";
  if (action.kind === "check" || action.kind === "call") return "#20865b";
  const fraction = action.fraction ?? Number(action.label.match(/([\d.]+)%/)[1]) / 100;
  return `hsl(8 68% ${56 - 30 * fraction / (fraction + 0.75)}%)`;
}

export function playActions(actions, bigBlind, disabled, onAction) {
  const buttons = el("div", "play-action-buttons");
  for (const index of actionOrder(actions)) {
    const action = actions[index];
    if (disabled.includes(action.id)) continue;
    const button = el("button", "drill-play-action");
    button.type = "button";
    button.dataset.action = action.id;
    button.style.background = actionColor(action);
    button.append(el("strong", "", action.label));
    if (["bet", "raise"].includes(action.kind)) button.append(el("span", "num", bb(action.amount, bigBlind)));
    button.onclick = () => onAction(action.id);
    buttons.append(button);
  }
  return buttons;
}

export function frequencyBar(actions, frequencies, bigBlind, label) {
  const bar = el("div", "frequency-bar");
  if (!frequencies) { bar.textContent = "—"; return bar; }
  const prefix = label ? `${label} · ` : "";
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", prefix + actions.map((a, i) => `${a.label} ${fmt(frequencies[i] * 100, 1)}%`).join(", "));
  for (const i of actionOrder(actions)) {
    const action = actions[i];
    const segment = el("span", "frequency-segment", frequencies[i] >= 0.1 ? `${Math.round(frequencies[i] * 100)}%` : "");
    segment.style.width = `${frequencies[i] * 100}%`;
    segment.style.background = actionColor(action);
    const tip = el("div"), frequency = el("div", "r");
    tip.append(el("div", "t", prefix + action.label + (["bet", "raise"].includes(action.kind) ? ` · ${bb(action.amount, bigBlind)}` : "")));
    frequency.append(el("span", "", "Frequency"), el("strong", "num", `${fmt(frequencies[i] * 100, 1)}%`));
    tip.append(frequency);
    segment.onmousemove = (event) => showTip(event, tip.outerHTML);
    segment.onmouseleave = hideTip;
    bar.append(segment);
  }
  return bar;
}

export function setActionFrequency(values, index, frequency, enabled) {
  const others = enabled.filter((i) => i !== index);
  const remaining = others.reduce((sum, i) => sum + values[i], 0);
  return values.map((value, i) => i === index ? frequency : !others.includes(i) ? 0
    : (1 - frequency) * (remaining > 0 ? value / remaining : 1 / others.length));
}

export function moveMixBoundary(values, boundary, frequency, enabled) {
  let total = 0;
  const bounds = enabled.slice(0, -1).map((i) => total += values[i]);
  frequency = Math.max(0, Math.min(1, frequency));
  bounds.forEach((value, i) => {
    bounds[i] = i < boundary ? Math.min(value, frequency) : i > boundary ? Math.max(value, frequency) : frequency;
  });
  const edges = [0, ...bounds, 1], result = values.map(() => 0);
  enabled.forEach((index, i) => { result[index] = edges[i + 1] - edges[i]; });
  return result;
}

const MIX_PADDING = 0.07;

export function initialFrequencies(actions, disabled = []) {
  const enabled = actionOrder(actions).filter((i) => !disabled.includes(actions[i].id));
  const values = actions.map(() => 0);
  enabled.forEach((index, i) => {
    values[index] = (Math.round((i + 1) * 20 / enabled.length) - Math.round(i * 20 / enabled.length)) / 20;
  });
  return values;
}

export function restrictFrequencies(actions, values, disabled) {
  const available = values.map((value, i) => disabled.includes(actions[i].id) ? 0 : value);
  const total = available.reduce((sum, value) => sum + value, 0);
  return total > 0 ? available.map((value) => value / total) : initialFrequencies(actions, disabled);
}

export function rememberFrequencies(actions, remembered, values, disabled) {
  const available = remembered.reduce((sum, value, i) => sum + (disabled.includes(actions[i].id) ? 0 : value), 0);
  return remembered.map((value, i) => disabled.includes(actions[i].id) ? value : values[i] * available);
}

export function mixEditor(canonical, chosen, draft, onChange, disabled = []) {
  const order = actionOrder(canonical), actions = order.map((i) => canonical[i]);
  chosen = chosen && order.map((i) => chosen[i]);
  draft = draft && order.map((i) => draft[i]);
  const enabled = actions.map((a, i) => disabled.includes(a.id) ? -1 : i).filter((i) => i >= 0);
  let values = chosen || draft;
  const editor = el("div", "mix-editor");
  const slider = el("fieldset", "mix-slider"), segments = [], handles = [];
  slider.setAttribute("aria-label", "Action frequencies");
  slider.dataset.answered = chosen ? "yes" : "no";
  slider.dataset.unavailable = enabled.length < 2 ? "yes" : "no";
  const scale = 1 + enabled.length * MIX_PADDING;
  function visualBoundary(index) {
    return (enabled.slice(0, index + 1).reduce((sum, i) => sum + values[i], 0) + (index + 1) * MIX_PADDING) / scale;
  }
  function change(next) {
    values = next;
    draw();
    const result = Array(values.length);
    order.forEach((index, i) => { result[index] = values[i]; });
    onChange(result);
  }
  for (const i of enabled) {
    const segment = el("span", "mix-segment");
    segment.style.background = actionColor(actions[i]);
    segment.dataset.index = i;
    slider.append(segment);
    segments.push(segment);
  }
  for (let i = 0; i < enabled.length - 1; i++) {
    const handle = el("button", "mix-handle");
    handle.type = "button";
    handle.setAttribute("role", "slider");
    handle.setAttribute("aria-label", `${actions[enabled[i]].label} / ${actions[enabled[i + 1]].label} divider`);
    handle.setAttribute("aria-valuemin", "0");
    handle.setAttribute("aria-valuemax", "100");
    handle.onkeydown = (event) => {
      const delta = { ArrowLeft: -0.05, ArrowDown: -0.05, ArrowRight: 0.05, ArrowUp: 0.05 }[event.key];
      if (delta === undefined && event.key !== "Home" && event.key !== "End") return;
      event.preventDefault();
      const current = enabled.slice(0, i + 1).reduce((sum, index) => sum + values[index], 0);
      const target = event.key === "Home" ? 0 : event.key === "End" ? 1 : Math.round((current + delta) * 20) / 20;
      change(moveMixBoundary(values, i, target, enabled));
    };
    slider.append(handle);
    handles.push(handle);
  }
  let drag = null, lastTap = null;
  slider.onpointerdown = (event) => {
    if (event.button !== 0 || slider.disabled || chosen) return;
    event.preventDefault();
    const rect = slider.getBoundingClientRect(), position = (event.clientX - rect.left) / rect.width;
    const boundary = handles.reduce((nearest, _, i) =>
      Math.abs(visualBoundary(i) - position) < Math.abs(visualBoundary(nearest) - position) ? i : nearest, 0);
    drag = { boundary, rect, start: event.clientX, moved: false, index: event.target.dataset.index };
    handles[boundary].focus({ preventScroll: true });
    slider.setPointerCapture(event.pointerId);
  };
  slider.onpointermove = (event) => {
    if (!drag || slider.disabled || chosen) return;
    if (!drag.moved && Math.abs(event.clientX - drag.start) < 6) return;
    drag.moved = true;
    let frequency = (event.clientX - drag.rect.left) / drag.rect.width * scale - (drag.boundary + 1) * MIX_PADDING;
    const snapped = Math.round(frequency * 20) / 20;
    if (Math.abs(frequency - snapped) < 0.012) frequency = snapped;
    change(moveMixBoundary(values, drag.boundary, frequency, enabled));
  };
  slider.onpointerup = (event) => {
    if (!drag) return;
    if (!drag.moved && !slider.disabled && !chosen && drag.index !== undefined) {
      const now = Date.now(), index = Number(drag.index);
      if (lastTap && lastTap.index === index && now - lastTap.time < 350) {
        change(setActionFrequency(values, index, 1, enabled));
        lastTap = null;
      } else {
        lastTap = { index, time: now };
        change(values);
      }
    } else lastTap = null;
    drag = null;
    slider.releasePointerCapture(event.pointerId);
  };
  slider.onlostpointercapture = slider.onpointercancel = () => { drag = null; };
  slider.oncontextmenu = (event) => {
    if (slider.disabled || chosen || event.target.dataset.index === undefined) return;
    event.preventDefault();
    drag = lastTap = null;
    change(setActionFrequency(values, Number(event.target.dataset.index), 0, enabled));
  };
  function draw() {
    segments.forEach((segment, index) => {
      const frequency = values[enabled[index]], width = (frequency + MIX_PADDING) / scale;
      segment.style.width = `${width * 100}%`;
      segment.classList.toggle("zero", frequency < 0.001);
      segment.textContent = width > 0.08 ? `${Math.round(frequency * 100)}%` : "";
    });
    handles.forEach((handle, index) => {
      const cumulative = enabled.slice(0, index + 1).reduce((sum, i) => sum + values[i], 0);
      handle.style.left = `${visualBoundary(index) * 100}%`;
      handle.setAttribute("aria-valuenow", String(Math.round(cumulative * 100)));
    });
  }
  editor.append(slider);
  draw();
  editor.querySelectorAll("button").forEach((button) => { button.dataset.answered = chosen ? "yes" : "no"; });
  return editor;
}

export function feedbackCards(feedback, bigBlind, onBrowse) {
  const result = document.createDocumentFragment();
  const legend = el("div", "feedback-legend");
  for (const index of actionOrder(feedback.hands[0].actions)) {
    const action = feedback.hands[0].actions[index];
    if (feedback.merged?.includes(action.id)) continue;
    const label = el("span");
    const dot = el("i");
    dot.style.background = actionColor(action);
    label.append(dot, document.createTextNode(action.label + (action.amount != null ? ` · ${bb(action.amount, bigBlind)}` : "")));
    legend.append(label);
  }
  result.append(legend);
  if (feedback.simplification) {
    const simplification = feedback.simplification;
    const comparison = el("div", "card feedback-ranges feedback-simplification");
    comparison.append(el("h3", "", "Strategy Simplification"));
    for (const [label, frequencies] of [["Original range", simplification.frequencies], ["Simplified range", feedback.frequencies.range]]) {
      const row = el("div", "feedback-bar-row");
      row.append(el("span", "", label), frequencyBar(feedback.hands[0].actions, frequencies, bigBlind, `Solver · ${label}`));
      comparison.append(row);
    }
    const stats = el("div", "simplification-stats");
    if (feedback.removed_frequency != null) stats.append(el("span", "", `Removed sizes used ${fmt(feedback.removed_frequency * 100, 1)}%`));
    const cost = simplification.cost.map((value) => fmt(100 * value / feedback.hands[0].pot, 2));
    stats.append(el("strong", "num", `EV cost ${cost[0] === cost[1] ? cost[0] : cost.join("–")}% pot`));
    comparison.append(stats);
    result.append(comparison);
  }
  const ranges = el("div", "card feedback-ranges");
  for (const [key, label] of [["range", "Full range · Solver"], ["dealt_solver", "Dealt hands · Solver"], ["dealt_user", "Dealt hands · You"]]) {
    if (key === "range" && feedback.simplification) continue;
    const row = el("div", "feedback-bar-row");
    row.append(el("span", "", label), frequencyBar(feedback.hands[0].actions, feedback.frequencies[key], bigBlind, label));
    ranges.append(row);
  }
  result.append(ranges);
  for (const hand of feedback.hands) {
    const card = el("div", `card feedback-hand ${hand.grade}`);
    const header = el("div", "feedback-hand-head");
    const symbol = hand.grade === "correct" ? "✓" : hand.grade === "frequency" ? "≈" : "↓";
    header.append(comboEl(hand.combo, ""), el("span", `feedback-loss num ${hand.grade}`,
      `${symbol} ${fmt(hand.loss_pct, 2)}% EV loss`));
    if (onBrowse) {
      const browse = el("button", "icon-btn feedback-browse");
      browse.type = "button";
      browse.setAttribute("aria-label", `Browse ${hand.combo}`);
      browse.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
      browse.onclick = () => onBrowse(hand.combo);
      header.append(browse);
    }
    card.append(header);
    for (const [label, frequencies] of [["Solver", hand.actions.map((a) => a.frequency)], ["You", hand.chosen]]) {
      const row = el("div", "feedback-bar-row");
      row.append(el("span", "", label), frequencyBar(hand.actions, frequencies, bigBlind, label));
      card.append(row);
    }
    result.append(card);
  }
  return result;
}

export function chipStack(amount) {
  const stack = el("span", "chip-stack");
  stack.setAttribute("aria-hidden", "true");
  const count = Math.max(2, Math.min(6, Math.ceil(Math.log10(amount + 1)) + 1));
  for (let i = 0; i < count; i++) {
    const chip = el("i");
    chip.style.bottom = `${i * 4}px`;
    stack.append(chip);
  }
  return stack;
}
