import { get, post, fmt } from "./api.js";
import { el, cardEl, comboEl, parseCards } from "./cards.js";
import { mixEditor, initialFrequencies, restrictFrequencies, rememberFrequencies, feedbackCards, chipStack, actionHeader, bb } from "./drill_controls.js";

import { liveConfig, renderBoardOptions, restoreSelection, refreshNode, resetNode, selectCurrentBoard } from "./drill_setup.js";
import { openSessions } from "./drill_sessions.js";
import { drawChart } from "./chart.js";
import { drillSettings } from "./settings.js";

const $ = (id) => document.getElementById(id);
let state = null, visible = false, busy = false, timer = null, sequence = 0, rendered = null, autoDealToken = null;
let requestError = "", draftNode = "";
const drafts = new Map();
const fullDrafts = new Map();
let sizeDraft = null, sizeTimer = null;

function fields() {
  $("drill-batch-field").classList.toggle("hidden", $("drill-source").value !== "saved");
  $("drill-live").classList.toggle("hidden", $("drill-source").value !== "live");
  $("drill-node-field").classList.toggle("hidden", $("drill-mode").value !== "node");
  renderBoardOptions();
  refreshNode();
}

function controls() {
  const active = !!state?.active, hand = state?.hand;
  $("drill-setup-summary").classList.toggle("hidden", !active);
  document.querySelector(".drill-settings").classList.toggle("active", active);
  $("drill-settings").disabled = busy || active;
  $("drill-start").classList.toggle("hidden", active);
  $("drill-start").disabled = busy;
  $("drill-stop").classList.toggle("hidden", !active);
  $("drill-stop").disabled = busy;
  $("drill-continue").classList.toggle("hidden", hand?.phase !== "feedback");
  $("drill-continue").disabled = busy;
  $("drill-continue").textContent = state?.config?.mode === "node" ? "Next hand" : "Continue";
  $("drill-submit").classList.toggle("hidden", hand?.phase !== "decision");
  const resolving = !!state?.resolution?.busy;
  $("drill-browse").classList.toggle("hidden", !hand);
  $("drill-browse").disabled = busy || resolving || !!state?.resolution?.error || sizeDraft !== null;
  $("drill-submit").disabled = busy || resolving || !!state?.resolution?.error || sizeDraft !== null || !hand?.exercises.every((e, i) => e.chosen || drafts.has(i));
  $("drill-next").classList.toggle("hidden", !active || (hand && hand.phase !== "complete"));
  $("drill-next").disabled = busy || !state?.can_deal;
  $("drill-next").textContent = hand ? "Next hand" : "Deal hand";
  $("drill-retry").classList.toggle("hidden", !active || !state?.error);
  $("drill-retry").disabled = busy || state?.preparing;
  document.querySelectorAll("#drill-actions button, #drill-actions .mix-slider").forEach((b) => {
    b.disabled = busy || b.dataset.unavailable === "yes" || b.dataset.answered === "yes" || b.dataset.empty === "yes";
  });
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
  renderConvergence(s, waiting);
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

function renderConvergence(s, waiting) {
  const chart = $("drill-convergence"), convergence = s.convergence;
  chart.classList.toggle("hidden", !convergence);
  if (!convergence) return;
  if (waiting) {
    if (chart.parentElement !== $("drill-loading")) $("drill-loading").append(chart);
  } else if (chart.parentElement !== $("drill-setup").parentElement) {
    $("drill-setup").before(chart);
  }
  $("drill-chart-status").textContent = s.preparation
    ? { building: "Building", solving: "Solving", saving: "Saving" }[s.preparation.stage]
    : s.error ? "Stopped" : "Ready";
  $("drill-chart-board").classList.toggle("hidden", waiting);
  $("drill-chart-board").replaceChildren(...parseCards(convergence.board).map((card) => cardEl(card, "xs")));
  const last = convergence.points.at(-1);
  $("drill-chart-value").textContent = last ? `${fmt(100 * last.explo / convergence.pot, 2)}%` : "";
  $("drill-chart").classList.toggle("hidden", !last);
  drawChart($("drill-chart"), convergence.points, convergence.pot, convergence.target);
}

function render(s) {
  if (!!state?.active !== s.active) $("drill-setup").open = !s.active;
  state = s;
  renderPreparation(s);
  $("drill-error").textContent = s.resolution?.error || s.error || requestError;
  controls();
  if (rendered === s.token) return;
  rendered = s.token;
  if (s.config) {
    for (const key of ["source", "mode", "seat", "node", "count"]) $("drill-" + key).value = s.config[key];
    if (s.config.source === "live") {
      for (const key of ["boards", "spot"]) $("drill-" + key).value = s.config[key];
      restoreSelection(s.config);
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
  const heroIndex = hand?.seat === "OOP" ? 0 : 1;
  for (const id of ["drill-hero-wager", "drill-villain-wager"]) $(id).replaceChildren();
  if (hand) {
    const pot = hand.node.pot.reduce((a, b) => a + b, 0);
    $("drill-pot").append(chipStack(pot), el("span", "num", `Pot ${bb(pot, hand.big_blind)}`));
    for (const [id, wager] of [["drill-hero-wager", hand.wagers[heroIndex]], ["drill-villain-wager", hand.wagers[1 - heroIndex]]]) {
      if (wager > 0) $(id).append(chipStack(wager), el("span", "num", bb(wager, hand.big_blind)));
    }
  }
  $("drill-result").textContent = hand?.result || "";
  $("drill-hero-seat").textContent = hand ? `Hero · ${hand.seat}` : "Hero";
  $("drill-villain-seat").textContent = hand ? `Villain · ${hand.seat === "OOP" ? "IP" : "OOP"}` : "Villain";
  $("drill-hero-stack").textContent = hand ? bb(hand.stacks[heroIndex], hand.big_blind) : "";
  $("drill-villain-stack").textContent = hand ? bb(hand.stacks[1 - heroIndex], hand.big_blind) : "";
  if (!hand) return;
  const buttonSeat = $(hand.seat === "IP" ? "drill-hero-seat" : "drill-villain-seat");
  buttonSeat.append(el("span", "dealer-button", "BTN"));
  if (hand.continuation || hand.hero) $("drill-hero").append(comboEl(hand.continuation?.combo || hand.hero, "lg"));
  else for (let i = 0; i < 2; i++) $("drill-hero").append(el("span", "drill-card-back"));
  if (hand.villain) $("drill-villain").append(comboEl(hand.villain, "lg"));
  else for (let i = 0; i < 2; i++) $("drill-villain").append(el("span", "drill-card-back"));
  for (const card of parseCards(hand.node.board)) $("drill-board").append(cardEl(card, "lg"));
  const key = `${s.token.split(":")[0]}:${s.token.split(":")[3]}:${s.number}:${hand.node.id}`;
  if (draftNode !== key) { drafts.clear(); fullDrafts.clear(); draftNode = key; sizeDraft = null; clearTimeout(sizeTimer); }
  const set = s.config.count > 1;
  const sizes = hand.actions.filter((a) => ["bet", "raise"].includes(a.kind));
  const toggles = sizes.length > 1 && parseCards(hand.node.board).length >= 3;
  const shared = set || toggles;
  const disabled = sizeDraft || s.resolution?.disabled || hand.disabled;
  $("drill-actions").classList.toggle("hand-set", shared);
  $("drill-actions").style.setProperty("--action-count", hand.actions.length);
  if (shared && hand.actions.length) $("drill-actions").append(actionHeader(hand.actions, hand.big_blind, disabled, toggles ? toggleSize : null));
  for (const [index, exercise] of hand.exercises.entries()) {
    if (!hand.actions.length) break;
    if (!exercise.chosen && !drafts.has(index)) drafts.set(index, initialFrequencies(hand.actions, disabled));
    const row = el("div", "drill-exercise");
    if (shared) row.append(comboEl(exercise.combo, ""));
    row.append(mixEditor(hand.actions, exercise.chosen, drafts.get(index),
      (frequencies) => {
        drafts.set(index, frequencies);
        if (fullDrafts.has(index)) fullDrafts.set(index, rememberFrequencies(hand.actions, fullDrafts.get(index), frequencies, disabled));
        controls();
      }, hand.big_blind, shared, disabled));
    $("drill-actions").append(row);
  }
  if (hand.feedback) $("drill-feedback").append(feedbackCards(hand.feedback, hand.big_blind));
  renderHistory(hand);
  controls();
}

function toggleSize(id) {
  const disabled = new Set(sizeDraft || state.resolution?.disabled || state.hand.disabled);
  if (disabled.has(id)) disabled.delete(id); else disabled.add(id);
  sizeDraft = [...disabled];
  for (const [index, values] of drafts) {
    if (!fullDrafts.has(index)) fullDrafts.set(index, values);
    drafts.set(index, restrictFrequencies(state.hand.actions, fullDrafts.get(index), sizeDraft));
  }
  clearTimeout(sizeTimer);
  sizeTimer = setTimeout(() => {
    const disabled = sizeDraft;
    sizeDraft = null;
    command("sizes", { disabled });
  }, 450);
  rendered = null;
  render(state);
}

function renderHistory(hand) {
  const streets = { 0: "Preflop", 3: "Flop", 4: "Turn", 5: "River" };
  const current = parseCards(hand.node.board).length;
  const groups = new Map();
  for (const item of hand.history.filter((item) => item.actor)) {
    if (!groups.has(item.street)) groups.set(item.street, []);
    groups.get(item.street).push(item);
  }
  if (!groups.has(current)) groups.set(current, []);
  if (hand.continuation) groups.get(current).push({ ...hand.continuation, actor: hand.seat, upcoming: true });
  const previous = el("div", "previous-streets");
  for (const [street, items] of groups) {
    const section = el("div", "history-street");
    section.append(el("strong", "", streets[street]));
    const actions = el("div", "history-actions");
    for (const item of items) actions.append(el("span", `drill-history-item${item.upcoming ? " upcoming" : ""}`,
      `${item.upcoming ? "Next · " : ""}${item.label}${item.amount != null && item.kind !== "call" ? " · " + bb(item.amount, hand.big_blind) : ""}`));
    section.append(actions);
    if (street === current) $("drill-history").append(section);
    else previous.append(section);
  }
  if (previous.children.length) $("drill-history").prepend(previous);
}

async function command(action, data = {}) {
  if (busy) return;
  if (action !== "sizes") { clearTimeout(sizeTimer); sizeDraft = null; }
  busy = true;
  ++sequence;
  controls();
  requestError = "";
  $("drill-error").textContent = "";
  try {
    let result = await post("/api/drill/" + action, { token: state?.token, ...data });
    if (action === "continue" && result.hand?.phase === "complete") {
      state = result;
      result = await post("/api/drill/next", { token: result.token });
    }
    render(result);
    return result;
  } catch (e) {
    requestError = e.message;
    $("drill-error").textContent = requestError;
    if (state) render(state);
  } finally {
    busy = false;
    controls();
    schedule();
  }
}

async function start() {
  if (!$("drill-count").reportValidity()) return;
  try {
    const source = $("drill-source").value, count = Number($("drill-count").value);
    const config = { ...drillSettings(), source, mode: $("drill-mode").value, seat: $("drill-seat").value,
      node: $("drill-node").value.trim(), batch: $("drill-batch").value,
      format: count > 1 ? "set" : "single", count };
    if (source === "live") Object.assign(config, await liveConfig());
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
  if (visible) timer = setTimeout(poll, state?.preparing || state?.resolution?.busy ? 700 : 2000);
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
  refreshNode();
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
  selectCurrentBoard();
  $("drill-mode").value = "node";
  $("drill-node").value = node;
  fields();
}

export function initDrill({ opened } = {}) {
  $("drill-source").onchange = () => { resetNode(); fields(); };
  $("drill-boards").onchange = () => { resetNode(); fields(); };
  $("drill-spot").onchange = resetNode;
  $("drill-batch").onchange = resetNode;
  $("drill-sessions").onclick = () => openSessions(async (session) => {
    await command("resume", { session });
    await firstHand();
  }, opened);
  $("drill-mode").onchange = fields;
  $("drill-start").onclick = start;
  $("drill-browse").onclick = async () => {
    const result = await command("browse");
    if (result) await opened(result.browse_node);
  };
  $("drill-submit").onclick = () => command("submit", { choices: state.hand.exercises.map((e, i) => e.chosen || drafts.get(i)) });
  for (const action of ["stop", "next", "continue", "retry"]) $("drill-" + action).onclick = () => command(action);
  fields();
}
