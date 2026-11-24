import { fmt, fmtChips } from "./api.js";
import { el, comboEl } from "./cards.js";

export function actionColor(action, index) {
  if (action.kind === "fold") return "#5865dc";
  if (action.kind === "check" || action.kind === "call") return "#20865b";
  return `hsl(${8 + index * 5} 68% ${43 + Math.min(index, 4) * 5}%)`;
}

export function frequencyBar(actions, frequencies) {
  const bar = el("div", "frequency-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", actions.map((a, i) => `${a.label} ${fmt(frequencies[i] * 100, 1)}%`).join(", "));
  for (const [i, action] of actions.entries()) {
    const segment = el("span", "frequency-segment", frequencies[i] >= 0.1 ? `${Math.round(frequencies[i] * 100)}%` : "");
    segment.style.width = `${frequencies[i] * 100}%`;
    segment.style.background = actionColor(action, i);
    bar.append(segment);
  }
  return bar;
}

export function mixEditor(actions, chosen, draft, onChange, submit) {
  let values = chosen || draft || actions.map((_, i) => (Math.round((i + 1) * 20 / actions.length) - Math.round(i * 20 / actions.length)) / 20);
  const editor = el("div", "mix-editor");
  const buttons = el("div", "mix-actions");
  const labels = [];
  for (const [i, action] of actions.entries()) {
    const button = el("button", `btn drill-action ${action.kind}`);
    button.style.borderTopColor = actionColor(action, i);
    button.append(el("strong", "", action.label));
    if (action.amount != null) button.append(el("span", "num", fmtChips(action.amount)));
    const label = el("span", "mix-percent num");
    labels.push(label);
    button.append(label);
    button.onclick = () => submit(actions.map((_, j) => Number(i === j)));
    buttons.append(button);
  }
  const mixing = el("div", "mix-controls");
  const slider = el("div", "mix-slider");
  let bar = frequencyBar(actions, values);
  slider.append(bar);
  const handles = actions.slice(1).map((_, i) => {
    const handle = el("button", "mix-handle");
    handle.setAttribute("role", "slider");
    handle.setAttribute("aria-label", `${actions[i].label} / ${actions[i + 1].label}`);
    function move(percent) {
      const before = values.slice(0, i).reduce((sum, f) => sum + f, 0);
      const end = before + values[i] + values[i + 1];
      const boundary = Math.max(before, Math.min(end, Math.round(percent / 5) / 20));
      values = values.map((v, j) => j === i ? boundary - before : j === i + 1 ? end - boundary : v);
      onChange(values);
      draw();
    }
    handle.onpointerdown = (event) => { event.preventDefault(); handle.setPointerCapture(event.pointerId); };
    handle.onpointermove = (event) => {
      if (!handle.hasPointerCapture(event.pointerId)) return;
      const bounds = slider.getBoundingClientRect();
      move((event.clientX - bounds.left) / bounds.width * 100);
    };
    handle.onpointerup = (event) => { if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId); };
    handle.onkeydown = (event) => {
      const boundary = values.slice(0, i + 1).reduce((sum, f) => sum + f, 0) * 100;
      const next = { ArrowLeft: boundary - 5, ArrowRight: boundary + 5, Home: 0, End: 100 }[event.key];
      if (next != null) { event.preventDefault(); move(next); }
    };
    slider.append(handle);
    return handle;
  });
  function draw() {
    const next = frequencyBar(actions, values);
    bar.replaceWith(next);
    bar = next;
    labels.forEach((label, i) => {
      label.textContent = `${Math.round(values[i] * 100)}%`;
      buttons.children[i].classList.toggle("chosen", !!chosen && values[i] > 0);
    });
    handles.forEach((handle, i) => {
      const before = values.slice(0, i).reduce((sum, f) => sum + f, 0) * 100;
      const boundary = before + values[i] * 100;
      handle.style.left = `${boundary}%`;
      handle.setAttribute("aria-valuemin", Math.round(before));
      handle.setAttribute("aria-valuemax", Math.round(boundary + values[i + 1] * 100));
      handle.setAttribute("aria-valuenow", Math.round(boundary));
      handle.setAttribute("aria-valuetext", `${Math.round(values[i] * 100)}% / ${Math.round(values[i + 1] * 100)}%`);
    });
  }
  const useMix = el("button", "btn small", "Use mix");
  useMix.onclick = () => submit(values);
  mixing.append(slider, useMix);
  editor.append(buttons, mixing);
  draw();
  editor.querySelectorAll("button").forEach((button) => { button.dataset.answered = chosen ? "yes" : "no"; });
  return editor;
}

export function feedbackCards(feedback, continuation) {
  const result = document.createDocumentFragment();
  const legend = el("div", "feedback-legend");
  for (const [i, action] of feedback.hands[0].actions.entries()) {
    const label = el("span");
    const dot = el("i");
    dot.style.background = actionColor(action, i);
    label.append(dot, document.createTextNode(action.label));
    legend.append(label);
  }
  result.append(legend);
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
