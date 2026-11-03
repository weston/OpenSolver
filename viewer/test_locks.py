#!/usr/bin/env python3
"""Node locking from the viewer (#200, part 1) against the real solver.

Usage: python3 viewer/test_locks.py [path-to-solver]

Covers /api/lock (whole node, some combos, as solved), /api/unlock, /api/lock_state, the undo copy
(/api/lock/revert), that a whole-tree re-solve with an over-fold lock moves the bettor's earlier
strategy, and that locks survive dump_tree / load_tree.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import server as vsrv  # noqa: E402
import library as vlib  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(REPO, "solver")
NUM = 1326

CFG = {
    "range_oop": "AA,KK,QQ,JJ,TT,99,AK,AQ,AJs,KQs,KJs,QJs,JTs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,88,77,AK,AQ,AJ,KQ,KJs,QJs,JTs,T9s",
    "board": "Kh9h4c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "", "turn_bet": "60", "turn_raise": "",
    "river_bet": "65", "river_raise": "", "allin_threshold": 50, "add_allin_limit": 300,
}
FACING = "r:0:b24"  # IP facing OOP's flop bet


class LocksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solver = vsrv.Solver(SOLVER)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = vlib.Library(tempfile.mkdtemp(prefix="wsolver-test-locks-"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"
        cls.tmp = tempfile.mkdtemp(prefix="wsolver-test-locks-files-")

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.solver.forget_locks()
        try:
            cls.solver.proc.kill()
        except Exception:
            pass

    def req(self, path, method="GET", body=None):
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=300) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def ok(self, path, method="GET", body=None):
        st, d = self.req(path, method, body)
        self.assertEqual(st, 200, (path, d))
        return d

    def solve(self, accuracy):
        self.ok("/api/solve", "POST", {"accuracy": accuracy})
        t0 = time.time()
        time.sleep(0.2)
        while self.ok("/api/progress")["solving"]:
            self.assertLess(time.time() - t0, 300, "solve did not finish")
            time.sleep(0.1)

    def strategy(self, node_id):
        return self.ok("/api/strategy?id=" + urllib.parse.quote(node_id))

    def overall(self, d, tok):
        """Range-weighted frequency of the child whose id ends in :tok."""
        k = next(i for i, c in enumerate(d["children"]) if c["id"].rsplit(":", 1)[1] == tok)
        tot = sum(w or 0 for w in d["reach"])
        return sum((w or 0) * (d["freq"][k][i] or 0) for i, w in enumerate(d["reach"])) / tot

    def flags(self, node_id):
        return self.ok("/api/node?id=" + urllib.parse.quote(node_id))["node"]["flags"].split()

    def build_and_solve(self):
        self.ok("/api/generate", "POST", CFG)
        self.ok("/api/build", "POST", {})
        self.solve(0.05)

    def test_1_overfold_lock_resolve_and_revert(self):
        self.build_and_solve()
        root = self.strategy("r:0")
        bet_before = self.overall(root, "b24")
        d = self.strategy(FACING)
        fold_before = self.overall(d, "f")
        kids = [c["id"].rsplit(":", 1)[1] for c in d["children"]]
        self.assertIn("f", kids)
        fold = [[1.0 if k == "f" else 0.0] * NUM for k in kids]

        st = self.ok("/api/lock", "POST", {"id": FACING, "mode": "node", "freq": fold, "snapshot": True})
        self.assertTrue(st["locked"] and st["undo"] and st["locks"] == {FACING: "node"}, st)
        self.assertIn("LOCKED", self.flags(FACING))
        self.assertAlmostEqual(self.overall(self.strategy(FACING), "f"), 1.0, places=4)

        self.solve(0.05)  # whole tree: OOP adapts upstream to the over-fold
        self.assertAlmostEqual(self.overall(self.strategy(FACING), "f"), 1.0, places=4, msg="the lock holds")
        bet_after = self.overall(self.strategy("r:0"), "b24")
        print(f"\n  IP fold {fold_before:.3f} -> 1.000 (locked); OOP root bet {bet_before:.3f} -> {bet_after:.3f}")
        self.assertGreater(bet_after, bet_before + 0.2, "OOP bets much more into an over-folder")

        self.ok("/api/lock/revert", "POST", {})
        self.assertNotIn("LOCKED", self.flags(FACING))
        self.assertAlmostEqual(self.overall(self.strategy("r:0"), "b24"), bet_before, places=6,
                               msg="revert brings back the unlocked solve")
        self.assertFalse(self.ok("/api/progress")["loaded_file"], "the reverted tree can be solved further")

    def test_2_combos_unlock_errors(self):
        self.build_and_solve()
        d = self.strategy(FACING)
        order = self.ok("/api/hand_order")["order"]
        aa = [1 if h[0] == "A" and h[2] == "A" else 0 for h in order]
        st = self.ok("/api/lock", "POST", {"id": FACING, "mode": "combos", "freq": d["freq"], "combos": aa})
        self.assertTrue(st["combo_locked"] and not st["locked"], st)
        self.assertEqual(st["combos"], aa)
        self.assertIn("COMBO_LOCKED", self.flags(FACING))
        st = self.ok("/api/lock", "POST", {"id": "r:0", "mode": "current"})
        self.assertTrue(st["locked"] and set(st["locks"]) == {"r:0", FACING})
        self.assertFalse(st["undo"], "no snapshot unless asked")
        st = self.ok("/api/unlock", "POST", {"id": "r:0"})
        self.assertFalse(st["locked"])
        self.ok("/api/unlock", "POST", {"all": True})
        self.assertFalse(set(self.flags(FACING)) & {"LOCKED", "COMBO_LOCKED"}, "unlock all clears combo locks too")

        bad = [
            {"id": "r:0:b24:c", "mode": "current"},                     # a deal, not a decision
            {"id": FACING, "mode": "node", "freq": [[1] * NUM]},         # wrong number of actions
            {"id": FACING, "mode": "node", "freq": [[-1] * NUM] * len(d["children"])},
            {"id": FACING, "mode": "combos", "freq": d["freq"], "combos": [0] * NUM},
            {"id": FACING, "mode": "nope"},
        ]
        for b in bad:
            st, r = self.req("/api/lock", "POST", b)
            self.assertEqual(st, 400, (b, r))
        st, r = self.req("/api/lock/revert", "POST", {})
        self.assertEqual(st, 400, "no undo copy for this tree")

    def test_3_locks_survive_save_load(self):
        self.build_and_solve()
        d = self.strategy(FACING)
        kids = [c["id"].rsplit(":", 1)[1] for c in d["children"]]
        mix = [[0.7 if k == "f" else 0.3 / (len(kids) - 1)] * NUM for k in kids]
        self.ok("/api/lock", "POST", {"id": FACING, "mode": "node", "freq": mix})
        want = self.strategy(FACING)["freq"]
        for mode, tol in (("lossless", 1e-7), ("", 1.0 / 2047 + 1e-6)):
            path = os.path.join(self.tmp, f"locked{mode}.cfr")
            self.solver.cmd(f'dump_tree "{path}" {mode}'.rstrip())
            self.ok("/api/load", "POST", {"path": path})
            self.assertIn("LOCKED", self.flags(FACING), mode or "default save")
            got = self.strategy(FACING)["freq"]
            diff = max(abs((a or 0) - (b or 0)) for ra, rb in zip(want, got) for a, b in zip(ra, rb))
            self.assertLess(diff, tol, f"{mode or 'default'} save: locked strategy kept (max diff {diff:.2e})")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
