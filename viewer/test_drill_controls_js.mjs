import { setActionFrequency, moveMixBoundary, initialFrequencies } from "./static/drill_controls.js";

let failures = 0;
function check(actual, expected, label) {
  const ok = actual.length === expected.length && actual.every((v, i) => Math.abs(v - expected[i]) < 1e-12);
  console.log(`${ok ? "ok" : "FAIL"}: ${label}`);
  if (!ok) failures++;
}

const actions = [{ id: "b75", kind: "bet", amount: 75 }, { id: "b25", kind: "bet", amount: 25 }, { id: "c", kind: "check" }];
check(initialFrequencies(actions), [0.35, 0.3, 0.35],
  "untouched frequencies sum to 100% in solver order with the displayed five-percent steps");
check(initialFrequencies(actions, ["b75"]), [0, 0.5, 0.5],
  "untouched hands submit zero frequency for disabled range options");
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
check(moveMixBoundary([0.2, 0.3, 0.5], 0, 0.9, [0, 1, 2]), [0.9, 0, 0.1],
  "dragging check to 90% pushes through the next bet size");
check(moveMixBoundary([0.4, 0.3, 0.3], 1, 0.1, [0, 1, 2]), [0.1, 0, 0.9],
  "dragging left pushes earlier dividers and keeps all frequencies nonnegative");
check(moveMixBoundary([1, 0, 0], 0, 0.9, [0, 1, 2]), [0.9, 0.1, 0],
  "a zero-frequency size can receive frequency again");
check(moveMixBoundary([0.4, 0, 0.6], 0, 0.9, [0, 2]), [0.9, 0, 0.1],
  "dragging past a disabled range option leaves it at zero");
check(moveMixBoundary([0.2, 0.3, 0.5], 0, 1.2, [0, 1, 2]), [1, 0, 0],
  "dragging beyond the bar selects a pure action");
if (failures) throw new Error(`${failures} frequency checks failed`);
