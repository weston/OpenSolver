#!/usr/bin/env python3
"""The viewer's Jobs view (#196): HTTP endpoints over the solve queue in tools/jobs.py.

The queue is the same one `python3 tools/jobs.py` uses (default ~/.wsolver/jobs), so batches
queued from the command line show up here and vice versa. Jobs run one at a time on a background
thread, each in its own solver process: the viewer's own solver stays free for browsing.

Endpoints (GET unless marked POST):
    /api/jobs/state                     queue, batches, runner state, current job progress, log
    /api/jobs/flop_files                flop files in the --flops-dir folder (name, board count)
    /api/jobs/report?batch=NAME[&line=L][&hands=1] per flop at the root or a line (#325), per hand (#338)
    /api/jobs/add          POST         {name, tree: <Build form body>, flops | flops_file,
                                         accuracy (% pot), max_iters?, save?}
    /api/jobs/run          POST         {ids?: [...], batch?}  start the runner
    /api/jobs/stop         POST         {now?: bool}
    /api/jobs/retry        POST         {ids: [...]} | {failed: true}
    /api/jobs/remove       POST         {ids: [...]} | {batch}
    /api/jobs/move         POST         {id, to}
    /api/jobs/open         POST         {id}  load the job's save into the viewer's solver
"""

import collections
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import jobs  # noqa: E402  tools/jobs.py

import server as vsrv  # noqa: E402

MAX_FLOP_FILE = 1 << 20


def default_flops_dir():
    """Where a PioSOLVER install keeps its flop subsets (the user's own copy; none ship here)."""
    env = os.environ.get("WSOLVER_FLOPS_DIR")
    if env:
        return env
    for p in (r"C:\PioSOLVER\preflop_subsets", "/mnt/c/PioSOLVER/preflop_subsets"):
        if os.path.isdir(p):
            return p
    return ""


class JobsService:
    def __init__(self, queue_dir, solver_exe, flops_dir=""):
        self.q = jobs.Queue(queue_dir)
        self.solver_exe = solver_exe
        self.flops_dir = os.path.abspath(os.path.expanduser(flops_dir)) if flops_dir else ""
        self.mu = threading.Lock()
        self.thread = None
        self.current = None    # id of the running job
        self.point = None      # its latest progress point
        self.log = collections.deque(maxlen=200)
        self.last_error = None
        # line reports reading saves in the background (#333): (batch, line key) -> task
        self.reports = {}

    # ---- runner thread
    def _log(self, msg):
        with self.mu:
            self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _progress(self, job, point):
        with self.mu:
            self.current = job["id"]
            self.point = point

    def _run(self, ids, batch):
        try:
            counts = jobs.run(self.q, ids, batch, None, self.solver_exe, 0, self._log, self._progress)
            self._log(f"finished: {counts['done']} done, {counts['failed']} failed")
        except Exception as e:   # JobError, or anything unexpected: show it rather than lose it
            with self.mu:
                self.last_error = str(e)
            self._log(f"error: {e}")
        finally:
            # However the run ended, no job of this process is running any more: one still marked
            # running under our pid would otherwise stay stuck (recover() skips live pids, and
            # Retry / remove refuse a running job), so it goes back to the queue.
            me = os.getpid()
            try:
                for j in self.q.jobs():
                    if j["status"] == "running" and j.get("runner_pid") == me:
                        self.q.update(j["id"], expect=lambda x: x["status"] == "running" and x.get("runner_pid") == me,
                                      status="queued", started=None, runner_pid=None)
            except Exception as e:
                self._log(f"error: could not re-queue the interrupted job: {e}")
            with self.mu:
                self.current = None
                self.point = None

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    # ---- operations
    def state(self):
        # a runner started from the CLI (tools/jobs.py run) works on the same queue: show it as running
        external = 0 if self.running() else self.q.runner()
        running = self.running() or bool(external)
        js = self.q.jobs()
        if not running and any(j["status"] == "running" for j in js):
            # Jobs a dead runner left "running" (the viewer restarted or crashed mid-job, a CLI
            # runner was killed) go back to the queue now, as the next run would do: otherwise
            # the tab shows them running forever, with no Retry or remove on their row.
            self.q.recover()
            js = self.q.jobs()
        with self.mu:
            cur = self.current
            if running and cur is None:  # between progress reports: the job marked running
                cur = next((j["id"] for j in js if j["status"] == "running"), None)
            out = {"jobs": js, "running": running, "current": cur, "point": self.point,
                   "log": list(self.log)[-40:], "error": self.last_error, "external": external,
                   "stopping": os.path.exists(self.q.stop_file)}
        batches = collections.OrderedDict()
        for j in js:
            b = batches.setdefault(j["batch"], {"name": j["batch"], "total": 0, "queued": 0, "running": 0,
                                                "done": 0, "failed": 0})
            b["total"] += 1
            b[j["status"]] += 1
        for name, b in batches.items():
            try:
                cfg = self.q.batch(name)
                b.update(accuracy_pct=cfg["accuracy_pct"], save=cfg["save"], out_dir=cfg["out_dir"],
                         max_iters=cfg.get("max_iters"))
            except jobs.JobError:
                pass
        out["batches"] = list(batches.values())
        return out

    def flop_files(self):
        if not self.flops_dir or not os.path.isdir(self.flops_dir):
            return {"dir": self.flops_dir, "files": []}
        files = []
        for fn in os.listdir(self.flops_dir):
            p = os.path.join(self.flops_dir, fn)
            if not fn.lower().endswith(".txt") or not os.path.isfile(p) or os.path.getsize(p) > MAX_FLOP_FILE:
                continue
            try:
                with open(p, errors="replace") as f:
                    n = len(jobs.parse_flops(f.read()))
            except (OSError, jobs.JobError):
                continue
            files.append({"name": fn, "boards": n})
        files.sort(key=lambda f: (f["boards"], f["name"]))
        return {"dir": self.flops_dir, "files": files}

    def _flop_file_text(self, name):
        name = str(name)
        if not self.flops_dir or os.path.basename(name) != name or not name.lower().endswith(".txt"):
            raise vsrv.SolverError("pick a flop file from the list")
        p = os.path.join(self.flops_dir, name)
        if not os.path.isfile(p):
            raise vsrv.SolverError(f"no flop file {name!r}")
        with open(p, errors="replace") as f:
            return f.read()

    def add(self, b):
        name = vsrv.no_newlines(str(b.get("name", "")).strip(), "name")
        tree = b.get("tree")
        if not isinstance(tree, dict):
            raise vsrv.SolverError("tree: the Build form is required")
        tree = dict(tree, board="")  # the queue sets each job's board
        setup, _ = vsrv.setup_from_body(tree)
        text = self._flop_file_text(b["flops_file"]) if b.get("flops_file") else str(b.get("flops", ""))
        try:
            flops = jobs.parse_flops(text)
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))
        if any(len(board) != 6 for board, _ in flops):
            raise vsrv.SolverError("the Build form's sizes describe a flop tree: use 3-card boards")

        def num(key, kind, lo):
            v = b.get(key)
            if v in (None, ""):
                return None
            try:
                v = kind(v)
            except (TypeError, ValueError):
                raise vsrv.SolverError(f"bad {key}")
            if not v >= lo:
                raise vsrv.SolverError(f"bad {key}")
            return v
        accuracy = num("accuracy", float, 1e-6) or 0.5
        max_iters = num("max_iters", int, 1)
        save = str(b.get("save", "no_rivers"))
        try:
            est = jobs.check_tree(self.solver_exe, setup, flops[0][0])
            added = self.q.add_batch(name, setup, flops, accuracy, max_iters, None, save, None,
                                     source="viewer Build form")
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))
        return {"added": len(added), "estimate": est.splitlines()}

    def run(self, b):
        if self.running():
            raise vsrv.SolverError("the queue is already running")
        if self.q.runner():
            raise vsrv.SolverError(f"a runner started from the command line is active (pid {self.q.runner()})")
        ids = b.get("ids") or None
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
            raise vsrv.SolverError("bad ids")
        batch = b.get("batch") or None
        if not any(j["status"] == "queued" and (not ids or j["id"] in ids) and (not batch or j["batch"] == batch)
                   for j in self.q.jobs()):
            raise vsrv.SolverError("nothing queued to run")
        with self.mu:
            self.last_error = None
        self.thread = threading.Thread(target=self._run, args=(ids, batch), daemon=True)
        self.thread.start()
        return {"started": True}

    def stop(self, b):
        if not self.running() and not self.q.runner():  # ours, or one started from the CLI
            raise vsrv.SolverError("the queue is not running")
        os.makedirs(self.q.root, exist_ok=True)
        with open(self.q.stop_file, "w") as f:
            f.write("now" if b.get("now") else "after")
        return {"stopping": "now" if b.get("now") else "after the current job"}

    def _jobs_arg(self, b):
        ids = b.get("ids")
        if not isinstance(ids, list) or not ids:
            raise vsrv.SolverError("ids required")
        try:
            return [self.q.job(str(i)) for i in ids]
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))

    def retry(self, b):
        ids = None if b.get("failed") else [j["id"] for j in self._jobs_arg(b)]
        try:
            return {"requeued": self.q.retry(ids)}  # under the queue lock, as the job is now
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))

    def remove(self, b):
        js = self.q.jobs(str(b["batch"])) if b.get("batch") else self._jobs_arg(b)
        if not js:
            raise vsrv.SolverError("nothing to remove")
        try:
            self.q.remove(js)
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))
        return {"removed": len(js)}

    def move(self, b):
        try:
            self.q.move(str(b.get("id")), int(b.get("to")))
        except (TypeError, ValueError):
            raise vsrv.SolverError("bad position")
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))
        return {"moved": True}

    def open(self, solver, b):
        try:
            j = self.q.job(str(b.get("id")))
        except jobs.JobError as e:
            raise vsrv.SolverError(str(e))
        if j["status"] != "done" or not os.path.isfile(j["out"]):
            raise vsrv.SolverError("that job has no save yet")
        return vsrv.api(solver, None, "POST", "/api/load", {}, {"path": j["out"]})[1] | {"board": j["board"]}

    def line_report(self, name, line, root=False):
        """A report at a line (#325). If every save's result at the line is cached it is answered at
        once; otherwise the saves are read in a background task (one per batch and line) and this
        answers {"reading": true, "progress": {done, total, board}} until it has finished (#333). Then
        the report is built from the job cache, so it is never older than the queue."""
        key = ":".join(jobs.parse_line(line))
        tid = (name, key)
        with self.mu:
            t = self.reports.get(tid)
            if t is not None and t["finished"]:
                del self.reports[tid]
                if t["error"]:
                    raise vsrv.SolverError(t["error"])
            elif t is not None:
                return {"reading": True, "line": key, "progress": dict(t["progress"])}
        # just read in the background, or all cached: build it now (a save that failed to read isn't
        # cached and is tried again here, once, rather than starting another task)
        if t is not None or not jobs.line_uncached(self.q, name, line):
            return jobs.line_report(self.q, name, line, self.solver_exe, root=root)
        if not self.solver_exe:
            raise vsrv.SolverError("no solver to read the saves with")
        with self.mu:
            if tid in self.reports:  # started meanwhile by another request
                return {"reading": True, "line": key, "progress": dict(self.reports[tid]["progress"])}
            t = {"progress": {"done": 0, "total": None, "board": None}, "finished": False, "error": None}
            self.reports[tid] = t

        def prog(done, total, board):
            t["progress"] = {"done": done, "total": total, "board": board}

        def work():
            try:
                jobs.line_report(self.q, name, line, self.solver_exe, progress=prog, root=root)
            except Exception as e:  # noqa: BLE001 - shown to whoever asks next
                t["error"] = str(e) or type(e).__name__
            t["finished"] = True
        threading.Thread(target=work, name=f"report {name} {key}", daemon=True).start()
        return {"reading": True, "line": key, "progress": dict(t["progress"])}

    def loaded(self, solver):
        """The finished job whose save the viewer has open, if any: {id, batch, board} or {} (the Study
        tab's Report button, #331: an aggregate report at the node you are looking at)."""
        path = getattr(solver, "loaded_path", None) if getattr(solver, "loaded_file", False) else None
        if not path:
            return {}
        want = os.path.realpath(path)
        for j in self.q.jobs():
            if j["status"] == "done" and j.get("out") and os.path.realpath(j["out"]) == want:
                return {"id": j["id"], "batch": j["batch"], "board": j["board"]}
        return {}

    # ---- HTTP
    def api(self, solver, method, path, query, body):
        sub = path[len("/api/jobs"):]
        if sub == "/state":
            return 200, self.state()
        if sub == "/flop_files":
            return 200, self.flop_files()
        if sub == "/loaded":
            return 200, self.loaded(solver)
        if sub == "/report":
            name = query.get("batch", [""])[0]
            line = query.get("line", [""])[0]
            hands = query.get("hands", [""])[0] in ("1", "true")
            try:
                # a line, or the root with the per-hand figures (#338): both read the saves
                if jobs.parse_line(line) or hands:
                    return 200, self.line_report(name, line, root=True)
                return 200, jobs.report(self.q, name, self.solver_exe)
            except jobs.JobError as e:
                raise vsrv.SolverError(str(e))
        if method != "POST":
            raise vsrv.SolverError("POST required")
        b = vsrv.need_body(body)
        ops = {"/add": self.add, "/run": self.run, "/stop": self.stop, "/retry": self.retry,
               "/remove": self.remove, "/move": self.move}
        if sub in ops:
            return 200, ops[sub](b)
        if sub == "/open":
            return 200, self.open(solver, b)
        return 404, {"error": "unknown endpoint"}
