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
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from library import Library, LibraryError  # noqa: E402
import hand_lines  # noqa: E402
import preflop_api  # noqa: E402

# Run as a script this module is __main__; resolve.py and jobs_api.py `import server`. Register it
# under that name so they share these classes (a second copy would have its own SolverError, which
# the request handler would not catch as a 400).
if __name__ == "__main__":
    sys.modules.setdefault("server", sys.modules["__main__"])

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
        self.loaded_path = None   # ... from this file (meaningful while loaded_file; the Study report button)
        self.browse_snapshot = None
        self.drill_context = None
        # The Build form the current tree's lines came from (op_generate): street refines rebuild
        # their subgames with these sizes, not with whatever the form holds by the time you browse.
        self.built_sizes = None
        self.preflop = None      # the tree is a preflop tree built here (preflop_api.build): {chips, flops}
        # preflop_api.build left its settings in the solver (16-bit storage, collapsed check-downs):
        # the next postflop setup puts the solver's own back (op_generate)
        self.preflop_touched = False
        self.holds = set()       # who keeps the solve parked (UPI hold): "browse" lease, "refine" (refine.py)
        self._lease_until = 0.0
        self._lease_thread = None
        self.iter_seconds = None  # measured solver time per iteration of the running solve (note_iters)
        self._iter_mark = None    # (reports, solver time, iterations) at the last report seen
        self._hold_wait = None    # how long the last hold took to land (a stand-in until then)
        self.locks = {}          # node id -> "node" | "combos": locks set from the viewer (#200)
        self.drill_variant = None
        self.undo = None         # {"path", "time"}: lossless save of the tree before its first lock
        self.start()

    def start(self):
        """Spawn the solver and its pump. Caller must hold cmd_lock (or be the only thread)."""
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.generation += 1
        self.q = queue.Queue()
        self.unread = 0  # answers of timed-out commands still to skip (#382)
        threading.Thread(target=self._pump, args=(self.generation, self.proc.stdout, self.q),
                         daemon=True).start()
        self.send_raw("set_end_string " + END)
        self._read_response()  # banner lines, skipped until the first END
        self.version = self.cmd("show_version")
        try:
            algorithms = self.cmd("list_algorithms").splitlines()
            self.devices = ["cpu", "gpu"] if any(a.startswith("v4_metal,") for a in algorithms) else ["cpu"]
        except SolverError:
            self.devices = ["cpu"]
        self.cpu_algorithm = "auto"
        self.device = "cpu"
        if "gpu" in self.devices:
            self.cmd("set_algorithm v4_metal")
            self.device = "gpu"
        self.active_device = None

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
                            self.active_device = "cpu"
                            self.progress = []
                        elif msg.startswith("device "):
                            self.active_device = msg.split()[1]
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
                # the answer still comes, later: the next command must skip it, or every command after
                # this one would read the one before's answer (#382)
                self.unread = getattr(self, "unread", 0) + 1
                raise SolverError("timeout waiting for solver response")
            if line is None:
                raise SolverError("solver exited")
            if line == END:
                return "\n".join(out)
            out.append(line)

    def _drain_unread(self, timeout):
        """Skip the answers of commands that timed out (caller holds cmd_lock). The solver answers in
        order, so these come before the next command's own answer."""
        while getattr(self, "unread", 0) > 0:
            out = []
            while True:
                try:
                    l = self.q.get(timeout=timeout)
                except queue.Empty:
                    raise SolverError("the solver is still busy with an earlier command") from None
                if l is None:
                    self.unread = 0
                    raise SolverError("solver exited")
                if l == END:
                    break
                out.append(l)
            self.unread -= 1

    def cmd(self, line, timeout=600):
        """Run one UPI command. Raises SolverError on an ERROR: response."""
        with self.cmd_lock:
            self._drain_unread(timeout)
            self.send_raw(line)
            resp = self._read_response(timeout)
        if resp.startswith("ERROR"):
            raise SolverError(resp.split("\n", 1)[0])
        return resp

    def cmds(self, lines, timeout=600):
        """Several commands in one lock hold; stops at the first ERROR."""
        with self.cmd_lock:
            self._drain_unread(timeout)
            for line in lines:
                self.send_raw(line)
                resp = self._read_response(timeout)
                if resp.startswith("ERROR"):
                    raise SolverError(f"{line.strip()[:120]} -> {resp.splitlines()[0]}")

    def device_algorithm(self, device=None):
        with self.state_lock:
            return "v4_metal" if (device or self.device) == "gpu" else self.cpu_algorithm

    def state(self):
        with self.state_lock:
            partial = dict(self.partial) if self.partial else None
            if partial:
                partial["progress"] = list(partial["progress"])
            return {"solving": self.solving, "suspended": "refine" in self.holds,
                    "devices": self.devices, "device": self.device, "active_device": self.active_device,
                    "dead": self.dead, "progress": list(self.progress),
                    "solver_status": self.solver_status, "events": self.events,
                    "tree": self.tree_present and not self.dead, "version": self.version,
                    "partial": partial, "loaded_file": self.loaded_file,
                    "loaded_path": self.loaded_path if self.loaded_file else None,
                    "drill_variant": self.drill_variant}

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
        iteration time is measured from the progress reports (note_iters); before the first
        two, the wait for the last hold to land (the cost a request just paid) stands in."""
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
            self._iter_mark = None

    def note_iters(self, iters):
        """Measure seconds per iteration from the whole-tree reports: the first poll after each
        report pairs its solver time with the iteration count then. Off by at most the
        iterations run between the report and the poll, over gaps of many iterations."""
        with self.state_lock:
            if not self.series or self.partial or not isinstance(iters, int):
                return
            n, t = len(self.series), self.series[-1]["t"]
            m = self._iter_mark
            if m is not None and n == m[0]:
                return
            if m is not None and iters > m[2] and t > m[1]:
                self.iter_seconds = (t - m[1]) / (iters - m[2])
            self._iter_mark = (n, t, iters)

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
                self.built_sizes = None
                self.preflop = None
                self.preflop_touched = False   # a new process: the solver's own settings
            self.holds.clear()
            self.forget_locks()

    def own_settings(self):
        """The commands that put back what a preflop build switched on (16-bit storage, collapsed
        check-downs): sent before a postflop tree is set up or a file loaded, so that tree is the one a
        viewer that never built a preflop tree gets."""
        return ["small_strats auto", "set_collapse_checkdowns off"] if self.preflop_touched else []

    def cmds_own(self, before, after, timeout=600):
        """`before`, the solver's own settings back if a preflop build changed them, `after`. The
        settings count as put back only once the commands went through: a failed `stop` leaves them
        to the next try."""
        with self.cmd_lock:
            out = self.cmds(before + self.own_settings() + after, timeout=timeout)
            self.preflop_touched = False
            return out

    def forget_locks(self):
        """A new tree (generate / build / load / restart): drop the lock list and the undo copy."""
        self.locks = {}
        self.drill_variant = None
        self.drill_context = None
        if self.browse_snapshot:
            self.browse_snapshot.cleanup()
            self.browse_snapshot = None
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
_preview = None   # fast preview's progress line: the target (chips) of the solve it follows


def set_resolver(resolver):
    global _resolver
    _resolver = resolver


_nl = None  # nl_lock.NLService (#200 part 2): plain-English locks via Sail; set by main()/tests


def set_nl(service):
    global _nl
    _nl = service


_jobs = None  # jobs_api.JobsService (#196), set by main()/tests; runs the solve queue
_drill = None


def set_drill(service):
    global _drill
    _drill = service


def set_jobs(service):
    global _jobs
    _jobs = service


def set_refiner(refiner):
    global _refiner, _preview
    _refiner = refiner
    _preview = None


def lib_query(query, name="name"):
    """Query-arg wrapper for library endpoints; LibraryError -> 400 via the handler."""
    return query.get(name, [""])[0]


# POSTs that change the main tree's strategies (or drop it): a re-solve shown in place was built on
# the old ranges, so it is no longer shown - nor is a running one once it finishes.
VARIANT_ENDERS = ("/api/lock", "/api/unlock", "/api/lock/revert", "/api/lock/apply", "/api/solve",
                  "/api/solve_node", "/api/restart", "/api/best_response")


def api(solver, lib, method, path, query, body):
    """Dispatch one API call; returns (status, data)."""
    if path.startswith("/api/drill/"):
        if _drill is None:
            raise SolverError("drilling is not initialized")
        return _drill.api(method, path, body)
    status, data = _api(solver, lib, method, path, query, body)
    if method == "POST" and path in VARIANT_ENDERS and status == 200 and _resolver is not None:
        _resolver.restore()   # only once it happened: a refused lock (while solving, say) changes nothing
    return status, data


def _api(solver, lib, method, path, query, body):
    if path == "/api/resolve" or path.startswith("/api/resolve/"):
        if _resolver is None:
            raise SolverError("re-solve support not initialized")
        return _resolver.api(method, path, query, body)
    if path.startswith("/api/nl/"):
        if _nl is None:
            raise SolverError("plain-English locks not initialized")
        solver.lease()  # it reads the node like a browse click (a no-op unless solving)
        return _nl.api(sys.modules[__name__], solver, method, path, body)
    if path.startswith("/api/jobs/"):
        if _jobs is None:
            raise SolverError("job queue not initialized")
        return _jobs.api(solver, method, path, query, body)
    if path == "/api/files" and method == "GET":
        return 200, saved_files(query.get("path", ["~"])[0])
    if path == "/api/refine" or path.startswith("/api/refine/"):
        if _refiner is None:
            raise SolverError("fast preview not initialized")
        return _refiner.api(method, path, query, body)
    if method == "GET" and path in BROWSE_PATHS:
        solver.lease()
    if _resolver is not None and method == "GET":
        served = _resolver.overlay(path, query)   # a re-solve shown in place, at or below its node
        if served is not None:
            return served
    global _preview
    if _refiner is not None:
        if method == "GET":
            served = _refiner.overlay(path, query)
            if served is not None:
                return served
        elif path in ("/api/build", "/api/load", "/api/restart", "/api/generate"):
            # (a preflop build does this itself, once its form is accepted: preflop_api.build)
            _refiner.invalidate()  # the main tree is new: old refines don't apply
            _preview = None
        elif path in LOCK_PATHS and not solver.state()["solving"]:
            # A lock, unlock or revert changes the main tree's strategies: a refined street was
            # solved against the old ones. (While solving the request is refused: keep them.)
            _refiner.invalidate()
        if method == "POST" and path == "/api/solve":
            b = need_body(body)
            # Only a fresh tree gets the progress line: Resume on a stopped solve just continues it.
            fresh = not solver.series and not solver.solving
            acc = b.get("accuracy", 0.05)
            if b.get("preview") and fresh and solver.tree_present and not solver.loaded_file and not solver.preflop \
                    and isinstance(acc, (int, float)) and not isinstance(acc, bool) \
                    and math.isfinite(acc) and acc > 0:
                _refiner.invalidate()
                try:
                    status, data = _api_main(solver, lib, method, path, query, body)
                finally:
                    solver.drop_holds()
                _preview = acc
                data["preview"] = True
                return status, data
            _preview = None   # any other solve (Resume, no target, preview off): no progress line
    target = _preview         # read once: a concurrent build may clear it
    if path == "/api/progress" and _refiner is not None and target is not None:
        status, data = _api_main(solver, lib, method, path, query, body)
        import refine as vrefine   # (refine imports this module: not at the top)
        data["preview"] = vrefine.preview_status(solver, target)
        return status, data
    if method == "POST" and path in ("/api/stop", "/api/solve", "/api/solve_node", "/api/build", "/api/load",
                                    "/api/lock/revert"):
        # these stop or restart the solve, and the solver drops any hold with it
        # (a preflop build can be refused long before its stop, the solve running on under a hold
        # that must still be let go: preflop_api.build drops the holds once its stop is sent)
        try:
            return _api_main(solver, lib, method, path, query, body)
        finally:
            solver.drop_holds()
    return _api_main(solver, lib, method, path, query, body)


LOCK_PATHS = ("/api/lock", "/api/unlock", "/api/lock/revert", "/api/lock/apply", "/api/best_response")
BROWSE_PATHS = ("/api/node", "/api/strategy", "/api/path", "/api/range", "/api/categories", "/api/ev",
                "/api/calc_results", "/api/hand_order", "/api/blockers", "/api/graphs", "/api/hand_lines")


def _api_main(solver, lib, method, path, query, body):
    if method == "POST" and path == "/api/device":
        device = need_body(body).get("device")
        with solver.cmd_lock:
            if device not in solver.devices:
                raise SolverError("unknown solve device")
            if device == "gpu" and solver.device != "gpu":
                settings = solver.cmd("show_settings")
                solver.cpu_algorithm = next((line.split(":", 1)[1].strip() for line in settings.splitlines()
                                             if line.startswith("algorithm:")), "auto")
            if device != solver.device:
                solver.cmd("set_algorithm " + solver.device_algorithm(device))
            with solver.state_lock:
                solver.device = device
        return 200, {"device": device}
    if path == "/api/status":
        return 200, solver.state()
    if path == "/api/hand_order":
        return 200, {"order": solver.cmd("show_hand_order").split()}

    # ---- saved ranges and tree configs (#116); state-free, no solver needed ----
    if path == "/api/library/list":
        if lib_query(query, "kind") == "sim" and _jobs is not None:
            lib.include_job_simulations(_jobs.saved_simulations())
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
            context = solver.drill_context
            if context and (node_id == context["root"] or node_id.startswith(context["root"] + ":")):
                depth = len(context["root"].split(":")) - 2
                return 200, {"nodes": nodes[depth:], "eff_stack": stack, "history": context["path"]}
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

    if path == "/api/blockers":
        return 200, blockers_at(solver, node_id_arg(query))

    if path == "/api/graphs":
        return 200, graphs_at(solver, node_id_arg(query))

    if path == "/api/hand_lines":
        # Beta, runout lines: how one hand of `player` plays every runout from this node on
        player = query.get("player", ["OOP"])[0].upper()
        if player not in ("OOP", "IP"):
            raise SolverError("player must be OOP or IP")
        return 200, hand_lines.hand_lines_at(sys.modules[__name__], solver, node_id_arg(query), player,
                                             query.get("hand", [""])[0], query.get("sizes", ["0"])[0] == "1")

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
            solver.note_iters(st["iters"])
        return 200, st

    if path == "/api/estimate":
        # the configured (not yet built) postflop tree: estimate_tree needs a built one, as in Pio
        return 200, {"text": solver.cmd(tree_build_command("estimate_schematic_tree", (solver.built_sizes or {}).get("smart_tree")))}

    if path == "/api/calc_results":
        return 200, {"text": solver.cmd("calc_results")}

    if path.startswith("/api/preflop/"):
        # a heads-up preflop tree from the Build tab (#492): defaults, the tree in words, memory, build
        return preflop_api.api(sys.modules[__name__], solver, method, path, body)

    # ---- state-changing endpoints (POST only) ----
    require_post(method)

    if path == "/api/library/save":
        b = need_body(body)
        if b.get("kind") == "sim":
            mode = need_body(b.get("content", {})).get("save_mode", "full")
            if mode not in ("full", "no_rivers", "no_turns"):
                raise SolverError("bad save mode")
            with solver.cmd_lock:
                metadata = simulation_metadata(solver)
                def write_tree(tmp):
                    metadata["save_mode"] = solver.cmd(f'dump_tree "{safe_path(tmp)}" {mode} report_mode', timeout=7200)
                name = lib.save_simulation(b.get("name"), write_tree, metadata, b.get("overwrite") is True)
        else:
            name = lib.save(b.get("kind"), b.get("name"), b.get("content"))
        return 200, {"name": name}
    if path == "/api/library/open":
        name = need_body(body).get("name")
        source = lib.simulation_path(name)
        job = lib.job_simulation(name)
        if job:
            with solver.cmd_lock:
                api(solver, lib, "POST", "/api/load", {}, {"path": source})
                lib.update_simulation_metadata(name, job["metadata"] | simulation_metadata(solver))
            return 200, {"ok": True}
        snapshot, target = lib.simulation_snapshot(name)
        try:
            with solver.cmd_lock:
                api(solver, lib, "POST", "/api/load", {}, {"path": target})
                solver.browse_snapshot = snapshot
                solver.loaded_path = source
                lib.update_simulation_metadata(name, lib.simulation_metadata(source) | simulation_metadata(solver))
        except Exception:
            if solver.browse_snapshot is not snapshot:
                snapshot.cleanup()
            raise
        return 200, {"ok": True}
    if path == "/api/library/import":
        b = need_body(body)
        source = os.path.abspath(os.path.expanduser(safe_path(b.get("path", ""))))
        name = lib.save_simulation(b.get("name"), lambda tmp: shutil.copyfile(source, tmp), {})
        return 200, {"name": name}
    if path == "/api/library/export":
        b = need_body(body)
        lib.export_simulation(b.get("name"), safe_path(b.get("path", "")))
        return 200, {"ok": True}
    if path == "/api/library/rename":
        b = need_body(body)
        if b.get("kind") == "sim" and solver is not None:
            with solver.cmd_lock:
                source = lib.simulation_path(b.get("from"))
                lib.rename("sim", b.get("from"), b.get("to"))
                if solver.loaded_path == source:
                    solver.loaded_path = lib.simulation_path(b.get("to"))
        else:
            lib.rename(b.get("kind"), b.get("from"), b.get("to"))
        return 200, {"name": b.get("to")}
    if path == "/api/library/delete":
        b = need_body(body)
        if b.get("kind") == "sim" and solver is not None:
            with solver.cmd_lock:
                source = lib.simulation_path(b.get("name"))
                lib.delete("sim", b.get("name"))
                if solver.loaded_path == source:
                    solver.loaded_path = None
        else:
            lib.delete(b.get("kind"), b.get("name"))
        return 200, {"ok": True}

    if path == "/api/generate":
        b = need_body(body)
        out = op_generate(solver, b)
        return 200, out

    if path == "/api/pio_import":
        return 200, op_pio_import(need_body(body))

    if path == "/api/build":
        if _resolver is not None:
            _resolver.main_tree_changed()  # #357: its subgame came from the old tree
        solver.cmd(tree_build_command("build_tree", (solver.built_sizes or {}).get("smart_tree")))
        solver.forget_locks()
        solver.tree_present = True
        solver.loaded_file = False
        solver.preflop = None
        solver.reset_series()
        solver.clear_partial()
        return 200, {"node_count": solver.cmd("node_count")}

    if path == "/api/solve":
        b = need_body(body)
        acc = b.get("accuracy", 0.05)
        if isinstance(acc, bool) or not isinstance(acc, (int, float)) or not math.isfinite(acc) or acc < 0:
            raise SolverError("bad accuracy")
        if solver.state()["solving"]:
            # the solver ignores a second go, but clearing the partial state would file a running
            # Solve further's progress under the main solve (#357)
            raise SolverError("the solver is running (stop it first)")
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
        solver.cmd(f'dump_tree "{path_arg}"{mode}', timeout=7200)  # big trees: minutes (#382)
        return 200, {"ok": True}

    if path == "/api/load":
        b = need_body(body)
        path_arg = safe_path(b.get("path", ""))
        solver.cmds_own(["stop", "wait_for_solver"], [f'load_tree "{path_arg}"'], timeout=7200)  # (#382)
        if _resolver is not None:
            _resolver.main_tree_changed()  # #357
        solver.forget_locks()
        solver.tree_present = True
        solver.loaded_file = True
        solver.loaded_path = path_arg
        solver.built_sizes = None
        solver.preflop = None
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

    if path == "/api/best_response":
        player = need_body(body).get("player")
        if player not in ("OOP", "IP"):
            raise SolverError("choose OOP or IP")
        with solver.cmd_lock:
            if solver.state()["solving"]:
                raise SolverError("the solver is running (stop it first)")
            solver.cmd("set_mes " + player, timeout=3600)
            solver.reset_series()
            solver.clear_partial()
        return 200, {"ok": True}

    if path == "/api/restart":
        solver.restart()
        return 200, {"ok": True}

    return 404, {"error": "unknown endpoint"}


def open_drill_snapshot(solver, path, context=None):
    snapshot = tempfile.TemporaryDirectory(prefix="wsolver-browse-")
    target = os.path.join(snapshot.name, "spot.cfr")
    try:
        shutil.copyfile(path, target)
        with solver.cmd_lock:
            api(solver, None, "POST", "/api/load", {}, {"path": target})
            solver.browse_snapshot = snapshot
            solver.drill_context = context
    except Exception:
        snapshot.cleanup()
        raise


def open_drill_variant(solver, path, locks, variant):
    with solver.cmd_lock:
        api(solver, None, "POST", "/api/load", {}, {"path": path})
        solver.cmd("load_all_nodes", timeout=3600)
        solver.loaded_file = False
        solver.loaded_path = None
        solver.locks = locks
        solver.drill_variant = variant


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
    is the one to act), a matchups-weighted EV (the same average as the
    range EVs in the study top bar), and the matrix classes its combos live in.
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
                "ev_num": 0.0, "ev_den": 0.0, "classes": set(), "indices": []}

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
            # calc_ev's matchups already carry the hand's own reach (reach * compatible opponent
            # mass), so they are the whole weight: multiplying by reach again would count it twice
            mu = mus[i] if mus else w
            if evs[i] is not None and mu and mu > 0:
                acc["ev_num"] += mu * evs[i]
                acc["ev_den"] += mu
            acc["classes"].add(cls)
            acc["indices"].append(i)

    total = sum(w for w in reach if w)

    def row(key, names):
        acc = accs[key]
        combos, cid = acc["combos"], acc["id"]
        r = {"id": cid, "name": names[cid] if cid < len(names) else str(cid),
             "combos": round(combos, 4), "pct": combos / total if total else 0,
             "strategy": [round(x / combos, 6) if combos else 0 for x in acc["strat"]]
                         if acc["strat"] is not None else None,
             "ev": round(acc["ev_num"] / acc["ev_den"], 4) if acc["ev_den"] > 0 else None,
             "matchups": round(acc["ev_den"], 6),  # the EV's weight: sum of reach * opponent mass
             "classes": sorted(acc["classes"], key=class_sort_key), "indices": acc["indices"]}
        return r

    made = [row(k, made_names) for k in sorted(accs) if k[0] == "m" and accs[k]["combos"] > 0]
    draws = [row(k, draw_names) for k in sorted(accs) if k[0] == "d" and accs[k]["combos"] > 0]
    made.reverse()   # strongest made hands first; best draws first
    draws.reverse()
    return {"id": nid, "board": board, "actor": actor, "player": player,
            "total": round(total, 4),
            "made_names": made_names, "draw_names": draw_names,
            "made": made, "draws": draws}


def graphs_at(solver, node_id):
    """Per-combo equity and EV of both players at any node, for the Graphs tab's distributions.

    For each player: reach (show_range), equity against the other range and its matchups
    (calc_eq_node), EV and its matchups (calc_ev). The page sorts the combos by equity or EV and
    plots them against the share of the range.
    """
    with solver.cmd_lock:
        # a fold or showdown has no EVs to ask for (calc_ev: "incorrect path"): use the node above it
        toks = node_id.split(":")
        node = parse_node_block(solver.cmd("show_node " + node_id))
        while node["type"] == "END_NODE" and len(toks) > 2:
            toks.pop()
            node = parse_node_block(solver.cmd("show_node " + ":".join(toks)))
        node_id = ":".join(toks)
        out = {"id": node_id}
        for pl in ("OOP", "IP"):
            eq = solver.cmd(f"calc_eq_node {pl} {node_id}").split("\n")
            ev = solver.cmd(f"calc_ev {pl} {node_id}").split("\n")
            out[pl.lower()] = {
                "reach": parse_floats(solver.cmd(f"show_range {pl} {node_id}"), NUM_COMBOS),
                "eq": parse_floats(eq[0], NUM_COMBOS),
                "eq_matchups": parse_floats(eq[1], NUM_COMBOS) if len(eq) > 1 and eq[1].strip() else None,
                "ev": parse_floats(ev[0], NUM_COMBOS),
                "ev_matchups": parse_floats(ev[1], NUM_COMBOS) if len(ev) > 1 and ev[1].strip() else None,
            }
    out["node"] = node
    return out


def blockers_at(solver, node_id):
    """What the actor's cards remove from the other player's range at a decision (the Blockers tab).

    Everything the tab needs for any combo the user picks, so picking one costs no further call:
    both reaches, each villain combo's equity against the actor's range (calc_eq_node), the
    actor's equity per combo, villain's made-hand and draw categories on this board, and for each
    actor action that hands the move to villain, villain's strategy there. Villain's reach at such
    a child is the same as here (only the actor acted), so the tab weighs those strategies with
    `villain_reach`. The combinatorics (which villain combos a hand blocks) are done in the page.
    """
    with solver.cmd_lock:
        node = parse_node_block(solver.cmd("show_node " + node_id))
        if node["type"] not in ("OOP_DEC", "IP_DEC"):
            raise SolverError("blockers need a decision node")
        actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
        villain = "IP" if actor == "OOP" else "OOP"
        board = "".join(node["board"].split())
        hero_reach = parse_floats(solver.cmd(f"show_range {actor} {node_id}"), NUM_COMBOS)
        villain_reach = parse_floats(solver.cmd(f"show_range {villain} {node_id}"), NUM_COMBOS)
        # equity per villain combo and its matchups (reach x compatible actor mass): the range
        # equity is matchup-weighted, as Pio's and the rest of the viewer's range figures are
        veq = solver.cmd(f"calc_eq_node {villain} {node_id}").split("\n")
        villain_eq = parse_floats(veq[0], NUM_COMBOS)
        villain_mu = parse_floats(veq[1], NUM_COMBOS) if len(veq) > 1 and veq[1].strip() else None
        hero_eq = parse_floats(solver.cmd(f"calc_eq_node {actor} {node_id}").split("\n")[0], NUM_COMBOS)
        vdec = "OOP_DEC" if villain == "OOP" else "IP_DEC"
        responses = []
        for kid in parse_children(solver.cmd("show_children " + node_id)):
            r = {"id": kid["id"], "node": kid, "children": None, "freq": None}
            if kid["type"] == vdec:
                rows = [l for l in solver.cmd("show_strategy " + kid["id"]).split("\n") if l.strip()]
                if rows and all(len(x.split()) == NUM_COMBOS for x in rows):  # not a preflop raw layout
                    r["children"] = parse_children(solver.cmd("show_children " + kid["id"]))
                    r["freq"] = [parse_floats(x, NUM_COMBOS) for x in rows]
            responses.append(r)
        cats = None
        if len(board) >= 6:
            names = solver.cmd("show_category_names").split("\n")
            lines = solver.cmd("show_categories " + board).split("\n")
            made, draw = [int(t) for t in lines[0].split()], [int(t) for t in lines[1].split()]
            if len(made) != NUM_COMBOS or len(draw) != NUM_COMBOS:
                raise SolverError("unexpected show_categories output")
            cats = {"made_names": names[0].split(), "draw_names": names[1].split(), "made": made, "draw": draw}
    return {"id": node_id, "node": node, "actor": actor, "villain": villain, "board": board,
            "hero_reach": hero_reach, "villain_reach": villain_reach, "hero_eq": hero_eq,
            "villain_eq": villain_eq, "villain_matchups": villain_mu, "responses": responses, "cats": cats}


def require_post(method):
    if method != "POST":
        raise SolverError("POST required")


def need_body(body):
    if not isinstance(body, dict):
        raise SolverError("JSON body required")
    return body


def safe_path(p):
    if not isinstance(p, str) or not p or '"' in p or "\n" in p or "\r" in p or "\x00" in p:
        raise SolverError("bad path")
    return p


def saved_files(path):
    path = safe_path(path or "~")
    target = os.path.abspath(os.path.expanduser(path))
    selected = target if os.path.isfile(target) else None
    directory = os.path.dirname(target) if selected else target
    entries = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if entry.name.startswith("."):
                    continue
                try:
                    folder = entry.is_dir()
                    if not folder and (not entry.name.lower().endswith(".cfr") or not entry.is_file()):
                        continue
                    stat = entry.stat()
                except FileNotFoundError:
                    continue
                entries.append({"name": entry.name, "path": entry.path, "folder": folder,
                                "bytes": None if folder else stat.st_size, "mtime": stat.st_mtime})
    except OSError as error:
        raise SolverError(str(error)) from None
    entries.sort(key=lambda entry: (not entry["folder"], entry["name"].casefold()))
    home = os.path.expanduser("~")
    places = [{"name": name, "path": location} for name, location in
              [("Home", home)] + [(name, os.path.join(home, name)) for name in ("Desktop", "Documents", "Downloads")]
              if os.path.isdir(location)]
    parent = os.path.dirname(directory)
    return {"path": directory, "parent": parent if parent != directory else None,
            "selected": selected, "entries": entries, "places": places}


def simulation_metadata(solver):
    node = parse_node_block(solver.cmd("show_node r:0"))
    return {"board": "".join(node["board"].split()), "pot": sum(node["pot"]),
            "stack": int(solver.cmd("show_effective_stack").strip()),
            "big_blind": 100 if node["board"] else max(node["pot"][:2])}


def no_newlines(value, name):
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise SolverError(f"bad {name}")
    return value


# The Build form's size grammar (viewer/static/sizes.js SIZE_RE / RAISE_RE): the server accepts exactly
# what the form does (#354), so a request can't build a size the form would have refused, nor the reverse.
_NUM = r"(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)"
SIZE_RE = re.compile(rf"(?:{_NUM}|(?:[1-9]\d*)?e{_NUM}?|a)", re.I)
RAISE_RE = re.compile(rf"(?:{_NUM}|(?:[1-9]\d*)?e{_NUM}?|[1-9]\d*(?:\.\d+)?x|a)", re.I)


def _check_sizes(tb, b, key, allow_mult=True):
    """A size list in tools/tree_builder.py's grammar (#114, #315): percentages, geometric Ne / NeP,
    raise multiples Nx (raise fields only), a; entries separated by commas, spaces or both, as
    PioViewer writes them. Returns the normalized comma-separated list."""
    val = no_newlines(str(b.get(key, "") if b.get(key) is not None else ""), key).strip()
    if not val:
        return ""
    parts = [p for p in re.split(r"[,\s]+", val) if p]
    form_re = RAISE_RE if allow_mult else SIZE_RE
    if any(not form_re.fullmatch(p) for p in parts):
        raise SolverError(f"bad sizes in {key} "
                          "(want percentages, geometric Ne, raise multiples Nx, or a)")
    try:
        specs = tb.parse_size_list(val)
    except ValueError:
        raise SolverError(f"bad sizes in {key} "
                          "(want percentages, geometric Ne, raise multiples Nx, or a)")
    if len(specs) != len(parts):
        raise SolverError(f"bad sizes in {key}")
    for part, spec in zip(parts, specs):
        if spec.kind == "mult":
            if not allow_mult or spec.value > 1000:
                raise SolverError(f"bad sizes in {key} (Nx is for raise fields, N <= 1000)")
        elif spec.kind == "geo":
            if spec.value is not None and spec.value > 99:
                raise SolverError(f"bad sizes in {key} (Ne takes N <= 99)")
            if spec.param is not None and (spec.param <= 0 or spec.param > 100000):
                raise SolverError(f"bad sizes in {key} (NeP takes 0 < P <= 100000)")
        elif spec.kind == "allin" and part.lower() not in ("a", "allin"):
            raise SolverError(f"bad sizes in {key}")  # tree_builder reads `2e.a` as a; the form doesn't
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
    if not (0 < allin_threshold <= 1e9) or not (0 <= add_allin_limit <= 1e9):
        raise SolverError("bad all-in numbers (threshold above 0, limit 0 or more)")
    # repr keeps every digit (:g rounded to 6 figures, #354)
    header = [f"#AllinThreshold#{allin_threshold!r}",
              f"#AddAllinOnlyIfLessThanThisTimesThePot#{add_allin_limit!r}"]
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
            "undo": bool(solver.undo), "can_solve": not solver.loaded_file,
            "can_snapshot": solver.undo is None and not solver.locks and not solver.loaded_file}


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
    shown = b.get("actions")
    if mode != "current" and shown is not None and list(shown) != _actions(kids):
        # rows for other actions: e.g. a refined street (fast preview) built from other sizes
        # than this tree's, with the same number of actions (#261)
        raise SolverError(f"the strategy shown is for actions {' '.join(map(str, shown))}, "
                          f"this node has {' '.join(_actions(kids))}")
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
            if (not isinstance(mask, list) or len(mask) != NUM_COMBOS
                    or any(m not in (0, 1) or isinstance(m, float) and m != int(m) for m in mask)):
                raise SolverError(f"combos must be {NUM_COMBOS} 0/1 values")
            if not any(mask):
                raise SolverError("no combos selected to lock")
            # combo_lock_node replaces the node's mask: keep the combos locked earlier (their
            # strategies are in `freq` as shown, i.e. as locked), so locking more hands adds (#357)
            old = parse_floats(solver.cmd("show_locked_combos " + node_id), NUM_COMBOS)
            mask = [1 if m or (o or 0) > 0 else 0 for m, o in zip(mask, old)]
            cmds.append(f"combo_lock_node {node_id} " + " ".join("1" if m else "0" for m in mask))
        else:
            cmds.append(f"lock_node {node_id}")
    else:
        cmds.append(f"lock_node {node_id}")
    with solver.cmd_lock:
        # the copy is of the *unlocked* solve: only before the first lock (a later lock with the
        # box ticked would otherwise keep a tree that already has the earlier locks in it)
        if b.get("snapshot") and solver.undo is None and not solver.locks and not solver.loaded_file:
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
            # Every lock set from this session.
            for i in list(solver.locks):
                solver.cmds([f"unlock_node {i}", f"combo_lock_node {i} {zeros}"])
            # plus locks that came with a loaded save, which are not in the list: unlock_all
            # clears node locks (as Pio, combo locks stay), combo_unlock_all the combo locks (#255)
            for c in ("unlock_all", "combo_unlock_all"):
                try:
                    solver.cmd(c)
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
        # used up: the tree *is* the copy now. Keeping it would make a later lock skip its own copy,
        # and a later revert throw away everything solved after this one.
        solver.forget_locks()
        solver.tree_present = True
        solver.loaded_file = False
        solver.reset_series()
        solver.clear_partial()
    return {"ok": True}


def tree_config_from_body(b):
    board = no_newlines(str(b.get("board", "")).replace(" ", "").strip(), "board")
    def whole(v):   # int() would truncate 10.5 to 10, building another pot than the form shows
        if isinstance(v, bool):
            raise ValueError
        f = float(v)
        if not f.is_integer():
            raise ValueError
        return int(f)

    try:
        pot = whole(b["pot"])
        stack = whole(b["stack"])
        allin_threshold = float(b.get("allin_threshold", 67))
        add_allin_limit = float(b.get("add_allin_limit", 300))
        if pot < 0 or stack <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):   # float() of a 400-digit int overflows
        raise SolverError("bad pot/stack/all-in numbers (pot and stack are whole chips)")

    tb = load_tree_builder()
    header = [f"#Pot#{pot}", f"#EffectiveStacks#{stack}"]
    if board:
        header.append(f"#Board#{board}")
    header += sizing_header(b, tb)
    header += pio_extra_lines(b)

    try:
        return tb.parse_config("\n".join(header))
    except (ValueError, ArithmeticError) as e:
        raise SolverError(f"bad sizes: {e}")


def tree_build_command(command, smart_tree):
    return command + (" smart_rivers" if smart_tree is True else "")


def setup_from_body(b):
    """The Build form (ranges, pot, stack, sizes, optional board) -> (UPI setup commands, line count).

    Shared by /api/generate and the job queue (#196), which runs the same setup on many flops: the
    board-free form has no set_board and expands the lines from the flop."""
    range_oop = no_newlines(str(b.get("range_oop", "")).strip(), "range_oop")
    range_ip = no_newlines(str(b.get("range_ip", "")).strip(), "range_ip")
    if not range_oop or not range_ip:
        raise SolverError("both ranges are required")
    tb = load_tree_builder()
    cfg = tree_config_from_body(b)
    board = cfg.board or ""

    try:
        # A 4- or 5-card board starts the tree on the turn or river: the builder must start
        # there too, or its lines run past the end of the hand.
        n_cards = len(board) // 2
        start = (tb.Node(n_cards - 3, (0, 0), tb.OOP, False, None, removed=tuple(cfg.removed_lines),
                         extra=tuple(cfg.extra_lines)) if n_cards in (4, 5) else None)  # hand edits too (#354)
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
    cmds = ["set_range OOP " + range_oop,
            "set_range IP " + range_ip]
    if board:
        cmds.append("set_board " + board)
    cmds += [f"set_eff_stack {cfg.stack}", f"set_pot 0 0 {cfg.pot}",
             "set_isomorphism 1 1", "clear_lines"]
    cmds += lines
    return cmds, lines


# These PioViewer settings travel with the form as raw tree builder header lines (#317).
PIO_EXTRA_KEYS = ("MergeSimilarBets", "MergeSimilarBetsThreshold", "CapEnabled", "CapPerStreet",
                  "ForceOOPBet", "ForceIPBet", "UseUnifiedRaiseAfterRaise", "UnifiedRaiseAfterRaise",
                  "RemovedLines_", "ExtraLines_")


def pio_extra_lines(b):
    """The form's `pio_extra` header lines, checked: each a `#Key#Value` line of a PIO_EXTRA_KEYS key."""
    extra = b.get("pio_extra") or []
    if not isinstance(extra, list) or len(extra) > 64:
        raise SolverError("bad pio_extra")
    out = []
    for line in extra:
        line = no_newlines(str(line), "pio_extra").strip()
        if len(line.splitlines()) > 1:  # \u2028, \x0b, \x85 ... also split lines in parse_config (#354)
            raise SolverError("bad pio_extra")
        m = re.fullmatch(r"#([A-Za-z_0-9]+)#.*", line)
        if not m or not any(m.group(1) == k or (k.endswith("_") and m.group(1).startswith(k))
                            for k in PIO_EXTRA_KEYS):
            raise SolverError(f"bad pio_extra line: {line[:60]}")
        if len(line) > 200000:
            raise SolverError("pio_extra line too long")
        out.append(line)
    return out


def op_pio_import(b):
    """POST /api/pio_import (#317): a PioViewer tree file -> Build form values.

    Bet merging and the other PIO_EXTRA_KEYS travel as `pio_extra`. `matches` says whether the builder reproduces the file's own
    add_lines (it does for all 145 configs of #313)."""
    text = b.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SolverError("empty file")
    if len(text) > 5_000_000:
        raise SolverError("file too large")
    tb = load_tree_builder()
    try:
        cfg = tb.parse_config(text)
    except (ValueError, ArithmeticError) as e:
        raise SolverError(f"not a PioViewer tree file: {e}")
    if cfg.pot <= 0 and cfg.stack <= 0:
        raise SolverError("not a PioViewer tree file (no #Pot# / #EffectiveStacks# header)")
    def size_list(specs):
        def one(s):
            if s.kind == "allin":
                return "a"
            if s.kind == "pct":  # the form's spelling of a number (#354)
                v = s.value.normalize()
                return format(v, "f") if v == v.to_integral() or "." in format(v, "f") else str(v)
            return s.text
        return ",".join(one(s) for s in specs)
    bd = (cfg.board or "").replace(" ", "")
    cards = [bd[i:i + 2] for i in range(0, len(bd), 2)]
    if len(bd) % 2 or len(cards) > 5 or len(set(cards)) != len(cards) or not all(card_token(c) for c in cards):
        bd = ""  # not a board: dropped, never echoed into the form (#384)
    form = {"pot": cfg.pot, "stack": cfg.stack, "board": bd,
            "allin_threshold": float(cfg.allin_threshold),
            "add_allin_limit": float(cfg.add_allin_max_pot_mult) if cfg.add_allin_max_pot_mult.is_finite() else 100000}
    ranges = {"range_oop": cfg.ranges.get("Range0") or cfg.raw_set_range.get("OOP", ""),
              "range_ip": cfg.ranges.get("Range1") or cfg.raw_set_range.get("IP", "")}
    form.update({k: v.strip() for k, v in ranges.items() if v.strip()})
    for i, street in enumerate(("flop", "turn", "river")):
        oop, ip = cfg.streets[i]
        form[f"{street}_bet"], form[f"{street}_raise"] = size_list(oop.bet), size_list(oop.raise_)
        form[f"{street}_donk"], form[f"{street}_allin"] = size_list(oop.donk), oop.add_allin
        form[f"{street}_bet_ip"], form[f"{street}_raise_ip"] = size_list(ip.bet), size_list(ip.raise_)
        form[f"{street}_allin_ip"] = ip.add_allin
    extra = []
    for raw in text.splitlines():
        m = re.fullmatch(r"#([A-Za-z_0-9]+)#.*", raw.strip())
        if m and any(m.group(1) == k or (k.endswith("_") and m.group(1).startswith(k)) for k in PIO_EXTRA_KEYS):
            extra.append(raw.strip())
    form["pio_extra"] = extra
    want = {tuple(l) for l in tb.extract_lines(text)}
    try:
        cmds, lines = setup_from_body(dict(form, range_oop=form.get("range_oop") or "AA",
                                           range_ip=form.get("range_ip") or "AA"))
        got = {tuple(int(x) for x in l.split()[1:]) for l in lines}
    except SolverError as e:
        raise SolverError(f"could not build this config: {e}")
    return {"form": form, "lines": len(got), "file_lines": len(want),
            "matches": bool(want) and got == want}


def op_generate(solver, b):
    """PioViewer-style bet sizes -> add_lines via tools/tree_builder.py, then configure the solver."""
    cmds, lines = setup_from_body(b)
    solver.cmds_own(["stop", "wait_for_solver"], cmds)
    solver.preflop = None
    solver.forget_locks()
    solver.tree_present = False
    solver.loaded_file = False
    solver.built_sizes = {k: v for k, v in b.items() if k not in ("range_oop", "range_ip")}
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
        # always revalidate: after an update the browser must not keep running the old page's scripts
        self.send_header("Cache-Control", "no-cache")
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

    MAX_BODY = 64 << 20  # far above any real request (a PioViewer import or a lock is a few MB at most)

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
            # a garbage length used to raise here (no answer at all), a negative one read until the client
            # closed, and any size was read whole (#394)
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._send(400, {"ok": False, "error": "bad Content-Length"})
                return
            if length > self.MAX_BODY:
                self.close_connection = True  # the unread body must not be parsed as the next request
                self._send(413, {"ok": False, "error": f"request body over {self.MAX_BODY >> 20} MB"})
                return
            raw = self.rfile.read(length)
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
    ap.add_argument("--jobs", default=os.environ.get("WSOLVER_JOBS", os.path.join("~", ".wsolver", "jobs")),
                    help="solve queue directory, shared with tools/jobs.py (#196)")
    ap.add_argument("--flops-dir", default=None,
                    help="folder of flop files for the Jobs view (default: a PioSOLVER install's "
                         "preflop_subsets, or $WSOLVER_FLOPS_DIR)")
    ap.add_argument("--exit-with-parent", nargs="?", type=int, const=0, default=None, metavar="PID",
                    help="stop when the process that started this one is gone (the macOS app's launcher, #479); "
                         "with its PID, also when it was gone before this one started")
    args = ap.parse_args(argv)
    # Read before anything slow (the solver starts below): a parent that goes during start-up must
    # not be missed. Without the PID, one that went before this line can only be told on a system
    # that hands orphans to process 1.
    parent = args.exit_with_parent or os.getppid()

    solver = Solver(os.path.abspath(args.solver))
    import resolve as vresolve  # local import: server/main <-> resolve module cycle
    set_resolver(vresolve.ResolveManager(solver, Solver, os.path.abspath(args.solver)))
    import nl_lock
    set_nl(nl_lock.NLService())   # reads the Sail key, if any; it never leaves this process
    import jobs_api
    set_jobs(jobs_api.JobsService(args.jobs, os.path.abspath(args.solver),
                                  args.flops_dir if args.flops_dir is not None else jobs_api.default_flops_dir()))
    import drill
    set_drill(drill.DrillService(solver, os.path.abspath(args.solver), _jobs.q))
    import refine as vrefine
    set_refiner(vrefine.RefineManager(solver, Solver, os.path.abspath(args.solver)))
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    srv.solver = solver
    srv.library = Library(args.library)
    srv.library.install_defaults()
    print(f"WSolver viewer: solver {solver.exe!r}, library {srv.library.root!r}, "
          f"UI on http://127.0.0.1:{srv.server_port}", flush=True)
    if args.exit_with_parent is not None:
        # Started by a launcher that owns this viewer: if it dies without stopping us (a crash, a
        # force quit), nobody else will, and the port stays taken and the solvers keep running. A
        # parent that is gone shows as a new parent pid (the process is adopted).
        def watch():
            while os.getppid() == parent and (parent != 1 or args.exit_with_parent):
                time.sleep(1)
            if os.getpgrp() == os.getpid():
                # In a process group of our own, as the launcher starts us: do what its Quit does,
                # an interrupt to the whole group. That reaches the solver of a queue job too,
                # which the shutdown below does not know about.
                os.killpg(os.getpid(), signal.SIGINT)
            else:
                srv.shutdown()
            time.sleep(10)   # a thread that will not end must not keep the process
            os._exit(0)
    try:
        if args.exit_with_parent is not None:
            # started in here: its interrupt can come at once, and must find the clean-up below in place
            threading.Thread(target=watch, daemon=True).start()
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            if _drill:
                _drill.close()
        finally:
            for s in ([solver] + ([_resolver.rsolver] if _resolver else []) + ([_refiner.rsolver] if _refiner else [])):
                try:
                    s.proc.kill()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
