#!/usr/bin/env python3
"""Tests for tools/jobs.py, the solve queue (#196).  usage: python3 tests/test_jobs.py [./solver]"""

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
import jobs  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def raises(fn, *a):
    try:
        fn(*a)
    except jobs.JobError:
        return True
    return False


TREE = """# a small flop tree
set_range OOP AA,KK,QQ,AK,AQs
set_range IP JJ,TT,AQ,KQ
set_board Kh9h4h
set_pot 0 0 100
set_eff_stack 100
add_line 50
add_line 0 50
add_line 50 100
build_tree
go
"""


def unit():
    print("[parsing]")
    fl = jobs.parse_flops("Ks5h2s:15.76\r\nTs7h2s:13.3\n\n# comment\nqs jh 4D\n")
    check(fl == [("Ks5h2s", 15.76), ("Ts7h2s", 13.3), ("QsJh4d", 1.0)], f"flop file lines {fl}")
    check(jobs.parse_flops("Kh9h4h, As7d2c:2") == [("Kh9h4h", 1.0), ("As7d2c", 2.0)], "comma list")
    check(raises(jobs.parse_flops, "Kh9h4h,Kh9h4h"), "a board listed twice")
    check(raises(jobs.parse_flops, "KhKh4h"), "a repeated card")
    check(raises(jobs.parse_flops, "Kh9h"), "too few cards")
    check(raises(jobs.parse_flops, "Kh9x4h"), "a bad card")
    check(raises(jobs.parse_flops, ""), "no boards")
    setup, dropped = jobs.tree_setup(TREE)
    check(not any(l.split()[0] in ("set_board", "build_tree", "go") for l in setup), "run commands dropped")
    check(sorted(d.split()[0] for d in dropped) == ["build_tree", "go", "set_board"], f"dropped {dropped}")
    check(sum(l.startswith("add_line") for l in setup) == 3, "add_lines kept")
    check(raises(jobs.tree_setup, "set_pot 0 0 100\n"), "a tree without lines")
    with open(os.path.join(REPO, "tests", "data", "pio_25bbHU_2sizes.txt")) as f:
        cfg_text = "\n".join(l for l in f.read().splitlines() if l.startswith("#"))  # headers only
    setup, _ = jobs.tree_setup(cfg_text)
    check(sum(l.startswith("add_line") for l in setup) > 100 and any(l.startswith("set_range OOP") for l in setup),
          f"PioViewer config expanded ({sum(l.startswith('add_line') for l in setup)} lines)")
    check(jobs.start_pot(["set_pot 10 20 30"]) == 60, "start pot")
    check(jobs.parse_results("EV OOP: 1.5\nEV IP: -2\nOOP's MES: 3\nIP's MES: 4\nExploitable for: 0.25") ==
          {"ev_oop": 1.5, "ev_ip": -2.0, "mes_oop": 3.0, "mes_ip": 4.0, "exploitability": 0.25}, "calc_results parse")

    print("[queue]")
    with tempfile.TemporaryDirectory() as td:
        q = jobs.Queue(td)
        setup, _ = jobs.tree_setup(TREE)
        a = q.add_batch("b1", setup, [("Kh9h4h", 1), ("As7d2c", 2)], 0.5, None, None, "no_rivers", None)
        b = q.add_batch("b2", setup, [("Qs8c3d", 1)], 1.0, 20, None, "full", None)
        check([j["id"] for j in a + b] == ["00001", "00002", "00003"], "ids in sequence")
        check(raises(q.add_batch, "b1", setup, [("Kh9h4h", 1)], 0.5, None, None, "no_rivers", None), "duplicate batch")
        check(raises(q.add_batch, "../x", setup, [("Kh9h4h", 1)], 0.5, None, None, "no_rivers", None), "bad name")
        check(a[0]["out"] == os.path.join(td, "saves", "b1", "Kh9h4h.cfr"), "default save path")
        q.move("00003", 1)
        check([j["id"] for j in q.jobs()] == ["00003", "00001", "00002"], "move to the front")
        q.move("00003", 3)
        check([j["id"] for j in q.jobs()] == ["00001", "00002", "00003"], "move to the back")
        check([j["id"] for j in q.jobs("b1")] == ["00001", "00002"], "filter by batch")
        q.remove(q.jobs("b2"))
        check(len(q.jobs()) == 2 and raises(q.batch, "b2"), "remove a batch (and its config)")
        j = q.job("00001")
        j.update(status="running", runner_pid=999999999)
        q.save(j)
        q.recover()
        check(q.job("00001")["status"] == "queued", "a dead runner's job goes back to the queue")
        q.lock()
        check(raises(q.lock), "one runner at a time")
        q.unlock()
        with open(q.lock_file, "w") as f:
            f.write("999999999")
        q.lock()
        check(open(q.lock_file).read() == str(os.getpid()), "a stale lock is taken over")
        q.unlock()
        check(raises(q.job, "../../etc"), "job ids are numbers")

    print("[queue edits racing the runner]")
    with tempfile.TemporaryDirectory() as td:
        q = jobs.Queue(td)
        setup, _ = jobs.tree_setup(TREE)
        check(raises(q.add_batch, "z", setup, [("Kh9h4h", 1)], 0.5, None, 0, "no_rivers", None)
              and raises(q.add_batch, "z", setup, [("Kh9h4h", 1)], 0.5, -5, None, "no_rivers", None),
              "a limit of 0 or less is refused, not taken as no limit")
        q.add_batch("b1", setup, [("Kh9h4h", 1), ("As7d2c", 2), ("Qs8c3d", 1)], 0.5, None, None, "no_rivers", None)
        listed = q.jobs()
        q.update("00001", status="running", runner_pid=os.getpid())  # the runner starts it after the listing
        check(raises(q.remove, listed[:1]), "remove re-reads: a job that started since it was listed is refused")
        check(raises(q.move, "00001", 3), "only a queued job can be moved")
        q.update("00001", status="done", runner_pid=None)
        q.move("00003", 1)
        check(q.job("00001")["status"] == "done", "move keeps a finished job's status")
        check([j["id"] for j in q.jobs() if j["status"] == "queued"] == ["00003", "00002"], "move among queued jobs")
        q.remove([q.job("00002")])
        check(q.update("00002", status="done") is None and raises(q.job, "00002"),
              "a status change of a removed job doesn't bring it back")
        check(jobs.run_job(q, listed[1], "/nonexistent", 1, log=lambda m: None) == "skipped",
              "the runner skips a job removed since it listed it")
        check(jobs.run_job(q, q.job("00001"), "/nonexistent", 1, log=lambda m: None) == "skipped",
              "the runner skips a job that is no longer queued")
        os.remove(os.path.join(q.batches_dir, "b1.json"))
        check(jobs.run_job(q, q.job("00003"), "/nonexistent", 1, log=lambda m: None) == "failed"
              and q.job("00003")["status"] == "failed", "a job whose batch is gone fails instead of looping")
        real = os.listdir
        os.listdir = lambda d: real(d) + ["99999.json"]  # listed, then removed before it was read
        try:
            ids = [j["id"] for j in q.jobs()]
        finally:
            os.listdir = real
        check(ids == ["00003", "00001"], f"a job removed between listing and reading is left out ({ids})")
        check(q.retry(None) == 1 and q.job("00003")["status"] == "queued", "retry --failed")
        q.update("00001", status="running", runner_pid=os.getpid())
        check(raises(q.retry, ["00001"]), "retry refuses a running job")

    print("[concurrent adds]")
    with tempfile.TemporaryDirectory() as td:
        import threading
        setup, _ = jobs.tree_setup(TREE)
        errs = []

        def add(k):
            try:
                jobs.Queue(td).add_batch(f"t{k}", setup, [("Kh9h4h", 1), ("As7d2c", 1)], 0.5, None, None,
                                          "no_rivers", None)
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=add, args=(k,)) for k in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        js = jobs.Queue(td).jobs()
        check(not errs and len(js) == 16 and len({j["id"] for j in js}) == 16,
              f"8 batches added at once get 16 distinct jobs ({len(js)} jobs, {len({j['id'] for j in js})} ids)")
        tree = os.path.join(td, "tree.txt")
        with open(tree, "w") as f:
            f.write(TREE)
        cmd = [sys.executable, os.path.join(REPO, "tools", "jobs.py"), "--queue", td]
        procs = [subprocess.Popen(cmd + ["add", f"p{k}", tree, "--flops", "Kh9h4h,As7d2c", "--no-check"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for k in range(4)]
        outs = [p.communicate()[1] for p in procs]
        js = jobs.Queue(td).jobs()
        check(len(js) == 16 + 8 and len({j["id"] for j in js}) == 24,
              f"4 processes adding at once: distinct ids ({len(js)} jobs; {[o[-200:] for o in outs if o]})")


def integration(solver):
    print("[run]")
    with tempfile.TemporaryDirectory() as td:
        qdir = os.path.join(td, "q")
        tree = os.path.join(td, "tree.txt")
        with open(tree, "w") as f:
            f.write(TREE)

        def cli(*a):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = jobs.main(["--queue", qdir] + list(a))
            return rc, buf.getvalue()

        rc, out = cli("add", "srp", tree, "--flops", "Kh9h4h,As7d2c,Qs8c3d", "--max-iters", "30",
                      "--solver", solver)
        check(rc == 0 and "queued 3 jobs" in out and "ignoring 3 run command" in out and "tree OK on Kh9h4h" in out,
              out.strip())
        badtree = os.path.join(td, "bad.txt")
        with open(badtree, "w") as f:
            f.write(TREE + "add_line 30 20\n")
        rc, out = cli("add", "nope", badtree, "--flops", "Kh9h4h", "--solver", solver)
        check(rc == 2 and not os.path.exists(os.path.join(qdir, "batches", "nope.json")),
              "add rejects a tree that does not build")
        rc, out = cli("run", "00002", "--solver", solver, "--threads", "2")
        check(rc == 0 and "1 done" in out, "run one job by id")
        q = jobs.Queue(qdir)
        st = [j["status"] for j in q.jobs()]
        check(st == ["queued", "done", "queued"], f"only that job ran {st}")
        j = q.job("00002")
        r = j["result"]
        check(os.path.getsize(j["out"]) > 0 and 0 < r["iterations"] <= 30 and r["exploitability"] >= 0,
              f"result recorded and saved ({r})")
        # the save opens and is the job's tree
        p = subprocess.run([solver], input=f'set_end_string END\nload_tree "{j["out"]}"\nshow_node r\nexit\n',
                           capture_output=True, text=True, timeout=120)
        check("load_tree ok!" in p.stdout and "As 7d 2c" in p.stdout and "INCOMPLETE_TREE" in p.stdout,
              "the save loads: right board, no_rivers")
        rc, out = cli("run", "00002", "--solver", solver)
        check(rc == 2, "a finished job is not re-run without retry")
        with open(q.stop_file, "w") as f:
            f.write("after")
        rc, out = cli("run", "--solver", solver, "--limit", "1", "--threads", "2")
        check(rc == 0 and "1 done" in out, "a stale stop request from before the run is ignored")
        rc, out = cli("run", "--solver", solver, "--threads", "2")
        check(rc == 0 and "1 done" in out and all(j["status"] == "done" for j in q.jobs()), "run the rest")
        rc, out = cli("add", "bad", tree, "--flops", "Kh9h4h", "--max-iters", "5", "--no-check")
        bad = q.jobs("bad")[0]
        b = q.batch("bad")
        b["setup"].append("add_line 30 20")  # an illegal amount: build_tree fails
        jobs._write_json(os.path.join(q.batches_dir, "bad.json"), b)
        rc, out = cli("run", "--solver", solver)
        check(rc == 1 and q.job(bad["id"])["status"] == "failed" and q.job(bad["id"])["error"],
              "a failing job is marked failed with its error")
        rc, out = cli("retry", "--failed")
        check(q.job(bad["id"])["status"] == "queued", "retry --failed")
        rc, out = cli("export", "srp")
        rows = out.strip().splitlines()
        check(len(rows) == 4 and rows[0].startswith("id,board,weight") and "Kh9h4h" in out, "export CSV")
        rc, out = cli("list")
        check(rc == 0 and "3 done" in out and "1 queued" in out, out.strip().splitlines()[-1])
        rc, out = cli("show", "00001")
        check(json.loads(out)["board"] == "Kh9h4h", "show")


def main():
    solver = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "solver"))
    unit()
    integration(solver)
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
