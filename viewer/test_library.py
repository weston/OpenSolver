#!/usr/bin/env python3
"""Tests for viewer/library.py and its HTTP API (issue #116).

No solver binary needed: library endpoints never touch the solver, so the HTTP
tests run the real Handler with solver=None and a Library rooted in a temp dir.
The real home directory is never touched.

Usage: python3 viewer/test_library.py
"""
import json
import os
import shutil
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

import library as libm  # noqa: E402
import server as vsrv  # noqa: E402

RANGE = "AA,KK,QQ,AKs,AKo"
CONFIG = {
    "schema": 1,
    "range_oop": "AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQs",
    "range_ip": "AA,KK,QQ,JJ,TT,99,AK,AQ,KQs",
    "board": "QhQc2c", "pot": 40, "stack": 60,
    "flop_bet": "60", "flop_raise": "50", "flop_donk": "",
    "turn_bet": "60", "turn_raise": "50", "turn_donk": "",
    "river_bet": "65", "river_raise": "50", "river_donk": "",
    "flop_allin": False, "turn_allin": False, "river_allin": True,
    "allin_threshold": 50, "add_allin_limit": 300, "iso": True,
    # extra fields the form redesign / #114 will add must survive a round trip
    "raise_cap": {"flop": 4, "turn": 3, "river": 2},
    "flop_bet_extra": "33, 75, 2e, 3x, a",
}


def make_library():
    return libm.Library(tempfile.mkdtemp(prefix="wsolver-lib-test-"))


class LibraryUnitTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.addCleanup(shutil.rmtree, self.lib.root, True)

    # ----- ranges -----
    def test_range_crud_nested_folders(self):
        name = "BTN vs BB/SRP/BB call"
        self.lib.save("range", name, RANGE)
        self.assertEqual(self.lib.get("range", name), RANGE + "\n")
        entries = self.lib.list("range")
        self.assertEqual([e["name"] for e in entries], [name])
        self.assertGreater(entries[0]["bytes"], 0)
        # files are plain text on disk
        with open(os.path.join(self.lib.root, "ranges", "BTN vs BB", "SRP", "BB call.txt")) as f:
            self.assertEqual(f.read().strip(), RANGE)
        # overwrite (edit a saved range and save it back)
        self.lib.save("range", name, "AA")
        self.assertEqual(self.lib.get("range", name), "AA\n")

    def test_range_rename_move_delete(self):
        self.lib.save("range", "a/b", RANGE)
        self.lib.save("range", "a/c", RANGE)
        self.lib.rename("range", "a/b", "other/d")  # move across folders
        self.assertEqual([e["name"] for e in self.lib.list("range")], ["a/c", "other/d"])
        self.assertEqual(self.lib.get("range", "other/d"), RANGE + "\n")
        with self.assertRaises(libm.LibraryError):
            self.lib.get("range", "a/b")
        # rename into an occupied name is refused
        with self.assertRaises(libm.LibraryError):
            self.lib.rename("range", "a/c", "other/d")
        # delete prunes empty folders but leaves non-empty ones
        self.lib.delete("range", "other/d")
        self.assertFalse(os.path.exists(os.path.join(self.lib.root, "ranges", "other")))
        self.assertTrue(os.path.isdir(os.path.join(self.lib.root, "ranges", "a")))
        self.lib.delete("range", "a/c")
        self.assertFalse(os.path.exists(os.path.join(self.lib.root, "ranges", "a")))
        with self.assertRaises(libm.LibraryError):
            self.lib.delete("range", "a/c")  # gone already

    def test_range_rejects_bad_content(self):
        for bad in ("", "   ", None, {"x": 1}, "ok\x00no"):
            with self.assertRaises(libm.LibraryError):
                self.lib.save("range", "x", bad)

    # ----- configs -----
    def test_config_round_trip_keeps_unknown_fields(self):
        name = "cash 100bb/SRP/oop 60"
        self.lib.save("config", name, CONFIG)
        back = self.lib.get("config", name)
        self.assertEqual(back, CONFIG)  # every field survives, extras included
        with open(os.path.join(self.lib.root, "configs", "cash 100bb", "SRP", "oop 60.json")) as f:
            self.assertEqual(json.load(f), CONFIG)

    def test_config_schema_enforced_and_defaulted(self):
        no_schema = {k: v for k, v in CONFIG.items() if k != "schema"}
        self.lib.save("config", "defaults", no_schema)
        back = self.lib.get("config", "defaults")
        self.assertEqual(back["schema"], 1)
        self.assertEqual(back["raise_cap"], CONFIG["raise_cap"])
        for bad in ({"schema": 2}, {"schema": "1"}, "not a dict", [1, 2], 42):
            with self.assertRaises(libm.LibraryError):
                self.lib.save("config", "bad", bad)

    def test_config_rejects_unserializable(self):
        with self.assertRaises(libm.LibraryError):
            self.lib.save("config", "x", {"schema": 1, "oops": object()})

    def test_get_of_malformed_file(self):
        os.makedirs(os.path.join(self.lib.root, "configs"))
        path = os.path.join(self.lib.root, "configs", "hand-edited.json")
        with open(path, "w") as f:
            f.write("{not json")
        with self.assertRaises(libm.LibraryError):
            self.lib.get("config", "hand-edited")
        with open(path, "w") as f:
            json.dump({"schema": 99}, f)
        with self.assertRaises(libm.LibraryError):
            self.lib.get("config", "hand-edited")

    # ----- path confinement -----
    def test_name_escapes_rejected(self):
        bad = ["../x", "a/../b", "..", "a//b", "/abs", "a/./b", "", ".",
               "a\\b", "nul\x00in-name", ".hidden/../x"]
        for kind in ("range", "config"):
            for name in bad:
                with self.assertRaises(libm.LibraryError, msg=f"{kind} {name!r}"):
                    self.lib.save(kind, name, RANGE if kind == "range" else CONFIG)
                with self.assertRaises(libm.LibraryError, msg=f"{kind} {name!r}"):
                    self.lib.get(kind, name)
                with self.assertRaises(libm.LibraryError, msg=f"{kind} {name!r}"):
                    self.lib.delete(kind, name)
        self.assertEqual(self.lib.list("range"), [])  # nothing got written anywhere

    def test_absolute_name_rejected_even_when_pointing_inside(self):
        inside = os.path.join(self.lib.root, "ranges", "sneaky")
        with self.assertRaises(libm.LibraryError):
            self.lib.save("range", inside, RANGE)

    def test_symlink_dir_escape_refused(self):
        """A folder inside the library that is a symlink to outside must not be usable."""
        outside = tempfile.mkdtemp(prefix="wsolver-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        os.makedirs(os.path.join(self.lib.root, "ranges"))
        os.symlink(outside, os.path.join(self.lib.root, "ranges", "escape"))
        with self.assertRaises(libm.LibraryError):
            self.lib.save("range", "escape/victim", RANGE)
        with self.assertRaises(libm.LibraryError):
            self.lib.get("range", "escape/victim")
        self.assertEqual(os.listdir(outside), [])  # nothing escaped

    def test_symlink_file_escape_refused(self):
        """A range file that is a symlink to a file outside the library is refused."""
        outside = tempfile.mkdtemp(prefix="wsolver-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w") as f:
            f.write("top secret range")
        os.makedirs(os.path.join(self.lib.root, "ranges"))
        os.symlink(secret, os.path.join(self.lib.root, "ranges", "link.txt"))
        with self.assertRaises(libm.LibraryError):
            self.lib.get("range", "link")
        # read it back exactly as written: the outside file is untouched
        with open(secret) as f:
            self.assertEqual(f.read(), "top secret range")
        # listing skips symlinks entirely
        self.assertEqual(self.lib.list("range"), [])

    def test_root_never_created_until_first_save(self):
        self.assertEqual(self.lib.list("range"), [])
        self.assertEqual(self.lib.list("config"), [])
        with self.assertRaises(libm.LibraryError):
            self.lib.get("range", "missing")

    def test_unknown_kind(self):
        for kind in ("trees", [1, 2], {"a": 1}, None, 3):  # unhashable kinds too (a 500 before, found by fuzzing)
            for op in (lambda: self.lib.list(kind), lambda: self.lib.get(kind, "x"),
                       lambda: self.lib.save(kind, "x", "y"), lambda: self.lib.delete(kind, "x"),
                       lambda: self.lib.rename(kind, "x", "y")):
                with self.assertRaises(libm.LibraryError):
                    op()


class LibraryHttpTest(unittest.TestCase):
    """The five endpoints over the real HTTP handler (solver not needed)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wsolver-lib-http-")
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), vsrv.Handler)
        cls.http.solver = None  # library endpoints must not touch the solver
        cls.http.library = libm.Library(cls.tmp)
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        shutil.rmtree(cls.tmp, True)

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if method == "POST" and data is None:
            data = b"{}"
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=30) as f:
                return f.status, json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_full_round_trip(self):
        st, d = self.req("/api/library/save", "POST",
                         {"kind": "config", "name": "six-max/SRP vs BTN", "content": CONFIG})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["name"], "six-max/SRP vs BTN")
        self.assertTrue(d["ok"])
        st, d = self.req("/api/library/save", "POST",
                         {"kind": "range", "name": "BTN vs BB/SRP/BB call", "content": RANGE})
        self.assertEqual(st, 200, d)

        st, d = self.req("/api/library/list?kind=config")
        self.assertEqual(st, 200)
        self.assertEqual([e["name"] for e in d["entries"]], ["six-max/SRP vs BTN"])
        st, d = self.req("/api/library/list?kind=range")
        self.assertEqual([e["name"] for e in d["entries"]], ["BTN vs BB/SRP/BB call"])

        # load it back: the config the build form gets must be exactly what was saved
        st, d = self.req("/api/library/get?kind=config&name=" +
                         urllib.parse.quote("six-max/SRP vs BTN"))
        self.assertEqual(st, 200, d)
        self.assertEqual(d["content"], CONFIG)
        st, d = self.req("/api/library/get?kind=range&name=" + urllib.parse.quote("BTN vs BB/SRP/BB call"))
        self.assertEqual(d["content"], RANGE + "\n")

        # rename, then delete
        st, d = self.req("/api/library/rename", "POST",
                         {"kind": "config", "from": "six-max/SRP vs BTN", "to": "six-max/SRP vs BTN 30bb"})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/library/list?kind=config")
        self.assertEqual([e["name"] for e in d["entries"]], ["six-max/SRP vs BTN 30bb"])
        st, d = self.req("/api/library/delete", "POST", {"kind": "range", "name": "BTN vs BB/SRP/BB call"})
        self.assertEqual(st, 200, d)
        st, d = self.req("/api/library/list?kind=range")
        self.assertEqual(d["entries"], [])

    def test_errors_are_400(self):
        for path, body in (
            ("/api/library/list?kind=trees", None),
            ("/api/library/get?kind=range&name=" + urllib.parse.quote("../x"), None),
            ("/api/library/get?kind=range&name=missing", None),
            ("/api/library/save", {"kind": "config", "name": "x", "content": {"schema": 2}}),
            ("/api/library/save", {"kind": "range", "name": "x", "content": ""}),
            ("/api/library/save", {"kind": "config", "name": "x", "content": ["nope"]}),
            ("/api/library/rename", {"kind": "range", "from": "nope", "to": "y"}),
            ("/api/library/delete", {"kind": "range", "name": "nope"}),
        ):
            st, d = self.req(path, "POST" if body is not None else "GET", body)
            self.assertEqual(st, 400, (path, d))
            self.assertIn("error", d)
        # nothing was written by any of the failed calls
        st, d = self.req("/api/library/list?kind=config")
        self.assertEqual(d["entries"], [])

    def test_library_endpoints_reject_wrong_method(self):
        # mutating endpoints are POST-only (read endpoints ignore the method, like /api/status)
        for path in ("/api/library/save", "/api/library/rename", "/api/library/delete"):
            st, d = self.req(path)
            self.assertEqual(st, 400, (path, d))


if __name__ == "__main__":
    unittest.main(verbosity=2)
