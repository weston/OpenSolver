import { prefs } from "./api.js";

const fields = ["ahead", "accuracy", "seconds", "frequency_tolerance"];
const input = (key) => document.getElementById("drill-" + key);

export function drillSettings() {
  for (const key of fields) {
    if (!input(key).checkValidity()) {
      location.hash = "#settings";
      requestAnimationFrame(() => input(key).reportValidity());
      throw new Error("Check the highlighted drill setting.");
    }
  }
  return { ...Object.fromEntries(fields.map((key) => [key, Number(input(key).value)])),
    merge_similar: input("merge-similar").checked };
}

export function initSettings() {
  const merge = input("merge-similar");
  merge.checked = prefs.get("drill-merge-similar", true) !== false;
  merge.onchange = () => prefs.set("drill-merge-similar", merge.checked);
  for (const key of fields) {
    const field = input(key);
    field.value = prefs.get("drill-" + key, field.defaultValue);
    if (!field.checkValidity()) field.value = field.defaultValue;
    field.oninput = () => {
      const valid = field.checkValidity();
      field.classList.toggle("invalid", !valid);
      if (valid) prefs.set("drill-" + key, Number(field.value));
    };
  }
}
