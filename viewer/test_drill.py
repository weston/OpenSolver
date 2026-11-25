import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import drill
from drill_grading import grade_frequencies
import server as vsrv
from library import Library


SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(vsrv.REPO, "solver")
TREE = {"range_oop": "AA,KK,QQ", "range_ip": "AA,KK,QQ", "board": "2c3d4h5s9c", "pot": 40,
        "stack": 60, "river_bet": "50", "river_raise": "50"}


class DrillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="wsolver-test-drill-")
        cls.solver = vsrv.Solver(SOLVER)
        cls.queue = drill.jobs.Queue(os.path.join(cls.temp.name, "jobs"))
        cls.service = drill.DrillService(cls.solver, SOLVER, cls.queue, random.Random(42))
        vsrv.set_drill(cls.service)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = Library(os.path.join(cls.temp.name, "library"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.http.shutdown()
        cls.http.server_close()
        vsrv.set_drill(None)
        drill.jobs.close_solver(cls.solver)
        cls.temp.cleanup()

    def setUp(self):
        self.service.close()
        self.solver.cmds(["set_threads 1", "set_range OOP AA,KK,QQ", "set_range IP AA,KK,QQ",
                         "set_board 2c3d4h5s9c", "set_pot 0 0 40", "set_eff_stack 60", "clear_lines",
                         "add_line 30 60 60", "add_line 0 30 60 60", "build_tree", "go 50 steps", "wait_for_solver"])
        self.solver.tree_present = True

    def request(self, action="state", data=None, expected=200):
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.base + "/api/drill/" + action, data=body,
                                         headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, result = response.status, json.load(response)
        except urllib.error.HTTPError as e:
            with e:
                status, result = e.code, json.load(e)
        self.assertEqual(status, expected, result)
        return result

    def post(self, endpoint, state, **data):
        return self.request(endpoint, {"token": state["token"], **data})

    def ready(self, count=1):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = self.request()
            self.assertIsNone(state["error"], state)
            if state["ready_count"] == count and not state["preparing"]:
                return state
            time.sleep(0.02)
        self.fail("simulations did not finish")

    def begin(self, **settings):
        self.request("start", {"source": "current", "seat": "OOP", **settings})
        return self.post("next", self.ready())

    def resolved(self):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = self.request()
            self.assertFalse(state["resolution"]["error"], state["resolution"])
            if not state["resolution"]["busy"]:
                return state
            time.sleep(0.02)
        self.fail("re-solve did not finish")

    def test_browse_opens_current_decision_and_keeps_drill_independent(self):
        self.addCleanup(self.service.rng.setstate, self.service.rng.getstate())
        node = "r:0:b30"
        state = self.begin(mode="node", node=node, seat="IP")
        strategy = self.service.hand.solver.cmd("show_strategy " + node)
        self.solver.cmds(["clear_lines", "add_line 10 10", "build_tree"])
        opened = self.post("browse", state)
        self.assertTrue(self.solver.loaded_file)
        snapshot = self.solver.loaded_path
        self.assertEqual(opened["browse_node"], node)
        self.assertEqual(self.solver.cmd("show_strategy " + node), strategy)
        self.assertEqual(opened["hand"], state["hand"])
        self.assertEqual(opened["token"], state["token"])
        self.solver.cmd("set_equal_strats")
        self.assertEqual(self.service.hand.solver.cmd("show_strategy " + node), strategy)
        self.finish(state)
        self.service.close()
        self.assertEqual(vsrv.parse_node_block(self.solver.cmd("show_node " + node))["type"], "IP_DEC")
        self.assertTrue(os.path.isfile(snapshot))
        self.solver.restart()
        self.assertFalse(os.path.exists(snapshot))

    def test_browse_resolved_ip_spot_maps_node_and_preserves_feedback(self):
        self.addCleanup(self.service.rng.setstate, self.service.rng.getstate())
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 20 20 20 30 30",
                         "add_line 20 20 20 40 40", "build_tree", "go 40 steps", "wait_for_solver"])
        node = "r:0:b20:c:7c:c"
        state = self.begin(format="set", seat="IP", mode="node", node=node)
        self.post("sizes", state, disabled=[node + ":b40"])
        state = self.resolved()
        choices = [float(a["kind"] == "check") for a in state["hand"]["actions"]]
        state = self.post("submit", state, choices=[choices] * len(state["hand"]["exercises"]))
        strategy = self.service.hand.solver.cmd("show_strategy " + node)
        opened = self.post("browse", state)
        self.assertEqual(opened["browse_node"], "r:0:c")
        self.assertEqual(self.solver.cmd("show_strategy r:0:c"), strategy)
        shown = vsrv.parse_node_block(self.solver.cmd("show_node r:0:c"))
        self.assertEqual(shown["board"], state["hand"]["node"]["board"])
        self.assertEqual(shown["pot"], state["hand"]["node"]["pot"])
        self.assertEqual([c["id"] for c in vsrv.parse_children(self.solver.cmd("show_children r:0:c"))],
                         ["r:0:c:b30", "r:0:c:c"])
        self.assertEqual(opened["hand"], state["hand"])
        self.assertEqual(opened["token"], state["token"])
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["phase"], "complete")
        opened = self.post("browse", state)
        self.assertEqual(opened["browse_node"], "r:0:c")
        self.service.close()
        self.assertEqual(self.solver.cmd("show_strategy r:0:c"), strategy)

    def test_size_toggles_compare_original_and_continue_resolved_strategy(self):
        self.solver.cmds(["clear_lines", "add_line 10 30 60 60", "add_line 20 40 60 60",
                         "add_line 0 10 30 60 60", "add_line 0 20 40 60 60", "build_tree", "go 100 steps", "wait_for_solver"])
        original = self.solver.cmd("show_strategy r:0")
        state = self.begin(format="set")
        dealt = state["hand"]["exercises"]
        disabled = ["r:0:b20"]
        self.post("sizes", state, disabled=disabled)
        state = self.resolved()
        self.assertEqual(state["hand"]["disabled"], disabled)
        self.assertEqual(state["hand"]["exercises"], dealt)
        self.assertNotIn("r:0:b20", self.service.hand.solver.cmd("show_children r:0"))
        choices = [float(a["id"] == "r:0:b10") for a in state["hand"]["actions"]]
        illegal = [float(a["id"] == "r:0:b20") for a in state["hand"]["actions"]]
        self.request("submit", {"token": state["token"], "choices": [illegal] * len(dealt)}, expected=400)
        state = self.post("submit", state, choices=[choices] * len(dealt))
        feedback = state["hand"]["feedback"]
        removed = next(i for i, a in enumerate(feedback["hands"][0]["actions"]) if a["id"] in disabled)
        self.assertEqual(feedback["frequencies"]["range"][removed], 0)
        self.assertGreater(feedback["original"]["frequencies"]["range"][removed], 0)
        self.assertEqual(feedback["removed_frequency"], feedback["original"]["frequencies"]["range"][removed])
        for result, solver in ((feedback, self.service.hand.solver), (feedback["original"], self.solver)):
            evs = vsrv.ev_pair(solver, "OOP", "r:0")
            mass = sum(w for w in evs["matchups"] if w)
            expected_ev = sum(ev * w for ev, w in zip(evs["ev"], evs["matchups"]) if ev is not None and w) / mass
            self.assertAlmostEqual(result["range_ev"], expected_ev)
        for actual, baseline in zip(feedback["hands"], feedback["original"]["hands"]):
            self.assertEqual(actual["chosen"], baseline["chosen"])
            combo = vsrv.hand_order(self.solver).index(actual["combo"])
            self.assertEqual([a["ev"] for a in baseline["actions"]],
                             [vsrv.ev_pair(self.solver, "OOP", a["id"])["ev"][combo] for a in baseline["actions"]])
        resolved_solver = self.service.resolver.rsolver
        self.post("continue", state)
        self.assertIs(self.service.playing_solver, resolved_solver)
        self.assertEqual(self.solver.cmd("show_strategy r:0"), original)

    def test_size_toggles_preserve_ip_line_and_can_restore_all_sizes(self):
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 20 20 20 30 30",
                         "add_line 20 20 20 40 40", "build_tree", "go 40 steps", "wait_for_solver"])
        node = "r:0:b20:c:7c:c"
        state = self.begin(format="set", seat="IP", mode="node", node=node)
        history = state["hand"]["history"]
        original = self.service.hand.solver.cmd("show_strategy " + node)
        self.post("sizes", state, disabled=[node + ":b40"])
        state = self.resolved()
        self.assertEqual(state["hand"]["history"], history)
        self.assertEqual(state["hand"]["stacks"], [40, 40])
        self.assertIn(node + ":b30", self.service.hand.solver.cmd("show_children " + node))
        state = self.post("sizes", state, disabled=[])
        self.assertIsNone(state["resolution"])
        self.assertEqual(self.service.hand.solver.cmd("show_strategy " + node), original)
        self.post("sizes", state, disabled=[node + ":b30", node + ":b40"])
        state = self.resolved()
        self.assertEqual([c["id"] for c in vsrv.parse_children(self.service.hand.solver.cmd("show_children " + node))], [node + ":c"])

    def test_later_street_keeps_original_comparison_through_another_resolve(self):
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 10 10 10 20 20",
                         "add_line 20 20 20 40 40", "add_line 0 0 10 10", "add_line 0 0 20 20",
                         "build_tree", "go 40 steps", "wait_for_solver"])
        state = self.begin(format="set")
        self.post("sizes", state, disabled=["r:0:b20"])
        state = self.resolved()
        check = [float(a["kind"] == "check") for a in state["hand"]["actions"]]
        state = self.post("submit", state, choices=[check] * len(state["hand"]["exercises"]))
        state = self.post("continue", state)
        node = state["hand"]["node"]["id"]
        self.assertTrue(node.startswith("r:0:c:c:"))
        self.assertIsNotNone(self.service.hand.original)
        self.post("sizes", state, disabled=[node + ":b20"])
        state = self.resolved()
        original = self.service.hand.original
        self.assertIs(original.solver, self.service.reader)
        self.assertEqual(original.solver.cmd("show_strategy " + node), self.solver.cmd("show_strategy " + node))
        check = [float(a["kind"] == "check") for a in state["hand"]["actions"]]
        state = self.post("submit", state, choices=[check] * len(state["hand"]["exercises"]))
        self.assertIn("original", state["hand"]["feedback"])
        self.finish(state)

    def test_changing_sizes_cancels_an_ongoing_resolve(self):
        self.solver.cmds(["add_line 10 60 60", "build_tree"])
        state = self.begin()
        original = self.service.hand.solver.cmd("show_strategy r:0")
        solve = drill.DrillResolve._build_and_solve

        def slow_solve(manager, ctx, body, acc, **kwargs):
            return solve(manager, ctx, body, 0, **{**kwargs, "seconds": 60})

        with patch.object(drill.DrillResolve, "_build_and_solve", slow_solve):
            state = self.post("sizes", state, disabled=["r:0:b10"])
            worker = self.service.resolver.worker
            self.assertTrue(state["resolution"]["busy"])
            state = self.post("sizes", state, disabled=[])
        self.assertFalse(worker.is_alive())
        self.assertIsNone(state["resolution"])
        self.assertEqual(self.service.hand.solver.cmd("show_strategy r:0"), original)

    def test_disabling_raises_keeps_call_fold_and_investments(self):
        self.solver.cmds(["set_eff_stack 100", "clear_lines", "add_line 30 60 100 100",
                         "add_line 30 80 100 100", "build_tree", "go 40 steps", "wait_for_solver"])
        state = self.begin(mode="node", node="r:0:b30", seat="IP", format="set")
        disabled = [a["id"] for a in state["hand"]["actions"] if a["kind"] == "raise"]
        self.assertEqual(len(disabled), 2)
        self.post("sizes", state, disabled=disabled)
        state = self.resolved()
        self.assertEqual(state["hand"]["wagers"], [30, 0])
        self.assertEqual(state["hand"]["stacks"], [70, 100])
        call = [float(a["kind"] == "call") for a in state["hand"]["actions"]]
        state = self.post("submit", state, choices=[call] * len(state["hand"]["exercises"]))
        self.assertIn("original", state["hand"]["feedback"])
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["stacks"], [70, 70])

    def test_resume_restores_setup_statistics_and_alternating_position(self):
        state = self.begin(mode="node", format="set", seat="alternate")
        session = state["token"].split(":")[0]
        choices = [float(a["kind"] == "check") for a in state["hand"]["actions"]]
        state = self.post("submit", state, choices=[choices] * len(state["hand"]["exercises"]))
        state = self.post("continue", state)
        stats = state["stats"]
        self.post("stop", state)
        self.solver.cmds(["set_board Kh9h4h2c3d", "build_tree"])
        state = self.request("resume", {"session": session})
        self.assertEqual(state["stats"], stats)
        self.assertEqual(state["token"].split(":")[0], session)
        state = self.post("next", self.ready())
        self.assertEqual(state["number"], 2)
        self.assertEqual(state["hand"]["seat"], "IP")
        self.assertIn("2c", state["hand"]["node"]["board"])
        self.assertIsNone(self.service.sessions.get(session)["ended"])
        previous = state
        self.request("resume", {"session": session})
        state = self.post("next", self.ready())
        self.assertNotEqual(state["token"], previous["token"])
        self.request("submit", {"token": previous["token"], "choices": []}, expected=400)

    def finish(self, state):
        for _ in range(30):
            hand = state["hand"]
            if hand["phase"] == "complete":
                return state
            if hand["phase"] == "decision":
                self.assertIsNone(hand["villain"])
                action = next((a for a in hand["actions"] if a["kind"] in ("check", "call")), hand["actions"][0])
                state = self.post("action", state, action=action["id"])
            else:
                state = self.post("continue", state)
        self.fail("hand did not finish")

    def test_grading_hidden_cards_alternation_and_study_isolation(self):
        self.solver.cmds(["add_line 60 60", "build_tree", "go 50 steps", "wait_for_solver"])
        before = self.solver.cmd("show_strategy r:0")
        state = self.begin(seat="alternate")
        self.assertEqual(state["hand"]["seat"], "OOP")
        shove = next(a for a in state["hand"]["actions"] if a.get("allin") and a["kind"] == "bet")
        self.assertEqual(shove["label"], "All-in 150%")
        self.assertNotIn("frequency", state["hand"]["actions"][0])
        self.assertNotIn("ev", state["hand"]["actions"][0])
        self.assertNotIn("hands", state["hand"])
        hero = vsrv.hand_order(self.solver).index(state["hand"]["hero"])
        expected = [vsrv.ev_pair(self.solver, "OOP", a["id"])["ev"][hero] for a in state["hand"]["actions"]]
        chosen = state["hand"]["actions"][0]["id"]
        old_token = state["token"]
        state = self.post("action", state, action=chosen)
        feedback = state["hand"]["feedback"]["hands"][0]
        self.assertEqual([a["ev"] for a in feedback["actions"]], expected)
        self.assertAlmostEqual(feedback["loss"], max(expected) - expected[0])
        self.assertAlmostEqual(state["stats"]["loss_pct"], feedback["loss_pct"])
        self.request("action", {"token": old_token, "action": chosen}, expected=400)
        self.assertEqual(self.request()["stats"]["decisions"], 1)
        self.solver.cmds(["set_board Kh9h4h2c3d", "build_tree"])
        state = self.finish(state)
        all_cards = drill.cards(state["hand"]["node"]["board"]) + drill.cards(state["hand"]["hero"]) + drill.cards(state["hand"]["villain"])
        self.assertEqual(len(set(all_cards)), len(all_cards))
        state = self.post("next", self.ready())
        self.assertEqual(state["hand"]["seat"], "IP")
        self.assertEqual(self.service.reader.cmd("show_strategy r:0"), before)
        self.finish(state)
        self.assertIn("Kh", self.solver.cmd("show_node r:0"))

    def test_selected_node_ends_after_one_decision(self):
        state = self.begin(mode="node", node="r:0:b30", seat="IP")
        self.assertEqual(state["hand"]["node"]["id"], "r:0:b30")
        self.assertEqual(state["hand"]["wagers"], [30, 0])
        self.assertEqual(state["hand"]["stacks"], [30, 60])
        self.assertEqual(state["hand"]["history"][0]["id"], "r:0:b30")
        shove = next(a for a in state["hand"]["actions"] if a.get("allin"))
        self.assertEqual((shove["kind"], shove["label"]), ("raise", "All-in 30%"))
        state = self.post("action", state, action=state["hand"]["actions"][-1]["id"])
        state = self.post("continue", state)
        self.assertEqual((state["hand"]["phase"], state["stats"]["decisions"]), ("complete", 1))
        self.assertIsNone(state["hand"]["villain"])

    def test_alternating_position_stays_fixed_across_streets_and_resolves(self):
        self.addCleanup(self.service.rng.setstate, self.service.rng.getstate())
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 10 10 20 20",
                         "add_line 20 20 40 40", "add_line 0 0 10 10", "add_line 0 0 20 20",
                         "build_tree", "go 40 steps", "wait_for_solver"])
        self.solver.cmd("set_strategy r:0 " + " ".join(["0"] * (2 * vsrv.NUM_COMBOS) + ["1"] * vsrv.NUM_COMBOS))
        state = self.begin(seat="alternate", format="set")
        self.post("sizes", state, disabled=["r:0:b20"])
        state = self.resolved()
        for number, seat in ((1, "OOP"), (2, "IP")):
            decisions = 0
            for _ in range(20):
                if state["hand"]["phase"] == "complete":
                    break
                self.assertEqual((state["number"], state["hand"]["seat"]), (number, seat))
                if state["hand"]["phase"] == "decision":
                    self.assertEqual(state["hand"]["node"]["type"], seat + "_DEC")
                    choices = [float(a["kind"] in ("check", "call")) for a in state["hand"]["actions"]]
                    state = self.post("submit", state, choices=[choices] * len(state["hand"]["exercises"]))
                    decisions += 1
                else:
                    state = self.post("continue", state)
            self.assertEqual(state["hand"]["seat"], seat)
            self.assertEqual(state["hand"]["phase"], "complete")
            self.assertGreaterEqual(decisions, 2)
            if number == 1:
                state = self.post("next", self.ready())

    def test_selected_river_line_only_shows_current_street_wagers(self):
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 20 20 20 40 40", "build_tree"])
        state = self.begin(mode="node", node="r:0:b20:c:7c:c:b40", seat="OOP")
        self.assertEqual(state["hand"]["wagers"], [0, 20])
        self.assertEqual(state["hand"]["stacks"], [40, 20])
        self.assertEqual(len(state["hand"]["history"]), 5)
        self.assertEqual(state["hand"]["history"][0]["amount"], 20)
        self.assertEqual(sum(state["hand"]["node"]["pot"]), 100)
        call = next(a for a in state["hand"]["actions"] if a["kind"] == "call")
        state = self.post("action", state, action=call["id"])
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["wagers"], [0, 0])
        self.assertEqual(sum(state["hand"]["node"]["pot"]), 120)
        self.assertEqual(state["hand"]["stacks"], [20, 20])

    def test_mixed_choices_preserve_frequencies_and_play_the_sampled_hand(self):
        state = self.begin(format="set", mode="node")
        actions = state["hand"]["actions"]
        choices = [0.25, 0.75]
        for bad in ([0, 0], [-0.1, 1.1], [0.5], [True, 0], [0.3, 0.3]):
            self.request("action", {"token": state["token"], "frequencies": bad}, expected=400)
        for i, exercise in enumerate(state["hand"]["exercises"]):
            state = self.post("action", state, frequencies=choices, exercise=i)
        feedback = state["hand"]["feedback"]["hands"]
        for entry in feedback:
            self.assertEqual(entry["chosen"], choices)
        continuation = state["hand"]["continuation"]
        self.assertIn(continuation["combo"], [f["combo"] for f in feedback])
        self.assertIn(continuation["id"], [a["id"] for a in actions])
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["hero"], continuation["combo"])
        self.assertEqual(state["hand"]["node"]["id"], continuation["id"])
        oop, ip = [set(drill.cards(self.service.hand.order[i])) for i in self.service.hand.hands]
        self.assertFalse(oop & ip)
        self.assertFalse((oop | ip) & set(drill.cards(state["hand"]["node"]["board"])))

    def test_submit_is_atomic_and_weighted_results_persist_in_session(self):
        self.solver.cmds(['set_range OOP AA:0.15,KK,QQ:0.5', 'build_tree', 'go 30 steps', 'wait_for_solver'])
        state = self.begin(format='set', mode='node')
        hand = state['hand']
        choices = [[i / 4, 1 - i / 4] for i in range(5)]
        self.request('submit', {'token': state['token'], 'choices': choices[:-1] + [[0, 0]]}, expected=400)
        unchanged = self.request()
        self.assertEqual(unchanged['token'], state['token'])
        self.assertTrue(all(e['chosen'] is None for e in unchanged['hand']['exercises']))
        self.assertEqual(unchanged['stats']['decisions'], 0)
        order = vsrv.hand_order(self.solver)
        own, other = [vsrv.parse_floats(self.solver.cmd(f'show_range {p} r:0'), 1326) for p in drill.PLAYERS]
        weights = [own[i] * sum(w for c, w in zip(order, other) if not set(drill.cards(c)) & set(drill.cards(combo)))
                   for i, combo in enumerate(order)]
        strategy = [vsrv.parse_floats(row, 1326) for row in self.solver.cmd('show_strategy r:0').splitlines() if row.strip()]
        state = self.post('submit', state, choices=choices)
        results = state['hand']['feedback']
        indices = [order.index(e['combo']) for e in hand['exercises']]
        mass = sum(weights[i] for i in indices)
        for a in range(2):
            self.assertAlmostEqual(results['frequencies']['range'][a], sum(weights[i] * strategy[a][i] / sum(r[i] for r in strategy) for i in range(1326) if weights[i]) / sum(weights))
            self.assertAlmostEqual(results['frequencies']['dealt_solver'][a], sum(weights[i] * strategy[a][i] / sum(r[i] for r in strategy) for i in indices) / mass)
            self.assertAlmostEqual(results['frequencies']['dealt_user'][a], sum(weights[i] * c[a] for i, c in zip(indices, choices)) / mass)
        session = state['token'].split(':')[0]
        state = self.post('continue', state)
        self.post('stop', state)
        from drill_sessions import Sessions
        persisted = Sessions(os.path.join(self.queue.root, 'drill-sessions.sqlite3'))
        try:
            row = next(s for s in persisted.list(0) if s['id'] == session)
            self.assertEqual((row['hands'], row['decisions']), (1, 5))
            self.assertIsNotNone(row['ended'])
            self.assertEqual(persisted.reviews(session, 0)[0]['feedback'], results)
            self.assertEqual(row['loss'], sum(h['loss_pct'] for h in results['hands']))
        finally:
            persisted.db.close()

    def test_board_filters_and_selected_boards_drive_the_live_queue(self):
        plan = drill.board_plan({'boards': 'filtered', 'board_filters': {'high': 'AK', 'middle': 'QJ', 'low': '23',
                                 'pairing': 'unpaired', 'suits': ['two-tone']}}, TREE)
        self.assertTrue(plan['board_pool'])
        for board in plan['board_pool']:
            self.assertIn(board[0], 'AK')
            self.assertIn(board[2], 'QJ')
            self.assertIn(board[4], '23')
            self.assertEqual(drill.jobs.texture_suits(board), 'two-tone')
        paired = drill.board_plan({'boards': 'filtered', 'board_filters': {'high': 'A', 'middle': 'A', 'low': '2',
                                  'pairing': 'paired', 'suits': ['rainbow']}}, TREE)
        self.assertEqual(len(paired['board_pool']), 12)
        self.request('start', {'tree': TREE, 'boards': 'filtered', 'board_filters': {'high': '2', 'middle': 'A'}}, expected=400)
        boards = ['2c3d4h5s9c', '2c3d4h5sTc']
        self.request('start', {'tree': TREE, 'boards': 'selected', 'selected_boards': boards, 'ahead': 3, 'seconds': 1})
        state = self.ready(3)
        self.assertEqual(state['config']['seat'], 'alternate')
        self.assertEqual(state['config']['selected_boards'], boards)
        queued = self.service.q.jobs()
        self.assertTrue(all(j['board'] in boards for j in queued))
        state = self.post('next', state)
        self.assertEqual(state['hand']['seat'], 'OOP')
        state = self.finish(state)
        state = self.post('next', state)
        self.assertEqual(state['hand']['seat'], 'IP')

    def test_node_picker_matches_generated_actions_and_resolves_next_street(self):
        tree = {**TREE, 'board': '2c3d4h5s', 'turn_bet': '50', 'turn_raise': '50', 'allin_threshold': 100, 'add_allin_limit': 0}
        config = {'tree': tree, 'boards': 'selected', 'selected_boards': [tree['board']], 'mode': 'node', 'ahead': 1, 'seconds': 1}
        root = self.request('preview', config)
        self.assertEqual(root['actor'], 'OOP')
        self.assertEqual(root['street'], 'Turn')
        checked = self.request('preview', {**config, 'node': 'r:0:c'})
        river = next(c for c in checked['choices'] if c['kind'] == 'check')
        self.assertEqual(river['id'], 'r:0:c:c:?')
        preview = self.request('preview', {**config, 'node': river['id']})
        self.assertEqual(preview['street'], 'River')
        self.request('start', {**config, 'node': river['id']})
        state = self.post('next', self.ready())
        self.assertEqual(len(drill.cards(state['hand']['node']['board'])), 5)
        expected = [(a['kind'], a['amount']) for a in preview['choices']]
        actual = [(a['kind'], a['amount']) for a in state['hand']['actions']]
        self.assertCountEqual(expected, actual)
        self.assertEqual([h['street'] for h in state['hand']['history'] if 'actor' in h], [4, 4])
        self.assertEqual(state['hand']['big_blind'], 100)

    def test_indifferent_actions_have_zero_loss_for_different_frequencies(self):
        self.solver.cmds(["set_range OOP AdAc", "set_range IP KdKc", "set_board 2h3s4d5h9c",
                         "clear_lines", "add_line 30 30", "add_line 0 30 30", "build_tree",
                         "set_accuracy 0.000001", "go 2000 steps", "wait_for_solver"])
        for choices in ([1, 0], [0, 1], [0.25, 0.75]):
            hand = drill.Hand(self.solver, "r:0", "OOP", "node", random.Random(1)).submit([choices])
            result = hand.feedback["hands"][0]
            self.assertTrue(all(0 < a["frequency"] < 1 for a in result["actions"]))
            self.assertEqual([a["ev"] for a in result["actions"]], [40, 40])
            self.assertEqual(result["loss"], 0)

    def test_mixed_actions_ignore_ev_noise_in_feedback_and_session_totals(self):
        self.addCleanup(self.service.rng.setstate, self.service.rng.getstate())
        self.solver.cmd("set_strategy r:0 " + " ".join(["0.4"] * 1326 + ["0.6"] * 1326))
        state = self.begin(format="set", mode="node")
        state = self.post("submit", state, choices=[[1, 0]] * len(state["hand"]["exercises"]))
        results = state["hand"]["feedback"]["hands"]
        self.assertTrue(any(abs(h["actions"][0]["ev"] - h["actions"][1]["ev"]) > 0.001 for h in results))
        self.assertTrue(all(h["loss"] == 0 and h["grade"] == "frequency" for h in results))
        self.assertEqual(state["stats"]["loss_pct"], 0)
        session = state["token"].split(":")[0]
        self.assertEqual(self.service.sessions.get(session)["loss"], 0)
        self.assertEqual(self.service.sessions.reviews(session, 0)[0]["feedback"]["hands"], results)

    def test_similar_sizes_keep_separated_choices_and_can_be_disabled(self):
        tree = {**TREE, "pot": 1000, "stack": 3000, "river_bet": "50 52 55 60 100", "river_raise": "",
                "allin_threshold": 100, "add_allin_limit": 0}
        for enabled, sizes in ((True, [1000, 600, 520]), (False, [1000, 600, 550, 520, 500])):
            with self.subTest(merge=enabled):
                self.service.close()
                config = {"tree": tree, "boards": "fixed", "ahead": 1, "seconds": 1}
                if not enabled:
                    config["merge_similar"] = False
                self.request("start", config)
                state = self.post("next", self.ready())
                self.assertEqual(state["config"]["merge_similar"], enabled)
                self.assertEqual([a["amount"] for a in state["hand"]["actions"] if a["kind"] == "bet"], sizes)

    def test_chance_deals_and_allin_runouts_exclude_both_hands(self):
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 60 60", "add_line 0 0 30 60 60", "build_tree"])
        self.solver.cmd("set_strategy r:0 " + " ".join(["0"] * 1326 + ["1"] * 1326))
        state = self.begin(seat="IP")
        self.assertEqual(state["hand"]["node"]["id"], "r:0:c")
        state = self.post("action", state, action="r:0:c:c")
        state = self.finish(state)
        self.assertTrue(any("board" in h for h in state["hand"]["history"]))
        all_cards = drill.cards(state["hand"]["node"]["board"]) + drill.cards(state["hand"]["hero"]) + drill.cards(state["hand"]["villain"])
        self.assertEqual(len(all_cards), 9)
        self.assertEqual(len(set(all_cards)), 9)
        self.post("stop", state)
        self.solver.cmd("set_strategy r:0 " + " ".join(["1"] * 1326 + ["0"] * 1326))
        state = self.begin(seat="IP")
        self.assertEqual(state["hand"]["node"]["id"], "r:0:b60")
        state = self.post("action", state, action="r:0:b60:c")
        state = self.finish(state)
        self.assertEqual(len(drill.cards(state["hand"]["node"]["board"])), 5)
        self.assertIn("wins", state["hand"]["result"])

    def test_live_buffer_defaults_to_three_and_refills_with_one_runner(self):
        state = self.request("start", {"source": "live", "boards": "fixed", "tree": TREE, "seconds": 1})
        self.assertEqual(state["config"]["ahead"], 3)
        state = self.ready(3)
        self.assertEqual(len(self.service.q.jobs()), 3)
        state = self.post("next", state)
        self.assertEqual(state["hand"]["node"]["board"].replace(" ", ""), TREE["board"])
        state = self.ready(3)
        self.assertEqual(len(self.service.q.jobs()), 4)
        saves = [j["out"] for j in self.service.q.jobs()]
        self.assertTrue(all(os.path.isfile(p) for p in saves))
        reader = self.service.reader.proc
        self.post("stop", state)
        self.assertFalse(any(os.path.exists(p) for p in saves))
        self.assertIsNotNone(reader.poll())
        self.request("start", {"source": "live", "boards": "fixed", "tree": TREE, "seconds": 1, "ahead": 1})
        self.ready(1)
        self.assertEqual(len(self.service.q.jobs()), 1)

    def test_on_demand_default_reports_build_solve_and_save_phases(self):
        observed = []
        entered = threading.Event()
        resume = threading.Event()
        phase = self.service._phase

        def report_phase(job, stage):
            phase(job, stage)
            observed.append(stage)
            entered.set()
            if not self.service.cancel.is_set() and not resume.wait(10):
                raise TimeoutError("test did not resume preparation")
            resume.clear()

        with patch.object(self.service, "_phase", report_phase):
            try:
                state = self.request("start", {"boards": "fixed", "tree": TREE, "seconds": 1, "ahead": 1})
                self.assertEqual(state["config"]["source"], "live")
                for stage in ("building", "solving", "saving"):
                    self.assertTrue(entered.wait(10))
                    entered.clear()
                    first = self.request()
                    second = self.request()
                    self.assertEqual(first["preparation"]["stage"], stage)
                    self.assertEqual(first["preparation"]["board"], TREE["board"])
                    self.assertGreaterEqual(second["preparation"]["elapsed"], first["preparation"]["elapsed"])
                    self.assertFalse(first["can_deal"])
                    self.assertTrue(first["preparing"])
                    curve = first["convergence"]
                    self.assertEqual(curve["board"], TREE["board"])
                    self.assertEqual(curve["pot"], TREE["pot"])
                    self.assertEqual(curve["target"], 0.5)
                    if stage == "building":
                        self.assertEqual(curve["points"], [])
                    if stage == "saving":
                        self.assertTrue(curve["points"])
                        self.assertEqual(curve["points"][-1]["explo"], first["progress"]["explo"])
                    resume.set()
                state = self.ready(1)
                self.assertIsNone(state["preparation"])
                self.assertEqual(observed, ["building", "solving", "saving"])
                curve = state["convergence"]
                self.assertEqual(curve["points"][-1]["explo"], self.service.q.job(curve["job"])["result"]["exploitability"])
                first_point = curve["points"][0]
                for i in range(1200):
                    self.service._progress({"id": curve["job"]}, {"t": 10 + i, "explo": 0.1, "pct": 0.25})
                retained = self.request()["convergence"]["points"]
                self.assertLessEqual(len(retained), 600)
                self.assertEqual(retained[0], first_point)
                self.assertEqual(retained[-1]["t"], 1209)
                self.post("next", state)
                self.assertTrue(entered.wait(10))
                entered.clear()
                fresh = self.request()
                self.assertIsNotNone(fresh["hand"])
                self.assertNotEqual(fresh["convergence"]["job"], curve["job"])
                self.assertEqual(fresh["convergence"]["points"], [])
                self.service.cancel.set()
            finally:
                resume.set()
                self.service.worker.join(10)

    def test_empty_live_queue_redeals_until_a_fresh_solve_is_ready(self):
        for seat in (*drill.PLAYERS, "alternate"):
            with self.subTest(seat=seat):
                self.service.close()
                self.request("start", {"source": "live", "boards": "fixed", "tree": TREE,
                                       "seconds": 1, "ahead": 1, "seat": seat})
                state = self.ready(1)
                resume = threading.Event()
                prepare_one = self.service.prepare_one

                def delayed_prepare():
                    if not resume.wait(10):
                        raise TimeoutError("test did not resume preparation")
                    return prepare_one()

                with patch.object(self.service, "prepare_one", delayed_prepare):
                    try:
                        state = self.post("next", state)
                        used_path = self.service.used_solve["path"]
                        for _ in range(3):
                            state = self.finish(state)
                            previous = state["hand"]
                            self.assertEqual(state["ready_count"], 0)
                            self.assertFalse(state["ready"])
                            self.assertTrue(state["can_deal"])
                            self.assertTrue(state["preparing"])
                            state = self.post("next", state)
                            self.assertEqual(self.service.used_solve["path"], used_path)
                            self.assertEqual(len(self.service.q.jobs()), 1)
                            self.assertIsNone(state["hand"]["feedback"])
                            if seat == "alternate":
                                self.assertNotEqual(state["hand"]["seat"], previous["seat"])
                            else:
                                self.assertEqual(state["hand"]["seat"], seat)
                                self.assertNotEqual(state["hand"]["hero"], previous["hero"])
                        state = self.finish(state)
                    finally:
                        resume.set()
                        self.service.worker.join(10)

                state = self.post("next", self.ready(1))
                self.assertNotEqual(self.service.used_solve["path"], used_path)
                self.assertFalse(os.path.exists(used_path))
                state = self.ready(1)
                self.assertEqual(len(self.service.q.jobs()), 2)
                state = self.post("stop", state)
                self.assertFalse(state["can_deal"])

    def test_redeal_with_only_one_compatible_hand_preserves_the_range(self):
        self.solver.cmds(["set_range OOP AcAd", "set_range IP KcKd", "build_tree",
                         "go 20 steps", "wait_for_solver"])
        for seat, combo in (("OOP", "AdAc"), ("IP", "KdKc")):
            with self.subTest(seat=seat):
                hand = drill.Hand(self.solver, "r:0", seat, "hand", random.Random(2))
                index = hand.hands[drill.PLAYERS.index(seat)]
                replay = drill.Hand(self.solver, "r:0", seat, "hand", random.Random(3), previous_combo=index)
                self.assertEqual(replay.order[replay.hands[drill.PLAYERS.index(seat)]], combo)

    def test_saved_batch_uses_finished_saves_and_leaves_them_intact(self):
        tree = {**TREE, "board": "2c3d4h5s", "turn_bet": "50"}
        setup, _ = vsrv.setup_from_body(tree)
        job = self.queue.add_batch("saved", setup, [(tree["board"], 1)], 1, 20, None, "full", None)[0]
        self.assertEqual(drill.jobs.run_job(self.queue, job, SOLVER, 1, log=lambda _: None), "done")
        with open(job["out"], "rb") as f:
            before = f.read()
        preview = self.request("preview", {"source": "saved", "batch": "saved", "node": "r:0:c:c:?"})
        self.assertTrue(all(c["id"].startswith("r:0:c:c:?:") for c in preview["choices"]))
        state = self.begin(source="saved", batch="saved", mode="node", node="r:0:c:c:?")
        self.assertEqual(len(drill.cards(state["hand"]["node"]["board"])), 5)
        opened = self.post("browse", state)
        node = state["hand"]["node"]["id"]
        self.assertEqual(opened["browse_node"], node)
        self.assertEqual(self.solver.cmd("show_strategy " + node), self.service.hand.solver.cmd("show_strategy " + node))
        state = self.finish(state)
        self.post("stop", state)
        with open(job["out"], "rb") as f:
            self.assertEqual(f.read(), before)

    def test_saved_drill_locks_are_separate_and_villain_best_response_preserves_hero(self):
        setup, _ = vsrv.setup_from_body(TREE)
        job = self.queue.add_batch("lock-drill", setup, [(TREE["board"], 1)], 1, 50, None, "full", None)[0]
        self.assertEqual(drill.jobs.run_job(self.queue, job, SOLVER, 1, log=lambda _: None), "done")
        with open(job["out"], "rb") as f:
            before = f.read()
        state = self.begin(source="saved", batch="lock-drill", format="set", mode="node", seat="alternate")
        session = state["token"].split(":")[0]
        expected = {}
        for _ in range(2):
            hand = state["hand"]
            choices = [[1 / len(hand["actions"])] * len(hand["actions"])] * len(hand["exercises"])
            state = self.post("submit", state, choices=choices)
            expected[hand["seat"]] = state["hand"]["feedback"]["hands"]
            state = self.post("continue", state)
            if hand["seat"] == "OOP":
                state = self.post("next", self.ready())
        self.post("stop", state)
        simulations = self.request("reviews", {"session": session})["simulations"]
        self.assertEqual((simulations[0]["oop"], simulations[0]["ip"]), (5, 5))
        for seat in ("OOP", "IP"):
            self.request("open-locks", {"session": session, "simulation": job["out"], "seat": seat})
            self.assertEqual(self.solver.state()["drill_variant"]["hero"], seat)
            self.assertFalse(self.solver.loaded_file)
            node = expected[seat][0]["node"]
            self.assertEqual(set(self.solver.locks), {node})
            rows = [vsrv.parse_floats(row, 1326) for row in self.solver.cmd("show_strategy " + node).splitlines() if row.strip()]
            order = vsrv.hand_order(self.solver)
            for hand in expected[seat]:
                for row, frequency in zip(rows, hand["chosen"]):
                    self.assertAlmostEqual(row[order.index(hand["combo"])], frequency, places=5)
            hero_strategy = self.solver.cmd("show_strategy " + node)
            villain = "IP" if seat == "OOP" else "OOP"
            from resolve import parse_evs
            ev_before = parse_evs(self.solver.cmd("calc_results"))[villain]
            status, _ = vsrv.api(self.solver, None, "POST", "/api/best_response", {}, {"player": villain})
            self.assertEqual(status, 200)
            self.assertEqual(self.solver.cmd("show_strategy " + node), hero_strategy)
            self.assertGreaterEqual(parse_evs(self.solver.cmd("calc_results"))[villain], ev_before - 1e-6)
            vsrv.api(self.solver, None, "POST", "/api/lock", {}, {"id": node, "mode": "current"})
            self.solver.cmds(["go 10 steps", "wait_for_solver"])
            self.assertEqual(self.solver.cmd("show_strategy " + node), hero_strategy)
        with open(job["out"], "rb") as f:
            self.assertEqual(f.read(), before)

    def test_live_session_resumes_after_service_restart(self):
        state = self.begin(source="live", tree=TREE, boards="selected", selected_boards=[TREE["board"]], mode="node", ahead=1)
        state = self.finish(state)
        session = state["token"].split(":")[0]
        self.post("stop", state)
        resumed = drill.DrillService(self.solver, SOLVER, self.queue, random.Random(42))
        vsrv.set_drill(resumed)
        try:
            state = self.request("resume", {"session": session})
            self.assertEqual(state["stats"]["hands"], 1)
            state = self.post("next", self.ready())
            self.assertEqual(state["number"], 2)
            self.assertEqual("".join(state["hand"]["node"]["board"].split()), TREE["board"])
            self.assertIn("r:0:b20", [a["id"] for a in state["hand"]["actions"]])
        finally:
            resumed.close()
            resumed.sessions.db.close()
            vsrv.set_drill(self.service)

    def test_hand_set_varies_classes_and_properties_and_grades_every_choice(self):
        broad = "AA,KK,QQ,JJ,TT,99,88,77,66,55,44,33,22,AK,AQ,AJ,KQ,QJ,JT,T9s,98s,87s,76s,A5s,A4s"
        self.solver.cmds(["set_range OOP " + broad, "set_range IP " + broad,
                         "set_board 2c3d9hKs", "clear_lines", "add_line 30 60 60", "add_line 0 30 60 60",
                         "build_tree", "go 10 steps", "wait_for_solver"])
        state = self.begin(format="set")
        exercises = state["hand"]["exercises"]
        self.assertEqual(len(exercises), 5)
        self.assertIsNone(state["hand"]["hero"])
        self.assertEqual(len({vsrv.combo_class(e["combo"]) for e in exercises}), 5)
        order = vsrv.hand_order(self.solver)
        categories = list(zip(*(row.split() for row in self.solver.cmd("show_categories 2c3d9hKs").splitlines()[:2])))
        self.assertGreater(len({categories[order.index(e["combo"])] for e in exercises}), 1)
        expected_losses = []
        for i, exercise in enumerate(exercises):
            action = next(a for a in state["hand"]["actions"] if a["kind"] == "check")
            values = [vsrv.ev_pair(self.solver, "OOP", a["id"])["ev"][order.index(exercise["combo"])] for a in state["hand"]["actions"]]
            chosen_index = next(k for k, a in enumerate(state["hand"]["actions"]) if a["id"] == action["id"])
            expected_losses.append(max(values) - values[chosen_index])
            state = self.post("action", state, action=action["id"], exercise=i)
            if i < 4:
                self.assertIsNone(state["hand"]["feedback"])
                self.assertEqual(state["hand"]["phase"], "decision")
                self.assertEqual(state["stats"]["decisions"], 0)
                self.request("continue", {"token": state["token"]}, expected=400)
                self.request("action", {"token": state["token"], "action": action["id"], "exercise": i}, expected=400)
        self.assertEqual(state["hand"]["phase"], "feedback")
        self.assertEqual(state["stats"]["decisions"], 5)
        self.assertEqual([f["loss"] for f in state["hand"]["feedback"]["hands"]], expected_losses)
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["history"][0]["kind"], "check")
        if state["hand"]["phase"] == "decision":
            self.assertEqual(len(state["hand"]["exercises"]), 5)
            self.assertTrue(all(e["chosen"] is None for e in state["hand"]["exercises"]))

    def test_stop_cancels_background_preparation_and_allows_a_new_session(self):
        state = self.request("start", {"source": "live", "boards": "fixed", "tree": TREE, "seconds": 1})
        worker = self.service.worker
        directory = self.service.temp.name
        self.post("stop", state)
        self.assertFalse(worker.is_alive())
        self.assertFalse(os.path.exists(directory))
        self.assertFalse(self.request()["active"])
        self.begin(mode="node", format="set", count=2)
        self.assertEqual(len(self.request()["hand"]["exercises"]), 2)

    def test_preflop_plays_from_the_blinds_and_deals_weighted_subset_flops(self):
        self.solver.cmds(["set_pot 10 5 0", "set_eff_stack 100", "clear_preflop_lines",
                         "add_preflop_line 25 25", "build_preflop_tree", "reset_subset",
                         "add_to_subset 0 2c 3d 4h", "add_to_subset 1 7c 8d 9h",
                         "set_board 2c3d4h", "set_pot 0 0 50", "set_eff_stack 75", "clear_lines",
                         "add_line 20 75 75", "add_line 0 20 75 75",
                         "add_schematic_tree r:0:b25:c", "add_all_flops"])
        state = self.begin(seat="IP")
        self.assertEqual(state["hand"]["node"]["board"], "")
        self.assertEqual(state["hand"]["node"]["type"], "IP_DEC")
        self.assertEqual(state["hand"]["stacks"], [90, 95])
        self.post("stop", state)
        state = self.begin(mode="node", node="r:0:b25:c", seat="IP")
        self.assertEqual(set(drill.cards(state["hand"]["node"]["board"])), {"7c", "8d", "9h"})
        self.assertEqual(state["hand"]["node"]["type"], "IP_DEC")
        state = self.finish(state)
        self.assertEqual(state["stats"]["decisions"], 1)

    def test_failed_preparation_keeps_ready_hands_and_waits_for_manual_retry(self):
        self.request("start", {"source": "live", "boards": "fixed", "tree": TREE, "seconds": 1})
        state = self.post("next", self.ready(3))
        self.ready(3)
        state = self.finish(state)
        self.service.solver_exe = os.path.join(self.temp.name, "missing-solver")
        try:
            state = self.post("next", state)
            self.service.worker.join(10)
            state = self.request()
            self.assertTrue(state["error"])
            self.assertEqual(state["ready_count"], 2)
            state = self.finish(state)
            state = self.post("next", state)
            self.assertTrue(state["error"])
            self.assertFalse(state["preparing"])
            self.assertEqual(state["ready_count"], 1)
        finally:
            self.service.solver_exe = SOLVER
        self.post("retry", state)
        state = self.ready(3)
        self.assertIsNone(state["error"])
        self.assertEqual(len(self.service.q.jobs()), 4)

    def test_invalid_settings_and_stale_session_do_not_change_play(self):
        for settings in ({"source": "wrong"}, {"mode": "node", "node": "r:0\nexit"}, {"seat": "both"},
                         {"source": "live", "ahead": 0}, {"source": "live", "ahead": 2.5},
                         {"source": "live", "ahead": True}, {"source": "live", "ahead": 21}):
            self.request("start", settings, expected=400)
        state = self.begin()
        self.request("stop", {"token": "old"}, expected=400)
        self.request("action", {"token": state["token"], "action": "r:0:fake"}, expected=400)
        self.assertEqual(self.request()["hand"], state["hand"])
        self.request("start", {}, expected=400)


class GradingTest(unittest.TestCase):
    def test_frequency_errors_are_free_and_wrong_actions_keep_their_cost(self):
        for choices, grade, loss in (([0.4, 0.6, 0], "correct", 0),
                                     ([0.45, 0.55, 0], "correct", 0),
                                     ([1, 0, 0], "frequency", 0),
                                     ([0, 1, 0], "frequency", 0),
                                     ([0.2, 0.3, 0.5], "mistake", 5),
                                     ([0, 0, 1], "mistake", 10)):
            with self.subTest(choices=choices):
                self.assertEqual(grade_frequencies(choices, [0.4, 0.6, 0], [10, 9, 0]),
                                 {"grade": grade, "loss": loss})
        self.assertEqual(grade_frequencies([1, 0], [1, 0], [10, None]), {"grade": "correct", "loss": 0})
        self.assertEqual(grade_frequencies([0.999, 0.001], [0.999, 0.001], [10, 0]), {"grade": "correct", "loss": 0})
        self.assertEqual(grade_frequencies([0, 1], [0.999, 0.001], [10, 0]), {"grade": "mistake", "loss": 9.99})

    def test_existing_session_results_and_totals_are_regraded_together(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sessions.sqlite3")
            sessions = drill.Sessions(path)
            sessions.start("session", {})
            hand = {"pot": 100, "chosen": [1, 0], "loss": 1, "loss_pct": 1,
                    "actions": [{"frequency": 0.4, "ev": 9}, {"frequency": 0.6, "ev": 10}]}
            sessions.record("session", 1, {"feedback": {"hands": [hand], "original": {"hands": [hand]}}}, (0, 1, 1))
            sessions.db.close()
            sessions = drill.Sessions(path)
            self.assertEqual(sessions.get("session")["loss"], 0)
            feedback = sessions.reviews("session", 0)[0]["feedback"]
            for result in (feedback["hands"][0], feedback["original"]["hands"][0]):
                self.assertEqual((result["loss"], result["loss_pct"], result["grade"]), (0, 0, "frequency"))
            sessions.db.close()
            sessions = drill.Sessions(path)
            self.assertEqual(sessions.reviews("session", 0)[0]["feedback"], feedback)
            self.assertEqual(sessions.get("session")["loss"], 0)
            sessions.db.close()


class DealTest(unittest.TestCase):
    def test_continuation_weights_reach_card_removal_and_each_submitted_mix(self):
        order = ["AcAd", "KcKd", "AcQd", "QcQh"]
        weights = drill.hand_weights(order, [1, 1, 0, 0], [0, 0, 3, 1], [])[:2]
        exercises = [{"chosen": [0.75, 0.25]}, {"chosen": [0, 1]}]
        rng = random.Random(7)
        hands, actions = [0, 0], [0, 0]
        for _ in range(10000):
            hand, action = drill.choose_continuation(exercises, weights, rng)
            hands[hand] += 1
            actions[action] += 1
        self.assertAlmostEqual(hands[0] / sum(hands), 0.2, delta=0.015)
        self.assertAlmostEqual(actions[0] / sum(actions), 0.15, delta=0.015)

    def test_joint_range_sampling_accounts_for_opponent_card_removal(self):
        order = ["AcAd", "KcKd", "AcQd", "QcQh"]
        ranges = [[1, 1, 0, 0], [0, 0, 3, 1]]
        rng = random.Random(2)
        counts = [0, 0]
        for _ in range(5000):
            oop, ip = drill.deal_hands(order, ranges, [], rng)
            counts[oop] += 1
            self.assertFalse(set(drill.cards(order[oop])) & set(drill.cards(order[ip])))
        self.assertAlmostEqual(counts[1] / sum(counts), 0.8, delta=0.025)
        for _ in range(20):
            self.assertEqual(drill.deal_hands(order, ranges, ["Ac"], rng), [1, 3])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
