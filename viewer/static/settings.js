import { fail, get, post, prefs } from "./api.js";

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
  field.disabled = savingDevice || st.solving || st.dead || benchRunning;
  currentDevice = st.device;
  mainSolving = st.solving || st.dead;
  benchButtons();
}

// ---- the device benchmark (#569): which device solves faster on this machine ----
let benchRunning = false, benchTimer = null, currentDevice = "cpu", mainSolving = false, benchShown = null;
const $ = (id) => document.getElementById(id);
const deviceName = (d) => (d === "gpu" ? "GPU" : "CPU");
const seconds = (s) => (s == null ? "—" : s >= 90 ? "90 s or more" : s >= 10 ? s.toFixed(1) + " s" : s.toFixed(2) + " s");

function benchButtons() {
  $("bench-start").disabled = benchRunning || mainSolving;
  $("bench-start").title = mainSolving && !benchRunning ? "Stop the solve first: it would slow down both devices" : "";
  $("bench-cancel").classList.toggle("hidden", !benchRunning);
  const rec = benchShown?.recommendation;
  const apply = $("bench-apply");
  apply.classList.toggle("hidden", benchRunning || !rec || rec.device === currentDevice);
  if (rec) apply.textContent = `Use the ${deviceName(rec.device)}`;
}

function renderBench(st) {
  benchShown = st;
  benchRunning = !!st.running;
  $("bench-progress").classList.toggle("hidden", !benchRunning);
  $("bench-bar-fill").style.width = st.total ? `${Math.round((100 * st.done) / st.total)}%` : "0";
  $("bench-step").textContent = benchRunning ? `${st.step || "Running"}… (${st.done} of ${st.total} solves)` : "";
  const rows = (st.results || []).filter((r) => r.cpu != null || r.gpu != null || r.gpu_note);
  $("bench-table").classList.toggle("hidden", !rows.length);
  $("bench-rows").replaceChildren(...rows.map((r) => {
    const tr = document.createElement("tr");
    const gpu = r.gpu_note ? "doesn't fit" : seconds(r.gpu);
    const faster = r.gpu_note ? "CPU either way" : r.cpu == null || r.gpu == null ? "…"
      : r.cpu < r.gpu ? `CPU ${(r.gpu / r.cpu).toFixed(2)}×` : `GPU ${(r.cpu / r.gpu).toFixed(2)}×`;
    for (const text of [r.tree, r.gpu_note ? "not timed" : r.cpu == null ? "…" : seconds(r.cpu), gpu, faster]) {
      tr.insertCell().textContent = text;
    }
    return tr;
  }));
  const unfit = rows.filter((r) => r.gpu_note);
  $("bench-unfit").classList.toggle("hidden", !unfit.length);
  $("bench-unfit").title = unfit.length ? `The solver said: ${unfit[0].gpu_note}` : "";
  $("bench-unfit").textContent = unfit.length
    ? `${unfit.map((r) => r.tree).join(" and ")} ${unfit.length > 1 ? "do" : "does"} not fit the GPU's memory, so ${unfit.length > 1 ? "they solve" : "it solves"} on the CPU whichever device is set.`
    : "";
  const rec = st.recommendation, verdict = $("bench-verdict");
  verdict.classList.toggle("hidden", !rec || benchRunning);
  if (rec) {
    const when = st.finished ? ` (measured ${new Date(st.finished * 1000).toLocaleString()})` : "";
    verdict.textContent = rec.verdict === "gpu_unfit"
      ? `None of these trees fit the GPU's memory, so the CPU is the device to use${when}.`
      : rec.verdict === "same"
      ? `The CPU and GPU took about as long (${seconds(rec.cpu)} and ${seconds(rec.gpu)} in all). Either is fine${when}.`
      : `The ${deviceName(rec.device)} is ${rec.ratio.toFixed(2)}× faster on the trees that fit both: ${seconds(rec.cpu)} on the CPU, ${seconds(rec.gpu)} on the GPU in all${when}.`
        + (rec.device === currentDevice ? ` It is already selected.` : "");
  }
  const error = st.error && st.error !== "cancelled" ? st.error : "";
  $("bench-error").classList.toggle("hidden", !error);
  $("bench-error").textContent = error ? `The benchmark stopped: ${error}` : "";
  benchButtons();
  if (!benchRunning && rec) prefs.set("device-benchmark", st);
}

async function pollBench() {
  clearTimeout(benchTimer);
  try {
    const st = await get("/api/device/benchmark");
    if (!st.running && !st.results?.length && !st.error) {
      const saved = prefs.get("device-benchmark", null);   // the last result, from before a restart
      renderBench(saved && saved.recommendation ? saved : st);
    } else renderBench(st);
    if (st.running) benchTimer = setTimeout(pollBench, 1000);
  } catch (e) { benchTimer = setTimeout(pollBench, 3000); }
}

function initBench() {
  $("bench-start").onclick = async () => {
    try { renderBench(await post("/api/device/benchmark", { smart_tree: smartTree() })); pollBench(); }
    catch (e) { fail(e); }
  };
  $("bench-cancel").onclick = async () => {
    try { renderBench(await post("/api/device/benchmark/cancel")); } catch (e) { fail(e); }
  };
  $("bench-apply").onclick = async () => {
    const device = benchShown?.recommendation?.device;
    if (!device) return;
    $("bench-apply").disabled = true;
    try {
      await post("/api/device", { device });
      currentDevice = device;
      $("solve-device").value = device;
      renderBench(benchShown);
    } catch (e) { fail(e); }
    finally { $("bench-apply").disabled = false; }
  };
  pollBench();
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
  initBench();
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
