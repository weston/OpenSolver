#!/usr/bin/env python3
"""Tests for viewer/refine.py (fast preview: street refines while the main solve runs).

Usage: python3 viewer/test_refine.py [path-to-solver]

Unit tests for the id helpers, then integration tests against the real solver on the small
tree of viewer/test_resolve.py:
- a refine pauses the running main solve, re-solves the turn subtree, and resumes the main
  solve;
- browse requests inside the refined subtree come from it, with ids in main-tree terms;
- requests above it come from the main tree;
- Stop during a refine keeps the main solve stopped;
- a rebuild drops the refined subtree;
- once the main solve meets its target, the overlay is off;
- a fresh solve with fast preview reports the loading modal's numbers.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))

import server as vsrv  # noqa: E402
import library as vlib  # noqa: E402
import refine as vrefine  # noqa: E402
from test_resolve import CFG, kill_solver  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.join(REPO, "solver")
TURN = "r:0:c:c:Kd"   # first decision after a turn card, below flop check-check


class IdHelpersTest(unittest.TestCase):
    def test_street_root(self):
        self.assertEqual(vrefine.street_root("r:0"), (None, None))
        self.assertEqual(vrefine.street_root("r:0:c:b30"), (None, None))
        self.assertEqual(vrefine.street_root("r:0:c:c:Kd"), ("r:0:c:c:Kd", []))
        self.assertEqual(vrefine.street_root("r:0:b30:c:Kd:c:b60:c:7s"),
                         ("r:0:b30:c:Kd", ["c", "b60", "c", "7s"]))

    def test_rewrite_ids(self):
        d = {"node": {"id": "r:0:c", "type": "IP_DEC"},
             "children": [{"id": "r:0:c:b60"}, {"id": "r:0:c:c"}], "other": "r:0:x"}
        vrefine._rewrite_ids(d, "r:0", "r:0:c:c:Kd")
        self.assertEqual(d["node"]["id"], "r:0:c:c:Kd:c")
        self.assertEqual([k["id"] for k in d["children"]], ["r:0:c:c:Kd:c:b60", "r:0:c:c:Kd:c:c"])
        self.assertEqual(d["other"], "r:0:x")   # only "id" fields


class RefineIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = vsrv.Solver(SOLVER)
        cls.mgr = vrefine.RefineManager(cls.main, vsrv.Solver, SOLVER)
        vsrv.set_refiner(cls.mgr)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.main
        cls.http.library = vlib.Library(tempfile.mkdtemp(prefix="wsolver-test-library-"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"
        st, d = cls.req("/api/generate", "POST", CFG)
        assert st == 200, d

    @classmethod
    def tearDownClass(cls):
        vsrv.set_refiner(None)
        cls.http.shutdown()
        cls.http.server_close()
        for s in (cls.main, cls.mgr.rsolver):
            kill_solver(s)

    @classmethod
    def req(cls, path, method="GET", body=None):
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        r = urllib.request.Request(cls.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=300) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_preview_follows_the_main_solve(self):
        self.assertEqual(self.req("/api/build", "POST")[0], 200)
        # no target: no loading modal
        st, d = self.req("/api/solve", "POST", {"accuracy": 0, "preview": True})
        self.assertNotIn("preview", d)
        self.req("/api/stop", "POST")
        self.main.cmd("wait_for_solver")
        self.assertEqual(self.req("/api/build", "POST")[0], 200)
        st, d = self.req("/api/solve", "POST", {"accuracy": 0.2, "preview": True})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["preview"])
        early_while_solving = False
        deadline = time.time() + 120
        while time.time() < deadline:
            p = self.req("/api/progress")[1]
            pg = p["preview"]
            self.assertEqual(pg["target"], 0.2)
            self.assertAlmostEqual(pg["preview_at"], vrefine.PREVIEW_FACTOR * 0.2)
            if pg["early"] and p["solving"]:
                early_while_solving = True
            if not p["solving"]:
                break
            time.sleep(0.02)
        self.main.cmd("wait_for_solver", timeout=600)
        time.sleep(0.2)
        p = self.req("/api/progress")[1]
        self.assertLessEqual(p["preview"]["explo"], 0.2)
        self.assertTrue(p["preview"]["early"])
        self.assertGreaterEqual(p["preview"]["first"], p["preview"]["explo"])
        if len(self.main.series) > 2:   # the hand-off point came before the end
            self.assertTrue(early_while_solving or self.main.series[-2]["explo"] <= 0.2)
        # seconds per iteration measured from the reports (the browse lease uses it)
        self.assertIsNotNone(self.main.iter_seconds)
        self.assertGreater(self.main.iter_seconds, 0)
        # Resume on a solved tree: no modal again
        st, d = self.req("/api/solve", "POST", {"accuracy": 0.1, "preview": True})
        self.assertNotIn("preview", d)
        self.main.cmd("wait_for_solver", timeout=600)
        # a rebuild forgets the modal's target
        self.assertEqual(self.req("/api/build", "POST")[0], 200)
        self.assertNotIn("preview", self.req("/api/progress")[1])

    def build_and_run_forever(self):
        """A fresh tree and a solve that does not stop by itself (accuracy 0)."""
        self.assertEqual(self.req("/api/build", "POST")[0], 200)
        self.assertEqual(self.req("/api/solve", "POST", {"accuracy": 0})[0], 200)
        deadline = time.time() + 30
        while time.time() < deadline:   # past the first iterations, so ranges are not uniform
            st, d = self.req("/api/progress")
            if (d.get("iters") or 0) >= 20:
                return d["iters"]
            time.sleep(0.05)
        self.fail("main solve did not start")

    def view_and_wait(self, node_id):
        body = dict(CFG, id=node_id, accuracy_pct=0.3)
        st, d = self.req("/api/refine/view", "POST", body)
        self.assertEqual(st, 200, d)
        self.assertIn(d["state"], ("running", "ready"))
        deadline = time.time() + 120
        while time.time() < deadline:
            st, s = self.req("/api/refine/status")
            if not s["busy"]:
                return s
            time.sleep(0.05)
        self.fail("refine did not finish")

    def test_refine_pauses_and_resumes_and_overlays(self):
        it0 = self.build_and_run_forever()
        s = self.view_and_wait(TURN + ":c")
        self.assertIsNone(s["error"])
        self.assertEqual(s["ready"]["root"], TURN)
        self.assertEqual(s["ready"]["sub_root"], "r:0")
        self.assertTrue(s["ready"]["main_paused"])
        # the main solve came back by itself and keeps iterating
        st, p = self.req("/api/progress")
        self.assertTrue(p["solving"])
        self.assertFalse(p["suspended"])
        time.sleep(0.3)
        self.assertGreater(self.req("/api/progress")[1]["iters"], it0)
        # inside the refined subtree: refined payload, main-tree ids
        st, d = self.req("/api/strategy?id=" + TURN + ":c")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["refined"]["root"], TURN)
        self.assertEqual(d["node"]["id"], TURN + ":c")
        self.assertTrue(all(k["id"].startswith(TURN + ":c:") for k in d["children"]))
        self.assertEqual(len(d["freq"]), len(d["children"]))
        # the refined strategy is a strategy: rows sum to 1 for reached combos
        for h, r in enumerate(d["reach"]):
            if r and r > 0:
                self.assertAlmostEqual(sum(row[h] for row in d["freq"]), 1.0, places=3)
                break
        st, d = self.req("/api/range?id=" + TURN)
        self.assertIn("refined", d)
        # above the street root: the main tree
        st, d = self.req("/api/strategy?id=r:0:c")
        self.assertEqual(st, 200, d)
        self.assertNotIn("refined", d)
        # same street again, main stopped (so its exploitability cannot halve meanwhile, which
        # would make the refine stale and start a fresh one): ready, no new job
        self.req("/api/stop", "POST")
        self.main.cmd("wait_for_solver")
        st, d = self.req("/api/refine/view", "POST", dict(CFG, id=TURN, accuracy_pct=0.3))
        if d["state"] != "ready":   # went stale between the refine and the stop
            self.view_and_wait(TURN)
            st, d = self.req("/api/refine/view", "POST", dict(CFG, id=TURN, accuracy_pct=0.3))
        self.assertEqual(d["state"], "ready")

    def test_stop_during_refine_keeps_main_stopped(self):
        self.build_and_run_forever()
        body = dict(CFG, id=TURN, accuracy_pct=0.01)  # tight: the job runs a little while
        self.assertEqual(self.req("/api/refine/view", "POST", body)[0], 200)
        deadline = time.time() + 30
        while time.time() < deadline and not self.req("/api/progress")[1]["suspended"]:
            time.sleep(0.01)
        self.req("/api/stop", "POST")
        deadline = time.time() + 120
        while time.time() < deadline and self.req("/api/refine/status")[1]["busy"]:
            time.sleep(0.05)
        time.sleep(0.3)
        st, p = self.req("/api/progress")
        self.assertFalse(p["solving"])
        self.assertFalse(p["suspended"])

    def test_rebuild_drops_refine_and_target_turns_overlay_off(self):
        self.build_and_run_forever()
        self.view_and_wait(TURN)
        self.assertIn("refined", self.req("/api/strategy?id=" + TURN)[1])
        self.req("/api/stop", "POST")
        self.main.cmd("wait_for_solver")
        self.assertEqual(self.req("/api/build", "POST")[0], 200)
        self.assertIsNone(self.req("/api/refine/status")[1]["ready"])
        self.assertNotIn("refined", self.req("/api/strategy?id=" + TURN)[1])
        # a solve that meets its target: refines are no longer offered or served
        self.main.cmds(["set_accuracy 0.5", "set_info_freq auto", "go", "wait_for_solver"], timeout=600)
        time.sleep(0.2)
        st, d = self.req("/api/refine/view", "POST", dict(CFG, id=TURN, accuracy_pct=0.3))
        self.assertEqual(d["state"], "none")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a.startswith("-")])
