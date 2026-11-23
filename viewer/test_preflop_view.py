#!/usr/bin/env python3
"""The viewer on a preflop tree (#490): one solved by tools/preflop.py and saved with --save-tree.

Usage: python3 viewer/test_preflop_view.py [path-to-solver]

What Study's preflop code (viewer/static/preflop.js and its use in study.js) takes from the
server, against the real solver on a 20 bb tree with three flops: the root has no board and the
blinds as its pot, the deal after a preflop call lists the tree's flops, a flop node starts with
both players' whole preflop money in, EVs are the result of the whole hand (a folded blind is
lost), a re-solve is refused before the flop and offered on it, and a preflop node can be locked.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
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
import resolve as vresolve  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.join(REPO, "solver")

FLOPS = {"Ks8s7h": 40, "QsJh4d": 35, "9s9h3d": 25}
OPEN = "r:0:b250"            # the small blind opens to 2.5 big blinds (100 chips each)
DEAL = OPEN + ":c"           # the big blind calls: the flop is dealt


class PreflopTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="wsolver-test-preflop-view-")
        d = cls.dir.name
        flop_file = os.path.join(d, "three.txt")
        with open(flop_file, "w") as f:
            f.write("".join(f"{k}:{w}\n" for k, w in FLOPS.items()))
        cls.tree = os.path.join(d, "tree.cfr")
        p = subprocess.run([sys.executable, os.path.join(REPO, "tools", "preflop.py"), "solve", os.path.join(d, "out.json"),
                            "--stack", "20", "--flops", flop_file, "--accuracy", "2", "--time", "30", "--solver", SOLVER,
                            "--save-tree", cls.tree], capture_output=True, text=True, timeout=300)
        assert p.returncode == 0 and os.path.exists(cls.tree), p.stderr[-2000:]
        cls.solver = vsrv.Solver(SOLVER)
        cls.mgr = vresolve.ResolveManager(cls.solver, vsrv.Solver, SOLVER)
        vsrv.set_resolver(cls.mgr)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = vlib.Library(os.path.join(d, "library"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"
        st, got = cls.req("/api/load", "POST", {"path": cls.tree})
        assert st == 200, got

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        vsrv.set_resolver(None)
        for sv in (cls.solver, cls.mgr.rsolver):
            try:
                sv.proc.kill()
            except Exception:
                pass
        cls.dir.cleanup()

    @classmethod
    def req(cls, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        r = urllib.request.Request(cls.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=120) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        st, d = self.req(path)
        self.assertEqual(st, 200, (path, d))
        return d

    def node(self, node_id):
        return self.get("/api/node?id=" + urllib.parse.quote(node_id))

    def test_the_page_ships_the_preflop_module(self):
        with urllib.request.urlopen(self.base + "/preflop.js", timeout=10) as f:
            self.assertEqual(f.status, 200)
            self.assertIn("export function preflopRaise", f.read().decode())
        with urllib.request.urlopen(self.base + "/study.js", timeout=10) as f:
            self.assertIn('from "./preflop.js"', f.read().decode())

    def test_root_has_no_board_and_the_blinds(self):
        d = self.node("r:0")
        self.assertEqual((d["node"]["type"], d["node"]["board"]), ("IP_DEC", ""))     # the small blind acts first
        self.assertEqual(d["node"]["pot"], [100.0, 50.0, 0.0])                          # big blind, small blind, dead
        toks = [c["id"].split(":")[-1] for c in d["children"]]
        self.assertEqual(sorted(t for t in toks if not t.startswith("b")), ["c", "f"])
        self.assertIn("b250", toks)
        # amounts on the preflop street are whole investments: the open's node has 250 from the small blind
        self.assertEqual(self.node(OPEN)["node"]["pot"], [100.0, 250.0, 0.0])

    def test_the_deal_after_a_call_lists_the_trees_flops(self):
        d = self.node(DEAL)
        self.assertEqual((d["node"]["type"], d["node"]["board"], d["node"]["pot"]), ("SPLIT_NODE", "", [250.0, 250.0, 0.0]))
        toks = [c["id"].split(":")[-1] for c in d["children"]]
        self.assertEqual(sorted(toks), sorted(FLOPS))
        for c in d["children"]:
            tok = c["id"].split(":")[-1]
            self.assertTrue(re.fullmatch(r"([2-9TJQKA][shdc]){3}", tok), tok)
            self.assertEqual(c["board"].replace(" ", ""), tok)
            # whole-hand accounting goes on after the flop: both have their 250 in
            self.assertEqual(c["pot"], [250.0, 250.0, 0.0])
            self.assertEqual(c["type"], "OOP_DEC")
        path = self.get("/api/path?id=" + urllib.parse.quote(d["children"][0]["id"]))
        self.assertEqual([n["board"].replace(" ", "") for n in path["nodes"]], ["", "", "", toks[0]])
        self.assertEqual(path["eff_stack"], 2000.0)                                      # the whole stack, blinds included

    def folded(self, node_id, player):
        """(EV, how often it folds) of `player`'s hands that all but always fold at `node_id`."""
        s = self.get("/api/strategy?id=" + urllib.parse.quote(node_id))
        self.assertEqual(s["actor"], player)
        fold = [c["id"].split(":")[-1] for c in s["children"]].index("f")
        ev = self.get(f"/api/ev?id={urllib.parse.quote(node_id)}&player={player}")["ev"]
        f = s["freq"][fold]
        return [(ev[i], f[i]) for i in range(len(ev)) if f[i] is not None and f[i] > 0.999 and ev[i] is not None]

    def lost(self, hands, blind):
        # a hand that folds f of the time is at f * -blind + (1 - f) * x, and no line wins or loses
        # more than a stack: the EV is within (1 - f) * (stack + blind) of the blind lost
        for v, f in hands:
            self.assertLessEqual(abs(v + blind), (1 - f) * (2000 + blind) + 1e-3, (v, f))

    def test_evs_are_the_result_of_the_whole_hand(self):
        # Study shows them over the node's pot and says so in the EV tooltip: a fold loses the blind
        sb = self.folded("r:0", "IP")
        self.assertTrue(sb, "no small blind hand always folds")
        self.lost(sb, 50)
        bb = self.folded(OPEN, "OOP")
        self.assertTrue(bb, "no big blind hand always folds to the open")
        self.lost(bb, 100)

    def test_on_a_flop_the_two_evs_cancel(self):
        flop = DEAL + ":Ks8s7h"
        total = {}
        for pl in ("OOP", "IP"):
            d = self.get(f"/api/ev?id={flop}&player={pl}")
            total[pl] = sum(e * m for e, m in zip(d["ev"], d["matchups"]) if e is not None)
        # nothing but the two players' money: what one wins the other loses
        self.assertGreater(abs(total["IP"]), 1.0)
        self.assertLess(abs(total["OOP"] + total["IP"]), 1e-4 * abs(total["IP"]))

    def test_a_resolve_is_for_flops_not_for_preflop_nodes(self):
        # Study turns "Sizes…" off before the flop for this
        for node_id in ("r:0", OPEN):
            st, d = self.req("/api/resolve/context?id=" + node_id)
            self.assertEqual(st, 400, d)
            self.assertIn("preflop", d["error"])
        d = self.get("/api/resolve/context?id=" + DEAL + ":Ks8s7h")
        self.assertEqual((d["board"], d["street"], d["stack"]), ("Ks8s7h", "flop", 2000))
        self.assertEqual(d["pot"], {"oop": 250, "ip": 250, "dead": 0})

    def test_side_panels_answer_before_the_flop(self):
        self.assertEqual(self.get("/api/categories?id=" + OPEN + "&player=OOP")["total"], 0)   # no board, no made hands
        g = self.get("/api/graphs?id=" + OPEN)
        self.assertTrue(g["ok"])
        st, d = self.req("/api/blockers?id=" + DEAL + "&player=OOP")
        self.assertEqual(st, 400, d)                                                          # a deal is not a decision

    def test_zz_a_preflop_node_can_be_locked(self):
        st, d = self.req("/api/lock", "POST", {"id": OPEN, "mode": "current", "snapshot": False})
        self.assertEqual((st, d.get("locked")), (200, True), str(d)[:200])
        self.assertTrue(self.get("/api/lock_state?id=" + OPEN)["locked"])
        st, d = self.req("/api/unlock", "POST", {"id": OPEN})
        self.assertEqual((st, d.get("locked")), (200, False), str(d)[:200])


if __name__ == "__main__":
    unittest.main(argv=["test_preflop_view.py", "-v"] if sys.argv[1:] and not sys.argv[1].startswith("-")
                  else sys.argv)
