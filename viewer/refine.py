#!/usr/bin/env python3
"""Fast preview: refine the street you are looking at while the main solve runs (experiment).

The viewer's "fast preview" mode hides solve time. The Study view opens as soon as the flop
strategy has settled (the client watches the root's range-weighted frequencies), and the main
solve keeps going in the background toward its target. When you look at a node on a later
street, the subtree from that street's first decision is re-solved here:
- ranges are the main tree's current reaches;
- bet sizes are the build's;
- the accuracy is the build's target, as a share of that node's pot.
The main solve is paused for the few seconds that takes, so the street in front of you gets
the whole machine, then it resumes exactly where it was. Stopping and resuming changes no
result: iterations are deterministic.

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
STALE = 0.5  # re-refine a street once the main solve's exploitability has fallen below this x the refine's
OVERLAY = ("/api/strategy", "/api/range", "/api/ev", "/api/node", "/api/categories")


def street_root(node_id):
    """(root, rest): root is the id through the FIRST dealt card (the first decision of the
    street after the tree's root street), rest the tokens below it. (None, None) on the
    root street."""
    toks = node_id.split(":")
    for i, t in enumerate(toks):
        if CARD.match(t):
            return ":".join(toks[:i + 1]), toks[i + 1:]
    return None, None


def _rewrite_ids(obj, src, dst):
    """Replace the id prefix src by dst in every "id" field (dicts and lists, recursively)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "id" and isinstance(v, str) and (v == src or v.startswith(src + ":")):
                obj[k] = dst + v[len(src):]
            else:
                _rewrite_ids(v, src, dst)
    elif isinstance(obj, list):
        for v in obj:
            _rewrite_ids(v, src, dst)
    return obj


class RefineManager(vresolve.ResolveManager):
    """A third solver process for street refines, plus the main solve's pause/resume."""

    def __init__(self, main_solver, solver_cls, exe, sketch=None):
        super().__init__(main_solver, solver_cls, exe)
        self.sketch = sketch    # SketchManager: the range source while the main solve is young
        self.gen = 0            # bumped when the main tree changes: older results are void
        self.root = None        # main-tree id of the street root being refined / refined
        self.ready = None       # finished refine: {root, sub_root, accuracy, seconds, explo_main, ...}
        self.pending = None     # (root, body, pct) queued behind a cancelled job
        self.paused_main = False

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

    def _start(self, root, body, pct):
        with self.mu:
            gen = self.gen
            self.busy, self.kind, self.node, self.root = True, "refine", root, root
            self.error, self.abort = None, False

        def job():
            tp = time.time()
            paused = self._pause_main()
            try:
                t0 = time.time()
                phases = {"pause": round(t0 - tp, 2)}
                src = self.sketch.solver if self.sketch is not None and self.sketch.active() else self.main
                ctx = vresolve.read_context(src, root, with_ranges=True)
                phases["context"] = round(time.time() - t0, 2)
                pot = ctx["pot"]["oop"] + ctx["pot"]["ip"] + ctx["pot"]["dead"]
                acc = max(pct, 0.01) / 100.0 * pot
                with self.mu:
                    self.ctx = {k: v for k, v in ctx.items() if not k.startswith("range_")}
                t1 = time.time()
                lines = self._build_and_solve(ctx, body, acc)
                phases["build+solve"] = round(time.time() - t1, 2)
                t1 = time.time()
                self._finish(ctx, acc, lines)
                phases["finish"] = round(time.time() - t1, 2)
                explo = self._main_explo()
                if src is not self.main:   # ranges from the sketch: stale once the main beats it
                    explo = (self.sketch.info or {}).get("explo")
                with self.mu:
                    if gen == self.gen and not self.abort:
                        self.ready = {"root": root, "sub_root": ctx["subgame_root"], "street": ctx["street"],
                                      "accuracy": acc, "accuracy_pct": pct, "pot": pot,
                                      "seconds": round(time.time() - t0, 2), "explo_main": explo,
                                      "main_paused": paused, "phases": phases}
            finally:
                if paused:
                    self._resume_main()

        threading.Thread(target=self._entry, args=(job,), daemon=True).start()

    def _entry(self, job):
        super()._entry(job)
        with self.mu:
            nxt, self.pending = self.pending, None
        if nxt:
            self._start(*nxt)

    def view(self, node_id, body):
        root, _ = street_root(node_id)
        if root is None or self._main_done():
            return {"state": "none"}
        try:
            pct = float(body.get("accuracy_pct", 0.3))
        except (TypeError, ValueError):
            raise vsrv.SolverError("bad accuracy_pct")
        with self.mu:
            if self.ready and self.ready["root"] == root and not self._stale(self.ready):
                return {"state": "ready", "root": root, "refine": dict(self.ready)}
            if self.busy and self.root == root:
                return {"state": "running", "root": root}
            busy = self.busy
            if busy:
                self.pending = (root, body, pct)
        if busy:
            self.stop()           # the job thread starts `pending` when it unwinds
            return {"state": "running", "root": root}
        # Only a decision node can root a subgame (an all-in runout has none).
        node = vsrv.parse_node_block(self.main.cmd("show_node " + root))
        if node["type"] not in ("OOP_DEC", "IP_DEC"):
            return {"state": "none"}
        self._start(root, body, pct)
        return {"state": "running", "root": root}

    def _main_explo(self):
        with self.main.state_lock:
            return self.main.series[-1].get("explo") if self.main.series else None

    def _main_done(self):
        """The main solve met its target: from then on it is the answer, refined or not."""
        with self.main.state_lock:
            return not self.main.solving and not self.main.suspended and \
                "required accuracy" in self.main.solver_status

    def _stale(self, r):
        e = self._main_explo()
        return r.get("explo_main") is not None and e is not None and e < STALE * r["explo_main"]

    def translate(self, node_id):
        """Refine-solver id for a main-tree id inside the refined subtree, else None."""
        if self._main_done():
            return None, None
        with self.mu:
            r = self.ready
            if not r or self.busy or not self.tree_ok:
                return None, None
        root, rest = street_root(node_id)
        if root != r["root"]:
            return None, None
        return r["sub_root"] + ("" if not rest else ":" + ":".join(rest)), r

    def overlay(self, path, query):
        """Serve a browse request from the refined subtree, or None to use the main tree."""
        if path not in OVERLAY or "id" not in query:
            return None
        sub, info = self.translate(query["id"][0])
        if sub is None:
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
        data["refined"] = {k: info[k] for k in ("root", "street", "accuracy", "accuracy_pct", "seconds")}
        return status, data

    def refine_status(self):
        with self.mu:
            return {"busy": self.busy, "root": self.root, "error": self.error, "gen": self.gen,
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


# ---------------------------------------------------------------- the flop sketch

SKETCH_PCT = 1.0   # the sketch tree is solved to this % of the starting pot


def _one_size(field):
    """The single later-street size the sketch keeps: the pot-% size nearest 75, else 75."""
    best = None
    for p in str(field or "").split(","):
        p = p.strip()
        try:
            v = float(p)
        except ValueError:
            continue
        if best is None or abs(v - 75) < abs(best - 75):
            best = v
    return "75" if best is None else f"{best:g}"


def sketch_body(b):
    """The build body with one bet size and no raises or donks on the turn and river. Flop
    actions are untouched, so every flop node has the same id in both trees."""
    s = dict(b)
    for street in ("turn", "river"):
        for sfx in ("", "_ip"):
            k = f"{street}_bet{sfx}"
            if sfx and k not in b:
                continue
            s[k] = _one_size(b.get(k))
            s[f"{street}_raise{sfx}"] = ""
        s[f"{street}_donk"] = ""
    return s


class SketchManager:
    """A small version of the tree, solved first so the flop can be shown within seconds
    (fast preview). Its own solver process; flop nodes are served from it until the main
    solve's exploitability beats the sketch's, and street refines take their ranges from it
    meanwhile."""

    def __init__(self, main_solver, solver_cls, exe):
        self.main = main_solver
        self.solver = solver_cls(exe)
        self.mu = threading.Lock()
        self.gen = 0
        self.state = "idle"      # idle | running | ready | error
        self.info = None         # {accuracy, accuracy_pct, explo, seconds, nodes}
        self.error = None
        self.abort = False

    def invalidate(self):
        with self.mu:
            self.gen += 1
            busy = self.state == "running"
            self.state, self.info, self.error, self.abort = "idle", None, None, busy
        if busy:
            try:
                self.solver.proc.kill()
            except Exception:
                pass

    def start(self, body, main_accuracy, start_main):
        """Build and solve the sketch, then run start_main() (which starts the main solve)."""
        self.invalidate()
        with self.mu:
            gen = self.gen
            self.state, self.abort = "running", False

        def job():
            t0 = time.time()
            ok = False
            try:
                if self.solver.dead:
                    self.solver.restart()
                pot = float(body["pot"])
                acc = SKETCH_PCT / 100.0 * pot
                vsrv.op_generate(self.solver, sketch_body(body))
                self.solver.cmd("build_tree")
                self.solver.tree_present = True
                self.solver.cmds([f"set_accuracy {acc:g}", "set_info_freq auto", "go"])
                self.solver.cmd("wait_for_solver", timeout=vresolve.SOLVE_TIMEOUT)
                explo = self.solver.series[-1]["explo"] if self.solver.series else None
                nodes = self.solver.cmd("node_count").split("\n")[0]
                with self.mu:
                    if gen == self.gen and not self.abort:
                        self.state = "ready"
                        self.info = {"accuracy": acc, "accuracy_pct": SKETCH_PCT, "explo": explo,
                                     "seconds": round(time.time() - t0, 2), "nodes": nodes}
                        ok = True
            except Exception as e:
                with self.mu:
                    if gen == self.gen and not self.abort:
                        self.state, self.error = "error", str(e)
            finally:
                if self.abort or self.solver.dead:
                    try:
                        self.solver.restart()
                    except Exception:
                        pass
            with self.mu:
                go = gen == self.gen and not self.abort
            if go:   # also after a sketch error: the main solve is what was asked for
                start_main()
            return ok

        threading.Thread(target=job, daemon=True).start()

    def active(self):
        """Serve flop nodes from the sketch: it is ready and the main solve has not beaten it."""
        with self.mu:
            if self.state != "ready" or not self.info:
                return False
            target = self.info["explo"] if self.info["explo"] is not None else self.info["accuracy"]
        with self.main.state_lock:
            e = self.main.series[-1].get("explo") if self.main.series else None
        return e is None or e > target

    def overlay(self, path, query):
        if path not in OVERLAY or "id" not in query:
            return None
        node_id = query["id"][0]
        if street_root(node_id)[0] is not None or not self.active():
            return None
        try:
            status, data = vsrv._api_main(self.solver, None, "GET", path, query, None)
        except vsrv.SolverError:
            return None
        if status != 200 or not isinstance(data, dict):
            return None
        with self.mu:
            info = dict(self.info) if self.info else {}
        data["sketch"] = info
        return status, data

    def status(self):
        with self.mu:
            return {"state": self.state, "info": dict(self.info) if self.info else None, "error": self.error,
                    "active": None}
