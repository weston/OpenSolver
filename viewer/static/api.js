// HTTP helpers and small UI services (toasts, tooltip, persisted prefs).

import { el } from "./cards.js";

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
export const get = (u) => fetchJSON(u);
export const post = (u, body) => fetchJSON(u, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
});
export const q = (id) => encodeURIComponent(id);

export function toast(msg, kind = "") {
  const t = el("div", `toast ${kind}`.trim(), msg);
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => { t.style.transition = "opacity .3s"; t.style.opacity = "0"; }, kind === "err" ? 6000 : 3000);
  setTimeout(() => t.remove(), kind === "err" ? 6400 : 3400);
}
export const fail = (e) => toast(String((e && e.message) || e), "err");

// One shared tooltip that follows the pointer.
const tipEl = () => document.getElementById("tip");
export function showTip(ev, html) {
  const t = tipEl();
  t.innerHTML = html;
  t.classList.remove("hidden");
  const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > innerHeight - 8) y = ev.clientY - h - pad;
  t.style.left = x + "px";
  t.style.top = y + "px";
}
export function hideTip() { tipEl().classList.add("hidden"); }

// Per-browser conveniences only (form state, theme). Never required for correctness.
export const prefs = {
  get(k, d) { try { const v = localStorage.getItem("ws-" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("ws-" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

export function fmt(v, digits = 1) {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}
export function fmtChips(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  return Math.abs(v - Math.round(v)) < 1e-6 ? String(Math.round(v)) : v.toFixed(1);
}
