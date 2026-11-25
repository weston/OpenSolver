import { post, fmt } from "./api.js";
import { el, parseCards, cardEl } from "./cards.js";
import { feedbackCards } from "./drill_controls.js";

export function openSessions(onResume, onOpen) {
  const dialog = document.getElementById("drill-sessions-dialog");
  const header = el("div", "card-head"), content = el("div", "sessions-content"), close = el("button", "btn small", "Close");
  header.append(el("h2", "", "Sessions"), close);
  close.onclick = () => dialog.close();
  dialog.replaceChildren(header, content);
  dialog.showModal();
  async function list(offset = 0) {
    content.textContent = "Loading…";
    try {
      const result = await post("/api/drill/sessions", { offset });
      content.replaceChildren();
      if (!result.sessions.length) content.append(el("span", "muted", "No sessions"));
      for (const session of result.sessions) {
        const button = el("button", "btn session-entry");
        const status = session.id === result.active ? "Active" : session.ended ? "Ended" : "Interrupted";
        button.append(el("strong", "", new Date(session.started * 1000).toLocaleString()),
          el("span", "", `${session.settings.spot || "Current tree"} · ${status}`),
          el("span", "num", `${session.hands} hands · ${session.decisions} decisions · ${fmt(session.decisions ? session.loss / session.decisions : 0, 2)}% EV loss`));
        button.onclick = () => reviews(session, 0);
        const entry = el("div", "session-row"), resume = el("button", "btn small", "Resume");
        resume.disabled = session.id === result.active || !(session.settings.tree || session.settings.source === "saved" || session.settings.path);
        resume.onclick = () => { dialog.close(); onResume(session.id); };
        entry.append(button, resume);
        content.append(entry);
      }
      pagination(offset, result.sessions.length, list);
    } catch (e) { content.textContent = e.message; }
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
    content.textContent = "Loading…";
    try {
      const result = await post("/api/drill/reviews", { session: session.id, offset });
      content.replaceChildren();
      const back = el("button", "btn small", "All sessions"); back.onclick = () => list(); content.append(back);
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
              dialog.close(); await onOpen();
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
    } catch (e) { content.textContent = e.message; }
  }
  list();
}
