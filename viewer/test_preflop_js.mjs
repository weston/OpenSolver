// Unit test for what Study knows about preflop trees (viewer/static/preflop.js, #490).
// usage: node viewer/test_preflop_js.mjs
import { dealName, seat, flopCards, sortFlops, parseFilter, flopMatches, preflopRaise } from "./static/preflop.js";

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const near = (a, b) => Math.abs(a - b) < 1e-12;

// ---- what is dealt, who is who
check(dealName(0) === "flop" && dealName(3) === "turn" && dealName(4) === "river", "dealName: a deal with no board yet is the flop");
check(seat("IP") === "small blind" && seat("OOP") === "big blind", "seat: IP is the small blind, OOP the big blind");

// ---- flop tokens
check(same(flopCards("Ks8s7h"), ["Ks", "8s", "7h"]), "flopCards: three cards");
check(flopCards("Ks8s") === null && flopCards("Ks8s7") === null && flopCards("Ks8s7hQd") === null && flopCards("c") === null
  && flopCards("b250") === null && flopCards("KsKs7h") === null && flopCards("Kx8s7h") === null && flopCards(undefined) === null,
  "flopCards: nothing else is a flop (two cards, four, an action token, a card twice, a bad suit)");
const flops = ["7h5d2c", "Ks8s7h", "AsKdQc", "9s9h3d", "AhAdKs", "QsJh4d", "Ks8h7h", "AsKsQs", "2s2h2d"];
check(same(sortFlops(flops), ["AhAdKs", "AsKsQs", "AsKdQc", "Ks8s7h", "Ks8h7h", "QsJh4d", "9s9h3d", "7h5d2c", "2s2h2d"]),
  `sortFlops: highest ranks first, then suits (${sortFlops(flops).join(" ")})`);
check(same(sortFlops(["8s7hKs", "7hKs8s"]), ["8s7hKs", "7hKs8s"]) && same(sortFlops(["x", "Ks8s7h", "c"]), ["Ks8s7h"]),
  "sortFlops: the order of a flop's own cards does not matter, and what is not a flop is left out");
check(sortFlops(flops).length === flops.length && same([...sortFlops(flops)].sort(), [...flops].sort()), "sortFlops: every flop once");

// ---- the filter
check(same(parseFilter("K8"), ["K", "8"]) && same(parseFilter("ks 8"), ["Ks", "8"]) && same(parseFilter("Ah,7d,2"), ["Ah", "7d", "2"])
  && same(parseFilter(""), []) && same(parseFilter("  "), []), "parseFilter: ranks with or without a suit, any case, spaces and commas");
check(parseFilter("x") === null && parseFilter("K8s7hQ") === null && parseFilter("1") === null && parseFilter("sK") === null,
  "parseFilter: not ranks, or more than three, is no filter");
const hit = (text) => flops.filter((f) => flopMatches(f, text));
check(hit("").length === flops.length, "flopMatches: an empty filter keeps every flop");
check(same(hit("K8"), ["Ks8s7h", "Ks8h7h"]) && same(hit("8k"), ["Ks8s7h", "Ks8h7h"]), "flopMatches: ranks in any order");
check(same(hit("AA"), ["AhAdKs"]) && same(hit("99"), ["9s9h3d"]) && same(hit("222"), ["2s2h2d"]) && same(hit("KK"), []),
  "flopMatches: a rank asked twice needs two cards of it");
check(same(hit("Ks"), ["Ks8s7h", "AhAdKs", "Ks8h7h", "AsKsQs"]) && same(hit("8h"), ["Ks8h7h"]) && same(hit("Kc"), []),
  "flopMatches: a card with its suit is that card");
check(same(hit("As A"), []) && same(hit("Ah A"), ["AhAdKs"]) && same(hit("A Ah"), ["AhAdKs"]) && same(hit("Ks K"), []),
  "flopMatches: a card and its bare rank need two cards (the exact one is not spent on the bare rank)");
check(same(hit("zz"), []) && same(hit("AKQJ"), []) && !flopMatches("c", "") && !flopMatches("b250", "2"),
  "flopMatches: a filter that cannot be read matches nothing, and neither does a token that is no flop");

// ---- preflop raise sizes: whole amounts, 100 chips to the big blind
const open = preflopRaise(150, 50, 100, 250);       // small blind opens to 2.5 bb
check(near(open.frac, 0.75) && open.to === 250 && near(open.mult, 2.5), "an open to 2.5 bb: 75% of the pot, to 250, 2.5x");
const pot = preflopRaise(150, 50, 100, 300);         // a pot-sized open is 3 bb
check(near(pot.frac, 1) && near(pot.mult, 3), "a pot-sized open is to 3 bb");
const three = preflopRaise(350, 100, 250, 1000);     // big blind 3-bets to 10 bb over 2.5
check(near(three.frac, 1.5) && three.to === 1000 && near(three.mult, 4), "a 3-bet to 10 bb over 2.5: 150% of the pot, 4.0x");
const iso = preflopRaise(200, 100, 100, 350);        // big blind raises a limp to 3.5 bb
check(near(iso.frac, 1.25) && near(iso.mult, 3.5), "a raise over a limp to 3.5 bb: 125% of the pot, 3.5x");
const four = preflopRaise(1250, 250, 1000, 2300);    // 4-bet to 23 bb over 10
check(near(four.frac, 0.65) && near(four.mult, 2.3), "a 4-bet to 23 bb over 10: 65% of the pot, 2.3x");
check(preflopRaise(0, 0, 0, 100).mult === 0 && preflopRaise(0, 0, 0, 100).frac === 0, "no pot and nothing to raise: zeros, not a division by zero");

console.log(fails ? `${fails} FAILED` : "ALL PASSED");
process.exit(fails ? 1 : 0);
