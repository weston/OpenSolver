#!/usr/bin/env python3
"""Job queue: solve one tree structure on many flops, one job at a time, and save each (#196).

A *batch* is one tree setup (ranges, pot, stack, lines, ...) plus a list of boards; it becomes one
*job* per board. Jobs wait in a queue on disk, so you can queue many, run them all later, run just
one, add more while a run is going, and pick up after a crash. Jobs run one at a time, each in a
fresh solver process with all threads.

Usage:
    python3 tools/jobs.py add NAME TREE (--flops-file FILE | --flops "Ks5h2s,Ts7h2s")
                              [--accuracy PCT] [--max-iters N | --max-seconds S]
                              [--save no_rivers|no_turns|full] [--out DIR]
    python3 tools/jobs.py list [--batch NAME]
    python3 tools/jobs.py run [ID ...] [--batch NAME] [--limit N] [--solver PATH] [--threads N]
    python3 tools/jobs.py stop [--now]       # after the current job (--now: stop it, re-queue it)
    python3 tools/jobs.py show ID
    python3 tools/jobs.py retry ID ... | --failed        # back to queued
    python3 tools/jobs.py remove ID ... | --batch NAME
    python3 tools/jobs.py move ID --to POS               # reorder (1 = next to run)
    python3 tools/jobs.py export NAME                    # results as CSV (board, weight, EVs, ...)

TREE is either a UPI script (set_range / set_pot / set_eff_stack / add_line ... as you would send
the solver) or a PioViewer-style config with `#Key#Value` sizing headers, which is expanded with
tools/tree_builder.py. Any set_board, build_tree, go, dump_tree, ... in it is dropped: the queue adds
its own. The setup is copied into the batch, so later edits to TREE do not change queued jobs.

Flop files: one board per line or comma-separated, each optionally followed by `:weight`
(`Ks5h2s:15.76`), e.g. a PioSOLVER `preflop_subsets/*.txt` file from your own install. The file is
read when you add the batch; nothing from it is stored except the boards and weights.

Defaults: accuracy 0.5% of the starting pot, saves `no_rivers` (later streets are re-solved when
browsed; calc_results keeps the solve's numbers, #195), to <queue>/saves/<batch>/<board>.cfr.
The queue lives in ~/.wsolver/jobs (override with --queue or WSOLVER_JOBS).

The solver must see the same file paths as this script (run it on the machine the solver runs on).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "viewer"))

STATUSES = ("queued", "running", "done", "failed")
SAVE_MODES = ("no_rivers", "no_turns", "full")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,99}$")
CARD = r"[2-9TJQKAtjqka][cdhsCDHS]"
BOARD_RE = re.compile(rf"((?:{CARD}\s*){{3,5}})(?::\s*([0-9]*\.?[0-9]+))?")
# Commands the queue issues itself; dropped from a batch's setup.
RUN_COMMANDS = {"set_board", "build_tree", "go", "wait_for_solver", "stop", "dump_tree", "load_tree",
                "exit", "set_end_string", "reset_end_string", "solve_partial", "solve_all_splits",
                "set_threads", "set_accuracy", "free_tree", "calc_results", "is_ready", "load_script",
                "load_script_silent", "stdoutredi", "stdoutredi_append", "stdoutback", "take_a_break"}


class JobError(Exception):
    """Bad input or queue state."""


# ------------------------------------------------------------------ input parsing

def normalize_board(text: str) -> str:
    cards = re.findall(CARD, text)
    if len(cards) < 3 or len(cards) > 5 or "".join(cards) != re.sub(r"\s+", "", text):
        raise JobError(f"bad board {text!r}")
    cards = [c[0].upper() + c[1].lower() for c in cards]
    if len(set(cards)) != len(cards):
        raise JobError(f"repeated card in board {text!r}")
    return "".join(cards)


def parse_flops(text: str) -> list[tuple[str, float]]:
    """Boards with optional weights: `Ks5h2s:15.76` per line, or a comma/space separated list."""
    out, seen = [], set()
    for raw in text.replace(",", "\n").replace(";", "\n").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pos = 0
        while pos < len(line):
            m = BOARD_RE.match(line, pos)
            if not m:
                raise JobError(f"cannot read a board in {raw.strip()!r}")
            board = normalize_board(m.group(1).strip())
            if board in seen:
                raise JobError(f"board {board} listed twice")
            seen.add(board)
            out.append((board, float(m.group(2)) if m.group(2) else 1.0))
            pos = m.end()
            while pos < len(line) and line[pos].isspace():
                pos += 1
    if not out:
        raise JobError("no boards given")
    return out


def tree_setup(text: str) -> tuple[list[str], list[str]]:
    """TREE file -> (setup commands, dropped commands). Expands PioViewer sizing configs."""
    lines = [l.strip() for l in text.splitlines()]
    is_config = any(re.match(r"^#[A-Za-z.]*(BetSize|Pot|EffectiveStacks)#", l) for l in lines)
    if is_config and not any(l.startswith("add_line ") for l in lines):
        import tree_builder
        cfg = tree_builder.parse_config(text)
        lines = tree_builder.format_script(cfg, tree_builder.build_lines(cfg))
        # A bare config carries its ranges only as #Range0# (OOP) / #Range1# (IP) headers.
        for who, key in (("OOP", "Range0"), ("IP", "Range1")):
            m = re.search(rf"^#{key}#(.+)$", text, re.M)
            if m and not any(l.startswith(f"set_range {who} ") for l in lines):
                lines.insert(0, f"set_range {who} {m.group(1).strip()}")
    setup, dropped = [], []
    for l in lines:
        l = l.strip()
        if not l or l.startswith("#"):
            continue
        cmd = l.split()[0]
        (dropped if cmd in RUN_COMMANDS else setup).append(l)
    if not any(l.startswith("add_line ") for l in setup):
        raise JobError("the tree has no add_line commands")
    return setup, dropped


# ------------------------------------------------------------------ the queue on disk

def _write_json(path: str, data) -> None:
    """Atomic write: a crash never leaves a half-written job file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Queue:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.jobs_dir = os.path.join(self.root, "jobs")
        self.batches_dir = os.path.join(self.root, "batches")
        self.stop_file = os.path.join(self.root, "stop")
        self.lock_file = os.path.join(self.root, "runner.lock")

    # ---- batches and jobs
    def batch(self, name: str) -> dict:
        try:
            with open(os.path.join(self.batches_dir, name + ".json")) as f:
                return json.load(f)
        except FileNotFoundError:
            raise JobError(f"no batch {name!r}") from None

    def jobs(self, batch: str | None = None) -> list[dict]:
        out = []
        if os.path.isdir(self.jobs_dir):
            for fn in os.listdir(self.jobs_dir):
                if fn.endswith(".json"):
                    with open(os.path.join(self.jobs_dir, fn)) as f:
                        j = json.load(f)
                    if batch is None or j["batch"] == batch:
                        out.append(j)
        out.sort(key=lambda j: (j["order"], j["id"]))
        return out

    def job(self, jid: str) -> dict:
        path = os.path.join(self.jobs_dir, jid + ".json")
        if not re.fullmatch(r"\d{1,9}", jid) or not os.path.exists(path):
            raise JobError(f"no job {jid!r}")
        with open(path) as f:
            return json.load(f)

    def save(self, job: dict) -> None:
        _write_json(os.path.join(self.jobs_dir, job["id"] + ".json"), job)

    def add_batch(self, name: str, setup: list[str], flops: list[tuple[str, float]], accuracy: float,
                  max_iters: int | None, max_seconds: float | None, save: str, out_dir: str | None,
                  source: str = "") -> list[dict]:
        if not NAME_RE.match(name):
            raise JobError("batch names: letters, digits, space, _ . + - (max 100)")
        if save not in SAVE_MODES:
            raise JobError(f"save mode must be one of {SAVE_MODES}")
        if not accuracy > 0:
            raise JobError("accuracy must be > 0 (percent of the starting pot)")
        if os.path.exists(os.path.join(self.batches_dir, name + ".json")):
            raise JobError(f"batch {name!r} exists (remove it first, or pick another name)")
        out_dir = os.path.abspath(os.path.expanduser(out_dir)) if out_dir else \
            os.path.join(self.root, "saves", name)
        _write_json(os.path.join(self.batches_dir, name + ".json"),
                    {"name": name, "setup": setup, "accuracy_pct": accuracy, "max_iters": max_iters,
                     "max_seconds": max_seconds, "save": save, "out_dir": out_dir, "source": source,
                     "created": time.time()})
        existing = self.jobs()
        next_id = max((int(j["id"]) for j in existing), default=0) + 1
        next_order = max((j["order"] for j in existing), default=0) + 1
        added = []
        for k, (board, weight) in enumerate(flops):
            j = {"id": f"{next_id + k:05d}", "batch": name, "board": board, "weight": weight,
                 "order": next_order + k, "status": "queued", "out": os.path.join(out_dir, board + ".cfr"),
                 "created": time.time(), "started": None, "finished": None, "result": None,
                 "error": None, "runner_pid": None}
            self.save(j)
            added.append(j)
        return added

    def remove(self, jobs: list[dict]) -> None:
        for j in jobs:
            if j["status"] == "running" and _pid_alive(j.get("runner_pid") or 0):
                raise JobError(f"job {j['id']} is running (stop it first)")
        for j in jobs:
            os.remove(os.path.join(self.jobs_dir, j["id"] + ".json"))
        # drop batches without jobs
        left = {j["batch"] for j in self.jobs()}
        for j in jobs:
            p = os.path.join(self.batches_dir, j["batch"] + ".json")
            if j["batch"] not in left and os.path.exists(p):
                os.remove(p)

    def move(self, jid: str, pos: int) -> None:
        jobs = self.jobs()
        j = next((x for x in jobs if x["id"] == jid), None)
        if j is None:
            raise JobError(f"no job {jid!r}")
        waiting = [x for x in jobs if x["status"] == "queued" and x["id"] != jid]
        pos = max(1, min(pos, len(waiting) + 1))
        waiting.insert(pos - 1, j)
        base = min((x["order"] for x in jobs), default=1)
        for k, x in enumerate(waiting):
            if x["order"] != base + k:
                x["order"] = base + k
                self.save(x)
        # finished/running jobs keep their place after the waiting ones
        for k, x in enumerate(y for y in jobs if y["status"] != "queued" and y["id"] != jid):
            x["order"] = base + len(waiting) + k
            self.save(x)

    def recover(self) -> None:
        """Jobs left 'running' by a runner that died go back to the queue."""
        for j in self.jobs():
            if j["status"] == "running" and not _pid_alive(j.get("runner_pid") or 0):
                j.update(status="queued", started=None, runner_pid=None)
                self.save(j)

    # ---- runner lock
    def lock(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(open(self.lock_file).read().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if _pid_alive(pid):
                    raise JobError(f"another runner is active (pid {pid})") from None
                os.remove(self.lock_file)  # stale
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return
        raise JobError("could not take the runner lock")

    def unlock(self) -> None:
        try:
            os.remove(self.lock_file)
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ running

def parse_results(text: str) -> dict:
    keys = {"EV OOP": "ev_oop", "EV IP": "ev_ip", "OOP's MES": "mes_oop", "IP's MES": "mes_ip",
            "Exploitable for": "exploitability"}
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if k.strip() in keys:
            try:
                out[keys[k.strip()]] = float(v)
            except ValueError:
                pass
    return out


def start_pot(setup: list[str]) -> float:
    pot = 0.0
    for l in setup:
        t = l.split()
        if t[0] == "set_pot" and len(t) >= 4:
            pot = sum(float(x) for x in t[1:4])
    return pot


def check_tree(solver_exe: str, setup: list[str], board: str) -> str:
    """Validate a batch's tree on one board without building it; returns the size estimate.

    estimate_schematic_tree expands every line, so a bad tree fails here at `add` instead of failing
    every job of the batch."""
    import server as vserver
    s = vserver.Solver(solver_exe)
    try:
        s.cmds(setup)
        s.cmd(f"set_board {board}")
        return s.cmd("estimate_schematic_tree", timeout=600)
    except vserver.SolverError as e:
        raise JobError(f"the tree does not build on {board}: {e}") from None
    finally:
        try:
            s.proc.kill()
            s.proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            pass


def run_job(q: Queue, job: dict, solver_exe: str, threads: int, log=print) -> str:
    """Solve one job; returns its final status. Raises KeyboardInterrupt through (job re-queued)."""
    import server as vserver  # viewer/server.py: the solver session wrapper

    b = q.batch(job["batch"])
    tag = f"[{job['id']} {job['board']}]"
    job.update(status="running", started=time.time(), finished=None, error=None, result=None,
               runner_pid=os.getpid())
    q.save(job)
    s = None
    t0 = time.time()
    try:
        s = vserver.Solver(solver_exe)
        s.cmd(f"set_threads {threads}")
        s.cmds(b["setup"])
        s.cmd(f"set_board {job['board']}")
        s.cmd("build_tree", timeout=7200)
        s.cmd(f"set_accuracy {b['accuracy_pct'] / 100.0:.10g} fraction")
        limit = f" {b['max_iters']} steps" if b.get("max_iters") else \
            f" {b['max_seconds']:g} seconds" if b.get("max_seconds") else ""
        before = s.state()["events"]
        s.cmd("go" + limit)
        pot = start_pot(b["setup"]) or 1.0
        last_report = 0.0
        stopping = False
        while True:
            st = s.state()
            if st["dead"]:
                raise vserver.SolverError("solver exited")
            if st["events"] > before and st["solver_status"].startswith("stopped") and not st["solving"]:
                break
            if not stopping and os.path.exists(q.stop_file) and open(q.stop_file).read().strip() == "now":
                s.send_raw("stop")
                stopping = True
            if time.time() - last_report > 5 and st["progress"]:
                pt = vserver.parse_progress_point(st["progress"])
                if pt:
                    log(f"{tag} {pt['t']:.0f} s, exploitable for {pt['explo']:.3f} "
                        f"({100 * pt['explo'] / pot:.3f}% of pot)")
                    last_report = time.time()
            time.sleep(0.2)
        if stopping:
            job.update(status="queued", started=None, runner_pid=None)
            q.save(job)
            log(f"{tag} stopped; back in the queue")
            return "queued"
        res = parse_results(s.cmd("calc_results", timeout=3600))
        try:
            res["iterations"] = int(float(s.cmd("show_iters")))
        except (ValueError, vserver.SolverError):
            pass
        res["seconds"] = round(time.time() - t0, 3)
        os.makedirs(os.path.dirname(job["out"]), exist_ok=True)
        mode = "" if b["save"] == "full" else " " + b["save"]
        s.cmd(f'dump_tree "{job["out"]}"{mode}', timeout=7200)
        job.update(status="done", finished=time.time(), result=res, runner_pid=None)
        q.save(job)
        expl = res.get("exploitability")
        log(f"{tag} done in {res['seconds']:.1f} s, {res.get('iterations', '?')} iterations, exploitable "
            f"for {expl if expl is None else f'{expl:.3f}'} -> {job['out']}")
        return "done"
    except KeyboardInterrupt:
        job.update(status="queued", started=None, runner_pid=None)
        q.save(job)
        raise
    except Exception as e:  # noqa: BLE001 - any failure fails this job, not the run
        job.update(status="failed", finished=time.time(), error=str(e)[:2000], runner_pid=None)
        q.save(job)
        log(f"{tag} FAILED: {e}")
        return "failed"
    finally:
        if s is not None:
            try:
                s.proc.kill()
                s.proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                pass


def run(q: Queue, ids: list[str] | None, batch: str | None, limit: int | None, solver_exe: str,
        threads: int, log=print) -> dict:
    """Run queued jobs one at a time (all, the given ids, or one batch). Returns status counts."""
    if not os.path.isfile(solver_exe):
        raise JobError(f"solver not found: {solver_exe}")
    q.lock()
    counts = {"done": 0, "failed": 0}
    try:
        q.recover()
        if os.path.exists(q.stop_file):
            os.remove(q.stop_file)
        if ids:
            for jid in ids:
                if q.job(jid)["status"] != "queued":
                    raise JobError(f"job {jid} is not queued (retry it first)")
        ran = 0
        while limit is None or ran < limit:
            if os.path.exists(q.stop_file):
                log("stop requested: no further jobs started")
                os.remove(q.stop_file)
                break
            # re-read every time: jobs may be added, removed or reordered while we run
            pending = [j for j in q.jobs(batch) if j["status"] == "queued" and (not ids or j["id"] in ids)]
            if not pending:
                break
            status = run_job(q, pending[0], solver_exe, threads, log)
            if status == "queued":  # stopped with --now
                break
            counts[status] += 1
            ran += 1
    finally:
        q.unlock()
    return counts


# ------------------------------------------------------------------ CLI

def fmt_job(j: dict) -> str:
    r = j.get("result") or {}
    extra = ""
    if j["status"] == "done":
        extra = (f"  EV {r.get('ev_oop', float('nan')):.3f}/{r.get('ev_ip', float('nan')):.3f}"
                 f"  expl {r.get('exploitability', float('nan')):.3f}  {r.get('seconds', 0):.1f}s")
    elif j["status"] == "failed":
        extra = "  " + (j.get("error") or "")[:80]
    return f"{j['id']}  {j['status']:<7} {j['batch']:<20} {j['board']:<10} w={j['weight']:<6g}{extra}"


def default_solver() -> str:
    for name in ("solver.exe", "solver") if os.name == "nt" else ("solver",):
        p = os.path.join(REPO, name)
        if os.path.isfile(p):
            return p
    return os.path.join(REPO, "solver")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", default=os.environ.get("WSOLVER_JOBS", os.path.join("~", ".wsolver", "jobs")),
                    help="queue directory (default ~/.wsolver/jobs or $WSOLVER_JOBS)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="queue a batch: one tree on many boards")
    a.add_argument("name")
    a.add_argument("tree", help="UPI script or PioViewer-style config")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--flops-file")
    g.add_argument("--flops", help="boards, comma-separated (optional :weight each)")
    a.add_argument("--accuracy", type=float, default=0.5, help="target, percent of the starting pot")
    lim = a.add_mutually_exclusive_group()
    lim.add_argument("--max-iters", type=int)
    lim.add_argument("--max-seconds", type=float)
    a.add_argument("--save", choices=SAVE_MODES, default="no_rivers")
    a.add_argument("--out", help="directory for the saves (default <queue>/saves/<name>)")
    a.add_argument("--solver", default=default_solver(), help="checks the tree on the first board")
    a.add_argument("--no-check", action="store_true", help="queue without checking the tree")

    ls = sub.add_parser("list", help="show jobs")
    ls.add_argument("--batch")

    r = sub.add_parser("run", help="run queued jobs one at a time")
    r.add_argument("ids", nargs="*")
    r.add_argument("--batch")
    r.add_argument("--limit", type=int)
    r.add_argument("--solver", default=default_solver())
    r.add_argument("--threads", type=int, default=0, help="0 = all hardware threads")

    st = sub.add_parser("stop", help="stop the runner after the current job")
    st.add_argument("--now", action="store_true", help="stop the current job too (it is re-queued)")

    sh = sub.add_parser("show", help="one job in full")
    sh.add_argument("id")

    rt = sub.add_parser("retry", help="put failed (or done) jobs back in the queue")
    rt.add_argument("ids", nargs="*")
    rt.add_argument("--failed", action="store_true", help="all failed jobs")

    rm = sub.add_parser("remove", help="remove jobs")
    rm.add_argument("ids", nargs="*")
    rm.add_argument("--batch")

    mv = sub.add_parser("move", help="reorder a queued job")
    mv.add_argument("id")
    mv.add_argument("--to", type=int, required=True, help="position among queued jobs, 1 = next")

    ex = sub.add_parser("export", help="a batch's results as CSV")
    ex.add_argument("name")
    ex.add_argument("-o", "--output", help="file (default stdout)")

    args = ap.parse_args(argv)
    q = Queue(args.queue)
    try:
        if args.cmd == "add":
            with open(args.tree) as f:
                setup, dropped = tree_setup(f.read())
            if args.flops_file:
                with open(args.flops_file) as f:
                    flops = parse_flops(f.read())
            else:
                flops = parse_flops(args.flops)
            if dropped:
                print(f"note: ignoring {len(dropped)} run command(s) from the tree "
                      f"({', '.join(sorted({d.split()[0] for d in dropped}))})")
            if not args.no_check:
                if os.path.isfile(args.solver):
                    est = check_tree(os.path.abspath(args.solver), setup, flops[0][0])
                    print(f"tree OK on {flops[0][0]}: " + "; ".join(est.splitlines()))
                else:
                    print(f"note: solver not found ({args.solver}); the tree was not checked")
            added = q.add_batch(args.name, setup, flops, args.accuracy, args.max_iters, args.max_seconds,
                                args.save, args.out, source=os.path.abspath(args.tree))
            print(f"queued {len(added)} jobs in batch {args.name!r} ({added[0]['id']}-{added[-1]['id']})")
        elif args.cmd == "list":
            jobs = q.jobs(args.batch)
            for j in jobs:
                print(fmt_job(j))
            n = {s: sum(j["status"] == s for j in jobs) for s in STATUSES}
            print(", ".join(f"{v} {k}" for k, v in n.items()))
        elif args.cmd == "run":
            counts = run(q, args.ids or None, args.batch, args.limit, os.path.abspath(args.solver), args.threads)
            print(f"finished: {counts['done']} done, {counts['failed']} failed")
            return 1 if counts["failed"] else 0
        elif args.cmd == "stop":
            os.makedirs(q.root, exist_ok=True)
            with open(q.stop_file, "w") as f:
                f.write("now" if args.now else "after")
            print("the runner stops " + ("now (the current job goes back to the queue)" if args.now
                                         else "after the current job"))
        elif args.cmd == "show":
            j = q.job(args.id)
            print(json.dumps(j, indent=1, sort_keys=True))
        elif args.cmd == "retry":
            jobs = [j for j in q.jobs() if j["status"] == "failed"] if args.failed else [q.job(i) for i in args.ids]
            for j in jobs:
                if j["status"] == "running":
                    raise JobError(f"job {j['id']} is running")
                j.update(status="queued", started=None, finished=None, error=None, result=None)
                q.save(j)
            print(f"re-queued {len(jobs)} job(s)")
        elif args.cmd == "remove":
            jobs = q.jobs(args.batch) if args.batch else [q.job(i) for i in args.ids]
            if not jobs:
                raise JobError("nothing to remove")
            q.remove(jobs)
            print(f"removed {len(jobs)} job(s); saved files are kept")
        elif args.cmd == "move":
            q.move(args.id, args.to)
            print(f"job {args.id} is now number {args.to} in the queue")
        elif args.cmd == "export":
            q.batch(args.name)
            fields = ["id", "board", "weight", "status", "ev_oop", "ev_ip", "mes_oop", "mes_ip",
                      "exploitability", "iterations", "seconds", "out"]
            out = open(args.output, "w", newline="") if args.output else sys.stdout
            w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for j in q.jobs(args.name):
                w.writerow({**j, **(j.get("result") or {})})
            if args.output:
                out.close()
    except JobError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted: the current job is back in the queue", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
