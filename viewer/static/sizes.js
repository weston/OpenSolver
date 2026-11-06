// Bet-size table shared by the Build form and the re-solve dialog: one OOP row and one IP row
// per street (#141). Field names follow viewer/server.py sizing_header: {street}_bet/_raise/
// _donk/_allin are OOP's (and IP's when IP has none of its own), {street}_bet_ip/_raise_ip/
// _allin_ip are IP's, {street}_raisecap caps the street for both.

import { el } from "./cards.js";

export const STREETS = ["flop", "turn", "river"];

// tools/tree_builder.py grammar (#114, #315), no zero sizes: % of pot, Ne / e, NeP / eP (geometric,
// at most P% of the pot), Nx (raises only), a. Entries are separated by commas, spaces or both.
export const SIZE_RE = /^([1-9]\d*(\.\d+)?|0\.\d*[1-9]\d*|([1-9]\d*)?e([1-9]\d*(\.\d+)?|0\.\d*[1-9]\d*)?|a)$/i;
export const RAISE_RE = /^([1-9]\d*(\.\d+)?|0\.\d*[1-9]\d*|([1-9]\d*)?e([1-9]\d*(\.\d+)?|0\.\d*[1-9]\d*)?|[1-9]\d*(\.\d+)?x|a)$/i;
/** The entries of a size field: split on commas and whitespace, as tools/tree_builder.py does. */
export const sizeParts = (v) => String(v).split(/[,\s]+/).filter(Boolean);

const DEFAULTS = {
  flop_bet: "30,60", flop_raise: "50", turn_bet: "60", turn_raise: "50",
  river_bet: "65", river_raise: "50", river_allin: true,
};

const cap1 = (s) => s[0].toUpperCase() + s.slice(1);
const pretty = (v) => String(v).replace(/,\s*/g, ", ");

/** Value of a field for a player: IP's own if the values have it, else OOP's, else the default. */
function valueFor(values, st, k, player) {
  if (player === "ip" && values[`${st}_${k}_ip`] !== undefined) return values[`${st}_${k}_ip`];
  if (values[`${st}_${k}`] !== undefined) return values[`${st}_${k}`];
  return DEFAULTS[`${st}_${k}`] ?? (k === "allin" ? false : "");
}

function sizeInput(k, value) {
  const inp = el("input");
  inp.dataset.k = k;
  inp.value = pretty(value ?? "");
  inp.spellcheck = false;
  if (k === "donk") inp.placeholder = "none";
  else inp.placeholder = "none";
  return inp;
}

function allinSwitch(checked) {
  const sw = el("label", "switch");
  const cb = el("input");
  cb.type = "checkbox";
  cb.dataset.k = "allin";
  cb.checked = !!checked;
  sw.append(cb, el("span"));
  return sw;
}

/** Fill a <tbody> with two rows (OOP, IP) per street. `values` uses the request field names. */
export function renderSizes(tbody, streets, values = {}) {
  tbody.textContent = "";
  for (const st of streets) {
    for (const pl of ["oop", "ip"]) {
      const tr = el("tr", `p-${pl}`);
      tr.dataset.street = st;
      tr.dataset.player = pl;
      if (pl === "oop") {
        const th = el("th", "street", cap1(st));
        th.rowSpan = 2;
        tr.appendChild(th);
      }
      const who = el("td", "who");
      who.appendChild(el("span", `badge ${pl}`, pl.toUpperCase()));
      if (pl === "ip") {
        const same = el("button", "same-btn", "= OOP");
        same.type = "button";
        same.title = "Copy OOP's sizes on this street to IP";
        same.onclick = () => {
          const src = tbody.querySelector(`tr[data-street="${st}"][data-player="oop"]`);
          for (const k of ["bet", "raise"]) tr.querySelector(`input[data-k="${k}"]`).value = src.querySelector(`input[data-k="${k}"]`).value;
          tr.querySelector('input[data-k="allin"]').checked = src.querySelector('input[data-k="allin"]').checked;
          tbody.dispatchEvent(new Event("input", { bubbles: true }));
        };
        who.appendChild(same);
      }
      tr.appendChild(who);
      for (const k of ["bet", "raise"]) {
        const td = el("td");
        td.appendChild(sizeInput(k, valueFor(values, st, k, pl)));
        tr.appendChild(td);
      }
      const donk = el("td");
      if (pl === "oop") donk.appendChild(sizeInput("donk", valueFor(values, st, "donk", pl)));
      else donk.appendChild(el("span", "na", "—"));
      tr.appendChild(donk);
      if (pl === "oop") {
        const td = el("td", "cap-cell");
        td.rowSpan = 2;
        const inp = el("input", "cap");
        inp.type = "number"; inp.min = 0; inp.max = 99; inp.placeholder = "∞";
        inp.dataset.k = "raisecap";
        inp.title = "Maximum bets + raises on the street, both players (empty = no cap)";
        const v = values[`${st}_raisecap`];
        inp.value = v == null ? "" : v;
        td.appendChild(inp);
        tr.appendChild(td);
      }
      const ai = el("td");
      ai.appendChild(allinSwitch(valueFor(values, st, "allin", pl)));
      tr.appendChild(ai);
      tbody.appendChild(tr);
    }
  }
}

/** The table's fields as request keys (IP always explicit, so the server never falls back). */
export function readSizes(tbody) {
  const f = {};
  for (const inp of tbody.querySelectorAll("input")) {
    const tr = inp.closest("tr");
    const k = inp.dataset.k;
    const sfx = tr.dataset.player === "ip" && k !== "raisecap" ? "_ip" : "";
    // the parts comma-joined: "33 75" is two sizes, not 3375 (#354)
    f[`${tr.dataset.street}_${k}${sfx}`] = inp.type === "checkbox" ? inp.checked : sizeParts(inp.value).join(",");
  }
  return f;
}

/** The server's limits (viewer/server.py _check_sizes, #354): Ne N <= 99, NeP P <= 100000, Nx N <= 1000,
 *  a percentage <= 100000. */
export function inBounds(p) {
  const t = p.toLowerCase();
  let m;
  if ((m = /^(\d*)e(.*)$/.exec(t))) return (!m[1] || Number(m[1]) <= 99) && (!m[2] || Number(m[2]) <= 100000);
  if (t.endsWith("x")) return Number(t.slice(0, -1)) <= 1000;
  if (t === "a") return true;
  return Number(t) <= 100000;
}

/** Mark invalid size fields; true when all are valid. */
export function validateSizes(tbody) {
  let ok = true;
  for (const inp of tbody.querySelectorAll("input:not([type=checkbox]):not(.cap)")) {
    const re = inp.dataset.k === "raise" ? RAISE_RE : SIZE_RE;
    const bad = sizeParts(inp.value).some((p) => !re.test(p) || !inBounds(p));
    inp.classList.toggle("invalid", bad);
    if (bad) ok = false;
  }
  return ok;
}
