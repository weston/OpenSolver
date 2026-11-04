#!/usr/bin/env python3
"""WSolver viewer server: drives ./solver over UPI and serves the browser UI on localhost.

Python 3 standard library only. The solver is spawned once and driven one command at a
time through a lock, the way tests/test_upi.py's Upi class does; asynchronous "SOLVER:"
blocks are demultiplexed for progress display. The HTTP API is a fixed set of endpoints
(whitelist by construction): there is deliberately no "run any UPI command" endpoint, so
nothing here can write arbitrary files (except `dump_tree` to a user-given path) or run
destructive commands (no stdoutredi).

    python3 viewer/server.py [--solver PATH] [--port 8765] [--library DIR]

`--library` sets the root for saved ranges and tree configs (viewer/library.py,
issue #116); it defaults to ~/.wsolver/library.

Then open http://127.0.0.1:8765 in a browser.
"""

import argparse
import importlib.util
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from library import Library, LibraryError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")
END = "END"
NUM_COMBOS = 1326

RANKS = "AKQJT98765432"
SUITS = "cdhs"


def load_tree_builder():
    """Import tools/tree_builder.py from the repo (no package layout to rely on)."""
    spec = importlib.util.spec_from_file_location("tree_builder", os.path.join(REPO, "tools", "tree_builder.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("tree_builder", mod)  # dataclasses look modules up here
    spec.loader.exec_module(mod)
    return mod


class SolverError(Exception):
    """Bad solver response, timeout, or dead solver."""


class Solver:
    """One UPI session with ./solver: a lock for commands, a pump thread for SOLVER blocks."""

    def __init__(self, exe):
        self.exe = exe
        self.cmd_lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.progress_cv = threading.Condition(self.state_lock)  # notified on every SOLVER event
        self.solving = False
        self.progress = []       # last periodic SOLVER: block (list of lines)
        self.solver_status = ""  # "started" / "stopped (<reason>)"
        self.events = 0          # bumped on every SOLVER event, for cheap polling
        self.tree_present = False
        self.dead = False
        self.version = ""
        self.generation = 0
        self.series = []         # convergence points of the current tree: {t, explo, ev_oop, ev_ip}
        self.partial = None      # live/last subtree solve (#153): {node, pot, accuracy, done, ...}
        self.loaded_file = False  # the tree came from load_tree (view-only: no solving further)
        self.last_generate = None  # the /api/generate body of the tree in memory (fast preview)
        self.holds = set()       # who keeps the solve parked (UPI hold): "browse" lease, "refine" (refine.py)
        self._lease_until = 0.0
        self._lease_thread = None
        self.iter_seconds = None  # measured solver time per iteration of the running solve (refine.py)
        self._hold_wait = None    # how long the last hold took to land (a stand-in until then)
        self.locks = {}          # node id -> "node" | "combos": locks set from the viewer (#200)
        self.undo = None         # {"path", "time"}: lossless save of the tree before its first lock
        self.start()

    def start(self):
        """Spawn the solver and its pump. Caller must hold cmd_lock (or be the only thread)."""
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.generation += 1
        self.q = queue.Queue()
        threading.Thread(target=self._pump, args=(self.generation, self.proc.stdout, self.q),
                         daemon=True).start()
        self.send_raw("set_end_string " + END)
        self._read_response()  # banner lines, skipped until the first END
        self.version = self.cmd("show_version")

    def _pump(self, generation, stream, q):
        """Demux solver stdout: SOLVER: blocks go to state, everything else to the response queue."""
        try:
            for line in stream:
                line = line.rstrip("\n")
                if line == "SOLVER:":
                    block = []
                    for l2 in stream:
                        l2 = l2.rstrip("\n")
                        if l2 == END:
                            break
                        block.append(l2)
                    point = parse_progress_point(block)
                    with self.state_lock:
                        # A "partial: <node>" first line marks a subtree (solve further) solve:
                        # its numbers are relative to the node's pot, so they must not enter the
                        # main tree's convergence series (#153).
                        if block and block[0].startswith("partial: ") and self.partial:
                            self.partial["progress"] = block[1:]
                            self.partial["point"] = point
                        else:
                            self.progress = block
                            if point is not None:
                                self.series.append(point)
                        self.events += 1
                        self.progress_cv.notify_all()
                elif line.startswith("SOLVER:"):
                    with self.state_lock:
                        msg = line[len("SOLVER: "):]
                        self.solver_status = msg
                        if msg.startswith("started"):
                            self.solving = True
                            self.progress = []
                        elif msg.startswith("stopped"):
                            self.solving = False
                            if self.partial:
                                self.partial["done"] = True
                                self.partial["stopped"] = msg[len("stopped ("):-1] or "requested"
                        self.events += 1
                        self.progress_cv.notify_all()
                else:
                    q.put(line)
        finally:
            q.put(None)
            with self.state_lock:
                if generation == self.generation:  # a stale pump must not mark a fresh solver dead
                    self.dead = True
                    self.solving = False

    def send_raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read_response(self, timeout=600):
        out = []
        while True:
            try:
                line = self.q.get(timeout=timeout)
            except queue.Empty:
                raise SolverError("timeout waiting for solver response")
            if line is None:
                raise SolverError("solver exited")
            if line == END:
                return "\n".join(out)
            out.append(line)

    def cmd(self, line, timeout=600):
        """Run one UPI command. Raises SolverError on an ERROR: response."""
        with self.cmd_lock:
            self.send_raw(line)
            resp = self._read_response(timeout)
        if resp.startswith("ERROR"):
            raise SolverError(resp.split("\n", 1)[0])
        return resp

    def cmds(self, lines, timeout=600):
        """Several commands in one lock hold; stops at the first ERROR."""
        with self.cmd_lock:
            for line in lines:
                self.send_raw(line)
                resp = self._read_response(timeout)
                if resp.startswith("ERROR"):
                    raise SolverError(f"{line.strip()[:120]} -> {resp.splitlines()[0]}")

    def state(self):
        with self.state_lock:
            partial = dict(self.partial) if self.partial else None
            if partial:
                partial["progress"] = list(partial["progress"])
            return {"solving": self.solving, "suspended": "refine" in self.holds,
                    "dead": self.dead, "progress": list(self.progress),
                    "solver_status": self.solver_status, "events": self.events,
                    "tree": self.tree_present, "version": self.version,
                    "partial": partial, "loaded_file": self.loaded_file}

    # ---- parking a running solve (UPI hold / release, an extension) ----

    @property
    def suspended(self):
        return "refine" in self.holds

    def hold(self, tag):
        """Park the running solve between iterations until every tag is released. Results are
        unaffected: a parked solve resumes exactly where it was."""
        with self.cmd_lock:
            if not self.holds:
                t = time.time()
                self.cmd("hold")
                self._hold_wait = time.time() - t
            self.holds.add(tag)

    def unhold(self, tag):
        with self.cmd_lock:
            if tag not in self.holds:
                return
            self.holds.discard(tag)
            if not self.holds:
                try:
                    self.cmd("release")
                except SolverError:
                    pass

    def drop_holds(self):
        """After stop / go / build: the solver has dropped its hold itself."""
        with self.cmd_lock:
            self.holds.clear()

    def lease(self):
        """Browsing during a solve: each UPI command would wait for an iteration boundary of its
        own (seconds per click on a big tree), so park the solve for the burst of requests a
        click makes and let it run again once no request has come for one iteration's time.
        That is the break-even: parked longer, the solve loses more than a request would have
        waited; shorter, the next request of the burst waits an iteration boundary again. The
        iteration time is measured (refine.py's flop watch sets it at each progress report);
        before the first report, the wait for the last hold to land (the cost a request just
        paid) stands in."""
        if not self.solving:
            return
        self.hold("browse")
        seconds = self.iter_seconds if self.iter_seconds is not None else (self._hold_wait or 0.0)
        with self.state_lock:
            self._lease_until = time.time() + seconds
            if self._lease_thread and self._lease_thread.is_alive():
                return
            self._lease_thread = threading.Thread(target=self._lease_watch, daemon=True)
            self._lease_thread.start()

    def _lease_watch(self):
        while True:
            with self.state_lock:
                left = self._lease_until - time.time()
            if left <= 0:
                self.unhold("browse")
                return
            time.sleep(left)

    def reset_series(self):
        with self.state_lock:
            self.series = []
            self.iter_seconds = None

    def start_partial(self, node_id, accuracy, pot):
        """Mark the subtree solve in flight, so the pump routes its blocks here (#153)."""
        with self.state_lock:
            self.partial = {"node": node_id, "accuracy": accuracy, "pot": pot,
                            "done": False, "stopped": None, "progress": [], "point": None}

    def clear_partial(self):
        with self.state_lock:
            self.partial = None

    def restart(self):
        with self.cmd_lock:
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
                for f in (self.proc.stdin, self.proc.stdout):
                    f.close()
            except Exception:
                pass
            self.start()  # cmd_lock is an RLock, cmd() inside start() is fine
            self.tree_present = False
            self.reset_series()
            with self.state_lock:
                self.dead = False
                self.solving = False
                self.progress = []
                self.solver_status = ""
                self.partial = None
                self.loaded_file = False
            self.holds.clear()
            self.last_generate = None
            self.forget_locks()

    def forget_locks(self):
        """A new tree (generate / build / load / restart): drop the lock list and the undo copy."""
        self.locks = {}
        if self.undo:
            try:
                os.remove(self.undo["path"])
            except OSError:
                pass
        self.undo = None


# ---------------------------------------------------------------- parsing UPI outputs

def parse_num(tok):
    """A UPI float; non-finite values become None (JSON has no nan/inf)."""
    v = float(tok)
    if not math.isfinite(v):
        return None
    return float(f"{v:.6g}")


def parse_progress_point(block):
    """One periodic SOLVER: block -> a convergence point, or None if it lacks the fields (#115)."""
    vals = {}
    for line in block:
        key, _, val = line.partition(":")
        try:
            vals[key.strip()] = float(val)
        except ValueError:
            continue
    if "running time" not in vals or "Exploitable for" not in vals:
        return None
    return {"t": vals["running time"], "explo": vals["Exploitable for"],
            "ev_oop": vals.get("EV OOP"), "ev_ip": vals.get("EV IP")}


def parse_floats(line, expect=None):
    vals = [parse_num(t) for t in line.split()]
    if expect is not None and len(vals) != expect:
        raise SolverError(f"expected {expect} floats, got {len(vals)}")
    return vals


def parse_node_block(text):
    lines = [l for l in text.split("\n") if l.strip()]
    d = {"id": lines[0], "type": lines[1] if len(lines) > 1 else "?", "board": "", "pot": [],
         "nchildren": 0, "flags": ""}
    if len(lines) > 2:
        b = lines[2].strip()
        d["board"] = "" if b == "---" else b
    if len(lines) > 3:
        d["pot"] = [parse_num(t) for t in lines[3].split()]
    for l in lines[4:]:
        if l.endswith(" children"):
            try:
                d["nchildren"] = int(l.split()[0])
            except ValueError:
                pass
        elif l.startswith("flags:"):
            d["flags"] = l[len("flags:"):].strip()
    return d


def parse_children(text):
    """show_children output -> list of {index, node-block-dict}."""
    kids = []
    cur = None
    for line in text.split("\n"):
        if line.startswith("child ") and line.rstrip().endswith(":"):
            cur = []
            kids.append(cur)
        elif cur is not None:
            cur.append(line)
    out = []
    for block_lines in kids:
        d = parse_node_block("\n".join(block_lines))
        out.append(d)
    return out


def card_token(s):
    return len(s) == 2 and s[0] in RANKS and s[1] in SUITS


NODE_ID_RE = re.compile(r"[rR][:0-9A-Za-z?]*")


def combo_class(combo):
    """"AhKd" -> "AKo" (the matrix class names used by the frontend)."""
    a, b = combo[0], combo[2]
    if a == b:
        return a + b
    hi, lo = (a, b) if RANKS.index(a) < RANKS.index(b) else (b, a)
    return hi + lo + ("s" if combo[1] == combo[3] else "o")


def class_sort_key(cls):
    """Matrix (row-major) order: pairs on the diagonal, suited above, offsuit below."""
    hi, lo = cls[0], cls[1]
    if len(cls) == 2:
        k = RANKS.index(hi)
        return (k, k)
    k = (RANKS.index(hi), RANKS.index(lo))
    return k if cls[2] == "s" else (k[1], k[0])


def node_id_arg(query, name="id", default="r:0"):
    """A node id becomes part of a UPI command line; anything outside the Pio id alphabet
    (digits, letters, ':', '?') could inject solver commands, so reject it."""
    v = query.get(name, [default])[0]
    if not NODE_ID_RE.fullmatch(v):
        raise SolverError("bad node id")
    return v


# ---------------------------------------------------------------- the API

_resolver = None  # resolve.ResolveManager, set by main()/tests; owns the 2nd solver
_refiner = None   # refine.RefineManager (fast preview), set by main()/tests; owns the 3rd
_sketch = None    # refine.SketchManager (fast preview's flop sketch); owns the 4th


def set_resolver(resolver):
    global _resolver
    _resolver = resolver


def set_refiner(refiner, sketch=None):
    global _refiner, _sketch
    _refiner = refiner
    _sketch = sketch


def lib_query(query, name="name"):
    """Query-arg wrapper for library endpoints; LibraryError -> 400 via the handler."""
    return query.get(name, [""])[0]


def api(solver, lib, method, path, query, body):
    """Dispatch one API call; returns (status, data)."""
    if path == "/api/resolve" or path.startswith("/api/resolve/"):
        if _resolver is None:
            raise SolverError("re-solve support not initialized")
        return _resolver.api(method, path, query, body)
    if path == "/api/refine" or path.startswith("/api/refine/"):
        if _refiner is None:
            raise SolverError("fast preview not initialized")
        return _refiner.api(method, path, query, body)
    if method == "GET" and path in BROWSE_PATHS:
        solver.lease()
    if _refiner is not None:
        if method == "GET":
            served = _refiner.overlay(path, query)
            if served is None and _sketch is not None:
                served = _sketch.overlay(path, query)
            if served is not None:
                return served
        elif path in ("/api/build", "/api/load", "/api/restart", "/api/generate"):
            _refiner.invalidate()  # the main tree is new: old refines and the sketch don't apply
            if _sketch is not None:
                _sketch.invalidate()
        elif path == "/api/stop" and _sketch is not None and _sketch.state == "running":
            _sketch.invalidate()   # cancels the sketch, and with it the main solve it would start
        elif path in LOCK_PATHS:
            # A lock, unlock or revert changes the main tree's strategies: a refined street was
            # solved against the old ones. A running sketch would start the main solve under the
            # lock, so it counts as solving.
            if _sketch is not None and _sketch.state == "running":
                raise SolverError("the solver is running (stop it first)")
            _refiner.invalidate()
            if _sketch is not None:
                _sketch.invalidate()
        if method == "POST" and path == "/api/solve" and _sketch is not None:
            b = need_body(body)
            # Only a fresh tree gets a sketch: Resume on a stopped solve just continues it.
            fresh = not solver.series and not solver.solving
            if b.get("preview") and fresh and solver.last_generate and solver.tree_present \
                    and not solver.loaded_file:
                acc = b.get("accuracy", 0.05)
                if isinstance(acc, bool) or not isinstance(acc, (int, float)) or not math.isfinite(acc) or acc < 0:
                    raise SolverError("bad accuracy")
            else:
                acc = 0
            if acc > 0:   # the sketch solves to the main target: with none (0), there is no sketch
                _refiner.invalidate()
                solver.clear_partial()

                def start_main():
                    try:
                        solver.cmds([f"set_accuracy {acc:g}", "set_info_freq auto", "go"])
                    except SolverError:
                        pass
                _sketch.start(solver.last_generate, acc, start_main)
                return 200, {"ok": True, "sketch": True}
    if path == "/api/progress" and _sketch is not None:
        status, data = _api_main(solver, lib, method, path, query, body)
        data["sketch"] = _sketch.status()
        return status, data
    if method == "POST" and path in ("/api/stop", "/api/solve", "/api/solve_node", "/api/build", "/api/load",
                                    "/api/lock/revert"):
        # these stop or restart the solve, and the solver drops any hold with it
        try:
            return _api_main(solver, lib, method, path, query, body)
        finally:
            solver.drop_holds()
    return _api_main(solver, lib, method, path, query, body)


LOCK_PATHS = ("/api/lock", "/api/unlock", "/api/lock/revert", "/api/lock/apply")
BROWSE_PATHS = ("/api/node", "/api/strategy", "/api/path", "/api/range", "/api/categories", "/api/ev",
                "/api/calc_results", "/api/hand_order")


def _api_main(solver, lib, method, path, query, body):
    if path == "/api/status":
        return 200, solver.state()
    if path == "/api/hand_order":
        return 200, {"order": solver.cmd("show_hand_order").split()}

    # ---- saved ranges and tree configs (#116); state-free, no solver needed ----
    if path == "/api/library/list":
        return 200, {"entries": lib.list(lib_query(query, "kind"))}
    if path == "/api/library/get":
        return 200, {"kind": lib_query(query, "kind"), "name": lib_query(query),
                     "content": lib.get(lib_query(query, "kind"), lib_query(query))}

    if path == "/api/node":
        node_id = node_id_arg(query)
        return 200, {"node": parse_node_block(solver.cmd("show_node " + node_id)),
                     "children": parse_children(solver.cmd("show_children " + node_id))}

    if path == "/api/strategy":
        node_id = node_id_arg(query)
        node = parse_node_block(solver.cmd("show_node " + node_id))
        if node["type"] not in ("OOP_DEC", "IP_DEC"):
            return 200, {"node": node, "children": parse_children(solver.cmd("show_children " + node_id))}
        actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
        kids = parse_children(solver.cmd("show_children " + node_id))
        raw = solver.cmd("show_strategy " + node_id)
        rows = [l for l in raw.split("\n") if l.strip()]
        if any(len(r.split()) != NUM_COMBOS for r in rows):
            # Not a postflop 1326 layout (e.g. a preflop node): hand the raw text to the UI.
            return 200, {"node": node, "children": kids, "actor": actor, "raw": raw}
        return 200, {"node": node, "children": kids, "actor": actor,
                     "freq": [parse_floats(r, NUM_COMBOS) for r in rows],
                     "reach": parse_floats(solver.cmd(f"show_range {actor} " + node_id), NUM_COMBOS),
                     "ev": ev_pair(solver, actor, node_id)}

    if path == "/api/lock_state":
        return 200, lock_state(solver, node_id_arg(query))
    if path == "/api/lock/export":
        return 200, op_lock_export(solver)

    if path == "/api/path":
        # Every node from the root to `id`, in one call, so the UI can draw the action line (#112).
        node_id = node_id_arg(query)
        toks = node_id.split(":")
        ids = [":".join(toks[:i]) for i in range(2, len(toks) + 1)] if len(toks) >= 2 else [node_id]
        with solver.cmd_lock:
            nodes = [parse_node_block(solver.cmd("show_node " + i)) for i in ids]
            stack = parse_num(solver.cmd("show_effective_stack").split()[0])
        return 200, {"nodes": nodes, "eff_stack": stack}

    if path == "/api/convergence":
        with solver.state_lock:
            return 200, {"points": list(solver.series), "solving": solver.solving}

    if path == "/api/range":
        node_id = node_id_arg(query)
        return 200, {"oop": parse_floats(solver.cmd("show_range OOP " + node_id), NUM_COMBOS),
                     "ip": parse_floats(solver.cmd("show_range IP " + node_id), NUM_COMBOS)}

    if path == "/api/categories":
        node_id = node_id_arg(query)
        player = query.get("player", ["actor"])[0].upper()
        if player not in ("OOP", "IP", "ACTOR"):
            raise SolverError("player must be OOP, IP or actor")
        return 200, categories_at(solver, node_id, player)

    if path == "/api/ev":
        node_id = node_id_arg(query)
        player = query.get("player", ["OOP"])[0].upper()
        if player not in ("OOP", "IP"):
            raise SolverError("player must be OOP or IP")
        return 200, ev_pair(solver, player, node_id)

    if path == "/api/progress":
        st = solver.state()
        if not st["dead"]:
            try:
                st["iters"] = int(solver.cmd("show_iters"))
            except (SolverError, ValueError):
                st["iters"] = None
        return 200, st

    if path == "/api/estimate":
        return 200, {"text": solver.cmd("estimate_tree")}

    if path == "/api/calc_results":
        return 200, {"text": solver.cmd("calc_results")}

    # ---- state-changing endpoints (POST only) ----
    require_post(method)

    if path == "/api/library/save":
        b = need_body(body)
        name = lib.save(b.get("kind"), b.get("name"), b.get("content"))
        return 200, {"name": name}
    if path == "/api/library/rename":
        b = need_body(body)
        lib.rename(b.get("kind"), b.get("from"), b.get("to"))
        return 200, {"name": b.get("to")}
    if path == "/api/library/delete":
        b = need_body(body)
        lib.delete(b.get("kind"), b.get("name"))
        return 200, {"ok": True}

    if path == "/api/generate":
        b = need_body(body)
        out = op_generate(solver, b)
        solver.last_generate = dict(b)   # fast preview builds its flop sketch from the same config
        return 200, out

    if path == "/api/build":
        solver.cmd("build_tree")
        solver.forget_locks()
        solver.tree_present = True
        solver.loaded_file = False
        solver.reset_series()
        solver.clear_partial()
        return 200, {"node_count": solver.cmd("node_count")}

    if path == "/api/solve":
        b = need_body(body)
        acc = b.get("accuracy", 0.05)
        if isinstance(acc, bool) or not isinstance(acc, (int, float)) or not math.isfinite(acc) or acc < 0:
            raise SolverError("bad accuracy")
        # Report (and check the target) every 10 iterations: a smoother convergence chart (#115)
        # and less overshoot past the target than the default 25.
        solver.cmds([f"set_accuracy {acc:g}", "set_info_freq auto", "go"])
        solver.clear_partial()
        return 200, {"ok": True}

    if path == "/api/solve_node":
        return 200, op_solve_node(solver, need_body(body))

    if path == "/api/stop":
        return 200, {"text": solver.cmd("stop")}

    if path == "/api/save":
        b = need_body(body)
        path_arg = safe_path(b.get("path", ""))
        mode = " no_rivers" if b.get("no_rivers") else " no_turns" if b.get("no_turns") else ""
        solver.cmd(f'dump_tree "{path_arg}"{mode}')
        return 200, {"ok": True}

    if path == "/api/load":
        b = need_body(body)
        path_arg = safe_path(b.get("path", ""))
        solver.cmds(["stop", "wait_for_solver", f'load_tree "{path_arg}"'])
        solver.forget_locks()
        solver.tree_present = True
        solver.loaded_file = True
        solver.reset_series()
        solver.clear_partial()
        return 200, {"ok": True}

    if path == "/api/lock":
        return 200, op_lock(solver, need_body(body))
    if path == "/api/unlock":
        return 200, op_unlock(solver, need_body(body))
    if path == "/api/lock/revert":
        return 200, op_lock_revert(solver)
    if path == "/api/lock/apply":
        return 200, op_lock_apply(solver, need_body(body))

    if path == "/api/restart":
        solver.restart()
        return 200, {"ok": True}

    return 404, {"error": "unknown endpoint"}


def ev_pair(solver, player, node_id):
    lines = solver.cmd(f"calc_ev {player} {node_id}").split("\n")
    ev = parse_floats(lines[0], NUM_COMBOS)
    matchups = parse_floats(lines[1], NUM_COMBOS) if len(lines) > 1 else None
    return {"ev": ev, "matchups": matchups}


def hand_order(solver):
    """show_hand_order, cached on the solver (the Pio hand order never changes)."""
    if not getattr(solver, "hand_order_cache", None):
        solver.hand_order_cache = solver.cmd("show_hand_order").split()
    return solver.hand_order_cache


def categories_at(solver, node_id, player):
    """Made-hand and draw category report at a decision node (#154).

    Chance and terminal nodes fall back to the closest decision above them, the same
    rule the Study matrix uses. Shares, per-category strategies and EVs are for
    `player` ("actor" resolves to the decision's actor; the toggle in the UI passes
    OOP/IP explicitly). Each category row carries its weighted combos, share of the
    range, a range-weighted strategy per child action (only when the queried player
    is the one to act), a range-and-matchups-weighted EV (the same average the Node
    tab shows), and the matrix classes its combos live in.
    """
    player = player.upper()
    empty = {"id": None, "board": "", "actor": None, "player": player if player != "ACTOR" else None,
             "total": 0, "made_names": [], "draw_names": [], "made": [], "draws": []}
    with solver.cmd_lock:
        toks = node_id.split(":")
        node = None
        while len(toks) >= 2:
            node = parse_node_block(solver.cmd("show_node " + ":".join(toks)))
            if node["type"] in ("OOP_DEC", "IP_DEC"):
                break
            toks.pop()
        if node is None or node["type"] not in ("OOP_DEC", "IP_DEC"):
            return empty
        nid = ":".join(toks)
        actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
        if player == "ACTOR":
            player = actor
        board = "".join(node["board"].split())
        if len(board) < 6:  # preflop decisions have no categories
            return {**empty, "id": nid, "actor": actor, "player": player}

        names = solver.cmd("show_category_names").split("\n")
        made_names, draw_names = names[0].split(), names[1].split()
        cat_lines = solver.cmd("show_categories " + board).split("\n")
        made_ids = [int(t) for t in cat_lines[0].split()]
        draw_ids = [int(t) for t in cat_lines[1].split()]
        if len(made_ids) != NUM_COMBOS or len(draw_ids) != NUM_COMBOS:
            raise SolverError("unexpected show_categories output")
        reach = parse_floats(solver.cmd(f"show_range {player} {nid}"), NUM_COMBOS)
        order = hand_order(solver)

        strat_rows = None
        if player == actor:
            rows = [l for l in solver.cmd("show_strategy " + nid).split("\n") if l.strip()]
            if all(len(r.split()) == NUM_COMBOS for r in rows):
                strat_rows = [parse_floats(r, NUM_COMBOS) for r in rows]

        ev_lines = solver.cmd(f"calc_ev {player} {nid}").split("\n")
        evs = parse_floats(ev_lines[0], NUM_COMBOS)
        mus = parse_floats(ev_lines[1], NUM_COMBOS) if len(ev_lines) > 1 else None

    def new_acc(cid):
        return {"id": cid, "combos": 0.0,
                "strat": [0.0] * len(strat_rows) if strat_rows else None,
                "ev_num": 0.0, "ev_den": 0.0, "classes": set()}

    accs = {}
    for i in range(NUM_COMBOS):
        w = reach[i] or 0
        if w <= 0:
            continue
        cls = combo_class(order[i])
        for key in (("m", made_ids[i]), ("d", draw_ids[i])):
            if key[1] < 0:  # blocked combo on a flop/turn board
                continue
            acc = accs.setdefault(key, new_acc(key[1]))
            acc["combos"] += w
            if acc["strat"] is not None:
                for j, row in enumerate(strat_rows):
                    acc["strat"][j] += w * (row[i] or 0)
            mu = mus[i] if mus else 1
            if evs[i] is not None and mu and mu > 0:
                acc["ev_num"] += w * mu * evs[i]
                acc["ev_den"] += w * mu
            acc["classes"].add(cls)

    total = sum(w for w in reach if w)

    def row(key, names):
        acc = accs[key]
        combos, cid = acc["combos"], acc["id"]
        r = {"id": cid, "name": names[cid] if cid < len(names) else str(cid),
             "combos": round(combos, 4), "pct": combos / total if total else 0,
             "strategy": [round(x / combos, 6) if combos else 0 for x in acc["strat"]]
                         if acc["strat"] is not None else None,
             "ev": round(acc["ev_num"] / acc["ev_den"], 4) if acc["ev_den"] > 0 else None,
             "classes": sorted(acc["classes"], key=class_sort_key)}
        return r

    made = [row(k, made_names) for k in sorted(accs) if k[0] == "m" and accs[k]["combos"] > 0]
    draws = [row(k, draw_names) for k in sorted(accs) if k[0] == "d" and accs[k]["combos"] > 0]
    made.reverse()   # strongest made hands first; best draws first
    draws.reverse()
    return {"id": nid, "board": board, "actor": actor, "player": player,
            "total": round(total, 4),
            "made_names": made_names, "draw_names": draw_names,
            "made": made, "draws": draws}


def require_post(method):
    if method != "POST":
        raise SolverError("POST required")


def need_body(body):
    if not isinstance(body, dict):
        raise SolverError("JSON body required")
    return body


def safe_path(p):
    if not isinstance(p, str) or not p or '"' in p or "\n" in p or "\r" in p:
        raise SolverError("bad path")
    return p


def no_newlines(value, name):
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise SolverError(f"bad {name}")
    return value


def _check_sizes(tb, b, key, allow_mult=True):
    """A comma-separated size list in tools/tree_builder.py's grammar (#114): percentages,
    geometric Ne, raise multiples Nx (raise fields only), a. Returns the normalized list."""
    val = no_newlines(str(b.get(key, "") if b.get(key) is not None else ""), key).strip()
    if not val:
        return ""
    parts = [p.strip() for p in val.split(",") if p.strip()]
    try:
        specs = tb.parse_size_list(val)
    except ValueError:
        raise SolverError(f"bad sizes in {key} "
                          "(want percentages, geometric Ne, raise multiples Nx, or a)")
    if len(specs) != len(parts):
        raise SolverError(f"bad sizes in {key}")
    for spec in specs:
        if spec.kind == "mult":
            if not allow_mult or spec.value > 1000:
                raise SolverError(f"bad sizes in {key} (Nx is for raise fields, N <= 1000)")
        elif spec.kind == "geo":
            if spec.value is not None and spec.value > 99:
                raise SolverError(f"bad sizes in {key} (Ne takes N <= 99)")
        elif spec.kind == "pct" and spec.value > 100000:
            raise SolverError(f"bad sizes in {key}")
    return ",".join(parts)


def _raise_cap(b, key):
    val = b.get(key)
    if val is None or val == "":
        return None
    try:
        cap = int(val)
        if cap < 0 or cap > 99:
            raise ValueError
    except (TypeError, ValueError):
        raise SolverError(f"bad {key} (want a non-negative integer)")
    return cap


def sizing_header(b, tb):
    """Sizing fields of a request body -> tree builder #Key#Value lines (all but pot/stack/board).

    Per street: {street}_bet / _raise / _donk / _allin are OOP's sizes (donk is OOP-only), and
    IP's too unless the body has IP's own {street}_bet_ip / _raise_ip / _allin_ip (#141; an
    empty IP value means IP has no sizes there). {street}_raisecap caps both players.
    """
    try:
        allin_threshold = float(b.get("allin_threshold", 67))
        add_allin_limit = float(b.get("add_allin_limit", 300))
    except (TypeError, ValueError):
        raise SolverError("bad all-in numbers")
    header = [f"#AllinThreshold#{allin_threshold:g}",
              f"#AddAllinOnlyIfLessThanThisTimesThePot#{add_allin_limit:g}"]
    for street, name in (("flop", "Flop"), ("turn", "Turn"), ("river", "River")):
        cap = _raise_cap(b, f"{street}_raisecap")
        for player, sfx in (("", ""), ("IP", "_ip")):
            def key(k):
                own = f"{street}_{k}{sfx}"
                return own if sfx and own in b else f"{street}_{k}"
            bet = _check_sizes(tb, b, key("bet"), allow_mult=False)
            rais = _check_sizes(tb, b, key("raise"))
            if bet:
                header.append(f"#{name}Config{player}.BetSize#{bet}")
            if rais:
                header.append(f"#{name}Config{player}.RaiseSize#{rais}")
            if not player:
                donk = _check_sizes(tb, b, f"{street}_donk", allow_mult=False)
                if donk:
                    header.append(f"#{name}Config.DonkBetSize#{donk}")
            if bool(b.get(key("allin"))):
                header.append(f"#{name}Config{player}.AddAllin#True")
            if cap is not None:
                header.append(f"#{name}Config{player}.RaiseCap#{cap}")
    return header


def op_solve_node(solver, b):
    """POST /api/solve_node (#153): continue solving the in-memory tree from a node down.

    accuracy is a target exploitability in chips (the UI sends % of the node's pot, converted).
    At the tree root this is exactly a continued whole-tree solve. Otherwise the solver runs
    `go_partial <id>`: async subtree iterations (solve_partial's iterate_subtree path) whose
    progress blocks are marked "partial: <id>" and routed into state()["partial"].
    """
    node_id = node_id_arg({"id": [str(b.get("id", "r:0"))]})
    try:
        acc = float(b.get("accuracy"))
    except (TypeError, ValueError):
        raise SolverError("bad accuracy")
    if not math.isfinite(acc) or acc <= 0 or acc > 1e9:
        raise SolverError("bad accuracy (want a positive number, chips)")
    if solver.state()["solving"]:
        raise SolverError("the solver is already running (stop it first)")
    if node_id in ("r", "r:0"):
        # continuing at the root IS the whole-tree solve (issue's suggested design)
        solver.cmds([f"set_accuracy {acc:g}", "set_info_freq auto", "go"])
        solver.clear_partial()
        return {"ok": True, "root": True}
    if solver.loaded_file:
        raise SolverError("solve further is not available for a tree loaded from a file "
                          "(view-only saves carry no solver state to continue from)")
    with solver.cmd_lock:
        node = parse_node_block(solver.cmd("show_node " + node_id))
        if node["type"] not in ("OOP_DEC", "IP_DEC"):
            raise SolverError(f"{node['type']} is not a decision node")
        pot = sum(p or 0 for p in node["pot"])
        # mark in flight BEFORE go_partial returns, so the pump routes the first block right
        solver.start_partial(node_id, acc, pot)
        try:
            solver.cmds([f"set_accuracy {acc:g}", "set_info_freq auto", "go_partial " + node_id])
        except SolverError:
            solver.clear_partial()
            raise
    return {"ok": True, "node": node_id, "pot": pot}


def _decision(solver, node_id):
    node = parse_node_block(solver.cmd("show_node " + node_id))
    if node["type"] not in ("OOP_DEC", "IP_DEC"):
        raise SolverError(f"{node['type']} is not a decision node")
    return node, parse_children(solver.cmd("show_children " + node_id))


def lock_state(solver, node_id):
    """GET /api/lock_state?id= (#200): is the node locked, which combos, and the session's lock list."""
    node, _ = _decision(solver, node_id)
    flags = node["flags"].split()
    combos = parse_floats(solver.cmd("show_locked_combos " + node_id), NUM_COMBOS)
    return {"id": node_id, "locked": "LOCKED" in flags, "combo_locked": "COMBO_LOCKED" in flags,
            "combos": [1 if (c or 0) > 0 else 0 for c in combos], "locks": dict(solver.locks),
            "undo": bool(solver.undo), "can_solve": not solver.loaded_file}


def op_lock(solver, b):
    """POST /api/lock (#200): set and lock a decision node's strategy.

    mode "current": lock the strategy as solved. mode "node": `freq` (actions x 1326, the
    show_strategy layout) becomes the node's strategy and the whole node is locked. mode "combos":
    the same, but only the combos in `combos` (1326 0/1) are locked; the others keep solving from
    the given values (send their current strategy). Before the tree's first lock, `snapshot` keeps
    a lossless save of it so /api/lock/revert can bring the unlocked solve back.
    """
    node_id = node_id_arg({"id": [str(b.get("id", ""))]})
    if solver.state()["solving"]:
        raise SolverError("the solver is running (stop it first)")
    mode = b.get("mode")
    if mode not in ("current", "node", "combos"):
        raise SolverError("mode must be current, node or combos")
    _, kids = _decision(solver, node_id)
    cmds = []
    if mode != "current":
        freq = b.get("freq")
        if not isinstance(freq, list) or len(freq) != len(kids) or any(
                not isinstance(r, list) or len(r) != NUM_COMBOS for r in freq):
            raise SolverError(f"freq must be {len(kids)} rows of {NUM_COMBOS} numbers")
        cols = []
        for i in range(NUM_COMBOS):
            try:
                v = [float(r[i]) if r[i] is not None else 0.0 for r in freq]
            except (TypeError, ValueError):
                raise SolverError("freq: numbers only")
            if any(not math.isfinite(x) or x < 0 for x in v):
                raise SolverError("freq: finite, non-negative numbers only")
            s = sum(v)
            cols.append([x / s for x in v] if s > 0 else [1.0 / len(v)] * len(v))
        rows = [" ".join(f"{cols[i][k]:.6g}" for i in range(NUM_COMBOS)) for k in range(len(kids))]
        cmds.append(f"set_strategy {node_id} " + " ".join(rows))
        cmds.append(f"unlock_node {node_id}")  # lock_node freezes the strategy just set
        if mode == "combos":
            mask = b.get("combos")
            if not isinstance(mask, list) or len(mask) != NUM_COMBOS:
                raise SolverError(f"combos must be {NUM_COMBOS} 0/1 values")
            if not any(mask):
                raise SolverError("no combos selected to lock")
            cmds.append(f"combo_lock_node {node_id} " + " ".join("1" if m else "0" for m in mask))
        else:
            cmds.append(f"lock_node {node_id}")
    else:
        cmds.append(f"lock_node {node_id}")
    with solver.cmd_lock:
        if b.get("snapshot") and solver.undo is None and not solver.loaded_file:
            import tempfile
            fd, snap = tempfile.mkstemp(prefix="wsolver-unlocked-", suffix=".cfr")
            os.close(fd)
            solver.cmd(f'dump_tree "{snap}" lossless', timeout=3600)
            solver.undo = {"path": snap}
        solver.cmds(cmds)
        solver.locks[node_id] = "combos" if mode == "combos" else "node"
    return lock_state(solver, node_id)


def op_unlock(solver, b):
    """POST /api/unlock {id} | {all: true} (#200). The node keeps its (locked) strategy as a start."""
    if solver.state()["solving"]:
        raise SolverError("the solver is running (stop it first)")
    zeros = " ".join(["0"] * NUM_COMBOS)
    with solver.cmd_lock:
        if b.get("all"):
            # Every lock set from this session, then Pio's unlock_all (node locks only, #169) for
            # locks that came with a loaded save, where the solver has it.
            for i in list(solver.locks):
                solver.cmds([f"unlock_node {i}", f"combo_lock_node {i} {zeros}"])
            try:
                solver.cmd("unlock_all")
            except SolverError:
                pass
            solver.locks = {}
            return {"locks": {}}
        node_id = node_id_arg({"id": [str(b.get("id", ""))]})
        _decision(solver, node_id)
        solver.cmds([f"unlock_node {node_id}", f"combo_lock_node {node_id} {zeros}"])
        solver.locks.pop(node_id, None)
        return lock_state(solver, node_id)


def _actions(kids):
    return [k["id"].rsplit(":", 1)[-1] for k in kids]


def op_lock_export(solver):
    """GET /api/lock/export (#200): the locks set in this session, in the form a library spot
    keeps them (`locks` in a config) and /api/lock/apply takes back after a build of the same tree.

    Each is {id, mode "node"|"combos", actions, freq, combos?}: `actions` the child tokens (so a
    different tree is noticed), `freq` one space-separated row of 1326 per action (the
    show_strategy layout, as solved now: for a whole-node lock, exactly what was locked), and for
    "combos" a 1326-character 0/1 string of the locked combos."""
    out = []
    with solver.cmd_lock:
        for node_id, mode in solver.locks.items():
            _, kids = _decision(solver, node_id)
            rows = [l for l in solver.cmd("show_strategy " + node_id).split("\n") if l.strip()]
            freq = [" ".join(f"{(v or 0):.6g}" for v in parse_floats(r, NUM_COMBOS)) for r in rows]
            item = {"id": node_id, "mode": mode, "actions": _actions(kids), "freq": freq}
            if mode == "combos":
                mask = parse_floats(solver.cmd("show_locked_combos " + node_id), NUM_COMBOS)
                item["combos"] = "".join("1" if (c or 0) > 0 else "0" for c in mask)
            out.append(item)
    return {"locks": out}


def op_lock_apply(solver, b):
    """POST /api/lock/apply {locks: [...]} (#200): set the locks /api/lock/export gave, on a tree
    just built from the same spot. A lock whose node is missing or has other actions (the tree
    changed since) is skipped and reported; the others are set. No undo copy is made."""
    locks = b.get("locks")
    if not isinstance(locks, list):
        raise SolverError("locks must be a list")
    applied, skipped = [], []
    for item in locks:
        if not isinstance(item, dict):
            raise SolverError("each lock must be an object")
        node_id = str(item.get("id", ""))
        try:
            node_id = node_id_arg({"id": [node_id]})
            _, kids = _decision(solver, node_id)
            if item.get("actions") is not None and item.get("actions") != _actions(kids):
                raise SolverError("the node has other actions now")
            freq = item.get("freq")
            if not isinstance(freq, list) or not all(isinstance(r, str) for r in freq):
                raise SolverError("freq must be one string row per action")
            body = {"id": node_id, "mode": item.get("mode"),
                    "freq": [[float(t) for t in r.split()] for r in freq]}
            if item.get("mode") == "combos":
                c = item.get("combos")
                if not isinstance(c, str) or len(c) != NUM_COMBOS or set(c) - {"0", "1"}:
                    raise SolverError(f"combos must be {NUM_COMBOS} 0/1 characters")
                body["combos"] = [1 if ch == "1" else 0 for ch in c]
            elif item.get("mode") != "node":
                raise SolverError("mode must be node or combos")
            op_lock(solver, body)
            applied.append(node_id)
        except (SolverError, ValueError) as e:
            if solver.state()["solving"]:
                raise
            skipped.append({"id": node_id, "error": str(e)})
    return {"applied": applied, "skipped": skipped, "locks": dict(solver.locks)}


def op_lock_revert(solver):
    """POST /api/lock/revert (#200): back to the tree as it was before its first lock."""
    if not solver.undo:
        raise SolverError("no unlocked copy of this tree was kept")
    if solver.state()["solving"]:
        raise SolverError("the solver is running (stop it first)")
    with solver.cmd_lock:
        solver.cmds([f'load_tree "{solver.undo["path"]}" full'])
        # a lossless save keeps the regrets: the reverted tree can be solved further
        solver.locks = {}
        solver.tree_present = True
        solver.loaded_file = False
        solver.reset_series()
        solver.clear_partial()
    return {"ok": True}


def op_generate(solver, b):
    """PioViewer-style bet sizes -> add_lines via tools/tree_builder.py, then configure the solver."""
    range_oop = no_newlines(str(b.get("range_oop", "")).strip(), "range_oop")
    range_ip = no_newlines(str(b.get("range_ip", "")).strip(), "range_ip")
    board = no_newlines(str(b.get("board", "")).replace(" ", "").strip(), "board")
    if not range_oop or not range_ip:
        raise SolverError("both ranges are required")
    try:
        pot = int(b["pot"])
        stack = int(b["stack"])
        allin_threshold = float(b.get("allin_threshold", 67))
        add_allin_limit = float(b.get("add_allin_limit", 300))
        if pot < 0 or stack <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SolverError("bad pot/stack/all-in numbers")

    tb = load_tree_builder()
    header = [f"#Pot#{pot}", f"#EffectiveStacks#{stack}"]
    if board:
        header.append(f"#Board#{board}")
    header += sizing_header(b, tb)

    try:
        cfg = tb.parse_config("\n".join(header))
        # A 4- or 5-card board starts the tree on the turn or river: the builder must start
        # there too, or its lines run past the end of the hand.
        n_cards = len(board) // 2
        start = tb.Node(n_cards - 3, (0, 0), tb.OOP, False, None) if n_cards in (4, 5) else None
        lines = tb.format_lines(tb.build_lines(cfg, start))
    except (ValueError, ArithmeticError) as e:
        raise SolverError(f"bad sizes: {e}")
    if not lines:
        raise SolverError("config produced no lines (check bet/raise sizes)")

    # Isomorphism is always on (owner decision, #113), for turn cards too: "1 1" also merges
    # suit-isomorphic turns when the tree starts on the turn (26% fewer nodes and faster on turn
    # trees, #137; EV and exploitability identical, strategies within <1 point of the "1 0" solve,
    # both equilibria). The UPI default stays "1 0" for Pio output parity. An "iso" field from old
    # clients is ignored.
    cmds = ["stop", "wait_for_solver",
            "set_range OOP " + range_oop,
            "set_range IP " + range_ip]
    if board:
        cmds.append("set_board " + board)
    cmds += [f"set_eff_stack {stack}", f"set_pot 0 0 {pot}",
             "set_isomorphism 1 1", "clear_lines"]
    cmds += lines
    solver.cmds(cmds)
    solver.forget_locks()
    solver.tree_present = False
    solver.loaded_file = False
    solver.reset_series()
    solver.clear_partial()
    return {"lines": len(lines), "sample": [l[len("add_line "):] for l in lines[:8]]}


# ---------------------------------------------------------------- HTTP layer

STATIC_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\.(html|js|css|svg)")
STATIC_TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, ctype="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
        else:
            body = payload if isinstance(payload, bytes) else payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if not self._allowed("GET"):
            return
        # Flat files in viewer/static only: the name pattern has no separators or dots beyond the
        # extension, so "..", subdirectories and absolute paths can never match.
        name = "index.html" if path == "/" else path[1:]
        m = STATIC_NAME_RE.fullmatch(name)
        full = os.path.join(STATIC, name)
        if not m or not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES[m.group(1)])

    def _allowed(self, method):
        """Refuse requests a web page on another site could make (#48 follow-up).

        Host must name the loopback address we serve on, which blocks DNS-rebinding pages from
        reading the API. POSTs must be application/json: browsers send that type cross-origin
        only after a CORS preflight, which this server never grants, so another site cannot
        trigger save/load (file writes) or solves with a "simple" text/plain request.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            self._send(403, {"ok": False, "error": "bad Host header"})
            return False
        if method == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(415, {"ok": False, "error": "Content-Type must be application/json"})
                return False
        return True

    def _api(self, method):
        if not self._allowed(method):
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        body = None
        if method == "POST":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            if raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    self._send(400, {"ok": False, "error": "bad JSON"})
                    return
        try:
            status, data = api(self.server.solver, self.server.library, method, parsed.path, query, body)
        except (SolverError, LibraryError) as e:
            status, data = 400, {"ok": False, "error": str(e)}
        except Exception as e:  # keep the server alive; show the broken command's error
            status, data = 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if isinstance(data, dict):
            data.setdefault("ok", status == 200)
        self._send(status, data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            self._api("GET")
        else:
            self._static(path)

    def do_POST(self):
        self._api("POST")

    def do_PUT(self):
        self._send(405, {"ok": False, "error": "method not allowed"})

    def do_DELETE(self):
        self._send(405, {"ok": False, "error": "method not allowed"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", default=os.path.join(REPO, "solver"), help="path to the solver binary")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--library", default=os.path.join("~", ".wsolver", "library"),
                    help="root for saved ranges and tree configs (#116)")
    args = ap.parse_args(argv)

    solver = Solver(os.path.abspath(args.solver))
    import resolve as vresolve  # local import: server/main <-> resolve module cycle
    set_resolver(vresolve.ResolveManager(solver, Solver, os.path.abspath(args.solver)))
    import refine as vrefine
    sketch = vrefine.SketchManager(solver, Solver, os.path.abspath(args.solver))
    set_refiner(vrefine.RefineManager(solver, Solver, os.path.abspath(args.solver), sketch), sketch)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    srv.solver = solver
    srv.library = Library(args.library)
    print(f"WSolver viewer: solver {solver.exe!r}, library {srv.library.root!r}, "
          f"UI on http://127.0.0.1:{srv.server_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        for s in ([solver] + ([_resolver.rsolver] if _resolver else []) + ([_refiner.rsolver] if _refiner else []) + ([_sketch.solver] if _sketch else [])):
            try:
                s.proc.kill()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
