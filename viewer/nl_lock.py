"""Plain-English node locks (#200, part 2): Kimi K3 via Sail turns a request into a lock spec.

The model never writes solver commands. It gets a compact summary of the node (street, board,
actions, the acting range by hand class and by category with reach weights, the solved
frequencies) and answers with a JSON spec:

    {"ok": true, "explanation": "...", "rules": [RULE, ...]}      or  {"ok": false, "explanation": "..."}

    RULE = {"hands": SEL, "mix": {"<action>": weight, ...}}
             the selected hands play this mix (weights are normalised; unlisted actions get 0)
         | {"hands": SEL, "target": {"action": "<action>", "overall": 0.25, "order": ORDER}}
             the action's share of the WHOLE range at the node becomes exactly `overall`, and
             only the selected hands change: they take the action in ORDER, each 100% until the
             target is met (one equal-strength group gets a fraction), the rest of them 0%
    SEL    = "all" | {"classes": ["AA", "AKs", ...], "categories": ["flush_draw", ...],
                      "exclude_classes": [...], "exclude_categories": [...]}
    ORDER  = "strongest" | "weakest" (equity vs the opponent's range at the node) | "current"
             (the hands that play the action most now, i.e. the solver's own preference) |
             "proportional" (no hand goes pure: every selected hand scales its frequency, as the
             editor's Overall column does; for a target with no hand detail)

Rules apply in order, each on the result of the ones before. The server checks the spec, does
the threshold arithmetic (reach-weighted, so the target is exact) and returns the proposed
strategy; nothing is locked here. The browser shows it in the lock editor (lock.js), where it can
be edited and is locked with the usual buttons.

The API key comes from $SAIL_API_KEY or ~/.config/sailresearch/api_key. It is only ever sent to
api.sailresearch.com, never returned to the browser, logged, or written anywhere.
"""
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

HOST = "api.sailresearch.com"
URL = f"https://{HOST}/v1/chat/completions"
MODEL = "moonshotai/Kimi-K3"
WINDOW = "asap"
# USD per 1M tokens, Kimi K3 in the asap window (docs.sailresearch.com/pricing, 2026-11)
PRICE = {"input": 1.50, "output": 10.00}
MAX_TOKENS = 4000
NUM = 1326
ORDERS = ("strongest", "weakest", "current", "proportional")


class NLError(Exception):
    """A request the model or the server could not turn into a lock (shown to the user)."""


def key_path():
    return os.path.join(os.path.expanduser("~"), ".config", "sailresearch", "api_key")


def load_key():
    """The Sail key, or None. Never logged or returned."""
    k = os.environ.get("SAIL_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(key_path(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


class SailClient:
    """One chat completion against Sail. The key is held here and goes in one header only."""

    def __init__(self, key, timeout=180):
        self._key = key
        self.timeout = timeout
        self.opener = urllib.request.urlopen   # tests swap in a fake

    def complete(self, messages):
        body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": MAX_TOKENS,
                           "temperature": 0, "metadata": {"completion_window": WINDOW}}).encode()
        req = urllib.request.Request(URL, data=body, method="POST", headers={
            "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
            "X-Sail-Completion-Window": WINDOW})
        assert urllib.parse.urlsplit(req.full_url).hostname == HOST
        try:
            with self.opener(req, timeout=self.timeout) as f:
                data = json.load(f)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("error", {})
                detail = detail.get("message", "") if isinstance(detail, dict) else str(detail)
            except Exception:
                pass
            raise NLError(f"Sail returned HTTP {e.code}{': ' + detail[:300] if detail else ''}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NLError(f"could not reach {HOST}: {getattr(e, 'reason', e)}") from None
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise NLError("unexpected response from Sail") from None
        u = data.get("usage") or {}
        return text, {"input_tokens": int(u.get("prompt_tokens") or 0),
                      "output_tokens": int(u.get("completion_tokens") or 0)}


def cost_usd(input_tokens, output_tokens):
    return (input_tokens * PRICE["input"] + output_tokens * PRICE["output"]) / 1e6


def estimate_tokens(text):
    return max(1, round(len(text) / 3.5))


# ------------------------------------------------------------------ hands

def action_label(tok, facing, pot):
    if tok == "f":
        return "fold"
    if tok == "c":
        return "call" if facing else "check"
    m = re.fullmatch(r"[br](\d+)", tok)
    if m:
        amt = int(m.group(1))
        if facing:
            return f"raise to {amt}"
        return f"bet {amt} ({100 * amt / pot:.0f}% pot)" if pot > 0 else f"bet {amt}"
    return tok


# ------------------------------------------------------------------ node context

def node_context(srv, solver, node_id):
    """Everything the proposal needs about a decision node, read from the solver.

    `srv` is the viewer's server module (its parsers); returns a dict with the 1326-long arrays
    (reach, freq rows, equity, made/draw category ids) plus the summary sent to the model."""
    with solver.cmd_lock:
        node = srv.parse_node_block(solver.cmd("show_node " + node_id))
        if node["type"] not in ("OOP_DEC", "IP_DEC"):
            raise NLError(f"{node['type']} is not a decision node")
        actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
        other = "IP" if actor == "OOP" else "OOP"
        kids = srv.parse_children(solver.cmd("show_children " + node_id))
        rows = [l for l in solver.cmd("show_strategy " + node_id).split("\n") if l.strip()]
        if any(len(r.split()) != NUM for r in rows):
            raise NLError("plain-English locks need a postflop node")
        freq = [[v or 0.0 for v in srv.parse_floats(r, NUM)] for r in rows]
        reach = [v or 0.0 for v in srv.parse_floats(solver.cmd(f"show_range {actor} {node_id}"), NUM)]
        eq_line = solver.cmd(f"calc_eq_node {actor} {node_id}").split("\n")[0]
        equity = [v if v is not None else -1.0 for v in srv.parse_floats(eq_line, NUM)]
        board = "".join(node["board"].split())
        names = solver.cmd("show_category_names").split("\n")
        made_names, draw_names = names[0].split(), names[1].split()
        cats = solver.cmd("show_categories " + board).split("\n")
        made = [int(t) for t in cats[0].split()]
        draw = [int(t) for t in cats[1].split()]
        order = srv.hand_order(solver)
    # the pot line is (OOP's chips in, IP's chips in, dead money): all of it is the pot
    pot = sum(node["pot"]) if node.get("pot") else 0
    toks = [k["id"].rsplit(":", 1)[-1] for k in kids]
    facing = "f" in toks
    acts = [{"key": t, "label": action_label(t, facing, pot)} for t in toks]
    street = {6: "flop", 8: "turn", 10: "river"}.get(len(board), "?")
    ctx = {"id": node_id, "actor": actor, "opponent": other, "board": board, "street": street,
           "pot": node.get("pot"), "acts": acts, "freq": freq, "reach": reach, "equity": equity,
           "made": made, "draw": draw, "made_names": made_names, "draw_names": draw_names,
           "order": order, "classes": [srv.combo_class(c) for c in order]}
    return ctx


def _cats(ctx, i):
    """Made-hand and draw category names of combo i (empty for a blocked combo)."""
    out = set()
    for ids, names in ((ctx["made"], ctx["made_names"]), (ctx["draw"], ctx["draw_names"])):
        if 0 <= ids[i] < len(names):
            out.add(names[ids[i]])
    return out


def _overall(ctx, freq, combos=None):
    idx = range(NUM) if combos is None else combos
    tot = sum(ctx["reach"][i] for i in idx)
    return [sum(ctx["reach"][i] * row[i] for i in idx) / tot if tot > 0 else 0.0 for row in freq], tot


def summary(ctx):
    """The compact view of the node the model gets (no tree, no solver ids beyond the node)."""
    keys = [a["key"] for a in ctx["acts"]]
    pct = lambda xs: {k: round(100 * x, 1) for k, x in zip(keys, xs)}
    by_class, by_cat = {}, {}
    for i in range(NUM):
        w = ctx["reach"][i]
        if w <= 0:
            continue
        by_class.setdefault(ctx["classes"][i], []).append(i)
        for name in _cats(ctx, i):
            if name != "no_draw":
                by_cat.setdefault(name, []).append(i)
    tot = sum(ctx["reach"])
    def rows(groups):
        out = []
        for name, idx in groups.items():
            f, w = _overall(ctx, ctx["freq"], idx)
            out.append({"hands": name, "share_pct": round(100 * w / tot, 2), "mix_pct": pct(f)})
        return sorted(out, key=lambda r: -r["share_pct"])
    overall, _ = _overall(ctx, ctx["freq"])
    return {
        "street": ctx["street"], "board": ctx["board"], "player_to_act": ctx["actor"],
        "pot_chips": ctx["pot"],
        "actions": [{"key": a["key"], "means": a["label"]} for a in ctx["acts"]],
        "overall_mix_pct": pct(overall),
        "range_by_class": rows(by_class),
        "range_by_category": rows(by_cat),
        "category_names": {"made": ctx["made_names"], "draws": ctx["draw_names"]},
    }


SYSTEM = """You turn a poker player's plain-English description of a strategy into a JSON lock spec \
for a solver. You never compute the exact hand thresholds: the server does that. Reply with one JSON \
object and nothing else.

Spec:
{"ok": true, "explanation": "<one or two sentences: what the lock does, in plain words>",
 "rules": [RULE, ...]}
or, when the request is ambiguous, impossible at this node (an action that does not exist), or not a \
strategy: {"ok": false, "explanation": "<why, and what the user could ask instead>"}

RULE is one of:
 {"hands": SEL, "mix": {"<action key>": <weight>, ...}}
    the selected hands play exactly this mix (weights are normalised; actions left out get 0)
 {"hands": SEL, "target": {"action": "<action key>", "overall": <0..1>, "order": ORDER}}
    the action's share of the WHOLE range becomes exactly `overall`; only the selected hands change,
    taking the action in ORDER until the target is met, the rest of them not at all
SEL: "all" or {"classes": [...], "categories": [...], "exclude_classes": [...], "exclude_categories": [...]}
    classes are matrix classes such as "AA", "AKs", "T9o" (list them explicitly, no "+" or "-");
    categories are names from category_names
ORDER: "strongest" | "weakest" (by equity against the opponent's range: those hands go pure first) | \
"current" (the hands that play the action most now go pure first) | "proportional" (every selected \
hand scales its frequency and nobody goes pure; use it when the request gives no hand detail)

Rules apply in order. "X never does A" is a target of 0 for A over "all". "X does A 60%" with no \
hand detail is a target over "all" with order "proportional". Use only the action keys given. Overall shares refer \
to the whole range of the player to act at this node."""


def build_messages(ctx, text):
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Node:\n" + json.dumps(summary(ctx), separators=(",", ":"))
             + "\n\nRequest: " + text.strip()}]


def parse_reply(text):
    """The first JSON object in the reply (models sometimes wrap it in a code fence)."""
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.S)
    if m:
        s = m.group(1)
    start = s.find("{")
    if start < 0:
        raise NLError("the model did not answer with a lock spec")
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
    except json.JSONDecodeError:
        raise NLError("the model's lock spec is not valid JSON") from None
    if not isinstance(obj, dict):
        raise NLError("the model's lock spec is not a JSON object")
    return obj


# ------------------------------------------------------------------ spec -> strategy

def _select(ctx, sel):
    """Combos (live: reach > 0) a selector picks."""
    live = [i for i in range(NUM) if ctx["reach"][i] > 0]
    if sel == "all":
        return live
    if not isinstance(sel, dict):
        raise NLError("hands must be \"all\" or an object with classes / categories")
    known_cats = set(ctx["made_names"]) | set(ctx["draw_names"])
    def names(field):
        v = sel.get(field) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise NLError(f"{field} must be a list of names")
        return v
    classes, cats = set(names("classes")), set(names("categories"))
    xclasses, xcats = set(names("exclude_classes")), set(names("exclude_categories"))
    all_classes = set(ctx["classes"])
    bad = sorted((classes | xclasses) - all_classes)
    if bad:
        raise NLError(f"unknown hand classes: {', '.join(bad[:8])}")
    bad = sorted((cats | xcats) - known_cats)
    if bad:
        raise NLError(f"unknown categories: {', '.join(bad[:8])}")
    cats_of = lambda i: _cats(ctx, i)
    out = []
    for i in live:
        if not (classes or cats) or ctx["classes"][i] in classes or cats_of(i) & cats:
            if ctx["classes"][i] not in xclasses and not cats_of(i) & xcats:
                out.append(i)
    return out


def _groups(ctx, combos, order, act_row):
    """Combos in fill order, in groups that must share one frequency: same class and same
    equity (suit-isomorphic combos), so a fractional threshold never splits them."""
    g = {}
    for i in combos:
        g.setdefault((ctx["classes"][i], round(ctx["equity"][i], 6), round(act_row[i], 6)), []).append(i)
    keyf = {"strongest": lambda k: (-k[1], k[0]), "weakest": lambda k: (k[1], k[0]),
            "current": lambda k: (-k[2], -k[1], k[0])}[order]
    return [g[k] for k in sorted(g, key=keyf)]


def _set_action(freq, i, a, v):
    """Combo i plays action a with frequency v; the other actions keep their proportions."""
    others = [k for k in range(len(freq)) if k != a]
    rest = sum(freq[k][i] for k in others)
    for k in others:
        freq[k][i] = (1 - v) * (freq[k][i] / rest if rest > 1e-12 else 1 / len(others))
    freq[a][i] = v


def _scale(freq, a, need, combos, reach):
    """Proportional target: the chosen hands supply `need` reach-weighted mass of action a.
    Down: each plays it k times as often. Up: each moves the same fraction t of the way to
    always, so hands that never played it start to. Linear in k / t, so the target is exact."""
    have = sum(reach[i] * freq[a][i] for i in combos)
    if need <= have:
        k = need / have if have > 0 else 0.0
        for i in combos:
            _set_action(freq, i, a, k * freq[a][i])
    else:
        gap = sum(reach[i] * (1 - freq[a][i]) for i in combos)
        t = (need - have) / gap if gap > 0 else 0.0
        for i in combos:
            _set_action(freq, i, a, freq[a][i] + t * (1 - freq[a][i]))


def apply_spec(ctx, spec):
    """Spec -> (proposed freq rows, notes). Raises NLError on anything invalid."""
    keys = [a["key"] for a in ctx["acts"]]
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        raise NLError("the spec has no rules")
    freq = [row[:] for row in ctx["freq"]]
    reach = ctx["reach"]
    R = sum(reach)
    if R <= 0:
        raise NLError("nobody reaches this node")
    notes = []
    for n, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise NLError(f"rule {n} is not an object")
        combos = _select(ctx, rule.get("hands", "all"))
        if not combos:
            raise NLError(f"rule {n} selects no hands in the range")
        if "mix" in rule:
            mix = rule["mix"]
            if not isinstance(mix, dict) or not mix:
                raise NLError(f"rule {n}: mix must be an object of action weights")
            bad = [k for k in mix if k not in keys]
            if bad:
                raise NLError(f"rule {n}: no action {', '.join(map(str, bad))} here (actions: {', '.join(keys)})")
            w = [mix.get(k, 0) for k in keys]
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in w) or sum(w) <= 0:
                raise NLError(f"rule {n}: mix weights must be non-negative numbers, not all 0")
            s = sum(w)
            for i in combos:
                for k in range(len(keys)):
                    freq[k][i] = w[k] / s
        elif "target" in rule:
            t = rule["target"]
            if not isinstance(t, dict) or t.get("action") not in keys:
                raise NLError(f"rule {n}: the target needs an action from {', '.join(keys)}")
            a = keys.index(t["action"])
            goal = t.get("overall")
            if isinstance(goal, bool) or not isinstance(goal, (int, float)) or not 0 <= goal <= 1:
                raise NLError(f"rule {n}: overall must be a share from 0 to 1")
            order = t.get("order", "current")
            if order not in ORDERS:
                raise NLError(f"rule {n}: order must be one of {', '.join(ORDERS)}")
            chosen = set(combos)
            fixed = sum(reach[i] * freq[a][i] for i in range(NUM) if i not in chosen) / R
            room = sum(reach[i] for i in combos) / R
            if goal < fixed - 1e-9 or goal > fixed + room + 1e-9:
                lo, hi = 100 * fixed, 100 * (fixed + room)
                raise NLError(f"rule {n}: {keys[a]} at {100 * goal:.1f}% overall is out of reach when only "
                              f"those hands change (it can go from {lo:.1f}% to {hi:.1f}%)")
            need = (goal - fixed) * R          # reach-weighted mass the chosen hands must supply
            if order == "proportional":
                _scale(freq, a, need, combos, reach)
            else:
                for grp in _groups(ctx, combos, order, freq[a]):
                    w = sum(reach[i] for i in grp)
                    v = 1.0 if need >= w else max(0.0, need / w)
                    for i in grp:
                        _set_action(freq, i, a, v)
                    need -= w * v
            got = sum(reach[i] * freq[a][i] for i in range(NUM)) / R
            notes.append(f"{ctx['acts'][a]['label']}: {100 * got:.2f}% of the range")
        else:
            raise NLError(f"rule {n} needs a mix or a target")
    return freq, notes


class NLService:
    """POST /api/nl/propose and GET /api/nl/status. `client` is a SailClient or a test fake."""

    def __init__(self, client=None):
        if client is None:
            key = load_key()
            client = SailClient(key) if key else None
        self.client = client

    def status(self):
        if self.client is None:
            return {"enabled": False, "model": MODEL,
                    "reason": "No Sail API key: set SAIL_API_KEY (or put it in ~/.config/sailresearch/api_key, "
                              "chmod 600) and restart the viewer."}
        return {"enabled": True, "model": MODEL, "window": WINDOW, "price_per_mtok": PRICE}

    def propose(self, srv, solver, b):
        if self.client is None:
            raise NLError(self.status()["reason"])
        text = b.get("text")
        if not isinstance(text, str) or not text.strip():
            raise NLError("describe the lock first")
        if len(text) > 2000:
            raise NLError("keep the request under 2000 characters")
        node_id = srv.node_id_arg({"id": [str(b.get("id", ""))]})
        ctx = node_context(srv, solver, node_id)
        messages = build_messages(ctx, text)
        reply, usage = self.client.complete(messages)
        cost = {**usage, "usd": round(cost_usd(usage["input_tokens"], usage["output_tokens"]), 5)}
        spec = parse_reply(reply)
        before, _ = _overall(ctx, ctx["freq"])
        out = {"id": node_id, "spec": spec, "explanation": str(spec.get("explanation", ""))[:1000], "cost": cost,
               "actions": [a["key"] for a in ctx["acts"]], "overall_before": before}
        if not spec.get("ok"):
            return {**out, "ok": False}
        try:
            freq, notes = apply_spec(ctx, spec)
        except NLError as e:
            return {**out, "ok": False, "error": str(e)}
        after, _ = _overall(ctx, freq)
        changed = sorted({ctx["classes"][i] for i in range(NUM) if ctx["reach"][i] > 0 and any(
            abs(freq[k][i] - ctx["freq"][k][i]) > 1e-9 for k in range(len(freq)))})
        return {**out, "ok": True, "freq": freq, "overall_after": after, "notes": notes, "classes": changed}

    def api(self, srv, solver, method, path, body):
        try:
            if path == "/api/nl/status":
                return 200, self.status()
            if path == "/api/nl/estimate":
                if method != "POST":
                    raise NLError("POST required")
                b = srv.need_body(body)
                node_id = srv.node_id_arg({"id": [str(b.get("id", ""))]})
                msgs = build_messages(node_context(srv, solver, node_id), str(b.get("text", "")))
                tin = estimate_tokens("".join(m["content"] for m in msgs))
                return 200, {"input_tokens": tin, "usd_input": round(cost_usd(tin, 0), 5),
                             "usd_max": round(cost_usd(tin, MAX_TOKENS), 5)}
            if path == "/api/nl/propose":
                if method != "POST":
                    raise NLError("POST required")
                return 200, self.propose(srv, solver, srv.need_body(body))
        except NLError as e:
            raise srv.SolverError(str(e)) from None
        return 404, {"error": "unknown endpoint"}
