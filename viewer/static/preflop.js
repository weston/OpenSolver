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

/** Chips in a big blind of the tree whose root this is: a preflop tree's root has no board and the
 *  big blind's whole blind as its first pot entry. 0 for a postflop tree (its root has a board). */
export function bigBlindOf(root) {
  if (!root || String(root.board || "").trim()) return 0;
  const bb = Number(root.pot && root.pot[0]);
  return Number.isFinite(bb) && bb > 0 ? bb : 0;
}

/** An exploitability in chips as bb/100, or null without a big blind. */
export function chipsToBb100(explo, chips) {
  return chips > 0 && Number.isFinite(explo) ? (explo / chips) * 100 : null;
}
