#!/usr/bin/env python3
"""Packaging checks for `make dist`, `make install` (#207) and what goes into the macOS app (#479).

These exist because the packaging path is part of what ships and had no coverage. The specific bug
that motivated them: `make dist` builds the tarball with `git archive`, so an **untracked** file is
silently absent from it. CHANGELOG.md was missing from the first tarball I cut, and nothing failed --
the archive was valid, just incomplete. A release that quietly loses a file is exactly the kind of
defect a test should catch.

Everything here runs staged into a temporary directory: no writes outside the repo's own dist/, and
no install to a real prefix.
"""
import os
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = 0


def check(cond, msg):
    global fails
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        fails += 1


def make(*args, **kw):
    return subprocess.run(["make", "-s", *args], cwd=ROOT, capture_output=True, text=True, **kw)


def version():
    src = open(os.path.join(ROOT, "src", "version.hpp")).read()
    m = re.search(r'SOLVER_VERSION "([^"]+)"', src)
    return m.group(1) if m else None


def test_dist():
    print("[make dist]")
    v = version()
    check(v is not None, f"version readable from src/version.hpp ({v})")
    r = make("dist")
    check(r.returncode == 0, "make dist succeeds")
    name = f"wsolver-{v}-src"
    tgz = os.path.join(ROOT, "dist", f"{name}.tar.gz")
    sha = tgz + ".sha256"
    check(os.path.exists(tgz), "tarball written")
    check(os.path.exists(sha), "checksum written")
    if not os.path.exists(tgz):
        return

    # The checksum must actually verify, not merely exist.
    r = subprocess.run(["shasum", "-a", "256", "-c", os.path.basename(sha)],
                       cwd=os.path.join(ROOT, "dist"), capture_output=True, text=True)
    check(r.returncode == 0, "checksum verifies with shasum -c")

    with tarfile.open(tgz) as t:
        names = t.getnames()
    rel = [n[len(name) + 1:] for n in names if n.startswith(name + "/")]

    # `make dist` archives HEAD, so HEAD is what the tarball must be judged against -- NOT the index.
    # Using `git ls-files` for this was a bug: the index also holds staged-but-uncommitted additions,
    # which `git archive HEAD` correctly omits, so the test failed in any mid-merge or
    # staged-not-yet-committed tree. That is a normal state, not a packaging fault.
    committed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                               cwd=ROOT, capture_output=True, text=True).stdout.split()

    # The files a recipient cannot build or evaluate the release without. Judged against HEAD for the
    # same reason: if one of these is not committed yet, that is the commit's problem, not dist's.
    for need in ("Makefile", "README.md", "CHANGELOG.md", "src/solver.cpp", "src/version.hpp",
                 "docs/commands.md", "tests/test_upi.py", "bench/run.py"):
        if need in committed:
            check(need in rel, f"tarball contains {need}")
        else:
            print(f"  skip: {need} not committed in HEAD, so dist cannot contain it")

    # Build output and local state must not ship. `git archive` gives this for free today; the test
    # is here so that stays true if dist ever stops using it.
    leaked = [n for n in rel if n.startswith(("build/", "dist/")) or n.endswith((".o", ".cfr", ".profraw"))
              or n == "solver" or n == "solver-release"]
    check(not leaked, f"no build output or local state in tarball ({leaked[:4]})")

    missing = sorted(set(committed) - set(rel))
    check(not missing, f"every file committed in HEAD is in the tarball ({len(missing)} missing: {missing[:4]})")

    # The bug that motivated this file: an untracked file is absent from the archive, and `make dist`
    # must say so rather than cutting a quietly incomplete release.
    probe = os.path.join(ROOT, "ZZ_untracked_probe.tmp")
    try:
        with open(probe, "w") as f:
            f.write("probe\n")
        r = make("dist")
        warned = "untracked" in (r.stdout + r.stderr).lower()
        check(warned, "make dist warns that untracked files are not in the tarball")
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def test_install():
    print("[make install / uninstall]")
    with tempfile.TemporaryDirectory() as td:
        r = make("install", f"DESTDIR={td}")
        check(r.returncode == 0, "make install succeeds (staged)")
        binary = os.path.join(td, "usr", "local", "bin", "wsolver")
        check(os.path.exists(binary), "binary installed as wsolver")
        if os.path.exists(binary):
            check(os.access(binary, os.X_OK), "installed binary is executable")
            # Run the installed copy, not the one in the build tree: a broken install can still
            # leave a working ./solver behind.
            out = subprocess.run([binary], input="show_version\nexit\n",
                                 capture_output=True, text=True).stdout
            check(version() in out, f"installed binary reports its version ({version()})")
        for doc in ("README.md", "commands.md"):
            check(os.path.exists(os.path.join(td, "usr", "local", "share", "doc", "wsolver", doc)),
                  f"doc installed: {doc}")
        r = make("uninstall", f"DESTDIR={td}")
        check(r.returncode == 0, "make uninstall succeeds")
        left = [os.path.join(dp, f) for dp, _, fs in os.walk(td) for f in fs]
        check(not left, f"uninstall removes everything it installed ({left[:3]})")

    # BINNAME must carry through to the docs directory too, or a renamed install scatters.
    with tempfile.TemporaryDirectory() as td:
        r = make("install", f"DESTDIR={td}", "PREFIX=/opt/ws", "BINNAME=wsolver-alt")
        check(r.returncode == 0, "make install honours PREFIX and BINNAME")
        check(os.path.exists(os.path.join(td, "opt", "ws", "bin", "wsolver-alt")),
              "renamed binary at the custom prefix")
        check(os.path.exists(os.path.join(td, "opt", "ws", "share", "doc", "wsolver-alt", "README.md")),
              "docs follow BINNAME")


def test_commands_doc():
    """docs/commands.md has a row for every registered UPI command and no other (#404)."""
    print("[docs/commands.md against src/upi*.cpp]")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_commands_doc
    errors, stale = check_commands_doc.problems()
    check(not errors, f"every command has a row, every row a command, the count is right {errors[:3]}")
    if stale:  # any source edit moves lines: not a failure
        print(f"  note: {len(stale)} line link(s) drifted; python3 tools/check_commands_doc.py --fix")


def test_macos_app():
    """What `make macos-pkg` puts in WSolver.app (#479), as far as it can be checked without building
    it (the build downloads a Python; packaging/macos/build.sh runs the built app itself, smoke.py).
    The failure this is for is the quiet one: a module the viewer starts to import, or a file its page
    starts to load, that the app does not carry -- everything passes here, and the installed app
    does not start."""
    print("[macOS app: packaging/macos]")
    import ast
    pk = os.path.join(ROOT, "packaging", "macos")
    r = subprocess.run([os.path.join(pk, "build.sh"), "--files"], cwd=ROOT, capture_output=True, text=True)
    staged = r.stdout.split()
    check(r.returncode == 0 and len(staged) > 20, f"build.sh --files lists the app's files ({len(staged)})")
    check(all(os.path.isfile(os.path.join(ROOT, f)) for f in staged), "each of them exists")
    viewer = os.path.join(ROOT, "viewer")
    want = [f"viewer/{f}" for f in os.listdir(viewer) if f.endswith(".py") and not f.startswith("test_")]
    want += [f"viewer/static/{f}" for f in os.listdir(os.path.join(viewer, "static"))
             if f not in ("package.json", ".DS_Store")]
    want += ["tools/tree_builder.py", "tools/jobs.py"]   # loaded by path: server.py, jobs_api.py
    missing = sorted(set(want) - set(staged))
    check(not missing, f"every viewer module, every file of its page and the two tools are in it {missing[:4]}")
    check(not [f for f in staged if "/test_" in f], "and no tests")

    # Imports of the staged Python: a module of this repo must be staged too, and one of the
    # standard library must not be among those build.sh takes out of the bundled Python.
    build_sh = open(os.path.join(pk, "build.sh")).read()
    trimmed = set(re.findall(r'"\$PYLIB/([a-z0-9_]+)"', build_sh)) - {"site-packages"}
    check({"tkinter", "test"} <= trimmed, f"the modules build.sh trims are read from it ({sorted(trimmed)})")
    lost, cut = [], []
    for f in (f for f in staged if f.endswith(".py")):
        for node in ast.walk(ast.parse(open(os.path.join(ROOT, f)).read())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
            for top in (n.split(".")[0] for n in names):
                for d in ("viewer", "tools"):
                    if os.path.isfile(os.path.join(ROOT, d, top + ".py")) and f"{d}/{top}.py" not in staged:
                        lost.append(f"{f}: {d}/{top}.py")
                if top in trimmed:
                    cut.append(f"{f}: {top}")
    check(not lost, f"every module of the repo they import is in it {lost[:4]}")
    check(not cut, f"none imports a module trimmed from the bundled Python {cut[:4]}")

    # The page: what a staged file loads from the viewer (./x.js, style.css, ...) must be staged.
    static = {f.split("/")[-1] for f in staged if f.startswith("viewer/static/")}
    refs = set()
    for f in (f for f in staged if f.startswith("viewer/static/") and f.endswith((".js", ".html"))):
        text = open(os.path.join(ROOT, f)).read()
        refs |= {(f, m) for m in re.findall(r"""(?:from|import)\s*\(?\s*["']\./([\w.-]+)["']""", text)}
        if f.endswith(".html"):
            refs |= {(f, m.lstrip("/")) for m in re.findall(r"""(?:src|href)=["']/?([\w.-]+\.\w+)["']""", text)}
    gone = sorted(f"{f}: {m}" for f, m in refs if m not in static)
    check(len(refs) > 10 and not gone, f"every file the page loads is in it ({len(refs)} references) {gone[:4]}")

    # The version the package is named after must be one a property list and a path can carry.
    check(re.fullmatch(r"[0-9]+(\.[0-9]+){1,2}", version() or "") is not None, f"the version fits a bundle ({version()})")
    if sys.platform == "darwin" and os.uname().machine == "arm64":   # elsewhere the target only says where it is built
        r = make("-n", "macos-pkg")
        check(r.returncode == 0 and "packaging/macos/build.sh" in r.stdout, "make macos-pkg is a target that runs build.sh")
    try:
        import xml.dom.minidom
        xml.dom.minidom.parse(os.path.join(pk, "distribution.xml.in"))
        check(True, "distribution.xml.in is well-formed")
    except Exception as e:  # noqa: BLE001
        check(False, f"distribution.xml.in is well-formed ({e})")
    for name in ("welcome.html", "conclusion.html", "icon.svg", "smoke.py", "launcher.m"):
        check(os.path.isfile(os.path.join(pk, name)), f"{name} is there")
    if sys.platform != "darwin":
        print("  (not macOS: the property list and the launcher are not checked)")
        return
    with tempfile.TemporaryDirectory() as td:
        plist = os.path.join(td, "Info.plist")
        text = open(os.path.join(pk, "Info.plist.in")).read()
        for k, v in (("VERSION", version()), ("MACOS_MIN", "13.3"), ("BUNDLE_ID", "com.wsolver.WSolver")):
            text = text.replace(f"@{k}@", v)
        check("@" not in text, "Info.plist.in has no placeholder build.sh does not fill")
        open(plist, "w").write(text)
        r = subprocess.run(["plutil", "-lint", plist], capture_output=True, text=True)
        check(r.returncode == 0, f"the filled Info.plist is a valid property list {r.stdout.strip()[-80:] if r.returncode else ''}")
    r = subprocess.run(["clang", "-fobjc-arc", "-fsyntax-only", "-Wall", "-Wextra", "-Werror",
                        os.path.join(pk, "launcher.m")], capture_output=True, text=True)
    check(r.returncode == 0, f"the launcher compiles without a warning {r.stderr.strip()[:300]}")


if __name__ == "__main__":
    test_commands_doc()
    test_macos_app()
    test_dist()
    test_install()
    print("ALL PASSED" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
