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
  const gpu = field.querySelector('option[value="gpu"]');
  if (gpu) gpu.textContent = st.gpu_name ? `GPU (${st.gpu_name})` : "GPU";
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

export function flopStability() {
  return prefs.get("flop-stability", false) === true;
}

export function initSettings() {
  const stability = document.getElementById("flop-stability");
  stability.checked = flopStability();
  stability.onchange = () => prefs.set("flop-stability", stability.checked);
  const smart = document.getElementById("smart-tree");
  smart.value = smartTreeLevel();
  smart.onchange = () => prefs.set("smart-tree-level", smart.value);
  const help = document.getElementById("smart-tree-help");
  const tip = document.getElementById("smart-tree-tip");
  const row = help.parentElement;
  let pinned = false;
  const showHelp = (open) => {
    if (!open) pinned = false;
    tip.hidden = !open;
    help.setAttribute("aria-expanded", String(open));
  };
  row.onpointerenter = (e) => { if (e.pointerType !== "touch") showHelp(true); };
  row.onpointerleave = () => { if (!row.contains(document.activeElement)) showHelp(false); };
  row.addEventListener("focusin", () => showHelp(true));
  row.addEventListener("focusout", (e) => { if (!row.contains(e.relatedTarget)) showHelp(false); });
  help.onclick = () => { help.focus(); pinned = !pinned; showHelp(pinned); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !tip.hidden) showHelp(false); });
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
