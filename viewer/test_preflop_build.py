#!/usr/bin/env python3
"""Heads-up preflop trees from the Build tab (#492): viewer/preflop_api.py against the real solver.

Usage: python3 viewer/test_preflop_build.py [path-to-solver]

The form's defaults are the tool's; a form is refused in the form's words before any solver is asked;
the tree in words, the megabytes and the tree built are the ones tools/preflop.py gives for the same
settings; a preflop tree solves to an accuracy in bb/100; and a postflop tree built after a preflop
one is the tree a viewer that never built a preflop tree builds.
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

import server as vsrv  # noqa: E402
import library as vlib  # noqa: E402
import resolve as vresolve  # noqa: E402
import refine as vrefine  # noqa: E402
import preflop_api  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.join(REPO, "solver")

FORM = {"stack": "20", "flops": "10"}   # everything else: the tool's defaults
ARGS = ["--stack", "20"]                # the same settings on the tool's command line
# A postflop tree that shows both settings a preflop build switches on: the turn and river are checked
# down (collapsed check-downs would make it 16 nodes, not 13,201), and 20 iterations in 16-bit storage
# end a little off the 32-bit ones.
POSTFLOP = {"range_oop": "AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQs", "range_ip": "AA,KK,QQ,JJ,TT,99,AK,AQ,KQs",
            "board": "QhQc2c", "pot": 40, "stack": 60, "flop_bet": "60", "flop_raise": "50",
            "turn_bet": "", "turn_raise": "", "river_bet": "", "river_raise": "",
            "allin_threshold": 50, "add_allin_limit": 300, "iso": True}


class Viewer:
    """A viewer of its own: solver, resolver, the fast preview's refiner and HTTP server."""

    def __init__(self, directory):
        self.solver = vsrv.Solver(SOLVER)
        self.mgr = vresolve.ResolveManager(self.solver, vsrv.Solver, SOLVER)
        self.refiner = vrefine.RefineManager(self.solver, vsrv.Solver, SOLVER)
        self.use()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        self.http.solver = self.solver
        self.http.library = vlib.Library(directory)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def use(self):
        """The server module serves one viewer: this one from now on."""
        vsrv.set_resolver(self.mgr)
        vsrv.set_refiner(self.refiner)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        vsrv.set_resolver(None)
        vsrv.set_refiner(None)
        for sv in (self.solver, self.mgr.rsolver, self.refiner.rsolver):
            try:
                sv.proc.kill()
            except Exception:
                pass

    def req(self, path, method="GET", body=None, timeout=300):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def solve(self, body, limit=120):
        """POST /api/solve and wait for the solve: (status, answer, the state after it). The request
        answers before the solver says it started, and until then the state is "not solving" with no
        new point: that is not "solved". Seen solving, or a new point, is."""
        before, seen = len(self.solver.series), False
        st, d = self.req("/api/solve", "POST", body)
        if st != 200:
            return st, d, None
        end = time.time() + limit
        while time.time() < end:
            state = self.req("/api/status")[1]
            seen = seen or state["solving"]
            if not state["solving"] and (seen or len(self.solver.series) > before or state.get("dead")):
                return st, d, state
            time.sleep(0.05)
        raise AssertionError("the solve did not start and stop")

    def wait_solved(self, limit=120):
        end = time.time() + limit
        while time.time() < end:
            st, d = self.req("/api/status")
            if st == 200 and not d["solving"]:
                return d
            time.sleep(0.2)
        raise AssertionError("the solve did not stop")


class PreflopBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="wsolver-test-preflop-build-")
        cls.v = Viewer(os.path.join(cls.dir.name, "library"))
        cls.pf = preflop_api.tool()

    @classmethod
    def tearDownClass(cls):
        cls.v.close()
        cls.dir.cleanup()

    def tool_settings(self, args=ARGS):
        return self.pf.settings_from(self.pf.parser().parse_args(["tree"] + args))

    def tool_tree(self, lines):
        """What the tool's script builds in a solver that has done nothing else: (node_count, the root's lines)."""
        with self.pf.Solver(SOLVER) as u:
            for line in lines:
                u.need(line)
            u.need("add_all_flops")
            return "\n".join(u.cmd("node_count")), "\n".join(u.cmd("show_children r:0"))

    def test_01_defaults_are_the_tools(self):
        st, d = self.v.req("/api/preflop/defaults")
        self.assertEqual(st, 200, d)
        a = self.pf.parser().parse_args(["tree"])
        f = d["form"]
        self.assertEqual((f["stack"], f["ante"], f["open"], f["iso"], f["threebet"], f["fourbet"], f["fivebet"]),
                         (a.stack, a.ante, a.open, a.iso, a.threebet, a.fourbet, a.fivebet))
        self.assertEqual((f["limp"], f["limp_raise"], f["depth"], f["raise_cap"], f["flop_bet"], f["raise"]),
                         (True, "", "flop", a.raise_cap, a.flop_bet, a.raise_))
        self.assertEqual(f["range_sb"], self.pf.ALL_HANDS)
        self.assertEqual(f["accuracy"], self.pf.SOLVE_ACCURACY)   # not the check's, and not None (#497)
        self.assertEqual(d["flops"], [str(c) for c in self.pf.SHIPPED] + ["all"])
        self.assertIn(f["flops"], d["flops"])
        self.assertEqual(d["chips"], 100)
        self.assertGreater(d["memory_mb"], 0)
        # the defaults, sent back, are the tool's default tree
        st, got = self.v.req("/api/preflop/describe", "POST", f)
        self.assertEqual(st, 200, got)
        s = self.tool_settings([])
        self.assertEqual(got["rows"], self.pf.tree_rows(s, self.pf.build_tree(s)))

    def test_02_tree_in_words(self):
        st, d = self.v.req("/api/preflop/describe", "POST", FORM)
        self.assertEqual(st, 200, d)
        s = self.tool_settings()
        rows = self.pf.tree_rows(s, self.pf.build_tree(s))
        self.assertEqual(d["rows"], rows)
        self.assertEqual((d["flops"], d["chips"]), (10, 100))
        self.assertEqual(d["exits"], sum(1 for r in rows if r["kind"] == "flop"))
        self.assertEqual(rows[0]["id"], "r:0")
        # every field reaches the tool: a tree without the limp, with two opens, another 3-bet, no 4-bet
        form = dict(FORM, limp=False, open="2, 3", threebet="3x", fourbet="", limp_raise="none", ante="0.5",
                    allin_threshold="40", depth="turn", turn_bet="33, 75", raise_cap=3, donk="25")
        st, d = self.v.req("/api/preflop/describe", "POST", form)
        self.assertEqual(st, 200, d)
        s = self.tool_settings(["--stack", "20", "--no-limp", "--open", "2, 3", "--3bet", "3x", "--4bet", "",
                                "--limp-raise", "", "--ante", "0.5", "--allin-threshold", "40", "--depth", "turn",
                                "--turn-bet", "33,75", "--raise-cap", "3", "--donk", "25"])
        self.assertEqual(d["rows"], self.pf.tree_rows(s, self.pf.build_tree(s)))
        ids = [r["id"] for r in d["rows"]]
        self.assertIn("r:0:b200", ids)
        self.assertIn("r:0:b300", ids)
        self.assertNotIn("r:0:c", ids)

    def test_03_refused_in_the_forms_words(self):
        cases = [({"open": "1.2"}, "Open 1.2"), ({"iso": "0.5"}, "Raise over a limp 0.5"),
                 ({"threebet": "zz"}, "3-bet: 'zz'"), ({"fourbet": "1x"}, "4-bet"), ({"stack": "abc"}, "Stack"),
                 ({"stack": "900"}, "Stack: at most 655"), ({"ante": "-1"}, "Ante"),
                 ({"allin_threshold": "150"}, "All-in threshold"), ({"limp": False, "open": ""}, "Open"),
                 ({"flop_bet": "3x"}, "Flop bet"), ({"raise": "abc"}, "Raise"), ({"raise_cap": 0}, "Cap"),
                 ({"raise_cap": "2"}, "Cap"), ({"depth": "showdown"}, "Betting through"), ({"limp": "yes"}, "Limp"),
                 ({"range_sb": ""}, "small blind's range"), ({"range_bb": "AA\nset_threads 1"}, "big blind's range"),
                 ({"open": "2.5\rexit"}, "Open"), ({"stack": ["20"]}, "Stack"),
                 # never a path: the tool's --flops reads a file, the form's choice is a shipped subset or all
                 ({"flops": "/etc/passwd"}, "Flops"), ({"flops": "flops/wsolver_10.txt"}, "Flops"),
                 ({"flops": "11"}, "Flops")]
        for path in ("/api/preflop/describe", "/api/preflop/estimate", "/api/preflop/build"):
            for change, want in cases:
                st, d = self.v.req(path, "POST", dict(FORM, **change))
                self.assertEqual(st, 400, (path, change, d))
                self.assertIn(want, d["error"], (path, change))
                self.assertNotIn("--", d["error"], (path, change))
        st, d = self.v.req("/api/status")
        self.assertFalse(d["tree"], "a refused build leaves the solver alone")
        self.assertEqual(self.v.req("/api/preflop/build")[0], 400)   # GET
        self.assertEqual(self.v.req("/api/preflop/build", "POST", [1])[0], 400)

    def test_04_estimate_is_the_tools(self):
        s = self.tool_settings()
        name, flops = self.pf.choose_flops("10")
        want = self.pf.estimate_mb(SOLVER, s, flops)
        st, d = self.v.req("/api/preflop/estimate", "POST", FORM)
        self.assertEqual(st, 200, d)
        self.assertEqual((d["mb"], d["flops"]), (want, 10))
        self.assertEqual(d["memory_mb"], self.pf.physical_memory_mb())
        key_count = len(preflop_api.ESTIMATES.cache)
        t = time.time()
        st, again = self.v.req("/api/preflop/estimate", "POST", FORM)
        self.assertEqual(again, d)
        self.assertEqual(len(preflop_api.ESTIMATES.cache), key_count, "the second answer is the remembered one")
        self.assertLess(time.time() - t, 0.5)
        # more flops, more memory
        st, more = self.v.req("/api/preflop/estimate", "POST", dict(FORM, flops="25"))
        self.assertEqual(st, 200, more)
        self.assertGreater(more["mb"], d["mb"])

    def test_05_ranges_that_cannot_be_dealt(self):
        # every hand of each holds the ace of spades and the ace of hearts
        form = dict(FORM, range_sb="AsAh", range_bb="AsAh")
        for path in ("/api/preflop/estimate", "/api/preflop/build"):
            st, d = self.v.req(path, "POST", form)
            self.assertEqual(st, 400, d)
            self.assertIn("no deal", d["error"])
        self.assertFalse(self.v.req("/api/status")[1]["tree"])

    def test_06_build_is_the_tools_tree(self):
        st, d = self.v.req("/api/preflop/build", "POST", FORM)
        self.assertEqual(st, 200, d)
        self.assertEqual((d["chips"], d["flops"]), (100, 10))
        # the size the form shows after a build is the build's own answer, the Estimate button's figure
        st, e = self.v.req("/api/preflop/estimate", "POST", FORM)
        self.assertEqual((d["mb"], d["memory_mb"]), (e["mb"], e["memory_mb"]))
        s = self.tool_settings()
        root = self.pf.build_tree(s)
        count, kids = self.tool_tree(self.pf.script(s, self.pf.choose_flops("10")[1], root))
        self.assertEqual(d["node_count"], count)
        self.assertEqual(self.v.solver.cmd("show_children r:0"), kids)
        self.assertEqual(self.v.solver.cmd("show_node r:0").split("\n")[3].split(), ["100", "50", "0"])
        self.assertTrue(self.v.req("/api/status")[1]["tree"])

    def test_07_solve_to_bb_per_100(self):
        # 2 bb/100 of a 100-chip big blind: 2 chips
        st, d, status = self.v.solve({"accuracy": 2.0, "preview": True})
        self.assertEqual(st, 200, d)
        self.assertIsNone(vsrv._preview, "no board to preview a preflop solve on")
        self.assertFalse(status.get("dead"))
        explo = float(self.v.solver.cmd("calc_results").split("Exploitable for:")[1].split()[0])
        self.assertLessEqual(explo, 2.0)
        ev = self.v.solver.cmd("calc_ev IP r:0")
        self.assertNotIn("error", ev.lower())

    def test_08_postflop_after_preflop_is_the_same_tree(self):
        def postflop(v):
            st, d = v.req("/api/generate", "POST", POSTFLOP)
            self.assertEqual(st, 200, d)
            st, d = v.req("/api/build", "POST", {})
            self.assertEqual(st, 200, d)
            count = d["node_count"]
            v.solver.cmd("set_accuracy 0")   # the solve before left its own target
            v.solver.cmd("go 20 steps")
            v.solver.cmd("wait_for_solver")
            return count, v.solver.cmd("show_tree_info"), v.solver.cmd("calc_results"), v.solver.cmd("show_strategy r:0")

        self.assertTrue(self.v.solver.preflop_touched)
        after = postflop(self.v)
        self.assertIn("TOTAL: 13201", after[0])
        self.assertFalse(self.v.solver.preflop_touched)
        self.assertIsNone(self.v.solver.preflop)
        fresh = Viewer(os.path.join(self.dir.name, "library2"))
        try:
            self.assertEqual(after, postflop(fresh))
            # and the other way round: a preflop tree after a postflop one is the tool's
            st, d = fresh.req("/api/preflop/build", "POST", FORM)
            self.assertEqual(st, 200, d)
            s = self.tool_settings()
            count, kids = self.tool_tree(self.pf.script(s, self.pf.choose_flops("10")[1], self.pf.build_tree(s)))
            self.assertEqual(d["node_count"], count)
        finally:
            fresh.close()
            self.v.use()

    def test_09_load_and_restart_forget_the_preflop_tree(self):
        st, d = self.v.req("/api/preflop/build", "POST", FORM)
        self.assertEqual(st, 200, d)
        self.assertEqual(self.v.solver.preflop, {"chips": 100, "flops": 10})
        # a file loaded next is stored as the solver chooses, not in the preflop build's 16 bits
        path = os.path.join(self.dir.name, "pf.cfr")
        count = self.v.solver.cmd("node_count")
        self.assertEqual(self.v.req("/api/save", "POST", {"path": path})[0], 200)
        sent = []
        cmds = self.v.solver.cmds
        self.v.solver.cmds = lambda lines, **kw: (sent.extend(lines), cmds(lines, **kw))[1]
        try:
            st, d = self.v.req("/api/load", "POST", {"path": path})
        finally:
            del self.v.solver.cmds
        self.assertEqual(st, 200, d)
        self.assertEqual(sent[2:4], ["small_strats auto", "set_collapse_checkdowns off"])
        self.assertIsNone(self.v.solver.preflop)
        self.assertFalse(self.v.solver.preflop_touched)
        self.assertEqual(self.v.solver.cmd("show_node r:0").split("\n")[3].split(), ["100", "50", "0"])
        self.assertEqual(self.v.solver.cmd("node_count"), count, "the saved tree comes back whole")
        st, d = self.v.req("/api/preflop/build", "POST", FORM)
        self.assertEqual(st, 200, d)
        st, d = self.v.req("/api/restart", "POST", {})
        self.assertEqual(st, 200, d)
        self.assertIsNone(self.v.solver.preflop)
        self.assertFalse(self.v.solver.preflop_touched)

    def test_10_no_street_of_a_preflop_tree_is_refined(self):
        """Study asks for a refined street at every turn and river node, with the Build form's postflop
        sizes (the fast preview, #445): a preflop tree's streets have their own sizes."""
        st, d = self.v.req("/api/preflop/build", "POST", dict(FORM, depth="turn"))
        self.assertEqual(st, 200, d)
        node = "r:0"
        while vrefine.street_root(node)[0] is None:   # calls and checks, down to the first turn card
            st, d = self.v.req("/api/node?id=" + node)
            self.assertEqual(st, 200, d)
            kids = [k["id"] for k in d["children"]]
            node = next((k for k in kids if k.endswith(":c")), kids[0])
            self.assertLess(node.count(":"), 12, node)
        st, d = self.v.req("/api/solve", "POST", {"accuracy": 1e-9})
        self.assertEqual(st, 200, d)
        try:
            self.assertTrue(self.v.solver.state()["solving"])
            body = dict(POSTFLOP, id=node, accuracy_pct=0.3)
            st, d = self.v.req("/api/refine/view", "POST", body)
            self.assertEqual((st, d.get("state")), (200, "none"), (node, d))
            time.sleep(1.0)
            status = self.v.req("/api/refine/status")[1]
            self.assertEqual((status["busy"], status["root"], status["ready"]), (False, None, None), status)
            self.assertTrue(self.v.solver.state()["solving"], "the main solve was paused")
            self.assertFalse(self.v.solver.suspended)
        finally:
            self.v.req("/api/stop", "POST", {})
            self.v.wait_solved()

    def test_11_what_a_review_found(self):
        v, solver = self.v, self.v.solver
        # numbers and text JSON can carry and a form cannot: refused in words, not a server error
        for cap in (float("inf"), float("nan"), 2.5, 0, True, "2"):
            st, d = v.req("/api/preflop/describe", "POST", dict(FORM, raise_cap=cap))
            self.assertEqual(st, 400, (cap, d))
            self.assertTrue(d["error"].startswith("Cap:"), d)
        for text in ("AA,KK\ud800", "AA\u0085KK", "AA\u2028KK", "AA\tKK"):
            st, d = v.req("/api/preflop/estimate", "POST", dict(FORM, range_sb=text))
            self.assertEqual(st, 400, (text, d))
            self.assertIn("not usable", d["error"])

        # a refused build leaves the tree that is there, and what was made for it
        st, d = v.req("/api/preflop/build", "POST", FORM)
        self.assertEqual(st, 200, d)
        vsrv._preview = 1.5
        try:
            st, d = v.req("/api/preflop/build", "POST", dict(FORM, open="abc"))
            self.assertEqual(st, 400, d)
            self.assertEqual(vsrv._preview, 1.5)
        finally:
            vsrv._preview = None
        self.assertTrue(solver.tree_present)
        # ... and a solve of it running, also one parked for a moment (a click in Study, a street being
        # refined): the hold is still known, so it is let go and the solve goes on
        st, d = v.req("/api/solve", "POST", {"accuracy": 1e-9})
        self.assertEqual(st, 200, d)
        try:
            solver.hold("test")
            st, d = v.req("/api/preflop/build", "POST", dict(FORM, open="abc"))
            self.assertEqual(st, 400, d)
            self.assertEqual(solver.holds, {"test"})
            solver.unhold("test")
            self.assertTrue(solver.state()["solving"])
            solver.hold("test")
            st, d = v.req("/api/preflop/build", "POST", FORM)   # accepted: its stop ends the hold too
            self.assertEqual(st, 200, d)
            self.assertEqual(solver.holds, set())
        finally:
            solver.unhold("test")
            v.req("/api/stop", "POST", {})
            v.wait_solved()

        # the estimate a build waits for is not the form's: a later click on Estimate does not end it,
        # and it does not end the form's
        class Running:
            killed = 0

            def kill(self):
                Running.killed += 1
        est = preflop_api.ESTIMATES
        est.running = Running()
        try:
            st, d = v.req("/api/preflop/build", "POST", dict(FORM, stack="21"))   # not in the cache
            self.assertEqual(st, 200, d)
            self.assertEqual(Running.killed, 0)
            self.assertIsInstance(est.running, Running)
            st, d = v.req("/api/preflop/estimate", "POST", dict(FORM, stack="22"))
            self.assertEqual(st, 200, d)
            self.assertEqual(Running.killed, 1, "the form's newer estimate ends the form's older one")
        finally:
            est.running = None

        # a build that fails in the solver: no tree, and nothing of the old one's solve
        st, d, _ = v.solve({"accuracy": 50.0})
        self.assertEqual(st, 200, d)
        self.assertTrue(solver.series)
        cmds = solver.cmds

        def failing(lines, **kw):
            if lines[:1] != ["stop"]:
                raise vsrv.SolverError("out of memory")
            return cmds(lines, **kw)
        solver.cmds = failing
        try:
            st, d = v.req("/api/preflop/build", "POST", FORM)
        finally:
            del solver.cmds
        self.assertEqual((st, d.get("error")), (400, "out of memory"))
        self.assertFalse(solver.tree_present)
        self.assertEqual(solver.series, [])
        self.assertIsNone(solver.preflop)
        self.assertEqual(v.req("/api/status")[1]["tree"], False)

        # the solver's own settings count as put back when they were sent, not when they were listed
        self.assertTrue(solver.preflop_touched)

        def timeout(lines, **kw):
            raise vsrv.SolverError("timeout waiting for solver response")
        solver.cmds = timeout
        try:
            st, d = v.req("/api/generate", "POST", POSTFLOP)
        finally:
            del solver.cmds
        self.assertEqual(st, 400, d)
        self.assertTrue(solver.preflop_touched)
        st, d = v.req("/api/generate", "POST", POSTFLOP)
        self.assertEqual(st, 200, d)
        self.assertFalse(solver.preflop_touched)
        st, d = v.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        self.assertIn("TOTAL: 13201", d["node_count"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a.startswith("-")])
