import { post } from "./api.js";
import { el, parseCards, cardEl } from "./cards.js";
import { feedbackCards } from "./drill_controls.js";

const SOURCE_NAMES = { live: "On demand", saved: "Pre-run", current: "Current tree" };
const sessionName = session => session.settings.spot || session.settings.batch || "Current tree";

export function initSessions(container, { onResume, onOpen, onAnalyze }) {
  const browser = el("div", "session-browser"), content = el("div", "session-reviews hidden");
  const filters = el("div", "session-filters"), search = el("input"), source = el("select");
  search.type = "search"; search.placeholder = "Search spots or batches"; search.setAttribute("aria-label", "Search sessions");
  for (const [value, label] of [["", "All sources"], ...Object.entries(SOURCE_NAMES)]) {
    const option = el("option", "", label); option.value = value; source.append(option);
  }
  source.setAttribute("aria-label", "Session source");
  filters.append(search, source);
  const dates = {};
  for (const [key, label] of [["since", "From"], ["until", "Through"]]) {
    const field = el("label", "session-date", label), input = el("input"); input.type = "date";
    input.setAttribute("aria-label", label + " date"); field.append(input); filters.append(field); dates[key] = input;
  }
  const toolbar = el("div", "session-toolbar"), count = el("strong"), all = el("button", "btn small", "Select matching");
  const clear = el("button", "btn small", "Clear selection"), analyze = el("button", "btn primary small", "Analyze selected");
  toolbar.append(count, all, clear, analyze);
  const tableWrap = el("div", "session-table-wrap"), table = el("table", "session-table");
  const head = el("thead"), heading = el("tr"), selectPage = el("input"); selectPage.type = "checkbox";
  selectPage.setAttribute("aria-label", "Select this page");
  const selectionCell = el("th"); selectionCell.append(selectPage); heading.append(selectionCell);
  for (const label of ["Started", "Spot", "Source", "Hands", ""]) heading.append(el("th", "", label));
  head.append(heading);
  const rows = el("tbody"); table.append(head, rows); tableWrap.append(table);
  const pager = el("div", "session-pagination"), previous = el("button", "btn small", "Previous"), next = el("button", "btn small", "Next"), pageCount = el("span", "muted");
  pager.append(previous, pageCount, next);
  const error = el("div", "error");
  browser.append(filters, toolbar, error, tableWrap, pager); container.replaceChildren(browser, content);
  let selected = null, offset = 0, request = 0, reviewVersion = 0, timer, loading = false;
  let data = { sessions: [], matching: [], total: 0 };
  const capacity = ids => new Set([...(selected || []), ...ids]).size <= 100;
  function selectionChanged() {
    const size = selected?.size || 0, pageIds = data.sessions.map(session => session.id);
    count.textContent = `${size} / 100 selected`;
    clear.disabled = !size; analyze.disabled = !size;
    all.disabled = loading || !data.matching.length || !capacity(data.matching) || data.matching.every(id => selected?.has(id));
    selectPage.checked = !!pageIds.length && pageIds.every(id => selected?.has(id));
    selectPage.indeterminate = !selectPage.checked && pageIds.some(id => selected?.has(id));
    selectPage.disabled = loading || !pageIds.length || (!selectPage.checked && !capacity(pageIds));
    rows.querySelectorAll('input[type="checkbox"]').forEach(input => {
      input.checked = !!selected?.has(input.value); input.disabled = !input.checked && size >= 100;
    });
  }
  all.onclick = () => { data.matching.forEach(id => selected.add(id)); selectionChanged(); };
  clear.onclick = () => { selected.clear(); selectionChanged(); };
  analyze.onclick = onAnalyze;
  selectPage.onchange = () => {
    for (const session of data.sessions) selectPage.checked ? selected.add(session.id) : selected.delete(session.id);
    selectionChanged();
  };
  previous.onclick = () => { offset -= 50; load(); };
  next.onclick = () => { offset += 50; load(); };
  for (const input of [search, source, ...Object.values(dates)]) input.oninput = () => {
    request++; offset = 0; clearTimeout(timer); loading = true;
    previous.disabled = next.disabled = true; selectionChanged(); timer = setTimeout(load, 180);
  };
  async function load() {
    clearTimeout(timer);
    const version = ++request;
    loading = true; selectionChanged(); browser.setAttribute("aria-busy", "true"); error.textContent = "";
    previous.disabled = next.disabled = true;
    const body = { offset, query: search.value, source: source.value };
    for (const [key, input] of Object.entries(dates)) if (input.value) {
      const date = new Date(input.value + "T00:00:00");
      if (key === "until") date.setDate(date.getDate() + 1);
      body[key] = date.getTime() / 1000;
    }
    try {
      const result = await post("/api/drill/sessions", body);
      if (version !== request) return;
      data = result;
      if (selected === null) selected = new Set(data.sessions.map(session => session.id));
      rows.replaceChildren();
      for (const session of data.sessions) {
        const row = el("tr"), cell = el("td"), input = el("input"); input.type = "checkbox"; input.value = session.id;
        const date = new Date(session.started * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
        input.setAttribute("aria-label", `Select ${sessionName(session)} · ${date}`);
        input.onchange = () => { input.checked ? selected.add(session.id) : selected.delete(session.id); selectionChanged(); };
        cell.append(input); row.append(cell, el("td", "session-started", date), el("td", "", sessionName(session)), el("td", "muted", SOURCE_NAMES[session.settings.source]), el("td", "num", session.hands));
        const actions = el("td", "session-row-actions"), review = el("button", "btn small", "Review"), resume = el("button", "btn small", session.id === data.active ? "Active" : "Resume");
        review.onclick = () => reviews(session, 0);
        resume.disabled = session.id === data.active || !(session.settings.tree || session.settings.source === "saved" || session.settings.path);
        resume.onclick = () => onResume(session.id);
        actions.append(review, resume); row.append(actions); rows.append(row);
      }
      if (!data.sessions.length) { const row = el("tr"), cell = el("td", "muted", "No sessions"); cell.colSpan = 6; row.append(cell); rows.append(row); }
      pageCount.textContent = data.total ? `${offset + 1}–${offset + data.sessions.length} of ${data.total}` : "0 sessions";
      previous.disabled = offset === 0; next.disabled = offset + data.sessions.length >= data.total;
    } catch (e) { if (version === request) error.textContent = e.message; }
    finally { if (version === request) { loading = false; browser.removeAttribute("aria-busy"); selectionChanged(); } }
  }
  function show() {
    reviewVersion++; browser.classList.remove("hidden"); content.classList.add("hidden"); return load();
  }
  function pagination(offset, count, load) {
    const row = el("div", "session-pagination");
    for (const [label, next, disabled] of [["Previous", offset - 50, offset === 0], ["Next", offset + 50, count < 50]]) {
      const button = el("button", "btn small", label); button.disabled = disabled;
      button.onclick = () => load(next); row.append(button);
    }
    content.append(row);
  }
  async function reviews(session, offset) {
    const version = ++reviewVersion;
    browser.classList.add("hidden"); content.classList.remove("hidden");
    content.textContent = "Loading…";
    try {
      const result = await post("/api/drill/reviews", { session: session.id, offset });
      if (version !== reviewVersion) return;
      content.replaceChildren();
      const back = el("button", "btn small", "All sessions"); back.onclick = show; content.append(back);
      for (const simulation of result.simulations) {
        const row = el("div", "session-simulation");
        row.append(el("strong", "", simulation.simulation.split(/[\\/]/).at(-1).replace(/\.cfr$/, "")));
        for (const seat of ["OOP", "IP"]) {
          const open = el("button", "btn small", `View ${seat} locks · ${simulation[seat.toLowerCase()]}`);
          open.disabled = !simulation[seat.toLowerCase()];
          open.onclick = async () => {
            row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
            try {
              await post("/api/drill/open-locks", { session: session.id, simulation: simulation.simulation, seat });
              await onOpen();
            } catch (e) {
              const error = el("span", "error", e.message); row.append(error);
              row.querySelectorAll("button").forEach((b, i) => { b.disabled = !simulation[["oop", "ip"][i]]; });
            }
          };
          row.append(open);
        }
        content.append(row);
      }
      for (const hand of result.reviews) {
        const review = el("details", "session-review"), summary = el("summary");
        summary.append(el("strong", "", `Hand ${hand.number} · ${hand.seat}`), ...parseCards(hand.node.board).map((c) => cardEl(c, "xs")));
        const feedback = el("div", "drill-feedback");
        feedback.append(feedbackCards(hand.feedback, hand.big_blind));
        review.append(summary, feedback); content.append(review);
      }
      if (!result.reviews.length) content.append(el("p", "muted", "No submitted decisions"));
      pagination(offset, Math.max(result.reviews.length, result.simulations.length), (page) => reviews(session, page));
    } catch (e) { if (version === reviewVersion) content.textContent = e.message; }
  }
  return { show, selection: async () => { if (selected === null) await load(); return [...(selected || [])]; } };
}
