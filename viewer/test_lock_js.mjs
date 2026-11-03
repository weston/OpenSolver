// Unit test for the lock editor's frequency scaling (viewer/static/lock.js, #200).
// usage: node viewer/test_lock_js.mjs
import { initHands, H } from "./static/cards.js";
import { _test } from "./static/lock.js";

const { L, scaleAction, freqs, allLive } = _test;
let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };

// 1326 combos in some order (the solver's order does not matter here)
const R = "23456789TJQKA", Su = "cdhs", cards = [];
for (const r of R) for (const s of Su) cards.push(r + s);
const order = [];
for (let i = 1; i < 52; i++) for (let j = 0; j < i; j++) order.push(cards[i] + cards[j]);
initHands(order);

let seed = 7;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const A = 3;
function fresh() {
  seed = 7;
  const freq = [[], [], []], reach = [];
  for (let i = 0; i < 1326; i++) {
    reach.push(rnd() < 0.3 ? 0 : rnd());
    // a third of hands never play action 0 (the case multiplication alone cannot move)
    const v = [rnd() < 0.33 ? 0 : rnd(), rnd(), rnd()];
    const s = v.reduce((a, b) => a + b, 0);
    v.forEach((x, k) => freq[k].push(x / s));
  }
  L.ctx = { acts: [0, 1, 2].map((k) => ({ k, label: "a" + k, color: "#000" })), freq, reach };
  L.edit = freq.map((r) => r.slice());
  L.blocked = () => false;
  L.sel = new Set();
  L.edited = new Set();
}
const sums = () => { let worst = 0; for (let i = 0; i < 1326; i++) worst = Math.max(worst, Math.abs(L.edit.reduce((s, r) => s + r[i], 0) - 1)); return worst; };

fresh();
const all = allLive();
const f0 = freqs(L.edit, all).f;
for (const t of [1.0, 0.9, 0.5, 0.05, 0]) {
  fresh();
  scaleAction(0, t, all);
  const f = freqs(L.edit, all).f;
  check(Math.abs(f[0] - t) < 1e-9, `scale action 0 from ${f0[0].toFixed(3)} to ${t}: got ${f[0].toFixed(9)}`);
  check(sums() < 1e-9, `every combo still sums to 1 (worst ${sums().toExponential(1)})`);
  // the other actions keep their ratio within each hand
  let ratioOk = true;
  for (let i = 0; i < 1326 && t < 1; i++) {
    const a = L.ctx.freq[1][i], b = L.ctx.freq[2][i];
    if (a > 1e-6 && b > 1e-6 && Math.abs(L.edit[1][i] / L.edit[2][i] - a / b) > 1e-6) ratioOk = false;
  }
  check(ratioOk, "the other actions keep their proportions");
}

// only some hands may change: AA and KK
fresh();
L.sel = new Set(["AA", "KK"]);
const sel = [...H.byClass.AA, ...H.byClass.KK].filter((i) => L.ctx.reach[i] > 0);
const before = L.edit.map((r) => r.slice());
const fs = freqs(L.edit, all).f[0];
scaleAction(0, fs + 0.001, sel);
check(Math.abs(freqs(L.edit, all).f[0] - (fs + 0.001)) < 1e-9, "a reachable target with only AA, KK changing");
let othersSame = true;
for (let i = 0; i < 1326; i++) if (!sel.includes(i)) for (let k = 0; k < A; k++) if (L.edit[k][i] !== before[k][i]) othersSame = false;
check(othersSame, "hands outside the selection are untouched");
let threw = false;
try { scaleAction(0, 0.99, sel); } catch (e) { threw = /can only go from/.test(e.message); }
check(threw, "an unreachable target with only two hands changing is refused");
check(L.edited.has("AA") && L.edited.has("KK") && L.edited.size === 2, "the changed hands are marked edited");

console.log(fails ? `${fails} FAILED` : "ALL PASSED");
process.exit(fails ? 1 : 0);
