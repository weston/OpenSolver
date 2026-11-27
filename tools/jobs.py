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
    python3 tools/jobs.py report NAME [--csv FILE]       # root strategy per flop + weighted average
    python3 tools/jobs.py report NAME --line r:0:b198:c:2d [--cards]  # at a line; a card = every card there

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
import threading
import time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "viewer"))

STATUSES = ("queued", "running", "done", "failed")
SAVE_MODES = ("no_rivers", "no_turns", "full")
# not ending in "." or " ": Windows maps folder "x." and "x " onto "x", so two batches would share saves (#362)
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _.+-]{0,98}[A-Za-z0-9_+-])?$")
CARD = r"[2-9TJQKAtjqka][cdhsCDHS]"
BOARD_RE = re.compile(rf"((?:{CARD}\s*){{3,5}})(?::\s*([0-9]*\.?[0-9]+))?")
# Commands the queue issues itself; dropped from a batch's setup.
RUN_COMMANDS = {"set_board", "build_tree", "go", "wait_for_solver", "stop", "dump_tree", "load_tree",
                "exit", "set_end_string", "reset_end_string", "solve_partial", "solve_all_splits",
                "set_threads", "set_accuracy", "free_tree", "calc_results", "is_ready", "load_script",
                "load_script_silent", "stdoutredi", "stdoutredi_append", "stdoutback", "take_a_break"}


class JobError(Exception):
    """Bad input or queue state."""


class LineError(JobError):
    """A report line that doesn't fit a job's tree (no such action, a card where none is dealt, not a
    decision). A fact about the tree, so it is cached like a result (#352)."""


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
    # a comment runs to the end of its line, commas and all
    uncommented = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
    for raw in uncommented.replace(",", "\n").replace(";", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        # `Ks5h2s Td7h2c`: whole boards separated by spaces (BOARD_RE alone would read one 5-card board)
        toks = line.split()
        if len(toks) > 1 and all(re.fullmatch(rf"(?:{CARD}){{3,5}}(?::[0-9]*\.?[0-9]+)?", t) for t in toks):
            for t in toks:
                b, _, w = t.partition(":")
                board = normalize_board(b)
                if board in seen:
                    raise JobError(f"board {board} listed twice")
                seen.add(board)
                out.append((board, float(w) if w else 1.0))
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

def _retry_shared(fn):
    """fn(), retried for ~2 s on PermissionError: on Windows a file being read by another process can't be
    replaced (and is briefly unreadable while being replaced), which is a wait, not a failure (#362)."""
    for k in range(100):
        try:
            return fn()
        except PermissionError:
            if os.name != "nt" or k == 99:
                raise
            time.sleep(0.02)


def _write_json(path: str, data) -> None:
    """Atomic write: a crash never leaves a half-written job file (fsynced before the rename, #362)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        _retry_shared(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str):
    def rd():
        with open(path) as f:
            return json.load(f)
    return _retry_shared(rd)


def _proc_start(pid: int) -> str | None:
    """When process `pid` started, as an opaque string, or None if unknown. With the pid it names one
    process: a pid reused later (as after a reboot) has another start (#398)."""
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                t = [ctypes.c_ulonglong() for _ in range(4)]
                if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
                    return None
                return str(t[0].value)  # creation time
            finally:
                k32.CloseHandle(h)
        with open(f"/proc/{pid}/stat") as f:
            start = f.read().rsplit(")", 1)[1].split()[19]  # field 22, after the command name
        try:
            with open("/proc/sys/kernel/random/boot_id") as f:
                start = f.read().strip()[:8] + ":" + start
        except OSError:
            pass
        return start
    except (OSError, IndexError, ValueError, AttributeError):
        return None


def _pid_alive(pid: int, start: str | None = None) -> bool:
    """Process `pid` is running and, if `start` is given and can be checked, is the one that started then."""
    if pid <= 0:
        return False
    if start:
        now = _proc_start(pid)
        if now is not None and now != start:
            return False  # the pid was reused
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


def _job_runner_alive(j: dict) -> bool:
    return _pid_alive(j.get("runner_pid") or 0, j.get("runner_start"))


class Queue:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.jobs_dir = os.path.join(self.root, "jobs")
        self.batches_dir = os.path.join(self.root, "batches")
        self.stop_file = os.path.join(self.root, "stop")
        self.lock_file = os.path.join(self.root, "runner.lock")

    # ---- the edit lock: every read-modify-write of the queue (add, remove, move, retry, the runner's
    # status changes) holds it, across processes (CLI next to the viewer) and threads (viewer handlers
    # next to its runner thread). Plain reads don't need it: each job file is replaced atomically.
    _held = threading.local()

    @contextmanager
    def edit(self):
        depth = getattr(Queue._held, "depth", 0)
        if depth:  # already held by this thread
            Queue._held.depth = depth + 1
            try:
                yield
            finally:
                Queue._held.depth = depth
            return
        os.makedirs(self.root, exist_ok=True)
        f = open(os.path.join(self.root, "queue.lock"), "a+")
        try:
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises
                        break
                    except OSError:
                        continue
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            Queue._held.depth = 1
            try:
                yield
            finally:
                Queue._held.depth = 0
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.close()  # closing also drops a flock

    # ---- batches and jobs
    def batch(self, name: str) -> dict:
        try:
            return _read_json(os.path.join(self.batches_dir, name + ".json"))
        except FileNotFoundError:
            raise JobError(f"no batch {name!r}") from None
        except ValueError:
            raise JobError(f"batch {name!r}: its file is damaged") from None

    def jobs(self, batch: str | None = None) -> list[dict]:
        out = []
        if os.path.isdir(self.jobs_dir):
            for fn in os.listdir(self.jobs_dir):
                if fn.endswith(".json"):
                    try:
                        j = _read_json(os.path.join(self.jobs_dir, fn))
                    except FileNotFoundError:
                        continue  # removed since the listing
                    except ValueError:
                        # a damaged file (cut short by a crash): left out, not the end of the queue (#362)
                        print(f"warning: skipping damaged job file {fn}", file=sys.stderr)
                        continue
                    if batch is None or j["batch"] == batch:
                        out.append(j)
        out.sort(key=lambda j: (j["order"], j["id"]))
        return out

    def job(self, jid: str) -> dict:
        path = os.path.join(self.jobs_dir, jid + ".json")
        if not re.fullmatch(r"\d{1,9}", jid):
            raise JobError(f"no job {jid!r}")
        try:
            return _read_json(path)
        except FileNotFoundError:
            raise JobError(f"no job {jid!r}") from None
        except ValueError:
            raise JobError(f"job {jid}: its file is damaged") from None

    def save(self, job: dict) -> None:
        _write_json(os.path.join(self.jobs_dir, job["id"] + ".json"), job)

    def update(self, jid: str, expect=None, **fields) -> dict | None:
        """Change some fields of a job as it is on disk now (not as a caller read it earlier).
        Returns the job, or None (nothing written) if it was removed or expect(job) is false."""
        with self.edit():
            try:
                j = self.job(jid)
            except JobError:
                return None
            if expect is not None and not expect(j):
                return None
            j.update(fields)
            self.save(j)
            return j

    def retry(self, jids: list[str] | None) -> int:
        """Back to queued: the given jobs, or (None) every failed one. A running job is refused."""
        with self.edit():
            js = [j for j in self.jobs() if j["status"] == "failed"] if jids is None else [self.job(i) for i in jids]
            for j in js:
                if j["status"] == "running" and _job_runner_alive(j):
                    raise JobError(f"job {j['id']} is running")
            for j in js:
                j.update(status="queued", started=None, finished=None, error=None, result=None, runner_pid=None)
                self.save(j)
            return len(js)

    def add_batch(self, name: str, setup: list[str], flops: list[tuple[str, float]], accuracy: float,
                  max_iters: int | None, max_seconds: float | None, save: str, out_dir: str | None,
                  source: str = "", smart_tree: bool = False) -> list[dict]:
        if not NAME_RE.match(name):
            raise JobError("batch names: letters, digits, space, _ . + - (max 100), not ending in . or space")
        if save not in SAVE_MODES:
            raise JobError(f"save mode must be one of {SAVE_MODES}")
        if not accuracy > 0:
            raise JobError("accuracy must be > 0 (percent of the starting pot)")
        if max_iters is not None and not max_iters > 0:
            raise JobError("max iterations must be > 0")
        if max_seconds is not None and not max_seconds > 0:
            raise JobError("max seconds must be > 0")
        with self.edit():
            return self._add_batch(name, setup, flops, accuracy, max_iters, max_seconds, save, out_dir, source, smart_tree)

    def _add_batch(self, name, setup, flops, accuracy, max_iters, max_seconds, save, out_dir, source, smart_tree):
        if os.path.exists(os.path.join(self.batches_dir, name + ".json")) and self.jobs(name):
            raise JobError(f"batch {name!r} exists (remove it first, or pick another name)")
        # (a batch file with no jobs is left from a crash between the writes below: reused, #362)
        out_dir = os.path.abspath(os.path.expanduser(out_dir)) if out_dir else \
            os.path.join(self.root, "saves", name)
        _write_json(os.path.join(self.batches_dir, name + ".json"),
                    {"name": name, "setup": setup, "smart_tree": smart_tree, "accuracy_pct": accuracy, "max_iters": max_iters,
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
        with self.edit():
            # as they are now (one may have started meanwhile), each once: a repeated id would be removed twice
            jobs = list({j["id"]: self.job(j["id"]) for j in jobs}.values())
            for j in jobs:
                if j["status"] == "running" and _job_runner_alive(j):
                    raise JobError(f"job {j['id']} is running (stop it first)")
            for j in jobs:
                os.remove(os.path.join(self.jobs_dir, j["id"] + ".json"))
            self._drop_empty_batches(jobs)

    def _drop_empty_batches(self, jobs: list[dict]) -> None:
        # drop batches without jobs
        left = {j["batch"] for j in self.jobs()}
        for j in jobs:
            p = os.path.join(self.batches_dir, j["batch"] + ".json")
            if j["batch"] not in left and os.path.exists(p):
                os.remove(p)

    def move(self, jid: str, pos: int) -> None:
        with self.edit():
            jobs = self.jobs()
            j = next((x for x in jobs if x["id"] == jid), None)
            if j is None:
                raise JobError(f"no job {jid!r}")
            if j["status"] != "queued":
                raise JobError(f"job {jid} is {j['status']}, not queued")
            waiting = [x for x in jobs if x["status"] == "queued" and x["id"] != jid]
            pos = max(1, min(pos, len(waiting) + 1))
            waiting.insert(pos - 1, j)
            base = min((x["order"] for x in jobs), default=1)
            # finished/running jobs keep their place after the waiting ones
            rest = [y for y in jobs if y["status"] != "queued"]
            for k, x in enumerate(waiting + rest):
                if x["order"] != base + k:
                    x["order"] = base + k
                    self.save(x)

    def recover(self) -> None:
        """Jobs left 'running' by a runner that died go back to the queue."""
        with self.edit():
            for j in self.jobs():
                if j["status"] == "running" and not _job_runner_alive(j):
                    j.update(status="queued", started=None, runner_pid=None)
                    self.save(j)

    # ---- runner lock
    def lock(self) -> None:
        """Become the queue's runner. Checked and taken under the edit lock, so of two runners started
        at once exactly one gets it (#362: the pid used to be written after the file was created, and a
        contender reading it in between took the empty file for a stale lock)."""
        os.makedirs(self.root, exist_ok=True)
        with self.edit():
            pid = self.runner()
            if pid:
                raise JobError(f"another runner is active (pid {pid})")
            fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {_proc_start(os.getpid()) or ''}".strip())
            _retry_shared(lambda: os.replace(tmp, self.lock_file))  # the pid is never seen half-written

    def _lock_owner(self) -> tuple[int, str | None]:
        """(pid, start) from the lock file: "pid start", or just "pid" from before #398."""
        try:
            with open(self.lock_file) as f:
                parts = f.read().split()
        except OSError:
            return 0, None
        try:
            return int(parts[0]) if parts else 0, parts[1] if len(parts) > 1 else None
        except ValueError:
            return 0, None

    def runner_pid(self) -> int:
        return self._lock_owner()[0]

    def runner(self) -> int:
        """The pid of the live runner holding the lock (this process or another), else 0. A pid
        reused by another process since the lock was taken (as after a reboot) is not it (#398)."""
        pid, start = self._lock_owner()
        return pid if _pid_alive(pid, start) else 0

    def unlock(self) -> None:
        """Release the runner lock if this process holds it (never another runner's)."""
        with self.edit():
            if self.runner_pid() == os.getpid():
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


def root_summary(s) -> dict:
    """The first decision's strategy over the acting player's whole range: {player, actions, freqs, mass}.

    Frequencies weight each hand by reach x matchups (its reach times the opponent's reach over the
    combos it doesn't block: calc_ev's second line, which already includes the hand's reach). That is
    how often the deal happens, and it is what PioViewer's aggregation report uses: recomputed from
    its own per-hand report files, flop rows match to the files' rounding (#323). `mass` is the
    root's total, sum of reach x matchups, which weights this flop in the batch average. Actions are
    the last step of each child id ("c", "b50", ...): the same on every flop of a batch, because line
    amounts do not depend on the board."""
    import math
    import server as vserver
    node = vserver.parse_node_block(s.cmd("show_node r:0"))
    player = {"OOP_DEC": "OOP", "IP_DEC": "IP"}.get(node["type"])
    if player is None:
        return {}
    actions = [k["id"].rsplit(":", 1)[1] for k in vserver.parse_children(s.cmd("show_children r:0"))]
    rows = [l.split() for l in s.cmd("show_strategy r:0").splitlines() if l.strip()]
    ev_cmd = f"calc_ev {player} r:0"
    try:
        result = s.cmd(ev_cmd + " matchups")
    except vserver.SolverError as e:
        if str(e) != "ERROR: calc_ev incorrect or missing argument":
            raise
        result = s.cmd(ev_cmd)
    mus = vserver.parse_floats(result.rsplit("\n", 1)[-1], vserver.NUM_COMBOS)
    num = [0.0] * len(rows)
    tot = 0.0
    for i, m in enumerate(mus):
        vals = [float(row[i]) for row in rows]
        if m is not None and math.isfinite(m) and m > 0 and all(math.isfinite(v) for v in vals):
            tot += m
            for a, v in enumerate(vals):
                num[a] += m * v
    if tot <= 0 or len(actions) != len(rows):
        return {}
    return {"player": player, "actions": actions, "freqs": [round(x / tot, 6) for x in num],
            "mass": float(f"{tot:.9g}")}


LINE_ACTION = re.compile(r"^(c|f|b\d+)$")


def parse_line(text: str) -> list[str]:
    """A report line -> its steps after the root: actions (c, f, b<amount>) and cards, where a card or
    `*` at a chance node stands for every card dealt there (#325). Takes a node id (r:0:b198:c:2d:c:b447),
    the same without r:0, or the steps separated by spaces or commas. Empty = the root."""
    toks = [t for t in re.split(r"[:\s,]+", text.strip()) if t]
    if toks[:2] == ["r", "0"]:
        toks = toks[2:]
    out = []
    for t in toks:
        if t == "*":
            out.append(t)
        elif LINE_ACTION.match(t.lower()):
            out.append(t.lower())
        elif len(t) == 2 and t[0].upper() in RANK_ORDER and t[1].lower() in "cdhs":
            out.append("*")  # every card here, as PioViewer's report does with the turn
        else:
            raise JobError(f"line step {t!r}: expected c, f, b<amount>, a card or *")
    return out


def _child_step(child_id: str) -> str:
    return child_id.rsplit(":", 1)[1]


def walk_line(s, steps: list[str]) -> list[tuple[str, list[str]]]:
    """The nodes a line reaches in the loaded tree: [(node id, cards dealt at its * steps)]. A * step
    expands to every card at that chance node. An action missing from the tree is an error at the
    first node; under an expanded card it only drops that branch."""
    import server as vserver
    leaves = [("r:0", [])]
    for t in steps:
        nxt = []
        for nid, cards in leaves:
            node = vserver.parse_node_block(s.cmd(f"show_node {nid}"))
            kids = [k["id"] for k in vserver.parse_children(s.cmd(f"show_children {nid}"))]
            chance = node["type"] == "SPLIT_NODE"
            if t == "*":
                if not chance:
                    raise LineError(f"a card at {nid}, which is not a chance node (its actions: "
                                   f"{', '.join(_child_step(k) for k in kids) or 'none'})")
                nxt.extend((k, cards + [_child_step(k)]) for k in kids)
                continue
            if chance:
                raise LineError(f"{t} at {nid}, which deals a card (put the card or * here)")
            match = [k for k in kids if _child_step(k) == t]
            if match:
                nxt.append((match[0], cards))
            elif len(leaves) == 1:
                raise LineError(f"no action {t} at {nid} (there: {', '.join(_child_step(k) for k in kids) or 'none'})")
        leaves = nxt
    if not leaves:
        raise LineError("the line reaches no node")
    return leaves


def node_summary(s, nid: str) -> dict:
    """A decision's strategy over the acting player's range, weighted by reach x matchups like
    root_summary, plus both players' matchup-weighted EVs (chips), the node's matchup mass and
    calc_global_freq (the node's matchups over the root's: no chance factor)."""
    import math
    import server as vserver
    node = vserver.parse_node_block(s.cmd(f"show_node {nid}"))
    player = {"OOP_DEC": "OOP", "IP_DEC": "IP"}.get(node["type"])
    if player is None:
        raise LineError(f"{nid} is not a decision ({node['type']})")
    actions = [_child_step(k["id"]) for k in vserver.parse_children(s.cmd(f"show_children {nid}"))]
    rows = [[float(x) for x in l.split()] for l in s.cmd(f"show_strategy {nid}").splitlines() if l.strip()]
    if len(rows) != len(actions):
        raise JobError(f"{nid}: {len(rows)} strategy rows for {len(actions)} actions")
    pot = [x for x in node["pot"] if x is not None]
    out = {"player": player, "actions": actions, "pot": sum(pot),
           "facing": abs(pot[0] - pot[1]) if len(pot) >= 2 else 0, "hands": {}}
    order = vserver.hand_order(s)
    for p in ("OOP", "IP"):
        e = vserver.ev_pair(s, p, nid)
        if e["matchups"] is None:
            raise JobError("calc_ev gave no matchups line")
        eq = vserver.parse_floats(s.cmd(f"calc_eq_node {p} {nid}").split("\n")[0], len(order))
        # per hand class (#336): sums of matchups, matchups x EV and matchups x equity, as PioViewer's
        # handsEV.csv aggregates them (each combo counts by reach x matchups)
        hs = {}
        for i, m in enumerate(e["matchups"]):
            v, q = e["ev"][i], eq[i]
            if m is None or not math.isfinite(m) or m <= 0 or v is None or not math.isfinite(v) \
                    or q is None or not math.isfinite(q):
                continue
            a = hs.setdefault(vserver.combo_class(order[i]), [0.0, 0.0, 0.0])
            a[0] += m
            a[1] += m * v
            a[2] += m * q
        out["hands"][p] = hs
        num = den = 0.0
        fnum = [0.0] * len(rows)
        fden = 0.0
        for i, (v, m) in enumerate(zip(e["ev"], e["matchups"])):
            if m is None or not math.isfinite(m) or m <= 0:
                continue
            if v is not None and math.isfinite(v):
                num += v * m
                den += m
            if p == player:
                vals = [r[i] for r in rows]
                if all(math.isfinite(x) for x in vals):
                    fden += m
                    for a, x in enumerate(vals):
                        fnum[a] += m * x
        out["ev_" + p.lower()] = round(num / den, 6) if den > 0 else None
        if p == player:
            out["freqs"] = [round(x / fden, 6) for x in fnum] if fden > 0 else None
            out["mass"] = float(f"{fden:.9g}")
    out["global"] = float(s.cmd(f"calc_global_freq {nid}").split()[0])
    return out


def line_result(s, steps: list[str]) -> dict:
    """One job's report at a line: the node summaries, one per dealt card at the * steps."""
    rows = []
    head = None
    hands = {}
    for nid, cards in walk_line(s, steps):
        n = node_summary(s, nid)
        if head is None:
            head = {k: n[k] for k in ("player", "actions", "pot", "facing")}
        elif n["actions"] != head["actions"] or n["player"] != head["player"]:
            continue  # a different decision under one card (see walk_line): left out
        rows.append({"cards": cards, **{k: n[k] for k in ("freqs", "mass", "ev_oop", "ev_ip", "global")}})
        for p, hs in n["hands"].items():
            tot = hands.setdefault(p, {})
            for c, a in hs.items():
                t = tot.setdefault(c, [0.0, 0.0, 0.0])
                for k in range(3):
                    t[k] += a[k]
    # over the cards of a * line: their mean, the same scale as the flop's mass (aggregate_cards)
    k = len(rows) or 1
    head["hands"] = {p: {c: [float(f"{x / k:.7g}") for x in a] for c, a in hs.items()} for p, hs in hands.items()}
    return {**head, "rows": rows}


def aggregate_cards(rows: list[dict]) -> dict:
    """A flop's figures from its per-card rows, as PioViewer's turn report rolls them up (#325):
    frequencies and EVs weighted by each card's matchup mass, Global % their plain mean (it has no
    chance factor), and the mass their mean (the flop's weight in the batch average)."""
    out = {"freqs": None, "mass": None, "ev_oop": None, "ev_ip": None, "global": None}
    if not rows:
        return out
    have = [r for r in rows if r["freqs"] is not None and r["mass"]]
    M = sum(r["mass"] for r in have)
    out["global"] = sum(r["global"] for r in rows) / len(rows)
    out["mass"] = M / len(rows)
    if M > 0:
        n = len(have[0]["freqs"])
        out["freqs"] = [sum(r["mass"] * r["freqs"][a] for r in have) / M for a in range(n)]
    for k in ("ev_oop", "ev_ip"):
        hv = [r for r in have if r[k] is not None]
        W = sum(r["mass"] for r in hv)
        out[k] = sum(r["mass"] * r[k] for r in hv) / W if W > 0 else None
    return out


def action_label(a: str, pot: float, facing: float = 0) -> str:
    """'c' -> 'check', 'b50' -> 'bet 50 (50%)' (pot = the pot at the node). Facing a bet (`facing` =
    the chips to call), 'c' is a call and 'bN' a raise to N; its percent is the raise over the pot
    once the call is in."""
    if a == "c":
        return "call" if facing else "check"
    if a == "f":
        return "fold"
    if a.startswith("b") and a[1:].isdigit():
        if facing:
            after = pot + facing
            return f"raise {a[1:]}" + (f" ({100 * (int(a[1:]) - facing) / after:.0f}%)" if after > 0 else "")
        return f"bet {a[1:]}" + (f" ({100 * int(a[1:]) / pot:.0f}%)" if pot > 0 else "")
    return a


RANK_ORDER = "23456789TJQKA"
# Flop textures the report can group by: key -> (title, function of a board like "Kh9h4c")
TEXTURES = {}


def _ranks(board: str) -> list[int]:
    return sorted((RANK_ORDER.index(board[i]) for i in range(0, 6, 2)), reverse=True)


def texture_high(board: str) -> str:
    """The top card: A, K, Q, J, T, then 9-or-lower boards together."""
    top = RANK_ORDER[_ranks(board)[0]]
    return top if top in "AKQJT" else "9 or lower"


def texture_suits(board: str) -> str:
    n = len({board[i + 1] for i in range(0, 6, 2)})
    return {1: "monotone", 2: "two-tone", 3: "rainbow"}[n]


def texture_pairing(board: str) -> str:
    n = len(set(_ranks(board)))
    return {3: "unpaired", 2: "paired", 1: "trips"}[n]


def texture_straight(board: str) -> str:
    """Unpaired boards whose three ranks fit in a five-rank window (the ace also counts low)
    make straights possible with two cards; the others do not."""
    r = set(_ranks(board))
    if len(r) < 3:
        return "paired (no 3-card straight)"
    lows = [r, {x if x != 12 else -1 for x in r}]
    return "straight possible" if any(max(s) - min(s) <= 4 for s in lows) else "no straight"


TEXTURES.update({"high": ("High card", texture_high), "suits": ("Suits", texture_suits),
                 "pairing": ("Pairing", texture_pairing), "straight": ("Straights", texture_straight)})
GROUP_ORDER = {"high": ["A", "K", "Q", "J", "T", "9 or lower"], "suits": ["rainbow", "two-tone", "monotone"],
               "pairing": ["unpaired", "paired", "trips"],
               "straight": ["straight possible", "no straight", "paired (no 3-card straight)"]}


def _weighted(rows: list, n_actions: int) -> dict:
    """Frequencies and EVs of report rows that have a root summary, averaged over the flops.

    Each flop counts by its weight times its matchup mass (sum of reach x matchups at the root), as
    PioViewer's aggregation report does (#323): boards differ in how many combos they block. A row
    without a mass (a summary recorded before #323) counts by its weight alone. `weight` is still
    the plain sum of the flop weights."""
    have = [x for x in rows if x["freqs"] is not None]
    wsum = sum(x["weight"] for x in have)
    if not have or wsum <= 0:
        return {}
    if all(x.get("mass") for x in have):
        ws = [x["weight"] * x["mass"] for x in have]
    else:
        ws = [x["weight"] for x in have]
    W = sum(ws)
    if W <= 0:
        return {}
    out = {"freqs": [sum(w * x["freqs"][a] for w, x in zip(ws, have)) / W for a in range(n_actions)],
           "weight": wsum, "flops": len(have)}
    for k in ("ev_oop", "ev_ip"):
        if all(x[k] is not None for x in have):
            out[k] = sum(w * x[k] for w, x in zip(ws, have)) / W
    return out


def group_rows(rows: list, by: str, n_actions: int) -> list:
    """Report rows grouped by a flop texture, each group weight-averaged like the total. The texture
    is the flop's (the first three cards), also for a batch of turn or river boards."""
    if by not in TEXTURES:
        raise JobError(f"group by one of {', '.join(TEXTURES)}")
    fn = TEXTURES[by][1]
    groups = {}
    for x in rows:
        groups.setdefault(fn(x["board"]), []).append(x)
    order = GROUP_ORDER[by]
    out = []
    for key in sorted(groups, key=lambda k: order.index(k) if k in order else len(order)):
        w = _weighted(groups[key], n_actions)
        if w:
            # only the flops that went into the average (a row without a root summary didn't)
            out.append({"key": key, **w, "boards": [x["board"] for x in groups[key] if x["freqs"] is not None]})
    return out


def report(q: Queue, name: str, solver_exe: str | None = None) -> dict:
    """Per-flop root strategy, EVs and exploitability of a batch, plus the weighted average.

    Done jobs from before root summaries were recorded are read from their saves (needs solver_exe)."""
    b = q.batch(name)
    pot = start_pot(b["setup"])
    rows, actions, player = [], None, None
    for j in q.jobs(name):
        if j["status"] != "done":
            continue
        r = j.get("result") or {}
        # read once: a summary ({} when the root has nothing to show) or the reason it failed is kept
        # summaries from before #323 have no matchup mass (their freqs were reach-weighted): re-read
        stale = "root" not in r or (r["root"] and "mass" not in r["root"])
        if stale and "root_error" not in r and solver_exe and os.path.isfile(j["out"]):
            import server as vserver
            s = None
            try:
                s = vserver.Solver(solver_exe)
                s.cmd(f'load_tree "{j["out"]}"', timeout=3600)
                r["root"] = root_summary(s)
            except (vserver.SolverError, OSError, ValueError, IndexError) as e:
                r["root_error"] = str(e)[:500]
            finally:
                if s is not None:
                    close_solver(s)
            # the load can take minutes: write it only to the same finished job (not one removed or
            # retried meanwhile), and as the job is now
            fin = j.get("finished")
            with q.edit():
                try:
                    cur = q.job(j["id"])
                except JobError:
                    cur = None
                if cur and cur["status"] == "done" and cur.get("finished") == fin:
                    cr = dict(cur.get("result") or {})
                    for k in ("root", "root_error"):
                        if k in r:
                            cr[k] = r[k]
                    cur["result"] = cr
                    q.save(cur)
        root = r.get("root") or {}
        if root and actions is None:
            actions, player = root["actions"], root["player"]
        rows.append({"id": j["id"], "board": j["board"], "weight": j["weight"], "ev_oop": r.get("ev_oop"),
                     "ev_ip": r.get("ev_ip"), "exploitability": r.get("exploitability"),
                     "freqs": root.get("freqs") if root.get("actions") == actions else None,
                     "mass": root.get("mass") if root.get("actions") == actions else None})
    n = len(actions or [])
    for x in rows:
        x["texture"] = {k: fn(x["board"]) for k, (_, fn) in TEXTURES.items()}
    return {"batch": name, "pot": pot, "player": player,
            "actions": actions or [], "labels": [action_label(a, pot) for a in actions or []],
            "rows": rows, "total": _weighted(rows, n),
            "groups": {k: group_rows(rows, k, n) for k in TEXTURES} if n else {},
            "group_titles": {k: t for k, (t, _) in TEXTURES.items()},
            "pending": sum(j["status"] != "done" for j in q.jobs(name))}


LINE_CACHE = 8  # line results kept per job (each is a few KB; a turn line has ~49 rows)


def line_uncached(q: Queue, name: str, line: str) -> list[dict]:
    """The done jobs of a batch whose result at this line isn't cached yet (a line report reads their
    saves). The empty line is the root as line_report(root=True) reads it, for the per-hand figures."""
    steps = parse_line(line)
    key = ":".join(steps)
    q.batch(name)
    return [j for j in q.jobs(name) if j["status"] == "done" and _cached_line(j, key) is None]


def _cached_line(j: dict, key: str):
    """A job's cached result at a line, or None. Results from before the per-hand sums (#336) count as
    missing (a line that doesn't fit the tree, cached as an error, doesn't)."""
    res = ((j.get("result") or {}).get("lines") or {}).get(key)
    if res is not None and "error" not in res and "hands" not in res:
        return None
    return res


def line_report(q: Queue, name: str, line: str, solver_exe: str | None, progress=None, root: bool = False,
                skip: dict | None = None, failures: dict | None = None) -> dict:
    """The batch's report at a line other than the root (#325), like PioViewer's aggregation report
    there: per flop the decision's strategy, both EVs and Global % (calc_global_freq); with a card or
    * in the line, one row per card dealt there under each flop (`cards`), rolled up into the flop's
    row by aggregate_cards. Each job's result is cached in the job, so the saves are read once per
    line. The batch average weights flops by weight x matchup mass, like the root report.

    `progress(done, total, board)` is called before each save is read (total = the saves to read) and
    once at the end, so a caller can show how far it got (#333).

    The empty line is the root report (report()), unless `root` asks for this report at the root, which
    reads the saves too but has the per-hand figures (`hands`, #336).

    `skip` ({job id: message}) leaves those uncached jobs unread, as rows with that error; `failures`
    (a dict) collects {job id: message} for the saves that couldn't be read (#352: the viewer's
    background task reads, the request that follows doesn't)."""
    import server as vserver
    steps = parse_line(line)
    if not steps and not root:
        return report(q, name, solver_exe)
    key = ":".join(steps)
    b = q.batch(name)
    head = None
    rows = []
    js = q.jobs(name)
    to_read = sum(j["status"] == "done" and _cached_line(j, key) is None for j in js)
    read = 0
    hand_tot = {}
    for j in js:
        if j["status"] != "done":
            continue
        r = j.get("result") or {}
        res = _cached_line(j, key)
        if res is None and skip is not None and j["id"] in skip:
            res = {"error": skip[j["id"]]}
        elif res is None:
            if not solver_exe:
                raise JobError("a report at a line reads the saves: give --solver")
            if progress:
                progress(read, to_read, j["board"])
            read += 1
            s = None
            cache = False  # only results, and lines that don't fit the tree: a failed read may work later
            try:
                if not os.path.isfile(j["out"]):
                    raise OSError(f"save not found: {j['out']}")
                s = vserver.Solver(solver_exe)
                s.cmd(f'load_tree "{j["out"]}"', timeout=3600)
                res = line_result(s, steps)
                cache = True
            except LineError as e:  # the line doesn't fit this tree
                res = {"error": str(e)[:500]}
                cache = True
            except (JobError, vserver.SolverError, OSError, ValueError, IndexError) as e:
                res = {"error": str(e)[:500]}
                if failures is not None:
                    failures[j["id"]] = res["error"]
            finally:
                if s is not None:
                    close_solver(s)
            if cache:
                # cache it in the job as it is now, only if it is still the same finished job (not one
                # removed or retried while the save was read)
                with q.edit():
                    try:
                        cur = q.job(j["id"])
                    except JobError:
                        cur = None
                    if cur and cur["status"] == "done" and cur.get("finished") == j.get("finished"):
                        cr = cur.get("result") or {}
                        lines = dict(cr.get("lines") or {})
                        lines.pop(key, None)
                        lines[key] = res
                        while len(lines) > LINE_CACHE:
                            lines.pop(next(iter(lines)))
                        cur["result"] = {**cr, "lines": lines}
                        q.save(cur)
        row = {"id": j["id"], "board": j["board"], "weight": j["weight"],
               "exploitability": r.get("exploitability"), "cards": None, "error": res.get("error"),
               "root_mass": (r.get("root") or {}).get("mass")}
        if "error" not in res and head is None and res.get("rows"):
            head = {k: res[k] for k in ("player", "actions", "pot", "facing")}
        if "error" not in res and head and res["actions"] == head["actions"] and res["player"] == head["player"]:
            expanded = any(c["cards"] for c in res["rows"])
            row.update(aggregate_cards(res["rows"]))
            for p, hs in (res.get("hands") or {}).items():
                tot = hand_tot.setdefault(p, {})
                for c, a in hs.items():
                    t = tot.setdefault(c, [0.0, 0.0, 0.0])
                    for k in range(3):
                        t[k] += j["weight"] * a[k]
            if expanded:
                row["cards"] = res["rows"]
        else:
            row.update({"freqs": None, "mass": None, "ev_oop": None, "ev_ip": None, "global": None})
            if "error" not in res:
                row["error"] = "a different decision at this line" if res.get("rows") else "the line reaches no node"
        row["texture"] = {k: fn(row["board"]) for k, (_, fn) in TEXTURES.items()}
        rows.append(row)
    if progress:
        progress(read, to_read, None)
    head = head or {"player": None, "actions": [], "pot": start_pot(b["setup"]), "facing": 0}
    n = len(head["actions"])
    total = _weighted(rows, n)
    if total:
        # Global % over the batch: the line's matchups over the root's, summed over the flops (each
        # weighted). So flops average by their root mass, not the line's: what PioViewer's total row
        # shows. The root mass is the root summary's, else the line's mass over its Global %.
        num = den = 0.0
        for x in rows:
            if x["freqs"] is None or x["global"] is None:
                continue
            rm = x.get("root_mass") or (x["mass"] / x["global"] if x["global"] > 0 else None)
            if rm:
                num += x["weight"] * rm * x["global"]
                den += x["weight"] * rm
        if den > 0:
            total["global"] = num / den
    return {"batch": name, "line": key, "pot": head["pot"], "facing": head["facing"], "player": head["player"],
            "hands": hand_rows(hand_tot, head["pot"]),
            "actions": head["actions"],
            "labels": [action_label(a, head["pot"], head["facing"]) for a in head["actions"]],
            "rows": rows, "total": total,
            "groups": {k: group_rows(rows, k, n) for k in TEXTURES} if n else {},
            "group_titles": {k: t for k, (t, _) in TEXTURES.items()},
            "pending": sum(j["status"] != "done" for j in q.jobs(name))}


def hand_rows(tot: dict, pot: float) -> dict:
    """Per player, the hand classes over the batch, best EV first (#336): EV in chips and in % of the
    node's pot, equity, equity realization (EV over equity x pot) and matchups, as in PioViewer's
    handsEV.csv. `tot` holds per class the weighted sums of matchups, matchups x EV, matchups x equity."""
    out = {}
    for p in ("OOP", "IP"):
        rows = []
        for c, (m, mev, meq) in (tot.get(p) or {}).items():
            if m <= 0:
                continue
            ev, eq = mev / m, meq / m
            rows.append({"hand": c, "ev": round(ev, 4), "ev_pct": round(100 * ev / pot, 4) if pot else None,
                         "equity": round(eq, 6), "eqr": round(ev / (eq * pot), 6) if eq > 0 and pot else None,
                         "matchups": round(m, 4)})
        rows.sort(key=lambda x: -x["ev"])
        out[p] = rows
    return out


def write_hands_csv(rep: dict, path: str) -> None:
    """PioViewer's handsEV.csv layout: per player a "Preflop Hands <player>" section of hand classes
    (EV and equity in % of the node's pot, realization in %, matchups), best EV first; plus EV in chips."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"Report at r:0:{rep['line']}" if rep["line"] else "Report at r:0", f"pot {rep['pot']:g}",
                    "EV and equity in % of the pot"])
        for p in ("OOP", "IP"):
            w.writerow([f"Preflop Hands {p}"])
            w.writerow(["Hand", "EV", "Equity", "Equity Realization", "Matchups", "EV chips"])
            for x in (rep.get("hands") or {}).get(p, []):
                w.writerow([x["hand"], x["ev_pct"], round(100 * x["equity"], 6),
                            round(100 * x["eqr"], 6) if x["eqr"] is not None else "", x["matchups"], x["ev"]])
            w.writerow([])


def write_full(q: Queue, name: str, line: str, solver_exe: str, prefix: str, progress=None) -> dict:
    """PioViewer's per-combo report files (#346): PREFIX_OOP_Full.csv and PREFIX_IP_Full.csv, one row per
    flop (and card, at a * step) per combo the player holds there: Flop[,Turn],Hand,Weight <P>,<P> EV
    (% of the node's pot), then the acting player's action frequencies (%), and EV in chips last. Combos
    with no weight or blocked by the node's board are left out, as in PioViewer's files. One streaming
    pass over the saves, nothing cached. Returns {"rows": {OOP: n, IP: n}, "flops": n, "skipped": [...]}."""
    import math
    import server as vserver
    steps = parse_line(line)
    js = [j for j in q.jobs(name) if j["status"] == "done"]
    ncard = steps.count("*")
    card_cols = (["Turn", "River"][:ncard] if ncard <= 2 else [f"Card {i + 1}" for i in range(ncard)])
    files = {p: open(f"{prefix}_{p}_Full.csv", "w", newline="") for p in ("OOP", "IP")}
    writers = {p: csv.writer(f) for p, f in files.items()}
    head = None
    header_written = False
    counts = {"OOP": 0, "IP": 0}
    skipped = []
    done = 0
    try:
        for i, j in enumerate(js):
            if progress:
                progress(i, len(js), j["board"])
            s = None
            try:
                s = vserver.Solver(solver_exe)
                s.cmd(f'load_tree "{j["out"]}"', timeout=3600)
                order = vserver.hand_order(s)
                buf = {"OOP": [], "IP": []}  # this job's rows: written only once all its nodes are read
                for nid, cards in walk_line(s, steps):
                    node = vserver.parse_node_block(s.cmd(f"show_node {nid}"))
                    actor = {"OOP_DEC": "OOP", "IP_DEC": "IP"}.get(node["type"])
                    if actor is None:
                        raise JobError(f"{nid} is not a decision ({node['type']})")
                    acts = [_child_step(k["id"]) for k in vserver.parse_children(s.cmd(f"show_children {nid}"))]
                    potv = [x for x in node["pot"] if x is not None]
                    pot = sum(potv)
                    facing = abs(potv[0] - potv[1]) if len(potv) >= 2 else 0
                    if head is None:
                        head = {"actor": actor, "actions": acts, "labels": [action_label(a, pot, facing) for a in acts]}
                    elif acts != head["actions"] or actor != head["actor"]:
                        continue  # a different decision under one card: left out, as in the line report
                    board = set(node["board"].split())
                    strat = [[float(x) for x in l.split()] for l in s.cmd(f"show_strategy {nid}").splitlines() if l.strip()]
                    # the job's whole board (a batch of turn boards keeps them apart), as PioViewer spaces it
                    flop = " ".join(j["board"][k:k + 2] for k in range(0, len(j["board"]), 2))
                    for p in ("OOP", "IP"):
                        reach = vserver.parse_floats(s.cmd(f"show_range {p} {nid}"), len(order))
                        ev = vserver.ev_pair(s, p, nid)["ev"]
                        for h, combo in enumerate(order):
                            w = reach[h]
                            if not w or w <= 0 or combo[:2] in board or combo[2:] in board:
                                continue
                            v = ev[h]
                            okv = v is not None and math.isfinite(v)
                            row = [flop] + cards + [""] * (ncard - len(cards)) + [
                                combo, round(w, 3), round(100 * v / pot, 4) if okv and pot else ""]
                            if p == actor:
                                row += [round(100 * r[h], 2) for r in strat]
                            row.append(round(v, 4) if okv else "")
                            buf[p].append(row)
                if not header_written:
                    _full_header(writers, card_cols, head)
                    header_written = True
                for p in ("OOP", "IP"):
                    writers[p].writerows(buf[p])
                    counts[p] += len(buf[p])
                done += 1
            except (JobError, vserver.SolverError, OSError, ValueError, IndexError) as e:
                skipped.append({"board": j["board"], "error": str(e)[:300]})
            finally:
                if s is not None:
                    close_solver(s)
        if progress:
            progress(len(js), len(js), None)
        if not header_written:  # every job failed: still a header, without the actions
            _full_header(writers, card_cols, None)
    finally:
        for f in files.values():
            f.close()
    return {"rows": counts, "flops": done, "skipped": skipped}


def _full_header(writers: dict, card_cols: list, head: dict | None) -> None:
    for p in ("OOP", "IP"):
        acts = [f"{l} freq" for l in head["labels"]] if head and p == head["actor"] else []
        writers[p].writerow(["Flop"] + card_cols + ["Hand", f"Weight {p}", f"{p} EV"] + acts + ["EV chips"])


def print_line_report(rep: dict, by: str | None = None, cards: bool = False, csv_path: str | None = None) -> None:
    """The CLI table of a line report; the CSV has PioViewer's layout (Flop, Turn, Global %, EVs, freqs:
    a flop row with an empty card, then its cards' rows, then the weighted total)."""
    labels = rep["labels"]
    print(f"{rep['batch']}: {rep['player'] or '?'} at {'r:0:' + rep['line'] if rep['line'] else 'the root'}, pot {rep['pot']:g}"
          + (f", facing {rep['facing']:g}" if rep["facing"] else ""))
    print(f"{'board':<10} {'weight':>7} {'global %':>9} {'EV OOP':>9} {'EV IP':>9}  " + "  ".join(f"{l:>14}" for l in labels))

    def fmt_row(name, weight, x):
        g = f"{100 * x['global']:.3f}" if x.get("global") is not None else "-"
        eo = f"{x['ev_oop']:.2f}" if x.get("ev_oop") is not None else "-"
        ei = f"{x['ev_ip']:.2f}" if x.get("ev_ip") is not None else "-"
        fr = "  ".join(f"{100 * f:>13.1f}%" for f in x["freqs"]) if x.get("freqs") else x.get("error") or "-"
        return f"{name:<10} {weight:>7} {g:>9} {eo:>9} {ei:>9}  {fr}"
    for x in rep["rows"]:
        print(fmt_row(x["board"], f"{x['weight']:g}", x))
        if cards and x["cards"]:
            for c in x["cards"]:
                print(fmt_row("  " + " ".join(c["cards"]), "", c))
    t = rep["total"]
    if t:
        print(fmt_row("weighted", f"{t['weight']:g}", t) + f"   ({t['flops']} flops)")
    if by and rep["groups"].get(by):
        print(f"\nby {rep['group_titles'][by].lower()}:")
        for g in rep["groups"][by]:
            print(fmt_row(g["key"][:10], f"{g['weight']:g}", g) + f"   ({g['flops']} flops)")
    errs = [x for x in rep["rows"] if x.get("error")]
    if errs:
        print(f"({len(errs)} flop(s) without this line: {errs[0]['board']}: {errs[0]['error']})")
    if rep["pending"]:
        print(f"({rep['pending']} job(s) not done yet are left out)")
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            ncard = max((len(c["cards"]) for x in rep["rows"] for c in (x["cards"] or [])), default=0)
            card_cols = ["Turn", "River"][:ncard] if ncard <= 2 else [f"Card {i + 1}" for i in range(ncard)]
            w.writerow(["Flop"] + card_cols + ["Weight", "Global %", "OOP EV", "IP EV"] +
                       [f"{l} freq" for l in labels])

            def cells(x):
                return ([round(100 * x["global"], 5) if x.get("global") is not None else "",
                         x.get("ev_oop"), x.get("ev_ip")] +
                        ([round(100 * v, 4) for v in x["freqs"]] if x.get("freqs") else [""] * len(labels)))
            for x in rep["rows"]:
                w.writerow([x["board"]] + [""] * ncard + [x["weight"]] + cells(x))
                for c in x["cards"] or []:
                    w.writerow([x["board"]] + c["cards"] + [""] * (ncard - len(c["cards"])) + [""] + cells(c))
            if t:
                w.writerow([""] + [""] * ncard + [t["weight"]] + cells(t))


def close_solver(s) -> None:
    """End a job's solver process and close its pipes."""
    try:
        s.proc.kill()
        s.proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        pass
    for f in (s.proc.stdin, s.proc.stdout):
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass


def check_tree(solver_exe: str, setup: list[str], board: str, smart_tree: bool = False) -> str:
    """Validate a batch's tree on one board without building it; returns the size estimate.

    estimate_schematic_tree expands every line, so a bad tree fails here at `add` instead of failing
    every job of the batch."""
    import server as vserver
    s = vserver.Solver(solver_exe)
    try:
        s.cmds(setup)
        s.cmd(f"set_board {board}")
        return s.cmd(vserver.tree_build_command("estimate_schematic_tree", smart_tree), timeout=600)
    except vserver.SolverError as e:
        raise JobError(f"the tree does not build on {board}: {e}") from None
    finally:
        close_solver(s)


def _stop_request(q: Queue) -> str:
    try:
        with open(q.stop_file) as f:
            return f.read().strip()
    except OSError:
        return ""


class _StopWatch:
    """While a blocking command runs (the setup and build_tree: minutes on a big tree), watch for
    `stop --now` and end the solver process, so the command returns at once (#256). `hit` tells the
    caller that the error it then sees is the stop, not a failure; read it after the with block (the
    join in __exit__ orders it). A stop landing just as the build ends still ends the solver: the
    job goes back to the queue, as asked."""

    def __init__(self, q: "Queue", s, poll: float = 0.2):
        self.q, self.s, self.poll = q, s, poll
        self.hit = False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self):
        while not self._done.wait(self.poll):
            try:
                now = _stop_request(self.q) == "now"
            except Exception:  # noqa: BLE001 - a garbled stop file: keep watching
                now = False
            if now:
                self.hit = True
                try:
                    self.s.proc.kill()
                except Exception:  # noqa: BLE001 - already gone
                    pass
                return

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._done.set()
        self._thread.join()
        return False


def run_job(q: Queue, job: dict, solver_exe: str, threads: int, log=print, progress=None, phase=None, algorithm=None) -> str:
    """Solve one job; returns its final status, or "skipped" if it was removed or changed (no
    longer queued) since the caller listed it. Raises KeyboardInterrupt through (job re-queued).

    progress(job, {"t", "explo", "pct", ...}) is called on each new solver progress report."""
    import server as vserver  # viewer/server.py: the solver session wrapper

    with q.edit():  # claim it: as it is now, not as the caller listed it
        try:
            b = q.batch(job["batch"])
        except JobError as e:  # its batch config is gone: fail it, or the run would pick it again
            gone = q.update(job["id"], expect=lambda j: j["status"] == "queued", status="failed",
                            finished=time.time(), error=str(e))
            if gone is not None:
                log(f"[{job['id']} {job['board']}] FAILED: {e}")
            return "failed" if gone is not None else "skipped"
        job = q.update(job["id"], expect=lambda j: j["status"] == "queued", status="running",
                       started=time.time(), finished=None, error=None, result=None, runner_pid=os.getpid(),
                       runner_start=_proc_start(os.getpid()))
        if job is None:
            return "skipped"
    jid = job["id"]
    tag = f"[{jid} {job['board']}]"
    s = None
    watch = None
    t0 = time.time()

    def requeue(why):
        q.update(jid, status="queued", started=None, runner_pid=None)
        log(f"{tag} {why}; back in the queue")
        return "queued"
    try:
        if phase:
            phase(job, "building")
        s = vserver.Solver(solver_exe)
        with _StopWatch(q, s) as watch:  # a stop --now during the build ends it; the job is re-queued
            s.cmd(f"set_threads {threads}")
            s.cmds(b["setup"])
            if algorithm is not None and (algorithm != "auto" or "gpu" in s.devices):
                s.cmd("set_algorithm " + algorithm)
            s.cmd(f"set_board {job['board']}")
            s.cmd(vserver.tree_build_command("build_tree", b.get("smart_tree")), timeout=7200)
        if watch.hit:  # the build finished just as the stop landed
            return requeue("stopped")
        s.cmd(f"set_accuracy {b['accuracy_pct'] / 100.0:.10g} fraction")
        s.cmd("set_info_freq auto")  # check the target on the viewer's adaptive schedule (#165)
        limit = f" {b['max_iters']} steps" if b.get("max_iters") else \
            f" {b['max_seconds']:g} seconds" if b.get("max_seconds") else ""
        before = s.state()["events"]
        s.cmd("go" + limit)
        if phase:
            phase(job, "solving")
        pot = start_pot(b["setup"]) or 1.0
        last_report = 0.0
        last_point = 0
        stopping = False
        while True:
            st = s.state()
            if st["dead"]:
                raise vserver.SolverError("solver exited")
            if progress:
                with s.state_lock:
                    points = s.series[last_point:]
                    last_point = len(s.series)
                for pt in points:
                    progress(job, dict(pt, pct=100 * pt["explo"] / pot))
            if st["events"] > before and st["solver_status"].startswith("stopped") and not st["solving"]:
                break
            if not stopping and _stop_request(q) == "now":
                s.send_raw("stop")
                stopping = True
            if time.time() - last_report > 5 and st["progress"]:
                pt = vserver.parse_progress_point(st["progress"])
                if pt:
                    log(f"{tag} {pt['t']:.0f} s, exploitable for {pt['explo']:.3f} "
                        f"({100 * pt['explo'] / pot:.3f}% of pot)")
                    last_report = time.time()
            with s.progress_cv:
                s.progress_cv.wait_for(lambda: s.events != st["events"] or s.dead, timeout=0.2)
        if stopping:
            return requeue("stopped")
        if phase:
            phase(job, "saving")
        # calc_results and the save run to the end, also after a stop --now: the solve is done, and
        # keeping it beats solving it again (the run then stops before the next job)
        res = parse_results(s.cmd("calc_results", timeout=3600))
        res["device"] = st["active_device"]
        try:
            res["root"] = root_summary(s)
        except (vserver.SolverError, ValueError, IndexError):
            pass  # a report can still read it from the save later
        try:
            res["iterations"] = int(float(s.cmd("show_iters")))
        except (ValueError, vserver.SolverError):
            pass
        res["seconds"] = round(time.time() - t0, 3)
        os.makedirs(os.path.dirname(job["out"]), exist_ok=True)
        mode = "" if b["save"] == "full" else " " + b["save"]
        cmd = f'dump_tree "{job["out"]}"{mode}'
        if b.get("smart_tree"):
            res["save_mode"] = s.cmd(cmd + " report_mode", timeout=7200)
        else:
            s.cmd(cmd, timeout=7200)
        q.update(jid, status="done", finished=time.time(), result=res, runner_pid=None)
        expl = res.get("exploitability")
        log(f"{tag} done in {res['seconds']:.1f} s, {res.get('iterations', '?')} iterations, exploitable "
            f"for {expl if expl is None else f'{expl:.3f}'} -> {job['out']}")
        return "done"
    except KeyboardInterrupt:
        q.update(jid, status="queued", started=None, runner_pid=None)
        raise
    except Exception as e:  # noqa: BLE001 - any failure fails this job, not the run
        if watch is not None and watch.hit:  # the solver was ended for a stop --now, mid-build
            return requeue("stopped during the tree build")
        q.update(jid, status="failed", finished=time.time(), error=str(e)[:2000], runner_pid=None)
        log(f"{tag} FAILED: {e}")
        return "failed"
    finally:
        if s is not None:
            close_solver(s)


def run(q: Queue, ids: list[str] | None, batch: str | None, limit: int | None, solver_exe: str,
        threads: int, log=print, progress=None, get_algorithm=None) -> dict:
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
            algorithm = get_algorithm() if get_algorithm else None
            status = run_job(q, pending[0], solver_exe, threads, log, progress, algorithm=algorithm)
            if status == "queued":  # stopped with --now: the request is done
                try:
                    os.remove(q.stop_file)
                except FileNotFoundError:
                    pass
                break
            if status == "skipped":  # removed or changed after we listed it: look again
                continue
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
    r.add_argument("--threads", type=int, default=0,
                   help="0 = the solver's default (on x86, all hardware threads or one per core, timed per tree, #276)")

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

    rp = sub.add_parser("report", help="aggregate report: root strategy per flop and weighted average")
    rp.add_argument("name")
    rp.add_argument("--csv", help="also write it as CSV")
    rp.add_argument("--by", choices=sorted(TEXTURES), help="also group the flops by texture: "
                    "high (top card), suits, pairing, straight")
    rp.add_argument("--solver", default=default_solver(),
                    help="reads the saves (a --line report, or older jobs without a root summary)")
    rp.add_argument("--line", default="",
                    help="report at this line instead of the root: a node id like r:0:b198:c:2d:c:b447, where a "
                         "card (or *) means every card dealt there, as in PioViewer's aggregation report")
    rp.add_argument("--cards", action="store_true", help="with --line through a card: also print each card's row")
    rp.add_argument("--full", metavar="PREFIX", help="also write PioViewer's per-combo files PREFIX_OOP_Full.csv and "
                    "PREFIX_IP_Full.csv (weight, EV, the actor's frequencies per combo per flop; reads every save)")
    rp.add_argument("--hands", metavar="FILE", help="also write each hand class's EV, equity, realization and "
                    "matchups over the batch as CSV (PioViewer's handsEV.csv; at the root too, reading the saves)")

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
            n = q.retry(None if args.failed else args.ids)
            print(f"re-queued {n} job(s)")
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
            js = q.jobs(args.name)
            actions = next(((j.get("result") or {}).get("root", {}).get("actions") for j in js
                            if (j.get("result") or {}).get("root")), None) or []
            fields = ["id", "board", "weight", "status", "ev_oop", "ev_ip", "mes_oop", "mes_ip",
                      "exploitability", "iterations", "seconds", "out"] + [f"freq_{a}" for a in actions]
            out = open(args.output, "w", newline="") if args.output else sys.stdout
            w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for j in js:
                r = j.get("result") or {}
                row = {**j, **r}
                root = r.get("root") or {}
                if root.get("actions") == actions:
                    row.update({f"freq_{a}": f for a, f in zip(actions, root["freqs"])})
                w.writerow(row)
            if args.output:
                out.close()
        elif args.cmd == "report" and (parse_line(args.line) or args.hands or args.full):
            exe = os.path.abspath(args.solver) if os.path.isfile(args.solver) else None
            rep = line_report(q, args.name, args.line, exe, root=True)
            print_line_report(rep, args.by, args.cards, args.csv)
            if args.hands:
                write_hands_csv(rep, args.hands)
                print(f"hands: {sum(len(v) for v in rep['hands'].values())} classes -> {args.hands}")
            if args.full:
                if not exe:
                    raise JobError("--full reads the saves: give --solver")
                res = write_full(q, args.name, args.line, exe, args.full,
                                 progress=lambda d, t, b: b and print(f"  full: {d + 1}/{t} {b}", file=sys.stderr))
                print(f"full: {res['rows']['OOP']} OOP and {res['rows']['IP']} IP rows over {res['flops']} flops -> "
                      f"{args.full}_OOP_Full.csv, {args.full}_IP_Full.csv")
                for x in res["skipped"]:
                    print(f"  skipped {x['board']}: {x['error']}")
                if not res["flops"]:
                    raise JobError("--full: no flop could be read (see above)")
        elif args.cmd == "report":
            rep = report(q, args.name, os.path.abspath(args.solver) if os.path.isfile(args.solver) else None)
            labels = rep["labels"]
            head = f"{'board':<10} {'weight':>7} {'EV OOP':>8} {'expl %':>7}  " + "  ".join(f"{l:>14}" for l in labels)
            print(f"{rep['batch']}: {rep['player'] or '?'} at the root, starting pot {rep['pot']:g}")
            print(head)
            pot = rep["pot"] or 1
            for x in rep["rows"]:
                ex = f"{100 * x['exploitability'] / pot:.2f}" if x["exploitability"] is not None else "-"
                fr = "  ".join(f"{100 * f:>13.1f}%" for f in x["freqs"]) if x["freqs"] else "(no root summary)"
                ev = f"{x['ev_oop']:.2f}" if x["ev_oop"] is not None else "-"
                print(f"{x['board']:<10} {x['weight']:>7g} {ev:>8} {ex:>7}  {fr}")
            t = rep["total"]
            if t:
                fr = "  ".join(f"{100 * f:>13.1f}%" for f in t["freqs"])
                ev = f"{t['ev_oop']:.2f}" if "ev_oop" in t else "-"
                print(f"{'weighted':<10} {t['weight']:>7g} {ev:>8} {'':>7}  {fr}   ({t['flops']} flops)")
            if args.by and rep["groups"].get(args.by):
                print(f"\nby {rep['group_titles'][args.by].lower()}:")
                for g in rep["groups"][args.by]:
                    fr = "  ".join(f"{100 * f:>13.1f}%" for f in g["freqs"])
                    ev = f"{g['ev_oop']:.2f}" if "ev_oop" in g else "-"
                    print(f"{g['key'][:18]:<18} {g['weight']:>7g} {ev:>8}  {fr}   ({g['flops']} flops)")
            if rep["pending"]:
                print(f"({rep['pending']} job(s) not done yet are left out)")
            if args.csv:
                with open(args.csv, "w", newline="") as f:
                    w = csv.writer(f)
                    tex = list(TEXTURES)
                    w.writerow(["board", "weight", "ev_oop", "ev_ip", "exploitability"] + labels + tex)
                    for x in rep["rows"]:
                        w.writerow([x["board"], x["weight"], x["ev_oop"], x["ev_ip"], x["exploitability"]] +
                                   (x["freqs"] or [""] * len(labels)) + [x["texture"][k] for k in tex])
                    if t:
                        w.writerow(["weighted", t["weight"], t.get("ev_oop"), t.get("ev_ip"), ""] + t["freqs"] +
                                   [""] * len(tex))
    except JobError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted: the current job is back in the queue", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
