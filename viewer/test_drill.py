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
        self.request("start", {"source": "current", **settings})
        return self.post("next", self.ready())

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
        before = self.solver.cmd("show_strategy r:0")
        state = self.begin(seat="alternate")
        self.assertEqual(state["hand"]["seat"], "OOP")
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
        self.assertEqual(state["hand"]["history"][0]["id"], "r:0:b30")
        state = self.post("action", state, action=state["hand"]["actions"][-1]["id"])
        state = self.post("continue", state)
        self.assertEqual((state["hand"]["phase"], state["stats"]["decisions"]), ("complete", 1))
        self.assertIsNone(state["hand"]["villain"])

    def test_selected_river_line_only_shows_current_street_wagers(self):
        self.solver.cmds(["set_board 2c3d4h5s", "clear_lines", "add_line 20 20 20 40 40", "build_tree"])
        state = self.begin(mode="node", node="r:0:b20:c:7c:c:b40", seat="OOP")
        self.assertEqual(state["hand"]["wagers"], [0, 20])
        self.assertEqual(len(state["hand"]["history"]), 5)
        self.assertEqual(state["hand"]["history"][0]["amount"], 20)
        self.assertEqual(sum(state["hand"]["node"]["pot"]), 100)
        call = next(a for a in state["hand"]["actions"] if a["kind"] == "call")
        state = self.post("action", state, action=call["id"])
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["wagers"], [0, 0])
        self.assertEqual(sum(state["hand"]["node"]["pot"]), 120)

    def test_mixed_choices_grade_expected_ev_and_play_the_sampled_hand(self):
        state = self.begin(format="set", mode="node")
        order = vsrv.hand_order(self.solver)
        actions = state["hand"]["actions"]
        choices = [0.25, 0.75]
        for bad in ([0, 0], [-0.1, 1.1], [0.5], [True, 0], [0.3, 0.3]):
            self.request("action", {"token": state["token"], "frequencies": bad}, expected=400)
        expected = []
        for i, exercise in enumerate(state["hand"]["exercises"]):
            values = [vsrv.ev_pair(self.solver, "OOP", a["id"])["ev"][order.index(exercise["combo"])] for a in actions]
            expected.append(max(values) - sum(v * f for v, f in zip(values, choices)))
            state = self.post("action", state, frequencies=choices, exercise=i)
        feedback = state["hand"]["feedback"]["hands"]
        for entry, loss in zip(feedback, expected):
            self.assertEqual(entry["chosen"], choices)
            self.assertAlmostEqual(entry["loss"], loss)
        continuation = state["hand"]["continuation"]
        self.assertIn(continuation["combo"], [f["combo"] for f in feedback])
        self.assertIn(continuation["id"], [a["id"] for a in actions])
        state = self.post("continue", state)
        self.assertEqual(state["hand"]["hero"], continuation["combo"])
        self.assertEqual(state["hand"]["node"]["id"], continuation["id"])
        oop, ip = [set(drill.cards(self.service.hand.order[i])) for i in self.service.hand.hands]
        self.assertFalse(oop & ip)
        self.assertFalse((oop | ip) & set(drill.cards(state["hand"]["node"]["board"])))

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
            if not resume.wait(10):
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
                    resume.set()
                state = self.ready(1)
                self.assertIsNone(state["preparation"])
                self.assertEqual(observed, ["building", "solving", "saving"])
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
        setup, _ = vsrv.setup_from_body(TREE)
        job = self.queue.add_batch("saved", setup, [(TREE["board"], 1)], 1, 20, None, "full", None)[0]
        self.assertEqual(drill.jobs.run_job(self.queue, job, SOLVER, 1, log=lambda _: None), "done")
        with open(job["out"], "rb") as f:
            before = f.read()
        state = self.begin(source="saved", batch="saved")
        state = self.finish(state)
        self.post("stop", state)
        with open(job["out"], "rb") as f:
            self.assertEqual(f.read(), before)

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
