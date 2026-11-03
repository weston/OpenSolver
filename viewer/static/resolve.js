// Re-solve dialog (#117): re-solve the subtree at a decision node with edited sizes, or search
// the best N-size subset of candidate bet sizes. Backend: viewer/resolve.py (/api/resolve/*),
// a second solver process; the main tree is untouched. Results are browsed in the Study view.

import { el, cardEl, parseCards } from "./cards.js";
import { get, post, toast, fail, prefs, fmt, fmtChips } from "./api.js";
import { enterSubgame } from "./study.js";
import { STREETS, renderSizes, readSizes, validateSizes, SIZE_RE, RAISE_RE } from "./sizes.js";

const $ = (id) => document.getElementById(id);

const R = { id: null, ctx: null, tab: "search", timer: null, kind: null };

function potOf(ctx) { return ctx.pot.oop + ctx.pot.ip + ctx.pot.dead; }

function drawContext(ctx) {
  const box = $("rs-context");
  box.textContent = "";
  box.appendChild(el("span", `badge ${ctx.actor.toLowerCase()}`, ctx.actor));
  const b = el("span", "pv-board");
  for (const c of parseCards(ctx.board)) b.appendChild(cardEl(c, "xs"));
  box.appendChild(b);
  const t = el("span");
  t.innerHTML = `${ctx.street} · pot <b>${fmtChips(potOf(ctx))}</b> · stack <b>${fmtChips(ctx.stack)}</b> · node <b></b>`;
  t.querySelector("b:last-child").textContent = ctx.id;
  box.appendChild(t);
}

function drawRows(ctx) {
  const saved = prefs.get("build", {}) || {};
  const first = STREETS.indexOf(ctx.street);
  renderSizes($("rs-rows"), STREETS.slice(first), saved);
  const sel = $("rs-street");
  sel.textContent = "";
  for (const st of STREETS.slice(first)) {
    const o = el("option", null, st[0].toUpperCase() + st.slice(1));
    o.value = st;
    sel.appendChild(o);
  }
}

function validate() {
  const ok = validateSizes($("rs-rows"));
  const cand = $("rs-candidates");
  const raising = candidatesAreRaises();
  const who = R.ctx ? R.ctx.actor : "";
  $("rs-cand-label").textContent = `Candidate ${raising ? "raise" : "bet"} sizes for ${who}`;
  const cre = raising ? RAISE_RE : SIZE_RE;
  const cbad = cand.value.split(",").map((x) => x.trim()).filter(Boolean).some((p) => !cre.test(p));
  cand.classList.toggle("invalid", cbad);
  return ok && (R.tab !== "search" || !cbad);
}

/** At a node facing a bet, a search on its own street varies the raise sizes (viewer/resolve.py). */
function candidatesAreRaises() {
  return !!(R.ctx && R.ctx.facing && $("rs-street").value === R.ctx.street);
}

function body() {
  const saved = prefs.get("build", {}) || {};
  const b = {
    id: R.id,
    accuracy: (Number($("rs-accuracy").value) / 100) * potOf(R.ctx),
    allin_threshold: saved.allin_threshold ?? 67,
    add_allin_limit: saved.add_allin_limit ?? 300,
  };
  return Object.assign(b, readSizes($("rs-rows")));
}

/* ------------------------------------------------------------------ run + progress */

async function go() {
  if (!validate()) { toast("Fix the highlighted sizes", "err"); return; }
  const b = body();
  prefs.set("rs-accuracy", $("rs-accuracy").value);
  try {
    if (R.tab === "search") {
      b.street = $("rs-street").value;
      b.candidates = $("rs-candidates").value.replace(/\s+/g, "");
      b.n = Number($("rs-n").value);
      prefs.set("rs-candidates", $("rs-candidates").value);
      await post("/api/resolve/search", b);
    } else {
      await post("/api/resolve/start", b);
    }
    R.kind = R.tab;
    $("rs-next").classList.add("hidden");
    poll();
  } catch (e) { fail(e); }
}

/** Largest first (owner, #141). Percentages by value; geometric/multiple/all-in after them. */
function largestFirst(sizes) {
  const key = (s) => (/^a$/i.test(s) ? 1e9 : /x$/i.test(s) ? 1e6 + parseFloat(s) : /e$/i.test(s) ? 1e3 - (parseFloat(s) || 0) : parseFloat(s));
  return [...sizes].sort((a, b) => key(b) - key(a));
}

function sizeChips(sizes) {
  return largestFirst(sizes).map((s) => `<span class="chip">${/^\d/.test(s) && !/[ex]$/i.test(s) ? s + "%" : s}</span>`).join("");
}

async function poll() {
  clearTimeout(R.timer);
  let st;
  try { st = await get("/api/resolve/status"); } catch (e) { fail(e); return; }
  if (!$("resolve-dialog").open) return;
  $("rs-progress").classList.remove("hidden");
  $("rs-stop").classList.toggle("hidden", !st.busy);
  $("rs-go").classList.toggle("hidden", !!st.busy);
  $("rs-go").disabled = st.busy;
  const bar = $("rs-bar-fill").parentElement;
  const status = $("rs-status");
  if (st.kind === "search" && st.total) {
    bar.classList.remove("indet");
    $("rs-bar-fill").style.width = Math.round((100 * st.done) / st.total) + "%";
    status.innerHTML = `${st.busy ? "Searching" : "Searched"} <b>${st.done}</b> / ${st.total} solves · ${st.method === "brute" ? "every subset" : "greedy forward selection"}`;
  } else {
    bar.classList.toggle("indet", !!st.busy);
    $("rs-bar-fill").style.width = st.busy ? "" : "100%";
    const ex = (st.solve && st.solve.progress || []).find((l) => l.startsWith("Exploitable"));
    const pot = R.ctx ? potOf(R.ctx) : 0;
    const exv = ex ? Number(ex.split(":")[1]) : null;
    status.innerHTML = st.busy ? `Re-solving${exv != null && pot ? ` · <b>${fmt((exv / pot) * 100, 2)}%</b> of the pot exploitable` : "…"}` : "";
  }
  if (st.error) {
    status.textContent = "";
    status.appendChild(el("span", "rerr", st.error));
  }

  // results
  const t = $("rs-table");
  t.textContent = "";
  const actor = st.context ? st.context.actor : R.ctx && R.ctx.actor;
  if (st.kind === "search" && st.rows && st.rows.length) {
    const isBest = (r) => (st.best && JSON.stringify(st.best.sizes) === JSON.stringify(r.sizes) ? 1 : 0);
    const rows = [...st.rows].sort((a, b) => b.ev - a.ev || isBest(b) - isBest(a));
    const top = rows[0].ev;
    t.innerHTML = `<tr><th>Sizes</th><th>${actor} EV</th><th>vs best</th><th>EV OOP</th><th>EV IP</th></tr>`;
    for (const r of rows) {
      const tr = el("tr");
      if (st.best && JSON.stringify(st.best.sizes) === JSON.stringify(r.sizes)) tr.className = "best";
      tr.innerHTML = `<td>${sizeChips(r.sizes)}</td><td class="num">${fmtChips(r.ev)}</td><td class="num delta">${r.ev === top ? "—" : fmt(r.ev - top, 2)}</td><td class="num">${fmtChips(r.ev_oop)}</td><td class="num">${fmtChips(r.ev_ip)}</td>`;
      t.appendChild(tr);
    }
  } else if (!st.busy && st.result && st.result.ev) {
    t.innerHTML = `<tr><th>Re-solved</th><th>EV OOP</th><th>EV IP</th><th>Nodes</th></tr>`;
    const tr = el("tr");
    const nodes = /TOTAL\D*(\d+)/.exec(st.result.node_count || "");
    tr.innerHTML = `<td>${st.result.lines} lines</td><td class="num">${fmtChips(st.result.ev.OOP)}</td><td class="num">${fmtChips(st.result.ev.IP)}</td><td class="num">${nodes ? Number(nodes[1]).toLocaleString() : "—"}</td>`;
    t.appendChild(tr);
  }
  if (!st.busy && st.tree && st.result && !st.error) {
    $("rs-next").classList.remove("hidden");
    $("rs-next-what").textContent = st.kind === "search" && st.best
      ? `Best: ${largestFirst(st.best.sizes).map((x) => (/^\d/.test(x) && !/[ex]$/i.test(x) ? x + "%" : x)).join(" · ")}. Opening it closes this dialog and browses the re-solved subtree.`
      : "Opening it closes this dialog and browses the re-solved subtree.";
    R.result = st.result;
    R.best = st.best;
  }
  if (st.busy) R.timer = setTimeout(poll, 600);
}

function browse() {
  const r = R.result;
  if (!r) return;
  const label = R.best ? largestFirst(R.best.sizes).map((s) => (/^\d/.test(s) && !/[ex]$/i.test(s) ? s + "%" : s)).join(" · ") : "edited sizes";
  $("resolve-dialog").close();
  enterSubgame({ origin: R.id, root: r.subgame_root, label });
}

/* ------------------------------------------------------------------ public */

export async function openResolve(nodeId) {
  try {
    R.ctx = await get("/api/resolve/context?id=" + encodeURIComponent(nodeId));
  } catch (e) { fail(e); return; }
  R.id = nodeId;
  R.result = null; R.best = null;
  drawContext(R.ctx);
  drawRows(R.ctx);
  $("rs-accuracy").value = prefs.get("rs-accuracy", "0.5");
  $("rs-candidates").value = prefs.get("rs-candidates", "25, 33, 50, 75, 100, 150");
  $("rs-progress").classList.add("hidden");
  $("rs-next").classList.add("hidden");
  $("rs-stop").classList.add("hidden");
  $("rs-go").disabled = false;
  $("rs-go").classList.remove("hidden");
  selectTab("search");
  $("resolve-dialog").showModal();
  // a job may already be running (dialog closed and reopened)
  try {
    const st = await get("/api/resolve/status");
    if (st.busy || (st.result && st.node === nodeId)) poll();
  } catch (e) { /* status is optional here */ }
}

/** "search" (Find best sizes, the default each time the dialog opens) or "edit". */
function selectTab(tab) {
  R.tab = tab;
  $("rs-tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $("rs-edit").classList.toggle("hidden", tab !== "edit");
  $("rs-search").classList.toggle("hidden", tab !== "search");
  $("rs-go").textContent = tab === "search" ? "Search" : "Re-solve";
  validate();
}

export function initResolve() {
  $("rs-tabs").querySelectorAll("button").forEach((b) => { b.onclick = () => selectTab(b.dataset.tab); });
  $("rs-candidates").addEventListener("input", validate);
  $("rs-street").addEventListener("change", validate);
  $("rs-rows").addEventListener("input", validate);
  $("rs-go").onclick = go;
  $("rs-browse").onclick = browse;
  $("rs-close").onclick = () => $("resolve-dialog").close();
  $("rs-stop").onclick = async () => { try { await post("/api/resolve/stop"); poll(); } catch (e) { fail(e); } };
}
