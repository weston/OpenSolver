import { el, RANKS } from "./cards.js";

export function defaultBoardFilters() {
  return { high: RANKS, middle: RANKS, low: RANKS, pairing: "both", suits: ["rainbow", "two-tone", "monotone"] };
}

export function renderBoardFilters(host, filters, onChange) {
  host.replaceChildren();
  function toggle(label, active, click) {
    const button = el("button", "btn small", label);
    button.type = "button"; button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active)); button.onclick = click;
    return button;
  }
  for (const [key, label] of [["high", "High card"], ["middle", "Middle card"], ["low", "Low card"]]) {
    const field = el("div", "board-filter"), ranks = el("div", "rank-options");
    field.setAttribute("role", "group"); field.setAttribute("aria-label", label);
    field.append(el("span", "", label));
    for (const rank of RANKS) ranks.append(toggle(rank, filters[key].includes(rank), () => {
      onChange({ ...filters, [key]: filters[key].includes(rank) ? filters[key].replace(rank, "") : filters[key] + rank });
    }));
    const all = toggle(filters[key].length === 13 ? "Clear" : "All", false, () => {
      onChange({ ...filters, [key]: filters[key].length === 13 ? "" : RANKS });
    });
    field.append(ranks, all); host.append(field);
  }
  for (const [key, label, values] of [["pairing", "Pairing", [["both", "Both"], ["unpaired", "Unpaired"], ["paired", "Paired"]]],
    ["suits", "Suits", [["rainbow", "Rainbow"], ["two-tone", "FD"], ["monotone", "Mono"]]]]) {
    const row = el("div", "texture-options"); row.setAttribute("role", "group"); row.setAttribute("aria-label", label);
    for (const [value, label] of values) row.append(toggle(label, key === "pairing" ? filters[key] === value : filters.suits.includes(value), () => {
      onChange({ ...filters, [key]: key === "pairing" ? value : filters.suits.includes(value) ? filters.suits.filter(s => s !== value) : [...filters.suits, value] });
    }));
    host.append(row);
  }
}
