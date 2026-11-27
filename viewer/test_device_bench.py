#!/usr/bin/env python3
"""Tests for viewer/device_bench.py (#569: benchmark the CPU and GPU from the Settings page).

Usage: python3 viewer/test_device_bench.py [path-to-solver]

- the recommendation picks the device with the smaller total time, and keeps the current one when
  the two are within 5%;
- the benchmark refuses to start without a GPU, or while the main tree is solving;
- with a GPU: a run over a small tree times both devices in its own solver, leaves the main
  solver's tree and device alone, recommends one, and Cancel stops a run;
- the HTTP routes answer;
- the Auto device's pick interpolates the measured speed-up by tree size, sends trees at least as
  large as one the GPU could not take to the CPU, and is made at /api/build from the tree's size;
- a finished profile is saved, and read back only for the same GPU;
- a tree larger than the GPU takes (the solver's show_gpu) goes to the CPU;
- the benchmark steps aside for a solve on the main tree: a manual run stops with a note, a background
  run waits, and under Auto with no profile it runs by itself once nothing solves;
- switching to Auto with a tree built picks a device for it.
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))

import server as vsrv  # noqa: E402
import device_bench as vbench  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.join(REPO, "solver")

with open(os.path.join(HERE, "defaults", "configs", "HUNL 3BP.json"), encoding="utf-8") as fh:
    SMALL = dict(json.load(fh), range_oop="AA,KK,QQ,AKs", range_ip="JJ,TT,AQs,KQs", board="Ah3cQd")
SMALL_TREES = (("small", SMALL, {}, 0.05),)


def wait_done(bench, seconds=300):
    deadline = time.monotonic() + seconds
    while bench.state()["running"]:
        if time.monotonic() > deadline:
            raise AssertionError("the benchmark did not finish")
        time.sleep(0.05)
    return bench.state()


class RecommendTest(unittest.TestCase):
    def test_faster_total_wins(self):
        rec = vbench.recommend([{"cpu": 1.0, "gpu": 2.0}, {"cpu": 10.0, "gpu": 8.0}], "cpu")
        self.assertEqual(rec["device"], "gpu")   # 11 s against 10 s
        self.assertEqual(rec["verdict"], "faster")
        self.assertEqual(rec["ratio"], 1.1)

    def test_close_keeps_current(self):
        rec = vbench.recommend([{"cpu": 10.0, "gpu": 10.4}], "gpu")
        self.assertEqual((rec["device"], rec["verdict"]), ("gpu", "same"))

    def test_gpu_took_none(self):
        rec = vbench.recommend([{"cpu": 5.0, "gpu": None, "gpu_note": "16-bit storage requires CPU"}], "gpu")
        self.assertEqual((rec["device"], rec["verdict"]), ("cpu", "gpu_unfit"))

    def test_unfit_trees_left_out(self):
        rec = vbench.recommend([{"cpu": 2.0, "gpu": 1.0}, {"cpu": 50.0, "gpu": None, "gpu_note": "too large"}], "cpu")
        self.assertEqual((rec["device"], rec["ratio"]), ("gpu", 2.0))

    def test_predict_by_size(self):
        results = [{"megabytes": 50, "cpu": 1.0, "gpu": 2.0},     # small: the CPU 2x faster
                   {"megabytes": 5000, "cpu": 4.0, "gpu": 2.0},   # large: the GPU 2x faster
                   {"megabytes": 20000, "cpu": None, "gpu": None, "gpu_note": "too large"}]
        self.assertEqual(vbench.predict(results, 10)["device"], "cpu")       # below: the nearest holds
        self.assertEqual(vbench.predict(results, 500)["speedup"], 1.0)       # halfway on log scales
        self.assertEqual(vbench.predict(results, 500)["device"], "cpu")      # a tie goes to the CPU
        self.assertEqual(vbench.predict(results, 3000)["device"], "gpu")
        self.assertEqual(vbench.predict(results, 9000)["device"], "gpu")     # above: the nearest holds
        self.assertEqual(vbench.predict(results, 20000)["device"], "cpu")    # did not fit
        self.assertEqual(vbench.predict([], 500)["device"], "cpu")           # nothing measured
        self.assertEqual(vbench.predict(results, 6000, limit_mb=5500)["device"], "cpu")   # over the GPU's memory
        self.assertIn("too large", vbench.predict(results, 6000, limit_mb=5500)["why"])
        self.assertEqual(vbench.predict(results, 5000, limit_mb=5500)["device"], "gpu")
        self.assertEqual(vbench.tree_megabytes("estimated tree size (v4_large): 2580 MB"), 2580)

    def test_nothing_timed(self):
        self.assertIsNone(vbench.recommend([{"cpu": 1.0, "gpu": None}], "cpu"))


class BenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = vsrv.Solver(SOLVER)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.main.proc.kill()
        except Exception:
            pass

    def bench(self, trees=SMALL_TREES, profile_path=None):
        return vbench.DeviceBenchmark(self.main, vsrv.Solver, SOLVER, trees, profile_path)

    def test_http_route(self):
        b = self.bench()
        vsrv.set_bench(b)
        try:
            status, st = vsrv.api(self.main, None, "GET", "/api/device/benchmark", {}, None)
            self.assertEqual(status, 200)
            self.assertFalse(st["running"])
            self.assertEqual(st["available"], "gpu" in self.main.devices)
        finally:
            vsrv.set_bench(None)

    def test_without_gpu_refused(self):
        if "gpu" in self.main.devices:
            self.skipTest("this machine has a GPU")
        with self.assertRaises(vsrv.SolverError):
            self.bench().start({})

    def test_refused_while_solving(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        b = self.bench()
        self.main.cmds(vsrv.setup_from_body(SMALL)[0])
        self.main.cmd("build_tree")
        self.main.cmds(["set_accuracy 0", "set_info_freq 1000", "go"])
        try:
            deadline = time.monotonic() + 10
            while not self.main.state()["solving"] and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaises(vsrv.SolverError):
                b.start({})
        finally:
            self.main.cmd("stop")
            self.main.cmd("wait_for_solver")

    def test_run_and_recommend(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        self.main.cmds(vsrv.setup_from_body(SMALL)[0])
        self.main.cmd("build_tree")
        nodes = self.main.cmd("node_count")
        device = self.main.device
        b = self.bench()
        b.start({"smart_tree": True})
        st = wait_done(b)
        self.assertIsNone(st["error"], st)
        row = st["results"][0]
        self.assertIsNone(row["gpu_note"], "the small tree fits the GPU")
        self.assertGreater(row["cpu"], 0)
        self.assertGreater(row["gpu"], 0)
        self.assertEqual(st["done"], st["total"])
        self.assertIn(st["recommendation"]["device"], ("cpu", "gpu"))
        self.assertEqual(st["recommendation"]["cpu"], round(row["cpu"], 2))
        self.assertEqual(self.main.cmd("node_count"), nodes, "the main tree is untouched")
        self.assertEqual(self.main.device, device, "the benchmark does not switch the device itself")
        self.assertIsNone(b.bench, "its solver is gone")
        self.assertGreater(row["megabytes"], 0)

    def test_profile_saved_and_read(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profile.json")
            b = self.bench(profile_path=path)
            b.start({})
            self.assertIsNone(wait_done(b)["error"])
            self.assertTrue(os.path.exists(path))
            again = self.bench(profile_path=path)
            st = again.state()
            self.assertTrue(st["profiled"])
            self.assertEqual(st["results"][0]["tree"], "small")
            with open(path, encoding="utf-8") as fh:
                profile = json.load(fh)
            profile["gpu_name"] = "another GPU"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(profile, fh)
            self.assertFalse(self.bench(profile_path=path).state()["profiled"], "another GPU's profile is ignored")

    def test_auto_build_picks_by_size(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        b = self.bench()
        vsrv.set_bench(b)
        saved = (self.main.device, self.main.auto_choice)
        try:
            self.main.device = "auto"
            self.main.cmds(vsrv.setup_from_body(SMALL)[0])
            mb = vbench.tree_megabytes(self.main.cmd(vsrv.tree_build_command("estimate_schematic_tree", None)))
            # a made-up profile: the GPU 3x faster on trees of this size
            b.profile = {"results": [{"megabytes": mb, "cpu": 3.0, "gpu": 1.0}], "gpu_name": self.main.gpu_name}
            vsrv.api(self.main, None, "POST", "/api/build", {}, b"{}")
            self.assertEqual(self.main.auto_choice["device"], "gpu")
            self.assertEqual(self.main.auto_choice["megabytes"], mb)
            self.assertIn("algorithm:" + self.main.gpu_algorithm, self.main.cmd("show_settings"))
            b.profile = {"results": [{"megabytes": mb, "cpu": 1.0, "gpu": 3.0}], "gpu_name": self.main.gpu_name}
            vsrv.api(self.main, None, "POST", "/api/build", {}, b"{}")
            self.assertEqual(self.main.auto_choice["device"], "cpu")
            self.assertIn("algorithm:" + self.main.cpu_algorithm, self.main.cmd("show_settings"))
        finally:
            self.main.device, self.main.auto_choice = saved
            self.main.cmd("set_algorithm " + self.main.device_algorithm())
            vsrv.set_bench(None)

    def test_gpu_limit_reported(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        text = self.main.cmd("show_gpu")
        self.assertIn("gpu: " + self.main.gpu_name, text)
        self.assertTrue(self.main.gpu_tree_limit_mb is None or self.main.gpu_tree_limit_mb > 0)

    def long_solve_on_main(self):
        self.main.cmds(vsrv.setup_from_body(SMALL)[0])
        self.main.cmd("build_tree")
        self.main.cmds(["set_accuracy 0", "set_info_freq 1000", "go"])
        deadline = time.monotonic() + 10
        while not self.main.state()["solving"] and time.monotonic() < deadline:
            time.sleep(0.01)

    def run_until_solving(self, b):
        deadline = time.monotonic() + 60
        while b.state()["step"] in (None, "Starting a solver", "Preparing the GPU") and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_steps_aside_for_a_solve(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                b = self.bench((("small", SMALL, {}, 0),))   # a target never reached
                b.start({}, automatic=automatic)
                self.run_until_solving(b)
                try:
                    self.long_solve_on_main()
                    st = wait_done(b, 30)
                finally:
                    self.main.cmd("stop")
                    self.main.cmd("wait_for_solver")
                if automatic:
                    self.assertIsNone(st["error"])
                    self.assertTrue(st["waiting"])
                else:
                    self.assertIn("a solve started", st["error"])
                self.assertFalse(st["profiled"])

    def test_measures_by_itself_under_auto(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        saved_idle, saved_device = vbench.IDLE_SECONDS, self.main.device
        vbench.IDLE_SECONDS = 0.5
        b = self.bench()
        try:
            self.main.device = "auto"
            b.watch()
            deadline = time.monotonic() + 120
            while not b.state()["profiled"] and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(b.state()["profiled"], "the background run made a profile")
        finally:
            b.close()
            vbench.IDLE_SECONDS = saved_idle
            self.main.device = saved_device

    def test_switch_to_auto_picks_for_the_tree(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        b = self.bench()
        vsrv.set_bench(b)
        saved = (self.main.device, self.main.auto_choice, self.main.tree_present, self.main.loaded_file)
        try:
            self.main.device, self.main.auto_choice = "cpu", None
            self.main.cmds(vsrv.setup_from_body(SMALL)[0])
            vsrv.api(self.main, None, "POST", "/api/build", {}, b"{}")
            self.assertIsNone(self.main.auto_choice, "no pick on the CPU")
            mb = vbench.tree_megabytes(self.main.cmd(vsrv.tree_build_command("estimate_schematic_tree", None)))
            b.profile = {"results": [{"megabytes": mb, "cpu": 3.0, "gpu": 1.0}], "gpu_name": self.main.gpu_name}
            vsrv.api(self.main, None, "POST", "/api/device", {}, {"device": "auto"})
            self.assertEqual(self.main.auto_choice["device"], "gpu")
            self.assertIn("algorithm:" + self.main.gpu_algorithm, self.main.cmd("show_settings"))
        finally:
            self.main.device, self.main.auto_choice, self.main.tree_present, self.main.loaded_file = saved
            self.main.cmd("set_algorithm " + self.main.device_algorithm())
            vsrv.set_bench(None)

    def test_cancel(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        b = self.bench((("small", SMALL, {}, 0),))   # a target never reached
        b.start({})
        deadline = time.monotonic() + 60
        while b.state()["step"] in (None, "Starting a solver", "Preparing the GPU") and time.monotonic() < deadline:
            time.sleep(0.05)
        b.cancel()
        st = wait_done(b, 60)
        self.assertEqual(st["error"], "cancelled")
        self.assertIsNone(st["recommendation"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a.startswith("-")])
