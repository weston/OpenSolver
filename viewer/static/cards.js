// Cards, hand classes and the solver's 1326 hand order. Shared by every view.

export const RANKS = "AKQJT98765432";      // matrix order: high to low
export const SUITS = "shdc";               // display order
export const GLYPH = { s: "♠", h: "♥", d: "♦", c: "♣" };

export const H = {
  order: [],        // combo strings in solver order, e.g. "2d2c"
  index: new Map(), // combo string (either card order) -> index
  byClass: {},      // "AKs" -> [indices]
  classes: [],      // 169 class names in matrix order (row-major)
};

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function classAt(row, col) {
  if (row === col) return RANKS[row] + RANKS[col];
  return row < col ? RANKS[row] + RANKS[col] + "s" : RANKS[col] + RANKS[row] + "o";
}

export function comboClass(c) {
  const a = RANKS.indexOf(c[0]), b = RANKS.indexOf(c[2]);
  if (a === b) return c[0] + c[2];
  const hi = a < b ? c[0] : c[2], lo = a < b ? c[2] : c[0];
  return hi + lo + (c[1] === c[3] ? "s" : "o");
}

export function initHands(order) {
  H.order = order;
  H.index = new Map();
  H.byClass = {};
  order.forEach((c, i) => {
    H.index.set(c, i);
    H.index.set(c.slice(2) + c.slice(0, 2), i);
    (H.byClass[comboClass(c)] ||= []).push(i);
  });
  H.classes = [];
  for (let r = 0; r < 13; r++) for (let c = 0; c < 13; c++) H.classes.push(classAt(r, c));
  // Display combos within a class in a stable, readable order: by suits of the first then second card.
  const so = (ch) => SUITS.indexOf(ch);
  for (const k in H.byClass) {
    H.byClass[k].sort((x, y) => {
      const a = displayCombo(order[x]), b = displayCombo(order[y]);
      return so(a[1]) - so(b[1]) || so(a[3]) - so(b[3]);
    });
  }
}

/** "2d2c" -> "2d2c" with the higher rank first (and for pairs, the earlier suit in SUITS first). */
export function displayCombo(c) {
  const a = RANKS.indexOf(c[0]), b = RANKS.indexOf(c[2]);
  if (a < b || (a === b && SUITS.indexOf(c[1]) <= SUITS.indexOf(c[3]))) return c;
  return c.slice(2) + c.slice(0, 2);
}

export function parseCards(s) {
  const t = (s || "").replace(/[\s,]/g, "");
  const out = [];
  for (let i = 0; i + 1 < t.length; i += 2) out.push(t[i].toUpperCase() + t[i + 1].toLowerCase());
  return out;
}

export function blockedBy(board) {
  const set = new Set(board);
  return (combo) => set.has(combo.slice(0, 2)) || set.has(combo.slice(2, 4));
}

/** A playing card graphic. size: "lg" | "" | "sm" | "xs" */
export function cardEl(card, size = "") {
  const e = el("span", `pc s-${card[1]} ${size}`.trim());
  e.appendChild(el("span", "r", card[0] === "T" ? "10" : card[0]));
  e.appendChild(el("span", "s", GLYPH[card[1]]));
  e.title = card;
  return e;
}

export function comboEl(combo, size = "xs") {
  const d = displayCombo(combo);
  const w = el("span", "combo-cards");
  w.appendChild(cardEl(d.slice(0, 2), size));
  w.appendChild(cardEl(d.slice(2, 4), size));
  return w;
}

export function suitColor(s) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--suit-${s}`).trim();
}
