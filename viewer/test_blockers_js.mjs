// Unit test for the Blockers tab's arithmetic (viewer/static/blockers.js).
// usage: node viewer/test_blockers_js.mjs
import { prepare, summarize, comboReport, shareChanges, cardEffects, DECK } from "./static/blockers.js";
import { comboClass } from "./static/cards.js";

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };
const near = (a, b, eps = 1e-9) => a != null && b != null && Math.abs(a - b) <= eps;

// all 1326 combos (the order does not matter to the arithmetic)
const order = [];
for (let i = 0; i < 52; i++) for (let j = i + 1; j < 52; j++) order.push(DECK[i] + DECK[j]);
check(order.length === 1326 && new Set(DECK).size === 52, "52 cards, 1326 combos");

// villain: AA (weight 1), KK (0.5), AKs (1), QJs (1); a 2-action response (fold / call) where AA and
// KK always call and the rest always fold; equities 0.8 for pairs, 0.4 otherwise
const board = "Td9d2c";
const vr = order.map(() => 0), eq = order.map(() => null), hr = order.map(() => 0), heq = order.map(() => null);
const fold = order.map(() => 0), call = order.map(() => 0);
order.forEach((c, i) => {
  const cls = comboClass(c);
  const w = { AA: 1, KK: 0.5, AKs: 1, QJs: 1 }[cls];
  if (!w) return;
  vr[i] = w;
  eq[i] = cls.length === 2 ? 0.8 : 0.4;
  if (cls === "AA" || cls === "KK") call[i] = 1; else fold[i] = 1;
});
order.forEach((c, i) => { if (comboClass(c) === "AKo") { hr[i] = 1; heq[i] = 0.3; } });
const data = {
  board, actor: "OOP", villain: "IP", hero_reach: hr, villain_reach: vr, hero_eq: heq, villain_eq: eq,
  responses: [
    { id: "r:0:b30", node: {}, children: [{ id: "r:0:b30:f" }, { id: "r:0:b30:c" }], freq: [fold, call] },
    { id: "r:0:c", node: {}, children: null, freq: null },   // not villain's move: ignored
  ],
  cats: null,
};
const P = prepare(data, order);
check(P.responses.length === 1, "responses without a villain strategy are ignored");

const base = summarize(P);
// AA 6 + KK 6*0.5 + AKs 4 + QJs 4 = 17 combos
check(near(base.mass, 17), `villain mass 17 (got ${base.mass})`);
check(near(base.responses[0].freq[1], 9 / 17), `villain calls 9/17 (got ${base.responses[0].freq[1]})`);
check(near(base.eq, (9 * 0.8 + 8 * 0.4) / 17), "range equity is reach-weighted");

// holding AhKc: removes AA combos with Ah (3), KK with Kc (3 * 0.5), AKs with Ah or Kc (AhKh, AcKc) = 2
const hero = order.findIndex((c) => (c === "AhKc" || c === "KcAh"));
const rep = comboReport(P, hero);
check(near(rep.before.mass - rep.after.mass, 3 + 1.5 + 2), `AhKc removes 6.5 combos (got ${rep.before.mass - rep.after.mass})`);
check(near(rep.removed.reduce((s, x) => s + x.w, 0), 6.5), "the removed list adds up to the mass removed");
check(rep.removed.every((x) => x.combo.includes("Ah") || x.combo.includes("Kc")), "every removed combo holds Ah or Kc");
check(near(rep.after.responses[0].freq[1], (9 - 4.5) / 10.5), `villain calls 4.5/10.5 given AhKc (got ${rep.after.responses[0].freq[1]})`);
check(near(rep.heroEq, 0.3) && rep.heroReach === 1 && !rep.blockedByBoard, "the hand's own equity and reach");

const cls = shareChanges(rep.before, rep.after, (s) => s.classes);
check(near(cls.reduce((s, x) => s + x.before, 0), 1) && near(cls.reduce((s, x) => s + x.after, 0), 1), "class shares sum to 1 before and after");
const qj = cls.find((x) => x.key === "QJs");
check(qj && qj.delta > 0 && near(qj.after, 4 / 10.5), "unblocked QJs gets a bigger share");
check(Math.abs(cls[0].delta) >= Math.abs(cls[cls.length - 1].delta), "largest change first");

// single cards: the board's cards are skipped; a card no villain combo holds changes nothing
const rows = cardEffects(P, (s) => s.responses[0].freq[1]);
check(rows.length === 49, `49 cards off the board (got ${rows.length})`);
const two = rows.find((r) => r.card === "3h");
check(two && near(two.removed, 0) && near(two.delta, 0), "the 3h blocks nothing");
const ace = rows.find((r) => r.card === "As");
// As removes 3 AA (call) and AsKs (fold): calls 6 of 13
check(ace && near(ace.removed, 4) && near(ace.after, 6 / 13), `As: removes 4, villain calls 6/13 (got ${ace && ace.after})`);
check(rows[0].card[0] === "A" || rows[0].card[0] === "Q" || rows[0].card[0] === "J" || rows[0].card[0] === "K", `the top card is one villain's range is built from (${rows[0].card})`);

// a hand that uses a board card
const onBoard = order.findIndex((c) => c.includes("Td") && c.includes("As"));
check(comboReport(P, onBoard).blockedByBoard, "a hand with a board card is flagged");

if (fails) { console.log(`${fails} FAILED`); process.exit(1); }
console.log("ALL PASSED");
