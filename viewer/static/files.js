import { el, cardEl, parseCards } from "./cards.js";
import { get, prefs, fmt } from "./api.js";

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 40;
const nameOf = (path) => path.split(/[/\\]/).pop();
const dateText = (time) => time ? new Date(time * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const byteText = (bytes) => bytes == null ? "" : bytes >= 2 ** 30 ? `${fmt(bytes / 2 ** 30, 1)} GB` : bytes >= 2 ** 20 ? `${fmt(bytes / 2 ** 20, 1)} MB` : `${fmt(bytes / 1024, 0)} KB`;
const history = () => {
  const saved = prefs.get("recent-saves", []);
  const entries = Array.isArray(saved) ? saved.filter((entry) => entry && typeof entry.path === "string").slice(0, 20) : [];
  const last = prefs.get("path", null);
  if (typeof last === "string" && last && !entries.some((entry) => entry.path === last)) entries.push({ path: last, name: nameOf(last) });
  return entries.slice(0, 20);
};
const F = { source: "saved", entries: [], saves: [], places: [], selected: null, folder: null, page: 0, request: 0, busy: false, load: null };

function drawPlaces() {
  const nav = $("open-places");
  nav.replaceChildren();
  const place = (name, key, action) => {
    const button = el("button", F.source === key ? "active" : "", name);
    button.type = "button";
    button.onclick = action;
    nav.appendChild(button);
  };
  place("Completed solves", "saved", () => showCollection("saved"));
  place("Recent", "recent", () => showCollection("recent"));
  for (const item of F.places) place(item.name, item.path, () => openFolder(item.path));
}

function drawLocation() {
  const bar = $("open-breadcrumbs");
  bar.replaceChildren();
  if (!F.folder) { bar.appendChild(el("strong", "", F.source === "saved" ? "Saved simulations" : "Recently opened or saved")); return; }
  const button = (name, path) => {
    const b = el("button", "btn ghost small", name);
    b.onclick = () => openFolder(path);
    bar.appendChild(b);
  };
  if (F.folder.parent) button("↑", F.folder.parent);
  const path = F.folder.path.replace(/\\/g, "/");
  let prefix = path.startsWith("/") ? "/" : "";
  if (prefix) button("/", prefix);
  for (const part of path.split("/").filter(Boolean)) {
    prefix += part + "/";
    button(part, prefix);
  }
}

function select(entry) {
  F.selected = entry;
  $("open-selection").textContent = entry ? [entry.name, byteText(entry.bytes)].filter(Boolean).join(" · ") : "";
  $("open-selection").classList.remove("rerr");
  $("open-submit").disabled = !entry || F.busy;
  $("open-list").querySelectorAll(".save-row").forEach((row) => row.setAttribute("aria-pressed", String(row.dataset.path === entry?.path)));
}

function drawList() {
  const query = $("open-search").value.trim().toLowerCase();
  const entries = F.entries.filter((entry) => `${entry.name} ${entry.board || ""} ${entry.path}`.toLowerCase().includes(query));
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });
  entries.sort((a, b) => Number(!!b.folder) - Number(!!a.folder) || ($("open-sort").value === "name" || a.folder ? byName(a, b) : (b.mtime || 0) - (a.mtime || 0) || byName(a, b)));
  if (F.selected && !entries.some((entry) => entry.path === F.selected.path)) select(null);
  F.page = Math.min(F.page, Math.max(0, Math.ceil(entries.length / PAGE_SIZE) - 1));
  const list = $("open-list");
  list.replaceChildren();
  const start = F.page * PAGE_SIZE;
  for (const entry of entries.slice(start, start + PAGE_SIZE)) {
    const row = el("button", "save-row");
    row.type = "button";
    row.dataset.path = entry.path;
    row.setAttribute("aria-pressed", String(entry.path === F.selected?.path));
    const preview = el("span", "save-row-preview");
    const board = entry.board || (/^(?:[2-9TJQKA][shdc]){3,5}$/i.test(entry.name.replace(/\.cfr$/i, "")) ? entry.name.replace(/\.cfr$/i, "") : "");
    if (board) for (const card of parseCards(board)) preview.appendChild(cardEl(card, "sm"));
    else preview.appendChild(el("span", entry.folder ? "folder-ico" : "item-ico spot"));
    const text = el("span", "save-row-text");
    text.append(el("strong", "", entry.name), el("span", "muted", entry.folder ? "Folder" : entry.details || byteText(entry.bytes)));
    row.append(preview, text, el("span", "save-row-date muted", entry.folder ? "›" : dateText(entry.mtime)));
    row.onclick = () => entry.folder ? openFolder(entry.path) : select(entry);
    row.ondblclick = () => { if (!entry.folder) { select(entry); submit(); } };
    list.appendChild(row);
  }
  if (!entries.length) {
    list.appendChild(el("div", "lib-empty", query ? "No matching saves" : "No saved simulations"));
    if (!F.folder) {
      const browse = el("button", "btn", "Browse files");
      browse.onclick = () => openFolder(prefs.get("save-folder", "~"));
      list.appendChild(browse);
    }
  }
  const pages = $("open-pages");
  pages.replaceChildren(el("span", "muted", entries.length ? `${start + 1}–${Math.min(start + PAGE_SIZE, entries.length)} of ${entries.length}` : ""));
  if (entries.length > PAGE_SIZE) for (const [label, page, disabled] of [["Previous", F.page - 1, F.page === 0], ["Next", F.page + 1, start + PAGE_SIZE >= entries.length]]) {
    const button = el("button", "btn small", label);
    button.disabled = disabled;
    button.onclick = () => { F.page = page; drawList(); };
    pages.appendChild(button);
  }
}

function showCollection(source) {
  F.request++;
  Object.assign(F, { source, folder: null, entries: source === "saved" ? F.saves : history(), page: 0 });
  $("open-search").value = "";
  select(null);
  drawPlaces(); drawLocation(); drawList();
}

async function openFolder(path) {
  const request = ++F.request;
  select(null);
  $("open-list").textContent = "Loading…";
  try {
    const folder = await get(`/api/files?path=${encodeURIComponent(path)}`);
    if (request !== F.request || !$("open-dialog").open) return;
    Object.assign(F, { folder, source: folder.path, entries: folder.entries, places: folder.places, page: 0 });
    prefs.set("save-folder", folder.path);
    $("open-search").value = "";
    drawPlaces(); drawLocation(); drawList();
    if (folder.selected) select(folder.entries.find((entry) => entry.path === folder.selected) || { name: nameOf(folder.selected), path: folder.selected });
  } catch (error) {
    if (request === F.request) $("open-list").replaceChildren(el("div", "rerr", error.message));
  }
}

async function submit() {
  if (!F.selected || F.busy) return;
  F.busy = true;
  const entry = F.selected;
  $("open-submit").disabled = true;
  $("open-submit").textContent = "Selecting…";
  $("open-dialog").setAttribute("aria-busy", "true");
  document.querySelector("#open-dialog .open-body").inert = true;
  document.querySelector("#open-dialog .open-path").inert = true;
  $("open-close").disabled = true;
  try {
    await F.load(entry.path);
    $("open-dialog").close();
  } catch (error) {
    $("open-selection").textContent = error.message;
    $("open-selection").classList.add("rerr");
  } finally {
    F.busy = false;
    $("open-submit").textContent = "Select";
    $("open-submit").disabled = !F.selected;
    $("open-close").disabled = false;
    $("open-dialog").setAttribute("aria-busy", "false");
    document.querySelector("#open-dialog .open-body").inert = false;
    document.querySelector("#open-dialog .open-path").inert = false;
  }
}

export async function chooseSimulationFile(load) {
  F.load = load;
  F.selected = null;
  const request = ++F.request;
  $("open-dialog").showModal();
  $("open-search").value = "";
  $("open-search").focus();
  select(null);
  $("open-list").textContent = "Loading…";
  try {
    const [state, home] = await Promise.all([get("/api/jobs/state"), get("/api/files")]);
    if (request !== F.request || !$("open-dialog").open) return;
    F.places = home.places;
    const batches = new Map(state.batches.map((batch) => [batch.name, batch]));
    F.saves = state.jobs.filter((job) => job.status === "done" && job.out).map((job) => {
      const batch = batches.get(job.batch);
      const details = [batch?.accuracy_pct != null ? `${fmt(batch.accuracy_pct, 2)}% target` : "", { full: "Full tree", no_rivers: "Through turn", no_turns: "Flop only" }[batch?.save]].filter(Boolean).join(" · ");
      return { name: job.batch, path: job.out, board: job.board, mtime: job.finished, details };
    });
    showCollection(F.saves.length ? "saved" : "recent");
  } catch (error) {
    if (request === F.request) $("open-list").replaceChildren(el("div", "rerr", error.message));
  }
}

export function initFiles() {
  $("open-close").onclick = () => $("open-dialog").close();
  $("open-dialog").onclose = () => { F.request++; };
  $("open-dialog").oncancel = (event) => { if (F.busy) event.preventDefault(); };
  $("open-search").oninput = () => { F.page = 0; drawList(); };
  $("open-sort").onchange = () => { F.page = 0; drawList(); };
  $("open-submit").onclick = submit;
  $("open-path-form").onsubmit = (event) => { event.preventDefault(); openFolder($("open-path").value.trim()); };
}
