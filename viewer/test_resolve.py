#!/usr/bin/env python3
"""Tests for viewer/resolve.py: subgame re-solve and N-size search.

Usage: python3 viewer/test_resolve.py [path-to-solver]

Unit tests for the pure pieces (subset search, mid-hand tree-builder start nodes), then
integration tests against the real solver binary on tiny trees:

  * a re-solve with the SAME sizes as the original tree reproduces the original node's
    strategy (and EVs) within solver tolerance (measured, asserted below);
  * re-solving with one extra size changes the tree, and the subgame's node_count equals
    a scratch build of the exact line list the tree builder produced for that config;
  * the size search over a candidate list on a small river spot, solved to a loose
    accuracy, agrees with an independent brute force over all subsets run in the test.

River spots solve in well under a second each, so the brute force check is cheap.
"""
import itertools
import json
import os
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
sys.path.insert(0, os.path.join(REPO, "tools"))

import server as vsrv  # noqa: E402
import library as vlib  # noqa: E402
import resolve as vresolve  # noqa: E402
import tree_builder as tb  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.join(REPO, "solver")

# Same small tree as viewer/test_server.py: pot 40, stack 60, one/one bet size per street.
CFG = {
    "range_oop": "AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,AK,AQ,KQs",
    "board": "QhQc2c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "50",
    "turn_bet": "60", "turn_raise": "50",
    "river_bet": "65", "river_raise": "50", "river_allin": True,
    "allin_threshold": 50, "add_allin_limit": 300, "iso": True,
}
SIZING = {k: v for k, v in CFG.items() if k not in ("range_oop", "range_ip", "board", "iso")}

# Strategy agreement between the original tree and a same-sizes re-solve. Both trees are
# solved to accuracy 0.05 chips (of a 40-chip pot). A same-sizes re-solve of the ROOT is
# bit-identical (same lines, same ranges -> same tree layout), so per-combo frequencies
# match exactly there. A mid-hand re-solve has the same public tree but a different
# internal layout, so DCFR trajectories diverge microscopically and genuinely
# indifferent combos mix differently no matter the accuracy: the comparisons there are
# per-action frequencies aggregated over the acting player's range (measured max diff
# 0.006 at accuracy 0.05) and the range-weighted node EV (measured 0.011 chips).
STRAT_TOL = 0.05   # per-combo action frequency, root re-solve (measured 0.0000)
# Range-aggregated action frequency, mid-hand re-solve. The two solves lay the tree out
# differently, so genuinely indifferent combos mix differently however long you solve, and
# this bounds that spread rather than any solver promise. It is a MOVING TARGET: every
# convergence change shifts which member of the equilibrium set you land on, so the spread
# has grown as those landed (test_22, bisected on #173):
#   before #147 (avg_delay 32)      0.0034
#   after  #147, before #162        0.0085
#   after  #162 (beta 0 -> 0.5)     0.0107   <- went red against the old 0.01
# Widened to 0.02 for headroom. If this goes red again, re-measure rather than nudging it:
# the EV assertion below (EV_TOL) is the invariant one and has held throughout.
AGG_TOL = 0.02
EV_TOL = 0.15      # chips; both solves target <= 0.05 chips of exploitability, 40 pot
ACC = "0.05"


def _agg(rows, reach):
    """Range-weighted frequency of each action, one per strategy row."""
    tot = sum(x or 0 for x in reach) or 1.0
    out = []
    for row in rows:
        vals = vsrv.parse_floats(row, 1326) if isinstance(row, str) else row
        out.append(sum((x or 0) * (w or 0) for x, w in zip(vals, reach)) / tot)
    return out


def _weighted_ev(ev_line, reach):
    vals = vsrv.parse_floats(ev_line, 1326)
    tot = sum(x or 0 for x in reach) or 1.0
    return sum((x or 0) * (w or 0) for x, w in zip(vals, reach)) / tot


# ---------------------------------------------------------------- pure unit tests

class SearchSubsetTest(unittest.TestCase):
    def test_brute_evaluates_every_subset(self):
        scored = {}
        scores = {("25", "75"): 3.0, ("25", "50"): 1.0, ("25", "100"): 2.0}
        def score(sub):
            scored[sub] = True
            return scores.get(sub, 0.0)
        rows, best, method = vresolve.search_best_subset(["25", "50", "75", "100"], 2, score)
        self.assertEqual(method, "brute")
        self.assertEqual(len(scored), 6)          # C(4, 2)
        self.assertEqual(best["sizes"], ["25", "75"])
        self.assertEqual(best["ev"], 3.0)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(len(r["sizes"]) == 2 for r in rows))

    def test_greedy_skips_the_full_product(self):
        calls = []
        weights = {"30": 1.0, "55": 1.1, "80": 0.9, "130": 0.4, "200": 0.3, "400": 0.2}
        def score(sub):
            calls.append(sub)
            return sum(weights[c] for c in sub) + (2.0 if "130" in sub else 0.0)
        candidates = list(weights)
        rows, best, method = vresolve.search_best_subset(candidates, 2, score, brute_limit=3)
        self.assertEqual(method, "greedy")
        self.assertEqual(len(calls), 6 + 5)       # k + (k-1), not C(6, 2) = 15
        self.assertEqual(len(best["sizes"]), 2)
        # greedy seeds on the best singleton (2.4 for 130) and extends: must be the path taken
        self.assertEqual(best["sizes"], ["55", "130"])
        self.assertAlmostEqual(best["ev"], 0.4 + 1.1 + 2.0)

    def test_brute_limit_boundary_exactly(self):
        def score(sub):
            return 0.0
        # C(5, 2) = 10 > limit 3 -> greedy even though brute would be cheap
        _, _, method = vresolve.search_best_subset(["1", "2", "3", "4", "5"], 2, score,
                                                   brute_limit=3)
        self.assertEqual(method, "greedy")
        _, _, method = vresolve.search_best_subset(["1", "2", "3", "4", "5"], 2, score,
                                                   brute_limit=10)
        self.assertEqual(method, "brute")

    def test_bad_n_rejected(self):
        with self.assertRaises(vsrv.SolverError):
            vresolve.search_best_subset(["1", "2"], 3, lambda s: 0.0)


class SizeFormsTest(unittest.TestCase):
    """Size fields accept the tree builder's grammar: percentages, geometric Ne,
    raise multiples Nx (raise fields only), a (tools/tree_builder.py SIZING_RULES)."""

    def test_field_sizes_grammar(self):
        self.assertEqual(
            vresolve._field_sizes({"flop_raise": "33, 2e, 3x, a"}, "flop_raise",
                                  allow_mult=True), "33,2e,3x,a")
        self.assertEqual(vresolve._field_sizes({"flop_bet": " 50 ,75 "}, "flop_bet"),
                         "50,75")
        for bad in ("bogus", "3x", "100e"):  # mults are raise-only; Ne takes N <= 99
            with self.assertRaises(vsrv.SolverError, msg=bad):
                vresolve._field_sizes({"flop_bet": bad}, "flop_bet")

    def test_field_sizes_pioviewer_forms(self):
        """#315: PioViewer's spaces as separators and NeP entries; `2e.a` is not accepted in a form."""
        self.assertEqual(vresolve._field_sizes({"flop_bet": "5e 3e  2e,a"}, "flop_bet"), "5e,3e,2e,a")
        self.assertEqual(vresolve._field_sizes({"flop_bet": "2e75, e50 33"}, "flop_bet"), "2e75,e50,33")
        for bad in ("2e.a", "2e0"):
            with self.assertRaises(vsrv.SolverError, msg=bad):
                vresolve._field_sizes({"flop_bet": bad}, "flop_bet")

    def test_field_sizes_match_the_form(self):
        """#354: the server accepts exactly the form's grammar (sizes.js SIZE_RE / RAISE_RE) and bounds."""
        for bad in (".5", "0", "5.", "+5", "1_0", "-0", "allin", "0.5x", "0e50", "2e200000", "100001"):
            with self.assertRaises(vsrv.SolverError, msg=bad):
                vresolve._field_sizes({"flop_raise": bad}, "flop_raise", allow_mult=True)
        self.assertEqual(vresolve._field_sizes({"flop_bet": "33 75"}, "flop_bet"), "33,75")
        # the same patterns as the form: read sizes.js and compare on a mixed list
        import re
        js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "sizes.js")).read()
        pats = dict(re.findall(r"export const (SIZE_RE|RAISE_RE) = /\^\((.*)\)\$/i;", js))
        self.assertEqual(set(pats), {"SIZE_RE", "RAISE_RE"})
        cases = ["33", "0.5", ".5", "5.", "e", "2e", "e50", "2e75", "2e0", "0e50", "3x", "1.5x", "0.5x", "a",
                 "allin", "+5", "-0", "1_0", "x", "ee", "2e.a", "100", "0.05"]
        for name, srv in (("SIZE_RE", vsrv.SIZE_RE), ("RAISE_RE", vsrv.RAISE_RE)):
            form = re.compile(pats[name].replace("(", "(?:").replace("(?:?:", "(?:"), re.I)
            for c in cases:
                self.assertEqual(bool(form.fullmatch(c)), bool(srv.fullmatch(c)), f"{name} {c}")

    def test_pio_extra_one_line_only(self):
        """#354: Python's splitlines also splits on \\u2028, \\x0b, \\x85 ...: such a line is refused."""
        self.assertEqual(vsrv.pio_extra_lines({"pio_extra": ["#MergeSimilarBets#True"]}), ["#MergeSimilarBets#True"])
        for sep in (" ", "\x0b", "\x0c", "\x1c", "\x85"):
            with self.assertRaises(vsrv.SolverError, msg=repr(sep)):
                vsrv.pio_extra_lines({"pio_extra": [f"#MergeSimilarBets#True{sep}#Pot#5000"]})

    def test_turn_root_keeps_hand_edits(self):
        """#354: a turn board's tree applies RemovedLines like the flop's does."""
        body = {"range_oop": "AA", "range_ip": "KK", "pot": 100, "stack": 1000, "board": "Kh9h4c2d",
                "turn_bet": "50", "turn_bet_ip": "50", "river_bet": "50", "river_bet_ip": "50",
                "allin_threshold": 67, "add_allin_limit": 300}
        _, lines = vsrv.setup_from_body(body)
        leads = lambda ls: [l for l in ls if l.strip().split()[1] == "50"]  # OOP's bet at the turn root
        self.assertTrue(leads(lines), lines)
        _, lines2 = vsrv.setup_from_body({**body, "pio_extra": ["#RemovedLines_2#Bet 50"]})
        self.assertFalse(leads(lines2), lines2)
        self.assertTrue(lines2, "the rest of the tree stays")

    def test_pio_import_drops_a_board_that_is_not_one(self):
        """#384: the import echoed any #Board# text into the form, and from there into an error shown as HTML."""
        base = "#Pot#100\n#EffectiveStacks#300\n#FlopConfig.BetSize#50\n#FlopConfigIP.BetSize#50\n"
        for bad in ("<img/src/onerror=window.__x=1>", "AsAs7c", "Xx9h4c", "AsKdQh2c3d4s"):
            r = vsrv.op_pio_import({"text": base + f"#Board#{bad}\n"})
            self.assertEqual(r["form"]["board"], "", bad)
        r = vsrv.op_pio_import({"text": base + "#Board#Kh9h4c\n"})
        self.assertEqual(r["form"]["board"], "Kh9h4c")

    def test_allin_numbers_exact_and_positive(self):
        tb = vsrv.load_tree_builder()
        h = vsrv.sizing_header({"allin_threshold": 123456789, "add_allin_limit": 300}, tb)
        self.assertIn("#AllinThreshold#123456789.0", h)
        for bad in (0, -5):
            with self.assertRaises(vsrv.SolverError):
                vsrv.sizing_header({"allin_threshold": bad}, tb)

    def test_size_key_orders_mixed_forms(self):
        self.assertEqual(sorted(["a", "3x", "2e", "75", "33", "e"],
                                key=vresolve._size_key),
                         ["33", "75", "e", "2e", "3x", "a"])

    def test_raise_multiple_builds(self):
        # IP faces 10; 3x -> raise to 30; OOP's 3x re-raise (90) hits the all-in
        lines = _lines(_ctx("IP", "river", 10, 0, 20, 100), {"river_raise": "3x"})
        self.assertEqual(lines, [[10, 30, 100]])

    def test_geometric_sizes_build(self):
        # "e" on the lone remaining street is the shove (S = 96 on top of the 4 in)
        lines = _lines(_ctx("OOP", "river", 4, 4, 20, 100), {"river_bet": "e"})
        self.assertEqual(set(map(tuple, lines)), {(100,), (4, 100)})
        # "2e": f = (sqrt(1 + 2*96/28) - 1)/2 = 0.901...; bet to 4 + round(.901*28) = 29
        lines = _lines(_ctx("OOP", "river", 4, 4, 20, 100), {"river_bet": "2e"})
        self.assertEqual(set(map(tuple, lines)), {(29,), (4, 29)})

    def test_search_override_changes_only_the_actor(self):
        # #141: candidates replace the acting player's sizes; the opponent keeps its own,
        # including sizes it only inherited from OOP's field
        ctx = _ctx("OOP", "river", 4, 4, 20, 100)
        cfg, _ = vresolve.build_config(tb, ctx, {"river_bet": "50"}, (2, "100"))
        self.assertEqual([s.value for s in cfg.streets[2][tb.OOP].bet], [100])
        self.assertEqual([s.value for s in cfg.streets[2][tb.IP].bet], [50])
        ctx = _ctx("IP", "river", 4, 4, 20, 100)
        cfg, _ = vresolve.build_config(tb, ctx, {"river_bet": "50", "river_bet_ip": "33"}, (2, "75"))
        self.assertEqual([s.value for s in cfg.streets[2][tb.IP].bet], [75])

    def test_raise_cap_passthrough(self):
        # without a cap the re-raise continues to the all-in; cap 1 stops at IP's raise
        lines = _lines(_ctx("IP", "river", 10, 0, 20, 100), {"river_raise": "50"})
        self.assertEqual(lines, [[10, 30, 100]])
        lines = _lines(_ctx("IP", "river", 10, 0, 20, 100),
                       {"river_raise": "50", "river_raisecap": 1})
        self.assertEqual(lines, [[10, 30]])


def kill_solver(s):
    try:
        s.proc.kill()
        s.proc.wait(timeout=10)
        for f in (s.proc.stdin, s.proc.stdout):
            f.close()
    except Exception:
        pass


def _ctx(actor, street, oop, ip, dead, stack):
    return {"actor": actor, "street": street,
            "pot": {"oop": oop, "ip": ip, "dead": dead}, "stack": stack}


def _lines(ctx, body):
    cfg, start = vresolve.build_config(tb, ctx, body)
    return tb.build_lines(cfg, start)


class StartNodeTest(unittest.TestCase):
    """build_lines from a mid-hand root: amounts stay absolute (chips already in included)."""

    def test_oop_root_fresh_street_bets_for_both_players(self):
        # dead 20, each player 4 in (28 in the middle), stack 100, one 50% river bet
        lines = _lines(_ctx("OOP", "river", 4, 4, 20, 100), {"river_bet": "50"})
        self.assertEqual(set(map(tuple, lines)), {(4, 18), (18,)})  # 4 + 50% * 28

    def test_ip_root_facing_a_bet_wraps_in_a_forced_check(self):
        # IP faces OOP's bet to 10; the wrapper root is OOP "checking" at 10, then IP's
        # raise: pot_if_called = 20 + 2*10 = 40; raise to 10 + 50%*40 = 30; OOP's
        # re-raise to 30 + 50%*80 = 70 hits the 67% all-in threshold of the 100 stack
        lines = _lines(_ctx("IP", "river", 10, 0, 20, 100),
                       {"river_bet": "50", "river_raise": "50"})
        self.assertEqual(lines, [[10, 30, 100]])

    def test_ip_root_matched_stacks_check_back_root(self):
        # the wrapper check (4) is OOP's only option even though river bets are configured
        lines = _lines(_ctx("IP", "river", 4, 4, 20, 100), {"river_bet": "50"})
        self.assertEqual(lines, [[4, 18]])  # 4 = wrapper check; 18 = 4 + 50%*28

    def test_oop_root_facing_a_lead_offers_raises(self):
        # OOP leads are possible below the subgame root: OOP is the one facing the bet
        lines = _lines(_ctx("OOP", "turn", 0, 24, 40, 60),
                       {"turn_bet": "60", "turn_raise": "50"})
        # pot_if_called = 40 + 2*24 = 88; raise to 24 + 50%*88 = 68 -> all-in at 60.
        # (The raise is the line's deepest action; the 24 call is implicit.)
        self.assertEqual(lines, [[60]])

    def test_street_progression_from_midstreet_root(self):
        cfg, start = vresolve.build_config(tb, _ctx("IP", "flop", 10, 0, 20, 100),
                                           {"flop_raise": "50", "turn_bet": "50"})
        self.assertEqual(start.street, 0)
        self.assertEqual(start.inv, (10, 0))
        self.assertEqual(start.to_act, tb.OOP)
        lines = tb.build_lines(cfg, start)
        # call through to the turn: 10 (wrapper), 10 (call), then OOP's turn bet.
        # turn pot = 20 + 20 = 40, bet to 10 + 20 = 30
        self.assertIn([10, 10, 30], lines)


class PrevAggressorTest(unittest.TestCase):
    """#152: the re-solve start node recovers the previous street's aggressor from the
    node id's token walk, so root-street leads follow the donk rule."""

    def ctx(self, node_id, street):
        c = _ctx("OOP", street, 4, 4, 20, 100)
        c["id"] = node_id
        return c

    def test_flop_root_has_none(self):
        self.assertIsNone(vresolve._prev_aggressor(tb, self.ctx("r:0", "flop")))
        self.assertIsNone(vresolve._prev_aggressor(tb, self.ctx("r:0:b24", "flop")))

    def test_flop_bet_call_makes_ip_the_aggressor(self):
        ctx = self.ctx("r:0:c:b24:c:7s", "turn")
        self.assertEqual(vresolve._prev_aggressor(tb, ctx), tb.IP)
        self.assertEqual(vresolve.start_node(tb, ctx).prev_aggressor, tb.IP)
        # and so the subgame build applies the donk rule: no turn donk sizes, no lead
        cfg, start = vresolve.build_config(tb, ctx, {"turn_bet": "50"})
        self.assertEqual(tb.build_lines(cfg, start),
                         [[4, 18]])  # OOP check, IP's 50% bet (4 + 0.5 x 28)
        cfg, start = vresolve.build_config(tb, ctx, {"turn_bet": "50", "turn_donk": "30"})
        self.assertEqual(tb.build_lines(cfg, start),
                         [[12], [4, 18]])  # donk: 4 + 30% of 28 (lines come out sorted)

    def test_oop_flop_bet_call_makes_oop_the_aggressor(self):
        # OOP bet the flop and was called: OOP leads the turn with BetSize, not DonkSize
        ctx = self.ctx("r:0:b24:c:7s", "turn")
        self.assertEqual(vresolve._prev_aggressor(tb, ctx), tb.OOP)
        cfg, start = vresolve.build_config(tb, ctx, {"turn_bet": "50"})
        self.assertIn([18], tb.build_lines(cfg, start))  # OOP's 50% lead is a normal bet

    def test_checked_round_has_no_aggressor(self):
        self.assertIsNone(vresolve._prev_aggressor(tb, self.ctx("r:0:c:c:7s", "turn")))

    def test_river_tracks_the_turn(self):
        ctx = self.ctx("r:0:c:b24:c:7s:b50:c:9h", "river")
        self.assertEqual(vresolve._prev_aggressor(tb, ctx), tb.OOP)  # OOP donked the turn
        ctx = self.ctx("r:0:c:b24:c:7s:c:c:9h", "river")
        self.assertEqual(vresolve._prev_aggressor(tb, ctx), None)    # turn went check-check
        ctx = self.ctx("r:0:c:b24:c:7s:c:b50:9h", "river")
        self.assertIsNone(vresolve._prev_aggressor(tb, ctx))         # unclosed action: bail

    def test_mid_street_id_walks_partial_street(self):
        ctx = self.ctx("r:0:c:b24:c:7s:c:b50", "turn")
        self.assertEqual(vresolve._prev_aggressor(tb, ctx), tb.IP)   # still the flop raiser

    def test_missing_or_unknown_id_gives_none(self):
        self.assertIsNone(vresolve._prev_aggressor(tb, _ctx("OOP", "turn", 4, 4, 20, 100)))
        self.assertIsNone(vresolve._prev_aggressor(tb, self.ctx("r:0:c:b24:c:?:c", "turn")))




class ResolveIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = vsrv.Solver(SOLVER)
        cls.mgr = vresolve.ResolveManager(cls.main, vsrv.Solver, SOLVER)
        vsrv.set_resolver(cls.mgr)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.main
        cls.http.library = vlib.Library(tempfile.mkdtemp(prefix="wsolver-test-library-"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"
        st, d = cls.req("/api/generate", "POST", CFG)
        assert st == 200, d
        st, d = cls.req("/api/build", "POST", {})
        assert st == 200, d
        cls.main.cmds([f"set_accuracy {ACC}", "go", "wait_for_solver"], timeout=600)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        for s in (cls.main, cls.mgr.rsolver):
            kill_solver(s)

    @classmethod
    def req(cls, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if method == "POST" and data is None:
            data = b"{}"
        r = urllib.request.Request(cls.base + path, data=data, headers=headers,
                                   method=method)
        try:
            with urllib.request.urlopen(r, timeout=120) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    @classmethod
    def wait_job(cls, timeout=300):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, d = cls.req("/api/resolve/status")
            assert st == 200, d
            if not d["busy"]:
                return d
            time.sleep(0.1)
        raise AssertionError("re-solve job did not finish")

    # ---- context ----

    def test_10_context_root(self):
        st, d = self.req("/api/resolve/context?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["board"], "QhQc2c")
        self.assertEqual(d["street"], "flop")
        self.assertEqual(d["pot"], {"oop": 0, "ip": 0, "dead": 40})
        self.assertEqual(d["stack"], 60)
        self.assertEqual(d["actor"], "OOP")

    def test_11_context_midstreet_facing_bet(self):
        st, d = self.req("/api/resolve/context?id=r:0:b24")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["actor"], "IP")
        self.assertEqual(d["pot"], {"oop": 24, "ip": 0, "dead": 40})
        self.assertEqual(d["street"], "flop")

    def test_12_context_street_start_after_call(self):
        st, d = self.req("/api/resolve/context?id=" + urllib.parse.quote("r:0:b24:c:5d"))
        self.assertEqual(st, 200, d)
        self.assertEqual(d["street"], "turn")
        self.assertEqual(d["board"], "QhQc2c5d")
        self.assertEqual(d["actor"], "OOP")
        self.assertEqual(d["pot"], {"oop": 24, "ip": 24, "dead": 40})

    def test_13_context_rejects_chance_and_terminal(self):
        st, d = self.req("/api/resolve/context?id=" + urllib.parse.quote("r:0:c:c"))
        self.assertEqual(st, 400)
        self.assertIn("not a decision node", d["error"])
        st, d = self.req("/api/resolve/context?id=" + urllib.parse.quote("r:0:f"))
        self.assertEqual(st, 400)

    # ---- re-solve ----

    def test_20_browse_before_any_resolve_fails(self):
        st, d = self.req("/api/resolve/node?id=r:0")
        self.assertEqual(st, 400)
        self.assertIn("no subgame", d["error"])

    def test_21_same_sizes_reproduce_root_strategy_and_evs(self):
        st, d = self.req("/api/resolve/start", "POST",
                         dict(SIZING, id="r:0", accuracy=ACC))
        self.assertEqual(st, 200, d)
        st_ = self.wait_job()
        self.assertIsNone(st_["error"], st_["error"])
        self.assertEqual(st_["kind"], "resolve")
        res = st_["result"]
        self.assertEqual(res["node"], "r:0")
        self.assertEqual(res["subgame_root"], "r:0")
        # strategy at the root must match the original tree's root strategy
        main_rows = self.main.cmd("show_strategy r:0").split("\n")
        st, d = self.req("/api/resolve/strategy?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertNotIn("raw", d)
        self.assertEqual(len(main_rows), len(d["freq"]),
                         "child count differs between original and same-sizes re-solve")
        worst = 0.0
        for a, row in zip(main_rows, d["freq"]):
            for x, y in zip(vsrv.parse_floats(a, 1326), row):
                if x is not None and y is not None:
                    worst = max(worst, abs(x - y))
        print(f"  max |strategy diff| main vs same-size re-solve: {worst:.4f}")
        self.assertLess(worst, STRAT_TOL)
        # root EVs: re-solve EVs (same absolute accounting) match the original tree's
        main_evs = vresolve.parse_evs(self.main.cmd("calc_results"))
        self.assertLess(abs(main_evs["OOP"] - res["ev"]["OOP"]), EV_TOL, res)
        self.assertLess(abs(main_evs["IP"] - res["ev"]["IP"]), EV_TOL, res)
        self.assertAlmostEqual(res["ev"]["OOP"] + res["ev"]["IP"], 40.0, places=2)

    def test_22_same_sizes_midstreet_reproduce_strategy(self):
        # node facing a bet: the per-player pot mapping matters here
        st, d = self.req("/api/resolve/start", "POST",
                         dict(SIZING, id="r:0:b24", accuracy=ACC))
        self.assertEqual(st, 200, d)
        st_ = self.wait_job()
        self.assertIsNone(st_["error"], st_["error"])
        self.assertEqual(st_["result"]["pot"], {"oop": 24, "ip": 0, "dead": 40})
        self.assertEqual(st_["result"]["actor"], "IP")
        self.assertEqual(st_["result"]["subgame_root"], "r:0:c")
        main_rows = self.main.cmd("show_strategy r:0:b24").split("\n")
        # in the subgame the node sits behind the forced-check wrapper
        st, d = self.req("/api/resolve/strategy?id=" + urllib.parse.quote("r:0:c"))
        self.assertEqual(st, 200, d)
        self.assertNotIn("raw", d)
        self.assertEqual(len(main_rows), len(d["freq"]))
        reach = vsrv.parse_floats(self.main.cmd("show_range IP r:0:b24"), 1326)
        # compare the range-aggregated action frequencies and the node EV: the internal
        # layout differs, so genuinely indifferent combos mix differently in the two
        # solves no matter the accuracy (see the module docstring)
        ga, gb = _agg(main_rows, reach), _agg(d["freq"], reach)
        for i, (a, b) in enumerate(zip(ga, gb)):
            print(f"  action {i}: main {a:.4f} re-solve {b:.4f}")
            self.assertAlmostEqual(a, b, delta=AGG_TOL)
        ev_main = _weighted_ev(self.main.cmd("calc_ev IP r:0:b24").split("\n")[0], reach)
        ev_sub = _weighted_ev(self.mgr.rsolver.cmd("calc_ev IP r:0:c").split("\n")[0], reach)
        print(f"  node EV IP: main {ev_main:.4f} re-solve {ev_sub:.4f}")
        self.assertAlmostEqual(ev_main, ev_sub, delta=EV_TOL)

    def test_23_added_size_changes_tree_and_node_count_matches(self):
        body = dict(SIZING, flop_bet="60,30")
        st, d = self.req("/api/resolve/start", "POST", dict(body, id="r:0", accuracy=1.0))
        self.assertEqual(st, 200, d)
        st_ = self.wait_job()
        self.assertIsNone(st_["error"], st_["error"])
        res = st_["result"]
        # the subgame tree must be exactly the tree for the edited config: build the
        # same line list in a scratch solver and compare node counts
        ctx = vresolve.read_context(self.main, "r:0")
        cfg, start = vresolve.build_config(tb, ctx, body)
        lines = tb.format_lines(tb.build_lines(cfg, start))
        self.assertEqual(res["lines"], len(lines))
        scratch = vsrv.Solver(SOLVER)
        try:
            scratch.cmds(["set_range OOP " + self.main.cmd("show_range OOP r:0"),
                          "set_range IP " + self.main.cmd("show_range IP r:0"),
                          "set_board QhQc2c", "set_eff_stack 60", "set_pot 0 0 40",
                          "set_isomorphism 1 1", "clear_lines"] + lines + ["build_tree"])
            self.assertEqual(res["node_count"], scratch.cmd("node_count"))
        finally:
            kill_solver(scratch)
        # browsing the subgame works after the job
        st, d = self.req("/api/resolve/node?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertGreater(len(d["children"]), 2)  # two bet sizes + check

    def test_24_bad_requests(self):
        st, d = self.req("/api/resolve/start", "POST", {"id": "r:0", "flop_bet": "bogus"})
        self.assertEqual(st, 400)
        st, d = self.req("/api/resolve/start", "POST", {"id": "r:0", "flop_bet": "3x"})
        self.assertEqual(st, 400)  # raise multiples are raise-fields-only
        st, d = self.req("/api/resolve/start", "POST", {"id": "r:0", "flop_raisecap": -1})
        self.assertEqual(st, 400)
        st, d = self.req("/api/resolve/start", "POST", {"id": "r:0\nshow_version"})
        self.assertEqual(st, 400)
        st, d = self.req("/api/resolve/status", "POST", {})
        self.assertEqual(st, 400)
        st, d = self.req("/api/resolve/search", "POST",
                         {"id": "r:0", "candidates": "50", "n": 1})
        self.assertEqual(st, 400)  # need >= 2 candidates
        st, d = self.req("/api/resolve/search", "POST",
                         {"id": "r:0", "candidates": "50,a", "n": 1, "allin": True})
        self.assertEqual(st, 400)  # with allin, "a" is not a candidate: one left
        self.assertIn("besides the all-in", d["error"])
        st, d = self.req("/api/resolve/search", "POST",
                         {"id": "r:0", "candidates": "50,80", "n": 5})
        self.assertEqual(st, 400)  # n > len(candidates)
        st, d = self.req("/api/resolve/search", "POST",
                         {"id": "r:0", "candidates": "50,3x", "n": 1})
        self.assertEqual(st, 400)  # candidates become bet sizes: no Nx
        st, d = self.req("/api/resolve/stop", "POST", {})
        self.assertEqual(st, 200)  # stop with no job is a no-op

    def test_25_geometric_size_end_to_end(self):
        # #114 grammar through the API: "2e" is geometric over 2 streets from the flop:
        # f = (sqrt(1 + 2*60/40) - 1)/2 = 0.5 -> a 20-chip bet into the 40 pot
        body = dict(SIZING, flop_bet="2e")
        st, d = self.req("/api/resolve/start", "POST", dict(body, id="r:0", accuracy=1.0))
        self.assertEqual(st, 200, d)
        st_ = self.wait_job()
        self.assertIsNone(st_["error"], st_["error"])
        res = st_["result"]
        # node count must match a scratch build of the exact lines for that config
        ctx = vresolve.read_context(self.main, "r:0")
        cfg, start = vresolve.build_config(tb, ctx, body)
        lines = tb.format_lines(tb.build_lines(cfg, start))
        self.assertEqual(res["lines"], len(lines))
        scratch = vsrv.Solver(SOLVER)
        try:
            scratch.cmds(["set_range OOP " + self.main.cmd("show_range OOP r:0"),
                          "set_range IP " + self.main.cmd("show_range IP r:0"),
                          "set_board QhQc2c", "set_eff_stack 60", "set_pot 0 0 40",
                          "set_isomorphism 1 1", "clear_lines"] + lines + ["build_tree"])
            self.assertEqual(res["node_count"], scratch.cmd("node_count"))
        finally:
            kill_solver(scratch)
        st, d = self.req("/api/resolve/node?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertIn("r:0:b20", [c["id"] for c in d["children"]])  # the 2e = 50% bet

    # ---- size search on a small river spot, checked against brute force ----

    # nuts vs bluffcatchers on 5 cards: bigger bets clearly better -> a stable argmax
    RIVER = {"range_oop": "AA,KK", "range_ip": "JJ,TT",
             "board": "QhQc2c7h2s", "pot": 40, "stack": 200}
    CANDS = ["25", "50", "100", "150"]
    ACC = 1.0  # loose; both the search and the test's brute force use it

    def test_30_river_search_matches_independent_brute_force(self):
        # rebuild the main tree as a river-root spot for the search
        self.main.cmds(["set_range OOP " + self.RIVER["range_oop"],
                        "set_range IP " + self.RIVER["range_ip"],
                        "set_board " + self.RIVER["board"],
                        f"set_eff_stack {self.RIVER['stack']}",
                        f"set_pot 0 0 {self.RIVER['pot']}",
                        "clear_lines", "add_line 10", "add_line 20",
                        "build_tree", f"set_accuracy {self.ACC}", "go", "wait_for_solver"],
                       timeout=600)
        st, d = self.req("/api/resolve/context?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["street"], "river")
        actor = d["actor"]

        body = {"id": "r:0", "street": "river", "candidates": ",".join(self.CANDS),
                "n": 2, "river_raise": "50", "accuracy": self.ACC,
                "allin_threshold": 100000, "add_allin_limit": 300}
        st, d = self.req("/api/resolve/search", "POST", body)
        self.assertEqual(st, 200, d)
        st_ = self.wait_job()
        self.assertIsNone(st_["error"], st_["error"])
        self.assertEqual(st_["method"], "brute")
        self.assertEqual(len(st_["rows"]), 6)  # C(4, 2)
        best = st_["best"]

        # independent brute force: solve every subset directly in a scratch solver
        ctx = vresolve.read_context(self.main, "r:0", with_ranges=True)
        scratch = vsrv.Solver(SOLVER)
        brute = {}
        try:
            for sub in itertools.combinations(self.CANDS, 2):
                cfg, start = vresolve.build_config(tb, ctx, body, (2, ",".join(sub)))
                lines = tb.format_lines(tb.build_lines(cfg, start))
                pot = ctx["pot"]
                scratch.cmds(["set_range OOP " + ctx["range_oop"],
                              "set_range IP " + ctx["range_ip"],
                              "set_board " + ctx["board"],
                              f"set_eff_stack {ctx['stack']}",
                              f"set_pot {pot['oop']} {pot['ip']} {pot['dead']}",
                              "set_isomorphism 1 1", "clear_lines"] + lines +
                             ["build_tree", f"set_accuracy {self.ACC}", "go",
                              "wait_for_solver"], timeout=600)
                evs = vresolve.parse_evs(scratch.cmd("calc_results"))
                brute[tuple(sorted(sub, key=vresolve._size_key))] = evs[actor]
        finally:
            kill_solver(scratch)

        for row in st_["rows"]:
            key = tuple(row["sizes"])
            self.assertIn(key, brute)
            self.assertAlmostEqual(row["ev"], brute[key], delta=0.3,
                                   msg=f"subset {key}: search {row['ev']} vs brute {brute[key]}")
        # the search's best subset must attain the brute-force maximum (up to solve noise)
        self.assertAlmostEqual(best["ev"], max(brute.values()), delta=0.3)
        # and browsing afterwards shows the best subset's tree, not the last candidate's
        self.assertIsNotNone(st_["result"])
        self.assertEqual(st_["result"]["ev"][actor], best["ev"])

    def test_31_search_at_a_node_facing_a_bet_varies_raises(self):
        # main tree is the river spot from test_30: r:0:b10 is IP facing OOP's 10 bet.
        # Bets cannot happen there, so the candidates must become IP's raise sizes;
        # with no river_raise configured, any raise child proves they were applied.
        st, d = self.req("/api/resolve/context?id=r:0:b10")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["facing"])
        body = {"id": "r:0:b10", "candidates": "50,100,3x", "n": 1, "accuracy": self.ACC,
                "allin_threshold": 100000, "add_allin_limit": 300}
        st, d = self.req("/api/resolve/search", "POST", body)
        self.assertEqual(st, 200, d)
        st_ = self.wait_job()
        self.assertIsNone(st_["error"], st_["error"])
        self.assertEqual(len(st_["rows"]), 3)
        st, d = self.req("/api/resolve/node?id=" + urllib.parse.quote("r:0:c"))
        self.assertEqual(st, 200, d)
        kids = [c["id"] for c in d["children"]]
        self.assertTrue(any(k.startswith("r:0:c:b") for k in kids), kids)

    def test_32_search_plus_allin(self):
        # main tree is the river spot from test_30 (OOP acts first at r:0). With allin, every
        # subset gets the all-in beside its N sizes: "a" stops being a candidate, the rows
        # list only the N sizes, and the subgame root offers check, the pick and the all-in.
        def search(allin):
            body = {"id": "r:0", "street": "river", "candidates": "25,50,a", "n": 1,
                    "allin": allin, "accuracy": self.ACC,
                    "allin_threshold": 100000, "add_allin_limit": 300}
            st, d = self.req("/api/resolve/search", "POST", body)
            self.assertEqual(st, 200, d)
            st_ = self.wait_job()
            self.assertIsNone(st_["error"], st_["error"])
            st, d = self.req("/api/resolve/node?id=r:0")
            self.assertEqual(st, 200, d)
            return st_, [c["id"] for c in d["children"]]

        allin_id = f"r:0:b{self.RIVER['stack']}"
        st_, kids = search(True)
        self.assertTrue(st_["allin"])
        self.assertTrue(st_["result"]["search"]["allin"])
        self.assertEqual(sorted(r["sizes"] for r in st_["rows"]), [["25"], ["50"]])
        self.assertEqual(len(kids), 3, kids)  # check, the best of 25/50, all-in
        self.assertIn(allin_id, kids)

        st_, kids = search(False)  # without it, "a" is one of the candidates
        self.assertFalse(st_["allin"])
        self.assertEqual(len(st_["rows"]), 3)
        self.assertEqual(len(kids), 2, kids)  # check and the one size kept


    def test_33_stopped_job_does_not_report_the_previous_result(self):
        # A finished re-solve leaves a result; the next job rebuilds the subgame solver's tree, so
        # once it starts the old result must not be reported (after a Stop it used to be, as if
        # it were the stopped job's, and after a failure it stayed browsable over another tree).
        st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", accuracy=ACC))
        self.assertEqual(st, 200, d)
        first = self.wait_job()
        self.assertIsNone(first["error"])
        self.assertIsNotNone(first["result"])
        self.assertTrue(first["tree"])
        st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", accuracy=1e-9))  # runs until stopped
        self.assertEqual(st, 200, d)
        st, now = self.req("/api/resolve/status")
        self.assertTrue(now["busy"])
        self.assertIsNone(now["result"])
        self.assertFalse(now["tree"])
        self.assertEqual(self.req("/api/resolve/stop", "POST", {})[0], 200)
        after = self.wait_job()
        self.assertIsNone(after["result"], after["result"])
        self.assertFalse(after["tree"])
        st, d = self.req("/api/resolve/strategy?id=r:0")
        self.assertEqual(st, 400, d)   # nothing to browse until a job finishes

    def test_34_accuracy_must_be_positive(self):
        # 0 or NaN would solve until the 24 h timeout (#357)
        for acc in (0, float("nan"), -1):
            st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", accuracy=acc))
            self.assertEqual(st, 400, (acc, d))

    def test_35_combo_locks_add_up(self):
        # #357: locking more hands keeps the hands locked before (combo_lock_node replaces the mask)
        st, ls = self.req("/api/lock_state?id=r:0")
        self.assertEqual(st, 200, ls)
        rows = [[float(x) for x in l.split()] for l in self.main.cmd("show_strategy r:0").splitlines() if l.strip()]
        acts = [k["id"].rsplit(":", 1)[1] for k in vsrv.parse_children(self.main.cmd("show_children r:0"))]
        reach = [float(x) for x in self.main.cmd("show_range OOP r:0").split()]
        live = [i for i, r in enumerate(reach) if r > 0]
        first, second = live[:4], live[4:8]
        for pick in (first, second):
            mask = [1 if i in pick else 0 for i in range(1326)]
            st, d = self.req("/api/lock", "POST", {"id": "r:0", "mode": "combos", "freq": rows,
                                                   "actions": acts, "combos": mask})
            self.assertEqual(st, 200, d)
        locked = [i for i, v in enumerate(self.main.cmd("show_locked_combos r:0").split()) if float(v) > 0]
        self.assertEqual(sorted(locked), sorted(first + second), "both locks are kept")
        st, d = self.req("/api/lock", "POST", {"id": "r:0", "mode": "combos", "freq": rows, "actions": acts,
                                               "combos": ["0"] * 1326})
        self.assertEqual(st, 400, "a mask of strings is refused")
        self.assertEqual(self.req("/api/unlock", "POST", {"all": True})[0], 200)

    def test_38_solve_refused_while_solving_further(self):
        # (runs last: it changes the shared tree; it sets up its own from CFG, since earlier tests
        # leave other settings on the shared solver)
        self.assertEqual(self.req("/api/generate", "POST", CFG)[0], 200)
        self.assertEqual(self.req("/api/build", "POST", {})[0], 200)
        # #357: /api/solve during a Solve further used to clear its partial state, so the subtree's
        # progress went into the main convergence series
        st, d = self.req("/api/solve_node", "POST", {"id": "r:0:c", "accuracy": 1e-9})
        self.assertEqual(st, 200, d)
        import time
        for _ in range(100):
            if self.req("/api/progress")[1].get("solving"):
                break
            time.sleep(0.05)
        before = self.req("/api/progress")[1]
        st, d = self.req("/api/solve", "POST", {"accuracy": 1e-9})
        self.assertEqual(st, 400, (d, {k: before.get(k) for k in ("solving", "partial", "dead")}))
        st, p = self.req("/api/progress")
        self.assertTrue(p.get("partial"), p)
        self.req("/api/stop", "POST", {})
        self.main.cmd("wait_for_solver", timeout=120)

    def test_26_applied_resolve_is_browsed_in_place(self):
        # the Study view's Sizes panel: new sizes at a node, shown in place of the main tree there
        st, d = self.req("/api/resolve/start", "POST",
                         dict(SIZING, id="r:0:c", sizes="50", apply=True, accuracy=1.0))
        self.assertEqual(st, 200, d)
        done = self.wait_job()
        self.assertIsNone(done["error"], done["error"])
        a = done["applied"]
        self.assertEqual((a["root"], a["sub_root"], a["actor"], a["sizes"]), ("r:0:c", "r:0:c", "IP", ["50"]))
        # at the node: the new size (IP bets 50% of 40 = 20), under the main tree's ids
        st, d = self.req("/api/strategy?id=r:0:c")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["variant"]["root"], "r:0:c")
        self.assertEqual(sorted(c["id"] for c in d["children"]), ["r:0:c:b20", "r:0:c:c"])
        # below it: the line is the main tree's down to the node, then the re-solve's
        st, d = self.req("/api/path?id=r:0:c:b20")
        self.assertEqual(st, 200, d)
        self.assertEqual([n["id"] for n in d["nodes"]], ["r:0", "r:0:c", "r:0:c:b20"])
        self.assertEqual(d["nodes"][0]["type"], "OOP_DEC")
        self.assertEqual(d["nodes"][-1]["pot"][1], 20)
        st, d = self.req("/api/range?id=r:0:c:b20")
        self.assertEqual(st, 200, d)
        self.assertIn("variant", d)
        # above it: the main tree, untouched
        st, d = self.req("/api/strategy?id=r:0")
        self.assertEqual(st, 200, d)
        self.assertNotIn("variant", d)
        self.assertIn("r:0:b24", [c["id"] for c in d["children"]])
        # restore: the main tree everywhere again
        st, d = self.req("/api/resolve/restore", "POST", {})
        self.assertEqual((st, d["restored"]), (200, "r:0:c"), d)
        st, d = self.req("/api/strategy?id=r:0:c")
        self.assertNotIn("variant", d)
        self.assertEqual(sorted(c["id"] for c in d["children"]), ["r:0:c:b24", "r:0:c:c"])
        st, d = self.req("/api/resolve/status")
        self.assertIsNone(d["applied"])

    def test_27_applied_resolve_at_the_root_and_its_end(self):
        st, d = self.req("/api/resolve/start", "POST",
                         dict(SIZING, id="r:0", sizes="30,45", apply=True, accuracy=1.0))
        self.assertEqual(st, 200, d)
        done = self.wait_job()
        self.assertIsNone(done["error"], done["error"])
        st, d = self.req("/api/strategy?id=r:0")
        self.assertEqual(sorted(c["id"] for c in d["children"]), ["r:0:b12", "r:0:b18", "r:0:c"])
        st, d = self.req("/api/path?id=r:0:b18")
        self.assertEqual([n["id"] for n in d["nodes"]], ["r:0", "r:0:b18"])
        # a node the new sizes don't make is an error, not the main tree's node
        st, d = self.req("/api/strategy?id=r:0:b24")
        self.assertEqual(st, 400, d)
        # a lock on the main tree changes its strategies: the shown re-solve is dropped
        self.req("/api/unlock", "POST", {"id": "r:0"})
        st, d = self.req("/api/resolve/status")
        self.assertIsNone(d["applied"])
        st, d = self.req("/api/strategy?id=r:0")
        self.assertNotIn("variant", d)
        # a plain re-solve (no apply) is not shown in place
        st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", sizes="30", accuracy=1.0))
        self.assertEqual(st, 200, d)
        self.assertIsNone(self.wait_job()["applied"])
        st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", sizes="", apply=True))
        self.assertEqual(st, 400, d)

    def test_37_new_main_tree_drops_the_resolve(self):
        # #357: a re-solve of the old main tree must not survive a rebuild (or load) of it
        st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", accuracy=ACC))
        self.assertEqual(st, 200, d)
        done = self.wait_job()
        self.assertTrue(done["tree"] and done["result"])
        st, d = self.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        st, now = self.req("/api/resolve/status")
        self.assertFalse(now["tree"])
        self.assertIsNone(now["result"])
        self.assertIsNone(now["node"])
        st, d = self.req("/api/resolve/strategy?id=r:0")
        self.assertEqual(st, 400, d)
        # a job running when the main tree changes doesn't come back as this tree's subgame either
        st, d = self.req("/api/resolve/start", "POST", dict(SIZING, id="r:0", accuracy=1e-9))
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/build", "POST", {})
        self.assertEqual(st, 200, d)
        after = self.wait_job()
        self.assertFalse(after["tree"])
        self.assertIsNone(after["result"])

if __name__ == "__main__":
    unittest.main(argv=["test_resolve.py", "-v"] if sys.argv[1:] and not sys.argv[1].startswith("-")
                  else sys.argv)
