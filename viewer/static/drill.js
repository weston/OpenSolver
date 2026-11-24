import { get, post, fmt, fmtChips } from "./api.js";
import { el, cardEl, comboEl, parseCards } from "./cards.js";
import { currentTree } from "./build.js";
import { drillSettings } from "./settings.js";
import { mixEditor, feedbackCards, chipStack } from "./drill_controls.js";

const $ = (id) => document.getElementById(id);
let state = null, visible = false, busy = false, timer = null, sequence = 0, rendered = null, autoDealToken = null;
let requestError = "", draftNode = "";
const drafts = new Map();

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

function renderPreparation(s) {
  const progress = s.progress, preparation = s.preparation;
  const stage = preparation?.stage || (progress ? "solving" : "building");
  const titles = { building: "Building tree", solving: "Solving simulation", saving: "Saving simulation" };
  const live = s.config?.source === "live";
  const title = live ? titles[stage] : "Loading simulation";
  $("drill-preparing").textContent = !s.active ? "" : s.preparing
    ? `${s.ready_count} ready · ${title}${preparation ? ` · ${fmt(preparation.elapsed, 0)}s` : "…"}`
    : s.ready ? `${s.ready_count} simulation${s.ready_count === 1 ? "" : "s"} ready` : "";
  const waiting = s.active && !s.hand && !s.error && !requestError;
  $("drill-loading").classList.toggle("hidden", !waiting);
  $("drill-table").classList.toggle("hidden", waiting);
  $("drill-play").setAttribute("aria-busy", String(waiting));
  if (!waiting) return;
  $("drill-loading-title").textContent = s.ready ? "Dealing hand" : title;
  const board = s.ready ? "" : preparation?.board || "";
  $("drill-loading-board").replaceChildren(...parseCards(board).map((card) => cardEl(card, "lg")));
  $("drill-loading-steps").classList.toggle("hidden", !live);
  const index = s.ready ? 3 : ["building", "solving", "saving"].indexOf(stage);
  [...$("drill-loading-steps").children].forEach((step, i) => {
    step.classList.toggle("active", i === index);
    step.classList.toggle("done", i < index);
  });
  $("drill-loading-stats").replaceChildren();
  if (s.ready) return;
  const stats = [];
  if (preparation) stats.push(["Elapsed", fmt(preparation.elapsed, 0) + "s"]);
  if (progress) stats.push(["Exploitability", fmt(progress.pct, 2) + "%"]);
  if (live && stage !== "building") stats.push(["Target", fmt(s.config.accuracy, 2) + "%"]);
  for (const [label, value] of stats) {
    const row = el("div", "kv");
    row.append(el("div", "k", label), el("div", "v num", value));
    $("drill-loading-stats").append(row);
  }
}

function render(s) {
  state = s;
  renderPreparation(s);
  $("drill-error").textContent = s.error || requestError;
  controls();
  if (rendered === s.token) return;
  rendered = s.token;
  if (s.config) {
    for (const key of ["source", "mode", "seat", "node", "format"]) $("drill-" + key).value = s.config[key];
    if (s.config.format === "set") $("drill-count").value = s.config.count;
    if (s.config.source === "live") {
      for (const key of ["boards", "spot"]) $("drill-" + key).value = s.config[key];
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
  $("drill-pot").replaceChildren();
  for (const id of ["drill-hero-wager", "drill-villain-wager"]) $(id).replaceChildren();
  if (hand) {
    const pot = hand.node.pot.reduce((a, b) => a + b, 0);
    $("drill-pot").append(chipStack(pot), el("span", "num", `Pot ${fmtChips(pot)}`));
    const player = hand.seat === "OOP" ? 0 : 1;
    for (const [id, wager] of [["drill-hero-wager", hand.wagers[player]], ["drill-villain-wager", hand.wagers[1 - player]]]) {
      if (wager > 0) $(id).append(chipStack(wager), el("span", "num", fmtChips(wager)));
    }
  }
  $("drill-result").textContent = hand?.result || (hand?.phase === "decision" ? "Your action" : "");
  $("drill-hero-seat").textContent = hand ? `You · ${hand.seat}` : "You";
  $("drill-villain-seat").textContent = hand ? `Solver · ${hand.seat === "OOP" ? "IP" : "OOP"}` : "Solver";
  if (!hand) return;
  if (hand.continuation || hand.hero) $("drill-hero").append(comboEl(hand.continuation?.combo || hand.hero, "lg"));
  else $("drill-hero").append(el("span", "drill-set-count", `${hand.exercises.length} hands`));
  if (hand.villain) $("drill-villain").append(comboEl(hand.villain, "lg"));
  else for (let i = 0; i < 2; i++) $("drill-villain").append(el("span", "drill-card-back"));
  for (const card of parseCards(hand.node.board)) $("drill-board").append(cardEl(card, "lg"));
  const key = `${s.token.split(":")[0]}:${s.number}:${hand.node.id}`;
  if (draftNode !== key) { drafts.clear(); draftNode = key; }
  $("drill-actions").classList.toggle("hand-set", s.config.format === "set");
  for (const [index, exercise] of hand.exercises.entries()) {
    if (!hand.actions.length) break;
    const row = el("div", "drill-exercise");
    if (s.config.format === "set") row.append(comboEl(exercise.combo, ""));
    row.append(mixEditor(hand.actions, exercise.chosen, drafts.get(index),
      (frequencies) => drafts.set(index, frequencies),
      (frequencies) => command("action", { frequencies, exercise: index })));
    $("drill-actions").append(row);
  }
  if (hand.feedback) $("drill-feedback").append(feedbackCards(hand.feedback, hand.continuation));
  for (const item of hand.history) {
    const row = el("span", "drill-history-item");
    if (item.board) for (const card of item.board) row.append(cardEl(card, "xs"));
    else row.textContent = `${item.actor} ${item.label}${item.amount != null ? " · " + fmtChips(item.amount) : ""}`;
    $("drill-history").append(row);
  }
  if (hand.continuation) {
    const action = hand.continuation;
    $("drill-history").append(el("span", "drill-history-item upcoming",
      `Next · ${hand.seat} ${action.label}${action.amount != null ? " · " + fmtChips(action.amount) : ""}`));
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
    if (state) renderPreparation(state);
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
    if (source === "live") Object.assign(config, drillSettings(), {
      spot, tree: spot ? (await get("/api/library/get?kind=config&name=" + encodeURIComponent(spot))).content : currentTree(true),
      boards: $("drill-boards").value });
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
    const build = el("option", "", "Current tree");
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
  $("drill-source").value = "live";
  $("drill-spot").value = "";
  $("drill-boards").value = "fixed";
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
