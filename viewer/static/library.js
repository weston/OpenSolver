// Library dialog (#116): saved ranges and spot configs in folders, stored by the server under
// ~/.wsolver/library (viewer/library.py). One dialog serves both kinds and both directions.

import { H, el, cardEl, parseCards } from "./cards.js";
import { parseRange, rangeStats } from "./ranges.js";
import { get, post, toast, fail, fmt } from "./api.js";

const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;

const L = {
  kind: "range",     // range | config
  mode: "load",      // load | save
  entries: [],
  selected: null,
  open: new Set(),   // expanded folders
  onLoad: null,      // (content, name) => void
  content: null,     // what "save" writes
  armedDelete: null,
};

/* ------------------------------------------------------------------ tree */

function treeOf(entries, query) {
  const root = { folders: new Map(), items: [] };
  const ql = query.trim().toLowerCase();
  for (const e of entries) {
    if (ql && !e.name.toLowerCase().includes(ql)) continue;
    const parts = e.name.split("/");
    let node = root, path = "";
    for (const p of parts.slice(0, -1)) {
      path = path ? path + "/" + p : p;
      if (!node.folders.has(p)) node.folders.set(p, { path, folders: new Map(), items: [] });
      node = node.folders.get(p);
    }
    node.items.push({ ...e, leaf: parts[parts.length - 1] });
  }
  return root;
}

function drawList() {
  const box = $("lib-list");
  box.textContent = "";
  const q = $("lib-search").value;
  const tree = treeOf(L.entries, q);
  const forceOpen = q.trim().length > 0;
  const walk = (node, depth) => {
    for (const [name, f] of [...node.folders.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const open = forceOpen || L.open.has(f.path);
      const row = el("button", "lib-row folder");
      row.style.paddingLeft = 10 + depth * 16 + "px";
      row.append(el("span", "chev2", open ? "▾" : "▸"), el("span", "folder-ico"), el("span", "nm", name));
      row.onclick = () => { if (L.open.has(f.path)) L.open.delete(f.path); else L.open.add(f.path); drawList(); };
      box.appendChild(row);
      if (open) walk(f, depth + 1);
    }
    for (const it of node.items.sort((a, b) => a.leaf.localeCompare(b.leaf))) {
      const row = el("button", "lib-row item" + (it.name === L.selected ? " sel" : ""));
      row.style.paddingLeft = 10 + depth * 16 + 16 + "px";
      row.append(el("span", L.kind === "range" ? "item-ico range" : "item-ico spot"), el("span", "nm", it.leaf));
      row.onclick = () => select(it.name);
      row.ondblclick = () => { select(it.name); primary(); };
      box.appendChild(row);
    }
  };
  walk(tree, 0);
  if (!box.children.length) {
    box.appendChild(el("div", "lib-empty", L.entries.length ? "No matches" :
      L.kind === "range" ? "No saved ranges yet.\nSave one from a range editor." : "No saved spots yet.\nSave the current setup to start a library."));
  }
}

async function select(name) {
  L.selected = name;
  L.armedDelete = null;
  drawList();
  updateButtons();
  if (L.mode === "save") $("lib-name").value = name;
  const pv = $("lib-preview");
  pv.textContent = "";
  try {
    const r = await get(`/api/library/get?kind=${L.kind}&name=${enc(name)}`);
    if (L.selected !== name) return;
    pv.appendChild(el("div", "pv-title", name.split("/").pop()));
    pv.appendChild(el("div", "pv-path muted", name.includes("/") ? name.slice(0, name.lastIndexOf("/")) : "Library root"));
    if (L.kind === "range") previewRange(pv, r.content);
    else previewConfig(pv, r.content);
  } catch (e) { fail(e); }
}

function miniMatrix(w, color) {
  const mm = el("div", "mini-matrix");
  for (const cls of H.classes) {
    const idx = H.byClass[cls];
    let s = 0;
    for (const i of idx) s += w[i];
    const c = el("div"), f = el("i");
    f.style.height = ((s / idx.length) * 100).toFixed(1) + "%";
    f.style.background = color;
    c.appendChild(f);
    mm.appendChild(c);
  }
  return mm;
}

function previewRange(pv, text) {
  try {
    const w = parseRange(text.trim());
    const st = rangeStats(w);
    pv.appendChild(el("div", "pv-stats", `${fmt(st.combos, 0)} combos · ${fmt(st.pct)}% of hands`));
    pv.appendChild(miniMatrix(w, "var(--accent)"));
  } catch (e) {
    pv.appendChild(el("div", "rerr", "Not a valid range: " + e.message));
  }
  pv.appendChild(el("pre", "pv-text", text.trim()));
}

function previewConfig(pv, c) {
  const b = el("div", "pv-board");
  for (const card of parseCards(c.board || "")) b.appendChild(cardEl(card, "sm"));
  pv.appendChild(b);
  pv.appendChild(el("div", "pv-stats", `Pot ${c.pot ?? "—"} · Stack ${c.stack ?? "—"}` + (c.pot && c.stack ? ` · SPR ${(c.stack / c.pot).toFixed(2)}` : "")));
  const pair = el("div", "pv-pair");
  for (const [k, color, label] of [["range_oop", "var(--oop)", "OOP"], ["range_ip", "var(--ip)", "IP"]]) {
    const col = el("div");
    try {
      const w = parseRange(c[k] || "");
      col.append(el("div", "pv-sub", `${label} · ${fmt(rangeStats(w).combos, 0)} combos`), miniMatrix(w, color));
    } catch (e) { col.appendChild(el("div", "rerr", `${label}: ${e.message}`)); }
    pair.appendChild(col);
  }
  pv.appendChild(pair);
  const t = el("table", "pv-sizes");
  t.innerHTML = "<tr><th></th><th>Bet</th><th>Raise</th><th>All-in</th></tr>";
  for (const st of ["flop", "turn", "river"]) {
    const tr = el("tr");
    tr.append(el("th", null, st[0].toUpperCase() + st.slice(1)), el("td", null, c[`${st}_bet`] || "—"),
      el("td", null, c[`${st}_raise`] || "—"), el("td", null, c[`${st}_allin`] ? "yes" : "—"));
    t.appendChild(tr);
  }
  pv.appendChild(t);
}

/* ------------------------------------------------------------------ actions */

function updateButtons() {
  const sel = !!L.selected;
  $("lib-rename").disabled = !sel;
  $("lib-delete").disabled = !sel;
  $("lib-delete").textContent = L.armedDelete === L.selected && sel ? "Confirm delete" : "Delete";
  $("lib-delete").classList.toggle("danger", L.armedDelete === L.selected && sel);
  if (L.mode === "load") {
    $("lib-ok").disabled = !sel;
    $("lib-ok").textContent = "Load";
  } else {
    const name = $("lib-name").value.trim();
    $("lib-ok").disabled = !name;
    $("lib-ok").textContent = L.entries.some((e) => e.name === name) ? "Overwrite" : "Save";
  }
}

async function refresh() {
  try {
    L.entries = (await get(`/api/library/list?kind=${L.kind}`)).entries;
  } catch (e) { fail(e); L.entries = []; }
  drawList();
  updateButtons();
}

async function primary() {
  try {
    if (L.mode === "load") {
      if (!L.selected) return;
      const r = await get(`/api/library/get?kind=${L.kind}&name=${enc(L.selected)}`);
      $("lib-dialog").close();
      L.onLoad && L.onLoad(r.content, L.selected);
    } else {
      const name = $("lib-name").value.trim().replace(/^\/+|\/+$/g, "");
      if (!name) return;
      await post("/api/library/save", { kind: L.kind, name, content: L.content });
      $("lib-dialog").close();
      toast(`Saved “${name}”`);
      L.onLoad && L.onLoad(L.content, name);
    }
  } catch (e) { fail(e); }
}

async function rename() {
  if (!L.selected) return;
  const row = $("lib-list").querySelector(".lib-row.sel");
  if (!row) return;
  const inp = el("input", "lib-rename-input");
  inp.value = L.selected;
  row.replaceWith(inp);
  inp.focus();
  inp.setSelectionRange(L.selected.lastIndexOf("/") + 1, L.selected.length);
  const done = async (commit) => {
    inp.onblur = null;
    const to = inp.value.trim().replace(/^\/+|\/+$/g, "");
    if (commit && to && to !== L.selected) {
      try {
        await post("/api/library/rename", { kind: L.kind, from: L.selected, to });
        L.selected = to;
        for (let p = to; p.includes("/"); p = p.slice(0, p.lastIndexOf("/"))) L.open.add(p.slice(0, p.lastIndexOf("/")));
      } catch (e) { fail(e); }
    }
    await refresh();
    if (L.selected) select(L.selected);
  };
  inp.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); done(true); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(false); }
  };
  inp.onblur = () => done(true);
}

async function del() {
  if (!L.selected) return;
  if (L.armedDelete !== L.selected) {
    L.armedDelete = L.selected;
    updateButtons();
    setTimeout(() => { if (L.armedDelete) { L.armedDelete = null; updateButtons(); } }, 3000);
    return;
  }
  try {
    await post("/api/library/delete", { kind: L.kind, name: L.selected });
    toast(`Deleted “${L.selected}”`);
    L.selected = null;
    L.armedDelete = null;
    $("lib-preview").textContent = "";
    await refresh();
  } catch (e) { fail(e); }
}

/* ------------------------------------------------------------------ public */

/**
 * Open the library.
 *   kind: "range" | "config";  mode: "load" | "save"
 *   onLoad(content, name): called with the loaded content (load) or after saving (save)
 *   content: what to save (save mode); suggest: a default name (save mode)
 */
export async function openLibrary({ kind, mode, onLoad, content = null, suggest = "" }) {
  Object.assign(L, { kind, mode, onLoad, content, selected: null, armedDelete: null });
  $("lib-title").textContent = (mode === "load" ? "Load " : "Save ") + (kind === "range" ? "range" : "spot");
  $("lib-save-row").classList.toggle("hidden", mode !== "save");
  $("lib-name").value = suggest;
  $("lib-search").value = "";
  $("lib-preview").textContent = "";
  if (mode === "save" && content != null) {
    const pv = $("lib-preview");
    pv.appendChild(el("div", "pv-title", "Saving"));
    if (kind === "range") previewRange(pv, content); else previewConfig(pv, content);
  }
  $("lib-dialog").showModal();
  (mode === "save" ? $("lib-name") : $("lib-search")).focus();
  await refresh();
}

export function initLibrary() {
  $("lib-search").addEventListener("input", drawList);
  $("lib-name").addEventListener("input", updateButtons);
  $("lib-name").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); primary(); } });
  $("lib-ok").onclick = primary;
  $("lib-cancel").onclick = () => $("lib-dialog").close();
  $("lib-rename").onclick = rename;
  $("lib-delete").onclick = del;
}
