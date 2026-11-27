#!/usr/bin/env python3
"""Fast preview: hide the solve time (viewer "Fast preview" checkbox, on by default).

1. Build & solve shows a progress line on the Build view (no dialog). Once the main solve's
   exploitability is within PREVIEW_FACTOR (10, the owner's choice) x the target, the strategy
   counts as readable and the Study view opens on the still-solving tree (server.py records the
   target; /api/progress reports {"preview": ...}).
2. The node on screen refreshes at each progress report of the main solve.
3. When you look at a node on a later street, the subtree from that street's first decision is
   re-solved here (RefineManager):
   - ranges are the main solve's current reaches at that street root;
   - bet sizes are the ones the tree was built with (server-side, not the form as it is now);
   - the accuracy is the build's target, as a share of that node's pot.
   The main solve is parked for the second or so that takes, so the street in front of you gets
   the whole machine, then it resumes exactly where it was (iterations are deterministic). A
   refine is redone when the main solve has reported progress since (RefineManager._stale).

A flop sketch (a smaller tree solved first, #241-#243) used to show the flop within seconds; it
was removed because it added its own solve time (about 11%) to every solve, and running it
alongside the main solve saved nothing (the same CPU work either way).

While a refined subtree exists, the browse endpoints (/api/strategy, /api/range, /api/ev,
/api/node, /api/categories) answer for nodes inside it from the refine solver, with node ids
translated both ways, and mark the payload "refined". Anything that fails there (an action
the rebuilt subgame does not have, say) falls back to the main tree.

This is the same unsafe subgame re-solving as viewer/resolve.py: the opponent's range at the
street root is frozen at its current reach. It's good for showing a sensible strategy fast;
the main solve is still the real answer.

Endpoints:
- POST /api/refine/view {id, accuracy_pct, sizes...}: the client calls it on every
  navigation. It starts a refine of id's street root if none is ready or running.
- GET /api/refine/status
- POST /api/refine/clear: drop the refined subtree; the overlay stops.
"""

import re
import threading
import time

import server as vsrv
import resolve as vresolve

CARD = re.compile(r"^[2-9TJQKA][cdhs]$")
OVERLAY = ("/api/path", "/api/strategy", "/api/range", "/api/ev", "/api/node", "/api/categories", "/api/blockers",
           "/api/graphs", "/api/hand_lines")


def street_root(node_id):
    """(root, rest): root is the id through the FIRST dealt card (the first decision of the
    street after the tree's root street), rest the tokens below it. (None, None) on the
    root street."""
    toks = node_id.split(":")
    for i, t in enumerate(toks):
        if CARD.match(t):
            return ":".join(toks[:i + 1]), toks[i + 1:]
    return None, None


_rewrite_ids = vresolve.rewrite_ids


class RefineManager(vresolve.ResolveManager):
    """A third solver process for street refines, plus the main solve's pause/resume."""

    def __init__(self, main_solver, solver_cls, exe):
        super().__init__(main_solver, solver_cls, exe)
        self.gen = 0            # bumped when the main tree changes: older results are void
        self.root = None        # main-tree id of the street root being refined / refined
        self.ready = None       # finished refine: {root, sub_root, accuracy, seconds, explo_main, ...}
        self.pending = None     # (root, body, pct) queued behind a cancelled job

    # ---- main solve pause / resume ----

    def _pause_main(self):
        st = self.main.state()
        if not st["solving"] or st["partial"]:
            return False
        try:
            self.main.hold("refine")   # parks the solve between iterations
        except vsrv.SolverError:
            return False
        return True

    def _resume_main(self):
        # A no-op if the user stopped or rebuilt meanwhile: that dropped the hold already.
        self.main.unhold("refine")

    # ---- jobs ----

    def invalidate(self):
        """The main tree changed (build, load, restart): forget results, cancel the job."""
        with self.mu:
            self.gen += 1
            self.ready, self.root, self.pending, self.error = None, None, None, None
            self.stop()

    def _start(self, root, body, pct, smart):
        with self.mu:
            gen = self.gen
            self.busy, self.kind, self.node, self.root = True, "refine", root, root
            self.ready, self.error, self.abort = None, None, False

        def job():
            tp = time.time()
            paused = self._pause_main()
            try:
                t0 = time.time()
                phases = {"pause": round(t0 - tp, 2)}
                with self.main.cmd_lock:
                    with self.main.state_lock:
                        points = len(self.main.series)
                        events = self.main.events
                    ctx = vresolve.read_context(self.main, root, with_ranges=True)
                    commands = self.main.cmd("show_tree_params turn_build " + root).splitlines() if smart else None
                    algorithm = self.main.device_algorithm()
                phases["context"] = round(time.time() - t0, 2)
                pot = ctx["pot"]["oop"] + ctx["pot"]["ip"] + ctx["pot"]["dead"]
                acc = max(pct, 0.01) / 100.0 * pot
                with self.mu:
                    self.ctx = {k: v for k, v in ctx.items() if not k.startswith("range_")}
                t1 = time.time()
                self.rsolver.cmd("set_algorithm " + algorithm)
                if smart:
                    self._solve_configured(ctx, commands, acc)
                    lines = [line for line in commands if line.startswith("add_line ")]
                else:
                    lines = self._build_and_solve(ctx, body, acc)
                phases["build+solve"] = round(time.time() - t1, 2)
                t1 = time.time()
                self._finish(ctx, acc, lines)
                phases["finish"] = round(time.time() - t1, 2)
                with self.mu:
                    if gen == self.gen and not self.abort:
                        self.ready = {"root": root, "sub_root": ctx["subgame_root"], "street": ctx["street"],
                                      "accuracy": acc, "accuracy_pct": pct, "pot": pot,
                                      "seconds": round(time.time() - t0, 2),
                                      "main_points": points, "main_events": events, "smart_turn": smart,
                                      "main_paused": paused, "phases": phases}
            finally:
                if paused:
                    self._resume_main()

        threading.Thread(target=self._entry, args=(job,), daemon=True).start()

    def _entry(self, job):
        super()._entry(job)
        with self.mu:
            if self.pending and not self.busy:
                nxt, self.pending = self.pending, None
                self._start(*nxt)

    def view(self, node_id, body):
        root, _ = street_root(node_id)
        if root is None:
            return {"state": "none"}
        node = vsrv.parse_node_block(self.main.cmd("show_node " + root))
        smart = "SMART_TURN" in node["flags"].split()
        if node["type"] not in ("OOP_DEC", "IP_DEC") or (not smart and self._main_done()):
            return {"state": "none"}
        try:
            pct = float(body.get("accuracy_pct", 0.1 if smart else 0.3))
        except (TypeError, ValueError):
            raise vsrv.SolverError("bad accuracy_pct")
        if not (0 < pct < float("inf")):
            raise vsrv.SolverError("bad accuracy_pct")
        built = self.main.built_sizes
        if built and not smart:
            body = dict(body, **built)
        with self.mu:
            if self.ready and self.ready["root"] == root and not self._stale(self.ready):
                return {"state": "ready", "root": root, "refine": dict(self.ready)}
            if not smart and not self._main_running():
                return {"state": "none"}
            if self.busy:
                if self.root != root or self.abort:
                    self.pending = (root, body, pct, smart)
                    if not self.abort:
                        self.stop()
            else:
                self.pending = None
                self._start(root, body, pct, smart)
            return {"state": "running", "root": root}

    def _main_done(self):
        """The main solve met its target: from then on it is the answer, refined or not. A tree loaded
        from a file is the answer too (#369: it used to be re-solved and shown instead of itself, with
        the Build form's sizes, as a loaded tree has no built ones)."""
        if getattr(self.main, "loaded_file", False):
            return True
        # A preflop tree built here (#492) has many flops and its own postflop sizes: a street of it
        # re-solved with the Build form's postflop sizes would be another game, shown in its place.
        if getattr(self.main, "preflop", None):
            return True
        with self.main.state_lock:
            return not self.main.solving and not self.main.suspended and \
                "required accuracy" in self.main.solver_status

    def _main_running(self):
        """A main solve is under way (or parked for a refine): only then does a new refine start (#369:
        browsing a stopped tree started refines and labelled them as running in the background)."""
        with self.main.state_lock:
            return self.main.solving or self.main.suspended

    def _stale(self, r):
        """A refine is out of date once its ranges are: the main solve has reported progress
        since (so redone at most once per report, and reports are spaced geometrically in
        iterations)."""
        with self.main.state_lock:
            if r["smart_turn"] and not self.main.solving:
                return self.main.events != r["main_events"]
            return len(self.main.series) > r["main_points"]

    def translate(self, node_id):
        """Refine-solver id for a main-tree id inside the refined subtree, else None."""
        with self.mu:
            r = self.ready
            if not r or self.busy or not self.tree_ok or (not r["smart_turn"] and self._main_done()):
                return None, None
        root, rest = street_root(node_id)
        if root != r["root"]:
            return None, None
        return r["sub_root"] + ("" if not rest else ":" + ":".join(rest)), r

    def overlay(self, path, query):
        """Serve a browse request from the refined subtree, or None to use the main tree."""
        if path not in OVERLAY or "id" not in query:
            return None
        with self.mu:
            sub, info = self.translate(query["id"][0])
            if sub is None or (path in ("/api/path", "/api/hand_lines") and not info["smart_turn"]):
                return None
            q = dict(query)
            q["id"] = [sub]
            try:
                status, data = vsrv._api_main(self.rsolver, None, "GET", path, q, None)
            except vsrv.SolverError:
                return None
            if status != 200 or not isinstance(data, dict):
                return None
            _rewrite_ids(data, info["sub_root"], info["root"])
            data["refined"] = {k: info[k] for k in ("root", "street", "accuracy", "accuracy_pct", "seconds", "smart_turn")}
        if path == "/api/path":
            vresolve.prepend_main_path(self.main, data, info["root"], info["sub_root"])
        return status, data

    def refine_status(self):
        with self.mu:
            return {"busy": self.busy or self.pending is not None, "root": self.root, "error": self.error, "gen": self.gen,
                    "ready": dict(self.ready) if self.ready else None,
                    "main_suspended": self.main.suspended}

    def api(self, method, path, query, body):
        sub = path[len("/api/refine"):]
        if sub == "/status":
            return 200, self.refine_status()
        if method != "POST":
            raise vsrv.SolverError("POST required")
        b = vsrv.need_body(body)
        if sub == "/view":
            return 200, self.view(vsrv.node_id_arg({"id": [str(b.get("id", ""))]}), b)
        if sub == "/clear":
            self.invalidate()
            return 200, {"ok": True}
        return 404, {"error": "unknown endpoint"}


# ---------------------------------------------------------------- the wait for a readable strategy

# The owner's choice: the wait ends, and the Study view opens on the still-solving tree,
# once the main solve is within this factor of the target (3% of the pot for a 0.3% target).
PREVIEW_FACTOR = 10


def preview_status(solver, target):
    """The wait's numbers for a solve started with fast preview to `target` chips: the first
    and the latest reported exploitability, and where the Study view opens."""
    with solver.state_lock:
        series = list(solver.series)
    first = series[0]["explo"] if series else None
    explo = series[-1]["explo"] if series else None
    goal = PREVIEW_FACTOR * target
    return {"target": target, "preview_at": goal, "first": first, "explo": explo,
            "early": explo is not None and explo <= goal}
