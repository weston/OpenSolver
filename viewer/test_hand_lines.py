#!/usr/bin/env python3
"""Tests for viewer/hand_lines.py (Beta view, runout lines, #467).

Usage: python3 viewer/test_hand_lines.py [path-to-solver]

Unit tests on hand-written `show_hand_lines` rows (reading a line off a path, naming sizes and
all-ins, the nested report), then /api/hand_lines against the real solver on the tiny tree of
viewer/test_server.py: what a hand does first agrees with /api/strategy, every runout's lines are
frequencies, and the id a runout opens in Study exists.
"""
import json
import os
import re
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
import hand_lines as hl  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.join(REPO, "solver")

# The tree of viewer/test_server.py: pot 40, stack 60, one bet size per street.
CFG = {
    "range_oop": "AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,AK,AQ,KQs",
    "board": "QhQc2c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "50",
    "turn_bet": "60", "turn_raise": "50",
    "river_bet": "65", "river_raise": "50", "river_allin": True,
    "allin_threshold": 50, "add_allin_limit": 300, "iso": True,
}

# A flop node, OOP to act, 40 in the middle. Each row: the rest of a node id, its weight, and the
# weight of the node with the row's cards dealt.
ROWS = """combos: AhAd
b24:f 3 10
b24:c:~ 1 10
c:c:~ 6 10
b24:c:Ks:c:c:~ 1 8
c:c:Ks:c:c:~ 3 8
c:c:Ks:b20:f 1 8
c:c:Ks:c:b20:c:~ 0.5 8
c:c:Ks:c:b20:f 0.5 8
c:c:Ks:c:c:2h:b40:c 2 4
c:c:Ks:c:c:2h:c:b40:b120:c 1 4
c:c:Ks:c:c:2h:c:c 1 4
c:c:Ks:c:c:2d:c:c 0.01 3
c:c:Ks:c:c:2d:b40:f 2.99 3
c:c:As:c:c:~ 2 2
"""
FLOP = {"actor": 0, "commit": [0, 0], "dead": 40}


def lines(levels, *cards):
    return {k: round(v, 6) for k, v in levels[tuple(cards)]["lines"].items()}


class SummarizeTest(unittest.TestCase):
    def test_oop_lines(self):
        lv = hl.summarize(ROWS, FLOP, 0)
        # the flop: a bet is a bet whatever the other player does with it
        self.assertEqual(lines(lv), {"b": 4, "x": 6})
        self.assertEqual((lv[()]["total"], lv[()]["base"]), (10, 10))
        # the turn Ks: both ways into it, the flop action first
        self.assertEqual(lines(lv, "Ks"), {"b x": 1, "x x": 3, "x b": 1, "x xc": 0.5, "x xf": 0.5})
        self.assertEqual((lv[("Ks",)]["total"], lv[("Ks",)]["base"]), (6, 8))
        self.assertEqual(lv[("Ks",)]["go"], "c:c:Ks")   # the heavier way in (5 of the 6)
        self.assertEqual(lines(lv, "Ks", "2h"), {"x x b": 2, "x x xr": 1, "x x x": 1})
        self.assertEqual(lv[("Ks", "2h")]["go"], "c:c:Ks:c:c:2h")
        self.assertEqual(set(lv), {(), ("Ks",), ("As",), ("Ks", "2h"), ("Ks", "2d")})

    def test_ip_lines(self):
        lv = hl.summarize(ROWS, FLOP, 1)
        # the same paths read for the other player: it folds or calls the bet, checks behind
        self.assertEqual(lines(lv), {"f": 3, "c": 1, "x": 6})
        self.assertEqual(lines(lv, "Ks"), {"c x": 1, "x x": 3, "x f": 1, "x b": 1})
        self.assertEqual(lines(lv, "Ks", "2h"), {"x x c": 2, "x x bc": 1, "x x x": 1})

    def test_sizes_and_all_in(self):
        lv = hl.summarize(ROWS, FLOP, 0, sizes=True, allin=[100, 100])
        self.assertEqual(lines(lv), {"b60": 4, "x": 6})                       # 24 into 40
        self.assertEqual(lines(lv, "Ks")["x b50"], 1)                         # 20 into 40
        # 40 into 40; the check-raise to 120 is more than the 100 behind: all-in
        self.assertEqual(lines(lv, "Ks", "2h"), {"x x b100": 2, "x x xra": 1, "x x x": 1})
        # a bet into a pot that already holds this street's chips: 48 more into 40 + 24 + 24
        row = "combos: AhAd\nb24:c:Ks:b72:c:~ 1 1\n"
        self.assertEqual(lines(hl.summarize(row, FLOP, 0, sizes=True, allin=[100, 100]), "Ks"), {"b60 b55": 1})
        # without the stacks no bet is called all-in, and a raise has no size
        lv = hl.summarize(ROWS, FLOP, 0, sizes=True)
        self.assertEqual(lines(lv, "Ks", "2h"), {"x x b100": 2, "x x xr": 1, "x x x": 1})

    def test_start_facing_a_bet(self):
        start = {"actor": 1, "commit": [24, 0], "dead": 40}
        rows = "combos: AhAd\nf 1 4\nc:~ 2 4\nb72:c:~ 1 4\nc:Ks:c:c:~ 2 2\n"
        lv = hl.summarize(rows, start, 1)
        self.assertEqual(lines(lv), {"f": 1, "c": 2, "r": 1})
        self.assertEqual(lines(lv, "Ks"), {"c x": 2})
        # the one who bet has nothing left to do on this street, unless it is raised
        lv = hl.summarize(rows, start, 0)
        self.assertEqual(lines(lv), {"-": 3, "c": 1})
        self.assertEqual(lines(lv, "Ks"), {"- x": 2})

    def test_a_deal_or_the_end_itself(self):
        lv = hl.summarize("combos: AhAd\n~ 5 5\nKs:c:c:~ 2 4\n", {"actor": None, "commit": [0, 0], "dead": 40}, 0)
        self.assertEqual(lines(lv), {"-": 5})
        self.assertEqual(lines(lv, "Ks"), {"- x": 2})
        self.assertEqual(lv[("Ks",)]["go"], "Ks")
        lv = hl.summarize("combos: AhAd\n. 5 5\n", {"actor": None, "commit": [0, 0], "dead": 40}, 0)
        self.assertEqual(lines(lv), {"-": 5})
        self.assertEqual(hl.summarize("combos: AhAd\n", FLOP, 0), {})
        with self.assertRaises(ValueError):
            hl.summarize("combos: AhAd\nzz 1 1\n", FLOP, 0)

    def test_payload(self):
        p = hl.to_payload(hl.summarize(ROWS, FLOP, 0))
        table, now = p["lines"], p["now"]
        named = lambda cell: [(table[i], f) for i, f in cell["lines"]]  # noqa: E731
        self.assertEqual(named(now), [("x", 0.6), ("b", 0.4)])        # most common first
        self.assertEqual(now["reach"], 1.0)
        self.assertNotIn("card", now)
        self.assertEqual([c["card"] for c in now["cards"]], ["As", "Ks"])  # high card first
        ks = now["cards"][1]
        self.assertEqual(ks["reach"], 0.75)                             # it sees the Ks 6 times in 8
        self.assertEqual(ks["go"], "c:c:Ks")
        self.assertEqual(named(ks)[0], ("x x", 0.5))
        self.assertAlmostEqual(sum(f for _, f in ks["lines"]), 1.0, places=3)
        self.assertEqual([c["card"] for c in ks["cards"]], ["2h", "2d"])   # spades, hearts, diamonds, clubs
        h2, d2 = ks["cards"]
        self.assertEqual(named(h2), [("x x b", 0.5), ("x x xr", 0.25), ("x x x", 0.25)])
        # a line under half a percent of a runout is left to "other"
        self.assertEqual(named(d2), [("x x b", 0.9967)])
        self.assertEqual(now["cards"][0]["cards"] if "cards" in now["cards"][0] else [], [])
        self.assertEqual(len(table), len(set(table)))
        self.assertEqual(hl.to_payload({}), {"lines": [], "now": None})


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solver = vsrv.Solver(SOLVER)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = vlib.Library(tempfile.mkdtemp(prefix="wsolver-test-library-"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"
        for path, body in (("/api/generate", CFG), ("/api/build", {}), ("/api/solve", {"accuracy": 0.5})):
            st, d = cls.req(path, "POST", body)
            assert st == 200, (path, d)
        deadline = time.time() + 120
        while time.time() < deadline:
            st, d = cls.req("/api/progress")
            if not d["solving"] and d["solver_status"].startswith("stopped"):
                break
            time.sleep(0.25)
        else:
            raise AssertionError("the solve did not finish")

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        try:
            cls.solver.proc.kill()
        except Exception:
            pass

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

    def lines(self, node, player, hand, sizes=False):
        st, d = self.req(f"/api/hand_lines?id={urllib.parse.quote(node)}&player={player}"
                         f"&hand={urllib.parse.quote(hand)}" + ("&sizes=1" if sizes else ""))
        self.assertEqual(st, 200, d)
        return d

    def check_cells(self, d, depth=0, cell=None):
        """Every cell: frequencies, most common first, that add up to no more than everything."""
        cell = cell or d["now"]
        fs = [f for _, f in cell["lines"]]
        self.assertTrue(fs, cell)
        self.assertEqual(fs, sorted(fs, reverse=True))
        self.assertTrue(all(0 <= i < len(d["lines"]) for i, _ in cell["lines"]))
        self.assertLessEqual(sum(fs), 1.001)
        self.assertGreater(sum(fs), 0.9)       # what is left out is under half a percent a line
        self.assertTrue(0 < cell["reach"] <= 1.0001, cell["reach"])
        for i, _ in cell["lines"]:
            self.assertEqual(len(d["lines"][i].split(" ")), depth + 1)   # one part per street so far
        n = 1
        for kid in cell.get("cards", []):
            self.assertTrue(kid["go"].endswith(kid["card"]))
            n += self.check_cells(d, depth + 1, kid)
        return n

    def test_first_action_agrees_with_the_strategy(self):
        order = vsrv.hand_order(self.solver)
        st, s = self.req("/api/strategy?id=r:0")
        self.assertEqual(st, 200, s)
        for hand in ("AhAd", "KsKd", "AsKs", "JsJh"):
            d = self.lines("r:0", "OOP", hand)
            self.assertEqual(d["actor"], "OOP")
            self.assertEqual([c["combo"] for c in d["combos"]], [hand])
            i = order.index(hand)
            want = {"b": 0.0, "x": 0.0}
            for child, row in zip(s["children"], s["freq"]):
                want["x" if child["id"].endswith(":c") else "b"] += row[i] or 0
            got = {"b": 0.0, "x": 0.0}
            for k, f in d["now"]["lines"]:
                got[d["lines"][k][0]] += f
            # lines under half a percent are left out, and frequencies are rounded to 4 places
            self.assertAlmostEqual(got["b"], want["b"], delta=0.02, msg=hand)
            self.assertAlmostEqual(got["x"], want["x"], delta=0.02, msg=hand)
            self.assertEqual(d["now"]["reach"], 1.0)

    def test_every_runout(self):
        d = self.lines("r:0", "OOP", "AA")
        self.assertEqual(d["board"], ["Qh", "Qc", "2c"])
        self.assertEqual(len(d["combos"]), 6)
        turns = d["now"]["cards"]
        self.assertEqual(len(turns), 49)                       # some AA sees every turn card
        self.assertEqual(len({t["card"] for t in turns}), 49)
        for t in turns:
            self.assertEqual(len(t["cards"]), 48, t["card"])
        self.assertEqual(self.check_cells(d), 1 + 49 + 49 * 48)
        # one combo does not see its own cards
        d1 = self.lines("r:0", "OOP", "AhAd")
        cards = [t["card"] for t in d1["now"]["cards"]]
        self.assertEqual(len(cards), 47)
        self.assertFalse({"Ah", "Ad"} & set(cards))
        self.assertTrue(all(len(t["cards"]) == 46 for t in d1["now"]["cards"]))
        # the way into a runout is a node of the tree, on that runout
        t = d1["now"]["cards"][5]
        r = t["cards"][7]
        st, n = self.req("/api/node?id=" + urllib.parse.quote("r:0:" + r["go"]))
        self.assertEqual(st, 200, n)
        self.assertEqual(n["node"]["board"].split(), ["Qh", "Qc", "2c", t["card"], r["card"]])

    def test_turn_and_river_nodes(self):
        d = self.lines("r:0:c:c:7d", "IP", "KK")
        self.assertEqual(d["board"][-1], "7d")
        self.assertEqual(len(d["now"]["cards"]), 48)
        self.assertTrue(all("cards" not in r for r in d["now"]["cards"]))
        self.assertEqual(self.check_cells(d), 49)
        d = self.lines("r:0:c:c:7d:c:c:3s", "IP", "KK")
        self.assertNotIn("cards", d["now"])
        self.assertAlmostEqual(sum(f for _, f in d["now"]["lines"]), 1.0, delta=0.02)

    def test_sizes(self):
        merged = self.lines("r:0", "OOP", "KsKd")
        split = self.lines("r:0", "OOP", "KsKd", sizes=True)
        self.assertTrue(split["sizes"] and not merged["sizes"])
        for label in merged["lines"]:
            self.assertRegex(label, r"^[xbcfr \-]+$")
        # the tree has one flop bet (60% of the pot) and all-ins; nothing else can be named
        firsts = {split["lines"][k].split(" ")[0] for k, _ in split["now"]["lines"]}
        for first in firsts:
            self.assertRegex(first, r"^(x(c|f|r|ra)?|b60(c|f|r|ra)?|ba)$")
        bets = lambda d: sum(f for k, f in d["now"]["lines"] if d["lines"][k][0] == "b")  # noqa: E731
        self.assertAlmostEqual(bets(merged), bets(split), delta=0.02)
        # a bet named all-in puts the whole stack in: Study names all-ins the same way
        st, p = self.req("/api/path?id=r:0")
        self.assertEqual(p["eff_stack"], CFG["stack"])
        allin = [l for l in split["lines"] if re.search(r"[br]a", l)]
        self.assertTrue(allin, split["lines"])

    def test_arguments(self):
        for query, want in (("id=r:0&player=OOP&hand=ZZ", "pick a hand"),
                            ("id=r:0&player=OOP&hand=AK", "suited or offsuit"),
                            ("id=r:0&player=OOP&hand=JJs", "a pair"),
                            ("id=r:0&player=OOP&hand=AhAh", "pick a hand"),
                            ("id=r:0&player=OOP&hand=", "pick a hand"),
                            ("id=r:0&player=BTN&hand=AA", "OOP or IP"),
                            ("id=r:0%0Ashow_version&player=OOP&hand=AA", "bad node id"),
                            ("id=r:0:c:c:c&player=OOP&hand=AA", "")):
            st, d = self.req("/api/hand_lines?" + query)
            self.assertEqual(st, 400, (query, d))
            self.assertIn(want, d["error"], query)
        # a hand the board takes, or one that is not in the range: no lines, and no error
        for hand, combos in (("QhQc", 0), ("72o", 9), ("99", 6)):
            d = self.lines("r:0", "OOP", hand)
            self.assertIsNone(d["now"], hand)
            self.assertEqual(d["lines"], [])
            self.assertEqual(len(d["combos"]), combos)
            self.assertTrue(all(c["reach"] == 0 for c in d["combos"]))
        # the one combo of QQ the board leaves
        d = self.lines("r:0", "OOP", "QQ")
        self.assertEqual([c["combo"] for c in d["combos"]], ["QsQd"])
        self.assertIsNotNone(d["now"])
        # where the hand has ended there is nothing to come
        st, n = self.req("/api/node?id=r:0:b24")
        fold = [c["id"] for c in n["children"] if c["id"].endswith(":f")][0]
        st, d = self.req("/api/hand_lines?player=OOP&hand=AA&id=" + urllib.parse.quote(fold))
        self.assertEqual(st, 400, d)
        self.assertIn("ended", d["error"])

    def test_expand_hand(self):
        order = vsrv.hand_order(self.solver)
        ex = lambda hand, board="Td9d2c": hl.expand_hand(vsrv, order, hand, board)  # noqa: E731
        self.assertEqual(len(ex("JJ")), 6)
        self.assertEqual(len(ex("JJ", "JdTd2c")), 3)
        self.assertEqual(len(ex("AKs")), 4)
        self.assertEqual(ex("KAs"), ex("AKs"))
        self.assertEqual(len(ex("AKo")), 12)
        self.assertEqual(len(ex("ATo")), 9)                    # the Td is on the board
        self.assertEqual(ex("KdAh"), ex("AhKd"))
        self.assertEqual(len(ex("AhKd")), 1)
        self.assertIn(ex("AhKd")[0], order)
        self.assertEqual(len(ex("AhKd, AsKs,KdAh")), 2)
        self.assertEqual(ex("Td9d"), [])
        for bad in ("AK", "JJs", "JJo", "ZZ", "AhAh", "", "Ah", "AhKdQs", "AA KK"):
            with self.assertRaises(vsrv.SolverError, msg=bad):
                ex(bad)


if __name__ == "__main__":
    unittest.main(argv=["test_hand_lines.py", "-v"] if sys.argv[1:] and not sys.argv[1].startswith("-")
                  else sys.argv)
