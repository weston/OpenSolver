#!/usr/bin/env python3
"""WSolver viewer server: drives ./solver over UPI and serves the browser UI on localhost.

Python 3 standard library only. The solver is spawned once and driven one command at a
time through a lock, the way tests/test_upi.py's Upi class does; asynchronous "SOLVER:"
blocks are demultiplexed for progress display. The HTTP API is a fixed set of endpoints
(whitelist by construction): there is deliberately no "run any UPI command" endpoint, so
nothing here can write arbitrary files (except `dump_tree` to a user-given path) or run
destructive commands (no stdoutredi).

    python3 viewer/server.py [--solver PATH] [--port 8765] [--library DIR]

`--library` sets the root for saved ranges and tree configs (viewer/library.py,
issue #116); it defaults to ~/.wsolver/library.

Then open http://127.0.0.1:8765 in a browser.
"""

import argparse
import importlib.util
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from library import Library, LibraryError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")
END = "END"
NUM_COMBOS = 1326

RANKS = "AKQJT98765432"
SUITS = "cdhs"


def load_tree_builder():
    """Import tools/tree_builder.py from the repo (no package layout to rely on)."""
    spec = importlib.util.spec_from_file_location("tree_builder", os.path.join(REPO, "tools", "tree_builder.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("tree_builder", mod)  # dataclasses look modules up here
    spec.loader.exec_module(mod)
    return mod


class SolverError(Exception):
    """Bad solver response, timeout, or dead solver."""


class Solver:
    """One UPI session with ./solver: a lock for commands, a pump thread for SOLVER blocks."""

    def __init__(self, exe):
        self.exe = exe
        self.cmd_lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.solving = False
        self.progress = []       # last periodic SOLVER: block (list of lines)
        self.solver_status = ""  # "started" / "stopped (<reason>)"
        self.events = 0          # bumped on every SOLVER event, for cheap polling
        self.tree_present = False
        self.dead = False
        self.version = ""
        self.generation = 0
        self.series = []         # convergence points of the current tree: {t, explo, ev_oop, ev_ip}
        self.start()

    def start(self):
        """Spawn the solver and its pump. Caller must hold cmd_lock (or be the only thread)."""
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.generation += 1
        self.q = queue.Queue()
        threading.Thread(target=self._pump, args=(self.generation, self.proc.stdout, self.q),
                         daemon=True).start()
        self.send_raw("set_end_string " + END)
        self._read_response()  # banner lines, skipped until the first END
        self.version = self.cmd("show_version")

    def _pump(self, generation, stream, q):
        """Demux solver stdout: SOLVER: blocks go to state, everything else to the response queue."""
        try:
            for line in stream:
                line = line.rstrip("\n")
                if line == "SOLVER:":
                    block = []
                    for l2 in stream:
                        l2 = l2.rstrip("\n")
                        if l2 == END:
                            break
                        block.append(l2)
                    point = parse_progress_point(block)
                    with self.state_lock:
                        self.progress = block
                        if point is not None:
                            self.series.append(point)
                        self.events += 1
                elif line.startswith("SOLVER:"):
                    with self.state_lock:
                        msg = line[len("SOLVER: "):]
                        self.solver_status = msg
                        if msg.startswith("started"):
                            self.solving = True
                            self.progress = []
                        elif msg.startswith("stopped"):
                            self.solving = False
                        self.events += 1
                else:
                    q.put(line)
        finally:
            q.put(None)
            with self.state_lock:
                if generation == self.generation:  # a stale pump must not mark a fresh solver dead
                    self.dead = True
                    self.solving = False

    def send_raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read_response(self, timeout=600):
        out = []
        while True:
            try:
                line = self.q.get(timeout=timeout)
            except queue.Empty:
                raise SolverError("timeout waiting for solver response")
            if line is None:
                raise SolverError("solver exited")
            if line == END:
                return "\n".join(out)
            out.append(line)

    def cmd(self, line, timeout=600):
        """Run one UPI command. Raises SolverError on an ERROR: response."""
        with self.cmd_lock:
            self.send_raw(line)
            resp = self._read_response(timeout)
        if resp.startswith("ERROR"):
            raise SolverError(resp.split("\n", 1)[0])
        return resp

    def cmds(self, lines, timeout=600):
        """Several commands in one lock hold; stops at the first ERROR."""
        with self.cmd_lock:
            for line in lines:
                self.send_raw(line)
                resp = self._read_response(timeout)
                if resp.startswith("ERROR"):
                    raise SolverError(f"{line.strip()[:120]} -> {resp.splitlines()[0]}")

    def state(self):
        with self.state_lock:
            return {"solving": self.solving, "dead": self.dead, "progress": list(self.progress),
                    "solver_status": self.solver_status, "events": self.events,
                    "tree": self.tree_present, "version": self.version}

    def reset_series(self):
        with self.state_lock:
            self.series = []

    def restart(self):
        with self.cmd_lock:
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
                for f in (self.proc.stdin, self.proc.stdout):
                    f.close()
            except Exception:
                pass
            self.start()  # cmd_lock is an RLock, cmd() inside start() is fine
            self.tree_present = False
            self.reset_series()
            with self.state_lock:
                self.dead = False
                self.solving = False
                self.progress = []
                self.solver_status = ""


# ---------------------------------------------------------------- parsing UPI outputs

def parse_num(tok):
    """A UPI float; non-finite values become None (JSON has no nan/inf)."""
    v = float(tok)
    if not math.isfinite(v):
        return None
    return float(f"{v:.6g}")


def parse_progress_point(block):
    """One periodic SOLVER: block -> a convergence point, or None if it lacks the fields (#115)."""
    vals = {}
    for line in block:
        key, _, val = line.partition(":")
        try:
            vals[key.strip()] = float(val)
        except ValueError:
            continue
    if "running time" not in vals or "Exploitable for" not in vals:
        return None
    return {"t": vals["running time"], "explo": vals["Exploitable for"],
            "ev_oop": vals.get("EV OOP"), "ev_ip": vals.get("EV IP")}


def parse_floats(line, expect=None):
    vals = [parse_num(t) for t in line.split()]
    if expect is not None and len(vals) != expect:
        raise SolverError(f"expected {expect} floats, got {len(vals)}")
    return vals


def parse_node_block(text):
    lines = [l for l in text.split("\n") if l.strip()]
    d = {"id": lines[0], "type": lines[1] if len(lines) > 1 else "?", "board": "", "pot": [],
         "nchildren": 0, "flags": ""}
    if len(lines) > 2:
        b = lines[2].strip()
        d["board"] = "" if b == "---" else b
    if len(lines) > 3:
        d["pot"] = [parse_num(t) for t in lines[3].split()]
    for l in lines[4:]:
        if l.endswith(" children"):
            try:
                d["nchildren"] = int(l.split()[0])
            except ValueError:
                pass
        elif l.startswith("flags:"):
            d["flags"] = l[len("flags:"):].strip()
    return d


def parse_children(text):
    """show_children output -> list of {index, node-block-dict}."""
    kids = []
    cur = None
    for line in text.split("\n"):
        if line.startswith("child ") and line.rstrip().endswith(":"):
            cur = []
            kids.append(cur)
        elif cur is not None:
            cur.append(line)
    out = []
    for block_lines in kids:
        d = parse_node_block("\n".join(block_lines))
        out.append(d)
    return out


def card_token(s):
    return len(s) == 2 and s[0] in RANKS and s[1] in SUITS


NODE_ID_RE = re.compile(r"[rR][:0-9A-Za-z?]*")


def node_id_arg(query, name="id", default="r:0"):
    """A node id becomes part of a UPI command line; anything outside the Pio id alphabet
    (digits, letters, ':', '?') could inject solver commands, so reject it."""
    v = query.get(name, [default])[0]
    if not NODE_ID_RE.fullmatch(v):
        raise SolverError("bad node id")
    return v


# ---------------------------------------------------------------- the API

_resolver = None  # resolve.ResolveManager, set by main()/tests; owns the 2nd solver


def set_resolver(resolver):
    global _resolver
    _resolver = resolver


def lib_query(query, name="name"):
    """Query-arg wrapper for library endpoints; LibraryError -> 400 via the handler."""
    return query.get(name, [""])[0]


def api(solver, lib, method, path, query, body):
    """Dispatch one API call; returns (status, data)."""
    if path == "/api/resolve" or path.startswith("/api/resolve/"):
        if _resolver is None:
            raise SolverError("re-solve support not initialized")
        return _resolver.api(method, path, query, body)
    if path == "/api/status":
        return 200, solver.state()
    if path == "/api/hand_order":
        return 200, {"order": solver.cmd("show_hand_order").split()}

    # ---- saved ranges and tree configs (#116); state-free, no solver needed ----
    if path == "/api/library/list":
        return 200, {"entries": lib.list(lib_query(query, "kind"))}
    if path == "/api/library/get":
        return 200, {"kind": lib_query(query, "kind"), "name": lib_query(query),
                     "content": lib.get(lib_query(query, "kind"), lib_query(query))}

    if path == "/api/node":
        node_id = node_id_arg(query)
        return 200, {"node": parse_node_block(solver.cmd("show_node " + node_id)),
                     "children": parse_children(solver.cmd("show_children " + node_id))}

    if path == "/api/strategy":
        node_id = node_id_arg(query)
        node = parse_node_block(solver.cmd("show_node " + node_id))
        if node["type"] not in ("OOP_DEC", "IP_DEC"):
            return 200, {"node": node, "children": parse_children(solver.cmd("show_children " + node_id))}
        actor = "OOP" if node["type"] == "OOP_DEC" else "IP"
        kids = parse_children(solver.cmd("show_children " + node_id))
        raw = solver.cmd("show_strategy " + node_id)
        rows = [l for l in raw.split("\n") if l.strip()]
        if any(len(r.split()) != NUM_COMBOS for r in rows):
            # Not a postflop 1326 layout (e.g. a preflop node): hand the raw text to the UI.
            return 200, {"node": node, "children": kids, "actor": actor, "raw": raw}
        return 200, {"node": node, "children": kids, "actor": actor,
                     "freq": [parse_floats(r, NUM_COMBOS) for r in rows],
                     "reach": parse_floats(solver.cmd(f"show_range {actor} " + node_id), NUM_COMBOS),
                     "ev": ev_pair(solver, actor, node_id)}

    if path == "/api/path":
        # Every node from the root to `id`, in one call, so the UI can draw the action line (#112).
        node_id = node_id_arg(query)
        toks = node_id.split(":")
        ids = [":".join(toks[:i]) for i in range(2, len(toks) + 1)] if len(toks) >= 2 else [node_id]
        with solver.cmd_lock:
            nodes = [parse_node_block(solver.cmd("show_node " + i)) for i in ids]
            stack = parse_num(solver.cmd("show_effective_stack").split()[0])
        return 200, {"nodes": nodes, "eff_stack": stack}

    if path == "/api/convergence":
        with solver.state_lock:
            return 200, {"points": list(solver.series), "solving": solver.solving}

    if path == "/api/range":
        node_id = node_id_arg(query)
        return 200, {"oop": parse_floats(solver.cmd("show_range OOP " + node_id), NUM_COMBOS),
                     "ip": parse_floats(solver.cmd("show_range IP " + node_id), NUM_COMBOS)}

    if path == "/api/ev":
        node_id = node_id_arg(query)
        player = query.get("player", ["OOP"])[0].upper()
        if player not in ("OOP", "IP"):
            raise SolverError("player must be OOP or IP")
        return 200, ev_pair(solver, player, node_id)

    if path == "/api/progress":
        st = solver.state()
        if not st["dead"]:
            try:
                st["iters"] = int(solver.cmd("show_iters"))
            except (SolverError, ValueError):
                st["iters"] = None
        return 200, st

    if path == "/api/estimate":
        return 200, {"text": solver.cmd("estimate_tree")}

    if path == "/api/calc_results":
        return 200, {"text": solver.cmd("calc_results")}

    # ---- state-changing endpoints (POST only) ----
    require_post(method)

    if path == "/api/library/save":
        b = need_body(body)
        name = lib.save(b.get("kind"), b.get("name"), b.get("content"))
        return 200, {"name": name}
    if path == "/api/library/rename":
        b = need_body(body)
        lib.rename(b.get("kind"), b.get("from"), b.get("to"))
        return 200, {"name": b.get("to")}
    if path == "/api/library/delete":
        b = need_body(body)
        lib.delete(b.get("kind"), b.get("name"))
        return 200, {"ok": True}

    if path == "/api/generate":
        b = need_body(body)
        return 200, op_generate(solver, b)

    if path == "/api/build":
        solver.cmd("build_tree")
        solver.tree_present = True
        solver.reset_series()
        return 200, {"node_count": solver.cmd("node_count")}

    if path == "/api/solve":
        b = need_body(body)
        acc = b.get("accuracy", 0.05)
        if not isinstance(acc, (int, float)) or acc < 0:
            raise SolverError("bad accuracy")
        # Report (and check the target) every 10 iterations: a smoother convergence chart (#115)
        # and less overshoot past the target than the default 25.
        solver.cmds([f"set_accuracy {acc:g}", "set_info_freq 10", "go"])
        return 200, {"ok": True}

    if path == "/api/stop":
        return 200, {"text": solver.cmd("stop")}

    if path == "/api/save":
        b = need_body(body)
        path_arg = safe_path(b.get("path", ""))
        mode = " no_rivers" if b.get("no_rivers") else " no_turns" if b.get("no_turns") else ""
        solver.cmd(f'dump_tree "{path_arg}"{mode}')
        return 200, {"ok": True}

    if path == "/api/load":
        b = need_body(body)
        path_arg = safe_path(b.get("path", ""))
        solver.cmds(["stop", "wait_for_solver", f'load_tree "{path_arg}"'])
        solver.tree_present = True
        solver.reset_series()
        return 200, {"ok": True}

    if path == "/api/restart":
        solver.restart()
        return 200, {"ok": True}

    return 404, {"error": "unknown endpoint"}


def ev_pair(solver, player, node_id):
    lines = solver.cmd(f"calc_ev {player} {node_id}").split("\n")
    ev = parse_floats(lines[0], NUM_COMBOS)
    matchups = parse_floats(lines[1], NUM_COMBOS) if len(lines) > 1 else None
    return {"ev": ev, "matchups": matchups}


def require_post(method):
    if method != "POST":
        raise SolverError("POST required")


def need_body(body):
    if not isinstance(body, dict):
        raise SolverError("JSON body required")
    return body


def safe_path(p):
    if not isinstance(p, str) or not p or '"' in p or "\n" in p or "\r" in p:
        raise SolverError("bad path")
    return p


def no_newlines(value, name):
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise SolverError(f"bad {name}")
    return value


def op_generate(solver, b):
    """PioViewer-style bet sizes -> add_lines via tools/tree_builder.py, then configure the solver."""
    range_oop = no_newlines(str(b.get("range_oop", "")).strip(), "range_oop")
    range_ip = no_newlines(str(b.get("range_ip", "")).strip(), "range_ip")
    board = no_newlines(str(b.get("board", "")).replace(" ", "").strip(), "board")
    if not range_oop or not range_ip:
        raise SolverError("both ranges are required")
    try:
        pot = int(b["pot"])
        stack = int(b["stack"])
        allin_threshold = float(b.get("allin_threshold", 67))
        add_allin_limit = float(b.get("add_allin_limit", 300))
        if pot < 0 or stack <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SolverError("bad pot/stack/all-in numbers")

    tb = load_tree_builder()

    def sizes(key, allow_mult=True):
        val = no_newlines(str(b.get(key, "")), key).strip()
        if not val:
            return ""
        parts = [p.strip() for p in val.split(",") if p.strip()]
        try:
            specs = tb.parse_size_list(val)
        except ValueError:
            raise SolverError(f"bad sizes in {key} "
                              "(want percentages, geometric Ne, raise multiples Nx, or a)")
        if len(specs) != len(parts):
            raise SolverError(f"bad sizes in {key}")
        for spec in specs:
            if spec.kind == "mult":
                if not allow_mult or spec.value > 1000:
                    raise SolverError(f"bad sizes in {key} (Nx is for raise fields, N <= 1000)")
            elif spec.kind == "geo":
                if spec.value is not None and spec.value > 99:
                    raise SolverError(f"bad sizes in {key} (Ne takes N <= 99)")
            elif spec.kind == "pct" and spec.value > 100000:
                raise SolverError(f"bad sizes in {key}")
        return ",".join(parts)

    def raise_cap(key):
        val = b.get(key)
        if val is None or val == "":
            return None
        try:
            cap = int(val)
            if cap < 0 or cap > 99:
                raise ValueError
        except (TypeError, ValueError):
            raise SolverError(f"bad {key} (want a non-negative integer)")
        return cap

    header = [
        f"#Pot#{pot}",
        f"#EffectiveStacks#{stack}",
        f"#AllinThreshold#{allin_threshold:g}",
        f"#AddAllinOnlyIfLessThanThisTimesThePot#{add_allin_limit:g}",
    ]
    if board:
        header.append(f"#Board#{board}")
    for street, name in (("flop", "Flop"), ("turn", "Turn"), ("river", "River")):
        bet = sizes(f"{street}_bet", allow_mult=False)
        rais = sizes(f"{street}_raise")
        donk = sizes(f"{street}_donk", allow_mult=False)
        allin = bool(b.get(f"{street}_allin"))
        cap = raise_cap(f"{street}_raisecap")
        for player in ("", "IP"):
            if bet:
                header.append(f"#{name}Config{player}.BetSize#{bet}")
            if rais:
                header.append(f"#{name}Config{player}.RaiseSize#{rais}")
            if not player and donk:
                header.append(f"#{name}Config.DonkBetSize#{donk}")
            if allin:
                header.append(f"#{name}Config{player}.AddAllin#True")
            if cap is not None:
                header.append(f"#{name}Config{player}.RaiseCap#{cap}")

    try:
        cfg = tb.parse_config("\n".join(header))
        lines = tb.format_lines(tb.build_lines(cfg))
    except (ValueError, ArithmeticError) as e:
        raise SolverError(f"bad sizes: {e}")
    if not lines:
        raise SolverError("config produced no lines (check bet/raise sizes)")

    # Isomorphism is always on (owner decision, #113); an "iso" field from old clients is ignored.
    cmds = ["stop", "wait_for_solver",
            "set_range OOP " + range_oop,
            "set_range IP " + range_ip]
    if board:
        cmds.append("set_board " + board)
    cmds += [f"set_eff_stack {stack}", f"set_pot 0 0 {pot}",
             "set_isomorphism 1 0", "clear_lines"]
    cmds += lines
    solver.cmds(cmds)
    solver.tree_present = False
    solver.reset_series()
    return {"lines": len(lines), "sample": [l[len("add_line "):] for l in lines[:8]]}


# ---------------------------------------------------------------- HTTP layer

STATIC_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\.(html|js|css|svg)")
STATIC_TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, ctype="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
        else:
            body = payload if isinstance(payload, bytes) else payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if not self._allowed("GET"):
            return
        # Flat files in viewer/static only: the name pattern has no separators or dots beyond the
        # extension, so "..", subdirectories and absolute paths can never match.
        name = "index.html" if path == "/" else path[1:]
        m = STATIC_NAME_RE.fullmatch(name)
        full = os.path.join(STATIC, name)
        if not m or not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES[m.group(1)])

    def _allowed(self, method):
        """Refuse requests a web page on another site could make (#48 follow-up).

        Host must name the loopback address we serve on, which blocks DNS-rebinding pages from
        reading the API. POSTs must be application/json: browsers send that type cross-origin
        only after a CORS preflight, which this server never grants, so another site cannot
        trigger save/load (file writes) or solves with a "simple" text/plain request.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            self._send(403, {"ok": False, "error": "bad Host header"})
            return False
        if method == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(415, {"ok": False, "error": "Content-Type must be application/json"})
                return False
        return True

    def _api(self, method):
        if not self._allowed(method):
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        body = None
        if method == "POST":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            if raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    self._send(400, {"ok": False, "error": "bad JSON"})
                    return
        try:
            status, data = api(self.server.solver, self.server.library, method, parsed.path, query, body)
        except (SolverError, LibraryError) as e:
            status, data = 400, {"ok": False, "error": str(e)}
        except Exception as e:  # keep the server alive; show the broken command's error
            status, data = 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if isinstance(data, dict):
            data.setdefault("ok", status == 200)
        self._send(status, data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            self._api("GET")
        else:
            self._static(path)

    def do_POST(self):
        self._api("POST")

    def do_PUT(self):
        self._send(405, {"ok": False, "error": "method not allowed"})

    def do_DELETE(self):
        self._send(405, {"ok": False, "error": "method not allowed"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", default=os.path.join(REPO, "solver"), help="path to the solver binary")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--library", default=os.path.join("~", ".wsolver", "library"),
                    help="root for saved ranges and tree configs (#116)")
    args = ap.parse_args(argv)

    solver = Solver(os.path.abspath(args.solver))
    import resolve as vresolve  # local import: server/main <-> resolve module cycle
    set_resolver(vresolve.ResolveManager(solver, Solver, os.path.abspath(args.solver)))
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    srv.solver = solver
    srv.library = Library(args.library)
    print(f"WSolver viewer: solver {solver.exe!r}, library {srv.library.root!r}, "
          f"UI on http://127.0.0.1:{srv.server_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        for s in ([solver] + ([_resolver.rsolver] if _resolver else [])):
            try:
                s.proc.kill()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
