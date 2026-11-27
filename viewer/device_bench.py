"""Compute-device benchmark for the Settings page (#569): which device solves faster on this machine.

Whether the GPU or the CPU is faster depends on the machine: Metal beats an M5 Pro's CPU cores, while
an RX 9060 XT loses most trees to 32 Zen 5 threads (README, "GPU solving"). So the Settings page can
time both here, on the viewer's own default flop trees, and recommend one.

It runs in a solver process of its own, so the tree being browsed is left alone, and one device at a
time on the same tree, built afresh for each (the GPU algorithm changes the storage the build picks)
with the Smart tree setting the page has, and solved to the same target: the solver's own time to
reach it is compared. (A fixed number of iterations would not do: the CPU prunes, so its iterations
get cheaper as a solve goes on, and the GPU's do not.) The GPU goes first, and a tree too large for its
memory, which the GPU algorithm then solves on the CPU, is noted and not timed. Before the first timed
GPU solve a short one compiles the kernels, which a long-running viewer does only once.

Endpoints (server.py):
    GET  /api/device/benchmark          -> state()
    POST /api/device/benchmark          {smart_tree?} starts it; refused while the main tree solves
    POST /api/device/benchmark/cancel   stops it
"""

import json
import os
import threading
import time

import server as vsrv

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIGS = os.path.join(HERE, "defaults", "configs")

# (label, saved Build config, target as a fraction of the starting pot)
TREES = (
    ("HUNL 4BP", "HUNL 4BP", 0.01),
    ("HUNL 3BP", "HUNL 3BP", 0.01),
    ("HUNL SRP", "HUNL SRP", 0.01),
)
MAX_SECONDS = 90      # per solve: one that takes longer on a device counts as this long there
CLOSE_RATIO = 1.05    # within 5%: about the same, keep what is set


def recommend(results, current):
    """{"device", "ratio", "verdict"} from the timed trees: the device with the smaller total time."""
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


class DeviceBenchmark:
    def __init__(self, main_solver, solver_cls, exe, trees=TREES):
        self.trees = trees
        self.main = main_solver
        self.solver_cls = solver_cls
        self.exe = exe
        self.mu = threading.Lock()
        self.bench = None        # the benchmark's own solver, while it runs
        self.thread = None
        self.cancelled = False
        self.st = {"running": False, "step": None, "done": 0, "total": 0, "results": [], "error": None,
                   "recommendation": None, "finished": None}

    # ---- state ----

    def state(self):
        with self.mu:
            st = json.loads(json.dumps(self.st))
        st["available"] = "gpu" in self.main.devices
        return st

    def _set(self, **kw):
        with self.mu:
            self.st.update(kw)

    # ---- running ----

    def start(self, body):
        if "gpu" not in self.main.devices:
            raise vsrv.SolverError("no GPU to compare with the CPU")
        if self.main.state()["solving"]:
            raise vsrv.SolverError("stop the solve first: it would slow down both devices")
        smart = body.get("smart_tree", True)
        if smart not in (True, False, "single"):
            raise vsrv.SolverError("bad smart_tree")
        with self.mu:
            if self.st["running"]:
                raise vsrv.SolverError("the benchmark is already running")
            self.cancelled = False
            self.st = {"running": True, "step": "Starting a solver", "done": 0, "total": 2 * len(self.trees),
                       "results": [], "error": None, "recommendation": None, "finished": None}
            self.thread = threading.Thread(target=self._run, args=(smart,), daemon=True)
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

    def _setup(self, config):
        """A Build config's setup commands: a saved default by name, or the config itself (tests)."""
        if isinstance(config, str):
            with open(os.path.join(CONFIGS, config + ".json"), encoding="utf-8") as fh:
                config = json.load(fh)
        return vsrv.setup_from_body(config)[0]

    def _solve(self, bench, setup, algorithm, smart, target, steps=None):
        """Build the tree for one device and solve it to `target` (or `steps` iterations): the solve's
        own seconds (its last report's running time, at most MAX_SECONDS), and None or, when the GPU
        algorithm fell back to the CPU (a tree too large for the GPU's memory), the solver's reason,
        that solve being stopped at once."""
        bench.cmd("set_algorithm " + algorithm)
        bench.cmds(setup)   # clear_lines and the lines again: a fresh tree each time
        bench.cmd(vsrv.tree_build_command("build_tree", smart), timeout=600)
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

    def _run(self, smart):
        bench = None
        try:
            bench = self.solver_cls(self.exe)
            with self.mu:
                self.bench = bench
                if self.cancelled:
                    return
            gpu_algorithm = self.main.gpu_algorithm or "v4_gpu"
            results = []
            for i, (label, config, target) in enumerate(self.trees):
                setup = self._setup(config)
                row = {"tree": label, "target": target, "cpu": None, "gpu": None, "gpu_note": None}
                results.append(row)
                if i == 0:
                    self._set(step="Preparing the GPU")
                    self._solve(bench, setup, gpu_algorithm, smart, target, steps=1)
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
                    seconds, note = self._solve(bench, setup, algorithm, smart, target)
                    row[device] = None if seconds is None else round(seconds, 3)
                    if device == "gpu":
                        row["gpu_note"] = note
                    with self.mu:
                        self.st["done"] += 1
                        self.st["results"] = json.loads(json.dumps(results))
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
