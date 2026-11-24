import { get, post, fmt, fmtChips, fmtEvPct } from "./api.js";
import { el, cardEl, comboEl, parseCards } from "./cards.js";
import { currentTree } from "./build.js";

const $ = (id) => document.getElementById(id);
let state = null, visible = false, busy = false, timer = null, sequence = 0, rendered = null, autoDealToken = null;
let requestError = "";

function fields() {
  $("drill-batch-field").classList.toggle("hidden", $("drill-source").value !== "saved");
  $("drill-live").classList.toggle("hidden", $("drill-source").value !== "live");
  $("drill-node-field").classList.toggle("hidden", $("drill-mode").value !== "node");
  $("drill-count-field").classList.toggle("hidden", $("drill-format").value !== "set");
}

function controls() {
  const active = !!state?.active, hand = state?.hand;
  $("drill-settings").disabled = busy || active;
  $("drill-start").classList.toggle("hidden", active);
  $("drill-start").disabled = busy;
  $("drill-stop").classList.toggle("hidden", !active);
  $("drill-stop").disabled = busy;
  $("drill-continue").classList.toggle("hidden", hand?.phase !== "feedback");
  $("drill-continue").disabled = busy;
  $("drill-continue").textContent = state?.config?.mode === "node" ? "Finish decision" : "Continue";
  $("drill-next").classList.toggle("hidden", !active || (hand && hand.phase !== "complete"));
  $("drill-next").disabled = busy || !state?.can_deal;
  $("drill-next").textContent = hand ? "Next hand" : "Deal hand";
  $("drill-retry").classList.toggle("hidden", !active || !state?.error);
  $("drill-retry").disabled = busy || state?.preparing;
  document.querySelectorAll("#drill-actions button").forEach((b) => { b.disabled = busy || b.dataset.answered === "yes"; });
}

function render(s) {
  state = s;
  const progress = s.progress;
  $("drill-preparing").textContent = !s.active ? "" : s.preparing
    ? `${s.ready_count} ready · Solving${progress ? ` · ${fmt(progress.pct, 2)}% · ${fmt(progress.t, 0)}s` : "…"}`
    : s.ready ? `${s.ready_count} simulation${s.ready_count === 1 ? "" : "s"} ready` : "";
  $("drill-error").textContent = s.error || requestError;
  controls();
  if (rendered === s.token) return;
  rendered = s.token;
  if (s.config) {
    for (const key of ["source", "mode", "seat", "node", "format"]) $("drill-" + key).value = s.config[key];
    if (s.config.format === "set") $("drill-count").value = s.config.count;
    if (s.config.source === "live") {
      for (const key of ["ahead", "boards", "accuracy", "seconds", "spot"]) $("drill-" + key).value = s.config[key];
    }
    if (s.config.source === "saved") $("drill-batch").value = s.config.batch;
    fields();
  }
  const hand = s.hand;
  $("drill-number").textContent = `Hand ${s.number}`;
  $("drill-number").classList.toggle("hidden", !hand);
  $("drill-stats").replaceChildren();
  if (s.active) {
    for (const [label, value] of [["Hands", s.stats.hands], ["Decisions", s.stats.decisions], ["Average EV loss", fmt(s.stats.loss_pct, 2) + "%"]]) {
      const row = el("div", "kv");
      row.append(el("div", "k", label), el("div", "v num", value));
      $("drill-stats").append(row);
    }
  }
  for (const id of ["drill-hero", "drill-villain", "drill-board", "drill-actions", "drill-feedback", "drill-history"]) $(id).replaceChildren();
  $("drill-feedback").classList.toggle("hidden", !hand?.feedback);
  $("drill-pot").textContent = hand ? `Pot ${fmtChips(hand.node.pot.reduce((a, b) => a + b, 0))}` : "";
  $("drill-result").textContent = hand?.result || (hand?.phase === "decision" ? "Your action" : "");
  $("drill-hero-seat").textContent = hand ? `You · ${hand.seat}` : "You";
  $("drill-villain-seat").textContent = hand ? `Solver · ${hand.seat === "OOP" ? "IP" : "OOP"}` : "Solver";
  if (!hand) return;
  if (hand.hero) $("drill-hero").append(comboEl(hand.hero, "lg"));
  else $("drill-hero").append(el("span", "drill-set-count", `${hand.exercises.length} hands`));
  if (hand.villain) $("drill-villain").append(comboEl(hand.villain, "lg"));
  else for (let i = 0; i < 2; i++) $("drill-villain").append(el("span", "drill-card-back"));
  for (const card of parseCards(hand.node.board)) $("drill-board").append(cardEl(card, "lg"));
  const multiple = s.config.format === "set";
  $("drill-actions").classList.toggle("hand-set", multiple);
  for (const [index, exercise] of hand.exercises.entries()) {
    if (!hand.actions.length) break;
    const row = multiple ? el("div", "drill-exercise") : $("drill-actions");
    if (multiple) row.append(comboEl(exercise.combo, ""));
    for (const action of hand.actions) {
      const b = el("button", `btn drill-action ${action.kind}`);
      b.classList.toggle("chosen", exercise.chosen === action.id);
      b.dataset.answered = exercise.chosen ? "yes" : "no";
      b.append(el("strong", "", action.label));
      if (action.amount != null) b.append(el("span", "num", fmtChips(action.amount)));
      b.onclick = () => command("action", { action: action.id, exercise: index });
      row.append(b);
    }
    if (multiple) $("drill-actions").append(row);
  }
  if (hand.feedback && multiple) {
    const table = el("table", "drill-review drill-set-review");
    const header = el("tr");
    const subheader = el("tr");
    const handLabel = el("th", "", "Hand");
    handLabel.rowSpan = 2;
    header.append(handLabel);
    for (const action of hand.feedback.hands[0].actions) {
      const label = el("th", "", action.label);
      label.colSpan = 2;
      header.append(label);
      subheader.append(el("th", "", "Frequency"), el("th", "", "EV"));
    }
    const loss = el("th", "", "EV loss");
    loss.rowSpan = 2;
    header.append(loss);
    table.append(header, subheader);
    for (const feedback of hand.feedback.hands) {
      const row = el("tr");
      const handCell = el("td");
      handCell.append(comboEl(feedback.combo, ""));
      row.append(handCell);
      for (const action of feedback.actions) {
        const chosen = action.id === feedback.chosen;
        row.append(el("td", chosen ? "selected num" : "num", (chosen ? "✓ " : "") + fmt(action.frequency * 100, 1) + "%"),
          el("td", chosen ? "selected num" : "num", fmtEvPct(action.ev, feedback.pot, 2)));
      }
      row.append(el("td", "num", fmt(feedback.loss_pct, 2) + "%"));
      table.append(row);
    }
    $("drill-feedback").append(table);
  } else if (hand.feedback) {
    for (const f of hand.feedback.hands) {
      const heading = el("div", "card-head");
      heading.append(el("h2", "", "Solver feedback"), el("span", "num", `EV loss ${fmt(f.loss_pct, 2)}%`));
      const table = el("table", "drill-review");
      const head = el("tr");
      for (const title of ["Action", "Frequency", "EV"]) head.append(el("th", "", title));
      table.append(head);
      for (const action of f.actions) {
        const row = el("tr", action.id === f.chosen ? "selected" : "");
        row.append(el("td", "", action.label + (action.id === f.chosen ? " ✓" : "")),
          el("td", "num", fmt(action.frequency * 100, 1) + "%"), el("td", "num", fmtEvPct(action.ev, f.pot, 2)));
        table.append(row);
      }
      $("drill-feedback").append(heading, table);
    }
  }
  for (const item of hand.history) {
    const row = el("span", "drill-history-item");
    if (item.board) for (const card of item.board) row.append(cardEl(card, "xs"));
    else row.textContent = `${item.actor} ${item.label}${item.amount != null ? " · " + fmtChips(item.amount) : ""}`;
    $("drill-history").append(row);
  }
  controls();
}

async function command(action, data = {}) {
  if (busy) return;
  busy = true;
  ++sequence;
  controls();
  requestError = "";
  $("drill-error").textContent = "";
  try {
    render(await post("/api/drill/" + action, { token: state?.token, ...data }));
  } catch (e) {
    requestError = e.message;
    $("drill-error").textContent = requestError;
  } finally {
    busy = false;
    controls();
    schedule();
  }
}

async function start() {
  try {
    const source = $("drill-source").value;
    const config = { source, mode: $("drill-mode").value, seat: $("drill-seat").value,
      node: $("drill-node").value.trim(), batch: $("drill-batch").value,
      format: $("drill-format").value, count: Number($("drill-count").value) };
    const spot = $("drill-spot").value;
    if (source === "live") Object.assign(config, { spot, tree: spot ? (await get("/api/library/get?kind=config&name=" + encodeURIComponent(spot))).content : currentTree(true), boards: $("drill-boards").value,
      accuracy: Number($("drill-accuracy").value), seconds: Number($("drill-seconds").value), ahead: Number($("drill-ahead").value) });
    await command("start", config);
    await firstHand();
  } catch (e) { $("drill-error").textContent = e.message; }
}

async function poll() {
  if (!visible || busy) { schedule(); return; }
  const seq = sequence;
  try {
    const s = await get("/api/drill/state");
    if (seq !== sequence || !visible) return;
    render(s);
    await firstHand();
  } catch (e) { if (seq === sequence) $("drill-error").textContent = e.message; }
  finally { schedule(); }
}

function schedule() {
  clearTimeout(timer);
  if (visible) timer = setTimeout(poll, state?.preparing ? 700 : 2000);
}

async function firstHand() {
  if (state?.active && !state.hand && state.ready && autoDealToken !== state.token) {
    autoDealToken = state.token;
    await command("next");
  }
}

export function showDrill(show) {
  visible = show;
  clearTimeout(timer);
  if (!show) return;
  poll();
  get("/api/jobs/state").then((s) => {
    const value = state?.config?.batch || $("drill-batch").value;
    $("drill-batch").replaceChildren();
    for (const batch of s.batches.filter((b) => b.done > 0)) {
      const option = el("option", "", `${batch.name} · ${batch.done}`);
      option.value = batch.name;
      $("drill-batch").append(option);
    }
    if ([...$("drill-batch").options].some((o) => o.value === value)) $("drill-batch").value = value;
  }).catch((e) => { $("drill-error").textContent = e.message; });
  get("/api/library/list?kind=config").then((s) => {
    const value = state?.config?.spot || $("drill-spot").value;
    const build = el("option", "", "Build settings");
    build.value = "";
    $("drill-spot").replaceChildren(build);
    for (const entry of s.entries) {
      const option = el("option", "", entry.name);
      option.value = entry.name;
      $("drill-spot").append(option);
    }
    if ([...$("drill-spot").options].some((o) => o.value === value)) $("drill-spot").value = value;
  }).catch((e) => { $("drill-error").textContent = e.message; });
}

export function drillHere(node) {
  if (state?.active) return;
  $("drill-source").value = "current";
  $("drill-mode").value = "node";
  $("drill-node").value = node;
  fields();
}

export function initDrill() {
  $("drill-source").onchange = fields;
  $("drill-mode").onchange = fields;
  $("drill-format").onchange = fields;
  $("drill-start").onclick = start;
  for (const action of ["stop", "next", "continue", "retry"]) $("drill-" + action).onclick = () => command(action);
  fields();
}
