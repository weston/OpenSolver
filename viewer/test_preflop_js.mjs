// Unit test for what Study knows about preflop trees (viewer/static/preflop.js, #490).
// usage: node viewer/test_preflop_js.mjs
import { dealName, seat, flopCards, sortFlops, parseFilter, flopMatches, preflopRaise, bigBlindOf, bb100ToChips, chipsToBb100,
  fmtMemory, fmtMemoryRange, memoryNote, builtSize, streetsBet, flopsLabel, flopsCount, nextEstimate, treeSummary } from "./static/preflop.js";

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

// ---- the Build tab's Preflop form (#492)
check(bigBlindOf({ board: "", pot: [100, 50, 0] }) === 100 && bigBlindOf({ board: "", pot: [20, 10, 4] }) === 20,
  "bigBlindOf: a preflop root (no board) has the big blind as its first pot entry");
check(bigBlindOf({ board: "Qs 8d 7s", pot: [0, 0, 40] }) === 0 && bigBlindOf({ board: "Qs8d7s", pot: [20, 20, 0] }) === 0
  && bigBlindOf(null) === 0 && bigBlindOf({ board: "" }) === 0 && bigBlindOf({ board: "", pot: ["x"] }) === 0,
  "bigBlindOf: 0 for a postflop tree, and for a root that cannot be read");
check(near(bb100ToChips(0.5, 100), 0.5) && near(bb100ToChips(2, 20), 0.4) && near(chipsToBb100(0.5, 100), 0.5)
  && near(chipsToBb100(bb100ToChips(1.25, 40), 40), 1.25), "bb/100 and chips of accuracy: 0.5 bb/100 of a 100-chip big blind is 0.5 chips");
check(chipsToBb100(1, 0) === null && chipsToBb100(NaN, 100) === null, "chipsToBb100: nothing without a big blind or a number");
check(fmtMemory(396) === "396 MB" && fmtMemory(2837) === "2.8 GB" && fmtMemory(16384) === "16.0 GB" && fmtMemory(0) === "0 MB"
  && fmtMemory(NaN) === "—", `fmtMemory: megabytes, then gigabytes (${fmtMemory(396)}, ${fmtMemory(2837)})`);
const fit = memoryNote(396, 16384, 25), over = memoryNote(85988, 16384, 10), unknown = memoryNote(396, null, 1755);
check(fit.fits && fit.text === "25 flops · about 396 MB · this machine has 16.0 GB", `memoryNote: ${fit.text}`);
check(!over.fits && over.text === "10 flops · about 84.0 GB · more than this machine's 16.0 GB", `memoryNote: ${over.text}`);
check(unknown.fits && unknown.text === "1,755 flops · about 396 MB" && memoryNote(16384, 16384, 1).fits && memoryNote(5, 9, 1).text.startsWith("1 flop ·"),
  "memoryNote: a machine of unknown size refuses nothing; a tree of exactly the machine's size is not refused here");
// two trees where the turn has bets (#504): the smallest, and the one with the tables the solver adds when memory is free
const two = memoryNote(802, 16384, 10, 888), one = memoryNote(396, 16384, 25, 396), tight = memoryNote(802, 850, 10, 888);
check(two.fits && two.text === "10 flops · about 802 MB, or 888 MB with the tables that make a solve faster (built when that much memory is free) · this machine has 16.0 GB"
  && one.text === fit.text && memoryNote(396, 16384, 25, undefined).text === fit.text && memoryNote(396, 16384, 25, null).text === fit.text
  && memoryNote(396, 16384, 25, 300).text === fit.text, `memoryNote: both sizes where they differ, one where they do not (${two.text})`);
check(tight.fits && !memoryNote(802, 801, 10, 888).fits, "memoryNote: only a smallest tree over the machine's memory is refused");
check(fmtMemoryRange(802, 888) === "802–888 MB" && fmtMemoryRange(2039, 2270) === "2.0–2.2 GB" && fmtMemoryRange(980, 1100) === "980 MB – 1.1 GB"
  && fmtMemoryRange(364, 364) === "364 MB" && fmtMemoryRange(85988, 86184) === "84.0–84.2 GB" && fmtMemoryRange(3194, 3195) === "3.1 GB"
  && fmtMemoryRange(18, undefined) === "18 MB" && fmtMemoryRange(18, null) === "18 MB" && fmtMemoryRange(NaN, 5) === "—",
  `fmtMemoryRange: the smallest tree to the one with the tables, one figure where they print the same (${fmtMemoryRange(802, 888)})`);
check(builtSize({ mb: 802, mb_tables: 888, built_mb: 888 }) === "about 888 MB, with the tables that make a solve faster"
  && builtSize({ mb: 802, mb_tables: 888, built_mb: 802 }) === "about 802 MB, without the tables that make a solve faster (not that much memory free)"
  && builtSize({ mb: 18, mb_tables: 18, built_mb: 18 }) === "about 18 MB" && builtSize({ mb: 802, mb_tables: 888, built_mb: null }) === "about 802 MB"
  && builtSize({ mb: 18 }) === "about 18 MB" && builtSize({ mb: 18, built_mb: 19 }) === "about 19 MB",
  "builtSize: what the solver counted for the built tree, and which of the two trees it is");
check(same(streetsBet("none"), { flop: false, turn: false, river: false }) && same(streetsBet("flop"), { flop: true, turn: false, river: false })
  && same(streetsBet("turn"), { flop: true, turn: true, river: false }) && same(streetsBet("river"), { flop: true, turn: true, river: true })
  && same(streetsBet("x"), { flop: false, turn: false, river: false }), "streetsBet: the streets up to the depth");
check(flopsLabel("25") === "25 flops" && flopsLabel("all") === "All 1,755 flops", "flopsLabel");
{
  const all = ["10", "25", "50", "100", "200", "all"];
  const asked = (chosen, mb, machine) => {   // the choices in the order they are asked, mb: choice -> answer
    const cells = {}, out = [];
    for (let c; (c = nextEstimate(all, chosen, cells, machine)) != null;) { out.push(c); cells[c] = mb[c]; }
    return out.join(" ");
  };
  const mb = { 10: 159, 25: 396, 50: 782, 100: 1513, 200: 2837, all: 13154 };
  check(flopsCount("all") === 1755 && flopsCount("50") === 50, "flopsCount");
  check(asked("25", mb, 16384) === "25 10 50 100 200", "nextEstimate: the chosen number first, then few to many, all flops not unless chosen");
  check(asked("all", mb, 16384) === "all 10 25 50 100 200", "nextEstimate: all flops when chosen");
  check(asked("25", mb, 1000) === "25 10 50 100" && asked("10", mb, 300) === "10 25" && asked("200", mb, 300) === "200 10 25"
    && asked("all", mb, 100) === "all 10", "nextEstimate: nothing above a number that is over the machine's memory");
  check(asked("25", mb, 0) === "25 10 50 100 200" && asked("7", mb, 16384) === "10 25 50 100 200"
    && nextEstimate(all, "25", { 25: 1, 10: 1, 50: 1, 100: 1, 200: 1 }, 16384) === null, "nextEstimate: unknown machine, unknown choice, done");
}
check(treeSummary({ rows: new Array(19), exits: 4, flops: 10 }) === "19 nodes before the flop · 4 ways to a flop · 10 flops"
  && treeSummary({ rows: [1], exits: 0, flops: 1755 }) === "1 node before the flop · no way to a flop · 1,755 flops"
  && treeSummary({ rows: [1, 2], exits: 1, flops: 1 }) === "2 nodes before the flop · 1 way to a flop · 1 flop", "treeSummary");

console.log(fails ? `${fails} FAILED` : "ALL PASSED");
process.exit(fails ? 1 : 0);
