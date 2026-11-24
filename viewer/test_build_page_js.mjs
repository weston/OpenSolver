// Test of the Build tab's Preflop form as a page (#520). viewer/static/build.js and preflop_build.js
// are loaded as the modules the browser loads, with the modules they import, and run against a stub
// document made from the page's own markup (viewer/static/index.html) and a stub fetch that answers
// as the viewer's server does for one small tree. What is checked is what the page then reads: the
// tree in words, the memory row and its cells, the note beside Estimate memory, the line after Build.
// usage: node viewer/test_build_page_js.mjs

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const is = (got, want, msg) => check(same(got, want), same(got, want) ? msg : `${msg}: it is ${JSON.stringify(got)}, not ${JSON.stringify(want)}`);
const near = (a, b) => Math.abs(a - b) < 1e-12;

// An error in the page's code after an await is a rejected promise nobody waits for: jsc drops it
// without a word and node ends the run on it. Both are asked to hand it over; the list is checked at the end.
const dropped = [];
if (typeof setUnhandledRejectionCallback === "function") setUnhandledRejectionCallback((promise, why) => dropped.push(String(why)));   // jsc
else process.on("unhandledRejection", (why) => dropped.push(String(why)));

/* ------------------------------------------------------------------ the stub document */

// It has what the build page touches and nothing else. Anything else the page reads, sets or calls
// is noted in `unknown` and throws. The list is checked at the end as well: the page catches some
// errors itself and shows them as a server's.
const unknown = [];
function refuse(what) { unknown.push(what); throw new Error(`the stub document has no ${what}`); }

/** What ends the test where it stands: the page does not do what the next step needs, or does not stop. */
class Stopped extends Error {}
// Every read, write and call on the stub document is counted, so that a loop of the page that never
// ends and touches the document on its way ends here. (One that asks the server is stopped sooner,
// the second time it asks for the same: see the stub server.) WORK_LIMIT is no measure of the page:
// the whole test is some 22 thousand of these, most of them the page opening (four grids of 13 x 13
// hands, a deck of 52 cards), and the limit is a hundred times that, a second or so of a loop.
const WORK_LIMIT = 2e6;
let work = 0;
function counted() {
  if (++work <= WORK_LIMIT) return;
  net.stopped ||= `the page read and wrote the document ${WORK_LIMIT.toLocaleString("en-US")} times, a hundred times what this whole test takes: a loop that would not end`;
  throw new Stopped(net.stopped);
}

const NODE = Symbol("node");   // a stub element's own record, for this file
const VOID = new Set(["meta", "link", "input"]);   // the page's tags that have no end tag
// What an element of a tag has besides what every element has, as a new one holds it.
const OWN = {
  input: () => ({ type: "text", value: "", checked: false, disabled: false, placeholder: "", min: "", max: "", oninput: null, onchange: null }),
  textarea: () => ({ value: "", placeholder: "" }),
  button: () => ({ type: "submit", disabled: false }),
  th: () => ({ rowSpan: 1 }),
  td: () => ({ rowSpan: 1 }),
};
const root = { tag: "#document", kids: [], up: null, on: {} };
const byId = new Map();
const newNode = (tag) => ({
  tag, kids: [], up: null, id: "", cls: "", title: "", onclick: null, data: {}, attrs: {}, on: {},
  own: OWN[tag] ? OWN[tag]() : {}, style: { setProperty(k, v) { this[k] = v; } }, face: null,
});
const face = (n) => (n.face ||= new Proxy(n, handler));

const text = (n) => n.kids.map((k) => (typeof k === "string" ? k : text(k))).join("");
function unlink(n) { if (n.up) { n.up.kids = n.up.kids.filter((k) => k !== n); n.up = null; } }
function link(parent, n) { unlink(n); n.up = parent; parent.kids.push(n); }
function clear(n) {
  for (const k of n.kids) if (typeof k !== "string") k.up = null;
  n.kids = [];
  if (n.tag === "select") delete n.own.chosen;
}
/** What the page appends: an element, or text. */
function attach(parent, x) {
  if (typeof x === "string" || typeof x === "number") { parent.kids.push(String(x)); return; }
  const n = x && x[NODE];
  if (!n) refuse(`way to append ${x} to <${parent.tag}>`);
  link(parent, n);
}
/** The elements under n, in the order of the document. */
function under(n, out = []) {
  for (const k of n.kids) if (typeof k !== "string") { out.push(k); under(k, out); }
  return out;
}

// The value of a form field, as a browser holds it.
const NUMBER = /^-?(\d+|\d*\.\d+)([eE][-+]?\d+)?$/;   // what an <input type=number> keeps; anything else reads ""
const options = (n) => under(n).filter((k) => k.tag === "option");
const optionValue = (o) => ("value" in o.own ? o.own.value : text(o));
const VALUE = {
  input: {
    get: (n) => n.own.value,
    set: (n, v) => {
      const s = String(v);
      if (n.own.type === "range" && !(NUMBER.test(s) && Number(s) >= Number(n.own.min) && Number(s) <= Number(n.own.max))) refuse(`rounding of "${s}" into a slider's range`);
      n.own.value = n.own.type === "number" && !NUMBER.test(s) ? "" : s;
    },
  },
  textarea: { get: (n) => n.own.value, set: (n, v) => { n.own.value = String(v); } },
  option: { get: optionValue, set: (n, v) => { n.own.value = String(v); } },
  // a select reads its chosen option, the first one until one is chosen; a value none of them has chooses none
  select: {
    get: (n) => { const os = options(n), o = n.own.chosen === null ? null : os.includes(n.own.chosen) ? n.own.chosen : os[0]; return o ? optionValue(o) : ""; },
    set: (n, v) => { n.own.chosen = options(n).find((o) => optionValue(o) === String(v)) || null; },
  },
};

const classes = (n) => n.cls.split(/\s+/).filter(Boolean);
function classList(n) {
  const add = (...cs) => { n.cls = [...new Set([...classes(n), ...cs])].join(" "); };
  const remove = (...cs) => { n.cls = classes(n).filter((c) => !cs.includes(c)).join(" "); };
  return {
    add, remove, contains: (c) => classes(n).includes(c),
    toggle: (c, on) => { const want = on === undefined ? !classes(n).includes(c) : !!on; (want ? add : remove)(c); return want; },
  };
}

// Selectors: a tag, #id, .class, [attr=value], :not(...) of these, and descendants by spaces.
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
function attr(n, name) {
  if (name.startsWith("data-")) { const k = camel(name.slice(5)); return k in n.data ? String(n.data[k]) : null; }
  if (name === "type" && "type" in n.own) return n.own.type;
  return name in n.attrs ? n.attrs[name] : null;
}
function compound(sel) {
  const tests = [];
  let rest = sel;
  const take = (re, test) => { const m = re.exec(rest); if (m) { rest = rest.slice(m[0].length); tests.push(test(m)); } return !!m; };
  take(/^[a-z][a-z0-9]*/, (m) => (n) => n.tag === m[0]);
  while (rest) {
    if (take(/^#([\w-]+)/, (m) => (n) => n.id === m[1])) continue;
    if (take(/^\.([\w-]+)/, (m) => (n) => classes(n).includes(m[1]))) continue;
    if (take(/^\[([\w-]+)=(?:"([^"]*)"|([\w-]+))\]/, (m) => (n) => attr(n, m[1]) === (m[2] ?? m[3]))) continue;
    if (take(/^:not\(([^()]+)\)/, (m) => { const inner = compound(m[1]); return (n) => !inner(n); })) continue;
    refuse(`way to read the selector "${sel}"`);
  }
  return (n) => tests.every((t) => t(n));
}
function matcher(sel) {
  const parts = String(sel).trim().split(/\s+/).map(compound);
  return (n) => {
    if (!parts[parts.length - 1](n)) return false;
    let i = parts.length - 2;
    for (let a = n.up; a && a !== root && i >= 0; a = a.up) if (parts[i](a)) i--;
    return i < 0;
  };
}
const within = (n, sel) => under(n).filter(matcher(sel));
/** What querySelectorAll gives: a list to walk, not an array. */
const list = (nodes) => { const faces = nodes.map(face); return { length: faces.length, forEach: (f) => faces.forEach((x, i) => f(x, i)), [Symbol.iterator]: () => faces[Symbol.iterator]() }; };

// What every element has: read (get), set, or called (fn).
const COMMON = {
  id: { get: (n) => n.id },
  className: { get: (n) => n.cls, set: (n, v) => { n.cls = String(v); } },
  title: { get: (n) => n.title, set: (n, v) => { n.title = String(v); } },
  onclick: { set: (n, v) => { n.onclick = v; } },
  spellcheck: { set: (n, v) => { n.attrs.spellcheck = String(v); } },
  dataset: { get: (n) => n.data },
  style: { get: (n) => n.style },
  classList: { get: classList },
  textContent: { get: text, set: (n, v) => { clear(n); if (v != null && String(v) !== "") n.kids.push(String(v)); } },
  innerHTML: { set: (n, v) => { clear(n); parseInto(n, String(v)); } },
  firstChild: { get: (n) => (!n.kids.length ? null : typeof n.kids[0] === "string" ? refuse("text as a first child") : face(n.kids[0])) },
  append: { fn: (n, ...xs) => { for (const x of xs) attach(n, x); } },
  appendChild: { fn: (n, x) => { attach(n, x); return x; } },
  replaceChildren: { fn: (n, ...xs) => { clear(n); for (const x of xs) attach(n, x); } },
  remove: { fn: unlink },
  closest: { fn: (n, sel) => { const ok = matcher(sel); for (let a = n; a && a !== root; a = a.up) if (ok(a)) return face(a); return null; } },
  querySelector: { fn: (n, sel) => { const f = within(n, sel)[0]; return f ? face(f) : null; } },
  querySelectorAll: { fn: (n, sel) => list(within(n, sel)) },
  addEventListener: { fn: (n, type, f) => { (n.on[type] ||= []).push(f); } },
};
const handler = {
  get(n, k) {
    if (k === NODE) return n;
    if (typeof k === "symbol") return undefined;
    counted();
    const c = COMMON[k];
    if (c && c.fn) return (...a) => c.fn(n, ...a);
    if (c && c.get) return c.get(n);
    if (k === "value" && VALUE[n.tag]) return VALUE[n.tag].get(n);
    if (!c && k in n.own) return n.own[k];
    return refuse(`.${k} to read on <${n.tag}>`);
  },
  set(n, k, v) {
    counted();
    const c = COMMON[k];
    if (c && c.set) c.set(n, v);
    else if (k === "value" && VALUE[n.tag]) VALUE[n.tag].set(n, v);
    else if (!c && k in n.own) n.own[k] = typeof n.own[k] === "boolean" ? !!v : typeof n.own[k] === "string" ? String(v) : v;
    else refuse(`.${String(k)} to set on <${n.tag}>`);
    return true;
  },
};

// The markup, as the page writes it: tags with attributes (name="value", or a name alone), text,
// comments, &amp; the only entity. A script's text is not markup. Anything that does not fit (a tag
// left open, a bare "<", another entity) throws.
const decode = (s) => s.replace(/&([a-z0-9#]+);/gi, (all, name) => (name === "amp" ? "&" : refuse(`entity ${all}`)));
function setAttr(n, name, v) {
  if (name === "id") { n.id = v; if (!byId.has(v)) byId.set(v, n); }
  else if (name === "class") n.cls = v;
  else if (name === "title") n.title = v;
  else if (name.startsWith("data-")) n.data[camel(name.slice(5))] = v;
  else if (name === "value" && VALUE[n.tag]) VALUE[n.tag].set(n, v);
  else if (name in n.own) n.own[name] = typeof n.own[name] === "boolean" ? true : v;
  else n.attrs[name] = v;
}
function parseInto(parent, html) {
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z][a-z0-9]*)\s*>|<([a-z][a-z0-9]*)((?:\s+[^\s=>\/"]+(?:="[^"]*")?)*)\s*(\/?)>|[^<]+/giy;
  let at = parent;
  for (let pos = 0; pos < html.length;) {
    token.lastIndex = pos;
    const m = token.exec(html);
    if (!m) throw new Error(`the stub document cannot read the markup at: ${html.slice(pos, pos + 60)}`);
    pos = token.lastIndex;
    if (m[1]) {
      if (at === parent || at.tag !== m[1].toLowerCase()) throw new Error(`markup: </${m[1]}> where <${at.tag}> is open`);
      at = at.up;
    } else if (m[2]) {
      const n = newNode(m[2].toLowerCase());
      const attrs = [...m[3].matchAll(/([^\s=>\/"]+)(?:="([^"]*)")?/g)].map((a) => [a[1].toLowerCase(), decode(a[2] ?? "")]);
      attrs.sort((a, b) => (b[0] === "type") - (a[0] === "type"));   // the type first: it decides what a value may be
      for (const [name, v] of attrs) setAttr(n, name, v);
      link(at, n);
      if (n.tag === "script") {
        const end = html.indexOf("</script>", pos);
        if (end < 0) throw new Error("markup: <script> is not closed");
        pos = end + "</script>".length;
      } else if (!VOID.has(n.tag) && !m[4]) at = n;
    } else if (m[0][0] !== "<") at.kids.push(decode(m[0]));
  }
  if (at !== parent) throw new Error(`markup: <${at.tag}> is not closed`);
}

/** An object of the page's world that has `members` and refuses anything else. */
const only = (name, members) => new Proxy(members, { get: (t, k) => { counted(); return typeof k === "symbol" || k in t ? t[k] : refuse(`${name}.${k}`); } });
const attached = (n) => { for (; n; n = n.up) if (n === root) return true; return false; };
const stubDocument = only("document", {
  getElementById: (id) => { const n = byId.get(String(id)); return n && attached(n) ? face(n) : null; },
  querySelector: (sel) => { const f = within(root, sel)[0]; return f ? face(f) : null; },
  querySelectorAll: (sel) => list(within(root, sel)),
  createElement: (tag) => face(newNode(String(tag).toLowerCase())),
  addEventListener: (type, f) => { (root.on[type] ||= []).push(f); },
});
const stored = new Map();   // what an earlier visit left in the browser
const stubStorage = only("localStorage", {
  getItem: (k) => (stored.has(String(k)) ? stored.get(String(k)) : null),
  setItem: (k, v) => { stored.set(String(k), String(v)); },
});

/* ------------------------------------------------------------------ the page's clock */

// setTimeout and clearTimeout of the page: nothing runs until the test moves the clock on.
const clock = { now: 0, next: 1, timers: [], running: false };
// A timer set by a timer is due a millisecond later at the earliest, so moving the clock on by a
// number of milliseconds runs a number of timers that has an end.
const stubSetTimeout = (fn, ms = 0) => { clock.timers.push({ id: clock.next, at: clock.now + Math.max(Number(ms) || 0, clock.running ? 1 : 0), fn }); return clock.next++; };
const stubClearTimeout = (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); };

/* ------------------------------------------------------------------ the stub server */

// The fixed answers are the viewer's own (viewer/preflop_api.py, asked through its HTTP server) for
// one tree: a stack of 5 big blinds, no limp, betting through the turn, everything else the tool's
// defaults, on a machine said to have 1,024 MB. flops -> MB: the smallest tree, the tree with the
// tables that make a solve faster. On that machine 10 and 25 flops fit either way, 50 fit as the
// smallest tree only, 100 do not fit, and 200 have no answer here: the page must not ask.
const MACHINE_MB = 1024;
const SIZES = { 10: [169, 256], 25: [423, 654], 50: [836, 1297], 100: [1618, 2524] };
const ALL_HANDS = "AA-22,AKs-A2s,KQs-K2s,QJs-Q2s,JTs-J2s,T9s-T2s,98s-92s,87s-82s,76s-72s,65s-62s,54s-52s,43s-42s,32s,"
  + "AKo-A2o,KQo-K2o,QJo-Q2o,JTo-J2o,T9o-T2o,98o-92o,87o-82o,76o-72o,65o-62o,54o-52o,43o-42o,32o";
const DEFAULTS = {
  form: {
    stack: "100", ante: "0", open: "2.5", iso: "3.5", threebet: "4x", limp_raise: "", fourbet: "2.3x", fivebet: "allin",
    allin_threshold: "50", postflop_allin_threshold: "67", postflop_allin_pot: "150", flop_bet: "50", turn_bet: "50", river_bet: "50",
    raise: "60", donk: "", limp: true, depth: "flop", raise_cap: 2, range_sb: ALL_HANDS, range_bb: ALL_HANDS, flops: "25", accuracy: 0.5,
  },
  depths: ["none", "flop", "turn", "river"], flops: ["10", "25", "50", "100", "200", "all"], chips: 100, memory_mb: MACHINE_MB, ok: true,
};
const row = (depth, what, id, kind, sb, bb, then) => ({ depth, what, id, kind, in: [sb, bb], then });
const ROWS = [
  row(0, "start", "r:0", "decision", "0.5", "1", "SB to act"),
  row(1, "SB raise 2.5", "r:0:b250", "decision", "2.5", "1", "BB to act"),
  row(2, "BB all-in", "r:0:b250:b500", "decision", "2.5", "5", "SB to act"),
  row(3, "SB call", "r:0:b250:b500:c", "allin", "5", "5", "all-in, showdown"),
  row(3, "SB fold", "r:0:b250:b500:f", "fold", "2.5", "5", "hand over"),
  row(2, "BB call", "r:0:b250:c", "flop", "2.5", "2.5", "flop, 3 postflop lines"),
  row(2, "BB fold", "r:0:b250:f", "fold", "2.5", "1", "hand over"),
  row(1, "SB fold", "r:0:f", "fold", "0.5", "1", "hand over"),
];
const NODE_COUNT = "H_DEC: 769\n  FLOP  21\n  TURN  748\n  RIVER 0\nV_DEC: 760\n  FLOP  12\n  TURN  748\n  RIVER 0\nSPLIT: 11\n  FLOP  11\n  TURN  0\n  RIVER 0\n"
  + "END: 1894\n  FLOP  24\n  TURN  1870\n  RIVER 0\nROOT: 1\n  FLOP  1\n  TURN  0\n  RIVER 0\nTOTAL: 3435";
const NO_STACK = "The stack must be more than one big blind";
const TOO_LARGE = "This tree is about 1,618 MB and this machine has 1,024 MB: take fewer flops, or stop the betting a street earlier";

/** Which tree a form asks for: "small" (the tree of the fixed answers), "no stack" (the same with a
 *  stack of 0, which the tool refuses), or null: a form the fixed answers do not cover. */
function treeOf(form) {
  const { stack, flops, range_bb, range_sb, ...rest } = form || {};
  const { stack: _s, flops: _f, range_bb: _b, range_sb: _r, accuracy: _a, ...want } = { ...DEFAULTS.form, limp: false, depth: "turn" };
  const sorted = (o) => Object.keys(o).sort().map((k) => [k, o[k]]);
  // every hand at full weight, as the page writes a range: the 169 classes by name
  const everyHand = (t) => typeof t === "string" && new Set(t.split(",")).size === 169 && t.split(",").every((c) => /^([2-9TJQKA])\1$|^[2-9TJQKA]{2}[so]$/.test(c));
  if (!same(sorted(rest), sorted(want)) || !everyHand(range_bb) || !everyHand(range_sb)) return null;
  return stack === "5" ? "small" : stack === "0" ? "no stack" : null;
}

/** The server's answer to a request, [status, data], or null when there is no fixed answer for it. */
function serve({ method, url, body }) {
  if (method === "GET" && url === "/api/preflop/defaults") return [200, DEFAULTS];
  if (method === "POST" && url === "/api/solve") return same(Object.keys(body).sort(), ["accuracy", "preview"]) ? [200, { ok: true }] : null;
  const m = /^\/api\/preflop\/(describe|estimate|build)$/.exec(url);
  if (method !== "POST" || !m) return null;
  const tree = treeOf(body), sizes = SIZES[body.flops];
  if (tree === "no stack") return [400, { ok: false, error: NO_STACK }];
  if (tree !== "small" || !sizes) return null;
  const flops = Number(body.flops), flops_name = `flops/wsolver_${flops}.txt`, [mb, mb_tables] = sizes;
  if (m[1] === "describe") return [200, { rows: ROWS, chips: 100, flops, flops_name, exits: 1, ok: true }];
  if (m[1] === "estimate") return [200, { mb, mb_tables, memory_mb: MACHINE_MB, flops, flops_name, depth: "turn", ok: true }];
  if (mb > MACHINE_MB) return [400, { ok: false, error: TOO_LARGE }];
  // the one build recorded: the solver built the tree with its tables
  return flops === 10 ? [200, { node_count: NODE_COUNT, mb, mb_tables, built_mb: mb_tables, memory_mb: MACHINE_MB, chips: 100, flops, flops_name, ok: true }] : null;
}

// fetch of the page. A request waits until the test answers it, so the page can be read while it
// waits. A loop in the page that never ends shows here as a request with no fixed answer, or as one
// the page already made since the user last did something. Such a request is never answered (the
// loop stops there, at a count of one) and `net.stopped` says why: the test fails on it at its next step.
const net = { waiting: [], seen: new Set(), stopped: null };
const never = new Promise(() => {});
const nameOf = (url, body) => url.split("/").pop() + (body && body.flops != null ? ` ${body.flops}` : "");
function stubFetch(url, opts = {}) {
  const method = opts.method || "GET", body = opts.body === undefined ? null : JSON.parse(opts.body);
  const name = nameOf(url, body), key = JSON.stringify([method, url, body]), reply = serve({ method, url, body });
  const stop = (why) => { net.stopped ||= why; return never; };
  if (!reply) return stop(`the page asked for "${name}" (${method} ${url}${body ? " " + JSON.stringify(body) : ""}), which has no fixed answer`);
  if (net.seen.has(key)) return stop(`the page asked for "${name}" a second time with nothing done in between: a loop that would not end`);
  net.seen.add(key);
  const [status, data] = reply;
  return new Promise((resolve) => net.waiting.push({
    name, body, send: () => resolve({ ok: status < 400, status, json: () => Promise.resolve(JSON.parse(JSON.stringify(data))) }),
  }));
}

/* ------------------------------------------------------------------ what the test does to the page */

const realSetTimeout = setTimeout;
/** Lets the page run until it waits for an answer or a timer: every promise that can go on, has. */
const settle = () => new Promise((r) => realSetTimeout(r, 0));
const waiting = () => net.waiting.map((r) => r.name);
const alive = () => { if (net.stopped) throw new Stopped(net.stopped); };

/** The server answers the request `name` the page waits for, and the page runs on. Without such a
 *  request the test stops: `why` says what the page should have asked for. */
async function answer(name, why = "") {
  alive();
  const i = net.waiting.findIndex((r) => r.name === name);
  if (i < 0) throw new Stopped(`the page does not ask for "${name}"${why ? ` (${why})` : ""}: it waits for ${waiting().length ? waiting().map((w) => `"${w}"`).join(", ") : "nothing"}`);
  const [r] = net.waiting.splice(i, 1);
  r.send();
  await settle();
  alive();
  return r;
}

/** An event of the user's on an element: its own handler and the listeners up to the document. */
function fire(target, type) {
  const ev = { type, target, preventDefault() {} };
  for (let n = target[NODE]; n; n = n.up) {
    const own = type === "click" ? n.onclick : n.own && n.own[`on${type}`];
    if (typeof own === "function") own(ev);
    for (const f of n.on[type] || []) f(ev);
  }
}
function press(button) {
  if (button.disabled) throw new Stopped(`the button "${button.textContent}" is disabled, so it cannot be pressed`);
  net.seen.clear();
  fire(button, "click");
  alive();
}
function type(input, value) {
  if (input.disabled) throw new Stopped(`the field #${input.id} is disabled, so nothing can be typed in it`);
  net.seen.clear();
  input.value = value;
  fire(input, "input");
  alive();
}
function choose(select, value) {
  net.seen.clear();
  select.value = value;
  if (select.value !== value) throw new Stopped(`#${select.id} has no option "${value}"`);
  fire(select, "input");
  fire(select, "change");
  alive();
}
/** Time passes: the page's timers that are due within `ms` run, the earliest first. */
function later(ms) {
  const end = clock.now + ms;
  clock.running = true;
  for (;;) {
    const due = clock.timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    clock.timers = clock.timers.filter((t) => t !== due);
    clock.now = due.at;
    net.seen.clear();
    due.fn();
  }
  clock.running = false;
  clock.now = end;
  alive();
}

/* ------------------------------------------------------------------ the page */

async function pageMarkup() {
  if (typeof readFile === "function") {   // jsc has no file module, and a readFile of its own
    const dir = decodeURIComponent(import.meta.url.replace(/^file:\/\//, "").replace(/[^/]*$/, ""));
    return readFile(`${dir}static/index.html`);
  }
  const { readFileSync } = await import("node:fs");
  return readFileSync(new URL("./static/index.html", import.meta.url), "utf8");
}

try {
  parseInto(root, await pageMarkup());
  // An earlier visit left the small tree in the form (viewer/static/api.js prefs); the fields it did
  // not keep take the tool's defaults.
  stored.set("ws-preflop", JSON.stringify({ stack: "5", limp: false, depth: "turn" }));
  for (const [name, value] of Object.entries({
    document: stubDocument, window: only("window", { addEventListener() {} }), localStorage: stubStorage,
    fetch: stubFetch, setTimeout: stubSetTimeout, clearTimeout: stubClearTimeout,
  })) Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });

  // The modules as the page loads them (a name one of them imports and another does not export
  // fails here, as it does in a browser). app.js is not loaded: it starts every view of the page.
  let initHands, initBuild;
  try {
    ({ initHands } = await import("./static/cards.js"));
    ({ initBuild } = await import("./static/build.js"));
  } catch (e) {
    throw new Stopped(`the build page's modules do not load: ${e}`);
  }
  check(typeof initBuild === "function", "the build page's modules load: build.js, preflop_build.js and what they import");

  const $ = (id) => stubDocument.getElementById(id) || refuse(`#${id} in the page`);
  const all = (sel, within = stubDocument) => [...within.querySelectorAll(sel)];
  const texts = (sel, within) => all(sel, within).map((e) => e.textContent);
  const has = (e, cls) => e.classList.contains(cls);
  const line = () => ($("build-info").querySelector(".err") ? "error: " : "") + $("build-info").textContent;
  const state = () => `${$("build-state").textContent} [${$("build-state").className}]`;
  const tree = () => texts(".pf-sum", $("pf-tree"))[0] || `error: ${texts(".err", $("pf-tree"))[0]}`;
  // the memory row: each cell as [flops, size, classes, tooltip]; the note, with "warn: " when it is marked
  const cells = () => all("button", $("pf-mem-row")).map((b) => [b.querySelector("span").textContent, b.querySelector("b").textContent, b.className, b.title]);
  const cell = (label) => all("button", $("pf-mem-row")).find((b) => b.querySelector("span").textContent === label) || refuse(`cell "${label}" in the memory row`);
  const asking = () => texts(".hint", $("pf-mem-row")).length > 0;   // the "…" after the cells while the solver is asked
  const note = () => (has($("pf-mem-text"), "warn") ? "warn: " : "") + $("pf-mem-text").textContent;
  const modeButton = (mode) => all("#build-mode button").find((b) => b.dataset.mode === mode);
  const TABLES = "with the tables that make a solve faster (built when they fit in nine tenths of the memory that is free)";
  const NOTE = {
    10: `10 flops · about 169 MB, or 256 MB ${TABLES} · this machine has 1.0 GB`,
    25: `25 flops · about 423 MB, or 654 MB ${TABLES} · this machine has 1.0 GB`,
    100: `warn: 100 flops · about 1.6 GB, or 2.5 GB ${TABLES} · more than this machine's 1.0 GB`,
  };
  const CHOSEN = "The number of flops chosen", TAKE = "Take this number of flops", OVER = "More than this machine's memory";

  // ---- the page opens
  const cards = [], order = [], built = [];
  for (const r of "23456789TJQKA") for (const s of "cdhs") cards.push(r + s);
  for (let i = 1; i < 52; i++) for (let j = 0; j < i; j++) order.push(cards[i] + cards[j]);
  initHands(order);   // app.js: the solver's order of the 1326 hands, before any view is set up
  initBuild({ built: (info) => built.push(info) });
  is(waiting(), ["defaults"], "opening the page asks the server for the preflop form's defaults and for nothing else");
  is([state(), $("btn-resume").disabled], ["not built [tag]", true], "no tree yet: the tag says not built and Solve is off");
  press(modeButton("preflop"));
  is([stubDocument.querySelector(".build").dataset.mode, all("#build-mode button").filter((b) => has(b, "active")).map((b) => b.textContent)],
    ["preflop", ["Preflop"]], "the Preflop switch shows the preflop form");
  press($("pf-estimate"));
  press($("btn-build"));
  is([waiting(), line()], [["defaults"], "error: The preflop form is not set up yet."],
    "before the defaults are in, Estimate memory asks nothing and Build says the form is not set up");

  await answer("defaults");
  is(all("option", $("pf-flops")).map((o) => [o.value, o.textContent]),
    [["10", "10 flops"], ["25", "25 flops"], ["50", "50 flops"], ["100", "100 flops"], ["200", "200 flops"], ["all", "All 1,755 flops"]],
    "the defaults fill the choices of flops");
  is(texts("option", $("pf-depth")), ["No street (checked down)", "The flop", "The turn", "The river"], "and the streets the betting can go through");
  is([$("pf-stack").value, $("pf-limp").checked, $("pf-depth").value, $("pf-open").value, $("pf-threebet").value, $("pf-cap").value, $("pf-flops").value],
    ["5", false, "turn", "2.5", "4x", "2", "25"], "the form holds what the earlier visit left, and the tool's defaults for the rest");
  is([$("pf-flop-bet").disabled, $("pf-turn-bet").disabled, $("pf-river-bet").disabled], [false, false, true],
    "betting through the turn: the river's size field is off");
  is(all(".editor").filter((e) => e.dataset.seat).map((e) => `${e.dataset.seat}: ${texts("h2", e)[0]}, ${texts(".stats b", e)[0]} combos`),
    ["BB: Big blind, 1326 combos", "SB: Small blind (button), 1326 combos"], "the two ranges are the blinds', every hand in them");
  await answer("describe 25", "the tree in words is asked for once the form is filled");
  is(tree(), "8 nodes before the flop · 1 way to a flop · 25 flops", "the tree in words: its line of counts");
  is(texts(".pf-act", $("pf-tree")), ["Blinds posted", "SB raise 2.5", "BB all-in", "SB call", "SB fold", "BB call", "BB fold", "SB fold"], "its actions, the start as the blinds posted");
  is([texts(".pf-in", $("pf-tree"))[3], texts(".pf-then", $("pf-tree"))[5], all(".pf-then", $("pf-tree"))[5].className, all(".pf-act", $("pf-tree"))[2].style.paddingLeft],
    ["2.5 / 5", "flop, 3 postflop lines", "pf-then flop", "32px"], "what each blind has in, what comes then, and the depth as an indent");
  // filling the form counted as a change, so the tree in words is asked for once more, 300 ms on
  later(300);
  await answer("describe 25");
  is([tree(), has($("pf-tree"), "stale"), waiting()], ["8 nodes before the flop · 1 way to a flop · 25 flops", false, []], "the tree in words is settled, and nothing more is asked");
  is([note(), cells(), $("pf-estimate").disabled], ["", [], false], "no memory estimate is shown before one is asked for");

  // ---- Estimate memory: the chosen number of flops first, then the others from few to many
  press($("pf-estimate"));
  is([waiting(), note(), cells(), asking(), $("pf-estimate").disabled], [["estimate 25"], "Asking the solver…", [], true, true],
    "Estimate memory asks for the chosen 25 flops first; while it waits the note says so and the button is off");
  await answer("estimate 25");
  is(cells(), [["25 flops", "423–654 MB", "pf-mem-cell active", CHOSEN]], "the answer makes the chosen number's cell: both sizes of the tree in one");
  is(note(), NOTE[25], "the note is of the chosen 25 flops: both sizes, and the machine's memory");
  is([asking(), $("pf-estimate").disabled], [true, true], "the row still says it is asking");
  await answer("estimate 10", "after the chosen number the others are asked from the fewest");
  is(note(), NOTE[25], "with a cell before it in the row, the note is still of the chosen 25 flops");
  await answer("estimate 50");
  is(cells().map((c) => c[0]), ["10 flops", "25 flops", "50 flops"], "the cells stand in the order of the choices, not of the answers");
  await answer("estimate 100", "the smallest tree on 50 flops fits the machine (836 of 1,024 MB), so more flops are asked for");
  is(waiting(), [], "100 flops are more than the machine has: 200 are not asked for");
  is(cells(), [
    ["10 flops", "169–256 MB", "pf-mem-cell", TAKE],
    ["25 flops", "423–654 MB", "pf-mem-cell active", CHOSEN],
    ["50 flops", "836 MB – 1.3 GB", "pf-mem-cell", TAKE],
    ["100 flops", "1.6–2.5 GB", "pf-mem-cell warn", OVER],
  ], "the row: 50 flops are not marked (the smallest tree fits, the tree with tables does not), 100 are");
  is([note(), asking(), $("pf-estimate").disabled], [NOTE[25], false, false], "the row is done: the note stays, the button is on again");

  // ---- a cell chooses its number of flops
  press(cell("10 flops"));
  is([$("pf-flops").value, JSON.parse(stored.get("ws-preflop")).flops], ["10", "10"], "a click on a cell chooses its number of flops, and the form is kept for the next visit");
  is(cells().map((c) => c.slice(2)), [["pf-mem-cell active", CHOSEN], ["pf-mem-cell", TAKE], ["pf-mem-cell", TAKE], ["pf-mem-cell warn", OVER]], "the chosen cell is the 10 now");
  is([note(), waiting()], [NOTE[10], []], "the note is of the 10 flops, from the estimates on show: nothing is asked");
  is(has($("pf-tree"), "stale"), true, "the tree in words is marked as being of the form before");
  later(300);
  is(waiting(), ["describe 10"], "the tree in words is asked for 300 ms after the form's last change");
  press(cell("100 flops"));
  is(note(), NOTE[100], "the note of a number that is more than the machine has says so and is marked");
  is(cells()[3].slice(2), ["pf-mem-cell active warn", OVER], "and its cell is the chosen one and marked");
  later(300);
  await answer("describe 100");
  is([tree(), has($("pf-tree"), "stale")], ["8 nodes before the flop · 1 way to a flop · 100 flops", false], "the tree in words is of the 100 flops");
  await answer("describe 10");
  is(tree(), "8 nodes before the flop · 1 way to a flop · 100 flops", "the answer for the 10 flops chosen before, coming after it, does not take its place");

  // ---- Build, refused by the server
  press($("btn-build"));
  is([waiting(), line(), $("btn-build").disabled, $("btn-solve").disabled], [["build 100"], "Checking the tree and its memory, then building…", true, true],
    "Build sends the form in one request; while it waits the line says so and the two build buttons are off");
  await answer("build 100");
  is(line(), `error: ${TOO_LARGE}`, "a refused build: the line is the server's reason, as an error");
  is([state(), built.length, $("btn-build").disabled, $("btn-solve").disabled, $("btn-resume").disabled], ["not built [tag]", 0, false, false, true],
    "and no tree is built: the tag, the page's other views, the buttons");
  is(cells().length, 4, "the estimates stay on show");

  // ---- another tree: its estimates are not these
  type($("pf-stack"), "0");
  is([note(), cells(), asking(), $("pf-estimate").disabled], ["", [], false, false], "another stack is another tree: the estimates go");
  later(300);
  await answer("describe 100");
  is(tree(), `error: ${NO_STACK}`, "settings that make no tree: the server's reason stands where the tree in words was");
  press($("pf-estimate"));
  await answer("estimate 100");
  is([note(), cells(), $("pf-estimate").disabled, waiting()], [`warn: ${NO_STACK}`, [], false, []], "an estimate the server refuses: its reason is the note, marked, and the asking ends");
  press($("pf-estimate"));
  is(note(), "Asking the solver…", "asked again, the note says so in place of the reason");
  await answer("estimate 100");
  type($("pf-stack"), "5");
  is(note(), "", "the reason goes with the form it was of");
  later(300);
  await answer("describe 100");

  // ---- an answer that comes after the form changed
  press($("pf-estimate"));
  is([waiting(), note()], [["estimate 100"], "Asking the solver…"], "Estimate memory asks for the chosen 100 flops");
  type($("pf-open"), "3");
  is([note(), asking(), $("pf-estimate").disabled], ["", false, false], "the form changes while the solver is asked: the note is cleared and the button is on");
  await answer("estimate 100");
  is([note(), cells(), waiting()], ["", [], []], "the answer for the form before is not shown, and its asking does not go on");
  type($("pf-open"), "2.5");
  later(300);
  await answer("describe 100");
  choose($("pf-flops"), "10");
  later(300);
  await answer("describe 10");
  is(tree(), "8 nodes before the flop · 1 way to a flop · 10 flops", "the tree in words is of the 10 flops chosen in the select");

  // ---- Build
  press($("btn-build"));
  is([waiting(), line()], [["build 10"], "Checking the tree and its memory, then building…"], "Build asks for the 10 flops chosen in the select");
  await answer("build 10");
  is(line(), "Built: 3,435 nodes · 10 flops · about 256 MB, with the tables that make a solve faster", "the line after Build: the nodes, the flops and the size the server answered");
  is(cells(), [["10 flops", "169–256 MB", "pf-mem-cell active", CHOSEN]], "the memory row takes the build's answer as the estimate of its number of flops");
  is([note(), asking()], [NOTE[10], false], "and the note reads it: both sizes, and the machine's memory");
  is([state(), built, $("btn-build").disabled, $("btn-solve").disabled, $("btn-resume").disabled], ["built [tag ok]", [{ bb: 100 }], false, false, false],
    "a tree is built: the tag, the big blind in chips for the page's other views, and Solve is on");
  press($("pf-estimate"));
  await answer("estimate 25", "the 10 flops are known from the build, so the next number is asked for");
  await answer("estimate 50");
  await answer("estimate 100");
  is([waiting(), cells().map((c) => c.slice(0, 2))], [[], [["10 flops", "169–256 MB"], ["25 flops", "423–654 MB"], ["50 flops", "836 MB – 1.3 GB"], ["100 flops", "1.6–2.5 GB"]]],
    "Estimate memory after Build asks for the other numbers only");

  // ---- the form changes while the server builds
  press($("btn-build"));
  type($("pf-open"), "3");
  await answer("build 10");
  is([cells(), note()], [[], ""], "a build that ends after the form changed: its sizes are not shown as the other form's");
  is(state(), "changed — rebuild [tag]", "and the tag says the form is no longer the built tree's");
  type($("pf-open"), "2.5");
  is(state(), "built [tag ok]", "back to the built form it says built");
  later(300);
  await answer("describe 10");

  // ---- Build & solve: the target is in bb/100, the solver's in chips
  type($("pf-accuracy"), "0.2");
  press($("btn-solve"));
  await answer("build 10");
  const solve = await answer("solve", "Build & solve starts the solve once the tree is built");
  check(near(solve.body.accuracy, 0.2) && solve.body.preview === false,
    `the solve is asked to 0.2 chips (0.2 bb/100 of a 100-chip big blind), without the fast preview (${JSON.stringify(solve.body)})`);
  is([texts(".toast", $("toasts")), waiting()], [["Solving…"], []], "and a toast says the solve runs");
  later(3400);
  is(texts(".toast", $("toasts")), [], "the toast goes after a while");

  // ---- the other form
  press(modeButton("postflop"));
  is([state(), line(), $("btn-resume").disabled, $("btn-resume").title],
    ["a preflop tree is built [tag]", "", true, "The tree in the solver is a preflop tree: switch to Preflop to solve it further"],
    "on the Postflop form the tag says a preflop tree is built, and Solve is off and says why");

  is([waiting(), clock.timers.length], [[], 0], "at the end the page waits for no answer and no timer");
  is(net.stopped, null, "every request the page made had a fixed answer, and none was made twice in one go");
} catch (e) {
  check(false, e instanceof Stopped ? e.message : `the test stopped on an error: ${e}${e && e.stack ? ` at ${String(e.stack).split("\n").slice(0, 3).join(" < ")}` : ""}`);
}
is(unknown, [], "the page touched nothing the stub document does not have");
await settle();
is(dropped, [], "no error of the page's code was left uncaught");

console.log(fails ? `${fails} FAILED` : "ALL PASSED");
process.exit(fails ? 1 : 0);
