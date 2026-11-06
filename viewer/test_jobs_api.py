#!/usr/bin/env python3
"""Integration tests for the viewer's Jobs view API (/api/jobs/*, #196) against the real solver.

Usage: python3 viewer/test_jobs_api.py [path-to-solver]
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
import jobs_api  # noqa: E402

SOLVER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(REPO, "solver")

TREE = {  # the Build form, without a board
    "range_oop": "AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,AK,AQ,KQs",
    "board": "", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "50", "turn_bet": "60", "turn_raise": "50",
    "river_bet": "65", "river_raise": "50", "river_allin": True,
    "allin_threshold": 50, "add_allin_limit": 300,
}


class JobsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wsolver-test-jobs-")
        cls.flops = os.path.join(cls.tmp, "flops")
        os.makedirs(cls.flops)
        with open(os.path.join(cls.flops, "three_flops.txt"), "w", newline="") as f:
            f.write("QsJs4h:12.2\r\nAs6s5h:10.53\r\nKs9s8h:11.82\r\n")  # a PioSOLVER-style flop file
        with open(os.path.join(cls.flops, "notes.txt"), "w") as f:
            f.write("not a flop list\n")
        cls.solver = vsrv.Solver(SOLVER)
        cls.service = jobs_api.JobsService(os.path.join(cls.tmp, "queue"), SOLVER, cls.flops)
        vsrv.set_jobs(cls.service)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = cls.solver
        cls.http.library = vlib.Library(os.path.join(cls.tmp, "library"))
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

    def req(self, path, method="GET", body=None):
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=120) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_idle(self, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, d = self.req("/api/jobs/state")
            if not d["running"]:
                return d
            time.sleep(0.2)
        self.fail("the queue did not finish")

    def test_01_flop_files(self):
        st, d = self.req("/api/jobs/flop_files")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["files"], [{"name": "three_flops.txt", "boards": 3}], "unreadable files are skipped")

    def test_02_add_validation(self):
        bad = [
            ({"name": "x", "tree": TREE, "flops_file": "../three_flops.txt"}, "pick a flop file"),
            ({"name": "x", "tree": TREE, "flops": "Kh9h4h2c"}, "3-card boards"),
            ({"name": "x", "tree": dict(TREE, flop_bet="zz"), "flops": "Kh9h4h"}, "bad sizes in flop_bet"),
            ({"name": "x", "flops": "Kh9h4h"}, "Build form"),
            ({"name": "x", "tree": TREE, "flops": "Kh9h4h", "accuracy": -1}, "accuracy"),
            ({"name": "../x", "tree": TREE, "flops": "Kh9h4h"}, "batch names"),
        ]
        for body, msg in bad:
            st, d = self.req("/api/jobs/add", "POST", body)
            self.assertEqual(st, 400, (body, d))
            self.assertIn(msg, d["error"])
        st, d = self.req("/api/jobs/state")
        self.assertEqual(d["jobs"], [], "nothing queued by a rejected add")
        st, d = self.req("/api/jobs/stop", "POST", {})
        self.assertEqual(st, 400, "stop when idle")

    def test_03_add_run_open(self):
        st, d = self.req("/api/jobs/add", "POST", {"name": "srp", "tree": TREE, "flops_file": "three_flops.txt",
                                                    "accuracy": 1.0, "max_iters": 40})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["added"], 3)
        self.assertTrue(any("MB" in l for l in d["estimate"]), d)
        st, d = self.req("/api/jobs/add", "POST", {"name": "pick", "tree": TREE, "flops": "Kh9h4h, 7c7d2s:2"})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/jobs/move", "POST", {"id": "00005", "to": 1})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/jobs/state")
        self.assertEqual([j["board"] for j in d["jobs"]][:2], ["7c7d2s", "QsJs4h"], "moved to the front")
        self.assertEqual([b["name"] for b in d["batches"]], ["pick", "srp"])

        # one job by id
        st, d = self.req("/api/jobs/run", "POST", {"ids": ["00002"]})
        self.assertEqual(st, 200, d)
        d = self.wait_idle()
        status = {j["id"]: j["status"] for j in d["jobs"]}
        self.assertEqual(status["00002"], "done", d["log"])
        self.assertEqual(sorted(s for i, s in status.items() if i != "00002"), ["queued"] * 4)
        st, d = self.req("/api/jobs/run", "POST", {"ids": ["00002"]})
        self.assertEqual(st, 400, "a finished job is not queued")

        # one batch
        st, d = self.req("/api/jobs/run", "POST", {"batch": "srp"})
        self.assertEqual(st, 200, d)
        st, d2 = self.req("/api/jobs/run", "POST", {})
        self.assertEqual(st, 400, "one runner at a time")
        d = self.wait_idle()
        srp = [j for j in d["jobs"] if j["batch"] == "srp"]
        self.assertTrue(all(j["status"] == "done" for j in srp), d["log"])
        self.assertTrue(all(j["result"]["iterations"] <= 40 and os.path.isfile(j["out"]) for j in srp))
        b = next(x for x in d["batches"] if x["name"] == "srp")
        self.assertEqual((b["total"], b["done"], b["save"]), (3, 3, "no_rivers"))

        # aggregate report
        st, rep = self.req("/api/jobs/report?batch=srp")
        self.assertEqual(st, 200, rep)
        self.assertEqual(len(rep["rows"]), 3)
        self.assertEqual(rep["player"], "OOP")
        self.assertTrue(rep["labels"] and len(rep["total"]["freqs"]) == len(rep["labels"]), rep)
        self.assertAlmostEqual(sum(rep["total"]["freqs"]), 1.0, places=4)
        st, d = self.req("/api/jobs/report?batch=nope")
        self.assertEqual(st, 400)
        # at a line (#325): the first action's child, then every turn under a check-check flop
        a0 = rep["actions"][0]
        st, lr = self.req("/api/jobs/report?batch=srp&line=" + a0)
        self.assertEqual(st, 200, lr)
        self.assertEqual((lr["line"], len(lr["rows"])), (a0, 3), lr)
        self.assertTrue(all(x["global"] is not None and x["freqs"] for x in lr["rows"]), lr["rows"])
        if "c" in rep["actions"]:
            st, tl = self.req("/api/jobs/report?batch=srp&line=" + urllib.parse.quote("r:0:c:c:*"))
            self.assertEqual(st, 200, tl)
            self.assertTrue(all(len(x["cards"]) == 49 for x in tl["rows"]), [len(x["cards"] or []) for x in tl["rows"]])
        st, d = self.req("/api/jobs/report?batch=srp&line=" + urllib.parse.quote("b50 x7"))
        self.assertEqual(st, 400, "a malformed line is refused")

        # open a finished save in the viewer's own solver
        st, d = self.req("/api/jobs/open", "POST", {"id": "00003"})
        self.assertEqual(st, 200, d)
        st, s = self.req("/api/status")
        self.assertTrue(s["tree"] and s["loaded_file"], s)
        st, n = self.req("/api/node?id=r")
        self.assertEqual(st, 200, n)
        self.assertIn("Ks", json.dumps(n))
        st, d = self.req("/api/jobs/open", "POST", {"id": "00004"})
        self.assertEqual(st, 400, "no save yet")

        # retry and remove
        st, d = self.req("/api/jobs/retry", "POST", {"ids": ["00001"]})
        self.assertEqual((st, d["requeued"]), (200, 1))
        st, d = self.req("/api/jobs/remove", "POST", {"batch": "pick"})
        self.assertEqual((st, d["removed"]), (200, 2))
        st, d = self.req("/api/jobs/state")
        self.assertEqual([b["name"] for b in d["batches"]], ["srp"])
        self.assertEqual([j["status"] for j in d["jobs"]].count("queued"), 1)

    def test_04_static(self):
        with urllib.request.urlopen(self.base + "/jobs.js", timeout=10) as f:
            self.assertIn("javascript", f.headers["Content-Type"])
        with urllib.request.urlopen(self.base + "/", timeout=10) as f:
            self.assertIn('id="view-jobs"', f.read().decode())

    def test_05_cli_runner(self):
        """A runner started from the CLI (another process holding the runner lock) shows as running;
        the tab can stop it but not start a second one."""
        q = self.service.q
        os.makedirs(q.root, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with open(q.lock_file, "w") as f:
                f.write(str(holder.pid))
            st, d = self.req("/api/jobs/state")
            self.assertTrue(d["running"] and d["external"] == holder.pid, d)
            st, d = self.req("/api/jobs/run", "POST", {})
            self.assertEqual(st, 400, d)
            self.assertIn("command line", d["error"])
            st, d = self.req("/api/jobs/stop", "POST", {})
            self.assertEqual(st, 200, d)
            with open(q.stop_file) as f:
                self.assertEqual(f.read(), "after")
        finally:
            holder.kill()
            holder.wait()
            for p in (q.lock_file, q.stop_file):
                if os.path.exists(p):
                    os.remove(p)
        st, d = self.req("/api/jobs/state")
        self.assertFalse(d["running"], "a dead runner's lock doesn't count")

    def test_06_dead_runner_job_is_requeued(self):
        """A job left 'running' by a runner that is gone (the viewer restarted mid-job) shows as
        queued, not running forever."""
        q = self.service.q
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        j = q.jobs()[0]
        q.update(j["id"], status="running", runner_pid=dead.pid, started=time.time())
        st, d = self.req("/api/jobs/state")
        self.assertEqual(st, 200, d)
        self.assertFalse(d["running"], d)
        mine = next(x for x in d["jobs"] if x["id"] == j["id"])
        self.assertEqual(mine["status"], "queued", mine)
        self.assertIsNone(mine.get("runner_pid"))
        self.assertIsNone(d["current"])

    def test_07_runner_thread_crash_requeues_its_job(self):
        """If the viewer's runner thread dies of an unexpected error mid-job, the job it marked
        running (under the viewer's own, live pid) goes back to the queue and the error shows."""
        q = self.service.q
        j = q.jobs()[0]
        real = jobs_api.jobs.run

        def crash(q_, ids, batch, *a, **k):
            q_.update(j["id"], status="running", runner_pid=os.getpid(), started=time.time())
            raise RuntimeError("boom")
        jobs_api.jobs.run = crash
        try:
            self.service._run(None, None)
        finally:
            jobs_api.jobs.run = real
        self.assertEqual(q.job(j["id"])["status"], "queued")
        st, d = self.req("/api/jobs/state")
        self.assertEqual(d["error"], "boom")
        self.assertEqual(next(x for x in d["jobs"] if x["id"] == j["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
