// What Study needs to know about a preflop tree (#490): one built by tools/preflop.py, whose root
// has no board. OOP is the big blind and IP the small blind (the button); amounts on the preflop
// street are whole investments, blinds included; after a preflop call the deal is a flop, one of the
// flops the tree was built on. No DOM in here: viewer/test_preflop_js.mjs runs it.

const RANK_ORDER = "AKQJT98765432";
const SUIT_ORDER = "shdc";

/** "flop", "turn" or "river": what is dealt at a deal node whose board has `cards` cards. */
export function dealName(cards) {
  return cards >= 4 ? "river" : cards === 3 ? "turn" : "flop";
}

/** Who a player is before the flop: IP posts the small blind and has the button. */
export function seat(player) {
  return player === "IP" ? "small blind" : "big blind";
}

/** The cards of a flop token: "Ks8s7h" -> ["Ks", "8s", "7h"]; null when it is not three cards. */
export function flopCards(tok) {
  if (typeof tok !== "string" || !/^([2-9TJQKA][shdc]){3}$/.test(tok)) return null;
  const cards = [tok.slice(0, 2), tok.slice(2, 4), tok.slice(4, 6)];
  return new Set(cards).size === 3 ? cards : null;
}

const rankOf = (card) => RANK_ORDER.indexOf(card[0]);

/** Flop tokens, highest ranks first (AAK before AKQ before KQJ ...), then by suits: a fixed place
 *  for every flop whatever order the tree holds them in. */
export function sortFlops(toks) {
  const key = (tok) => {
    const cards = [...flopCards(tok)].sort((a, b) => rankOf(a) - rankOf(b) || SUIT_ORDER.indexOf(a[1]) - SUIT_ORDER.indexOf(b[1]));
    return cards.map((c) => String.fromCharCode(65 + rankOf(c))).join("") + cards.map((c) => SUIT_ORDER.indexOf(c[1])).join("");
  };
  return toks.filter(flopCards).map((t) => [key(t), t]).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)).map((x) => x[1]);
}

/** What a typed filter asks for: ranks, each with a suit or not ("K8", "ks 8", "KK", "Ah7"), or
 *  null when it is not that. */
export function parseFilter(text) {
  const t = String(text || "").replace(/[\s,]+/g, "");
  const want = [];
  for (let i = 0; i < t.length;) {
    const r = t[i].toUpperCase();
    if (!RANK_ORDER.includes(r)) return null;
    const s = (t[i + 1] || "").toLowerCase();
    if (s && SUIT_ORDER.includes(s)) { want.push(r + s); i += 2; }
    else { want.push(r); i += 1; }
  }
  return want.length <= 3 ? want : null;
}

/** Does the flop hold what the filter asks for? Each asked card or rank needs a card of its own
 *  ("KK" is a flop with two kings). An empty filter matches every flop, one that cannot be read
 *  matches none. */
export function flopMatches(tok, text) {
  const cards = flopCards(tok);
  const want = parseFilter(text);
  if (!cards || !want) return false;
  const left = [...cards];
  // exact cards first, so "Ks K" does not spend the Ks on the bare K
  for (const w of [...want].sort((a, b) => b.length - a.length)) {
    const k = left.findIndex((c) => (w.length === 2 ? c === w : c[0] === w));
    if (k < 0) return false;
    left.splice(k, 1);
  }
  return true;
}

/** A raise on the preflop street, where every amount is a whole investment: to `amt` over the
 *  `facing` that is in front (the big blind for an open, the open for a 3-bet), in a pot of `pot`.
 *  frac: the increment over the call as a share of the pot after calling, the viewer's headline
 *  size; mult: `amt` as a multiple of what it raises (an open to 2.5 big blinds is 2.5x). */
export function preflopRaise(pot, own, facing, amt) {
  const potAfterCall = pot + (facing - own);
  return {
    frac: potAfterCall > 0 ? (amt - facing) / potAfterCall : 0,
    to: amt,
    mult: facing > 0 ? amt / facing : 0,
  };
}

/* ---- the Build tab's Preflop form (#492): a heads-up preflop tree, set up in big blinds ---- */

/** Chips in a big blind of the tree whose root this is: a preflop tree's root has no board and the
 *  big blind's whole blind as its first pot entry. 0 for a postflop tree (its root has a board). */
export function bigBlindOf(root) {
  if (!root || String(root.board || "").trim()) return 0;
  const bb = Number(root.pot && root.pot[0]);
  return Number.isFinite(bb) && bb > 0 ? bb : 0;
}

/** A target in bb/100 as the solver's accuracy, in chips: both are a win rate per hand, so 0.5
 *  bb/100 of a 100-chip big blind is 0.5 chips. */
export function bb100ToChips(bb100, chips) {
  return (bb100 / 100) * chips;
}

/** An exploitability in chips as bb/100, or null without a big blind. */
export function chipsToBb100(explo, chips) {
  return chips > 0 && Number.isFinite(explo) ? (explo / chips) * 100 : null;
}

/** Megabytes in words: "396 MB", "2.8 GB". */
export function fmtMemory(mb) {
  if (!Number.isFinite(mb) || mb < 0) return "—";
  return mb < 1000 ? `${Math.round(mb)} MB` : `${(mb / 1024).toFixed(1)} GB`;
}

/** A tree's two sizes in one: the smallest tree, to the tree with the tables that make a solve faster
 *  ("802–888 MB"); one figure where there is one (no bets on the turn, or the two print the same). */
export function fmtMemoryRange(mb, mbTables) {
  const a = fmtMemory(mb), b = fmtMemory(mbTables);
  if (!(mbTables > mb) || a === b) return a;
  const [n, unit] = a.split(" ");
  return b.endsWith(` ${unit}`) ? `${n}–${b}` : `${a} – ${b}`;
}

/** What a memory estimate says. mb is the smallest tree; mbTables, when larger, the tree with the tables
 *  the solver adds when that much memory is free as it builds (#504: either can be what a build takes).
 *  fits is false when even the smallest tree is larger than all of the machine's memory (the one case
 *  that is certain; below it the solver decides when it builds). */
export function memoryNote(mb, machineMb, flops, mbTables) {
  const fits = !(machineMb > 0) || mb <= machineMb;
  const size = mbTables > mb
    ? `about ${fmtMemory(mb)}, or ${fmtMemory(mbTables)} with the tables that make a solve faster (built when that much memory is free)`
    : `about ${fmtMemory(mb)}`;
  const parts = [`${Number(flops).toLocaleString("en-US")} flop${flops === 1 ? "" : "s"}`, size];
  if (machineMb > 0) parts.push(fits ? `this machine has ${fmtMemory(machineMb)}` : `more than this machine's ${fmtMemory(machineMb)}`);
  return { text: parts.join(" · "), fits };
}

/** The size in the line after a build ({mb, mb_tables, built_mb} of the server's answer): what the
 *  solver counted for the tree it built, and which of the two trees that is when there are two. */
export function builtSize(b) {
  const built = Number.isFinite(b.built_mb) ? b.built_mb : b.mb;
  if (!(b.mb_tables > b.mb) || !Number.isFinite(b.built_mb)) return `about ${fmtMemory(built)}`;
  return `about ${fmtMemory(built)}, ${built > b.mb ? "with the tables that make a solve faster"
    : "without the tables that make a solve faster (not that much memory free)"}`;
}

/** The streets a tree bets on when its betting goes through `depth` (none, flop, turn or river):
 *  the size fields of the other streets are not used. */
export function streetsBet(depth) {
  const n = ["none", "flop", "turn", "river"].indexOf(depth);
  return { flop: n >= 1, turn: n >= 2, river: n >= 3 };
}

/** A choice of flops in words: a shipped subset by its count, or every flop. */
export function flopsLabel(choice) {
  return choice === "all" ? "All 1,755 flops" : `${choice} flops`;
}

/** How many flops a choice deals. */
export function flopsCount(choice) { return choice === "all" ? 1755 : Number(choice); }

/** Which number of flops to ask the solver's memory estimate for next, or null when the row is done.
 *  The chosen one first, then the others from few to many ("all" only when it is the chosen one: it
 *  takes a minute to answer). cells: what is known, choice -> MB. More flops take more memory, so
 *  nothing above a number that is already over the machine's memory is asked. */
export function nextEstimate(choices, chosen, cells, machineMb) {
  const over = choices.filter((c) => cells[c] != null && machineMb > 0 && cells[c] > machineMb).map(flopsCount);
  const least = over.length ? Math.min(...over) : Infinity;
  const order = [chosen, ...choices.filter((c) => c !== chosen && c !== "all")].filter((c) => choices.includes(c));
  for (const c of order) if (cells[c] == null && flopsCount(c) < least) return c;
  return null;
}

/** One line over the tree in words. */
export function treeSummary(d) {
  const n = d.rows.length, x = d.exits;
  return `${n} node${n === 1 ? "" : "s"} before the flop · ${x === 0 ? "no way" : x === 1 ? "1 way" : `${x} ways`} to a flop`
    + ` · ${Number(d.flops).toLocaleString("en-US")} flop${d.flops === 1 ? "" : "s"}`;
}
