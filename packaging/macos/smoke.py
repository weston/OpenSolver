#!/usr/bin/env python3
"""Does the staged WSolver.app run? (#479; packaging/macos/build.sh runs this before it packs.)

    <app>/Contents/Resources/python/bin/python3 smoke.py <WSolver.app> <version>

Run with the app's own Python in an empty environment, so that what it proves is what a Mac with
nothing else installed has: the trimmed Python still holds every module the viewer imports, the
viewer finds its page and its tools inside the bundle, and the solver in the bundle starts, says
the version the package is named after, and solves a small tree. The viewer is started the way the
launcher starts it, on a port of its own and with a library and a queue in a temporary folder, so
nothing of the user's is touched.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

READY = "UI on http://"
# a small flop tree: seconds to build and solve
CFG = {
    "range_oop": "AA,KK,QQ,AK", "range_ip": "AA,KK,JJ,AK",
    "board": "QhQc2c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "", "turn_bet": "60", "turn_raise": "",
    "river_bet": "65", "river_raise": "", "river_allin": True,
    "allin_threshold": 50, "add_allin_limit": 300, "iso": True,
}
fails = 0


def check(cond, msg):
    global fails
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        fails += 1


def children(pid):
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    return [int(a) for a, b in (ln.split() for ln in out.splitlines() if ln.strip()) if int(b) == pid]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    app, version = os.path.abspath(sys.argv[1]), sys.argv[2]
    res = os.path.join(app, "Contents", "Resources")
    python = os.path.join(res, "python", "bin", "python3")
    print(f"[the app runs] {app}")
    check(os.path.realpath(sys.executable) == os.path.realpath(python), "run by the app's own Python")
    import ssl   # the natural-language lock asks a service over https
    check(ssl.create_default_context().cert_store_stats()["x509_ca"] > 0, "the system's certificates are found")

    tmp = tempfile.mkdtemp(prefix="wsolver-smoke-")
    srv = subprocess.Popen(
        [python, "-u", "-B", "-E", "-s", os.path.join(res, "viewer", "server.py"),
         "--solver", os.path.join(res, "solver"), "--port", "0",
         "--library", os.path.join(tmp, "library"), "--jobs", os.path.join(tmp, "jobs"), "--exit-with-parent"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = srv.stdout.readline()
        check(READY in line, f"the viewer starts ({line.strip()[:120]})")
        if READY not in line:
            print(srv.stdout.read())
            return
        base = "http://" + line.split(READY, 1)[1].strip()

        def req(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            r = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                       headers={"Content-Type": "application/json"} if data is not None else {})
            with urllib.request.urlopen(r, timeout=120) as f:
                raw = f.read()
                return json.loads(raw) if f.headers.get_content_type() == "application/json" else raw

        st = req("/api/status")
        check(st.get("ok") is True, "the solver in the bundle answers")
        check(str(st.get("version", "")).endswith(" " + version), f"it is version {version} ({st.get('version')})")
        static = os.path.join(res, "viewer", "static")
        names = sorted(os.listdir(static))
        same = [n for n in names if req("/" + n) == open(os.path.join(static, n), "rb").read()]
        check(len(names) > 10 and same == names, f"the page is served from the bundle ({len(same)} of {len(names)} files)")

        req("/api/generate", CFG)    # tools/tree_builder.py, through the viewer
        built = req("/api/build", {})
        check("TOTAL" in built.get("node_count", ""), "a tree is built")
        req("/api/solve", {"accuracy": 1.0})
        deadline, done = time.time() + 120, False
        while time.time() < deadline and not done:
            p = req("/api/progress")
            done = not p["solving"] and p["solver_status"].startswith("stopped")
            time.sleep(0.2)
        check(done, "it is solved")
        node = req("/api/node?id=r:0")
        check(len(node.get("children", [])) >= 2, "and browsed")

        kids = children(srv.pid)
        check(len(kids) >= 1, f"the viewer runs its solvers as children ({len(kids)})")
        srv.send_signal(signal.SIGINT)   # what quitting the app sends
        try:
            srv.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        check(srv.poll() is not None, "the viewer stops when told to")
        deadline = time.time() + 5
        while time.time() < deadline and any(alive(k) for k in kids):
            time.sleep(0.1)
        check(not any(alive(k) for k in kids), "and leaves no solver behind")
    finally:
        if srv.poll() is None:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("the app runs" if not fails else f"{fails} check(s) FAILED")
    sys.exit(1 if fails else 0)
