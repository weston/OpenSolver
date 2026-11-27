"""Compute-device benchmark and the Auto device (#569): which device solves each tree faster here.

Whether the GPU or the CPU is faster depends on the machine and on the tree: Metal beats an M5 Pro's
CPU cores, while an RX 9060 XT loses most trees to 32 Zen 5 threads (README, "GPU solving"), and a
tree too large for the GPU's memory solves on the CPU whatever is set. So the Settings page can time
both devices here on trees of several sizes, and the Auto device then picks, for each tree it builds,
the one this profile says is faster at that tree's size.

The benchmark runs in a solver process of its own, so the tree being browsed is left alone. Each tree
is built afresh for each device (the GPU algorithm changes the storage the build picks), with Smart
tree building on Medium, and solved to the same target: the solver's own time to reach it is
compared. (A fixed number of iterations would not do: the CPU prunes, so its iterations get cheaper
as a solve goes on, and the GPU's do not.) The GPU goes first, and a tree too large for its memory,
which the GPU algorithm then solves on the CPU, is noted and not timed. Before the first timed GPU
solve a short one compiles the kernels, which a long-running viewer does only once. Each tree's size
is the solver's own estimate of its 32-bit storage, the same figure the Auto device reads before a
build. The profile is saved, so it outlives the viewer.

Endpoints (server.py):
    GET  /api/device/benchmark          -> state()
    POST /api/device/benchmark          starts it; refused while the main tree solves
    POST /api/device/benchmark/cancel   stops it
"""

import json
import math
import os
import re
import threading
import time

import server as vsrv

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIGS = os.path.join(HERE, "defaults", "configs")

# (label, saved Build config, changes to it, target as a fraction of the starting pot), smallest first:
# about 46 MB, 450 MB, 2.6 GB and 7.3 GB of 32-bit storage with Smart tree building on Medium.
TREES = (
    ("Turn, 3-bet pot", "HUNL 3BP", {"turn_card": True}, 0.01),
    ("Flop, 4-bet pot", "HUNL 4BP", {}, 0.01),
    ("Flop, 3-bet pot", "HUNL 3BP", {}, 0.01),
    ("Flop, single-raised pot", "HUNL SRP",
     {"turn_bet": "75", "turn_bet_ip": "75", "turn_raise": "2e", "turn_raise_ip": "2e"}, 0.02),
)
SMART = True          # Medium: the viewer's default
MAX_SECONDS = 90      # per solve: one that takes longer on a device counts as this long there
CLOSE_RATIO = 1.05    # within 5%: about the same
ESTIMATE = re.compile(r"estimated tree size \(v4_large\):\s*([0-9.]+)\s*MB")


def tree_megabytes(estimate_text):
    """The 32-bit size in MB from estimate_schematic_tree's answer, or None."""
    m = ESTIMATE.search(estimate_text or "")
    return float(m.group(1)) if m else None


def recommend(results, current):
    """{"device", "ratio", "verdict"} over all the timed trees: the device with the smaller total time."""
    done = [r for r in results if r.get("cpu") is not None and r.get("gpu") is not None]
    if not done:
        # no tree ran on both: the GPU took none of them (too little memory), so the CPU it is
        if any(r.get("gpu_note") for r in results):
            return {"device": "cpu", "ratio": None, "verdict": "gpu_unfit", "cpu": None, "gpu": None}
        return None
    cpu, gpu = sum(r["cpu"] for r in done), sum(r["gpu"] for r in done)
    if max(cpu, gpu) <= CLOSE_RATIO * min(cpu, gpu):
        return {"device": current, "ratio": round(max(cpu, gpu) / min(cpu, gpu), 2), "verdict": "same",
                "cpu": round(cpu, 2), "gpu": round(gpu, 2)}
    device = "gpu" if gpu < cpu else "cpu"
    return {"device": device, "ratio": round(max(cpu, gpu) / min(cpu, gpu), 2), "verdict": "faster",
            "cpu": round(cpu, 2), "gpu": round(gpu, 2)}


def predict(results, megabytes):
    """The Auto device's pick for a tree of `megabytes` (32-bit): {"device", "speedup", "why"}, where
    speedup is how many times faster the GPU should be (below 1: the CPU is faster).

    Between two measured sizes the GPU's speed-up is interpolated on log scales; outside them the
    nearest measurement holds. A tree at least as large as one the GPU could not take goes to the CPU,
    as does any tree when nothing was timed on both. Ties (within 5%) go to the CPU, which needs
    no setup and takes every tree."""
    timed = sorted((r for r in results if r.get("cpu") and r.get("gpu") and r.get("megabytes")),
                   key=lambda r: r["megabytes"])
    unfit = [r["megabytes"] for r in results if r.get("gpu_note") and r.get("megabytes")]
    if megabytes is None or not timed:
        return {"device": "cpu", "speedup": None, "why": "no benchmark of both devices yet"}
    if unfit and megabytes >= min(unfit):
        return {"device": "cpu", "speedup": None, "why": "too large for the GPU's memory"}
    x = math.log(max(megabytes, 1e-3))
    pts = [(math.log(r["megabytes"]), math.log(r["cpu"] / r["gpu"])) for r in timed]
    if x <= pts[0][0]:
        y = pts[0][1]
    elif x >= pts[-1][0]:
        y = pts[-1][1]
    else:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= x <= x1:
                y = y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y0
                break
    speedup = math.exp(y)
    if speedup > CLOSE_RATIO:
        return {"device": "gpu", "speedup": round(speedup, 2), "why": "the GPU is faster on trees this size here"}
    why = "the CPU is faster on trees this size here" if speedup < 1 / CLOSE_RATIO         else "about as fast on trees this size here; the CPU needs no setup"
    return {"device": "cpu", "speedup": round(speedup, 2), "why": why}


class DeviceBenchmark:
    def __init__(self, main_solver, solver_cls, exe, trees=TREES, profile_path=None):
        self.trees = trees
        self.main = main_solver
        self.solver_cls = solver_cls
        self.exe = exe
        self.profile_path = profile_path
        self.mu = threading.Lock()
        self.bench = None        # the benchmark's own solver, while it runs
        self.thread = None
        self.cancelled = False
        self.st = {"running": False, "step": None, "done": 0, "total": 0, "results": [], "error": None,
                   "recommendation": None, "finished": None}
        self.profile = self._load()   # the last finished benchmark: {"results", "finished", "gpu_name"}

    # ---- the saved profile ----

    def _load(self):
        if not self.profile_path:
            return None
        try:
            with open(os.path.expanduser(self.profile_path), encoding="utf-8") as fh:
                profile = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(profile, dict) or not isinstance(profile.get("results"), list):
            return None
        if profile.get("gpu_name") != self.main.gpu_name:
            return None   # measured with another GPU (or none): it says nothing of this one
        return profile

    def _save(self, profile):
        if not self.profile_path:
            return
        path = os.path.expanduser(self.profile_path)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(profile, fh, indent=1)
            os.replace(path + ".tmp", path)
        except OSError:
            pass   # kept in memory for this session

    def choose(self, megabytes):
        """The Auto device's pick for a tree of this size (see predict)."""
        with self.mu:
            results = list(self.profile["results"]) if self.profile else []
        return predict(results, megabytes)

    # ---- state ----

    def state(self):
        with self.mu:
            st = json.loads(json.dumps(self.st))
            profile = json.loads(json.dumps(self.profile)) if self.profile else None
        if not st["running"] and not st["results"] and not st["error"] and profile:
            st.update(results=profile["results"], finished=profile.get("finished"),
                      recommendation=recommend(profile["results"], self.main.device))
        st["available"] = "gpu" in self.main.devices
        st["profiled"] = bool(profile)
        return st

    def _set(self, **kw):
        with self.mu:
            self.st.update(kw)

    # ---- running ----

    def start(self, body=None):
        if "gpu" not in self.main.devices:
            raise vsrv.SolverError("no GPU to compare with the CPU")
        if self.main.state()["solving"]:
            raise vsrv.SolverError("stop the solve first: it would slow down both devices")
        with self.mu:
            if self.st["running"]:
                raise vsrv.SolverError("the benchmark is already running")
            self.cancelled = False
            self.st = {"running": True, "step": "Starting a solver", "done": 0, "total": 2 * len(self.trees),
                       "results": [], "error": None, "recommendation": None, "finished": None}
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self.state()

    def cancel(self):
        with self.mu:
            self.cancelled = True
            bench = self.bench
        if bench is not None:
            try:
                bench.proc.kill()   # the quickest way out of a solve; the thread then finishes
            except Exception:
                pass
        return self.state()

    def close(self):
        self.cancel()

    @staticmethod
    def _config(name, changes):
        """A Build config: a saved default by name (with changes), or the config itself (tests)."""
        if isinstance(name, dict):
            return dict(name, **changes)
        with open(os.path.join(CONFIGS, name + ".json"), encoding="utf-8") as fh:
            config = json.load(fh)
        changes = dict(changes)
        if changes.pop("turn_card", False):   # the same spot one card later: a turn tree
            board = config["board"]
            config["board"] = board + next(c for c in ("7s", "2d", "9h", "4c") if c not in board)
        return dict(config, **changes)

    def _solve(self, bench, setup, algorithm, target, steps=None):
        """Build the tree for one device and solve it to `target` (or `steps` iterations): the solve's
        own seconds (its last report's running time, at most MAX_SECONDS), and None or, when the GPU
        algorithm fell back to the CPU (a tree too large for the GPU's memory), the solver's reason,
        that solve being stopped at once."""
        bench.cmd("set_algorithm " + algorithm)
        bench.cmds(setup)   # clear_lines and the lines again: a fresh tree each time
        bench.cmd(vsrv.tree_build_command("build_tree", SMART), timeout=600)
        bench.cmds([f"set_accuracy {target:g} fraction", "set_info_freq 5"])
        with bench.state_lock:
            bench.device_message = None
            bench.solver_status = ""
        bench.cmd(f"go {steps} steps" if steps else f"go {MAX_SECONDS} seconds")
        if algorithm != "auto":
            with bench.progress_cv:
                bench.progress_cv.wait_for(lambda: bench.device_message or bench.solver_status.startswith("stopped")
                                           or bench.dead, timeout=600)
                message = bench.device_message or ""
            if message.startswith("device cpu"):
                bench.cmd("stop")
                bench.cmd("wait_for_solver", timeout=600)
                reason = message[len("device cpu"):].strip(" ()")
                return None, reason or "the GPU could not take this tree"
        bench.cmd("wait_for_solver", timeout=MAX_SECONDS + 600)
        with bench.state_lock:
            block = list(bench.progress)
        seconds = next((float(line.split(":", 1)[1]) for line in block if line.startswith("running time:")), None)
        if seconds is None:
            raise vsrv.SolverError("the solve gave no report")
        return min(seconds, MAX_SECONDS), None

    def _run(self):
        bench = None
        try:
            bench = self.solver_cls(self.exe)
            with self.mu:
                self.bench = bench
                if self.cancelled:
                    return
            gpu_algorithm = self.main.gpu_algorithm or "v4_gpu"
            results = []
            for i, (label, name, changes, target) in enumerate(self.trees):
                setup = vsrv.setup_from_body(self._config(name, changes))[0]
                bench.cmds(setup)
                megabytes = tree_megabytes(bench.cmd(vsrv.tree_build_command("estimate_schematic_tree", SMART)))
                row = {"tree": label, "megabytes": megabytes, "target": target, "cpu": None, "gpu": None,
                       "gpu_note": None}
                results.append(row)
                if i == 0:
                    self._set(step="Preparing the GPU")
                    self._solve(bench, setup, gpu_algorithm, target, steps=1)
                for device, algorithm in (("gpu", gpu_algorithm), ("cpu", "auto")):
                    if self.cancelled:
                        return
                    if row["gpu_note"]:   # the GPU cannot take it: nothing to compare
                        with self.mu:
                            self.st["done"] += 1
                        continue
                    if self.main.state()["solving"]:
                        raise vsrv.SolverError("a solve started during the benchmark: run it again when nothing is solving")
                    self._set(step=f"{label} on the {device.upper()}")
                    seconds, note = self._solve(bench, setup, algorithm, target)
                    row[device] = None if seconds is None else round(seconds, 3)
                    if device == "gpu":
                        row["gpu_note"] = note
                    with self.mu:
                        self.st["done"] += 1
                        self.st["results"] = json.loads(json.dumps(results))
            finished = time.time()
            profile = {"results": results, "finished": finished, "gpu_name": self.main.gpu_name}
            with self.mu:
                self.profile = profile
            self._save(profile)
            self._set(recommendation=recommend(results, self.main.device))
        except Exception as e:  # noqa: BLE001 - reported on the page
            if not self.cancelled:
                self._set(error=str(e) or e.__class__.__name__)
        finally:
            with self.mu:
                self.bench = None
                self.st["running"] = False
                self.st["step"] = None
                self.st["finished"] = time.time()
                if self.cancelled and not self.st["error"]:
                    self.st["error"] = "cancelled"
            if bench is not None:
                try:
                    bench.proc.kill()
                except Exception:
                    pass

    # ---- HTTP ----

    def api(self, method, path, body):
        if path == "/api/device/benchmark" and method == "GET":
            return 200, self.state()
        if path == "/api/device/benchmark" and method == "POST":
            return 200, self.start(vsrv.need_body(body))
        if path == "/api/device/benchmark/cancel" and method == "POST":
            return 200, self.cancel()
        raise vsrv.SolverError("unknown benchmark request")
