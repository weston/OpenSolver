#!/usr/bin/env python3
"""Fast preview: hide the solve time (viewer "Fast preview" checkbox, on by default).

Apart from the modal's hand-off point, nothing here uses a tuned threshold; each step stops or
switches when a comparison says so.
1. Build & solve first solves a sketch of the tree (SketchManager: one turn/river size, no
   turn/river raises) to the build's own target; a loading modal shows its progress. Once the
   sketch is within PREVIEW_FACTOR (10, the owner's choice) x the target, the modal closes and
   the Study view opens on the still-solving sketch. When the sketch reaches the target, the main
   solve starts.
2. Each flop node is shown from the sketch until the main tree's strategy there has caught up
   with the sketch's (see SketchManager), and from the main tree after; once the main solve
   meets its target it shows everything.
3. When you look at a node on a later street, the subtree from that street's first decision is
   re-solved here (RefineManager):
   - ranges are the current reaches of whichever tree shows the flop line above it;
   - bet sizes are the build's;
   - the accuracy is the build's target, as a share of that node's pot.
   The main solve is parked for the second or so that takes, so the street in front of you gets
   the whole machine, then it resumes exactly where it was (iterations are deterministic). A
   refine is redone when its ranges are out of date (RefineManager._stale).

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
                from_sketch = self.sketch is not None and self.sketch.serves_line(root)
                src = self.sketch.solver if from_sketch else self.main
                with self.main.state_lock:
                    points = len(self.main.series)
                sk_points = len(self.sketch.solver.series) if from_sketch else 0
                sk_running = from_sketch and self.sketch.state == "running"
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
                with self.mu:
                    if gen == self.gen and not self.abort:
                        self.ready = {"root": root, "sub_root": ctx["subgame_root"], "street": ctx["street"],
                                      "accuracy": acc, "accuracy_pct": pct, "pot": pot,
                                      "seconds": round(time.time() - t0, 2),
                                      "source": "sketch" if from_sketch else "main", "main_points": points,
                                      "sketch_points": sk_points, "sketch_running": sk_running,
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

    def _main_done(self):
        """The main solve met its target: from then on it is the answer, refined or not."""
        with self.main.state_lock:
            return not self.main.solving and not self.main.suspended and \
                "required accuracy" in self.main.solver_status

    def _stale(self, r):
        """A refine is out of date once its ranges are: they came from the sketch and the main
        tree now shows that flop line, or they came from the main solve and it has reported
        progress since (so redone at most once per report, and reports are spaced
        geometrically in iterations)."""
        if r["source"] == "sketch":
            if r.get("sketch_running") and len(self.sketch.solver.series) > r["sketch_points"]:
                return True   # made while the sketch was still solving, and it has reported since
            return not (self.sketch is not None and self.sketch.serves_line(r["root"]))
        with self.main.state_lock:
            return len(self.main.series) > r["main_points"]

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

# The owner's choice: the loading modal ends, and the Study view opens on the still-solving sketch,
# once the sketch is within this factor of the target (3% of the pot for a 0.3% target).
PREVIEW_FACTOR = 10


def _one_size(field):
    """The single later-street size the sketch keeps: the median of the build's sizes (the
    larger middle one of an even count). A field with no numeric size is kept as it is."""
    sizes = []
    for p in str(field or "").split(","):
        try:
            sizes.append(float(p.strip()))
        except ValueError:
            continue
    if not sizes:
        return field
    return f"{sorted(sizes)[len(sizes) // 2]:g}"


def sketch_body(b):
    """The build body with one bet size and no raises or donks on the turn and river. Flop
    actions are untouched, so every flop node has the same id in both trees."""
    s = dict(b)
    for street in ("turn", "river"):
        for sfx in ("", "_ip"):
            k = f"{street}_bet{sfx}"
            if k not in b:
                continue
            s[k] = _one_size(b.get(k))
            s[f"{street}_raise{sfx}"] = ""
        s[f"{street}_donk"] = ""
    return s


def read_node(solver, node_id):
    """(actor, strategy rows, actor's reach) at a decision node, or None for any other node."""
    node = vsrv.parse_node_block(solver.cmd("show_node " + node_id))
    if node["type"] not in ("OOP_DEC", "IP_DEC"):
        return None
    actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
    rows = [r for r in solver.cmd("show_strategy " + node_id).split("\n") if r.strip()]
    if any(len(r.split()) != vsrv.NUM_COMBOS for r in rows):
        return None
    return (actor, [vsrv.parse_floats(r, vsrv.NUM_COMBOS) for r in rows],
            vsrv.parse_floats(solver.cmd(f"show_range {actor} {node_id}"), vsrv.NUM_COMBOS))


def strategy_gap(a, b):
    """How differently two trees play one node: the share of the hand mix (combos weighted by
    a's reach) that would choose a different action, i.e. the reach-weighted total variation
    distance between the two strategies. None when they are not comparable."""
    if a is None or b is None or a[0] != b[0] or len(a[1]) != len(b[1]):
        return None
    _, fa, reach = a
    fb = b[1]
    tot = sum(reach)
    if tot <= 0:
        return None
    acc = 0.0
    for h, r in enumerate(reach):
        if r > 0:
            acc += r * sum(abs(x[h] - y[h]) for x, y in zip(fa, fb))
    return 0.5 * acc / tot


class SketchManager:
    """A small version of the tree (one later-street size, no later-street raises) solved first,
    so the flop can be shown within seconds (fast preview). It has its own solver process.

    How far: to the build's own accuracy target. Measured on a big full-range SRP (541K nodes;
    sketch 133K), the sketch's flop stops getting closer to the full tree's answer at about that
    point: what is left is the gap between the two trees, which more solving cannot close.

    Which tree shows a flop node: the sketch, until the main solve's strategy there is closer
    to the truth. That is judged per node, with no tolerance, from the distance between the two
    trees' strategies at each main progress report (FlopWatch): while the main solve is still
    converging the distance shrinks; once it stops shrinking, what remains is the sketch's own
    error, and the main tree has caught up with the sketch. On the measured tree that switched
    the root at iteration 140 of 198 (ideal: 84) and never switched a node early. Once the main
    solve meets its target, it shows everything.

    Street refines take their ranges from whichever tree shows the flop line above them."""

    def __init__(self, main_solver, solver_cls, exe):
        self.main = main_solver
        self.solver = solver_cls(exe)
        self.mu = threading.Lock()
        self.gen = 0
        self.state = "idle"      # idle | running | ready | error
        self.info = None         # {accuracy, accuracy_pct, explo, seconds, nodes}
        self.error = None
        self.abort = False
        self.target = None       # the sketch's accuracy target in chips while it runs
        self.early = False       # running, and within PREVIEW_FACTOR x the target: browsable
        self.nodes = {}          # flop node id -> {"sketch": read_node(...), "gaps": [...], "main": bool}
        self.avg_delay = None    # the main solver's set_avg_delay: no comparisons up to it

    def invalidate(self):
        with self.mu:
            self.gen += 1
            busy = self.state == "running"
            self.state, self.info, self.error, self.abort = "idle", None, None, busy
            self.nodes, self.target, self.early = {}, None, False
        with self.main.progress_cv:
            self.main.progress_cv.notify_all()   # the watch thread notices the new gen and exits
        if busy:
            try:
                self.solver.proc.kill()
            except Exception:
                pass

    def start(self, body, main_accuracy, start_main):
        """Build and solve the sketch to the main solve's target (main_accuracy, in chips: the
        same pot), then run start_main() (which starts the main solve) and watch the flop."""
        self.invalidate()
        with self.mu:
            gen = self.gen
            self.state, self.abort, self.target, self.early = "running", False, main_accuracy, False

        def job():
            t0 = time.time()
            try:
                if self.solver.dead:
                    self.solver.restart()
                pot = float(body["pot"])
                acc = main_accuracy
                self.solver.reset_series()
                vsrv.op_generate(self.solver, sketch_body(body))
                self.solver.cmd("build_tree")
                self.solver.tree_present = True
                self.solver.cmds([f"set_accuracy {acc:g}", "set_info_freq auto"])
                ev0 = self.solver.events
                self.solver.cmd("go")
                self._wait_sketch(gen, acc, ev0)
                explo = self.solver.series[-1]["explo"] if self.solver.series else None
                nodes = self.solver.cmd("node_count").split("\n")[0]
                with self.mu:
                    if gen == self.gen and not self.abort:
                        self.state = "ready"
                        self.info = {"accuracy": acc, "accuracy_pct": 100.0 * acc / pot if pot else None,
                                     "explo": explo, "seconds": round(time.time() - t0, 2), "nodes": nodes}
                if self.state == "ready":
                    self.track("r:0")    # the root and the first decision after each root action
                    for k in vsrv.parse_children(self.solver.cmd("show_children r:0")):
                        self.track(k["id"])
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
                ready = self.state == "ready"
            if go:   # also after a sketch error: the main solve is what was asked for
                start_main()
                if ready:
                    self._watch(gen)

        threading.Thread(target=job, daemon=True).start()

    def _wait_sketch(self, gen, acc, ev0):
        """Wait for the sketch solve to stop, without holding the solver's command lock (so the
        flop can be browsed meanwhile), and mark it browsable once it is within PREVIEW_FACTOR x
        the target."""
        s = self.solver
        deadline = time.time() + vresolve.SOLVE_TIMEOUT
        seen = -1
        with s.progress_cv:
            while True:
                with self.mu:
                    if gen != self.gen or self.abort:
                        return
                if s.dead:
                    raise vsrv.SolverError("sketch solver died")
                if s.events != seen:
                    seen = s.events
                    e = s.series[-1]["explo"] if s.series else None
                    if e is not None and e <= PREVIEW_FACTOR * acc:
                        with self.mu:
                            if gen == self.gen:
                                self.early = True
                    if s.events > ev0 and not s.solving and s.solver_status.startswith("stopped"):
                        return
                left = deadline - time.time()
                if left <= 0:
                    raise vsrv.SolverError("sketch solve timed out")
                s.progress_cv.wait(min(left, 1.0))

    # ---- the flop watch: which tree shows each flop node ----

    def track(self, node_id):
        """Start comparing a flop decision node (idempotent). Its sketch strategy is fixed, so
        it is read once. If the main solve is running, its strategy is read right away (a
        browse request has the main solve parked anyway), so the next report can compare."""
        with self.mu:
            if self.state != "ready" or node_id in self.nodes:
                return
            gen = self.gen
            self.nodes[node_id] = None   # claimed: concurrent calls return
        try:
            sk = read_node(self.solver, node_id)
        except vsrv.SolverError:
            sk = None
        with self.mu:
            if gen != self.gen:
                return
            if sk is None:               # not a decision node: the main tree always shows it
                self.nodes[node_id] = {"sketch": None, "gaps": [], "main": True}
                return
            self.nodes[node_id] = {"sketch": sk, "gaps": [], "main": False}
        if self.main.solving and not self._main_done():
            self._checkpoint(gen, only=[node_id])

    def _watch(self, gen):
        """Runs after the main solve starts: one checkpoint per progress report, until the
        tree changes (a Stop and Resume keeps it going)."""
        seen = 0
        while True:
            with self.main.progress_cv:
                while True:
                    with self.mu:
                        if gen != self.gen:
                            return
                    n = len(self.main.series)
                    if n > seen:
                        break
                    self.main.progress_cv.wait()
            seen = n
            self._checkpoint(gen)

    def _checkpoint(self, gen, only=None):
        with self.mu:
            if gen != self.gen:
                return
            ids = [k for k, v in self.nodes.items() if v and not v["main"] and (only is None or k in only)]
        main = self.main
        held = False
        try:
            if main.solving:
                main.hold("watch")
                held = True
            if self.avg_delay is None:
                self.avg_delay = int(main.cmd("show_avg_delay"))
            iters = int(main.cmd("show_iters"))
            with main.state_lock:
                t = main.series[-1]["t"] if main.series else None
            reads = {}
            if iters > self.avg_delay:   # (show_iters counts the whole solve, Resume included)   # the average restarts at avg_delay + 1: compare after it
                for k in ids:
                    reads[k] = read_node(main, k)
        except (vsrv.SolverError, ValueError):
            return
        finally:
            if held:
                main.unhold("watch")
        if only is None and t is not None:
            prev = getattr(self, "_last_cp", None)
            if prev and prev[0] == gen and iters > prev[2] and t > prev[1]:
                main.iter_seconds = (t - prev[1]) / (iters - prev[2])
            self._last_cp = (gen, t, iters)
        with self.mu:
            if gen != self.gen:
                return
            for k, m in reads.items():
                v = self.nodes.get(k)
                if not v or v["main"]:
                    continue
                g = strategy_gap(m, v["sketch"])
                if g is None:
                    v["main"] = True
                    continue
                if v["gaps"] and iters <= v["gaps"][-1][0]:
                    continue             # no iteration since the last look: nothing to compare
                if v["gaps"] and g >= v["gaps"][-1][1]:
                    v["main"] = True     # stopped shrinking: the main solve has caught up here
                v["gaps"].append((iters, round(g, 5)))

    def _main_done(self):
        with self.main.state_lock:
            return not self.main.solving and "required accuracy" in self.main.solver_status

    def serves(self, node_id):
        """Whether the sketch shows this flop node now (tracking it if new)."""
        if street_root(node_id)[0] is not None or self._main_done():
            return False
        with self.mu:
            if self.state == "running":
                return self.early     # still solving: shown live, compared once it is done
            if self.state != "ready":
                return False
        self.track(node_id)
        with self.mu:
            v = self.nodes.get(node_id)
            return bool(v) and not v["main"]

    def serves_line(self, street_root_id):
        """Whether a later street's ranges come from the sketch: some flop decision on the line
        to it is still shown from the sketch."""
        toks = street_root_id.split(":")
        line = [":".join(toks[:i]) for i in range(2, len(toks))]
        return any(self.serves(k) for k in line)

    def overlay(self, path, query):
        if path not in OVERLAY or "id" not in query:
            return None
        node_id = query["id"][0]
        if not self.serves(node_id):
            return None
        self.solver.lease()    # a still-running sketch: park it for the burst of requests
        try:
            status, data = vsrv._api_main(self.solver, None, "GET", path, query, None)
        except vsrv.SolverError:
            return None
        if status != 200 or not isinstance(data, dict):
            return None
        with self.mu:
            info = dict(self.info) if self.info else {}
            info["solving"] = self.state == "running"
        data["sketch"] = info
        return status, data

    def status(self):
        with self.mu:
            st = {"state": self.state, "info": dict(self.info) if self.info else None, "error": self.error,
                  "early": self.early, "events": self.solver.events,
                  "flop_sketch": sum(1 for v in self.nodes.values() if v and not v["main"]),
                  "flop_main": sum(1 for v in self.nodes.values() if v and v["main"])}
            target = self.target
        if st["state"] == "running":
            with self.solver.state_lock:
                last = self.solver.series[-1]["explo"] if self.solver.series else None
            first = self.solver.series[0]["explo"] if self.solver.series else None
            st["progress"] = {"explo": last, "first": first, "target": target,
                              "preview_at": None if target is None else PREVIEW_FACTOR * target}
        return st
