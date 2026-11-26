#!/usr/bin/env python3
"""Integration tests for the viewer server against the real solver binary.

Usage: python3 viewer/test_server.py [path-to-solver]

Builds a tiny tree (a few MB), solves it briefly and exercises the whole JSON API:
static files, hand order, generate/estimate/build, browsing, strategy grid data,
range/EV views, live solving progress, save + load. Runs in well under 20 s.
"""
import json
import os
import subprocess
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

    def test_device_selection(self):
        _, state = self.req("/api/status")
        self.assertEqual(state["device"], "cpu")
        if "gpu" in state["devices"]:
            try:
                status, data = self.req("/api/device", "POST", {"device": "gpu"})
                self.assertEqual(status, 200, data)
                self.assertIn("algorithm:v4_metal", self.solver.cmd("show_settings"))
                self.assertEqual(self.req("/api/progress")[1]["device"], "gpu")
            finally:
                self.req("/api/device", "POST", {"device": "cpu"})
        else:
            self.assertGreaterEqual(self.req("/api/device", "POST", {"device": "gpu"})[0], 400)

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

    def test_03c_bad_content_length(self):
        """#394: a garbage, negative or huge Content-Length gets an answer, and nothing huge is read."""
        import http.client
        for value, want in (("abc", 400), ("-5", 400), (str(1 << 40), 413)):
            c = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=10)
            c.putrequest("POST", "/api/status")
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", value)
            c.endheaders()
            r = c.getresponse()
            self.assertEqual(r.status, want, value)
            self.assertIn("Content-Length" if want == 400 else "MB", json.loads(r.read())["error"])
            c.close()
        st, _ = self.req("/api/status")  # the server still answers
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
        # pot and stack are whole chips: 40.5 used to be truncated to 40 (a different tree than
        # the form shows); a whole number written as 40.0 is fine
        for bad in ({"pot": 40.5}, {"stack": 59.9}, {"pot": True}, {"pot": 10 ** 400}):
            st, d = self.req("/api/generate", "POST", dict(CFG, **bad))
            self.assertEqual(st, 400, (bad, d))
            self.assertIn("whole chips", d["error"])
        st, d = self.req("/api/generate", "POST", dict(CFG, pot=40.0, stack="60"))
        self.assertEqual(st, 200, d)
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

    def test_saved_file_browser_lists_folders_and_save_metadata_without_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = os.path.join(directory, "A folder")
            os.mkdir(folder)
            path = os.path.join(directory, "Kh9h4h.cfr")
            with open(path, "wb") as file:
                file.write(b"metadata only")
            with open(os.path.join(directory, "notes.txt"), "w") as file:
                file.write("not a save")
            before = self.req("/api/status")[1]
            st, listing = self.req("/api/files?path=" + urllib.parse.quote(directory))
            self.assertEqual(st, 200, listing)
            self.assertEqual([entry["name"] for entry in listing["entries"]], ["A folder", "Kh9h4h.cfr"])
            self.assertTrue(listing["entries"][0]["folder"])
            self.assertEqual(listing["entries"][1]["bytes"], 13)
            self.assertEqual(listing["parent"], os.path.dirname(directory))
            st, selected = self.req("/api/files?path=" + urllib.parse.quote(path))
            self.assertEqual(st, 200, selected)
            self.assertEqual(selected["selected"], path)
            self.assertEqual(selected["path"], directory)
            st, child = self.req("/api/files?path=" + urllib.parse.quote(folder))
            self.assertEqual(st, 200, child)
            self.assertEqual(child["entries"], [])
            self.assertEqual(child["parent"], directory)
            after = self.req("/api/status")[1]
            self.assertEqual(after["tree"], before["tree"])
            self.assertEqual(after["loaded_path"], before["loaded_path"])
            self.assertEqual(self.req("/api/files?path=" + urllib.parse.quote(directory + "\nexit"))[0], 400)

    def test_simulation_library_save_import_backup_and_browse_after_delete(self):
        self.assertEqual(self.req("/api/generate", "POST", CFG)[0], 200)
        self.assertEqual(self.req("/api/build", "POST")[0], 200)
        name = "Review/Paired flop"
        st, saved = self.req("/api/library/save", "POST", {"kind": "sim", "name": name, "content": {"save_mode": "full"}})
        self.assertEqual(st, 200, saved)
        entry = next(e for e in self.req("/api/library/list?kind=sim")[1]["entries"] if e["name"] == name)
        self.assertEqual(entry["board"], CFG["board"])
        self.assertEqual(entry["pot"], CFG["pot"])
        self.assertEqual(entry["stack"], CFG["stack"])
        self.assertEqual(self.req("/api/library/save", "POST", {"kind": "sim", "name": name, "content": {}})[0], 400)
        self.assertEqual(self.req("/api/library/open", "POST", {"name": name})[0], 200)
        with tempfile.TemporaryDirectory() as directory:
            backup = os.path.join(directory, "backup.cfr")
            self.assertEqual(self.req("/api/library/export", "POST", {"name": name, "path": backup})[0], 200)
            self.assertEqual(self.req("/api/library/import", "POST", {"name": "Imported", "path": backup})[0], 200)
        renamed = "Favorites/Paired flop"
        self.assertEqual(self.req("/api/library/rename", "POST", {"kind": "sim", "from": name, "to": renamed})[0], 200)
        self.assertEqual(self.req("/api/library/delete", "POST", {"kind": "sim", "name": renamed})[0], 200)
        st, strategy = self.req("/api/strategy?id=r:0:c:c:As")
        self.assertEqual(st, 200, strategy)
        self.assertIn("As", strategy["node"]["board"])
        old_snapshot = self.solver.browse_snapshot.name
        self.assertEqual(self.req("/api/library/open", "POST", {"name": "Imported"})[0], 200)
        self.assertFalse(os.path.exists(old_snapshot))
        self.assertEqual(self.req("/api/library/get?kind=sim&name=Imported")[1]["content"]["board"], CFG["board"])
        self.assertEqual(self.req("/api/library/delete", "POST", {"kind": "sim", "name": "Imported"})[0], 200)
        self.solver.forget_locks()

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
        for group in (d["made"], d["draws"]):
            indices = [i for c in group for i in c["indices"]]
            self.assertEqual(sorted(indices), [i for i, w in enumerate(s["reach"]) if w and w > 0])
            for c in group:
                self.assertAlmostEqual(sum(s["reach"][i] for i in c["indices"]), c["combos"], places=3)
        order = vsrv.hand_order(self.solver)
        self.assertEqual({order[i] for i in by_name["quads"]["indices"]}, {"QsQd"})
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

    def test_08c_blockers(self):
        # the Blockers tab's data: both reaches, equities, villain's strategy after each actor action
        st, d = self.req("/api/blockers?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertEqual((d["actor"], d["villain"], d["board"]), ("OOP", "IP", CFG["board"]))
        for k in ("hero_reach", "villain_reach", "hero_eq", "villain_eq"):
            self.assertEqual(len(d[k]), 1326, k)
        st, rng = self.req("/api/range?id=r:0")
        self.assertEqual(d["villain_reach"], rng["ip"])
        self.assertEqual(d["hero_reach"], rng["oop"])
        for e, w in zip(d["villain_eq"], d["villain_reach"]):
            if w:
                self.assertTrue(e is not None and 0 <= e <= 1, e)
        st, kids = self.req("/api/node?id=r:0")
        self.assertEqual([r["id"] for r in d["responses"]], [c["id"] for c in kids["children"]])
        answered = [r for r in d["responses"] if r["freq"]]
        self.assertTrue(answered, "IP answers at least one OOP action")
        for r in answered:
            self.assertEqual(r["node"]["type"], "IP_DEC")
            self.assertEqual(len(r["freq"]), len(r["children"]))
            for i, w in enumerate(d["villain_reach"]):  # each in-range villain combo's mix sums to 1
                if w:
                    self.assertAlmostEqual(sum(row[i] or 0 for row in r["freq"]), 1.0, places=3)
        self.assertEqual(len(d["cats"]["made"]), 1326)
        self.assertIn("overpair", d["cats"]["made_names"])
        # at IP's decision the roles swap
        st, d2 = self.req("/api/blockers?id=r:0:c")
        self.assertEqual(st, 200, d2)
        self.assertEqual((d2["actor"], d2["villain"]), ("IP", "OOP"))
        # a deal is not a decision; bad ids are refused
        st, _ = self.req("/api/blockers?id=" + urllib.parse.quote("r:0:c:c"))
        self.assertEqual(st, 400)
        st, _ = self.req("/api/blockers?id=" + urllib.parse.quote("r:0\nshow_version"))
        self.assertEqual(st, 400)

    def test_08d_graphs(self):
        # the Graphs tab's data: per-combo equity and EV of both players, at any node
        st, d = self.req("/api/graphs?id=r:0")
        self.assertEqual(st, 200, d)
        st, rng = self.req("/api/range?id=r:0")
        for pl in ("oop", "ip"):
            for k in ("reach", "eq", "ev"):
                self.assertEqual(len(d[pl][k]), 1326, (pl, k))
            self.assertEqual(d[pl]["reach"], rng[pl])
            for e, w in zip(d[pl]["eq"], d[pl]["reach"]):
                if w:
                    self.assertTrue(e is not None and 0 <= e <= 1, e)
            self.assertEqual(len(d[pl]["eq_matchups"]), 1326)
        # the two equities are complementary over the matchups (a showdown has a winner or a split)
        tot = sum(m * (e or 0) for m, e in zip(d["oop"]["eq_matchups"], d["oop"]["eq"])) + \
            sum(m * (e or 0) for m, e in zip(d["ip"]["eq_matchups"], d["ip"]["eq"]))
        self.assertAlmostEqual(tot / sum(d["oop"]["eq_matchups"]), 1.0, places=3)
        # a deal is fine; a fold uses the node above it (calc_ev has nothing at a terminal)
        st, d2 = self.req("/api/graphs?id=" + urllib.parse.quote("r:0:c:c"))
        self.assertEqual((st, d2["id"]), (200, "r:0:c:c"))
        st, root = self.req("/api/node?id=r:0")
        bet = next(c["id"] for c in root["children"] if ":b" in c["id"])
        st, kids = self.req("/api/node?id=" + urllib.parse.quote(bet))
        fold = next(c["id"] for c in kids["children"] if c["id"].endswith(":f"))
        st, d3 = self.req("/api/graphs?id=" + urllib.parse.quote(fold))
        self.assertEqual((st, d3["id"]), (200, bet))
        st, _ = self.req("/api/graphs?id=" + urllib.parse.quote("r:0\nshow_version"))
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

    def test_09a_range_ev_conserves_chips(self):
        # A range's EV is the matchups-weighted mean of its hands' EVs: calc_ev's matchups already
        # include the hand's own reach, so weighting by reach again skews the mean toward the
        # hands that reach the node most. The two players' range EVs then stop summing to the
        # starting pot (EVs are chips relative to the tree start) wherever reach is uneven -
        # here, below the root once the solve above has run.
        st, kids = self.req("/api/node?id=r:0")
        self.assertEqual(st, 200, kids)
        ids = ["r:0:c"] + [k["id"] for k in kids["children"] if k["id"].split(":")[-1].startswith("b")][:1]
        self.assertEqual(len(ids), 2, kids)
        for nid in ids:
            q = urllib.parse.quote(nid)
            total = 0.0
            for pl in ("OOP", "IP"):
                st, d = self.req(f"/api/categories?id={q}&player={pl}")
                self.assertEqual(st, 200, d)
                rows = [c for c in d["made"] if c["ev"] is not None]
                w = sum(c["matchups"] for c in rows)
                self.assertGreater(w, 0)
                total += sum(c["ev"] * c["matchups"] for c in rows) / w
            self.assertAlmostEqual(total, CFG["pot"], delta=0.01, msg=nid)

    def test_09b_solve_further_subtree(self):
        """#153: solve further at a decision node mutates only that subtree."""
        st, d = self.req("/api/strategy?id=" + urllib.parse.quote("r:0:c"))
        self.assertEqual(st, 200, d)
        child_before = d["freq"]
        st, d = self.req("/api/strategy?id=r:0")
        root_before = d["freq"]
        st, conv_before = self.req("/api/convergence")
        # input validation first
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c", "accuracy": 0})
        self.assertEqual(st, 400)
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c", "accuracy": "junk"})
        self.assertEqual(st, 400)
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c show_version", "accuracy": 0.1})
        self.assertEqual(st, 400)
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c:c", "accuracy": 0.1})
        self.assertEqual(st, 400)  # a chance node, not a decision
        # the real run: subtree solve on the main tree to 0.3 chips (0.75% of the 40-chip pot)
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c", "accuracy": 0.3})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["node"], "r:0:c")
        self.assertEqual(d["pot"], 40)
        partial = None
        deadline = time.time() + 90
        while time.time() < deadline:
            st, d = self.req("/api/progress")
            self.assertEqual(st, 200)
            if d["partial"] and d["partial"]["done"]:
                partial = d["partial"]
                break
            time.sleep(0.25)
        self.assertIsNotNone(partial, "subtree solve never reported done")
        self.assertEqual(partial["node"], "r:0:c")
        self.assertEqual(partial["pot"], 40)
        self.assertIsNotNone(partial["point"], partial)
        self.assertLessEqual(partial["point"]["explo"], 0.31, partial["point"])
        self.assertTrue(any(l.startswith("Exploitable for:") for l in partial["progress"]), partial["progress"])
        # strategies changed at the node but are bit-identical above it
        st, d = self.req("/api/strategy?id=" + urllib.parse.quote("r:0:c"))
        self.assertNotEqual(d["freq"], child_before, "subtree strategy should change")
        st, d = self.req("/api/strategy?id=r:0")
        self.assertEqual(d["freq"], root_before, "root strategy must be untouched")
        # partial blocks never pollute the whole-tree convergence series
        st, conv_after = self.req("/api/convergence")
        self.assertEqual(conv_after["points"], conv_before["points"])

    def test_09c_solve_further_root_and_busy(self):
        """#153: at the root it is the whole-tree solve; a busy solver refuses a new one."""
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0", "accuracy": 0.0001})
        self.assertEqual(st, 200, d)
        self.assertTrue(d.get("root"))
        seen_solving = False
        deadline = time.time() + 30
        while time.time() < deadline:
            st, d = self.req("/api/progress")
            if d["solving"]:
                seen_solving = True
                break
            if d["solver_status"].startswith("stopped"):
                break
            time.sleep(0.1)
        if seen_solving:
            st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c", "accuracy": 0.05})
            self.assertEqual(st, 400)
            self.assertIn("already running", d["error"])
            st, d = self.req("/api/stop", "POST", {})
            self.assertEqual(st, 200, d)
            deadline = time.time() + 30
            while time.time() < deadline:
                st, d = self.req("/api/progress")
                if not d["solving"]:
                    break
                time.sleep(0.1)
            self.assertFalse(d["solving"])
        # a root run goes through /api/solve's path, so no partial state survives
        st, d = self.req("/api/status")
        self.assertIsNone(d["partial"])

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

    def test_10a_solve_further_refused_on_loaded_tree(self):
        # #153: view-only saves carry no solver state — solve further is refused (UI disables it)
        st, d = self.req("/api/status")
        self.assertEqual(st, 200)
        self.assertTrue(d["loaded_file"])
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c", "accuracy": 0.2})
        self.assertEqual(st, 400)
        self.assertIn("file", d["error"])

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

    def test_10f_pio_import(self):
        # #317: a PioViewer tree file fills the Build form; the fixture's own 266 lines come back exactly
        with open(os.path.join(REPO, "tests", "data", "pio_25bbHU_2sizes.txt")) as fh:
            st, d = self.req("/api/pio_import", "POST", {"text": fh.read()})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["matches"], d)
        self.assertEqual(d["file_lines"], 266)
        f = d["form"]
        self.assertEqual((f["pot"], f["stack"], f["board"]), (40, 230, "QhQc2c"))
        self.assertEqual(f["pio_extra"], [])
        # settings without a form field travel as pio_extra and change the build: ForceOOPBet takes
        # OOP's flop check away, MergeSimilarBets folds 30% into 33%
        text = ("#Pot#100\n#EffectiveStacks#1000\n#AllinThreshold#67\n#ForceOOPBet#True\n"
                "#MergeSimilarBets#True\n#MergeSimilarBetsThreshold#12\n#FlopConfig.BetSize#30 33\n")
        st, d = self.req("/api/pio_import", "POST", {"text": text})
        self.assertEqual(st, 200, d)
        f = d["form"]
        self.assertEqual(f["flop_bet"], "30,33")
        self.assertEqual(sorted(f["pio_extra"]), sorted(["#ForceOOPBet#True", "#MergeSimilarBets#True",
                                                         "#MergeSimilarBetsThreshold#12"]))
        st, d = self.req("/api/generate", "POST",
                         dict(CFG, **{k: f[k] for k in f if k not in ("range_oop", "range_ip", "board")}))
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/node?id=r:0")
        self.assertEqual([c["id"] for c in d["children"]], ["r:0:b33", "r:0:c"])  # 30% merged into 33%
        # ForceOOPBet: no line starts with a check, so after one (which the solver adds everywhere,
        # as Pio does from the same lines) IP has no bet
        st, d = self.req("/api/node?id=r:0:c")
        self.assertEqual([c["id"] for c in d["children"]], ["r:0:c:c"])
        # only the listed PioViewer keys pass, one line each
        for bad in (["#Pot#1"], ["set_board AhKhQh"], ["#MergeSimilarBets#True\nset_pot 0 0 1"], "x"):
            st, d = self.req("/api/generate", "POST", dict(CFG, pio_extra=bad))
            self.assertEqual(st, 400, (bad, d))
        st, d = self.req("/api/pio_import", "POST", {"text": "hello"})
        self.assertEqual(st, 400, d)

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


class SolverTimeoutTest(unittest.TestCase):
    def test_a_timed_out_answer_is_skipped(self):
        """#382: a command that times out still gets its answer later; the next command must read its
        own answer, not that one (before, every later command read the one before's)."""
        s = vsrv.Solver(SOLVER)
        try:
            s.cmds(["set_range OOP AA,KK", "set_range IP QQ,JJ", "set_board Kh9h4c2d", "set_pot 0 0 100",
                    "set_eff_stack 200", "add_line 50", "build_tree", "set_accuracy 0", "go 3 seconds"])
            with self.assertRaises(vsrv.SolverError):
                s.cmd("wait_for_solver", timeout=0.5)  # still solving: times out, its answer comes later
            self.assertEqual(s.unread, 1)
            self.assertEqual(s.cmd("is_ready", timeout=60), "is_ready ok!")
            self.assertEqual(s.unread, 0)
            self.assertEqual(s.cmd("echo next"), "next")
        finally:
            s.proc.kill()


class ExitWithParentTest(unittest.TestCase):
    """--exit-with-parent (#479): a viewer started by the macOS app's launcher stops, and takes its
    solvers with it, when the launcher is gone without having stopped it."""
    # stands in for the launcher: starts the viewer, says its pid, and waits to be killed -- or, as
    # "gone-at-once", has told the viewer its own pid and is gone before the viewer is up
    PARENT = ("import os, subprocess, sys, time\n"
              "mode = sys.argv[1]\n"
              "p = subprocess.Popen(sys.argv[2:] + ([str(os.getpid())] if mode == 'gone-at-once' else []),\n"
              "                     start_new_session=mode != 'shared-group')\n"
              "print(p.pid, flush=True)\n"
              "time.sleep(0 if mode == 'gone-at-once' else 600)\n")

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def run_case(self, group):
        with tempfile.TemporaryDirectory() as td:
            parent = subprocess.Popen(
                [sys.executable, "-c", self.PARENT, group, sys.executable, "-u", os.path.join(HERE, "server.py"),
                 "--solver", SOLVER, "--port", "0", "--library", os.path.join(td, "library"),
                 "--jobs", os.path.join(td, "jobs"), "--exit-with-parent"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            pids = []
            try:
                viewer = int(parent.stdout.readline())
                pids.append(viewer)
                ready = parent.stdout.readline()
                self.assertIn("UI on http://127.0.0.1:", ready)
                base = "http://" + ready.split("UI on http://")[1].strip()
                if group == "gone-at-once":   # it is already on its way out: nothing to ask it
                    self.assertEqual(parent.wait(timeout=30), 0)
                else:
                    with urllib.request.urlopen(base + "/api/status", timeout=30) as r:
                        self.assertTrue(json.loads(r.read())["ok"])
                    ps = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
                    solvers = [int(a) for a, b in (ln.split() for ln in ps.splitlines() if ln.strip()) if int(b) == viewer]
                    pids += solvers
                    self.assertTrue(solvers, "the viewer has a solver running")
                    self.assertTrue(self.alive(viewer))
                    parent.kill()   # no chance to stop anything, as in a crash
                    parent.wait()
                deadline = time.time() + 20
                while time.time() < deadline and any(self.alive(p) for p in pids):
                    time.sleep(0.1)
                self.assertEqual([p for p in pids if self.alive(p)], [], "the viewer and its solvers are gone")
                with self.assertRaises(OSError):   # and so is the port
                    urllib.request.urlopen(base + "/api/status", timeout=5)
                if group != "shared-group":   # nothing else of the viewer's group either
                    ps = subprocess.run(["ps", "-axo", "pid=,pgid="], capture_output=True, text=True).stdout
                    self.assertEqual([ln for ln in ps.splitlines() if ln.split()[1:] == [str(viewer)]], [])
            finally:
                parent.kill()
                parent.stdout.close()
                for p in pids:   # only what a failed run left behind
                    try:
                        os.kill(p, 9)
                    except OSError:
                        pass

    def test_in_a_process_group_of_its_own(self):
        self.run_case("own-group")   # as the launcher starts it: the whole group is interrupted

    def test_in_its_parents_process_group(self):
        self.run_case("shared-group")   # then only the viewer shuts down (and stops its solvers)

    def test_parent_gone_before_the_viewer_is_up(self):
        self.run_case("gone-at-once")   # told the parent's pid, it sees at once that this is not its parent


if __name__ == "__main__":
    unittest.main(argv=["test_server.py", "-v"] if sys.argv[1:] and not sys.argv[1].startswith("-")
                     else sys.argv)
