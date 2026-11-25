import { fmt } from "./api.js";
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

export function frequencyBar(actions, frequencies) {
  const bar = el("div", "frequency-bar");
  if (!frequencies) { bar.textContent = "—"; return bar; }
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", actions.map((a, i) => `${a.label} ${fmt(frequencies[i] * 100, 1)}%`).join(", "));
  for (const i of actionOrder(actions)) {
    const action = actions[i];
    const segment = el("span", "frequency-segment", frequencies[i] >= 0.1 ? `${Math.round(frequencies[i] * 100)}%` : "");
    segment.style.width = `${frequencies[i] * 100}%`;
    segment.style.background = actionColor(action);
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

export function mixEditor(canonical, chosen, draft, onChange, bigBlind, sharedHeader = false, disabled = []) {
  const order = actionOrder(canonical), actions = order.map((i) => canonical[i]);
  chosen = chosen && order.map((i) => chosen[i]);
  draft = draft && order.map((i) => draft[i]);
  const enabled = actions.map((a, i) => disabled.includes(a.id) ? -1 : i).filter((i) => i >= 0);
  let values = chosen || draft || actions.map((_, i) => {
    const position = enabled.indexOf(i);
    return position < 0 ? 0 : (Math.round((position + 1) * 20 / enabled.length) - Math.round(position * 20 / enabled.length)) / 20;
  });
  let selected = !!(chosen || draft);
  const editor = el("div", "mix-editor"), buttons = el("div", "mix-actions");
  const mixing = el("div", "mix-controls"), labels = [], actionButtons = [];
  function choose(index, frequency) {
    values = setActionFrequency(values, index, frequency, enabled);
    selected = true;
    draw();
    const result = Array(values.length);
    order.forEach((index, i) => { result[index] = values[i]; });
    onChange(result);
  }
  for (const [i, action] of actions.entries()) {
    const cell = el("div", "mix-action-cell"), button = el("button", `btn drill-action ${action.kind}`);
    button.style.borderTopColor = actionColor(action);
    button.setAttribute("aria-label", action.label);
    button.dataset.unavailable = disabled.includes(action.id) ? "yes" : "no";
    const name = sharedHeader ? action.allin ? "All-in" : action.kind[0].toUpperCase() + action.kind.slice(1) : action.label;
    button.append(el("strong", "", name));
    if (!sharedHeader && action.amount != null) button.append(el("span", "num", bb(action.amount, bigBlind)));
    button.onclick = () => choose(i, 1);
    actionButtons.push(button);
    const picker = el("button", "btn mix-frequency"), label = el("span", "mix-percent num");
    picker.setAttribute("aria-label", `${action.label} frequency`);
    picker.setAttribute("aria-haspopup", "dialog");
    picker.setAttribute("aria-expanded", "false");
    picker.dataset.unavailable = disabled.includes(action.id) || enabled.length < 2 ? "yes" : "no";
    const arrow = el("span", "", "▾"); arrow.setAttribute("aria-hidden", "true");
    picker.append(label, arrow);
    labels.push(label);
    const menu = el("div", "frequency-picker");
    menu.setAttribute("popover", "auto");
    menu.setAttribute("role", "dialog");
    menu.setAttribute("aria-label", `${action.label} frequency`);
    menu.style.borderTopColor = actionColor(action);
    menu.append(el("strong", "", action.label));
    const grid = el("div", "frequency-presets");
    for (let percent = 0; percent <= 100; percent += 5) {
      const preset = el("button", "btn small num", `${percent}%`);
      preset.onclick = () => { choose(i, percent / 100); menu.hidePopover(); picker.focus(); };
      grid.append(preset);
    }
    menu.append(grid);
    menu.addEventListener("toggle", () => {
      const open = menu.matches(":popover-open");
      picker.setAttribute("aria-expanded", String(open));
      cell.classList.toggle("picking", open);
    });
    picker.onclick = () => {
      if (menu.matches(":popover-open")) { menu.hidePopover(); return; }
      [...grid.children].forEach((preset, index) => {
        const active = Math.abs(values[i] * 100 - index * 5) < 0.01;
        preset.classList.toggle("active", active);
        preset.setAttribute("aria-pressed", String(active));
      });
      menu.showPopover();
      const anchor = picker.getBoundingClientRect(), bounds = menu.getBoundingClientRect();
      menu.style.left = `${Math.max(8, Math.min(innerWidth - bounds.width - 8, anchor.left + anchor.width / 2 - bounds.width / 2))}px`;
      menu.style.top = `${Math.max(8, Math.min(innerHeight - bounds.height - 8, anchor.bottom + 6))}px`;
    };
    cell.append(button, picker, menu);
    buttons.append(cell);
  }
  function draw() {
    mixing.replaceChildren(frequencyBar(actions, values));
    labels.forEach((label, i) => {
      label.textContent = `${Math.round(values[i] * 100)}%`;
      actionButtons[i].classList.toggle("chosen", selected && values[i] > 0);
    });
  }
  editor.append(buttons, mixing);
  draw();
  editor.querySelectorAll("button").forEach((button) => { button.dataset.answered = chosen ? "yes" : "no"; });
  return editor;
}

export function feedbackCards(feedback, continuation, bigBlind) {
  if (feedback.original) {
    const comparison = el("div", "feedback-comparison");
    for (const [label, value] of [["Original", feedback.original], ["Re-solved", { ...feedback, original: null }]]) {
      const column = el("section", "drill-feedback");
      column.append(el("h3", "", label), feedbackCards(value, continuation, bigBlind));
      comparison.append(column);
    }
    return comparison;
  }
  const result = document.createDocumentFragment();
  const legend = el("div", "feedback-legend");
  for (const index of actionOrder(feedback.hands[0].actions)) {
    const action = feedback.hands[0].actions[index];
    const label = el("span");
    const dot = el("i");
    dot.style.background = actionColor(action);
    label.append(dot, document.createTextNode(action.label + (action.amount != null ? ` · ${bb(action.amount, bigBlind)}` : "")));
    legend.append(label);
  }
  result.append(legend);
  const ranges = el("div", "card feedback-ranges");
  for (const [key, label] of [["range", "Full range · Solver"], ["dealt_solver", "Dealt hands · Solver"], ["dealt_user", "Dealt hands · You"]]) {
    const row = el("div", "feedback-bar-row");
    row.append(el("span", "", label), frequencyBar(feedback.hands[0].actions, feedback.frequencies[key]));
    ranges.append(row);
  }
  result.append(ranges);
  for (const hand of feedback.hands) {
    const card = el("div", "card feedback-hand");
    card.classList.toggle("continuing", hand.combo === continuation?.combo);
    const header = el("div", "feedback-hand-head");
    const matched = hand.loss_pct < 0.005;
    header.append(comboEl(hand.combo, ""), el("span", `feedback-loss num ${matched ? "matched" : "cost"}`,
      `${matched ? "✓" : "↓"} ${fmt(hand.loss_pct, 2)}% EV loss`));
    card.append(header);
    for (const [label, frequencies] of [["You", hand.chosen], ["Solver", hand.actions.map((a) => a.frequency)]]) {
      const row = el("div", "feedback-bar-row");
      row.append(el("span", "", label), frequencyBar(hand.actions, frequencies));
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
