// Blocker effects: what holding one combo (or one card) removes from the other player's range.
// Pure arithmetic over an /api/blockers answer, with no DOM, so viewer/test_blockers_js.mjs can check it.
//
// Holding a hand changes nothing about villain's strategy; it only rules out villain's combos that
// share one of its cards. Every number here is therefore villain's range (reach at the node) with
// those combos taken out, set against the whole range:
//   - share of each hand class and each made-hand / draw category,
//   - villain's range equity against the actor's range (calc_eq_node per villain combo, weighted by
//     its matchups as Pio's range equity is),
//   - villain's frequency of each action after each actor action (villain's reach is the same at
//     the child: only the actor acted).

import { comboClass, parseCards } from "./cards.js";

const RANK_CH = "23456789TJQKA";

/** The 52 cards, aces first, spades first within a rank. */
export const DECK = [];
for (let r = RANK_CH.length - 1; r >= 0; r--) for (const s of "shdc") DECK.push(RANK_CH[r] + s);

/** Combo "AsKd" -> ["As", "Kd"]. */
export const cardsOf = (combo) => [combo.slice(0, 2), combo.slice(2, 4)];

/**
 * Precompute what the summaries need from one /api/blockers answer and the solver's hand order.
 * Combos that touch the board are left out everywhere (their reach is zero anyway).
 */
export function prepare(data, order) {
  const board = new Set(parseCards(data.board));
  const live = [];
  for (let i = 0; i < order.length; i++) {
    const [a, b] = cardsOf(order[i]);
    if (!board.has(a) && !board.has(b)) live.push(i);
  }
  const responses = (data.responses || []).filter((r) => r.freq && r.freq.length);
  return { data, order, board, live, responses, cls: order.map(comboClass), cards: order.map(cardsOf) };
}

/**
 * Villain's range at the node restricted to the combos `keep(i)` accepts.
 * Returns mass (weighted combos), average equity (matchup-weighted, over combos with an equity),
 * per-class and per-category masses, and per response the action frequencies.
 */
export function summarize(P, keep = () => true) {
  const { data } = P;
  const w = data.villain_reach, eq = data.villain_eq, mu = data.villain_matchups;
  let mass = 0, eqNum = 0, eqDen = 0;
  const classes = new Map();
  const made = new Map(), draw = new Map();
  const resp = P.responses.map((r) => ({ id: r.id, num: r.freq.map(() => 0), den: 0 }));
  for (const i of P.live) {
    const r = w[i] || 0;
    if (r <= 0 || !keep(i)) continue;
    mass += r;
    // matchup-weighted, like Pio's range equity (reach-weighted if the answer has no matchups)
    const m = mu ? mu[i] || 0 : r;
    if (eq[i] != null && m > 0) { eqNum += m * eq[i]; eqDen += m; }
    classes.set(P.cls[i], (classes.get(P.cls[i]) || 0) + r);
    if (data.cats) {
      const m = data.cats.made[i], d = data.cats.draw[i];
      if (m >= 0) made.set(m, (made.get(m) || 0) + r);
      if (d >= 0) draw.set(d, (draw.get(d) || 0) + r);
    }
    P.responses.forEach((x, k) => {
      const acc = resp[k];
      acc.den += r;
      x.freq.forEach((row, a) => { acc.num[a] += r * (row[i] || 0); });
    });
  }
  return {
    mass,
    eq: eqDen > 0 ? eqNum / eqDen : null,
    classes, made, draw,
    responses: resp.map((x) => ({ id: x.id, freq: x.num.map((v) => (x.den > 0 ? v / x.den : 0)) })),
  };
}

/** Does villain combo i share a card with any of `cards`? */
const touches = (P, i, cards) => cards.includes(P.cards[i][0]) || cards.includes(P.cards[i][1]);

/**
 * The full report for the actor holding the combo at index `hero` (solver order): villain's range
 * before and after, the combos removed (largest first), and the actor's own reach and equity.
 */
export function comboReport(P, hero) {
  const held = P.cards[hero];
  const before = summarize(P);
  const after = summarize(P, (i) => !touches(P, i, held));
  const removed = [];
  for (const i of P.live) {
    const r = P.data.villain_reach[i] || 0;
    if (r > 0 && touches(P, i, held)) removed.push({ i, combo: P.order[i], cls: P.cls[i], w: r });
  }
  removed.sort((a, b) => b.w - a.w || a.i - b.i);
  return {
    held, before, after, removed,
    heroReach: P.data.hero_reach[hero] || 0,
    heroEq: P.data.hero_eq[hero],
    blockedByBoard: held.some((c) => P.board.has(c)),
  };
}

/**
 * Shares before and after for every class / category in either summary, largest change first.
 * Rows: {key, before, after, delta, massBefore, massAfter}; shares are fractions of each range's mass.
 */
export function shareChanges(before, after, pick) {
  const a = pick(before), b = pick(after);
  const keys = new Set([...a.keys(), ...b.keys()]);
  const rows = [];
  for (const k of keys) {
    const mb = a.get(k) || 0, ma = b.get(k) || 0;
    const sb = before.mass > 0 ? mb / before.mass : 0, sa = after.mass > 0 ? ma / after.mass : 0;
    rows.push({ key: k, before: sb, after: sa, delta: sa - sb, massBefore: mb, massAfter: ma });
  }
  rows.sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta) || (y.massBefore - x.massBefore));
  return rows;
}

/**
 * The effect of holding each single card not on the board: villain's mass removed, and the
 * change in villain's equity and in each response frequency. `metric(summary)` picks the number
 * to rank by (e.g. villain's fold frequency after a bet); rows come back largest |change| first.
 */
export function cardEffects(P, metric) {
  const before = summarize(P);
  const m0 = metric(before);
  const rows = [];
  for (const card of DECK) {
    if (P.board.has(card)) continue;
    const after = summarize(P, (i) => P.cards[i][0] !== card && P.cards[i][1] !== card);
    const m1 = metric(after);
    rows.push({
      card,
      removed: before.mass - after.mass,
      removedPct: before.mass > 0 ? (before.mass - after.mass) / before.mass : 0,
      before: m0, after: m1,
      delta: m0 != null && m1 != null ? m1 - m0 : null,
      summary: after,
    });
  }
  rows.sort((x, y) => Math.abs(y.delta ?? 0) - Math.abs(x.delta ?? 0) || y.removed - x.removed);
  return rows;
}
