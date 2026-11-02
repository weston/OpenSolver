"use strict";
/* WSolver viewer frontend: plain JS, no build step. Talks to viewer/server.py. */

const RANKS = "AKQJT98765432";
const SUITS = ["s", "h", "d", "c"];
const SUIT_GLYPH = { s: "♠", h: "♥", d: "♦", c: "♣" };
const SUIT_COLOR = { s: "#d7dce2", h: "#e26d6d", d: "#5b9ff0", c: "#57b36a" };

const state = {
  handOrder: null,      // cached [1326] combo strings
  classIdx: {},         // "AQs" -> [combo indices in hand order]
  nodeId: "r:0",
  node: null,           // {node, children}
  strat: null,          // /api/strategy response for current node
  range: null,          // /api/range response for current node
  ev: null,             // /api/ev response for current node
  view: "strategy",     // strategy | range | ev
  evPlayer: "OOP",
  selClass: null,
  solving: false,
};

const $ = (id) => document.getElementById(id);

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
const get = (u) => fetchJSON(u);
const post = (u, body) => fetchJSON(u, { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(body || {}) });

function showErr(msg) {
  const el = $("err");
  if (!msg) { el.classList.add("hidden"); return; }
  el.textContent = msg; el.classList.remove("hidden");
}

/* ---------------------------------------------------------------- hand grid helpers */

function comboClass(c) {
  const r1 = RANKS.indexOf(c[0]), r2 = RANKS.indexOf(c[2]);
  if (r1 === r2) return c[0] + c[2];
  const hi = r1 < r2 ? c[0] : c[2], lo = r1 < r2 ? c[2] : c[0];
  return hi + lo + (c[1] === c[3] ? "s" : "o");
}

function buildClassIndex(order) {
  state.classIdx = {};
  order.forEach((c, i) => {
    const k = comboClass(c);
    (state.classIdx[k] = state.classIdx[k] || []).push(i);
  });
}

function classFor(row, col) {
  if (row === col) return RANKS[row] + RANKS[col];
  return row < col ? RANKS[row] + RANKS[col] + "s" : RANKS[col] + RANKS[row] + "o";
}

/* ------------------------------------------------------------- UPI id / label helpers */

function childToken(parentId, childId) {
  // child ids extend the parent by one ':' -separated token
  const toks = childId.split(":");
  return toks[toks.length - 1];
}

function isCard(tok) {
  return tok.length === 2 && RANKS.includes(tok[0]) && "cdhs".includes(tok[1]);
}
function isFlop(tok) { return tok.length === 6 && [0, 2, 4].every((i) => isCard(tok.slice(i, i + 2))); }

function actionLabel(tok, parent, child) {
  if (tok === "f") return "Fold";
  if (tok === "c") {
    const p = parent && parent.pot, c_ = child && child.pot;
    if (p && c_ && (p[0] !== c_[0] || p[1] !== c_[1])) return "Call";
    return "Check";
  }
  if (tok.startsWith("b")) {
    const p = parent && parent.pot;
    const verb = p && p[0] !== p[1] ? "Raise" : "Bet";
    return `${verb} ${tok.slice(1)}`;
  }
  return tok;
}

function lastBlockExploit(block) {
  for (const l of block || []) if (l.startsWith("Exploitable")) return l.replace("Exploitable for:", "").trim();
  return null;
}

/* ---------------------------------------------------------------- piece colors */

const WARM = ["#d34f4f", "#e8963c", "#e0c94a", "#c96dd8", "#58c1d6", "#9ec94a"];

function actionColors(labels) {
  const colors = [];
  let warm = 0;
  for (const l of labels) {
    if (l === "Fold") colors.push("#3579f0");
    else if (l === "Call") colors.push("#69b34c");
    else if (l === "Check") colors.push("#3f8f3f");
    else colors.push(WARM[warm++ % WARM.length]);
  }
  return colors;
}

/* ---------------------------------------------------------------- navigation */

function breadcrumb(id) {
  const toks = id.split(":");
  const parts = [];
  let cur = "";
  toks.forEach((t, i) => {
    cur = i === 0 ? t : cur + ":" + t;
    parts.push({ tok: t, id: cur });
  });
  return parts;
}

async function gotoNode(id) {
  state.nodeId = id;
  state.selClass = null;
  state.strat = state.range = state.ev = null;
  try {
    showErr(null);
    state.node = await get("/api/node?id=" + encodeURIComponent(id));
    renderBreadcrumb();
    renderNodeBody();
  } catch (e) { showErr(String(e.message || e)); }
}

function renderBreadcrumb() {
  const bc = $("breadcrumb");
  bc.textContent = "";
  for (const p of breadcrumb(state.nodeId)) {
    const a = document.createElement("a");
    a.textContent = p.tok;
    a.onclick = () => gotoNode(p.id);
    bc.appendChild(a);
    const sep = document.createElement("span");
    sep.className = "sep"; sep.textContent = ":";
    bc.appendChild(sep);
  }
  const refresh = document.createElement("button");
  refresh.textContent = "⟳ refresh";
  refresh.style.marginLeft = "10px";
  refresh.onclick = () => gotoNode(state.nodeId);
  bc.appendChild(refresh);
}

/* ---------------------------------------------------------------- node body */

async function renderNodeBody() {
  const { node, children } = state.node;
  const info = $("node-info");
  const board = node.board || "—";
  info.innerHTML = `<b>${node.id}</b> &nbsp; type <b>${node.type}</b> &nbsp; board <b>${board}</b>` +
    (node.pot && node.pot.length ? ` &nbsp; pot <b>${node.pot.join(" / ")}</b> (oop/ip/dead)` : "") +
    (node.flags ? ` &nbsp; <span style="opacity:.7">${node.flags}</span>` : "");
  const body = $("node-body");
  body.textContent = "";
  $("details").innerHTML = "";

  if (node.type === "OOP_DEC" || node.type === "IP_DEC") {
    try {
      state.strat = await get("/api/strategy?id=" + encodeURIComponent(state.nodeId));
    } catch (e) { showErr(String(e.message || e)); return; }
    renderDecision(body);
  } else if ((node.type === "SPLIT_NODE" || node.type === "ROOT") && children.length) {
    renderChance(body, children);
  } else {
    body.appendChild(el("p", null, `${node.type}: end of the line.`));
    const btn = el("button", "viewtabs", "Show EVs");
    btn.onclick = async () => {
      try {
        showErr(null);
        state.view = "ev"; state.evPlayer = "OOP";
        state.ev = await get(`/api/ev?id=${encodeURIComponent(state.nodeId)}&player=${state.evPlayer}`);
        renderEv(body, true);
      } catch (e) { showErr(String(e.message || e)); }
    };
    body.appendChild(btn);
  }
}

/* decision node: action buttons + strategy/range/EV grid */

function renderDecision(body) {
  body.textContent = "";
  $("details").innerHTML = "";
  const d = state.strat;
  const labels = d.children.map((c) => actionLabel(childToken(state.nodeId, c.id), d.node, c));
  const colors = actionColors(labels);

  if (d.raw) {  // e.g. preflop node: no 1326 layout
    body.appendChild(el("pre", null, d.raw));
    return;
  }

  // aggregate overall frequencies (weighted by the acting player's reach)
  let totW = 0;
  const actW = labels.map(() => 0);
  (d.reach || []).forEach((w, i) => {
    if (!w) return;
    totW += w;
    d.freq.forEach((row, k) => { actW[k] += w * (row[i] || 0); });
  });
  const overall = actW.map((x) => (totW > 0 ? x / totW : 0));

  // action buttons
  const btns = el("div", "actions");
  d.children.forEach((c, k) => {
    const b = document.createElement("button");
    b.style.borderTop = `3px solid ${colors[k]}`;
    b.innerHTML = `<span>${labels[k]}</span><span class="freq">${(overall[k] * 100).toFixed(1)}%</span>`;
    b.onclick = () => gotoNode(c.id);
    btns.appendChild(b);
  });
  body.appendChild(btns);

  // view tabs
  const tabs = el("div", "viewtabs");
  for (const [name, title] of [["strategy", "Strategy"], ["range", "Range"], ["ev", "EV"]]) {
    const b = el("button", null, title);
    if (state.view === name) b.classList.add("active");
    b.onclick = () => switchView(name, body);
    tabs.appendChild(b);
  }
  body.appendChild(tabs);

  if (state.view === "strategy") renderStrategy(body, labels, colors, overall);
  else if (state.view === "range") switchView("range", body);
  else switchView("ev", body);
}

async function switchView(name, body) {
  state.view = name;
  const names = ["strategy", "range", "ev"];
  const tabRow = body.querySelector(".viewtabs");
  if (tabRow) tabRow.querySelectorAll("button").forEach((b, i) => b.classList.toggle("active", names[i] === name));
  const d = state.strat;
  const labels = d.children.map((c) => actionLabel(childToken(state.nodeId, c.id), d.node, c));
  const colors = actionColors(labels);
  if (name === "strategy") { renderDecision(body); return; }
  body.querySelectorAll(".dynamic").forEach((n) => n.remove());
  if (name === "range") {
    try {
      if (!state.range) { showErr(null); state.range = await get("/api/range?id=" + encodeURIComponent(state.nodeId)); }
      renderRange(body, labels, colors);
    } catch (e) { showErr(String(e.message || e)); }
  } else {
    try {
      if (!state.ev) {
        showErr(null);
        state.ev = await get(`/api/ev?id=${encodeURIComponent(state.nodeId)}&player=${state.evPlayer}`);
      }
      renderEv(body, true, labels, colors);
    } catch (e) { showErr(String(e.message || e)); }
  }
}

/* ---------------------------------------------------------------- grids */

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function baseGrid(onCell) {
  const wrap = el("div", "grid-wrap dynamic");
  const table = el("table", "grid");
  const head = el("tr");
  head.appendChild(el("th", null, ""));
  for (let c = 0; c < 13; c++) head.appendChild(el("th", null, RANKS[c]));
  table.appendChild(head);
  for (let r = 0; r < 13; r++) {
    const tr = el("tr");
    tr.appendChild(el("th", null, RANKS[r]));
    for (let c = 0; c < 13; c++) {
      const td = el("td", "cell");
      td.dataset.class = classFor(r, c);
      tr.appendChild(td);
      onCell && onCell(td);
    }
    table.appendChild(tr);
  }
  wrap.appendChild(table);
  return wrap;
}

function cellCombos(cls) { return state.classIdx[cls] || []; }

function renderStrategy(body, labels, colors, overall) {
  const d = state.strat;
  const wrap = baseGrid((td) => {
    const cls = td.dataset.class;
    const combos = cellCombos(cls);
    let w = 0;
    const agg = labels.map(() => 0);
    for (const i of combos) {
      const rw = d.reach[i] || 0;
      if (rw <= 0) continue;
      w += rw;
      for (let k = 0; k < labels.length; k++) agg[k] += rw * (d.freq[k][i] || 0);
    }
    if (w <= 0) { td.classList.add("empty"); td.appendChild(el("div", "txt", cls)); return; }
    const bar = el("div", "bar");
    let left = 0;
    agg.forEach((v, k) => {
      const f = v / w;
      const sp = document.createElement("span");
      sp.style.background = colors[k];
      sp.style.flexGrow = f.toFixed(4);
      bar.appendChild(sp);
      left += f;
    });
    td.appendChild(bar);
    td.appendChild(el("div", "txt", cls));
    td.title = `${cls} — weight ${w.toFixed(2)}`;
    td.onclick = () => { state.selClass = cls; markSel(); renderDetails(labels, colors); };
  });
  body.appendChild(wrap);

  const legend = el("div", "legend dynamic");
  labels.forEach((l, k) => {
    const item = el("div", "item");
    const sw = el("span", "swatch"); sw.style.background = colors[k];
    item.appendChild(sw);
    item.appendChild(el("span", null, `${l}: ${(overall[k] * 100).toFixed(1)}%`));
    legend.appendChild(item);
  });
  const who = el("span", null, `— ${d.actor} to act, aggregated over its range at this node`);
  who.style.color = "var(--muted)";
  legend.appendChild(who);
  body.appendChild(legend);
  renderDetails(labels, colors);
}

function markSel() {
  document.querySelectorAll(".cell").forEach((td) => td.classList.toggle("sel", td.dataset.class === state.selClass));
}

function renderDetails(labels, colors) {
  const box = $("details");
  box.innerHTML = "";
  if (!state.selClass || !state.strat || !state.strat.freq) return;
  const d = state.strat;
  const combos = cellCombos(state.selClass)
    .filter((i) => (d.reach[i] || 0) > 0)
    .sort((a, b) => d.reach[b] - d.reach[a]);
  if (!combos.length) return;
  const t = el("table");
  const hr = el("tr");
  hr.appendChild(el("th", "hand", "Hand"));
  hr.appendChild(el("th", null, "Weight"));
  labels.forEach((l) => hr.appendChild(el("th", null, l)));
  hr.appendChild(el("th", null, "EV"));
  t.appendChild(hr);
  for (const i of combos) {
    const tr = el("tr");
    const hand = el("td", "hand");
    hand.appendChild(colorCard(state.handOrder[i]));
    tr.appendChild(hand);
    tr.appendChild(el("td", null, (d.reach[i] || 0).toFixed(3)));
    labels.forEach((l, k) => {
      const v = (d.freq[k][i] || 0) * 100;
      tr.appendChild(el("td", null, v.toFixed(v < 10 && v > 0 ? 1 : 0)));
    });
    const ev = d.ev && d.ev.ev ? d.ev.ev[i] : null;
    tr.appendChild(el("td", null, ev == null ? "—" : ev.toFixed(2)));
    t.appendChild(tr);
  }
  box.appendChild(t);
}

function colorCard(combo) {
  const frag = document.createElement("span");
  for (const pos of [[0], [2]]) {
    const sp = el("span", null, combo.substr(pos[0], 2));
    sp.style.color = SUIT_COLOR[combo[pos[0] + 1]];
    frag.appendChild(sp);
  }
  return frag;
}

function shadeCell(td, text, frac, lo, hi, v) {
  td.appendChild(el("div", "fill"));
  const f = Math.max(0, Math.min(1, frac));
  const hue = lo + (hi - lo) * f;
  td.firstChild.style.background = `hsl(${hue}, 55%, 32%)`;
  td.appendChild(el("div", "txt", text));
  td.title = v == null ? td.dataset.class : `${td.dataset.class}: ${v}`;
}

function renderRange(body) {
  for (const [title, vec] of [["OOP range", state.range.oop], ["IP range", state.range.ip]]) {
    body.appendChild(el("h3", "dynamic", `${title} — reach weights at ${state.nodeId}`));
    body.appendChild(baseGrid((td) => {
      const combos = cellCombos(td.dataset.class);
      let w = 0;
      for (const i of combos) w += vec[i] || 0;
      if (w <= 0) { td.classList.add("empty"); td.appendChild(el("div", "txt", td.dataset.class)); return; }
      const n = combos.length;
      shadeCell(td, td.dataset.class, w / n, 210, 210, w.toFixed(2) + " of " + n);
    }));
  }
}

function renderEv(body, withToggle, labels, colors) {
  const data = state.ev;
  if (withToggle) {
    const tabs = el("div", "viewtabs dynamic");
    for (const p of ["OOP", "IP"]) {
      const b = el("button", null, p);
      if (state.evPlayer === p) b.classList.add("active");
      b.onclick = async () => {
        state.evPlayer = p; state.ev = null;
        try {
          state.ev = await get(`/api/ev?id=${encodeURIComponent(state.nodeId)}&player=${p}`);
          body.querySelectorAll(".evgrid, h3.dynamic.evtitle").forEach((n) => n.remove());
          renderEv(body, false, labels, colors);
        } catch (e) { showErr(String(e.message || e)); }
      };
      tabs.appendChild(b);
    }
    body.appendChild(tabs);
  }
  // class-level average EV, matchup-weighted
  const vals = {}, mur = {};
  let lo = Infinity, hi = -Infinity;
  for (const cls in state.classIdx) {
    let num = 0, den = 0;
    for (const i of cellCombos(cls)) {
      const e = data.ev[i], m = data.matchups ? data.matchups[i] : 1;
      if (e == null || m <= 0) continue;
      num += m * e; den += m;
    }
    if (den > 0) { vals[cls] = num / den; lo = Math.min(lo, vals[cls]); hi = Math.max(hi, vals[cls]); }
  }
  const title = el("h3", "dynamic evtitle", `EV for ${state.evPlayer} at ${state.nodeId}` +
    (hi > lo ? ` — ${lo.toFixed(2)} (red) … ${hi.toFixed(2)} (green)` : ""));
  body.appendChild(title);
  const wrap = baseGrid((td) => {
    const cls = td.dataset.class;
    const v = vals[cls];
    if (v == null) { td.classList.add("empty"); td.appendChild(el("div", "txt", cls)); return; }
    const f = hi > lo ? (v - lo) / (hi - lo) : 0.5;
    shadeCell(td, cls, f, 0, 140, v.toFixed(2));
  });
  wrap.classList.add("evgrid");
  body.appendChild(wrap);
}

/* ---------------------------------------------------------------- chance nodes */

function renderChance(body, children) {
  const cards = new Set(children.map((c) => childToken(state.nodeId, c.id)).filter(isCard));
  const flops = new Set(children.map((c) => childToken(state.nodeId, c.id)).filter(isFlop));
  if (cards.size) {
    body.appendChild(el("p", null, "Deal:"));
    const byCard = new Map(children.map((c) => [childToken(state.nodeId, c.id), c.id]));
    const table = el("table", "cardgrid");
    const hr = el("tr");
    hr.appendChild(el("th"));
    RANKS.split("").forEach((r) => hr.appendChild(el("th", null, r)));
    table.appendChild(hr);
    for (const s of SUITS) {
      const tr = el("tr");
      const th = el("th", null, SUIT_GLYPH[s]); th.style.color = SUIT_COLOR[s];
      tr.appendChild(th);
      for (const r of RANKS) {
        const card = r + s;
        const td = el("td");
        const b = el("button", null, r);
        b.style.color = SUIT_COLOR[s];
        if (byCard.has(card)) b.onclick = () => gotoNode(byCard.get(card));
        else b.disabled = true;
        b.title = card;
        td.appendChild(b);
        tr.appendChild(td);
      }
      table.appendChild(tr);
    }
    body.appendChild(table);
  }
  if (flops.size || !cards.size) {  // flop subsets, or an odd chance node: plain buttons
    const btns = el("div", "actions");
    for (const c of children) {
      const tok = childToken(state.nodeId, c.id);
      if (isCard(tok)) continue;
      const b = el("button", null, tok);
      b.onclick = () => gotoNode(c.id);
      btns.appendChild(b);
    }
    body.appendChild(btns);
  }
}

/* ---------------------------------------------------------------- status + polling */

async function poll() {
  try {
    const st = await get("/api/progress");
    $("hdr-status").textContent = st.version || "";
    $("hdr-tree").classList.toggle("hidden", !st.tree);
    $("hdr-solving").classList.toggle("hidden", !st.solving);
    const was = state.solving;
    state.solving = st.solving;
    const ex = lastBlockExploit(st.progress);
    $("hdr-exploit").textContent = ex ? `exploitable ${ex}` : (st.solver_status || "");
    const lines = [];
    if (st.solver_status && !st.solver_status.startsWith("stopped")) lines.push(st.solver_status);
    lines.push(...st.progress);
    if (st.solver_status && st.solver_status.startsWith("stopped")) lines.push(st.solver_status);
    $("solve-progress").textContent = lines.length ? lines.join("\n") : "not solving";
    if (st.dead) explodeDeadSolver();
    if (was && !st.solving && state.node && !$("tab-browse").classList.contains("hidden")) {
      gotoNode(state.nodeId);  // pick up freshly solved data
    }
    if (was && !st.solving && !$("tab-build").classList.contains("hidden")) {
      // solve finished: show final calc_results
      try {
        const r = await get("/api/calc_results");
        $("solve-progress").textContent = (lines.join("\n") + "\n\n" + r.text).trim();
      } catch (e) { /* tree may be gone */ }
    }
  } catch (e) { $("hdr-status").textContent = "server error"; }
}

function explodeDeadSolver() {
  const b = $("hdr-status");
  b.textContent = "solver died — restart it in the Save/load tab";
}

/* ---------------------------------------------------------------- build & solve UI */

function sizeFields() {
  const v = (id) => $(id).value.trim();
  return {
    range_oop: v("f-range-oop"), range_ip: v("f-range-ip"), board: v("f-board"),
    pot: Number(v("f-pot")), stack: Number(v("f-stack")),
    flop_bet: v("f-flop-bet"), flop_raise: v("f-flop-raise"), flop_donk: v("f-flop-donk"),
    turn_bet: v("f-turn-bet"), turn_raise: v("f-turn-raise"), turn_donk: v("f-turn-donk"),
    river_bet: v("f-river-bet"), river_raise: v("f-river-raise"), river_donk: v("f-river-donk"),
    flop_allin: $("f-flop-allin").checked, turn_allin: $("f-turn-allin").checked,
    river_allin: $("f-river-allin").checked,
    allin_threshold: Number(v("f-allin-threshold")), add_allin_limit: Number(v("f-allin-limit")),
    iso: $("f-iso").checked,
  };
}

function wireBuild() {
  $("btn-generate").onclick = async () => {
    $("build-out").textContent = "generating lines and sending them to the solver…";
    try {
      showErr(null);
      const r = await post("/api/generate", sizeFields());
      $("build-out").textContent = `${r.lines} lines generated and loaded.\n` +
        (r.sample.length < r.lines ? `first ${r.sample.length}: ` : "") + r.sample.map((l) => "add_line " + l).join("\n");
      $("btn-estimate").disabled = false;
      $("btn-build").disabled = false;
    } catch (e) { showErr(String(e.message || e)); $("build-out").textContent = ""; }
  };
  $("btn-estimate").onclick = async () => {
    try {
      showErr(null);
      const r = await get("/api/estimate");
      $("build-out").textContent = r.text;
    } catch (e) { showErr(String(e.message || e)); }
  };
  $("btn-build").onclick = async () => {
    $("build-out").textContent = "building… this can take a while for big trees";
    try {
      showErr(null);
      const r = await post("/api/build");
      $("build-out").textContent = `built: ${r.node_count} nodes`;
      $("btn-solve").disabled = false;
      $("hdr-tree").classList.remove("hidden");
    } catch (e) { showErr(String(e.message || e)); }
  };
  $("btn-solve").onclick = async () => {
    try {
      showErr(null);
      await post("/api/solve", { accuracy: Number($("f-accuracy").value) });
    } catch (e) { showErr(String(e.message || e)); }
  };
  $("btn-stop").onclick = async () => {
    try { showErr(null); await post("/api/stop"); } catch (e) { showErr(String(e.message || e)); }
  };
}

function wireIO() {
  const only = (a, b) => { $(a).onchange = () => { if ($(a).checked) $(b).checked = false; }; };
  only("f-norivers", "f-noturns"); only("f-noturns", "f-norivers");
  $("btn-load").onclick = async () => {
    try {
      showErr(null);
      await post("/api/load", { path: $("f-path").value.trim() });
      $("io-out").textContent = "loaded (lazy: streets load as you browse them)";
      switchTab("browse");
      gotoNode("r:0");
    } catch (e) { showErr(String(e.message || e)); }
  };
  $("btn-save").onclick = async () => {
    try {
      showErr(null);
      await post("/api/save", { path: $("f-path").value.trim(), no_rivers: $("f-norivers").checked,
                                no_turns: $("f-noturns").checked });
      $("io-out").textContent = "saved to " + $("f-path").value.trim();
    } catch (e) { showErr(String(e.message || e)); }
  };
  $("btn-restart").onclick = async () => {
    try {
      showErr(null);
      await post("/api/restart");
      $("solver-ver").textContent = "restarted";
    } catch (e) { showErr(String(e.message || e)); }
  };
}

/* ---------------------------------------------------------------- tabs + boot */

function switchTab(name) {
  document.querySelectorAll("button.tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  for (const id of ["browse", "build", "io"]) {
    $("tab-" + id).classList.toggle("hidden", id !== name);
  }
}

async function boot() {
  document.querySelectorAll("button.tab").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
  wireBuild(); wireIO();
  try {
    const ho = await get("/api/hand_order");
    state.handOrder = ho.order;
    buildClassIndex(ho.order);
  } catch (e) { showErr("could not get hand order: " + (e.message || e)); return; }
  const st = await get("/api/status");
  if (st.tree) gotoNode("r:0");
  else $("node-body").appendChild(el("p", null, "No tree yet: build one (Build & solve tab) or load a save (Save / load tab)."));
  setInterval(poll, 1500);
  poll();
}

boot();
