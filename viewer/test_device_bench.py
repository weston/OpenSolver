#!/usr/bin/env python3
"""Tests for viewer/device_bench.py (#569: benchmark the CPU and GPU from the Settings page).

Usage: python3 viewer/test_device_bench.py [path-to-solver]

- the recommendation picks the device with the smaller total time, and keeps the current one when
  the two are within 5%;
- the benchmark refuses to start without a GPU, or while the main tree is solving;
- with a GPU: a run over a small tree times both devices in its own solver, leaves the main
  solver's tree and device alone, recommends one, and Cancel stops a run;
- the HTTP routes answer.
"""
import json
import os
import sys
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
SMALL_TREES = (("small", SMALL, 0.05),)


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

    def bench(self, trees=SMALL_TREES):
        return vbench.DeviceBenchmark(self.main, vsrv.Solver, SOLVER, trees)

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

    def test_cancel(self):
        if "gpu" not in self.main.devices:
            self.skipTest("no GPU here")
        b = self.bench((("small", SMALL, 0),))   # a target never reached
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
