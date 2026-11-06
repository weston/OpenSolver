// Unit test for the Build form's size fields (viewer/static/sizes.js, #354).
// usage: node viewer/test_sizes_js.mjs
import { readSizes, inBounds, SIZE_RE, RAISE_RE } from "./static/sizes.js";

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };

// a stand-in for the sizes table: inputs with data-k inside rows with data-street / data-player
function fakeBody(fields) {
  const inputs = fields.map(([street, player, k, value, type]) => {
    const tr = { dataset: { street, player } };
    return { dataset: { k }, type: type || "text", value, checked: false, closest: () => tr };
  });
  return { querySelectorAll: () => inputs };
}

const f = readSizes(fakeBody([
  ["flop", "oop", "bet", "33 75"],
  ["flop", "ip", "bet", " 25,  50  a "],
  ["turn", "oop", "raise", "5e 3e,2e  a"],
  ["river", "oop", "bet", ""],
  ["flop", "oop", "raisecap", "3"],
]));
check(f.flop_bet === "33,75", `space-separated sizes stay apart: ${f.flop_bet} (was 3375)`);
check(f.flop_bet_ip === "25,50,a", `IP's field: ${f.flop_bet_ip}`);
check(f.turn_raise === "5e,3e,2e,a", `mixed separators: ${f.turn_raise}`);
check(f.river_bet === "", "an empty field stays empty");
check(f.flop_raisecap === "3", "the raise cap number passes through");

for (const [p, want] of [["99e", true], ["100e", false], ["2e100000", true], ["2e200000", false],
  ["1000x", true], ["5000x", false], ["100000", true], ["100001", false], ["a", true], ["e", true], ["e50", true]])
  check(inBounds(p) === want, `inBounds(${p}) = ${want}`);
for (const p of ["0", ".5", "5.", "+5", "1_0", "-0", "allin", "0e50", "2e0"])
  check(!SIZE_RE.test(p) && !RAISE_RE.test(p), `the form refuses ${p}`);
check(!SIZE_RE.test("0.5x") && !RAISE_RE.test("0.5x") && RAISE_RE.test("1.5x") && !SIZE_RE.test("3x"),
  "Nx: raise fields only, N >= 1");

console.log(fails ? `${fails} FAILED` : "ALL PASSED");
if (fails) process.exit(1);
