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

        # the copy is used up: the next lock keeps a copy of the solve as it is then, so a later
        # revert doesn't throw away what was solved after this one
        st = self.ok("/api/lock_state?id=" + urllib.parse.quote(FACING))
        self.assertTrue(not st["undo"] and st["can_snapshot"], st)
        self.solve(0.03)
        bet_resolved = self.overall(self.strategy("r:0"), "b24")
        self.ok("/api/lock", "POST", {"id": FACING, "mode": "node", "freq": fold, "snapshot": True})
        self.solve(0.05)
        self.ok("/api/lock/revert", "POST", {})
        self.assertAlmostEqual(self.overall(self.strategy("r:0"), "b24"), bet_resolved, places=6,
                               msg="a second revert goes back to the solve before the second lock")

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
        st = self.ok("/api/lock", "POST", {"id": "r:0", "mode": "current", "snapshot": True})
        self.assertFalse(st["undo"] or st["can_snapshot"], "no 'unlocked' copy of a tree that already has locks")
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

    def test_3b_unlock_all_clears_a_loaded_saves_combo_locks(self):
        # #255: a save's locks are not in the session's list (load forgets it), and unlock_all
        # leaves combo locks (as Pio), so Unlock all needs combo_unlock_all for those
        self.build_and_solve()
        d = self.strategy(FACING)
        order = self.ok("/api/hand_order")["order"]
        aa = [1 if h[0] == "A" and h[2] == "A" else 0 for h in order]
        self.ok("/api/lock", "POST", {"id": FACING, "mode": "combos", "freq": d["freq"], "combos": aa})
        self.ok("/api/lock", "POST", {"id": "r:0", "mode": "current"})
        path = os.path.join(self.tmp, "combo_locked.cfr")
        self.solver.cmd(f'dump_tree "{path}" lossless')
        self.ok("/api/load", "POST", {"path": path})
        st = self.ok("/api/lock_state?id=" + urllib.parse.quote(FACING))
        self.assertTrue(st["combo_locked"] and not st["locks"], "the save's combo lock, not in the session list")
        self.ok("/api/unlock", "POST", {"all": True})
        self.assertFalse(set(self.flags(FACING)) & {"LOCKED", "COMBO_LOCKED"}, "unlock all clears the save's combo lock")
        self.assertFalse(set(self.flags("r:0")) & {"LOCKED", "COMBO_LOCKED"}, "and its node lock")

    def test_4_locks_invalidate_fast_preview(self):
        """A lock changes the tree the refined streets were solved against."""
        class Stub:
            def __init__(self):
                self.calls = 0

            def invalidate(self):
                self.calls += 1

            def overlay(self, path, query):
                return None

        self.build_and_solve()
        refiner = Stub()
        vsrv.set_refiner(refiner)
        try:
            self.ok("/api/lock", "POST", {"id": FACING, "mode": "current", "snapshot": True})
            self.assertEqual(refiner.calls, 1, "lock drops refines")
            self.ok("/api/unlock", "POST", {"id": FACING})
            self.ok("/api/lock/revert", "POST", {})
            self.assertEqual(refiner.calls, 3)
        finally:
            vsrv.set_refiner(None)

    def test_5_spot_locks_export_apply(self):
        """A library spot keeps its locks (/api/lock/export) and sets them again on a fresh build
        of the same tree (/api/lock/apply)."""
        self.build_and_solve()
        d = self.strategy(FACING)
        kids = [c["id"].rsplit(":", 1)[1] for c in d["children"]]
        mix = [[0.8 if k == "f" else 0.2 / (len(kids) - 1)] * NUM for k in kids]
        self.ok("/api/lock", "POST", {"id": FACING, "mode": "node", "freq": mix})
        order = self.ok("/api/hand_order")["order"]
        aa = [1 if h[0] == "A" and h[2] == "A" else 0 for h in order]
        self.ok("/api/lock", "POST", {"id": "r:0", "mode": "combos", "freq": self.strategy("r:0")["freq"],
                                      "combos": aa})
        want = {i: self.strategy(i)["freq"] for i in (FACING, "r:0")}
        exp = self.ok("/api/lock/export")["locks"]
        self.assertEqual({l["id"]: l["mode"] for l in exp}, {FACING: "node", "r:0": "combos"})
        self.assertEqual(next(l for l in exp if l["id"] == "r:0")["combos"], "".join(map(str, aa)))

        # into the library with the spot and back, as the Build tab does
        spot = dict(CFG, schema=1, locks=exp)
        self.ok("/api/library/save", "POST", {"kind": "config", "name": "Locked/spot", "content": spot})
        back = self.ok("/api/library/get?kind=config&name=Locked/spot")["content"]["locks"]
        self.assertEqual(back, exp)
        st, r = self.req("/api/library/save", "POST", {"kind": "config", "name": "Bad", "content": dict(spot, locks=[1])})
        self.assertEqual(st, 400, r)

        self.ok("/api/generate", "POST", CFG)
        self.ok("/api/build", "POST", {})
        self.assertEqual(self.ok("/api/lock_state?id=r:0")["locks"], {}, "a new build has no locks")
        bad = dict(back[0], id="r:0:b24:c")                 # a chance node now: skipped
        other = dict(back[0], actions=["x", "y"])           # different actions: skipped
        r = self.ok("/api/lock/apply", "POST", {"locks": back + [bad, other]})
        self.assertEqual(sorted(r["applied"]), sorted([FACING, "r:0"]))
        self.assertEqual(len(r["skipped"]), 2, r["skipped"])
        self.assertIn("LOCKED", self.flags(FACING))
        self.assertIn("COMBO_LOCKED", self.flags("r:0"))
        for i, w in want.items():
            got = self.strategy(i)["freq"]
            rows = range(NUM) if i == FACING else [j for j in range(NUM) if aa[j]]
            diff = max(abs((w[k][j] or 0) - (got[k][j] or 0)) for k in range(len(w)) for j in rows)
            self.assertLess(diff, 1e-5, f"{i}: the locked strategy comes back (max diff {diff:.2e})")
        self.solve(0.2)
        got = self.strategy(FACING)["freq"]
        self.assertLess(max(abs((a or 0) - (b or 0)) for ra, rb in zip(want[FACING], got) for a, b in zip(ra, rb)),
                        1e-5, "and holds through the solve")

    def test_6_subgame_resolve_keeps_locks(self):
        """Re-solve… (resolve.py) carries the main tree's locks at or below the node."""
        import resolve as vresolve
        mgr = vresolve.ResolveManager(self.solver, vsrv.Solver, SOLVER)
        vsrv.set_resolver(mgr)
        sizing = {k: CFG[k] for k in CFG if k.endswith(("_bet", "_raise"))}

        def resolve(node, **extra):
            self.ok("/api/resolve/start", "POST", dict(sizing, id=node, accuracy=0.2, **extra))
            t0 = time.time()
            while True:
                st = self.ok("/api/resolve/status")
                if not st["busy"]:
                    self.assertIsNone(st["error"], st["error"])
                    return st["result"]
                self.assertLess(time.time() - t0, 300)
                time.sleep(0.1)

        try:
            self.build_and_solve()
            kids = [c["id"].rsplit(":", 1)[1] for c in self.strategy(FACING)["children"]]
            fold = [[1.0 if k == "f" else 0.0] * NUM for k in kids]
            self.ok("/api/lock", "POST", {"id": FACING, "mode": "node", "freq": fold})

            r = resolve("r:0")
            self.assertEqual(r["locks"], {"applied": [FACING], "skipped": []})
            sub = self.ok("/api/resolve/strategy?id=" + FACING)
            self.assertAlmostEqual(self.overall(sub, "f"), 1.0, places=4, msg="the lock holds in the subgame")
            bet_locked = self.overall(self.ok("/api/resolve/strategy?id=r:0"), "b24")
            r = resolve("r:0", locks=False)
            self.assertNotIn("locks", r)
            bet_free = self.overall(self.ok("/api/resolve/strategy?id=r:0"), "b24")
            self.assertGreater(bet_locked, bet_free + 0.2, f"locked {bet_locked:.3f} vs free {bet_free:.3f}")

            # an IP node: the subgame root is r:0:c behind the forced-check wrapper
            r = resolve(FACING)
            self.assertEqual(r["subgame_root"], "r:0:c")
            self.assertEqual(r["locks"]["applied"], [FACING])
            self.assertAlmostEqual(self.overall(self.ok("/api/resolve/strategy?id=r:0:c"), "f"), 1.0, places=4)

            # other sizes: the locked node is not in the new tree, so it is reported, not guessed
            r = resolve("r:0", flop_bet="33")
            self.assertEqual(r["locks"]["applied"], [])
            self.assertEqual([s["id"] for s in r["locks"]["skipped"]], [FACING])
        finally:
            vsrv.set_resolver(None)
            mgr.stop()
            try:
                mgr.rsolver.proc.kill()
                mgr.rsolver.proc.wait(timeout=10)
                for f in (mgr.rsolver.proc.stdin, mgr.rsolver.proc.stdout):
                    f.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
