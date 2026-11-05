// Jobs view (#196): queue solves of the Build tab's tree on many flops, run them one at a time,
// open finished saves. Talks to /api/jobs/* (viewer/jobs_api.py), the same queue tools/jobs.py uses.

import { get, post, toast, fail, prefs, fmt } from "./api.js";
import { el, cardEl } from "./cards.js";
import { currentTree } from "./build.js";

const $ = (id) => document.getElementById(id);
let onOpened = async () => {};
let visible = false;
let timer = null;
let last = null;

const STATUS = { queued: "", running: "busy", done: "ok", failed: "err" };

function boardEl(board) {
  const wrap = el("span", "jobs-board");
  for (let i = 0; i < board.length; i += 2) wrap.appendChild(cardEl(board.slice(i, i + 2), "xs"));
  return wrap;
}

function btn(label, cls, title, fn) {
  const b = el("button", `btn tiny ${cls}`.trim(), label);
  b.title = title;
  b.onclick = async (ev) => {
    ev.stopPropagation();
    b.disabled = true;
    try { await fn(); } catch (e) { fail(e); }
    b.disabled = false;
    refresh();
  };
  return b;
}

function batchPct(st, j) {
  const b = st.batches.find((x) => x.name === j.batch);
  return b ? b.accuracy_pct : null;
}

function render(st) {
  last = st;
  const running = st.running;
  $("jobs-state").textContent = running ? (st.stopping ? "stopping…" : "running") : "idle";
  $("jobs-state").className = "tag" + (running ? " busy" : "");
  $("jobs-run").classList.toggle("hidden", running);
  $("jobs-stop").classList.toggle("hidden", !running);
  $("jobs-stop-now").classList.toggle("hidden", !running);
  const nFailed = st.jobs.filter((j) => j.status === "failed").length;
  $("jobs-retry-failed").classList.toggle("hidden", !nFailed);
  $("jobs-run").disabled = !st.jobs.some((j) => j.status === "queued");

  const bs = $("jobs-batches");
  bs.replaceChildren();
  for (const b of st.batches) {
    const chip = el("div", "jobs-batch");
    const pct = b.total ? (100 * b.done) / b.total : 0;
    chip.appendChild(el("b", "", b.name));
    chip.appendChild(el("span", "muted", `${b.done}/${b.total} done${b.failed ? ` · ${b.failed} failed` : ""}` +
      ` · ${fmt(b.accuracy_pct, 2)}% · ${b.save === "full" ? "full" : b.save.replace("_", " ")}`));
    const bar = el("div", "jobs-bar");
    bar.appendChild(el("i"));
    bar.firstChild.style.width = pct + "%";
    chip.appendChild(bar);
    const tools = el("span", "jobs-batch-tools");
    if (!running && b.queued) tools.appendChild(btn("Run", "", "Run this batch's queued jobs", () => post("/api/jobs/run", { batch: b.name })));
    tools.appendChild(btn("Remove", "ghost", "Remove the batch's jobs (saves are kept)", async () => {
      if (!confirm(`Remove all ${b.total} jobs of ${b.name}? Saved files are kept.`)) return;
      await post("/api/jobs/remove", { batch: b.name });
    }));
    chip.appendChild(tools);
    bs.appendChild(chip);
  }

  const body = $("jobs-body");
  body.replaceChildren();
  $("jobs-empty").classList.toggle("hidden", st.jobs.length > 0);
  for (const j of st.jobs) {
    const tr = el("tr", j.status);
    const r = j.result || {};
    tr.appendChild(el("td", "muted", String(Number(j.id))));
    tr.appendChild(el("td", "", j.batch));
    const tdb = el("td");
    tdb.appendChild(boardEl(j.board));
    tr.appendChild(tdb);
    tr.appendChild(el("td", "num", fmt(j.weight, 2)));
    const tds = el("td");
    let label = j.status;
    if (j.status === "running" && st.point && st.current === j.id) {
      label = `running · ${fmt(st.point.pct, 2)}% · ${fmt(st.point.t, 0)}s`;
    }
    const tag = el("span", `tag ${STATUS[j.status] || ""}`.trim(), label);
    if (j.error) tag.title = j.error;
    tds.appendChild(tag);
    tr.appendChild(tds);
    // exploitability as % of the pot the batch targets (chips in the tooltip)
    const pot = r.ev_oop != null && r.ev_ip != null ? r.ev_oop + r.ev_ip : null;
    const ex = el("td", "num", r.exploitability != null && pot ? fmt((100 * r.exploitability) / pot, 2) + "%" : "—");
    if (r.exploitability != null) ex.title = `${fmt(r.exploitability, 3)} chips (target ${fmt(batchPct(st, j), 2)}% of the pot)`;
    tr.appendChild(ex);
    tr.appendChild(el("td", "num", r.ev_oop != null ? fmt(r.ev_oop, 2) : "—"));
    tr.appendChild(el("td", "num", r.seconds != null ? fmt(r.seconds, 1) + "s" : "—"));
    const act = el("td", "jobs-actions");
    if (j.status === "done") act.appendChild(btn("Open", "", "Open the save in Study", async () => {
      await post("/api/jobs/open", { id: j.id });
      toast(`Opened ${j.board} (${j.batch})`);
      await onOpened();
    }));
    if (j.status === "queued" && !running) act.appendChild(btn("Run", "", "Run just this job", () => post("/api/jobs/run", { ids: [j.id] })));
    if (j.status === "queued") act.appendChild(btn("↑", "ghost", "Run next", () => post("/api/jobs/move", { id: j.id, to: 1 })));
    if (j.status === "failed" || j.status === "done") act.appendChild(btn("Retry", "ghost", "Queue it again", () => post("/api/jobs/retry", { ids: [j.id] })));
    if (j.status !== "running") act.appendChild(btn("✕", "ghost", "Remove (the save is kept)", () => post("/api/jobs/remove", { ids: [j.id] })));
    tr.appendChild(act);
    body.appendChild(tr);
  }
  const log = $("jobs-log");
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = st.log.join("\n") || "Nothing has run yet.";
  if (atEnd) log.scrollTop = log.scrollHeight;
}

async function refresh() {
  clearTimeout(timer);
  try { render(await get("/api/jobs/state")); } catch (e) { $("jobs-state").textContent = "offline"; }
  if (visible) timer = setTimeout(refresh, last && last.running ? 1500 : 4000);
}

async function loadFlopFiles() {
  const sel = $("jb-file");
  sel.replaceChildren(el("option", "", "Type the boards…"));
  sel.firstChild.value = "";
  try {
    const ff = await get("/api/jobs/flop_files");
    for (const f of ff.files) {
      const o = el("option", "", `${f.name.replace(/\.txt$/i, "")} (${f.boards} flops)`);
      o.value = f.name;
      sel.appendChild(o);
    }
    sel.title = ff.dir ? `Flop files from ${ff.dir}` : "No flop folder (start the viewer with --flops-dir)";
  } catch (e) { /* the list stays: type the boards */ }
  sel.value = prefs.get("jobs-file", "");
  if (sel.value !== prefs.get("jobs-file", "")) sel.value = "";
  $("jb-list-field").classList.toggle("hidden", !!sel.value);
}

async function addBatch() {
  const info = (h, err) => { $("jb-info").innerHTML = err ? `<span class="err">${h}</span>` : h; };
  let tree;
  try { tree = currentTree(); } catch (e) { info(String(e.message || e), true); return; }
  const name = $("jb-name").value.trim();
  if (!name) { info("Give the batch a name.", true); return; }
  const file = $("jb-file").value;
  const body = {
    name, tree, accuracy: Number($("jb-acc").value),
    save: document.querySelector('input[name="jb-save"]:checked').value,
  };
  if (file) body.flops_file = file; else body.flops = $("jb-list").value;
  $("jb-add").disabled = true;
  info("Checking the tree…");
  try {
    const r = await post("/api/jobs/add", body);
    const est = (r.estimate || []).find((l) => /MB|GB/.test(l)) || "";
    info(`Queued ${r.added} jobs${est ? ` · ${est.replace(/^estimated tree size/, "each about").replace(/\(v4_large\):\s*/, "")}` : ""}.`);
    toast(`Queued ${r.added} jobs in ${name}`);
    refresh();
  } catch (e) { info(String(e.message || e), true); }
  $("jb-add").disabled = false;
}

export function showJobs(on) {
  visible = on;
  if (on) refresh(); else clearTimeout(timer);
}

export function initJobs({ opened }) {
  onOpened = opened || onOpened;
  $("jobs-run").onclick = async () => { try { await post("/api/jobs/run", {}); refresh(); } catch (e) { fail(e); } };
  $("jobs-stop").onclick = async () => { try { await post("/api/jobs/stop", {}); refresh(); } catch (e) { fail(e); } };
  $("jobs-stop-now").onclick = async () => { try { await post("/api/jobs/stop", { now: true }); refresh(); } catch (e) { fail(e); } };
  $("jobs-retry-failed").onclick = async () => { try { await post("/api/jobs/retry", { failed: true }); refresh(); } catch (e) { fail(e); } };
  $("jb-file").onchange = () => { prefs.set("jobs-file", $("jb-file").value); $("jb-list-field").classList.toggle("hidden", !!$("jb-file").value); };
  $("jb-add").onclick = addBatch;
  $("jb-acc").value = prefs.get("jobs-acc", "0.5");
  $("jb-acc").oninput = () => prefs.set("jobs-acc", $("jb-acc").value);
  loadFlopFiles();
}
