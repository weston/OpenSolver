#!/usr/bin/env python3
"""Subgame re-solve support for the viewer (issue #117): re-solve a subtree with
different bet sizes, and search for the best N-size subset at a node.

This module owns a SECOND solver process (the same Solver class as server.py), so the
tree being browsed is left untouched. At a decision node X of the main tree it reads
both players' ranges (show_range), the chips already in the middle (show_node's pot
line) and the board, then builds a fresh tree in the subgame solver whose root stands
exactly at X:

    set_range OOP/IP   <reach weights at X, verbatim>
    set_board          <board at X>
    set_pot <oop> <ip> <dead>     (show_node's pot line, verbatim)
    set_eff_stack      <show_effective_stack>   (absolute stack: add_line amounts are
                                                 absolute and include the per-player
                                                 part of set_pot, so the lines the
                                                 tree builder emits from the pot line
                                                 land on the right totals)
    add_line ...       from tools/tree_builder.py, started at a Node whose investments
                       are the pot line's per-player parts

Solver tree roots are always OOP-to-act (src/action_tree.cpp), so when X is IP's
decision the subgame gets a forced-check wrapper root: the tree builder's start node
is OOP with the pot line's investments and OOP's bet sizes on the root street removed,
so the root's only child leads to X. In that case X itself is r:0:c inside the
subgame, not r:0 ("subgame_root" in the API tells the two apart).

Size search: given a candidate size list and N, evaluate N-size subsets (all of them
below BRUTE_FORCE_LIMIT combinations, greedy forward selection above it), solving each
to a loose accuracy, and report the acting player's EV at X for each candidate. With
"allin", every subset also offers the all-in, on top of its N sizes.

THIS IS UNSAFE RE-SOLVING. The opponent's range at X is frozen at its solved reach;
there is no gadget game grafting the opponent's counterfactual values, so the subgame
equilibrium ignores that an adaptive opponent could have avoided X with bad hands.
Same caveat as Pio's on-the-fly recalculations: fine for exploration, not a proof.

The HTTP endpoints registered under /api/resolve/* in server.py are documented in
README.md ("Viewer: subgame re-solve"). Tests: viewer/test_resolve.py.
"""

import itertools
import re
import threading
from math import comb

import server as vsrv

STREET_INDEX = {"flop": 0, "turn": 1, "river": 2}
STREET_NAME = {0: "flop", 1: "turn", 2: "river"}
CARD_TOKEN = re.compile(r"^[2-9TJQKA][cdhs]$")   # a dealt-card token inside a node id
BRUTE_FORCE_LIMIT = 12     # evaluate all C(k, n) subsets at or below this; greedy above
DEFAULT_ACCURACY = 0.5     # chips; for both re-solves and each search candidate
SOLVE_TIMEOUT = 24 * 3600  # a subgame solve can legitimately take a long time

BROWSE_MAP = {"/node": "/api/node", "/strategy": "/api/strategy", "/range": "/api/range",
              "/ev": "/api/ev", "/calc_results": "/api/calc_results", "/path": "/api/path",
              "/categories": "/api/categories", "/blockers": "/api/blockers"}

_tb = None  # memoized tools/tree_builder.py module


def _tree_builder():
    global _tb
    if _tb is None:
        _tb = vsrv.load_tree_builder()
    return _tb


# ---------------------------------------------------------------- config -> lines

def _field_sizes(b, key, allow_mult=False):
    """Size list from a JSON body, normalized to comma-separated entries: the same grammar and
    checks as /api/generate (viewer/server.py _check_sizes; tools/tree_builder.py)."""
    return vsrv._check_sizes(_tree_builder(), b, key, allow_mult)


def _config_header(b):
    """The sizing #Key#Value lines (pot/stack are added by the caller): the same per-player
    rules as /api/generate (viewer/server.py sizing_header, #141)."""
    return vsrv.sizing_header(b, _tree_builder())


def read_context(main, node_id, with_ranges=False):
    """Everything the subgame setup needs about node X of the main tree.

    show_node's pot line at a non-root node is the cumulative absolute investment of
    each player plus dead money (`an.inv[0] an.inv[1] an.dead`); those three numbers are
    exactly the set_pot arguments that make a fresh tree start at X.
    """
    node = vsrv.parse_node_block(main.cmd("show_node " + node_id))
    if node["type"] not in ("OOP_DEC", "IP_DEC"):
        raise vsrv.SolverError(f"{node['type']} is not a decision node")
    cards = node["board"].split()
    if len(cards) < 3:
        raise vsrv.SolverError("preflop nodes are not supported")
    if len(node["pot"]) != 3:
        raise vsrv.SolverError("unexpected pot line at node")
    actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
    ctx = {"id": node_id, "node_type": node["type"], "actor": actor,
           # the solver's roots are OOP-to-act; an IP-decision subgame starts behind
           # a forced-check wrapper, so X itself is the wrapper's check child
           "subgame_root": "r:0" if actor == "OOP" else "r:0:c",
           "board": "".join(cards), "street": STREET_NAME[len(cards) - 3],
           "pot": {"oop": int(node["pot"][0]), "ip": int(node["pot"][1]),
                   "dead": int(node["pot"][2])},
           "stack": int(float(main.cmd("show_effective_stack").strip()))}
    # X faces a bet on its own street: there only raise sizes matter (size search)
    ctx["facing"] = ctx["pot"]["oop"] != ctx["pot"]["ip"]
    if with_ranges:
        for who in ("OOP", "IP"):
            ctx[f"range_{who.lower()}"] = main.cmd(f"show_range {who} {node_id}")
    return ctx


def _prev_aggressor(tb, ctx):
    """The previous street's aggressor at ctx's node, recovered from its node id (#152).

    The id encodes the whole line (r:0:<action or dealt card>:...), so - unlike a bare
    node snapshot - it says who was aggressive on the street before the subgame root's
    street: an OOP lead there is then - correctly - a donk lead, and follows the donk
    rule like in a full build. Returns tb.OOP / tb.IP, or None (leads use bet sizes, the
    old approximation) when the id is missing, the root sits on the tree's first street,
    the previous street saw no aggression, or any token is unrecognized.
    """
    node_id = str(ctx.get("id", "") or "")
    tokens = node_id.split(":")[2:]  # drop "r" and the root token
    street = STREET_INDEX.get(ctx.get("street"))
    if street is None:
        return None
    # the street the id's first token is on: ctx's street minus the dealt cards in the id
    cur = street - sum(1 for t in tokens if CARD_TOKEN.match(t))
    if cur < 0:
        return None
    to_act, pending, aggr, prev = tb.OOP, False, None, None
    for tok in tokens:
        if CARD_TOKEN.match(tok):
            if pending:
                return None           # a card cannot arrive with a bet still open
            prev, aggr = aggr, None   # street boundary: remember who was aggressive
            cur += 1
            to_act, pending = tb.OOP, False
        elif tok == "c":
            if pending:
                pending = False       # a call ends the street (a card token follows)
            elif to_act == tb.OOP:
                to_act = tb.IP        # OOP checked; IP to act behind
            # IP's check behind also ends the street; the card token carries it
        elif tok.startswith("b") and tok[1:].isdigit():
            aggr, to_act, pending = to_act, 1 - to_act, True
        else:
            return None               # unknown token: don't guess
    return prev if cur == street else None


def start_node(tb, ctx):
    """The build_lines root for a subgame starting at ctx.

    Investments carry the chips already in, so a root facing a bet has unequal
    investments and the builder offers raises. The builder always starts at OOP: the
    solver's postflop tree roots are always OOP-to-act as well (src/action_tree.cpp),
    so when X is IP's decision the build starts from OOP with the same investments -
    the checked-through / already-bet state then re-emerges as the root's sole child
    (OOP's bet sizes on the root street are removed in build_config, so the check is
    its only line), and the node corresponding to X in the subgame is r:0:c instead
    of r:0 (ctx["subgame_root"]).

    The previous street's aggressor comes from the node id's token walk
    (_prev_aggressor, #152). It stays None - a lead on the root street then uses bet
    sizes, never donk sizes - when the id doesn't say (documented approximation).
    """
    return tb.Node(STREET_INDEX[ctx["street"]], (ctx["pot"]["oop"], ctx["pot"]["ip"]),
                   tb.OOP, False, _prev_aggressor(tb, ctx))


def build_config(tb, ctx, body, bet_override=None):
    """(Config, start Node) for the subgame at ctx.

    The builder's cfg.pot is only the DEAD part of the pot line and cfg.stack the
    absolute effective stack; the per-player chips live in the start node's
    investments, which the solver expects since add_line amounts include the
    per-player part of set_pot. bet_override = (street index, "a,b,c") replaces the
    acting player's bet sizes on that street (size search) - or its raise sizes when the
    street is X's own and X faces a bet, since bets cannot occur there.
    """
    body = dict(body)
    if bet_override is not None:
        s_idx, sizes = bet_override
        facing = ctx.get("facing", ctx["pot"]["oop"] != ctx["pot"]["ip"])
        kind = "raise" if facing and s_idx == STREET_INDEX[ctx["street"]] else "bet"
        key = f"{STREET_NAME[s_idx]}_{kind}"
        # The candidates are the acting player's sizes; the opponent keeps theirs (#141).
        # IP inherits OOP's field when it has none of its own, so pin it before overriding.
        if ctx["actor"] == "OOP":
            body.setdefault(key + "_ip", body.get(key, ""))
            body[key] = sizes
        else:
            body[key + "_ip"] = sizes
    header = [f"#Pot#{ctx['pot']['dead']}", f"#EffectiveStacks#{ctx['stack']}"]
    header += _config_header(body)
    cfg = tb.parse_config("\n".join(header))
    if ctx["actor"] == "IP":
        # X is IP's decision: the artificial OOP root above it may only check, so OOP
        # gets no bet (or donk) sizes on the root street. Those sizes are dead below
        # X anyway: once IP acts there, OOP only ever faces bets (i.e. raises).
        root_street = cfg.streets[STREET_INDEX[ctx["street"]]][tb.OOP]
        root_street.bet = []
        root_street.donk = []
    return cfg, start_node(tb, ctx)


# ---------------------------------------------------------------- size search

def _size_key(s):
    """Deterministic order for candidate sizes: percentages by value, then geometric
    by street count, then raise multiples, then the all-in. Order only affects row
    presentation, not what gets evaluated."""
    rank = {"pct": 0, "geo": 1, "mult": 2, "allin": 3}
    try:
        (spec,) = _tree_builder().parse_size_list(str(s))
    except ValueError:
        return (len(rank), 0.0, str(s))
    return (rank.get(spec.kind, len(rank)),
            float(spec.value) if spec.value is not None else 0.0, str(s))


def search_best_subset(candidates, n, score, brute_limit=BRUTE_FORCE_LIMIT,
                       progress=None):
    """Find the n-size subset of `candidates` with the highest score(subset).

    `score` gets a tuple of candidate strings and returns a number (the acting
    player's EV); each distinct subset is scored at most once per call except a
    deliberate re-score of the final pick (the row is updated in place).
    Brute force when C(k, n) <= brute_limit, greedy forward selection above it
    (evaluate every singleton, keep the best, extend it by the best next candidate,
    and so on: k + (k-1) + ... scores instead of C(k, n)).

    Returns (rows, best, method): rows has {"sizes": [...], "ev": float} per scored
    subset (greedy's rows include the shorter warm-up subsets), best is the chosen
    n-subset's row, method is "brute" or "greedy".
    """
    candidates = [str(c) for c in candidates]
    if not (1 <= n <= len(candidates)):
        raise vsrv.SolverError("need 1 <= n <= number of candidates")
    rows = []
    seen = {}

    def evaluate(sub):
        sub = tuple(sorted(sub, key=_size_key))
        ev = float(score(sub))
        if sub in seen:
            rows[seen[sub]]["ev"] = ev  # re-scored (final replay): update in place
        else:
            seen[sub] = len(rows)
            rows.append({"sizes": list(sub), "ev": ev})
        if progress:
            progress()
        return ev

    if comb(len(candidates), n) <= brute_limit:
        best = max((evaluate(sub), sub)
                   for sub in itertools.combinations(candidates, n))[1]
        return rows, {"sizes": list(sorted(best, key=_size_key)),
                      "ev": rows[seen[tuple(sorted(best, key=_size_key))]]["ev"]}, "brute"

    chosen = []
    while len(chosen) < n:
        remaining = [c for c in candidates if c not in chosen]
        best = max(remaining, key=lambda c: evaluate(tuple(chosen + [c])))
        chosen.append(best)
    final = tuple(sorted(chosen, key=_size_key))
    return rows, dict(rows[seen[final]]), "greedy"


def parse_evs(calc_results_text):
    """calc_results text -> {"OOP": ev, "IP": ev}."""
    out = {}
    for line in calc_results_text.splitlines():
        m = re.match(r"EV (OOP|IP): (-?[\d.e+]+)", line.strip())
        if m:
            out[m.group(1)] = float(m.group(2))
    if "OOP" not in out or "IP" not in out:
        raise vsrv.SolverError("could not parse calc_results")
    return out


def _accuracy(b, default=DEFAULT_ACCURACY):
    try:
        acc = float(b.get("accuracy", default))
    except (TypeError, ValueError):
        raise vsrv.SolverError("bad accuracy")
    if not (0 < acc <= 1e9):  # 0 or NaN would solve until the 24 h timeout (#357)
        raise vsrv.SolverError("bad accuracy")
    return acc


def _body_id(b):
    return vsrv.node_id_arg({"id": [str(b.get("id", ""))]})


# ---------------------------------------------------------------- the manager

class ResolveManager:
    """Owns the subgame solver process and the (single) background job running on it."""

    def __init__(self, main_solver, solver_cls, exe):
        self.main = main_solver          # vsrv.Solver browsing the original tree
        self.rsolver = solver_cls(exe)   # second process, for subgames
        self.mu = threading.Lock()       # guards everything below
        self.abort = False
        self.busy = False
        self.kind = None                 # "resolve" | "search" (kept after completion)
        self.error = None
        self.node = None                 # main-tree id of the subgame root
        self.ctx = None                  # last job's context (public parts only)
        self.rows = []                   # search comparison table
        self.done = 0
        self.total = 0
        self.method = None
        self.best = None
        self.allin = False               # search: every subset also offers the all-in
        self.result = None               # summary of the finished (re-)solve
        self.tree_ok = False             # rsolver currently holds a browsable tree
        self.carried = None              # main-tree locks set on the last subgame (#200)
        self.tree_gen = 0                # bumped by main_tree_changed: older jobs' results are dropped (#357)
        self.job_tree_gen = 0

    # ---- jobs (run on a daemon thread) ----

    def _launch(self, kind, node_id, job):
        with self.mu:
            if self.busy:
                raise vsrv.SolverError("a re-solve job is already running "
                                       "(POST /api/resolve/stop first)")
            self.busy, self.kind, self.node = True, kind, node_id
            self.error, self.rows, self.best, self.ctx = None, [], None, None
            self.done, self.total, self.method = 0, 0, None
            # The job rebuilds the subgame solver's tree: the last result no longer describes it
            # (shown after a Stop, or browsable after a failure, it would pass for this job's).
            self.result, self.tree_ok = None, False
            self.allin = False
            self.abort = False
            self.job_tree_gen = self.tree_gen
        threading.Thread(target=self._entry, args=(job,), daemon=True).start()

    def main_tree_changed(self):
        """The main solver built or loaded another tree (#357): a re-solve of the old one no longer
        belongs to anything shown. Stop a running job and forget the last result and subgame."""
        self.stop()
        with self.mu:
            self.tree_gen += 1
            self.result, self.tree_ok, self.node, self.ctx = None, False, None, None
            self.rows, self.best, self.error, self.carried = [], None, None, None

    def _entry(self, job):
        try:
            job()
        except Exception as e:
            with self.mu:
                if not self.abort:
                    self.error = str(e)
        finally:
            if self.abort or self.rsolver.dead:
                # cancelled or the process died: bring the subgame solver back so the
                # next job (or plain browsing after the next re-solve) starts clean
                try:
                    self.rsolver.restart()
                except Exception:
                    pass
                with self.mu:
                    self.tree_ok = False
            with self.mu:
                self.busy = False

    def stop(self):
        """Cancel a running job. The job thread's pending read dies with the process;
        it restarts the subgame solver before reporting back."""
        with self.mu:
            busy = self.busy
            if busy:
                self.abort = True
                # under the lock: a job finishing now can't clear busy in between, so an idle,
                # healthy subgame solver is never killed (#357)
                try:
                    self.rsolver.proc.kill()
                except Exception:
                    pass
        return {"ok": True, "was_busy": busy}

    def _build_and_solve(self, ctx, body, acc, bet_override=None, locks=None):
        """Configure rsolver for the subgame at ctx, build, set the carried locks, solve to
        acc; return lines. self.carried reports the locks ({applied, skipped}, main ids)."""
        tb = _tree_builder()
        cfg, start = build_config(tb, ctx, body, bet_override)
        lines = tb.build_lines(cfg, start)
        if not lines:
            raise vsrv.SolverError("config produced no lines (check bet/raise sizes)")
        pot = ctx["pot"]
        cmds = ["stop", "wait_for_solver",
                "set_range OOP " + ctx["range_oop"],
                "set_range IP " + ctx["range_ip"],
                "set_board " + ctx["board"],
                f"set_eff_stack {ctx['stack']}",
                f"set_pot {pot['oop']} {pot['ip']} {pot['dead']}",
                "set_isomorphism 1 1", "clear_lines"]
        cmds += tb.format_lines(lines)
        cmds.append("build_tree")
        self.rsolver.cmds(cmds)
        self.carried = self._apply_locks(locks)
        self.rsolver.cmds([f"set_accuracy {acc:g}", "go"])
        self.rsolver.cmd("wait_for_solver", timeout=SOLVE_TIMEOUT)
        return lines

    def _finish(self, ctx, acc, lines, extra=None):
        calc = self.rsolver.cmd("calc_results")
        count = self.rsolver.cmd("node_count")
        result = {"node": ctx["id"], "subgame_root": ctx["subgame_root"],
                  "street": ctx["street"], "board": ctx["board"],
                  "actor": ctx["actor"], "pot": ctx["pot"], "stack": ctx["stack"],
                  "accuracy": acc, "lines": len(lines), "node_count": count,
                  "calc_results": calc, "ev": parse_evs(calc)}
        if extra:
            result.update(extra)
        with self.mu:
            if self.job_tree_gen != self.tree_gen:  # the main tree changed meanwhile: not this tree's subgame
                return
            self.result = result
            self.tree_ok = True

    def _fresh_ctx(self, node_id):
        with self.mu:
            if self.busy:
                raise vsrv.SolverError("a re-solve job is already running "
                                       "(POST /api/resolve/stop first)")
        ctx = read_context(self.main, node_id, with_ranges=True)
        with self.mu:
            self.ctx = {k: v for k, v in ctx.items() if not k.startswith("range_")}
        return ctx

    def subtree_locks(self, ctx):
        """The main tree's viewer locks (#200) at X or below it, with ids moved into the
        subgame (X -> its subgame root). Bet tokens are absolute amounts and the subgame starts
        from X's investments, so a node keeps its id whenever the edited sizes still make it."""
        x = ctx["id"]
        if not any(i == x or i.startswith(x + ":") for i in list(self.main.locks)):  # a lock may land meanwhile
            return []
        out = []
        for lock in vsrv.op_lock_export(self.main)["locks"]:
            i = lock["id"]
            if i == x or i.startswith(x + ":"):
                out.append({**lock, "id": ctx["subgame_root"] + i[len(x):], "main_id": i})
        return out

    def _apply_locks(self, locks):
        """Set the carried locks on the freshly built subgame; {applied, skipped} in main ids."""
        self.rsolver.forget_locks()
        if not locks:
            return None
        back = {l["id"]: l["main_id"] for l in locks}
        r = vsrv.op_lock_apply(self.rsolver, {"locks": locks})
        return {"applied": [back.get(i, i) for i in r["applied"]],
                "skipped": [{**s, "id": back.get(s["id"], s["id"])} for s in r["skipped"]]}

    def start_resolve(self, node_id, body):
        acc = _accuracy(body)
        ctx = self._fresh_ctx(node_id)          # eager: bad nodes/sizes -> 400, not a job error
        build_config(_tree_builder(), ctx, body)
        locks = self.subtree_locks(ctx) if body.get("locks", True) else []

        def job():
            with self.mu:
                self.total = 1
            lines = self._build_and_solve(ctx, body, acc, locks=locks)
            with self.mu:
                self.done = 1
            self._finish(ctx, acc, lines, {"locks": self.carried} if self.carried else None)

        self._launch("resolve", node_id, job)

    def start_search(self, node_id, body):
        candidates = _field_sizes({"candidates": body.get("candidates", "")},
                                  "candidates", allow_mult=True).split(",")
        candidates = sorted(set(c for c in candidates if c), key=_size_key)
        # allin: the all-in is offered beside every subset, on top of its n sizes (not one of
        # them), so it is no longer a candidate itself
        allin = bool(body.get("allin"))
        extra = ("a",) if allin else ()
        if allin:
            candidates = [c for c in candidates if c.lower() != "a"]

        def sizes(sub):
            return ",".join(tuple(sub) + extra)
        if len(candidates) < 2:
            raise vsrv.SolverError("need at least 2 candidate sizes" +
                                   (" besides the all-in" if allin else ""))
        try:
            n = int(body["n"])
        except (KeyError, TypeError, ValueError):
            raise vsrv.SolverError("bad n")
        if not (1 <= n <= len(candidates)):
            raise vsrv.SolverError("need 1 <= n <= number of candidates")
        acc = _accuracy(body)
        ctx = self._fresh_ctx(node_id)
        s_idx = STREET_INDEX[ctx["street"]] if body.get("street") is None \
            else _street(body["street"])
        if s_idx < STREET_INDEX[ctx["street"]]:
            raise vsrv.SolverError("search street is before the subgame root street")
        # validate sizes eagerly -> 400 (incl. Nx candidates, which only raises accept)
        build_config(_tree_builder(), ctx, body, (s_idx, sizes(candidates)))

        def job():
            brute = comb(len(candidates), n) <= BRUTE_FORCE_LIMIT
            with self.mu:
                self.allin = allin
                self.method = "brute" if brute else "greedy"
                self.total = (comb(len(candidates), n) if brute
                              else n * (2 * len(candidates) - n + 1) // 2) + 1  # +1: final replay
            ev_pairs = {}

            def score(sub):
                self._build_and_solve(ctx, body, acc, (s_idx, sizes(sub)))
                evs = parse_evs(self.rsolver.cmd("calc_results"))
                ev_pairs[sub] = evs
                row = {"sizes": list(sub), "ev": evs[ctx["actor"]],
                       "ev_oop": evs["OOP"], "ev_ip": evs["IP"]}
                with self.mu:
                    self.done += 1
                    for i, r in enumerate(self.rows):  # keep the live table current
                        if r["sizes"] == list(sub):
                            self.rows[i] = row
                            break
                    else:
                        self.rows.append(row)
                return evs[ctx["actor"]]

            rows, best, method = search_best_subset(candidates, n, score)
            for row in rows:
                pair = ev_pairs[tuple(row["sizes"])]
                row["ev_oop"], row["ev_ip"] = pair["OOP"], pair["IP"]
            # leave the subgame solver holding the best subset's tree for browsing
            best_pair = ev_pairs[tuple(best["sizes"])]
            best["ev_oop"], best["ev_ip"] = best_pair["OOP"], best_pair["IP"]
            with self.mu:
                self.rows, self.best = rows, best
            lines = self._build_and_solve(ctx, body, acc, (s_idx, sizes(best["sizes"])))
            with self.mu:
                self.done += 1
            self._finish(ctx, acc, lines, {"search": {"street": STREET_NAME[s_idx],
                                                      "n": n, "candidates": candidates,
                                                      "allin": allin, "method": method}})

        self._launch("search", node_id, job)

    # ---- HTTP ----

    def status(self):
        with self.mu:
            return {"busy": self.busy, "kind": self.kind, "node": self.node,
                    "error": self.error, "done": self.done, "total": self.total,
                    "method": self.method, "allin": self.allin, "rows": [dict(r) for r in self.rows],
                    "best": self.best, "context": self.ctx, "result": self.result,
                    "tree": self.tree_ok, "solve": self.rsolver.state()}

    def api(self, method, path, query, body):
        sub = path[len("/api/resolve"):]
        if sub == "/context":
            if method != "GET":
                raise vsrv.SolverError("GET required")
            return 200, read_context(self.main, vsrv.node_id_arg(query))
        if sub == "/status":
            if method != "GET":
                raise vsrv.SolverError("GET required")
            return 200, self.status()
        if sub in BROWSE_MAP:
            with self.mu:
                if not self.tree_ok:
                    raise vsrv.SolverError("no subgame tree (run a re-solve first)")
                if self.busy:
                    raise vsrv.SolverError("re-solve in progress")
            return vsrv._api_main(self.rsolver, None, "GET", BROWSE_MAP[sub], query, None)
        if method != "POST":
            raise vsrv.SolverError("POST required")
        b = vsrv.need_body(body)
        if sub == "/start":
            self.start_resolve(_body_id(b), b)
            return 200, {"started": "resolve"}
        if sub == "/search":
            self.start_search(_body_id(b), b)
            return 200, {"started": "search"}
        if sub == "/stop":
            return 200, self.stop()
        return 404, {"error": "unknown endpoint"}


def _street(name):
    name = vsrv.no_newlines(str(name), "street").strip().lower()
    if name not in STREET_INDEX:
        raise vsrv.SolverError("street must be flop, turn or river")
    return STREET_INDEX[name]
