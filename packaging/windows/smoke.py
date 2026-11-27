#!/usr/bin/env python3
"""Does the staged (or installed) Windows app run? (#565; packaging/windows/build.sh runs this
before it writes the installer.)

    <app>\\python\\python.exe -B -E -s smoke.py <app folder> <version>

Run with the app's own Python, so that what it proves is what a PC with nothing else installed has:
the trimmed Python still holds every module the viewer imports, the viewer finds its page and its
tools in the app folder, and the solver there starts, says the version the installer is named
after, and solves a small tree. The viewer is started by the app's own launcher, OpenSolver.exe, with
a port of its own and a library, a job queue and a log in a temporary folder, so nothing of the
user's is touched. Closing its window while a solve runs must ask first (and Cancel keep it all
running); closing it when nothing runs must quit and leave nothing running; and a launcher killed
outright, as a crash would end it, must still leave nothing.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
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
# one that takes a while to solve to a tight target, for quitting in the middle of it
LONG = dict(CFG, range_oop="22+,A2s+,K9s+,ATo+", range_ip="22+,A2s+,K9s+,ATo+", board="QhJc2c", stack=200,
            flop_bet="33,75", flop_raise="60")
fails = 0

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
SYNCHRONIZE, QUERY = 0x00100000, 0x1000
WM_CLOSE, WM_COMMAND, IDCANCEL = 0x0010, 0x0111, 2


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def check(cond, msg):
    global fails
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        fails += 1


def processes():
    """(pid, parent pid, exe name) of every process."""
    snap = k32.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
    out = []
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile.lower()))
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


def children(pid, name=None):
    return [p for p, pp, exe in processes() if pp == pid and (name is None or exe == name)]


class Proc:
    """A process held by a handle, so that its pid cannot name another one while it is watched."""

    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(SYNCHRONIZE | QUERY, False, pid)

    def alive(self):
        return bool(self.h) and k32.WaitForSingleObject(self.h, 0) == 0x102   # WAIT_TIMEOUT

    def wait(self, seconds):
        return not self.h or k32.WaitForSingleObject(self.h, int(seconds * 1000)) == 0

    def exit_code(self):
        code = wt.DWORD()
        k32.GetExitCodeProcess(self.h, ctypes.byref(code))
        return code.value


def windows_of(pid, cls="OpenSolverLauncher"):
    """The visible top-level windows of class `cls` (#32770: a message box) that process `pid` has."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        owner = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        name = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, name, 64)
        if owner.value == pid and name.value == cls and u32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    u32.EnumWindows(each, 0)
    return found


def until(cond, seconds):
    deadline = time.time() + seconds
    while not cond() and time.time() < deadline:
        time.sleep(0.1)
    return cond()


def start(app, tmp, run):
    """The launcher, as the Start menu starts it but for the options that keep it to `tmp`; and
    the address its viewer is on, read from its log (one of its own: not the last run's)."""
    logs = os.path.join(tmp, f"logs{run}")
    launcher = subprocess.Popen([os.path.join(app, "OpenSolver.exe"), "--no-browser", "--new-instance",
                                 "--log-dir", logs, "--port", "0",
                                 "--library", os.path.join(tmp, "library"), "--jobs", os.path.join(tmp, "jobs")])
    log = os.path.join(logs, "viewer.log")
    deadline = time.time() + 60
    while time.time() < deadline and launcher.poll() is None:
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        line = next((ln for ln in text.splitlines() if READY in ln), None)
        if line:
            return launcher, "http://" + line.split(READY, 1)[1].strip(), line
        time.sleep(0.1)
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            print(f.read())
    except OSError:
        pass
    return launcher, None, None


def tree(launcher):
    """The launcher's viewer and the viewer's solvers, each held."""
    viewers = children(launcher.pid, "python.exe")
    viewer = Proc(viewers[0]) if viewers else None
    solvers = [Proc(p) for p in children(viewer.pid, "solver.exe")] if viewer else []
    return viewer, solvers


def main():
    app, version = os.path.abspath(sys.argv[1]), sys.argv[2]
    python = os.path.join(app, "python", "python.exe")
    print(f"[the app runs] {app}")
    check(os.path.normcase(os.path.realpath(sys.executable)) == os.path.normcase(os.path.realpath(python)),
          "run by the app's own Python")
    import ssl   # the natural-language lock asks a service over https
    check(ssl.create_default_context().cert_store_stats()["x509_ca"] > 0, "the system's certificates are found")

    tmp = tempfile.mkdtemp(prefix="opensolver-smoke-")
    launcher = None
    try:
        launcher, base, line = start(app, tmp, 1)
        check(base is not None, f"the launcher starts the viewer ({(line or '').strip()[:120]})")
        if base is None:
            return

        def req(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            r = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                       headers={"Content-Type": "application/json"} if data is not None else {})
            with urllib.request.urlopen(r, timeout=120) as f:
                raw = f.read()
                return json.loads(raw) if f.headers.get_content_type() == "application/json" else raw

        st = req("/api/status")
        check(st.get("ok") is True, "the solver in the app answers")
        check(st.get("version") == f"OpenSolver {version}", f"the packaged solver reports OpenSolver {version} ({st.get('version')})")
        static = os.path.join(app, "viewer", "static")
        names = sorted(os.listdir(static))
        same = [n for n in names if req("/" + n) == open(os.path.join(static, n), "rb").read()]
        check(len(names) > 10 and same == names, f"the page is served from the app ({len(same)} of {len(names)} files)")

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

        # Closing the window while a solve runs asks first, and Cancel keeps everything running.
        req("/api/generate", LONG)
        req("/api/build", {})
        req("/api/solve", {"accuracy": 0.00001})
        check(until(lambda: req("/api/status")["solving"], 10), "a long solve runs")
        for w in windows_of(launcher.pid):
            u32.PostMessageW(w, WM_CLOSE, 0, 0)
        asked = until(lambda: windows_of(launcher.pid, "#32770"), 10)
        check(bool(asked) and launcher.poll() is None, "closing the window while it runs asks first")
        for d in asked:
            u32.PostMessageW(d, WM_COMMAND, IDCANCEL, 0)
        until(lambda: not windows_of(launcher.pid, "#32770"), 10)
        check(launcher.poll() is None and req("/api/status")["solving"], "and Cancel leaves it running")
        req("/api/stop", {})
        check(until(lambda: not req("/api/status")["solving"], 30), "until it is stopped")

        viewer, solvers = tree(launcher)
        check(viewer is not None and len(solvers) >= 1,
              f"the launcher runs the viewer, the viewer its solvers ({len(solvers)})")
        wins = windows_of(launcher.pid)
        check(len(wins) == 1, "the launcher has its window")
        for w in wins:
            u32.PostMessageW(w, WM_CLOSE, 0, 0)   # what closing the window or Quit does
        try:
            launcher.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        check(launcher.poll() is not None, "the launcher quits when its window is closed")
        if viewer:
            check(viewer.wait(5) and viewer.exit_code() == 0, f"the viewer stops on its Ctrl+C (exit code {viewer.exit_code():#x})")
        deadline = time.time() + 5
        while time.time() < deadline and any(s.alive() for s in solvers):
            time.sleep(0.1)
        check(solvers and not any(s.alive() for s in solvers), "and leaves no solver behind")

        # A launcher that is killed (a crash, End task) takes the viewer and its solvers with it.
        launcher, base, _ = start(app, tmp, 2)
        check(base is not None, "the launcher starts again")
        if base is None:
            return
        viewer, solvers = tree(launcher)
        launcher.kill()
        launcher.wait()
        gone = viewer is not None and viewer.wait(5)
        deadline = time.time() + 5
        while time.time() < deadline and any(s.alive() for s in solvers):
            time.sleep(0.1)
        check(gone and solvers and not any(s.alive() for s in solvers),
              f"a killed launcher leaves no viewer and no solver behind ({len(solvers)} solver(s))")
    finally:
        if launcher and launcher.poll() is None:
            launcher.kill()   # a failed run: its job object takes the rest
            launcher.wait()
        for _ in range(20):   # the log may still be held for a moment by what is ending
            shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.exists(tmp):
                break
            time.sleep(0.25)


if __name__ == "__main__":
    main()
    print("the app runs" if not fails else f"{fails} check(s) FAILED")
    sys.exit(1 if fails else 0)
