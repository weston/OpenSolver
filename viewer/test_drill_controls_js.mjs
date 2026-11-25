import { setActionFrequency } from "./static/drill_controls.js";

let failures = 0;
function check(actual, expected, label) {
  const ok = actual.length === expected.length && actual.every((v, i) => Math.abs(v - expected[i]) < 1e-12);
  console.log(`${ok ? "ok" : "FAIL"}: ${label}`);
  if (!ok) failures++;
}

check(setActionFrequency([0.2, 0.3, 0.5], 0, 0.9, [0, 1, 2]), [0.9, 0.0375, 0.0625],
  "setting check to 90% preserves the relative mix of the remaining actions");
check(setActionFrequency([1, 0, 0], 0, 0.9, [0, 1, 2]), [0.9, 0.05, 0.05],
  "reducing a pure action shares its remaining frequency among available actions");
check(setActionFrequency([0.4, 0, 0.6], 0, 0.9, [0, 2]), [0.9, 0, 0.1],
  "disabled range options receive no frequency when another action changes");
check(setActionFrequency([1, 0, 0], 0, 0, [0, 1, 2]), [0, 0.5, 0.5],
  "setting a pure action to zero still produces a complete mix");
check(setActionFrequency([0.2, 0.3, 0.5], 1, 1, [0, 1, 2]), [0, 1, 0],
  "a direct action click selects 100% of that action");
if (failures) throw new Error(`${failures} frequency checks failed`);
