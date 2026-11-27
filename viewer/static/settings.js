import { fail, post, prefs } from "./api.js";

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
  return Object.fromEntries(fields.map((key) => [key, Number(input(key).value)]));
}

let savingDevice = false;

export function updateDevice(st) {
  const available = st.devices?.includes("gpu");
  document.getElementById("device-settings").classList.toggle("hidden", !available);
  const field = document.getElementById("solve-device");
  if (!savingDevice) field.value = st.device;
  field.disabled = savingDevice || st.solving || st.dead;
}

const smartTreeModes = { off: false, medium: true, strong: "single" };

function smartTreeLevel() {
  const level = prefs.get("smart-tree-level", null);
  if (Object.hasOwn(smartTreeModes, level)) return level;
  if (prefs.get("smart-tree", true) !== true) return "off";
  return prefs.get("river-sizing", "adaptive") === "single" ? "strong" : "medium";
}

export function smartTree() {
  return smartTreeModes[smartTreeLevel()];
}

export function initSettings() {
  const smart = document.getElementById("smart-tree");
  smart.value = smartTreeLevel();
  smart.onchange = () => prefs.set("smart-tree-level", smart.value);
  const device = document.getElementById("solve-device");
  device.onchange = async () => {
    savingDevice = true;
    device.disabled = true;
    try { await post("/api/device", { device: device.value }); }
    catch (e) { fail(e); }
    finally { savingDevice = false; device.disabled = false; }
  };
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
