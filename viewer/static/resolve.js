// Sizes panel (#117, made seamless): at a decision, solve from here with other sizes, or find the
// best N of a list of candidates. The result is shown in place: the tree being browsed answers from
// the re-solve at and below that node (viewer/resolve.py overlay), with the same ids, tabs and line,
// until "Restore original". Nothing is saved; the main tree itself is untouched.

import { el } from "./cards.js";
import { get, post, toast, fail, prefs, fmt, fmtEvPct } from "./api.js";
import { study, sizesHere, variantChanged, variantText } from "./study.js";
import { STREETS, renderSizes, readSizes, validateSizes, SIZE_RE, RAISE_RE, sizeParts, inBounds } from "./sizes.js";

const $ = (id) => document.getElementById(id);

const R = {
  id: null,        // the node the panel is open at
  ctx: null,       // /api/resolve/context there
  timer: null,     // status poll while a job runs
  job: null,       // {kind, node} of the job this page started
  shown: null,     // the applied re-solve ({root, sizes, ...}) last seen in /api/resolve/status
  pollSeq: 0,      // only the latest status request counts (one sent before a Start says "idle")
};

const potOf = (ctx) => ctx.pot.oop + ctx.pot.ip + ctx.pot.dead;
const sizeLabel = (s) => (/^a$/i.test(s) ? "all-in" : /^[\d.]+$/.test(s) ? s + "%" : s);

/* ------------------------------------------------------------------ the panel */

function field(cls, labelText, input) {
  const f = el("label", cls);
  if (labelText) f.appendChild(el("span", null, labelText));
  f.appendChild(input);
  return f;
}

function draw() {
  const ctx = R.ctx;
  const pop = $("rs-pop");
  pop.textContent = "";
  const raising = !!ctx.facing;

  const head = el("div", "rs-pop-head");
  head.append(el("span", `badge ${ctx.actor.toLowerCase()}`, ctx.actor),
              el("b", null, raising ? "Raise sizes here" : "Bet sizes here"),
              el("span", "muted", `${ctx.street} · pot ${fmt(potOf(ctx), 0)}`));
  const x = el("button", "rs-x", "×");
  x.title = "Close (Esc)";
  x.onclick = closeSizes;
  head.appendChild(x);
  pop.appendChild(head);

  // 1. the sizes: type them, Solve
  const row = el("div", "rs-row");
  const inp = el("input", "rs-sizes-in");
  inp.id = "rs-sizes";
  inp.spellcheck = false;
  inp.autocomplete = "off";
  inp.value = sizesHere();
  inp.placeholder = raising ? "e.g. 50, 100, a" : "e.g. 33, 75, a";
  inp.oninput = validate;
  inp.onkeydown = (e) => { if (e.key === "Enter") solve(); };
  const go = el("button", "btn primary", "Solve");
  go.id = "rs-solve";
  go.onclick = solve;
  row.append(inp, go);
  pop.appendChild(row);
  pop.appendChild(el("div", "hint rs-hint", raising
    ? "% of the pot after calling, comma-separated (2.5x for a multiple); a = all-in."
    : "% of the pot, comma-separated; a = all-in."));

  // 2. or the best N of some candidates
  const srch = el("div", "rs-row rs-find");
  const n = el("select");
  n.id = "rs-n";
  for (const k of [1, 2, 3, 4]) {
    const o = el("option", null, String(k));
    if (k === Number(prefs.get("rs-n", 2))) o.selected = true;
    n.appendChild(o);
  }
  const cand = el("input");
  cand.id = "rs-candidates";
  cand.spellcheck = false;
  cand.value = prefs.get("rs-candidates", "25, 33, 50, 75, 100, 150");
  cand.oninput = validate;
  const ai = el("input");
  ai.type = "checkbox";
  ai.id = "rs-allin";
  ai.checked = prefs.get("rs-allin", true) !== false;
  const sb = el("button", "btn", "Find best");
  sb.id = "rs-search";
  sb.onclick = search;
  srch.append(el("span", "rs-word", "Best"), n, el("span", "rs-word", "of"), cand,
              field("check rs-ai", null, ai), sb);
  srch.querySelector(".rs-ai").append(" + all-in");
  srch.querySelector(".rs-ai").title = "Every subset also offers the all-in, on top of its N sizes";
  pop.appendChild(srch);

  // 3. what else the subtree uses: the tree's own sizes on later streets, the accuracy
  const more = el("details", "rs-more");
  more.appendChild(el("summary", null, "Other sizes and accuracy"));
  const t = el("table", "sizes rs-sizes");
  t.innerHTML = "<thead><tr><th></th><th></th><th>Bet</th><th>Raise</th><th>Donk</th><th title=\"Maximum bets + raises on the street\">Cap</th><th>All-in</th></tr></thead>";
  const tb = el("tbody");
  tb.id = "rs-rows";
  t.appendChild(tb);
  renderSizes(tb, STREETS.slice(STREETS.indexOf(ctx.street)), ctx.built || prefs.get("build", {}) || {});
  tb.addEventListener("input", validate);
  const acc = el("input");
  acc.id = "rs-accuracy";
  acc.type = "number"; acc.step = "0.1"; acc.min = "0.05";
  acc.value = prefs.get("rs-accuracy", "0.5");
  acc.oninput = validate;
  const accRow = el("div", "rs-acc-row");
  const sfx = el("div", "suffix");
  sfx.append(acc, el("i", null, "% pot"));
  accRow.append(field("field", "Accuracy", sfx),
                el("p", "hint", ctx.built ? "The tree's own sizes, as it was built. Your sizes above replace "
                  + `${ctx.actor}'s ${raising ? "raises" : "bets"} on this street.` : "From the Build form (this tree was loaded "
                  + `from a file). Your sizes above replace ${ctx.actor}'s ${raising ? "raises" : "bets"} on this street.`));
  more.append(t, accRow);
  pop.appendChild(more);

  // 4. progress and the search's table
  const state = el("div", "rs-state hidden");
  state.id = "rs-state";
  pop.appendChild(state);

  pop.appendChild(el("p", "hint rs-foot", "The opponent's range here stays as solved (an unsafe re-solve, like Pio's). "
    + "The result replaces the strategies from this node down until you restore the original; nothing is saved."));
  validate();
}

function validate() {
  if (!R.ctx || !$("rs-sizes")) return false;
  const re = R.ctx.facing ? RAISE_RE : SIZE_RE;
  const bad = (v, rx) => { const p = sizeParts(v); return !p.length || p.some((x) => !rx.test(x) || !inBounds(x)); };
  const sOk = !bad($("rs-sizes").value, re);
  const cOk = !bad($("rs-candidates").value, re) && sizeParts($("rs-candidates").value).length >= 2;
  const acc = $("rs-accuracy");
  const aOk = acc.value.trim() !== "" && Number(acc.value) > 0;
  $("rs-sizes").classList.toggle("invalid", !sOk);
  $("rs-candidates").classList.toggle("invalid", !cOk);
  acc.classList.toggle("invalid", !aOk);
  const rowsOk = validateSizes($("rs-rows"));
  return { sizes: sOk && aOk && rowsOk, search: cOk && aOk && rowsOk };
}

function body() {
  const base = R.ctx.built || prefs.get("build", {}) || {};
  prefs.set("rs-accuracy", $("rs-accuracy").value);
  return Object.assign(readSizes($("rs-rows")), {
    id: R.id,
    accuracy: (Number($("rs-accuracy").value) / 100) * potOf(R.ctx),
    allin_threshold: base.allin_threshold ?? 67,
    add_allin_limit: base.add_allin_limit ?? 300,
    apply: true,
  });
}

/* ------------------------------------------------------------------ jobs */

async function start(kind, extra) {
  const b = Object.assign(body(), extra);
  const prev = R.shown;
  try {
    await post(kind === "search" ? "/api/resolve/search" : "/api/resolve/start", b);
  } catch (e) { fail(e); return; }
  R.job = { kind, node: R.id };
  // the subgame solver's tree is being replaced: until the new one is ready, the original shows
  if (prev) { R.shown = null; variantChanged(prev.root); }
  watch();
}

function solve() {
  const ok = validate();
  if (!ok.sizes) { toast("Fix the highlighted sizes", "err"); return; }
  start("resolve", { sizes: sizeParts($("rs-sizes").value).join(",") });
}

function search() {
  const ok = validate();
  if (!ok.search) { toast("Give at least two valid candidate sizes", "err"); return; }
  prefs.set("rs-candidates", $("rs-candidates").value);
  prefs.set("rs-n", Number($("rs-n").value));
  prefs.set("rs-allin", $("rs-allin").checked);
  start("search", {
    street: R.ctx.street,
    candidates: sizeParts($("rs-candidates").value).join(","),
    n: Number($("rs-n").value),
    allin: $("rs-allin").checked,
  });
}

async function stop() {
  try { await post("/api/resolve/stop"); } catch (e) { fail(e); }
  poll();
}

function watch() {
  clearTimeout(R.timer);
  poll();
}

async function poll() {
  clearTimeout(R.timer);
  const seq = ++R.pollSeq;
  let st;
  try { st = await get("/api/resolve/status"); } catch (e) { return; }
  if (seq !== R.pollSeq) return;
  // a job running from before a reload (or another tab): show its result when it ends, too
  if (st.busy && !R.job) R.job = { kind: st.kind, node: st.node };
  const before = R.shown;
  if (!st.busy) R.shown = st.applied || null;   // before drawing: the table marks the row shown
  drawBusy(st);
  drawState(st);
  if (st.busy) { R.timer = setTimeout(poll, 600); return; }
  // finished
  const job = R.job;
  R.job = null;
  if (!job) return;
  if (st.error) { toast(st.error, "err"); return; }
  if (st.applied) {
    variantChanged(st.applied.root);
    const what = variantText(st.applied);
    if (job.kind === "resolve") {
      closeSizes();
      toast(`Now showing ${what} from here. Restore original any time.`);
    } else {
      toast(`Best: ${what}. Showing it from here.`);
      const inp = $("rs-sizes");
      if (inp && R.id === st.applied.root) { inp.value = st.applied.sizes.join(", "); validate(); }
    }
  } else if (before) {
    variantChanged(before.root);
  }
}

/** The node head's "Solving…" tag, shown while a job runs (panel open or not). */
function drawBusy(st) {
  const tag = $("rs-busy");
  if (!tag) return;
  tag.classList.toggle("hidden", !st.busy);
  if (!st.busy) return;
  const ctx = st.context;
  const pot = ctx ? ctx.pot.oop + ctx.pot.ip + ctx.pot.dead : 0;
  let txt = st.kind === "search" ? `Finding sizes ${st.done}/${st.total || "…"}` : "Solving sizes…";
  const ex = (st.solve && st.solve.progress || []).find((l) => l.startsWith("Exploitable"));
  const exv = ex ? Number(ex.split(":")[1]) : null;
  if (st.kind !== "search" && exv != null && pot) txt = `Solving sizes · ${fmt((exv / pot) * 100, 2)}%`;
  tag.textContent = txt;
  tag.title = `Re-solving at ${st.node}` + (st.kind === "search" ? ` (${st.method === "brute" ? "every subset" : "greedy"})` : "");
}

/** The panel's progress line, Stop, and the search's table (Show any row). */
function drawState(st) {
  const box = $("rs-state");
  if (!box || !R.ctx) return;
  const mine = st.node === R.id;
  if (!st.busy && !(mine && st.kind === "search" && st.rows && st.rows.length) && !(mine && st.error)) {
    box.classList.add("hidden");
    setRunning(false);
    return;
  }
  box.classList.remove("hidden");
  box.textContent = "";
  setRunning(st.busy);
  if (st.busy && !mine) {
    box.appendChild(el("div", "rs-status", `Solving sizes at ${st.node}: stop it to start one here.`));
  }
  if (st.busy) {
    const bar = el("div", "rs-bar" + (st.kind === "search" && st.total ? "" : " indet"));
    const fill = el("span");
    if (st.kind === "search" && st.total) fill.style.width = Math.round((100 * st.done) / st.total) + "%";
    bar.appendChild(fill);
    const line = el("div", "rs-progress-line");
    const sbtn = el("button", "btn small danger", "Stop");
    sbtn.onclick = stop;
    line.append(bar, sbtn);
    box.appendChild(line);
  }
  if (mine && st.error) box.appendChild(el("div", "rs-status rerr", st.error));
  if (mine && st.kind === "search" && st.rows && st.rows.length) {
    const pot = potOf(R.ctx);
    // greedy's warm-up rows have fewer sizes than N: once it is done, only the N-size ones compete
    const rows = [...st.rows].filter((r) => !st.best || r.sizes.length === st.best.sizes.length)
      .sort((a, b) => b.ev - a.ev);
    const top = rows.length ? rows[0].ev : 0;
    const extra = st.allin ? ["a"] : [];
    const shown = R.shown && R.shown.root === R.id ? JSON.stringify(R.shown.sizes) : null;
    const t = el("table", "rs-table");
    t.innerHTML = `<tr><th>Sizes</th><th>${R.ctx.actor} EV</th><th>vs best</th><th></th></tr>`;
    for (const r of rows) {
      const sizes = r.sizes.concat(extra);
      const tr = el("tr");
      const c = el("td");
      for (const s of sizes) c.appendChild(el("span", "chip", sizeLabel(s)));
      const d = r.ev - top;
      const btd = el("td");
      if (!st.busy) {
        if (shown && JSON.stringify(sizes) === shown) btd.appendChild(el("span", "rs-shown", "Shown"));
        else {
          const b = el("button", "btn small", "Show");
          b.title = "Solve with these sizes to the full accuracy and show them";
          b.onclick = () => start("resolve", { sizes: sizes.join(",") });
          btd.appendChild(b);
        }
      }
      tr.append(c, el("td", "num", fmtEvPct(r.ev, pot)),
                el("td", "num delta", r === rows[0] ? "best" : ptsBehind(d, pot)), btd);
      t.appendChild(tr);
    }
    box.appendChild(t);
  }
}

/** How far a row is behind the best, in points of the pot: "−0.8 pts", "−0.04 pts" (never "−0.0"). */
function ptsBehind(d, pot) {
  const x = Math.abs((100 * d) / pot);
  return x < 0.005 ? "same" : `−${fmt(x, x < 0.1 ? 2 : 1)} pts`;
}

function setRunning(busy) {
  for (const id of ["rs-solve", "rs-search"]) { const b = $(id); if (b) b.disabled = !!busy; }
}

/* ------------------------------------------------------------------ public */

export async function openSizes(nodeId) {
  const pop = $("rs-pop");
  if (!pop.classList.contains("hidden") && R.id === nodeId) { closeSizes(); return; }
  let ctx;
  try { ctx = await get("/api/resolve/context?id=" + encodeURIComponent(nodeId)); } catch (e) { fail(e); return; }
  if (study.id !== nodeId) return;   // moved on meanwhile
  R.id = nodeId;
  R.ctx = ctx;
  draw();
  pop.classList.remove("hidden");
  $("btn-resolve").classList.add("active");
  const inp = $("rs-sizes");
  inp.focus();
  inp.select();
  poll();
}

export function closeSizes() {
  $("rs-pop").classList.add("hidden");
  $("btn-resolve").classList.remove("active");
  R.id = null;
}

async function restore() {
  let r;
  try { r = await post("/api/resolve/restore"); } catch (e) { fail(e); return; }
  R.shown = null;
  closeSizes();
  if (r.restored) variantChanged(r.restored);
  toast("Original solve restored.");
}

export function initResolve() {
  $("btn-restore").onclick = restore;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("rs-pop").classList.contains("hidden")) { e.stopPropagation(); closeSizes(); }
  }, true);
  // the panel belongs to one node: moving away closes it
  document.addEventListener("study-node", (e) => {
    if (R.id && e.detail !== R.id) closeSizes();
  });
  // a job may be running, or a re-solve applied, from before a reload
  poll();
}
