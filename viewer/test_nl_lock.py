#!/usr/bin/env python3
"""Plain-English locks (#200, part 2) with a fake model: no network, no key needed.

Usage: python3 viewer/test_nl_lock.py [path-to-solver]

Covers the threshold step (targets hit exactly, in the asked order, isomorphic combos kept
together), mix rules, refusals (unknown actions, unreachable targets, the model saying no, bad
JSON), the no-key behaviour, the Sail request (host, headers, window) with urlopen faked, that the
key never reaches the model prompt or an API response, and that a proposal locks.
"""
import io
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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import server as vsrv  # noqa: E402
import library as vlib  # noqa: E402
import nl_lock  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(REPO, "solver")
NUM = 1326
SECRET = "sk-test-DO-NOT-LEAK-1234567890"

CFG = {
    "range_oop": "AA,KK,QQ,JJ,TT,99,AK,AQ,AJs,KQs,KJs,QJs,JTs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,88,77,AK,AQ,AJ,KQ,KJs,QJs,JTs,T9s",
    "board": "Kh9h4c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "", "turn_bet": "60", "turn_raise": "",
    "river_bet": "65", "river_raise": "", "allin_threshold": 50, "add_allin_limit": 300,
}
FACING = "r:0:b24"  # IP facing OOP's flop bet: call / fold


class FakeClient:
    """Answers with whatever the test queues; records the prompts it was sent."""

    def __init__(self):
        self.replies, self.sent = [], []

    def complete(self, messages):
        self.sent.append(messages)
        reply = self.replies.pop(0)
        return (reply if isinstance(reply, str) else json.dumps(reply)), {"input_tokens": 1500, "output_tokens": 200}


class NLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solver = vsrv.Solver(SOLVER)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = vlib.Library(tempfile.mkdtemp(prefix="wsolver-test-nl-"))
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"
        cls.fake = FakeClient()
        vsrv.set_nl(nl_lock.NLService(client=cls.fake))
        cls.req_s(cls, "/api/generate", "POST", CFG)
        cls.req_s(cls, "/api/build", "POST", {})
        cls.req_s(cls, "/api/solve", "POST", {"accuracy": 0.05})
        time.sleep(0.2)
        while cls.req_s(cls, "/api/progress")[1]["solving"]:
            time.sleep(0.1)
        cls.order = cls.req_s(cls, "/api/hand_order")[1]["order"]

    @classmethod
    def tearDownClass(cls):
        vsrv.set_nl(None)
        cls.http.shutdown()
        cls.http.server_close()
        cls.solver.forget_locks()
        try:
            cls.solver.proc.kill()
        except Exception:
            pass

    def req_s(self, path, method="GET", body=None):
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=300) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def ok(self, path, method="GET", body=None):
        st, d = self.req_s(path, method, body)
        self.assertEqual(st, 200, (path, d))
        return d

    def propose(self, reply, node=FACING, text="whatever"):
        self.fake.replies.append(reply)
        return self.req_s("/api/nl/propose", "POST", {"id": node, "text": text})

    def ctx(self, node=FACING):
        return nl_lock.node_context(vsrv, self.solver, node)

    def overall(self, ctx, freq, k):
        R = sum(ctx["reach"])
        return sum(ctx["reach"][i] * freq[k][i] for i in range(NUM)) / R

    # ------------------------------------------------------------------ targets

    def test_target_weakest_exact(self):
        ctx = self.ctx()
        keys = [a["key"] for a in ctx["acts"]]
        f = keys.index("f")
        st, r = self.propose({"ok": True, "explanation": "IP folds 60%, weakest first",
                              "rules": [{"hands": "all", "target": {"action": "f", "overall": 0.6, "order": "weakest"}}]})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(self.overall(ctx, r["freq"], f), 0.6, places=9)
        self.assertAlmostEqual(r["overall_after"][f], 0.6, places=9)
        live = [i for i in range(NUM) if ctx["reach"][i] > 0]
        folds = [i for i in live if r["freq"][f][i] > 1 - 1e-9]
        keeps = [i for i in live if r["freq"][f][i] < 1e-9]
        self.assertTrue(folds and keeps)
        self.assertLessEqual(max(ctx["equity"][i] for i in folds), min(ctx["equity"][i] for i in keeps) + 1e-9,
                             "the weakest hands fold, the strongest don't")
        partial = {(ctx["classes"][i], round(ctx["equity"][i], 6)) for i in live if 1e-9 < r["freq"][f][i] < 1 - 1e-9}
        self.assertLessEqual(len(partial), 1, "one group at most is split")
        # combos of a class with the same equity (suit-isomorphic) share one frequency
        g = {}
        for i in live:
            g.setdefault((ctx["classes"][i], round(ctx["equity"][i], 6)), set()).add(round(r["freq"][f][i], 9))
        self.assertTrue(all(len(v) == 1 for v in g.values()))
        for i in live:
            self.assertAlmostEqual(sum(row[i] for row in r["freq"]), 1.0, places=9)

    def test_target_strongest_on_categories_and_proportional(self):
        ctx = self.ctx("r:0")
        keys = [a["key"] for a in ctx["acts"]]
        b = keys.index("b24")
        before = self.overall(ctx, ctx["freq"], b)
        # bet 30% overall, from top pair or better only, strongest first
        cats = ["top_pair", "top_pair_tp", "overpair", "two_pair", "set"]
        st, r = self.propose({"ok": True, "explanation": "value only",
                              "rules": [{"hands": "all", "target": {"action": "b24", "overall": 0.0, "order": "current"}},
                                        {"hands": {"categories": cats}, "target": {"action": "b24", "overall": 0.3, "order": "strongest"}}]},
                             node="r:0")
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(self.overall(ctx, r["freq"], b), 0.3, places=9)
        for i in range(NUM):
            if ctx["reach"][i] > 0 and r["freq"][b][i] > 1e-9:
                self.assertTrue(nl_lock._cats(ctx, i) & set(cats), ctx["order"][i])
        # no hand detail: every hand scales, the target is still exact, and nobody goes pure who wasn't
        st, r = self.propose({"ok": True, "explanation": "bet more", "rules": [
            {"hands": "all", "target": {"action": "b24", "overall": min(0.95, before + 0.2), "order": "proportional"}}]}, node="r:0")
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(self.overall(ctx, r["freq"], b), min(0.95, before + 0.2), places=9)
        mixed_before = sum(1 for i in range(NUM) if ctx["reach"][i] > 0 and 1e-6 < ctx["freq"][b][i] < 1 - 1e-6)
        mixed_after = sum(1 for i in range(NUM) if ctx["reach"][i] > 0 and 1e-6 < r["freq"][b][i] < 1 - 1e-6)
        self.assertGreaterEqual(mixed_after, mixed_before - 5)

    def test_never_and_mix(self):
        ctx = self.ctx("r:0")
        keys = [a["key"] for a in ctx["acts"]]
        st, r = self.propose({"ok": True, "explanation": "OOP never bets",
                              "rules": [{"hands": "all", "target": {"action": "b24", "overall": 0, "order": "current"}},
                                        {"hands": {"classes": ["AA", "KK"]}, "mix": {"b24": 3, "c": 1}}]}, node="r:0")
        self.assertTrue(r["ok"], r)
        b, c = keys.index("b24"), keys.index("c")
        for i in range(NUM):
            if ctx["reach"][i] <= 0:
                continue
            if ctx["classes"][i] in ("AA", "KK"):
                self.assertAlmostEqual(r["freq"][b][i], 0.75, places=9)
                self.assertAlmostEqual(r["freq"][c][i], 0.25, places=9)
            else:
                self.assertAlmostEqual(r["freq"][b][i], 0.0, places=9)
        self.assertIn("AA", r["classes"])

    # ------------------------------------------------------------------ refusals

    def test_refusals(self):
        st, r = self.propose({"ok": True, "explanation": "raise", "rules": [{"hands": "all", "mix": {"r60": 1}}]})
        self.assertEqual(st, 200)
        self.assertFalse(r["ok"])
        self.assertIn("no action r60", r["error"])
        st, r = self.propose({"ok": True, "explanation": "x", "rules": [
            {"hands": {"classes": ["AA"]}, "target": {"action": "f", "overall": 0.99, "order": "weakest"}}]})
        self.assertFalse(r["ok"])
        self.assertIn("out of reach", r["error"])
        st, r = self.propose({"ok": True, "explanation": "x", "rules": [{"hands": {"classes": ["AKx"]}, "mix": {"f": 1}}]})
        self.assertIn("unknown hand classes", r["error"])
        st, r = self.propose({"ok": True, "explanation": "x", "rules": [{"hands": {"categories": ["monsters"]}, "mix": {"f": 1}}]})
        self.assertIn("unknown categories", r["error"])
        st, r = self.propose({"ok": False, "explanation": "There is no raise at this node; IP can only call or fold."})
        self.assertEqual(st, 200)
        self.assertFalse(r["ok"])
        self.assertIn("no raise", r["explanation"])
        self.assertNotIn("freq", r)
        st, r = self.propose("Sorry, I can't help with that.")
        self.assertEqual(st, 400, r)
        st, r = self.propose('Here you go:\n```json\n{"ok": true, "explanation": "fold all", "rules": [{"hands": "all", "mix": {"f": 1}}]}\n```')
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"], "a fenced JSON reply is read")
        st, r = self.req_s("/api/nl/propose", "POST", {"id": "r:0:b24:c", "text": "x"})
        self.assertEqual(st, 400, "a chance node is refused before the model is asked")
        st, r = self.req_s("/api/nl/propose", "POST", {"id": FACING, "text": "  "})
        self.assertEqual(st, 400)

    # ------------------------------------------------------------------ prompt, cost, key

    def test_prompt_is_compact_and_costed(self):
        st, r = self.propose({"ok": False, "explanation": "no"}, text="IP folds a lot")
        msgs = self.fake.sent[-1]
        user = msgs[-1]["content"]
        self.assertIn("IP folds a lot", user)
        node = json.loads(user.split("Node:\n", 1)[1].split("\n\nRequest:", 1)[0])
        self.assertEqual(node["player_to_act"], "IP")
        self.assertEqual({a["key"]: a["means"] for a in node["actions"]}, {"c": "call", "f": "fold"})
        root = nl_lock.summary(self.ctx("r:0"))
        self.assertEqual({a["key"]: a["means"] for a in root["actions"]}, {"b24": "bet 24 (60% pot)", "c": "check"})
        self.assertLess(len(user), 40000, "a compact node summary, not the tree")
        self.assertEqual(r["cost"]["input_tokens"], 1500)
        self.assertAlmostEqual(r["cost"]["usd"], (1500 * 1.5 + 200 * 10) / 1e6, places=7)
        e = self.ok("/api/nl/estimate", "POST", {"id": FACING, "text": "IP folds a lot"})
        self.assertGreater(e["input_tokens"], 100)
        self.assertGreater(e["usd_max"], e["usd_input"])

    def test_no_key(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"SAIL_API_KEY": "", "HOME": home, "USERPROFILE": home}):
            svc = nl_lock.NLService()
            self.assertIsNone(svc.client)
            vsrv.set_nl(svc)
            try:
                st = self.ok("/api/nl/status")
                self.assertFalse(st["enabled"])
                self.assertIn("SAIL_API_KEY", st["reason"])
                s, r = self.req_s("/api/nl/propose", "POST", {"id": FACING, "text": "fold"})
                self.assertEqual(s, 400)
                self.ok("/api/strategy?id=" + FACING)   # the rest of the viewer is unaffected
            finally:
                vsrv.set_nl(nl_lock.NLService(client=self.fake))
            # the key file is read when the variable is unset
            os.makedirs(os.path.join(home, ".config", "sailresearch"))
            with open(os.path.join(home, ".config", "sailresearch", "api_key"), "w") as f:
                f.write(SECRET + "\n")
            self.assertEqual(nl_lock.load_key(), SECRET)

    def test_sail_request_and_key_stays_server_side(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["host"] = urllib.parse.urlsplit(req.full_url).hostname
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = json.loads(req.data)
            return Resp(json.dumps({"choices": [{"message": {"content": json.dumps(
                {"ok": True, "explanation": "fold all", "rules": [{"hands": "all", "mix": {"f": 1}}]})}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}).encode())

        with mock.patch.dict(os.environ, {"SAIL_API_KEY": SECRET}):
            svc = nl_lock.NLService()
        self.assertIsInstance(svc.client, nl_lock.SailClient)
        vsrv.set_nl(svc)
        try:
            svc.client.opener = fake_urlopen
            st, r = self.req_s("/api/nl/propose", "POST", {"id": FACING, "text": "IP always folds"})
            self.assertEqual(st, 200, r)
            self.assertTrue(r["ok"])
            self.assertEqual(seen["host"], "api.sailresearch.com")
            self.assertEqual(seen["headers"]["authorization"], "Bearer " + SECRET)
            self.assertEqual(seen["headers"]["x-sail-completion-window"], "asap")
            self.assertEqual(seen["body"]["model"], "moonshotai/Kimi-K3")
            self.assertEqual(seen["body"]["metadata"]["completion_window"], "asap")
            self.assertNotIn(SECRET, json.dumps(seen["body"]), "the key is a header, not in the prompt")
            self.assertNotIn(SECRET, json.dumps(r))
            self.assertNotIn(SECRET, json.dumps(self.ok("/api/nl/status")))
            self.assertEqual(r["cost"], {"input_tokens": 1000, "output_tokens": 100, "usd": round((1000 * 1.5 + 100 * 10) / 1e6, 5)})

            def http_error(req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(
                    json.dumps({"error": {"message": "model_capacity_unavailable"}}).encode()))
            svc.client.opener = http_error
            st, r = self.req_s("/api/nl/propose", "POST", {"id": FACING, "text": "IP always folds"})
            self.assertEqual(st, 400)
            self.assertIn("503", r["error"])
            self.assertIn("model_capacity_unavailable", r["error"])
            self.assertNotIn(SECRET, json.dumps(r))
        finally:
            vsrv.set_nl(nl_lock.NLService(client=self.fake))

    # ------------------------------------------------------------------ end to end

    def test_proposal_locks(self):
        ctx = self.ctx()
        f = [a["key"] for a in ctx["acts"]].index("f")
        st, r = self.propose({"ok": True, "explanation": "fold 70%", "rules": [
            {"hands": "all", "target": {"action": "f", "overall": 0.7, "order": "weakest"}}]})
        self.assertTrue(r["ok"], r)
        self.ok("/api/lock", "POST", {"id": FACING, "mode": "node", "freq": r["freq"]})
        try:
            d = self.ok("/api/strategy?id=" + FACING)
            got = self.overall(ctx, [[v or 0 for v in row] for row in d["freq"]], f)
            self.assertAlmostEqual(got, 0.7, places=4, msg="the locked strategy is the proposal")
        finally:
            self.ok("/api/unlock", "POST", {"all": True})


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
