#!/usr/bin/env python3
"""Integration tests for the viewer server against the real solver binary.

Usage: python3 viewer/test_server.py [path-to-solver]

Builds a tiny tree (a few MB), solves it briefly and exercises the whole JSON API:
static files, hand order, generate/estimate/build, browsing, strategy grid data,
range/EV views, live solving progress, save + load. Runs in well under 20 s.
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

CFG = {
    "range_oop": "AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,AK,AQ,KQs",
    "board": "QhQc2c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "50",
    "turn_bet": "60", "turn_raise": "50",
    "river_bet": "65", "river_raise": "50", "river_allin": True,
    "allin_threshold": 50, "add_allin_limit": 300, "iso": True,
}


class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solver = vsrv.Solver(SOLVER)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = vlib.Library(tempfile.mkdtemp(prefix="wsolver-test-library-"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        try:
            cls.solver.proc.kill()
        except Exception:
            pass

    def req(self, path, method="GET", body=None, extra_headers=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if method == "POST" and data is None:
            data = b"{}"
        headers.update(extra_headers or {})
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=120) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_00_static_and_status(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as f:
            html = f.read().decode()
            self.assertEqual(f.status, 200)
        self.assertIn("<title>WSolver</title>", html)
        # every module the page imports is served, with a JavaScript content type
        for name in ("app.js", "cards.js", "ranges.js", "api.js", "chart.js", "study.js", "build.js", "style.css"):
            with urllib.request.urlopen(self.base + "/" + name, timeout=10) as f:
                self.assertEqual(f.status, 200, name)
                if name.endswith(".js"):
                    self.assertIn("javascript", f.headers["Content-Type"])
        st, d = self.req("/api/status")
        self.assertEqual(st, 200)
        self.assertFalse(d["solving"])
        self.assertTrue(d["version"])
        self.assertFalse(d["tree"])

    def test_01_hand_order(self):
        st, d = self.req("/api/hand_order")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["order"]), 1326)
        self.assertEqual(d["order"][0], "2d2c")  # Pio order

    def test_02_no_tree_yet(self):
        st, d = self.req("/api/node?id=r:0")
        self.assertEqual(st, 400)
        self.assertIn("error", d)

    def test_03_no_raw_command_endpoint(self):
        st, d = self.req("/api/cmd", "POST", {"cmd": "stdoutredi /tmp/x"})
        self.assertEqual(st, 404)
        # node ids must not allow UPI command injection via %0A / spaces
        st, d = self.req("/api/node?id=" + urllib.parse.quote("r:0\nstdoutredi /tmp/x"))
        self.assertEqual(st, 400)
        st, d = self.req("/api/strategy?id=" + urllib.parse.quote("r:0 show_version"))
        self.assertEqual(st, 400)

    def test_03b_cross_site_requests_refused(self):
        # A page on another site can send text/plain POSTs without a CORS preflight; they must not
        # reach the solver (save/load write and read files).
        st, d = self.req("/api/save", "POST", None, {"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        self.assertIn("application/json", d["error"])
        # DNS rebinding: the browser sends the attacker's hostname in Host.
        st, d = self.req("/api/status", "GET", None, {"Host": f"evil.example:{self.http.server_port}"})
        self.assertEqual(st, 403)
        st, _ = self.req("/api/status", "GET", None, {"Host": f"localhost:{self.http.server_port}"})
        self.assertEqual(st, 200)

    def test_04_traversal_blocked(self):
        try:
            r = urllib.request.urlopen(self.base + "/../solver", timeout=10)
            code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 404)
        # only flat html/js/css/svg names under viewer/static: no Python sources, no subpaths
        for path in ("/server.py", "/test_server.py", "/static/app.js", "/%2e%2e/server.py", "/.hidden.js"):
            try:
                code = urllib.request.urlopen(self.base + path, timeout=10).status
            except urllib.error.HTTPError as e:
                code = e.code
            self.assertEqual(code, 404, path)

    def test_05_generate_estimate_build(self):
        st, d = self.req("/api/generate", "POST", CFG)
        self.assertEqual(st, 200, d)
        self.assertGreater(d["lines"], 0)
        st, d = self.req("/api/estimate")
        self.assertEqual(st, 200, d)
        self.assertIn("MB", d["text"])
        st, d = self.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        self.assertIn("TOTAL", d["node_count"])

    def test_06_browse(self):
        st, d = self.req("/api/node?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertIn(d["node"]["type"], ("OOP_DEC", "IP_DEC"))
        self.assertGreaterEqual(len(d["children"]), 2)
        # descend one action, then the chance node must offer runout cards
        st, d = self.req("/api/node?id=" + urllib.parse.quote(d["children"][0]["id"]))
        self.assertEqual(st, 200, d)

    def test_06b_path(self):
        # the whole line from the root in one call (#112): one node block per id prefix
        st, d = self.req("/api/node?id=r:0")
        child = d["children"][0]["id"]
        st, d = self.req("/api/node?id=" + urllib.parse.quote(child))
        deeper = d["children"][0]["id"]
        st, d = self.req("/api/path?id=" + urllib.parse.quote(deeper))
        self.assertEqual(st, 200, d)
        self.assertEqual([n["id"] for n in d["nodes"]], ["r:0", child, deeper])
        self.assertEqual(d["eff_stack"], CFG["stack"])
        st, d = self.req("/api/path?id=" + urllib.parse.quote("r:0\nshow_version"))
        self.assertEqual(st, 400)

    def test_07_strategy_grid_data(self):
        st, d = self.req("/api/strategy?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertIn("freq", d)
        self.assertEqual(len(d["freq"]), len(d["children"]))  # one row per child action
        for row in d["freq"]:
            self.assertEqual(len(row), 1326)
        self.assertEqual(len(d["reach"]), 1326)
        self.assertGreater(sum(x or 0 for x in d["reach"]), 0)
        # aggregate per-action frequencies: must be a probability distribution
        tot = sum(x or 0 for x in d["reach"])
        agg = 0.0
        for row in d["freq"]:
            agg += sum((x or 0) * (w or 0) for x, w in zip(row, d["reach"])) / tot
        self.assertAlmostEqual(agg, 1.0, places=2)
        self.assertEqual(len(d["ev"]["ev"]), 1326)

    def test_08_range_and_ev_views(self):
        st, d = self.req("/api/range?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertEqual(len(d["oop"]), 1326)
        self.assertEqual(len(d["ip"]), 1326)
        st, d = self.req("/api/ev?id=r:0&player=IP")
        self.assertEqual(st, 200, d)
        self.assertEqual(len(d["ev"]), 1326)

    def test_08b_categories(self):
        # #154: category breakdown at the root decision (pre-solve strategies are uniform)
        st, d = self.req("/api/categories?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["board"], CFG["board"])
        self.assertEqual(d["id"], "r:0")
        self.assertEqual(d["player"], d["actor"])  # default player is the actor
        # category shares partition the range: made and draw buckets each sum to it
        self.assertAlmostEqual(sum(c["combos"] for c in d["made"]), d["total"], places=3)
        self.assertAlmostEqual(sum(c["combos"] for c in d["draws"]), d["total"], places=3)
        for c in d["made"] + d["draws"]:
            self.assertAlmostEqual(c["pct"], c["combos"] / d["total"], places=4)
        # hand-counted categories of the known test range on QhQc2c:
        # AA + KK are overpairs (12 combos), JJ + TT underpairs, QsQd is quads
        by_name = {c["name"]: c for c in d["made"]}
        self.assertAlmostEqual(by_name["overpair"]["combos"], 12.0, places=4)
        self.assertAlmostEqual(by_name["underpair"]["combos"], 12.0, places=4)
        self.assertAlmostEqual(by_name["quads"]["combos"], 1.0, places=4)
        self.assertEqual(set(by_name["overpair"]["classes"]), {"AA", "KK"})
        self.assertIn("AQs", by_name["trips"]["classes"])  # trips: AQ + KQs (10 combos)
        self.assertAlmostEqual(by_name["trips"]["combos"], 10.0, places=4)
        # per-category strategies agree with the range-weighted strategy of the node:
        # reconstruct the overall action mix from the category rows and compare it
        # against /api/strategy's own rows
        st, s = self.req("/api/strategy?id=r:0")
        self.assertEqual(st, 200, s)
        tot = sum(w or 0 for w in s["reach"])
        for j, row in enumerate(s["freq"]):
            overall = sum((f or 0) * (w or 0) for f, w in zip(row, s["reach"])) / tot
            from_cats = sum(c["combos"] * c["strategy"][j] for c in d["made"]) / d["total"]
            self.assertAlmostEqual(from_cats, overall, places=3)
        # EVs are present for in-range categories
        self.assertIsNotNone(by_name["overpair"]["ev"])
        # the opponent toggle reports shares/EVs but no strategy
        st, d2 = self.req("/api/categories?id=r:0&player=IP")
        self.assertEqual(st, 200, d2)
        self.assertTrue(all(c["strategy"] is None for c in d2["made"]))
        self.assertAlmostEqual(sum(c["combos"] for c in d2["made"]), d2["total"], places=3)
        # chance and terminal nodes fall back to the last decision, like the matrix
        st, d3 = self.req("/api/categories?id=" + urllib.parse.quote("r:0:c:c"))
        self.assertEqual(st, 200, d3)
        self.assertEqual(d3["id"], "r:0:c")  # checked-through flop -> deal turn
        self.assertEqual(d3["actor"], "IP")
        # bad inputs
        st, _ = self.req("/api/categories?id=r:0&player=BB")
        self.assertEqual(st, 400)
        st, _ = self.req("/api/categories?id=" + urllib.parse.quote("r:0\nshow_version"))
        self.assertEqual(st, 400)

    def test_09_solve_with_live_progress(self):
        st, d = self.req("/api/solve", "POST", {"accuracy": 1.0})
        self.assertEqual(st, 200, d)
        saw_solving_or_done = False
        deadline = time.time() + 90
        last = None
        while time.time() < deadline:
            st, d = self.req("/api/progress")
            self.assertEqual(st, 200)
            last = d
            if d["solving"] or d["solver_status"].startswith("stopped"):
                saw_solving_or_done = True
            if not d["solving"] and d["solver_status"].startswith("stopped"):
                break
            time.sleep(0.25)
        self.assertTrue(saw_solving_or_done)
        self.assertTrue(last["solver_status"].startswith("stopped"), last)
        text = "\n".join(last["progress"])
        self.assertIn("Exploitable for:", text)
        expl = float([l for l in last["progress"]
                      if l.startswith("Exploitable for:")][0].split(":")[1])
        self.assertLess(expl, 1.5)  # accuracy target was 1.0 chips
        st, d = self.req("/api/calc_results")
        self.assertIn("EV OOP:", d["text"])
        # convergence series (#115): one point per progress block, time increasing
        st, d = self.req("/api/convergence")
        self.assertEqual(st, 200, d)
        pts = d["points"]
        self.assertGreaterEqual(len(pts), 1)
        self.assertTrue(all(pts[i]["t"] <= pts[i + 1]["t"] for i in range(len(pts) - 1)))
        self.assertAlmostEqual(pts[-1]["explo"], expl, places=3)

    def test_10_save_and_load(self):
        path = os.path.join(tempfile.mkdtemp(), "tiny.cfr")
        st, d = self.req("/api/save", "POST", {"path": path, "no_rivers": True})
        self.assertEqual(st, 200, d)
        self.assertTrue(os.path.getsize(path) > 0)
        st, d = self.req("/api/load", "POST", {"path": path})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/node?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["node"]["flags"])  # truncated save: INCOMPLETE_TREE
        # strategy data still available after reload
        st, d = self.req("/api/strategy?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertIn("freq", d)

    def test_10b_new_tree_resets_series_and_ignores_iso(self):
        # isomorphism is always on (#113): an old client's iso=False must not turn it off
        st, d = self.req("/api/generate", "POST", dict(CFG, iso=False))
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/convergence")
        self.assertEqual(d["points"], [])
        st, d = self.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/node?id=r:0")
        self.assertEqual(st, 200, d)

    def test_10c_per_player_sizes(self):
        # #141: IP's own sizes; an empty IP list means IP has no bets on that street
        cfg = dict(CFG, flop_bet="50", flop_bet_ip="75", flop_raise="", flop_raise_ip="",
                   river_allin=False, river_allin_ip=False, allin_threshold=100)  # no promotion
        st, d = self.req("/api/generate", "POST", cfg)
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/node?id=r:0")
        self.assertIn("r:0:b20", [c["id"] for c in d["children"]])      # OOP 50% of 40
        st, d = self.req("/api/node?id=r:0:c")
        self.assertIn("r:0:c:b30", [c["id"] for c in d["children"]])    # IP 75% of 40
        st, d = self.req("/api/generate", "POST", dict(cfg, flop_bet_ip=""))
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/build", "POST", {})
        st, d = self.req("/api/node?id=r:0:c")
        self.assertEqual([c["id"] for c in d["children"]], ["r:0:c:c"])   # IP can only check
        # bad IP sizes are rejected like OOP's
        st, d = self.req("/api/generate", "POST", dict(cfg, flop_bet_ip="3x"))
        self.assertEqual(st, 400)

    def test_10d_turn_and_river_boards(self):
        # a 4- or 5-card board starts the tree on that street (builder lines must not run past it)
        for board, first_kid in (("QhQc2c5d", "r:0:b"), ("QhQc2c5d9s", "r:0:b")):
            st, d = self.req("/api/generate", "POST", dict(CFG, board=board))
            self.assertEqual(st, 200, d)
            st, d = self.req("/api/build", "POST", {})
            self.assertEqual(st, 200, d)
            st, d = self.req("/api/node?id=r:0")
            self.assertEqual(st, 200, d)
            self.assertEqual(d["node"]["board"].replace(" ", ""), board)
            self.assertTrue(any(c["id"].startswith(first_kid) for c in d["children"]), d["children"])

    def test_10e_oop_needs_donk_sizes_to_lead(self):
        # #152 (owner's rule): after OOP calls the flop bet, OOP can lead the turn only
        # with turn donk sizes configured; an empty Donk column means check only.
        # a deeper stack / threshold of 100 keeps the small sizes off the all-in
        cfg = dict(CFG, stack=200, allin_threshold=100)
        for body, turn_children in (
                (cfg, None),                                  # no donk: check only
                (dict(cfg, turn_donk="30"), [":b50", ":c"])):  # 24 + 30% of the 88 pot
            st, d = self.req("/api/generate", "POST", body)
            self.assertEqual(st, 200, d)
            st, d = self.req("/api/build", "POST", {})
            self.assertEqual(st, 200, d)
            # flop: OOP checks, IP bets 24 (60% of 40), OOP calls -> the turn
            st, d = self.req("/api/node?id=r:0:c:b24:c")
            self.assertEqual(st, 200, d)
            self.assertEqual(d["node"]["type"], "SPLIT_NODE")
            turn = d["children"][0]["id"]
            st, d = self.req("/api/node?id=" + urllib.parse.quote(turn))
            self.assertEqual(st, 200, d)
            self.assertEqual(d["node"]["type"], "OOP_DEC")
            want = [turn + ":c"] if turn_children is None \
                else [turn + suffix for suffix in turn_children]
            self.assertEqual([c["id"] for c in d["children"]], want)

    def test_11_restart_solver(self):
        st, d = self.req("/api/restart", "POST", {})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/status")
        self.assertEqual(st, 200)
        self.assertFalse(d["solving"])
        self.assertFalse(d["dead"])
        self.assertTrue(d["version"])

    def test_12_library_config_round_trip_generate(self):
        """Issue #116 acceptance: a saved config, loaded back, generates the same lines."""
        cfg = dict(CFG, schema=1)
        st, d = self.req("/api/library/save", "POST",
                         {"kind": "config", "name": "round-trip/SRP", "content": cfg})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/library/get?kind=config&name=" + urllib.parse.quote("round-trip/SRP"))
        self.assertEqual(st, 200, d)
        self.assertEqual(d["content"], cfg)
        loaded = {k: v for k, v in d["content"].items() if k != "schema"}
        st, via_lib = self.req("/api/generate", "POST", loaded)
        self.assertEqual(st, 200, via_lib)
        st, direct = self.req("/api/generate", "POST", CFG)
        self.assertEqual(st, 200, direct)
        self.assertEqual(via_lib, direct)  # same line count and same first lines


if __name__ == "__main__":
    unittest.main(argv=["test_server.py", "-v"] if sys.argv[1:] and not sys.argv[1].startswith("-")
                     else sys.argv)
