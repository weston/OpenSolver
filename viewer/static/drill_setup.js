import { get, post } from "./api.js";
import { el, cardEl, parseCards, RANKS, SUITS } from "./cards.js";
import { currentTree } from "./build.js";
import { drillSettings } from "./settings.js";
import { actionOrder, bb } from "./drill_controls.js";

const $ = (id) => document.getElementById(id);
let selected = [], previewSequence = 0;
const filters = { high: RANKS, middle: RANKS, low: RANKS, pairing: "both", suits: ["rainbow", "two-tone", "monotone"] };

export function boardConfig() {
  return { boards: $("drill-boards").value, selected_boards: selected, board_filters: filters };
}

export async function liveConfig() {
  const spot = $("drill-spot").value;
  return { ...drillSettings(), ...boardConfig(), spot,
    tree: spot ? (await get("/api/library/get?kind=config&name=" + encodeURIComponent(spot))).content : currentTree(true) };
}

function toggleButton(label, active, click) {
  const button = el("button", "btn small", label);
  button.type = "button";
  button.classList.toggle("active", active);
  button.setAttribute("aria-pressed", active);
  button.onclick = click;
  return button;
}

export function renderBoardOptions() {
  const host = $("drill-board-options"), mode = $("drill-boards").value;
  host.replaceChildren();
  host.classList.toggle("hidden", mode === "random");
  if (mode === "selected") {
    const button = el("button", "btn small wide", `Select boards · ${selected.length}`);
    button.type = "button";
    button.onclick = openBoardPicker;
    host.append(button);
    for (const board of selected.slice(0, 4)) {
      const row = el("div", "selected-board");
      row.append(...parseCards(board).map((c) => cardEl(c, "xs")));
      host.append(row);
    }
  } else if (mode === "filtered") {
    for (const [key, label] of [["high", "High card"], ["middle", "Middle card"], ["low", "Low card"]]) {
      const field = el("div", "board-filter"), ranks = el("div", "rank-options");
      field.append(el("span", "", label));
      for (const rank of RANKS) ranks.append(toggleButton(rank, filters[key].includes(rank), () => {
        filters[key] = filters[key].includes(rank) ? filters[key].replace(rank, "") : filters[key] + rank;
        renderBoardOptions(); refreshNode();
      }));
      const all = toggleButton(filters[key].length === 13 ? "Clear" : "All", false, () => {
        filters[key] = filters[key].length === 13 ? "" : RANKS; renderBoardOptions(); refreshNode();
      });
      field.append(ranks, all); host.append(field);
    }
    for (const [key, values] of [["pairing", [["both", "Both"], ["unpaired", "Unpaired"], ["paired", "Paired"]]],
      ["suits", [["rainbow", "Rainbow"], ["two-tone", "FD"], ["monotone", "Mono"]]]]) {
      const row = el("div", "texture-options");
      for (const [value, label] of values) row.append(toggleButton(label, key === "pairing" ? filters[key] === value : filters.suits.includes(value), () => {
        if (key === "pairing") filters.pairing = value;
        else filters.suits = filters.suits.includes(value) ? filters.suits.filter((s) => s !== value) : [...filters.suits, value];
        renderBoardOptions(); refreshNode();
      }));
      host.append(row);
    }
  }
}

function openBoardPicker() {
  const dialog = $("drill-board-dialog");
  let draft = [], boards = [...selected];
  dialog.replaceChildren();
  const heading = el("div", "card-head"), close = el("button", "btn small", "Done");
  heading.append(el("h2", "", "Select boards"), close);
  const list = el("div", "board-selection-list"), slots = el("div", "board-slots"), deck = el("div", "deck");
  const add = el("button", "btn primary", "Add board"), clear = el("button", "btn small", "Clear cards");
  function draw() {
    slots.replaceChildren(...draft.map((c) => cardEl(c)));
    add.disabled = ![3, 4, 5].includes(draft.length) || (boards.length > 0 && draft.length * 2 !== boards[0].length) || boards.length >= 100;
    deck.querySelectorAll("button").forEach((b) => { b.classList.toggle("picked", draft.includes(b.dataset.card)); b.disabled = draft.length === 5 && !draft.includes(b.dataset.card); });
    list.replaceChildren();
    for (const [i, board] of boards.entries()) {
      const row = el("div", "selected-board"), remove = el("button", "btn small", "×");
      remove.setAttribute("aria-label", "Remove board");
      remove.onclick = () => { boards.splice(i, 1); draw(); };
      row.append(...parseCards(board).map((c) => cardEl(c, "sm")), remove); list.append(row);
    }
  }
  for (const suit of SUITS) for (const rank of RANKS) {
    const card = rank + suit, button = el("button");
    button.dataset.card = card;
    button.setAttribute("aria-label", card);
    button.append(cardEl(card, "sm"));
    button.onclick = () => { draft = draft.includes(card) ? draft.filter((c) => c !== card) : [...draft, card]; draw(); };
    deck.append(button);
  }
  clear.onclick = () => { draft = []; draw(); };
  add.onclick = () => { boards = [...new Set([...boards, draft.join("")])]; draft = []; draw(); };
  close.onclick = () => { selected = boards; dialog.close(); renderBoardOptions(); resetNode(); };
  dialog.append(heading, list, slots, deck, clear, add);
  draw(); dialog.showModal();
}

export function restoreSelection(config) {
  selected = config.selected_boards || selected;
  Object.assign(filters, config.board_filters || {});
  renderBoardOptions();
}

export function selectCurrentBoard() {
  selected = currentTree(true).board ? [currentTree(true).board] : [];
  $("drill-boards").value = "selected";
  renderBoardOptions();
}

export function resetNode() {
  $("drill-node").value = "r:0";
  refreshNode();
}

export async function refreshNode() {
  if ($("drill-mode").value !== "node" || $("drill-settings").disabled) return;
  const sequence = ++previewSequence, host = $("drill-node-picker");
  host.textContent = "Loading…";
  try {
    const source = $("drill-source").value;
    const settings = source === "live" ? await liveConfig() : { batch: $("drill-batch").value };
    const result = await post("/api/drill/preview", { ...settings, source, node: $("drill-node").value });
    if (sequence !== previewSequence) return;
    host.replaceChildren();
    const path = el("div", "node-path");
    for (const item of result.path) {
      const button = el("button", "btn small", `${item.actor ? item.actor + " " : ""}${item.label}`);
      button.type = "button";
      button.onclick = () => { $("drill-node").value = item.id; refreshNode(); };
      path.append(button);
    }
    const heading = el("strong", "node-position", `${result.street} · ${result.actor}`);
    const choices = el("div", "node-choices");
    for (const index of actionOrder(result.choices)) {
      const action = result.choices[index], button = el("button", "btn small", action.label + (action.amount != null ? ` · ${bb(action.amount)}` : ""));
      button.type = "button";
      button.disabled = action.terminal;
      button.onclick = () => { $("drill-node").value = action.id; refreshNode(); };
      choices.append(button);
    }
    host.append(path, heading, choices);
  } catch (e) { if (sequence === previewSequence) host.textContent = e.message; }
}
