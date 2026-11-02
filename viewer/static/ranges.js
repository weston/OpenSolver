// Range text <-> 1326 weights, in the solver's syntax (src/range.cpp):
//   items separated by commas, later items override earlier ones, optional ":weight" (0..1)
//   AA  AKs  AKo  AK  AsKs  22+  A2s+  K9o+  T8+  TT-77  A5s-A2s  KTo-K8o  J9-J6
// The solver stays the authority (it re-parses on build); this mirrors it for the editor.

import { RANKS, H, comboClass } from "./cards.js";

const R = (ch) => RANKS.indexOf(ch.toUpperCase()); // 0 = A ... 12 = 2

function setClass(w, hi, lo, kind, v) {
  const name = hi === lo ? RANKS[hi] + RANKS[lo]
    : RANKS[Math.min(hi, lo)] + RANKS[Math.max(hi, lo)];
  const kinds = hi === lo ? [""] : kind ? [kind] : ["s", "o"];
  for (const k of kinds) for (const i of H.byClass[name + k] || []) w[i] = v;
}

function parseClass(s) {
  // -> {hi, lo, kind} with hi/lo as rank indices (hi = stronger = smaller index)
  const m = /^([2-9TJQKA])([2-9TJQKA])([so]?)$/i.exec(s);
  if (!m) return null;
  const a = R(m[1]), b = R(m[2]);
  const kind = m[3].toLowerCase();
  if (a === b && kind) return null;
  return { hi: Math.min(a, b), lo: Math.max(a, b), kind };
}

function applyItem(body, v, w) {
  if (/^[2-9TJQKA][cdhs][2-9TJQKA][cdhs]$/i.test(body)) {
    const c = body[0].toUpperCase() + body[1].toLowerCase() + body[2].toUpperCase() + body[3].toLowerCase();
    const i = H.index.get(c);
    if (i == null) throw new Error(`invalid combo '${body}'`);
    w[i] = v;
    return;
  }
  const dash = body.indexOf("-");
  if (dash > 0) {
    const a = parseClass(body.slice(0, dash)), b = parseClass(body.slice(dash + 1));
    if (!a || !b || a.kind !== b.kind) throw new Error(`invalid dash range '${body}'`);
    if (a.hi === a.lo && b.hi === b.lo) {
      for (let r = Math.min(a.hi, b.hi); r <= Math.max(a.hi, b.hi); r++) setClass(w, r, r, "", v);
      return;
    }
    if (a.hi !== b.hi) throw new Error(`dash range with different high ranks '${body}'`);
    for (let r = Math.min(a.lo, b.lo); r <= Math.max(a.lo, b.lo); r++) setClass(w, a.hi, r, a.kind, v);
    return;
  }
  if (body.endsWith("+")) {
    const c = parseClass(body.slice(0, -1));
    if (!c) throw new Error(`unknown token '${body}'`);
    if (c.hi === c.lo) { for (let r = 0; r <= c.hi; r++) setClass(w, r, r, "", v); return; }
    for (let r = c.hi + 1; r <= c.lo; r++) setClass(w, c.hi, r, c.kind, v);
    return;
  }
  const c = parseClass(body);
  if (!c) throw new Error(`unknown token '${body}'`);
  setClass(w, c.hi, c.lo, c.kind, v);
}

export function parseRange(text) {
  const w = new Float32Array(1326);
  for (const raw of text.split(",")) {
    const item = raw.trim();
    if (!item) continue;
    let body = item, v = 1;
    const colon = item.indexOf(":");
    if (colon >= 0) {
      body = item.slice(0, colon).trim();
      v = Number(item.slice(colon + 1).trim());
      if (!Number.isFinite(v) || v < 0) throw new Error(`invalid weight in '${item}'`);
      v = Math.min(v, 1);
    }
    if (!body) throw new Error(`empty item in '${item}'`);
    applyItem(body, v, w);
  }
  return w;
}

const fmtW = (v) => String(Math.round(v * 1000) / 1000);

/** Weights -> compact text: whole classes where uniform, single combos otherwise. */
export function formatRange(w) {
  const items = [];
  const kind = (c) => (c.length === 2 ? 0 : c[2] === "s" ? 1 : 2);
  const ordered = [...H.classes].sort((a, b) => kind(a) - kind(b) || H.classes.indexOf(a) - H.classes.indexOf(b));
  for (const cls of ordered) {
    const idx = H.byClass[cls];
    const vals = idx.map((i) => w[i]);
    if (vals.every((x) => x === 0)) continue;
    if (vals.every((x) => x === vals[0])) {
      items.push(vals[0] >= 1 ? cls : `${cls}:${fmtW(vals[0])}`);
    } else {
      for (const i of idx) if (w[i] > 0) items.push(w[i] >= 1 ? H.order[i] : `${H.order[i]}:${fmtW(w[i])}`);
    }
  }
  return items.join(",");
}

export function rangeStats(w) {
  let combos = 0;
  for (let i = 0; i < 1326; i++) combos += w[i];
  return { combos, pct: (combos / 1326) * 100 };
}

export { comboClass };
