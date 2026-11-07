#!/usr/bin/env python3
"""End-to-end UPI tests: drives the solver binary over stdin/stdout like a PioSOLVER client.

Usage: python3 tests/test_upi.py ./solver [--big]

By default the multi-street checks run on a small tree (tens of MB) so `make test` fits in
< 4 GB of RAM. With --big they run on the real 25bb fixture tree instead (~6.4 GB float32,
~3.3 GB with small_strats) — use that on a large-RAM machine.
"""
import math
import os
import platform
import re
import time
import subprocess
import sys
import tempfile
import threading
import queue

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "data", "pio_25bbHU_2sizes.txt")
END = "END"


class Upi:
    def __init__(self, exe):
        self.p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.q = queue.Queue()
        self.solver_msgs = []
        threading.Thread(target=self._pump, daemon=True).start()
        self.send_raw("set_end_string " + END)
        self.read_response()  # banner lines are skipped until the first END

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)

    def send_raw(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _next(self, timeout=600):
        line = self.q.get(timeout=timeout)
        if line is None:
            raise RuntimeError("solver exited")
        return line

    def read_response(self, timeout=600):
        """Read one command response, collecting SOLVER: blocks separately."""
        out = []
        while True:
            line = self._next(timeout)
            if line.startswith("SOLVER:") and not out:
                if line == "SOLVER:":
                    block = []
                    while True:
                        l2 = self._next(timeout)
                        if l2 == END:
                            break
                        block.append(l2)
                    self.solver_msgs.append(block)
                else:
                    self.solver_msgs.append([line])
                continue
            if line == END:
                return "\n".join(out)
            out.append(line)

    def cmd(self, line, timeout=600):
        self.send_raw(line)
        return self.read_response(timeout)

    def close(self):
        try:
            self.send_raw("exit")
            self.p.wait(timeout=30)
        except Exception:
            self.p.kill()


FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def floats(s):
    return [float(x) for x in s.split()]


def hand_order(u):
    return u.cmd("show_hand_order").split()


def load_fixture(u, max_lines=None):
    """Feed the real PioViewer-generated tree script. Returns (range0_text, range1_text, floats0)."""
    r0 = r1 = None
    f0 = None
    n_lines = 0
    with open(FIXTURE) as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("#Range0#"):
                r0 = line[len("#Range0#"):]
            elif line.startswith("#Range1#"):
                r1 = line[len("#Range1#"):]
            if not line or line.startswith("#"):
                continue
            if line.startswith("set_range OOP"):
                f0 = floats(line.split(" ", 2)[2])
            if line.startswith("add_line"):
                n_lines += 1
                if max_lines is not None and n_lines > max_lines:
                    continue
            if line.startswith("build_tree"):
                continue
            resp = u.cmd(line)
            if "ERROR" in resp:
                raise RuntimeError(f"{line[:60]} -> {resp}")
    return r0, r1, f0


# Small default tree: same board/pot as the 25bb fixture, but tiny ranges and 10 lines, so the
# default run exercises build/solve/strategies/isomorphism/locking/save-load/lines in tens of MB.
SMALL_LINES = ["24 230 230", "24 24", "12 36 230 230", "12 12", "0 12 12", "0 12 36 36",
               "0 0", "0 0 20 20", "0 0 0 0 20 20", "0 0 0 0 0 24 24"]


def set_small_tree(u):
    u.cmd("set_board QhQc2c")
    u.cmd("set_range OOP AA,KK,QQ,JJ,TT,AK,AQ,AJs,KQ,QJs,JTs")
    u.cmd("set_range IP AA,KK,QQ,JJ,TT,99,AK,AQ,AJs,KQs")
    u.cmd("set_pot 0 0 40")
    u.cmd("set_eff_stack 230")
    u.cmd("set_isomorphism 1 0")
    u.cmd("clear_lines")
    for l in SMALL_LINES:
        resp = u.cmd("add_line " + l)
        if resp != "add_line ok!":
            raise RuntimeError(f"add_line {l} -> {resp}")


def solve_and_check(u, steps, max_expl):
    u.cmd("set_accuracy 0")
    u.cmd("set_info_freq 50")
    u.send_raw(f"go {steps} steps")
    check(u.read_response() == "go ok!", "go returns immediately")
    u.cmd("wait_for_solver")
    res = u.cmd("calc_results").split("\n")
    evo, evi = float(res[0].split(": ")[1]), float(res[1].split(": ")[1])
    expl = float(res[4].split(": ")[1])
    check(abs(evo + evi - 40) < 0.05, f"EV OOP + EV IP = pot ({evo:.3f} + {evi:.3f})")
    check(expl < max_expl, f"exploitability {expl:.4f} chips")
    check(any(m[0].startswith("SOLVER: stopped") for m in u.solver_msgs), "SOLVER: stopped message")
    check(any(m and m[0].startswith("running time") for m in u.solver_msgs), "SOLVER info block")
    return evo


def check_tree_shape(u, order):
    check(u.cmd("is_tree_present") == "true", "is_tree_present")
    node = u.cmd("show_node r").split("\n")
    check(node[0] == "r" and node[1] == "ROOT" and node[2] == "Qh Qc 2c " and node[3] == "0 0 40 ", "show_node r")
    node = u.cmd("show_node r:0").split("\n")
    check(node[1] == "OOP_DEC" and node[4].endswith("children"), "show_node r:0")
    kids = [k for k in u.cmd("show_children r:0").split("\n\n") if k.strip()]
    names = [k.split("\n")[1].split(":")[-1] for k in kids]
    check(names == ["b24", "b12", "c"], f"root actions in Pio order {names}")


def check_strategies(u, order, evo):
    st = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
    check(len(st) == 3 and all(len(l) == 1326 for l in st), "show_strategy shape")
    live = [i for i in range(1326) if sum(l[i] for l in st) > 0]
    check(all(abs(sum(l[i] for l in st) - 1) < 1e-4 for i in live), "strategies sum to 1")
    board_blocked = [i for i, h in enumerate(order) if any(c in ("Qh", "Qc", "2c") for c in (h[:2], h[2:]))]
    check(all(sum(l[i] for l in st) == 0 for i in board_blocked), "board-blocked hands have 0 strategy")
    ev = u.cmd("calc_ev OOP r:0").split("\n")
    evs, mus = floats(ev[0]), floats(ev[1])
    rng = floats(u.cmd("show_range OOP r:0"))
    # matchups are already weighted by the hand's reach (Pio semantics); EV is nan/inf off-range
    pairs = [(e, m) for e, m in zip(evs, mus) if m > 0]
    tot = sum(e * m for e, m in pairs) / sum(m for _, m in pairs)
    check(abs(tot - evo) < 0.05, f"calc_ev root average {tot:.3f} matches calc_results {evo:.3f}")
    # range after a bet = range * strategy
    rb = floats(u.cmd("show_range OOP r:0:b12"))
    check(all(abs(rb[i] - rng[i] * st[1][i]) < 1e-5 for i in range(1326)), "show_range after action = range * strategy")
    eq = u.cmd("calc_eq_node OOP r:0").split("\n")
    check(len(eq) == 3 and 0.3 < float(eq[2]) < 0.7, f"calc_eq_node total {float(eq[2]):.3f}")


def check_isomorphism(u, order):
    print("[isomorphism: QhQc2c: turn cards of suits d/s are equivalent (board uses h,c)]")
    a = floats(u.cmd("show_strategy r:0:c:c:7d").split("\n")[0])
    b = floats(u.cmd("show_strategy r:0:c:c:7s").split("\n")[0])
    idx = {h: i for i, h in enumerate(order)}

    def swap_ds(h):
        t = {"d": "s", "s": "d"}
        c1 = h[0] + t.get(h[1], h[1])
        c2 = h[2] + t.get(h[3], h[3])
        return idx.get(c1 + c2, idx.get(c2 + c1))

    dev = max(abs(a[i] - b[swap_ds(h)]) for i, h in enumerate(order))
    check(dev < 1e-6, f"7d and 7s turn strategies are suit-swapped copies (max dev {dev})")
    n7s = u.cmd("show_node r:0:c:c:7s").split("\n")
    check(n7s[0] == "r:0:c:c:7s" and n7s[2] == "Qh Qc 2c 7s ", "show_node keeps requested isomorphic card")
    sp = u.cmd("show_children r:0:c:c")
    check(sp.count("child ") == 49, "SPLIT_NODE lists all 49 turn cards")


def check_locking(u):
    print("[locking]")
    before = u.cmd("show_strategy r:0")
    u.cmd("lock_node r:0")
    u.cmd("go 20 steps")
    u.cmd("wait_for_solver")
    after = u.cmd("show_strategy r:0")
    bv, av = floats(before.replace("\n", " ")), floats(after.replace("\n", " "))
    check(max(abs(x - y) for x, y in zip(bv, av)) < 1e-4, "locked node strategy unchanged by solving")
    check("LOCKED" in u.cmd("show_node r:0"), "LOCKED flag")
    u.cmd("unlock_node r:0")
    pure = " ".join(["0"] * 1326 + ["1"] * 1326 + ["0"] * 1326)
    check(u.cmd("set_strategy r:0 " + pure) == "set_strategy ok!", "set_strategy")
    st2 = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
    in_range = [i for i, w in enumerate(floats(u.cmd("show_range OOP r:0"))) if w > 0]
    check(all(abs(st2[1][i] - 1) < 1e-4 for i in in_range), "set_strategy round-trips (in-range hands)")


def check_lock_save_load(u):
    """Locks must survive dump_tree / load_tree (#200 part 1, persistence).

    The flags live in the node records and the combo mask in its own section, so a lock can be lost
    in three distinct ways: the flag, the mask, or the locked strategy itself. A lost flag is the
    dangerous one -- nothing looks wrong until the next solve quietly moves a strategy the user
    pinned, so this also re-solves after the reload and checks the locked combos did not budge.
    """
    print("[locks survive save / load]")
    u.cmd("unlock_node r:0")
    weights = floats(u.cmd("show_range OOP r:0"))
    in_range = [i for i, w in enumerate(weights) if w > 0]
    check(len(in_range) > 30, f"enough in-range combos to lock ({len(in_range)})")
    picked = in_range[::3]
    vec = ["0"] * 1326
    for i in picked:
        vec[i] = "1"
    check(u.cmd("combo_lock_node r:0 " + " ".join(vec)) == "combo_lock_node ok!", "combo_lock_node")
    check("COMBO_LOCKED" in u.cmd("show_node r:0"), "COMBO_LOCKED flag set")
    # Out-of-range combos cannot be locked (they have no local hand), so the readback is exactly the
    # in-range subset we asked for. Checking this before saving separates a mask bug from a save bug.
    got = [i for i, v in enumerate(floats(u.cmd("show_locked_combos r:0"))) if v > 0]
    check(got == picked, f"show_locked_combos returns the combos we locked ({len(got)} vs {len(picked)})")
    combos_before = u.cmd("show_locked_combos r:0")
    rows_before = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "locked.cfr")
        check(u.cmd(f'dump_tree "{path}" lossless') == "dump_tree ok!", "dump_tree with a combo lock")
        u.cmd("free_tree")
        check(u.cmd(f'load_tree "{path}" full') == "load_tree ok!", "load_tree with a combo lock")
        check("COMBO_LOCKED" in u.cmd("show_node r:0"), "COMBO_LOCKED flag survives reload")
        check(u.cmd("show_locked_combos r:0") == combos_before, "locked combo set survives reload")
        rows_mid = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
        dev = max(abs(a - b) for ra, rb in zip(rows_before, rows_mid) for a, b in zip(ra, rb))
        check(dev < 1e-6, f"strategy exact after lossless reload (max dev {dev:.2e})")
        u.cmd("go 30 steps")
        u.cmd("wait_for_solver")
        rows_after = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
        dev_locked = max(abs(rows_before[k][i] - rows_after[k][i])
                         for k in range(len(rows_before)) for i in picked)
        check(dev_locked < 1e-4, f"reloaded lock still holds under solving (max dev {dev_locked:.2e})")
        # Guard against the above becoming vacuous: if those 30 iterations ever stop moving anything
        # (a no-op `go` after a reload, say), the lock check would pass no matter how broken the lock
        # is. The unlocked combos at the same node are the control and must move.
        rest = sorted(set(in_range) - set(picked))
        dev_free = max(abs(rows_before[k][i] - rows_after[k][i])
                       for k in range(len(rows_before)) for i in rest)
        # 3e-4 sits ~3x above the 1e-4 lock tolerance it guards and ~4x below the movement actually
        # observed here (1.3e-3): the tree is well converged by this point, so the control is small.
        check(dev_free > 3e-4,
              f"control: unlocked combos at the same node did move ({dev_free:.2e}), so the lock "
              f"check above is not vacuous")
        # A whole-node lock through the default (quantised) save: the flag is exact, the strategy is
        # only within the compression precision, like every other default-save strategy.
        u.cmd("unlock_node r:0")
        u.cmd("lock_node r:0")
        rows_before = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
        q = os.path.join(td, "locked_q.cfr")
        check(u.cmd(f'dump_tree "{q}"') == "dump_tree ok!", "dump_tree (default) with a node lock")
        u.cmd("free_tree")
        u.cmd(f'load_tree "{q}" full')
        check("LOCKED" in u.cmd("show_node r:0"), "LOCKED flag survives a default (quantised) save")
        rows_after = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
        dev = max(abs(a - b) for ra, rb in zip(rows_before, rows_after) for a, b in zip(ra, rb))
        check(dev < 1.0 / 2047 + 1e-6, f"locked strategy within 11-bit precision (max dev {dev:.2e})")
        u.cmd("unlock_node r:0")


def check_save_load(u, strat_path, deep_paths=(), whole_tree=False):
    print("[save / load]")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "t.cfr")
        check(u.cmd(f'dump_tree "{path}" lossless') == "dump_tree ok!", "dump_tree lossless")
        check(u.cmd(f'check_tree_health "{path}"') == "File OK", "check_tree_health")
        meta = u.cmd(f'show_metadata "{path}"').split("\n")
        check(meta[1] == "ROOT" and meta[2].rstrip() == "Qh Qc 2c", f"show_metadata ({meta[2]!r})")
        s_before = u.cmd("show_strategy " + strat_path)
        deep_before = [u.cmd("show_strategy " + d) for d in deep_paths]
        bad = [d for d, r in zip(deep_paths, deep_before) if r.startswith("ERROR") or len(r.split()) < 10]
        check(not bad, f"deep test nodes exist {bad}")
        all_before = u.cmd("print_all_strats") if whole_tree else None
        u.cmd("free_tree")
        check(u.cmd("is_tree_present") == "false", "free_tree")
        # Default (fast) load is lazy: node data is decompressed per street segment on first access.
        check(u.cmd(f'load_tree "{path}"') == "load_tree ok!", "load_tree")
        check("FAST_LOAD" in u.cmd("show_node r"), "fast load flag")
        deep_after = [u.cmd("show_strategy " + d) for d in reversed(deep_paths)][::-1]
        check(deep_after == deep_before, f"lazy reads of {len(deep_paths)} deep nodes (deepest first) are exact")
        check(u.cmd("show_strategy " + strat_path) == s_before, "strategy identical after reload")
        if whole_tree:
            check(u.cmd("print_all_strats") == all_before, "every strategy identical after lazy load")
            u.cmd("free_tree")
            check(u.cmd(f'load_tree "{path}" full') == "load_tree ok!", "load_tree full")
            check(u.cmd("print_all_strats") == all_before, "every strategy identical after full load")
        # Default saves keep only the displayed strategies, quantised to set_compression_precision bits
        # (11): frequencies within 1/2047 and far smaller. The flop-only workflow: save no_turns, browse,
        # and the later streets are re-solved on demand.
        sizes = {"lossless": os.path.getsize(path)}
        for opt in ("", "no_turns"):
            q = os.path.join(td, f"q{opt}.cfr")
            check(u.cmd(f'dump_tree "{q}" {opt}'.rstrip()) == "dump_tree ok!", f"dump_tree {opt or '(default)'}")
            sizes[opt or "default"] = os.path.getsize(q)
            u.cmd("free_tree")
            u.cmd(f'load_tree "{q}"')
            got = [u.cmd("show_strategy " + d) for d in (strat_path,) + (tuple(deep_paths) if not opt else ())]
            want = [s_before] + (deep_before if not opt else [])
            diff = max(abs(float(x) - float(y)) for a, b in zip(want, got) for x, y in zip(a.split(), b.split()))
            check(diff < 1.0 / 2047 + 1e-6, f"{opt or 'default'} save: strategies within 11-bit precision (max diff {diff:.2e})")
            if opt and deep_paths:
                turn = deep_paths[0]
                r = u.cmd("show_strategy " + turn)
                check(not r.startswith("ERROR") and "ALREADY_RECALCED" in u.cmd("show_node " + turn),
                      f"no_turns save: {turn} re-solved on demand")
            u.cmd("free_tree")
            u.cmd(f'load_tree "{path}" full')
        check(sizes["default"] * 2 < sizes["lossless"] and sizes["no_turns"] * 10 < sizes["default"],
              f"save sizes: lossless {sizes['lossless'] >> 10} KB > default {sizes['default'] >> 10} KB > no_turns {sizes['no_turns'] >> 10} KB")
        small = os.path.join(td, "small.cfr")
        res_before = u.cmd("calc_results")
        u.cmd(f'dump_tree "{small}" no_rivers lossless')
        check(os.path.getsize(small) < os.path.getsize(path), "no_rivers save is smaller")
        if deep_paths:
            u.cmd("free_tree")
            u.cmd(f'load_tree "{small}"')
            flags = u.cmd("show_node r")
            check("INCOMPLETE_TREE" in flags and "FAST_LOAD" in flags, "no_rivers: incomplete + fast-load flags")
            check(u.cmd("show_strategy " + strat_path) == s_before, "no_rivers: lazily loaded strategy exact")
            check(u.cmd("rebuild_tree").startswith("ERROR"), "rebuild_tree needs load_all_nodes")
            # The rivers are unsolved until browsed: calc_results reports the solve behind the save, as
            # Pio does, also after re-saving the truncated tree (#195).
            check(u.cmd("calc_results") == res_before, "no_rivers: calc_results is the saved solve's")
            again = os.path.join(td, "again.cfr")
            u.cmd(f'dump_tree "{again}" no_rivers')
            u.cmd("free_tree")
            u.cmd(f'load_tree "{again}"')
            check(u.cmd("calc_results") == res_before, "re-saved no_rivers: calc_results carried over")
            u.cmd("free_tree")
            u.cmd(f'load_tree "{path}"')
            u.cmd("set_equal_strats")  # must not be undone by later lazy reads of the file
            eq_lazy = [u.cmd("show_strategy " + d) for d in deep_paths]
            u.cmd("free_tree")
            u.cmd(f'load_tree "{path}" full')
            u.cmd("set_equal_strats")
            eq_full = [u.cmd("show_strategy " + d) for d in deep_paths]
            check(eq_lazy == eq_full and eq_lazy[0] != deep_before[0], "set_equal_strats on a lazily loaded tree sticks")
            u.cmd("free_tree")
            u.cmd(f'load_tree "{small}"')
        # The tree must not depend on the temp file any more (it is corrupted, then deleted, below).
        check(u.cmd("load_all_nodes") == "load_all_nodes ok!", "load_all_nodes")
        with open(path, "r+b") as f:
            f.seek(200)
            f.write(b"\xff\xfe\xfd")
        check(u.cmd(f'check_tree_health "{path}"') == "File CORRUPTED", "corruption detected")


def check_lines(u, min_lines):
    print("[lines]")
    lines = u.cmd("show_all_lines").split("\n")
    check(lines[0] == "r" and lines[1] == "r:0" and len(lines) > min_lines, f"show_all_lines ({len(lines)} lines)")
    f_c = float(u.cmd("calc_line_freq r:0:c"))
    f_b1 = float(u.cmd("calc_line_freq r:0:b12"))
    f_b2 = float(u.cmd("calc_line_freq r:0:b24"))
    check(abs(f_c + f_b1 + f_b2 - 1) < 1e-4, f"root action frequencies sum to 1 ({f_c + f_b1 + f_b2:.5f})")
    wins = u.cmd("show_win_evs OOP r:0:c:c:?:c:c:?:c:c").split()
    check(len(wins) == 3 and float(wins[0]) == 40 and float(wins[1]) == 0 and float(wins[2]) == 20,
          f"show_win_evs check-down {wins}")


def run_big_tree(u, order):
    """The real 25bb fixture tree (~6.4 GB float32 / ~3.3 GB small_strats); behind --big."""
    print("[build + solve the real Pio script tree]")
    est = u.cmd("estimate_schematic_tree")
    check("MB" in est, "estimate_schematic_tree")
    # 6.4 GB in float32: pin 16-bit storage so the tree fits 16 GB machines and the save/load round
    # trip below is exact (auto mode could otherwise save float32 and reload compressed, see #40).
    u.cmd("small_strats on")
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree")
    check_tree_shape(u, order)
    u.cmd("set_accuracy 0.2")
    u.cmd("set_info_freq 50")
    u.send_raw("go 300 steps")
    check(u.read_response() == "go ok!", "go returns immediately")
    u.cmd("wait_for_solver")
    res = u.cmd("calc_results").split("\n")
    evo, evi = float(res[0].split(": ")[1]), float(res[1].split(": ")[1])
    expl = float(res[4].split(": ")[1])
    check(abs(evo + evi - 40) < 0.05, f"EV OOP + EV IP = pot ({evo:.3f} + {evi:.3f})")
    check(expl < 0.4, f"exploitability {expl:.4f} chips (< 1% pot)")
    check(any(m[0].startswith("SOLVER: stopped") for m in u.solver_msgs), "SOLVER: stopped message")
    check(any(m and m[0].startswith("running time") for m in u.solver_msgs), "SOLVER info block")

    print("[strategies / ranges / EVs]")
    check_strategies(u, order, evo)
    check_isomorphism(u, order)
    check_locking(u)
    check_save_load(u, "r:0:c:b24")
    check_lock_save_load(u)
    check_lines(u, 100)


def run_small_tree(u, order):
    """Default: the same coverage on a small tree that fits in a few hundred MB."""
    print("[build + solve a small multi-street tree] (25bb fixture skipped; run with --big for it)")
    set_small_tree(u)
    est = u.cmd("estimate_schematic_tree")
    check("MB" in est, f"estimate_schematic_tree ({est.splitlines()[-1]})")
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree")
    check_tree_shape(u, order)
    evo = solve_and_check(u, 400, 0.1)

    print("[strategies / ranges / EVs]")
    check_strategies(u, order, evo)
    check_isomorphism(u, order)
    check_locking(u)
    check_save_load(u, "r:0:c:b12", deep_paths=("r:0:c:c:3d", "r:0:c:b12:c:Ad:c", "r:0:c:c:Ks:c:c:3h", "r:0:b12:c:7d:c:c:9s"),
                    whole_tree=True)
    check_lock_save_load(u)
    check_lines(u, 10)


def check_calc_eq_preflop_exact(u):
    print("[calc_eq_preflop: exact equities match recorded PioSOLVER output]")
    u.cmd("set_range OOP 22+,A2s+,K9s+,Q9s+,J9s+,T9s,ATo+,KJo+")
    u.cmd("set_range IP 22+,A2s+,K5s+,Q8s+,J8s+,T8s+,97s+,87s,76s,A2o+,K9o+,QTo+,JTo")
    r = u.cmd("calc_eq_preflop OOP").split("\n")
    eq = floats(r[0])
    # Recorded Pio output (issue #5): classes AA AKs AKo KK AQs AQo KQo KQs and the total.
    want = [0.850457072, 0.653672099, 0.636298418, 0.760216177,
            0.627568841, 0.608110905, 0.491490602, 0.517899454]
    ok8 = all(abs(a - b) < 1e-6 for a, b in zip(eq[:8], want))
    check(len(eq) == 169 and ok8, f"first 8 class equities match Pio to 1e-6 ({eq[:8]})")
    check(abs(float(r[2]) - 0.550421774) < 1e-6, f"total equity matches Pio ({r[2]})")
    # #125: Pio prints nan, not 0, for classes with no matchups (probe4: all 169 classes).
    toks, mu = r[0].split(), floats(r[1])
    check(all((t == "nan") == (m == 0) for t, m in zip(toks, mu)) and "nan" in toks,
          "classes with 0 matchups print nan, like Pio")
    check(r[0] == " ".join(toks) + " " and r[1].endswith(" "), "equity and matchup lines end in a space, like Pio")
    check(len(r[2].lstrip("0.")) == 9, f"total printed with 9 significant digits like Pio's 0.550421774 ({r[2]})")


def check_output_format(u):
    """Byte-level format details matched against PioSOLVER 3.10.1 (#125)."""
    print("[output format: trailing spaces, %.9g, blank lines (#125)]")
    for c in ("free_tree", "set_board KhQs7d4c2s", "set_range OOP AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s",
              "set_range IP AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s", "set_pot 0 0 100", "set_eff_stack 200",
              "clear_lines", "add_line 75 200", "add_line 0 75 200", "add_line 0 50", "build_tree",
              "set_accuracy 5", "go", "wait_for_solver"):
        u.cmd(c)
    ho = u.cmd("show_hand_order")
    check(ho.endswith("AsAh ") and len(ho.split()) == 1326, "show_hand_order ends in a space")
    ev = u.cmd("calc_ev OOP r:0").split("\n")
    check(len(ev) == 2 and all(l.endswith(" ") and len(l.split()) == 1326 for l in ev),
          "calc_ev: both 1326-value lines end in a space")
    st = u.cmd("show_strategy r:0").split("\n")
    check(all(l.endswith(" ") for l in st), "show_strategy lines end in a space")
    # %.9g of the float: the nearest float to 0.3 prints as 0.300000012 (we used to print 0.3).
    u.cmd("set_range OOP AA:0.3")
    rng = u.cmd("show_range OOP")
    check("0.300000012 " in rng and rng.endswith(" ") and len(rng.split()) == 1326,
          "floats print as %.9g of the float32, like Pio (0.3 -> 0.300000012)")
    for c in ("show_strategy_pp r:0", "calc_ev_pp OOP r:0", "calc_eq_pp OOP", "show_locked_combos r:0"):
        out = u.cmd(c)
        check(out.endswith("\n") and not out.endswith("\n\n"), f"{c.split()[0]} ends with one blank line, like Pio")
    check(u.cmd("set_incentives_node r:0 1 2") == "set_incentives_node ok!", "set_incentives_node")
    inc = u.cmd("show_incentives r:0")
    check(inc == "1.000000 2.000000 ", f"show_incentives prints '%f ' per action like Pio ({inc!r})")
    u.cmd("clear_incentives r:0")


def check_calc_ev_zero_reach(u):
    """calc_ev for an in-range hand that never reaches the node: its counterfactual EV, finite (#247)."""
    print("[calc_ev: in-range hands with zero reach]")
    set_small_tree(u)
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree")
    u.cmd("set_accuracy 0")
    u.cmd("go 60 steps")
    u.cmd("wait_for_solver")
    order = u.cmd("show_hand_order").split()
    kids = re.findall(r"^r:0:(\w+)$", u.cmd("show_children r:0"), re.M)
    check("c" in kids and len(kids) >= 2, f"root actions {kids}")
    rows = [floats(l) for l in u.cmd("show_strategy r:0").split("\n")]
    aa = [i for i, h in enumerate(order) if h[0] == "A" and h[2] == "A"]
    for i in aa:   # OOP checks AA always: AA reaches every bet node with exactly 0
        for k, tok in enumerate(kids):
            rows[k][i] = 1.0 if tok == "c" else 0.0
    u.cmd("set_strategy r:0 " + " ".join(" ".join(f"{x:.6g}" for x in r) for r in rows))
    u.cmd("unlock_node r:0")
    u.cmd("lock_node r:0")
    u.cmd("go 30 steps")
    u.cmd("wait_for_solver")
    bet = "r:0:" + next(t for t in kids if t != "c")
    reach = floats(u.cmd(f"show_range OOP {bet}"))
    ev, mu = (floats(l) for l in u.cmd(f"calc_ev OOP {bet}").split("\n"))
    check(all(reach[i] == 0 and mu[i] == 0 for i in aa), "AA never reaches the bet node (reach and matchups 0)")
    check(all(math.isfinite(ev[i]) and -300 < ev[i] < 300 for i in aa),
          f"AA still gets a finite EV there, like Pio (#247): {[round(ev[i], 2) for i in aa[:3]]}")
    # out of OOP's range (22 without the board's deuce): still cfv / 0 or nan
    out = [i for i, h in enumerate(order) if h[0] == "2" and h[2] == "2" and "2c" not in h]
    check(out and all(not math.isfinite(ev[i]) for i in out), "hands outside the range stay inf / nan")
    u.cmd("unlock_node r:0")
    u.cmd("free_tree")


def check_threads_auto(u):
    """#276: under the default thread count a whole-tree solve times iterations 18-23, alternating every
    hardware thread and half of them, keeps the faster, and says so; results are those of any explicit count."""
    print("[default thread count: tuned per tree]")
    setup = ["set_range OOP AA,KK,QQ,JJ,TT,AK,AQ,KQ,QJs,JTs", "set_range IP AA,KK,QQ,JJ,TT,99,AK,AQ,KQ,KJ,QJ",
             "set_board KhQs7d", "set_pot 0 0 100", "set_eff_stack 150", "clear_lines", "add_line 0 50", "add_line 50 150"]

    def solve(threads):
        u.cmd("free_tree")
        for c in setup:
            u.cmd(c)
        u.cmd(f"set_threads {threads}")
        u.cmd("build_tree")
        u.cmd("set_accuracy 0")
        u.solver_msgs.clear()
        u.cmd("go 30 steps")
        u.cmd("wait_for_solver")
        tuned = [m[0] for m in u.solver_msgs if m and m[0].startswith("SOLVER: threads:")]
        return u.cmd("calc_results"), floats(u.cmd("show_strategy r:0")), tuned, u.cmd("show_cpu_info")

    hw = os.cpu_count() or 1
    res0, s0, tuned0, cpu0 = solve(0)
    res3, s3, tuned3, _ = solve(3)
    if hw >= 4 and platform.machine().lower() not in ("arm64", "aarch64"):
        m = re.match(r"SOLVER: threads: (\d+) ", tuned0[0]) if len(tuned0) == 1 else None
        check(m is not None and int(m.group(1)) in (hw, hw // 2), f"default threads: one tuning line ({tuned0})")
        check(m is not None and f"max threads: {m.group(1)}" in cpu0, "the pool keeps the tuned count")
    check(not tuned3, "an explicit set_threads is not tuned")
    check(res0 == res3 and s0 == s3, "tuned and explicit thread counts give identical results")
    u.cmd("set_threads 0")


def check_schematic_after_build(u):
    """#306: Pio's preflop workflow builds each schematic before attaching it (set_board, set_pot, set_eff_stack,
    lines, build_tree, add_schematic_tree), so the tree in memory is the postflop one by then. The exit is
    resolved in the preflop tree all the same; it used to fail with "incorrect path"."""
    print("[add_schematic_tree after build_tree, Pio's order]")
    for c in ("set_range OOP 22+,A2s+,K9s+,ATo+,KJo+", "set_range IP 22+,A2s+,K5s+,A2o+,K9o+", "set_pot 10 5 0",
              "set_eff_stack 1000", "clear_preflop_lines", "add_preflop_line 25 25", "add_preflop_line 25 100 100"):
        u.cmd(c)
    check(u.cmd("build_preflop_tree") == "build_preflop_tree ok!", "build_preflop_tree")
    u.cmd("reset_subset")
    u.cmd("add_to_subset 1 As Ks Qs")
    for c in ("set_board AsKsQs", "set_pot 0 0 50", "set_eff_stack 975", "clear_lines", "add_line 0 25 25"):
        u.cmd(c)
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree of the schematic")
    check(u.cmd("add_schematic_tree r:0:b25:c") == "add_schematic_tree ok!", "add_schematic_tree after build_tree (#306)")
    check(u.cmd("add_schematic_tree r:0:b99:c").startswith("ERROR"), "a path that is not a preflop exit is refused")
    for c in ("set_pot 0 0 200", "set_eff_stack 900", "clear_lines", "add_line 0 100 100"):  # the 3-bet pot's exit
        u.cmd(c)
    u.cmd("build_tree")
    check(u.cmd("add_schematic_tree r:0:b25:b100:c") == "add_schematic_tree ok!", "a second schematic, also after build_tree")
    for c in ("set_pot 0 0 20", "set_eff_stack 990", "clear_lines", "add_line 0 10 10"):  # the limped pot's exit
        u.cmd(c)
    u.cmd("build_tree")
    check(u.cmd("add_schematic_tree r:0:c:c") == "add_schematic_tree ok!", "the limped pot's schematic")
    check(u.cmd("add_all_flops") == "add_all_flops ok!", "add_all_flops")
    check("r:0:b25:c:AsKsQs" in u.cmd("show_children r:0:b25:c"), "the flop hangs under the exit")
    check(u.cmd("show_node r:0:b25:c:AsKsQs").splitlines()[3] == "0 0 50 ", "with the schematic's pot")
    # Like Pio, add_all_flops accepts exits without a schematic: they stay pure preflop exits (exact equity).
    check(u.cmd("build_preflop_tree") == "build_preflop_tree ok!", "build_preflop_tree again (schematics cleared)")
    for c in ("set_pot 0 0 50", "set_eff_stack 975", "clear_lines", "add_line 25", "add_line 0 25"):
        u.cmd(c)
    check(u.cmd("add_schematic_tree r:0:b25:c") == "add_schematic_tree ok!", "one schematic only")
    check(u.cmd("add_all_flops") == "add_all_flops ok!", "add_all_flops with exits left without a schematic")
    check(u.cmd("show_node r:0:c:c").splitlines()[1] == "END_NODE" and
          "r:0:b25:c:AsKsQs" in u.cmd("show_children r:0:b25:c"), "those exits stay END_NODE; the attached one deals flops")
    res = u.cmd("calc_results")
    check("nan" not in res and "inf" not in res, "and the tree evaluates")
    u.cmd("reset_subset")
    u.cmd("free_tree")


def check_iso_symmetry(u):
    """#300: on a board with a suit symmetry (4c2d6c: hearts and spades are interchangeable), suit-swapped hands
    stay equal and EV OOP + EV IP stays the pot. With a flop all-in, rounding asymmetry used to grow into
    different strategies (KsQs 0.246 vs KhQh 1.000) and an EV sum of 60.311 on a 60 pot."""
    print("[isomorphism keeps suit-symmetric hands symmetric]")
    for c in ("set_range OOP AQo,KK,KQs", "set_range IP TT,KQo", "set_board 4c2d6c", "set_pot 0 0 60", "set_eff_stack 10",
              "clear_lines", "add_line 10", "add_line 0 10"):
        u.cmd(c)
    u.cmd("build_tree")
    u.cmd("set_accuracy 0")
    u.cmd("go 100 steps")
    u.cmd("wait_for_solver")
    res = u.cmd("calc_results").splitlines()
    ev = float(res[0].split(": ")[1]) + float(res[1].split(": ")[1])
    check(abs(ev - 60) < 1e-3, f"EV OOP + EV IP = pot on a symmetric flop with an all-in ({ev:.3f})")
    pp = u.cmd("show_strategy_pp r:0")
    row = lambda c: re.search(r"^" + c + r":\s+(.*)$", pp, re.M).group(1).split()
    check(row("KsQs") == row("KhQh") and row("KsKc") == row("KhKc"), "suit-swapped hands get the same strategy")


def check_union_hands(u):
    """set_union_hands: opponent-only hands get a slot, a signed inf and _pp rows (#9)."""
    print("[set_union_hands: hands of either range]")
    # IP holds 88, OOP does not, and the board blocks none of it: exactly 6 opponent-only
    # combos for OOP. Nothing else differs between the two ranges in OOP's direction.
    setup = [
        "set_range OOP AA:0.5,KK,QQ:0.25,JJ,TT,99,AK:0.75,AQ,AJ,KQ,KJ,QJ,JT,T9",
        "set_range IP AA,KK,QQ,JJ,TT,99,88,AK,AQ:0.5,KQ,QJ,JT",
        "set_board Kh9h4h",
        "set_pot 0 0 60",
        "set_eff_stack 200",
        "clear_lines ",
        "add_line 20 20 60 60 200",
        "add_line 0 20 20 60 60",
        "add_line 0 0 40",
        "add_line 20 200",
    ]

    def solve(union):
        u.cmd("free_tree")
        for c in setup:
            u.cmd(c)
        check(u.cmd("set_union_hands " + ("on" if union else "off")) == "set_union_hands ok!",
              f"set_union_hands {'on' if union else 'off'} accepted")
        u.cmd("build_tree")
        u.cmd("set_accuracy 5")
        u.cmd("go")
        u.cmd("wait_for_solver")
        res = u.cmd("calc_results").split("\n")
        combos = lambda out: [l.split(":")[0] for l in out.split("\n") if re.match(r"^[2-9TJQKA][cdhs][2-9TJQKA][cdhs]:", l)]
        return {
            "evs": (res[0], res[1], res[4]),
            "ev_pp": combos(u.cmd("calc_ev_pp OOP r:0")),
            "ev_pp_ip": combos(u.cmd("calc_ev_pp IP r:0")),
            "ev_pp_lines": len(u.cmd("calc_ev_pp OOP r:0").rstrip("\n").split("\n")),  # #125: + a blank line
            "strat_pp": combos(u.cmd("show_strategy_pp r:0")),
            "strat": u.cmd("show_strategy r:0"),
            "deep": u.cmd("calc_ev OOP r:0:b20:b200").split("\n")[0].split(),
        }

    off, on = solve(False), solve(True)

    check(u.cmd("set_union_hands maybe").startswith("ERROR"), "set_union_hands rejects a bad argument")
    s_on, s_off = (floats(x["strat"].replace("\n", " ")) for x in (on, off))
    zeros = [i for i, v in enumerate(s_off) if v == 0]
    check(all(s_on[i] == 0 for i in zeros) and max(abs(a - b) for a, b in zip(s_on, s_off)) < 0.05,
          "show_strategy prints opponent-only hands as 0 either way, as Pio does (probe 12)")

    added = sorted(set(on["ev_pp"]) - set(off["ev_pp"]))
    check(len(off["ev_pp"]) == 142 and len(on["ev_pp"]) == 148,
          f"calc_ev_pp lists the union ({len(off['ev_pp'])} -> {len(on['ev_pp'])}, expected 142 -> 148)")
    check(added == ["8d8c", "8h8c", "8h8d", "8s8c", "8s8d", "8s8h"],
          f"the added hands are exactly the six 88 combos ({added})")
    check(len(on["strat_pp"]) == 148 and sorted(set(on["strat_pp"]) - set(off["strat_pp"])) == added,
          "show_strategy_pp lists the same opponent-only hands")

    # #61: recorded PioSOLVER 3.10.1 output on this exact tree. Pio always lists the union, for
    # both players: an "EV: <x>" header plus 148 rows, then a blank line (#125) - the "150 lines" of
    # #9. With set_union_hands on we list the same hands; only the order among equal-EV
    # (suit-isomorphic) combos may differ, which depends on last-bit float noise.
    with open(os.path.join(HERE, "data", "pio_probe2_calc_ev_pp.txt")) as f:
        blocks = [b for b in f.read().split("END\n") if b.strip()]
    pio = [[l.split(":")[0] for l in b.split("\n") if re.match(r"^[2-9TJQKA][cdhs][2-9TJQKA][cdhs]:", l)]
           for b in blocks]
    check(len(pio) == 2 and len(pio[0]) == 148 and len(pio[1]) == 148, "recorded Pio calc_ev_pp: 148 rows per player")
    check(sorted(on["ev_pp"]) == sorted(pio[0]), "calc_ev_pp OOP with union_hands lists exactly Pio's hands")
    check(sorted(on["ev_pp_ip"]) == sorted(pio[1]), "calc_ev_pp IP with union_hands lists exactly Pio's hands")
    check(on["ev_pp_lines"] == 149, f"calc_ev_pp is an EV header + 148 rows like Pio ({on['ev_pp_lines']} non-blank lines)")

    # Equilibrium must not move: the extra hands enter with reach 0.
    check(off["evs"] == on["evs"], f"equilibrium unchanged by union_hands ({off['evs']} vs {on['evs']})")

    # Deep node where OOP has 20 in and faces an all-in: the opponent-only hands have a
    # negative counterfactual value, so Pio's signed -inf rather than a bare inf.
    # Look at exactly the six 88 combos, the opponent-only hands: OOP's own hands that never
    # reach the node print their counterfactual EV instead (#247).
    order = u.cmd("show_hand_order").split()
    idx88 = [i for i, c in enumerate(order) if c[0] == "8" and c[2] == "8"]
    n_off = sorted(off["deep"][i] for i in idx88)
    n_on = sorted(on["deep"][i] for i in idx88)
    check(n_off == ["inf"] * 6, f"without union_hands the six hands print unsigned inf ({n_off[:2]}...)")
    check(n_on == ["-inf"] * 6, f"with union_hands they print -inf, the sign of their cfv ({n_on[:2]}...)")

    # #302: a save of a union-hands tree records the setting, so it loads (the hand lists, and the storage,
    # depend on it), even with the session's setting off; it used to fail with "tree shape mismatch".
    res = u.cmd("calc_results")
    strat = u.cmd("show_strategy r:0")
    with tempfile.TemporaryDirectory() as td:
        for mode in ("lossless", ""):
            path = os.path.join(td, f"union{mode}.cfr")
            check(u.cmd(f'dump_tree "{path}" {mode}'.strip()) == "dump_tree ok!", f"dump_tree {mode or 'default'} of a union-hands tree")
            u.cmd("set_union_hands off")
            check(u.cmd(f'load_tree "{path}"') == "load_tree ok!", f"a {mode or 'default'} union-hands save loads (#302)")
            if mode == "lossless":
                check(u.cmd("calc_results") == res and u.cmd("show_strategy r:0") == strat,
                      "lossless union-hands round trip: identical results and strategy")
            u.cmd("set_union_hands on")

    u.cmd("set_union_hands off")
    u.cmd("free_tree")


def check_force_line(u):
    """force_line keeps only the lines the forced one is a prefix of; the line must exist (Pio 3.10.1,
    probe 11 and the cases below, each run on Pio with the same children)."""
    print("[force_line]")
    u.cmd("set_range OOP AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s")
    u.cmd("set_range IP AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s")
    u.cmd("set_board KhQs7d4c2s")
    u.cmd("set_pot 0 0 100")
    u.cmd("set_eff_stack 200")

    def kids(node):
        return " ".join(sorted(i.rsplit(":", 1)[1] for i in re.findall(r"^(r:\S+)$", u.cmd("show_children " + node), re.M)))

    def tree(forces, lines=("0 50", "0 50 150", "75")):
        u.cmd("free_tree")
        u.cmd("clear_lines")
        for l in lines:
            u.cmd("add_line " + l)
        out = [u.cmd("force_line " + f) for f in forces]
        check(u.cmd("build_tree") == "build_tree ok!", f"build_tree after force_line {forces}")
        return out

    tree([])
    check(kids("r:0") == "b75 c" and kids("r:0:c:b50") == "b150 c f", "no force: the whole tree")
    tree(["0 50"])
    check(kids("r:0") == "c" and kids("r:0:c") == "b50" and kids("r:0:c:b50") == "b150 c f",
          "force_line 0 50: only that path down to it, everything below it")
    tree(["0 50 150"])
    check(kids("r:0:c:b50") == "b150" and kids("r:0:c:b50:b150") == "c f", "force_line 0 50 150")
    out = tree(["0", "0 50 50"])
    check(out == ["force_line ok!"] * 2 and kids("r:0:c") == "b50" and kids("r:0:c:b50") == "c",
          "an implicit call can be forced, and a longer force applies below a shorter one")
    out = tree(["0 50 150 200", "0 50", "75"])
    check(out == ["ERROR: force_line Specified line doesn't exists.", "force_line ok!",
                  "ERROR: force_line Specified line doesn't exists."],
          f"a line the tree lacks, or one an earlier force removed, is refused ({out})")
    check(kids("r:0") == "c" and kids("r:0:c:b50") == "b150 c f", "a refused force changes nothing")

    # remove_line acts at once on the live tree, like Pio (same responses and children on 3.10.1)
    u.cmd("free_tree")
    u.cmd("clear_lines")
    for l in ("0 50", "0 50 150", "75"):
        u.cmd("add_line " + l)
    err = "ERROR: remove_line Can't remove the line (it doesn't exist or is the last child)"
    out = [u.cmd("remove_line " + l) for l in ("0 60", "0 50 0", "0 50 0", "0 50 150", "0 50 150")]
    check(out == [err, "remove_line ok!", err, "remove_line ok!", err],
          f"remove_line: a missing line, or one already removed, is refused ({out})")
    u.cmd("build_tree")
    check(kids("r:0:c") == "b50 c" and kids("r:0:c:b50") == "c", "the fold and the raise are gone")
    check(u.cmd("remove_line 0 50 50").startswith("ERROR"), "the last child can't be removed")
    # a bad line kept by add_line (Incorrect bet size) leaves no buildable tree: remove_line still
    # refuses a line on no added line, and can take the bad one out (#257)
    u.cmd("free_tree")
    u.cmd("clear_lines")
    bad = [u.cmd("add_line " + l) for l in ("0 50", "75 30")]
    check(bad[1] == "ERROR: add_line Incorrect bet size.", f"a raise below the bet is kept with an error ({bad})")
    out = [u.cmd("remove_line " + l) for l in ("0 60", "0 50 0", "75 30")]
    check(out == [err, "remove_line ok!", "remove_line ok!"],
          f"remove_line with a bad line pending: on an added line, or its implicit fold ({out})")
    check(u.cmd("build_tree") == "build_tree ok!" and kids("r:0") == "c" and kids("r:0:c:b50") == "c",
          "the bad line removed, the tree builds, without the fold")
    u.cmd("free_tree")
    u.cmd("clear_lines")
    out = [u.cmd(c) for c in ("add_line 0 50", "add_line 75 30", "force_line 0 50 50", "force_line 0 60")]
    check(out[2:] == ["force_line ok!", "ERROR: force_line Specified line doesn't exists."],
          f"force_line with no tree: an implicit call on an added line, not a missing line ({out[2:]})")
    u.cmd("clear_lines")
    # removing deletes the subtree: re-adding the line brings back only its own path
    u.cmd("free_tree")
    u.cmd("clear_lines")
    for c in ("add_line 0 50", "add_line 0 50 150", "remove_line 0 50", "add_line 0 50"):
        u.cmd(c)
    u.cmd("build_tree")
    check(kids("r:0:c") == "b50 c" and kids("r:0:c:b50") == "c f", "re-added after remove_line: its path, not 0 50 150")
    # a line added after force_line goes in beside the forced one
    u.cmd("free_tree")
    u.cmd("clear_lines")
    for c in ("add_line 0 50", "force_line 0 50", "add_line 75"):
        u.cmd(c)
    u.cmd("build_tree")
    check(kids("r:0") == "b75 c" and kids("r:0:c") == "b50", "add_line after force_line is added beside it")
    u.cmd("free_tree")
    u.cmd("clear_lines")


def check_global_freq(u):
    """calc_global_freq counts the dealt cards' card removal, as Pio (probe 13: an unsolved tree, so both
    solvers hold the same uniform strategies)."""
    print("[calc_global_freq: card removal of the dealt cards]")
    for c in ("set_range OOP AA:0.5,KK,QQ:0.25,JJ,TT,99,AK:0.75,AQ,AJ,KQ,KJ,QJ,JT,T9",
              "set_range IP AA,KK,QQ,JJ,TT,99,88,AK,AQ:0.5,KQ,QJ,JT", "set_board Kh9h4h", "set_pot 0 0 60",
              "set_eff_stack 200", "clear_lines", "add_line 20 20 60 60 200", "add_line 0 20 20 60 60",
              "add_line 0 0 40", "add_line 20 200", "build_tree"):
        u.cmd(c)
    g1 = float(u.cmd("calc_global_freq r:0:c:c:2c:c"))
    g2 = float(u.cmd("calc_global_freq r:0:b20:c:As:c:c:3d"))
    check(abs(g1 - 0.125) < 1e-6, f"a 2c turn blocks nothing: 1/8 from the strategies, Pio 0.125 ({g1})")
    check(abs(g2 - 0.0692357495) < 1e-5, f"an As turn blocks AA/AK/AQ/AJ combos: Pio 0.0692357495 ({g2})")
    u.cmd("free_tree")


def check_scripts(u):
    """load_script(_silent), skip_if_done, LABEL: and stdoutredi as Pio 3.10.1 answers them (#251)."""
    print("[load_script / load_script_silent / skip_if_done / stdoutredi]")
    td = tempfile.mkdtemp(prefix="wsolver-scripts-")
    s2 = os.path.join(td, "s2.txt")
    with open(s2, "w") as f:
        f.write("is_ready\nset_eff_stack 11\nLABEL: a\nshow_node r\nset_eff_stack 22\nis_ready\n")
    u.cmd("free_tree")
    u.send_raw("load_script " + s2)
    got = [u.read_response() for _ in range(5)]
    check(got == ["is_ready ok!", "set_eff_stack ok!", "", "ERROR: show_node missing/incorrect tree", "load_script ok!"],
          f"load_script: each line answers, LABEL: answers empty, the first ERROR ends it, then its ok ({got})")
    got = u.cmd("load_script_silent " + s2)
    check(got == "is_ready ok!\nset_eff_stack ok!\nERROR: show_node missing/incorrect tree\nload_script_silent ok!",
          f"load_script_silent: the same answers without end strings, in one response ({got!r})")
    s5, s6 = os.path.join(td, "s5.txt"), os.path.join(td, "s6.txt")
    with open(s6, "w") as f:
        f.write("set_eff_stack 7\n")
    with open(s5, "w") as f:
        f.write(f"is_ready\nload_script {s6}\nis_ready\n")
    got = u.cmd("load_script_silent " + s5)
    check(got == "is_ready ok!\nset_eff_stack ok!\nload_script ok!\nis_ready ok!\nload_script_silent ok!",
          f"a script nested in load_script_silent is silent too: one response ({got!r})")
    got = u.cmd("is_ready")
    check(got == "is_ready ok!", f"the next command's answer is its own ({got!r})")
    s7, s8 = os.path.join(td, "s7.txt"), os.path.join(td, "s8.txt")
    with open(s7, "w") as f:
        f.write("show_node r\nset_eff_stack 9\n")
    with open(s8, "w") as f:
        f.write(f"load_script {s7}\nis_ready\n")
    u.cmd("free_tree")
    u.send_raw("load_script " + s8)
    got = [u.read_response() for _ in range(4)]
    check(got == ["ERROR: show_node missing/incorrect tree", "load_script ok!", "is_ready ok!", "load_script ok!"],
          f"an ERROR in a nested script ends only that one; the outer goes on ({got})")
    check(u.cmd("is_ready") == "is_ready ok!", "and the stream is in step after it")
    s4 = os.path.join(td, "s4.txt")
    with open(s4, "w") as f:
        f.write(f"skip_if_done {s4} here\nis_ready\nLABEL: here\nset_eff_stack 5\n")
    u.send_raw("load_script " + s4)
    got = [u.read_response() for _ in range(4)]
    check(got == ["skip_if_done ok!", "", "set_eff_stack ok!", "load_script ok!"],
          f"skip_if_done skips to the label, which answers empty ({got})")
    nope = os.path.join(td, "nope.txt")
    check(u.cmd("load_script " + nope) == f"ERROR: load_script couldn't open {nope}: No such file or directory",
          "a missing script: Pio's message")
    out = os.path.join(td, "out.txt")
    u.send_raw("stdoutredi " + out)
    u.send_raw("is_ready")
    check(u.cmd("stdoutback") == "stdoutback ok!", "stdoutback answers on stdout")
    with open(out) as f:
        text = f.read()
    check(text == "stdoutredi ok!\nEND\nis_ready ok!\nEND\n", f"stdoutredi answers into the file, like Pio ({text!r})")


def check_go_partial(u):
    """go_partial (#153): asynchronous subtree solves, like `go` but below a node."""
    print("[go_partial: asynchronous subtree solving]")
    set_small_tree(u)
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree for go_partial")
    u.cmd("set_accuracy 0")
    u.cmd("go 200 steps")
    u.cmd("wait_for_solver")
    root_strat = u.cmd("show_strategy r:0")
    child_strat = u.cmd("show_strategy r:0:c")

    # A step-limited subtree solve: progress blocks are marked, DCFR clock restarts,
    # strategies at and below the node change, everything above is bit-identical.
    u.solver_msgs.clear()
    u.cmd("set_info_freq 10")
    check(u.cmd("go_partial r:0:c 100 steps") == "go_partial ok!", "go_partial returns immediately")
    u.cmd("wait_for_solver")
    blocks = [b for b in u.solver_msgs if b and b[0].startswith("partial: ")]
    check(blocks and all(b[0] == "partial: r:0:c" for b in blocks), f"progress blocks marked partial: r:0:c ({len(blocks)})")
    text = "\n".join(blocks[-1]) if blocks else ""
    check("Exploitable for:" in text and "EV OOP:" in text, "partial block has the stats block")
    check(any(b and b[0].startswith("SOLVER: stopped (required number of steps done") for b in u.solver_msgs),
          "stopped (required number of steps done)")
    check(u.cmd("show_strategy r:0") == root_strat, "strategy above the node is untouched")
    check(u.cmd("show_strategy r:0:c") != child_strat, "the subtree's strategy changed")

    # An accuracy-driven subtree solve stops on the subtree exploitability target.
    u.solver_msgs.clear()
    u.cmd("set_accuracy 0.02")
    check(u.cmd("go_partial r:0:c") == "go_partial ok!", "go_partial with an accuracy target")
    u.cmd("wait_for_solver")
    blocks = [b for b in u.solver_msgs if b and b[0].startswith("partial: ")]
    m = re.search(r"Exploitable for: ([-\d.eE+]+)", "\n".join(blocks[-1])) if blocks else None
    # the check runs at the report that crossed the target: printed value can round up by 0.0005
    check(m is not None and float(m.group(1)) <= 0.0205, f"stops at the target ({m and m.group(1)} <= 0.02)")
    check(any(b and b[0].startswith("SOLVER: stopped (required accuracy reached") for b in u.solver_msgs),
          "stopped (required accuracy reached)")
    check(u.cmd("show_strategy r:0") == root_strat, "strategy above the node still untouched")

    # A pending hold (the viewer's browse lease) must not park a subtree solve it never saw start
    # (#241 review): every way a solve starts drops the hold, as go does.
    check(u.cmd("hold") == "hold ok!", "hold with no solve running")
    u.solver_msgs.clear()
    check(u.cmd("go_partial r:0:c 20 steps") == "go_partial ok!", "go_partial after a hold")
    stopped = lambda: any(b and b[0].startswith("SOLVER: stopped") for b in list(u.solver_msgs))
    deadline = time.time() + 20   # no wait_for_solver here: it would drop the hold itself
    while time.time() < deadline and not stopped():
        u.cmd("echo")   # the harness collects SOLVER: messages while it reads a response
        time.sleep(0.02)
    check(stopped(), "go_partial runs although a hold was pending")
    u.cmd("wait_for_solver")

    check(u.cmd("go_partial").startswith("ERROR"), "go_partial needs an argument")
    check(u.cmd("go_partial r:0:bogus").startswith("ERROR"), "go_partial on a bad node errors")
    check(u.cmd("go_partial r:0:c 2 eons").startswith("ERROR"), "go_partial on a bad unit errors")
    u.cmd("free_tree")


def check_go_limits_and_errors(u):
    """Pio 3.10 behavior, probed black-box: go limits below one iteration still run one, the unit is
    required, timed stops say "timeout reached", bad players say "incorrect player", and a load_tree
    that cannot open its file drops the current tree."""
    print("[go limits, player errors, failed load_tree]")
    set_small_tree(u)
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree")
    u.cmd("set_accuracy 0")

    def run(cmd):
        before = int(u.cmd("show_iters"))
        u.solver_msgs.clear()
        resp = u.cmd(cmd)
        u.cmd("wait_for_solver")
        stops = [b[0] for b in u.solver_msgs if b and b[0].startswith("SOLVER: stopped")]
        return resp, int(u.cmd("show_iters")) - before, stops

    for cmd, why in (("go 0 steps", "required number of steps done"), ("go -5 steps", "required number of steps done"),
                     ("go 1.7 steps", "required number of steps done"), ("go 0 seconds", "timeout reached")):
        resp, n, stops = run(cmd)
        check(resp == "go ok!" and n == 1 and stops == [f"SOLVER: stopped ({why})"],
              f"{cmd}: one iteration, stopped ({why}) [{resp!r}, {n}, {stops}]")
    resp, n, _ = run("go 3")
    check(resp == "ERROR: go incorrect or missing argument" and n == 0, "go without a unit errors (Pio)")
    for cmd in ("calc_ev XYZ r:0", "show_range XYZ", "set_range XYZ 0.5", "calc_eq XYZ", "show_nuts XYZ"):
        name = cmd.split()[0]
        check(u.cmd(cmd) == f"ERROR: {name} incorrect player", f"{cmd}: incorrect player")
    check(u.cmd("set_first_iteration_player XYZ") == "ERROR: set_first_iteration_player incorrect or missing argument",
          "set_first_iteration_player keeps the generic error (Pio)")
    missing = os.path.join(tempfile.gettempdir(), "wsolver_no_such_dir", "x.cfr")
    r = u.cmd("load_tree " + missing)
    check(r.startswith(f"ERROR: load_tree couldn't open {missing}: "), f"load_tree error text ({r})")
    check(u.cmd("is_tree_present") == "false", "a failed load_tree drops the current tree (Pio)")
    u.cmd("set_accuracy 0.005")


def check_info_freq_auto(u):
    """set_info_freq auto: adaptive report schedule; stops on the target with fewer checks."""
    print("[set_info_freq auto]")
    set_small_tree(u)
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree for info_freq auto")
    check(u.cmd("set_info_freq auto") == "set_info_freq ok!", "set_info_freq auto accepted")
    u.cmd("set_accuracy 0.005")
    u.solver_msgs.clear()
    u.cmd("go")
    u.cmd("wait_for_solver")
    blocks = [b for b in u.solver_msgs if b and any(l.startswith("Exploitable for:") for l in b)]
    m = re.search(r"Exploitable for: ([-\d.eE+]+)", "\n".join(blocks[-1])) if blocks else None
    check(m is not None and float(m.group(1)) <= 0.0055, f"auto stops at the target ({m and m.group(1)} <= 0.005)")
    check(any(b and b[0].startswith("SOLVER: stopped (required accuracy reached") for b in u.solver_msgs),
          "auto: stopped (required accuracy reached)")
    iters = int(u.cmd("show_iters").split()[-1])
    check(len(blocks) < max(3, iters // 10), f"auto checks less often than every 10 ({len(blocks)} reports, {iters} iterations)")
    check(u.cmd("set_info_freq auto2").startswith("ERROR"), "set_info_freq rejects other words")
    u.solver_msgs.clear()
    u.cmd("set_accuracy 0")
    u.cmd("go 30 steps")
    u.cmd("wait_for_solver")
    n_auto = len([b for b in u.solver_msgs if b and any(l.startswith("Exploitable for:") for l in b)])
    check(n_auto < 10, f"a rejected set_info_freq leaves auto on ({n_auto} reports in 30 steps)")
    u.cmd("set_info_freq 25")
    u.cmd("free_tree")


def check_tree_construction_edges(exe):
    """#371: preflop calc_eq, a decision left with no actions, chip amounts out of range, an all-zero subset."""
    print("[tree construction edges]")

    def run(lines):
        p = subprocess.run([exe], input="\n".join(lines + ["exit"]) + "\n", capture_output=True, text=True, timeout=600)
        return p.stdout

    out = run(["set_range OOP KK", "set_range IP AA", "set_pot 10 5 0", "set_eff_stack 100", "add_preflop_line 100",
               "build_preflop_tree", "calc_eq_node OOP r:0:b100:c"])
    tail = [l for l in out.splitlines() if l.strip()]
    tot = float(tail[-1]) if tail and re.fullmatch(r"[0-9.]+", tail[-1]) else None
    vals = {v for l in tail[-3:-1] for v in l.split() if v not in ("nan", "0")}
    check(tot is not None and abs(tot - 0.1805) < 0.003 and vals and all(abs(float(v) - 0.1805) < 0.003 for v in tail[-3].split()
                                                                     if v != "nan"),
          f"preflop calc_eq_node: KK vs AA is about 0.18 (got {tot}; was 0.075 from an unset board)")
    out = run(["add_line 0 50", "remove_line 0 50 50", "remove_line 0 50 0", "set_board AsKd7c2h3s",
               "set_range OOP AA,KK,QQ", "set_range IP AA,KK,JJ", "set_pot 0 0 100", "set_eff_stack 200", "build_tree"])
    check("ERROR: build_tree the removed lines leave a decision with no actions" in out,
          "build_tree refuses a decision the removed lines left without actions")
    out = run(["set_eff_stack 3000000000", "set_pot 1500000000 1500000000 0", "add_line 50 4294967346"])
    check(out.count("incorrect or missing argument") == 3, "chip amounts out of range are refused, not wrapped")
    out = run(["set_range OOP QQ+,AKs", "set_range IP QQ+,AKs,JTs", "set_pot 10 5 0", "set_eff_stack 100",
               "add_preflop_line 25 25", "build_preflop_tree", "add_to_subset 0 As Ks Qs", "set_board AsKsQs",
               "set_pot 0 0 50", "set_eff_stack 75", "add_line 25", "add_schematic_tree 25 25", "add_all_flops"])
    check("the flop subset's weights are all 0" in out, "add_all_flops refuses an all-zero subset")
    out = run(["set_range OOP QQ+,AKs", "set_range IP QQ+,AKs,JTs", "set_pot 10 5 0", "set_eff_stack 100",
               "add_preflop_line 25 25", "build_preflop_tree", "add_to_subset 1 As Ks Qs", "set_board AsKsQs7d",
               "set_pot 0 0 50", "set_eff_stack 75", "add_line 25", "add_schematic_tree r:0:b25:c"])
    check("ERROR: add_schematic_tree a schematic is a flop tree: set a 3-card board" in out,
          "add_schematic_tree refuses a turn board, whose lines would be read as flop lines (#396)")


def check_core_review(exe):
    """#380: 16-bit combo-locked rows hold their values; pruning periods below 2 are refused; set_mes
    plays the best response for the unlocked hands of a combo-locked node."""
    print("[solver core: locks under 16-bit, pruning period, set_mes]")
    setup = ["set_threads 4", "set_range OOP AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s",
             "set_range IP AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s", "set_board KhQs7d4c", "set_pot 0 0 100",
             "set_eff_stack 200", "add_line 50 150 200", "add_line 0 50 150 200"]
    mask = " ".join("1" if i % 2 == 0 else "0" for i in range(1326))

    def run(lines):
        p = subprocess.run([exe], input="\n".join(["set_end_string END"] + lines + ["exit"]) + "\n",
                           capture_output=True, text=True, timeout=900)
        return [b.strip() for b in p.stdout.split("END") if b.strip()]

    # lock the even combos of r:0 at 0.3 / 0.7 under 16-bit storage, solve 300 iterations, read them back
    strat = "\n".join([" ".join(["0.3"] * 1326), " ".join(["0.7"] * 1326)])
    out = run(setup + ["small_strats on", "build_tree", f"set_strategy r:0 {strat.replace(chr(10), ' ')}",
                       f"combo_lock_node r:0 {mask}", "set_info_freq 1000", "go 300 steps", "wait_for_solver",
                       "show_range OOP r:0", "show_strategy r:0"])
    rows = [l.split() for l in out[-1].splitlines() if l.strip()]
    reach = [float(x) for x in out[-2].split()]
    dev = max(abs(float(rows[0][i]) - 0.3) for i in range(0, 1326, 2) if reach[i] > 0)
    check(dev < 0.003, f"16-bit combo-locked rows hold their value after 300 iterations (max |freq - 0.3| {dev:.5f})")
    out = run(["set_pruning on 1", "set_pruning on 0", "set_pruning on 2"])
    check(out[1:] == ["ERROR: set_pruning incorrect or missing argument"] * 2 + ["set_pruning ok!"],
          f"pruning periods below 2 are refused ({out[1:]})")
    # set_mes at a combo-locked node: unlocked hands become pure, locked ones stay
    out = run(setup + ["build_tree", "set_info_freq 1000", "go 60 steps", "wait_for_solver",
                       f"combo_lock_node r:0 {mask}", "show_strategy r:0", "set_mes OOP", "show_range OOP r:0",
                       "show_strategy r:0"])
    before = [[float(x) for x in l.split()] for l in out[-4].splitlines() if l.strip()]
    reach = [float(x) for x in out[-2].split()]
    after = [[float(x) for x in l.split()] for l in out[-1].splitlines() if l.strip()]
    unl = [i for i in range(1, 1326, 2) if reach[i] > 0]
    lck = [i for i in range(0, 1326, 2) if reach[i] > 0]
    pure = sum(1 for i in unl if max(r[i] for r in after) > 0.999)
    kept = all(abs(after[k][i] - before[k][i]) < 1e-3 for i in lck for k in range(len(after)))
    check(unl and pure == len(unl) and kept, f"set_mes: {pure}/{len(unl)} unlocked hands pure, locked kept: {kept}")


def check_mid_solve_settings(u):
    """#364: settings changed while a solve runs take effect between iterations (no data race), and a
    report frequency set mid-solve is used: before, the frequency was read once at the start, so a solve
    started with set_info_freq 0 never reported, so never stopped at its accuracy target."""
    print("[settings mid-solve]")
    set_small_tree(u)
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree for mid-solve settings")
    u.cmd("set_info_freq 0")
    u.cmd("set_accuracy 0.01")
    u.solver_msgs.clear()
    check(u.cmd("go 30 seconds") == "go ok!", "go with reports off")
    time.sleep(0.3)
    for c, want in (("set_dcfr 1.3 0 4 0.5 50", "set_dcfr ok!"), ("set_avg_delay 32", "set_avg_delay ok!"),
                    ("set_pruning on 8", "set_pruning ok!"), ("set_first_iteration_player OOP",
                                                               "set_first_iteration_player ok!")):
        check(u.cmd(c) == want, f"{c} while solving")
    check(u.cmd("show_iters").strip().isdigit(), "show_iters while solving")
    check(u.cmd("set_info_freq 5") == "set_info_freq ok!", "set_info_freq 5 while solving")
    t0 = time.time()
    u.cmd("wait_for_solver")
    stopped = [b for b in u.solver_msgs if b and b[0].startswith("SOLVER: stopped")]
    check(stopped and "required accuracy reached" in stopped[-1][0] and time.time() - t0 < 25,
          f"the new report frequency is used: the solve stops at its target ({stopped[-1][0] if stopped else None})")
    u.cmd("set_info_freq 25")
    # #378: info_freq 0 reports by time (like Pio), so the target still stops the solve
    u.cmd("build_tree")
    u.cmd("set_info_freq 0")
    u.cmd("set_accuracy 0.02")
    u.solver_msgs.clear()
    t0 = time.time()
    u.cmd("go 30 seconds")
    u.cmd("wait_for_solver")
    stopped = [b for b in u.solver_msgs if b and b[0].startswith("SOLVER: stopped")]
    check(stopped and "required accuracy reached" in stopped[-1][0] and time.time() - t0 < 20,
          f"info_freq 0 still stops on the target, by time ({time.time() - t0:.1f} s, "
          f"{stopped[-1][0] if stopped else None})")
    u.cmd("set_info_freq 25")
    u.cmd("free_tree")


def check_predictive(u):
    """#408: set_predictive (PCFR+-style matching, #71) converges and splits the pot like the default;
    bad arguments are refused, and so is switching it mid-solve."""
    print("[set_predictive]")
    set_small_tree(u)
    u.cmd("set_accuracy 0")
    u.cmd("set_info_freq 100")
    res = {}
    for mode in ("off", "on"):
        u.cmd("build_tree")
        check(u.cmd(f"set_predictive {mode}") == "set_predictive ok!", f"set_predictive {mode}")
        u.cmd("go 300 steps")
        u.cmd("wait_for_solver")
        r = u.cmd("calc_results").split("\n")
        res[mode] = [float(r[i].split(": ")[1]) for i in (0, 1, 4)]
    (eo, ei, xo), (po, pi, xp) = res["off"], res["on"]
    check(xp < 0.4 and xp < 4 * xo + 0.02, f"predictive converges (exploitability {xp:.4f} vs {xo:.4f} off, pot 40)")
    check(abs(po + pi - 40) < 0.05 and abs(po - eo) < 0.5, f"and gives the default's EVs ({po:.3f} vs {eo:.3f})")
    check(u.cmd("set_predictive maybe").startswith("ERROR") and u.cmd("set_predictive").startswith("ERROR"),
          "set_predictive refuses an argument other than on / off")
    u.cmd("go 60 seconds")
    check(u.cmd("set_predictive off") == "ERROR: set_predictive stop the solver first", "refused while solving")
    u.cmd("stop")
    u.cmd("wait_for_solver")
    check(u.cmd("set_predictive off") == "set_predictive ok!", "accepted once stopped")
    u.cmd("free_tree")


def check_setting_errors(u):
    """A setting command that errors leaves the setting as it was (it used to assign, then reject)."""
    print("[setting errors change nothing]")
    u.cmd("set_accuracy 0.3 fraction")
    check(u.cmd("set_accuracy 5 foo").startswith("ERROR"), "set_accuracy with a bad mode errors")
    st = u.cmd("show_settings").split("\n")
    check("accuracy: 0.300000" in st and "accuracy mode: fraction" in st, "a bad set_accuracy keeps the old one")
    check(u.cmd("set_pruning on -5").startswith("ERROR"), "set_pruning with a negative period errors")
    check(u.cmd("set_pruning on") == "set_pruning ok!", "set_pruning on still takes the old period")
    u.cmd("set_accuracy 0")


def check_legacy_params(u):
    """Pio 3.10 behavior, probed black-box: show_tree_params, set_bet_sizes/set_donk_bet, unlock_all,
    atoi-style chip amounts (fractions truncate), partial set_pot, a bad set_board clears the board."""
    print("[tree params, unlock_all, chip parsing]")
    u.cmd("free_tree")
    check(u.cmd("set_board Kh9h4h2c3d") == "set_board ok!", "set_board")
    check(u.cmd("set_board 2c").startswith("ERROR"), "bad set_board errors")
    tp = u.cmd("show_tree_params").split("\n")
    check(tp[0] == "board: not set" and [l.split(":")[0] for l in tp] == ["board", "pot", "bet_sizes", "donk_bet"],
          "bad set_board clears the board; show_tree_params layout")
    u.cmd("set_board Kh9h4h2c3d")
    u.cmd("set_pot 5 6 7")
    check(u.cmd("set_pot 1 2") == "set_pot ok!", "set_pot with two values")
    check(u.cmd("set_pot 9").startswith("ERROR"), "set_pot with one value errors")
    check(u.cmd("set_eff_stack").startswith("ERROR"), "bare set_eff_stack errors")
    check(u.cmd("set_bet_sizes 33.5,75 x 50") == "set_bet_sizes ok!", "set_bet_sizes")
    check(u.cmd("set_donk_bet -1") == "set_donk_bet ok!", "set_donk_bet")
    check(u.cmd("show_tree_params") == "board: Kh 9h 4h 2c 3d \npot: 9 2 7 \nbet_sizes: 33 0 50 \ndonk_bet: TRUE",
          "show_tree_params: partial set_pot keeps the rest, sizes parse like atoi")
    check(u.cmd("set_donk_bet 0.5") == "set_donk_bet ok!" and u.cmd("show_tree_params").endswith("donk_bet: FALSE"),
          "set_donk_bet 0.5 is false")
    u.cmd("set_bet_sizes")
    check(u.cmd("unlock_all") == "ERROR: unlock_all missing/incorrect tree", "unlock_all needs a tree")
    u.cmd("set_range OOP AA,KK,QQ")
    u.cmd("set_range IP AA,KK,QQ,JJ")
    u.cmd("set_pot 0 0 61.7")
    u.cmd("set_eff_stack 200.9")
    u.cmd("clear_lines")
    for l in ("30.7", "0 30.2", "0 30.9 80.5", "40"):
        check(u.cmd("add_line " + l) == "add_line ok!", f"add_line {l} accepted")
    check(u.cmd("remove_line 40.9") == "remove_line ok!", "remove_line with a fraction")
    check(u.cmd("add_line abc").startswith("ERROR"), "add_line without a digit still errors")
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree with truncated amounts")
    lines = u.cmd("show_all_lines").split("\n")
    check("r:0:b30" in lines and "r:0:c:b30:b80" in lines and "r:0:b40" not in lines,
          "fractions truncate (30.7 -> b30, 80.5 -> b80) and remove_line 40.9 removed the 40 line")
    check(u.cmd("show_node r:0").split("\n")[3].split() == ["0", "0", "61"], "set_pot 61.7 is 61")
    u.cmd("lock_node r:0:c")
    order = hand_order(u)
    aa = [i for i, h in enumerate(order) if h[0] == "A" and h[2] == "A"]
    v = ["0"] * 1326
    for i in aa:
        v[i] = "1"
    check(u.cmd("combo_lock_node r:0 " + " ".join(v)) == "combo_lock_node ok!", "combo_lock_node")
    check(u.cmd("unlock_all") == "unlock_all ok!", "unlock_all")
    check(sum(floats(u.cmd("show_locked_combos r:0:c"))) == 0, "unlock_all clears node locks")
    check(sum(floats(u.cmd("show_locked_combos r:0"))) == len(aa), "unlock_all keeps combo locks (as Pio)")
    check(u.cmd("combo_unlock_all") == "combo_unlock_all ok!", "combo_unlock_all (extension, #255)")
    check(sum(floats(u.cmd("show_locked_combos r:0"))) == 0 and "COMBO_LOCKED" not in u.cmd("show_node r:0"),
          "combo_unlock_all clears combo locks: no mask, no flag")
    u.cmd("free_tree")
    for c in ("lock_node", "combo_unlock_all", "show_node r:0", "set_rake 0.05 3", "set_icm 100 100", "clear_cache", "go 10 steps"):
        check(u.cmd(c).endswith(" missing/incorrect tree"), f"{c} without a tree: missing/incorrect tree first")

    print("[over-long lines keep their prefix]")
    u.cmd("set_board Kh9h4h")
    u.cmd("set_pot 0 0 100")
    u.cmd("set_eff_stack 200")
    u.cmd("clear_lines")
    long_msg = "ERROR: add_line Specified line is too long. Some actions were not included in the tree."
    check(u.cmd("add_line 50 50 200 200 0") == long_msg, "a line past an all-in call: Pio's add_line error")
    check(u.cmd("add_line 50 0") == "add_line ok!", "a fold ends a line exactly")
    check(u.cmd("build_tree") == "build_tree ok!", "build_tree keeps the valid prefix")
    lines = u.cmd("show_all_lines").split("\n")
    check("r:0:b50:c:?:b200:c" in lines and not any(l.startswith("r:0:b50:c:?:b200:c:") for l in lines),
          "the over-long line stops at the all-in call")
    # add_line checks the line against the current config (probed on Pio 3.10): an amount below
    # the bet faced is "Incorrect bet size." (0 facing a bet is a fold), and with no stack set
    # Pio has no all-in root, so nothing is "too long" before the river.
    bad_size = "ERROR: add_line Incorrect bet size."
    u.cmd("clear_lines")
    check(u.cmd("add_line 50 30") == bad_size, "an amount between own investment and the bet faced")
    check(u.cmd("add_line 50 50 100 0") == bad_size, "an amount below own investment")
    u.cmd("clear_lines")
    u.cmd("set_eff_stack 0")
    check(u.cmd("add_line 75 200") == "add_line ok!", "no stack set: no all-in root at add time (Pio)")
    check(u.cmd("add_line 50 50 200 200 0") == bad_size, "no stack set: amounts are still checked")
    check(u.cmd("add_line 0 0 0 0 0 0 0") == long_msg, "no stack set: past the river is too long")
    u.cmd("clear_lines")
    u.cmd("set_eff_stack 200")

    print("[rake and ICM belong to the tree]")
    u.cmd("set_board KhQs7d4c2s")
    u.cmd("set_pot 0 0 100")
    u.cmd("set_eff_stack 200")
    u.cmd("clear_lines")
    u.cmd("add_line 75 200")
    u.cmd("build_tree")
    path = "r:0:b75:b200:c"
    check(u.cmd(f"show_win_evs OOP {path}") == "300 -200 50", "no rake")
    u.cmd("set_rake 0.05 3")
    check(u.cmd(f"show_win_evs OOP {path}") == "297 -200 48.5", "set_rake on the tree")
    u.cmd("build_tree")
    check(u.cmd(f"show_win_evs OOP {path}") == "300 -200 50", "build_tree starts without rake (Pio)")
    for c in ("set_icm_point OOP 0 0", "set_icm_point OOP 200 40", "set_icm_point OOP 400 66",
              "set_icm_point IP 0 0", "set_icm_point IP 300 52", "set_icm_point IP 500 72", "set_icm 200 300"):
        u.cmd(c)
    check(u.cmd(f"show_win_evs OOP {path}") == "39 -40 6.5", "set_icm on the tree")
    u.cmd("build_tree")
    check(u.cmd(f"show_win_evs OOP {path}") == "300 -200 50", "build_tree starts without ICM (Pio)")
    check(u.cmd("set_icm 200 300") == "set_icm ok!" and u.cmd(f"show_win_evs OOP {path}") == "39 -40 6.5",
          "the ICM points survive build_tree")
    u.cmd("reset_icm_tables")
    u.cmd("free_tree")


def check_framing(exe):
    """Raw stdout around the terminator and exit, byte for byte as PioSOLVER 3.10.1 printed it for
    the same input (black-box, 2026-11-04): reset_end_string answers without a terminator line and
    leaves the later answers bare; nothing follows exit. quit is our alias (Pio rejects it)."""
    print("[framing: reset_end_string, exit, quit]")

    def raw(lines):
        p = subprocess.run([exe], input="".join(l + "\n" for l in lines), capture_output=True,
                           text=True, timeout=60)
        out = p.stdout
        return out[out.index("set_end_string ok!"):], p.returncode

    out, rc = raw(["set_end_string END", "is_ready", "reset_end_string", "is_ready",
                   "set_end_string XX", "is_ready", "exit", "is_ready"])
    want = ("set_end_string ok!\nEND\nis_ready ok!\nEND\nreset_end_string ok!\nis_ready ok!\n"
            "set_end_string ok!\nXX\nis_ready ok!\nXX\n")
    check(out == want and rc == 0, f"reset_end_string / exit framing matches Pio ({out!r}, rc {rc})")
    out, rc = raw(["set_end_string END", "quit", "is_ready"])
    check(out == "set_end_string ok!\nEND\n" and rc == 0, f"quit exits like exit ({out!r}, rc {rc})")


def check_exit_while_parked(exe):
    """The input ending while a solve is parked by hold must end the process (#293). run() set the stop
    flag without waking the parked solver thread, then joined it forever: a viewer that died mid-burst
    left its solver, and the whole tree's memory, behind. exit itself was fine (stop_solver releases)."""
    print("[input ends while a solve is parked]")
    setup = ["set_range OOP AA,KK,QQ,AK", "set_range IP AA,KK,JJ,AQ", "set_board KhQs7d4c2s", "set_pot 0 0 100",
             "set_eff_stack 100", "add_line 50 100", "add_line 0 50 100", "build_tree", "set_accuracy 0", "go", "hold"]
    for tail, what in ((["exit"], "exit"), ([], "end of input")):
        try:
            p = subprocess.run([exe], input="".join(l + "\n" for l in setup + tail), capture_output=True,
                               text=True, timeout=30)
            check(p.returncode == 0 and "hold ok!" in p.stdout, f"{what} after hold during a solve ends the process")
        except subprocess.TimeoutExpired:
            check(False, f"{what} after hold during a solve ends the process (hung for 30 s)")


def check_save_robustness(exe):
    """#360: corrupt or crafted saves give an ERROR, never a crash or a huge allocation, and a tree loaded
    from a truncated save is dumped no deeper than it has solved data."""
    import gzip
    import struct
    print("[save robustness]")
    setup = ["set_threads 2", "set_board Kh9h4h2c",
             "set_range OOP 22+,A2s+,K7s+,Q9s+,J9s+,T9s,ATo+,KJo+",
             "set_range IP 22+,A4s+,K8s+,Q9s+,J9s+,T9s,98s,A9o+,KTo+,QJo",
             "set_pot 0 0 100", "set_eff_stack 400", "clear_lines", "add_line 0 0 33 33", "add_line 0 33 33 100 100",
             "build_tree"]

    def run(lines):
        p = subprocess.run([exe], input="\n".join(lines + ["exit"]) + "\n", capture_output=True, text=True, timeout=300)
        return p.returncode, p.stdout

    with tempfile.TemporaryDirectory() as td:
        full, nr, b = (os.path.join(td, n) for n in ("full.cfr", "nr.cfr", "b.cfr"))
        rc, out = run(setup + ["go 200 steps", "wait_for_solver", f'dump_tree "{full}"', f'dump_tree "{nr}" no_rivers',
                               f'load_tree "{nr}"', f'dump_tree "{b}"', f'load_tree "{b}"', "calc_results"])
        ex = [float(l.split(":")[1]) for l in out.splitlines() if l.startswith("Exploitable for:")]
        check(rc == 0 and ex and ex[-1] < 1.0,
              f"a full dump of a no_rivers load keeps it truncated: re-solved, not unsolved rivers (exploitable {ex[-1:]})")
        raw = bytearray(gzip.open(full).read())
        cfglen = struct.unpack_from("<I", raw, 18)[0]
        mlen_off = 22 + cfglen
        flags_off = mlen_off + 8 + 16

        def crafted(name, fn):
            d = bytearray(raw)
            fn(d)
            path = os.path.join(td, name + ".cfr")
            with gzip.open(path, "wb") as f:
                f.write(bytes(d))
            rc, out = run([f'load_tree "{path}"', "show_locked_combos r:0", "go 2 steps", "wait_for_solver"])
            first = next((l for l in out.splitlines() if "load_tree" in l), "")
            check(rc == 0 and first.startswith("ERROR") and "file format error" in first,
                  f"crafted save ({name}): {first[:70]} (rc {rc})")
        crafted("truncate field 7", lambda d: struct.pack_into("<I", d, 12, 7))
        crafted("metadata size 2^34", lambda d: struct.pack_into("<Q", d, mlen_off, 1 << 34))
        crafted("combo-lock flag without a mask", lambda d: d.__setitem__(flags_off, d[flags_off] | 2))
        # a lazily loaded save that disappears: its segments can't be read -> ERROR, not std::terminate
        gone = os.path.join(td, "gone.cfr")
        with open(full, "rb") as src, open(gone, "wb") as dst:
            dst.write(src.read())
        p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        p.stdin.write(f'load_tree "{gone}"\nis_ready\n')
        p.stdin.flush()
        time.sleep(1.0)
        os.remove(gone)
        o, _ = p.communicate("load_all_nodes\ncalc_results\nexit\n", timeout=120)
        check(p.returncode == 0 and "ERROR" in o and "terminate" not in o,
              f"a lazy load whose file is gone answers ERROR (rc {p.returncode})")
        # #376: go on such a tree answers once (the ERROR), not "go ok!" and then an ERROR
        with open(full, "rb") as src, open(gone, "wb") as dst:
            dst.write(src.read())
        p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        p.stdin.write(f'set_end_string END\nload_tree "{gone}"\nis_ready\n')
        p.stdin.flush()
        time.sleep(1.0)
        os.remove(gone)
        o, _ = p.communicate("go 2 steps\ngo_partial r:0 2 steps\nis_ready\nexit\n", timeout=120)
        tail = o[o.index("is_ready ok!") + len("is_ready ok!"):] if "is_ready ok!" in o else o
        blocks = [b.strip() for b in tail.split("END") if b.strip()]
        check(p.returncode == 0 and "go ok!" not in tail and "go_partial ok!" not in tail
              and len(blocks) == 3 and blocks[0].startswith("ERROR: go ") and blocks[1].startswith("ERROR: go_partial")
              and blocks[2] == "is_ready ok!",
              f"go / go_partial on an unreadable lazy tree answer once each ({blocks[:3]})")


def main(exe, big=False):
    check_framing(exe)
    check_save_robustness(exe)
    check_tree_construction_edges(exe)
    check_core_review(exe)
    check_exit_while_parked(exe)
    u = Upi(exe)
    try:
        print("[basics]")
        check(u.cmd("is_ready") == "is_ready ok!", "is_ready")
        order = hand_order(u)
        check(len(order) == 1326 and order[:3] == ["2d2c", "2h2c", "2h2d"] and order[-1] == "AsAh", "hand order")
        check(u.cmd("echo hello world") == "hello", "echo (first argument, like Pio)")
        check(u.cmd("bogus_command").startswith("ERROR"), "unknown command errors")
        check(u.cmd("set_board Ac Ad Ah").startswith("ERROR"), "spaced board rejected (UPI 2+)")

        print("[range text vs Pio 1326 floats]")
        r0, r1, f0 = load_fixture(u)
        u.cmd("set_range OOP " + r0)
        mine = floats(u.cmd("show_range OOP"))
        diff = max(abs(a - b) for a, b in zip(mine, f0))
        check(len(mine) == 1326 and diff < 1e-6, f"Pio range text parses to the same 1326 weights (max diff {diff})")
        # restore the exact floats
        load_fixture(u)

        if big:
            run_big_tree(u, order)
            u.cmd("small_strats auto")  # run_big_tree pins 16-bit storage; later sections use the default
        else:
            run_small_tree(u, order)

        check_calc_eq_preflop_exact(u)

        check_calc_ev_zero_reach(u)
        check_union_hands(u)
        check_schematic_after_build(u)
        check_iso_symmetry(u)
        check_threads_auto(u)
        check_output_format(u)

        check_force_line(u)
        check_scripts(u)
        check_global_freq(u)
        check_go_partial(u)
        check_go_limits_and_errors(u)
        check_info_freq_auto(u)
        check_setting_errors(u)
        check_mid_solve_settings(u)
        check_predictive(u)

        print("[river tree + set_mes + rake]")
        u.cmd("set_board KhQs7d4c2s")
        u.cmd("set_range OOP AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s")
        u.cmd("set_range IP AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s")
        u.cmd("set_pot 0 0 100")
        u.cmd("set_eff_stack 200")
        u.cmd("clear_lines")
        for l in ("75 200", "0 75 200", "0 50", "75"):
            u.cmd("add_line " + l)
        u.cmd("build_tree")
        u.cmd("set_accuracy 0")
        u.cmd("go 400 steps")
        u.cmd("wait_for_solver")
        res = u.cmd("calc_results").split("\n")
        expl = float(res[4].split(": ")[1])
        check(expl < 0.05, f"river exploitability {expl:.5f}")
        # Locked nodes: a best response may not deviate there (Pio semantics, probe9).
        pure = " ".join(["1"] * 1326 + ["0"] * 1326)
        u.cmd("set_strategy r:0 " + pure)
        u.cmd("lock_node r:0")
        u.cmd("go 100 steps"); u.cmd("wait_for_solver")
        rl = u.cmd("calc_results").split("\n")
        ev_l, mes_l = float(rl[0].split(": ")[1]), float(rl[2].split(": ")[1])
        check(mes_l - ev_l < 0.3, f"locked root: OOP MES close to EV ({ev_l:.3f} vs {mes_l:.3f})")
        u.cmd("unlock_node r:0")
        u.cmd("set_mes IP")
        res2 = u.cmd("calc_results").split("\n")
        ev_ip, mes_ip = float(res2[1].split(": ")[1]), float(res2[3].split(": ")[1])
        check(abs(ev_ip - mes_ip) < 1e-3, f"after set_mes IP, IP EV equals IP MES ({ev_ip:.4f} vs {mes_ip:.4f})")
        u.cmd("set_rake 0.05 3")
        u.cmd("set_equal_strats")
        u.cmd("go 300 steps")
        u.cmd("wait_for_solver")
        res3 = u.cmd("calc_results").split("\n")
        s3 = float(res3[0].split(": ")[1]) + float(res3[1].split(": ")[1])
        check(97 - 0.01 < s3 < 100, f"with rake, EV sum below pot ({s3:.3f})")
        # Pio semantics (probe8): set_icm needs ICM points; rake and ICM are last-one-wins.
        check(u.cmd("set_icm 200 300") == "ERROR: set_icm ICM points not defined.\nset_icm ok!", "set_icm without points")
        for c in ["set_icm_point OOP 0 0", "set_icm_point OOP 200 40", "set_icm_point OOP 400 66",
                  "set_icm_point IP 0 0", "set_icm_point IP 300 52", "set_icm_point IP 500 72", "set_icm 200 300"]:
            u.cmd(c)
        icm = u.cmd("show_win_evs OOP r:0:b75:b200:c")
        check(icm != "300 -200 50" and icm.split()[1] == "-40", f"ICM payoffs ({icm})")
        u.cmd("set_rake 0.05 3")
        check(u.cmd("show_win_evs OOP r:0:b75:b200:c") == "297 -200 48.5", "set_rake turns ICM off (chips + rake)")
        u.cmd("reset_icm_tables")
        check(u.cmd("show_win_evs OOP r:0:b75:b200:c") == "297 -200 48.5", "reset_icm_tables keeps rake")
        check(u.cmd("set_icm 200 300") == "set_icm ok!", "reset tables count as defined")
        u.cmd("set_rake 0 0")
        print("[pruning (set_pruning, #51)]")
        check(u.cmd("set_pruning").startswith("ERROR"), "set_pruning needs on/off")
        check(u.cmd("set_pruning on -1").startswith("ERROR"), "negative period rejected")
        check(u.cmd("set_pruning on 8") == "set_pruning ok!", "set_pruning on 8")
        u.cmd("set_board KhQs7d4c2s")
        u.cmd("set_range OOP AA,KK,QQ,77,44,22,AK,AQ,KQ,QJs,JTs,T9s,98s,65s,A5s")
        u.cmd("set_range IP AA,KK,QQ,JJ,TT,AK,AQ,KQ,KJ,QJ,JT,T9s")
        u.cmd("set_pot 0 0 100")
        u.cmd("set_eff_stack 200")
        u.cmd("clear_lines")
        for l in ("75 200", "0 75 200", "0 50", "75"):
            u.cmd("add_line " + l)
        u.cmd("build_tree")
        u.cmd("set_accuracy 0")
        u.cmd("go 400 steps")
        u.cmd("wait_for_solver")
        res = u.cmd("calc_results").split("\n")
        evo, evi = float(res[0].split(": ")[1]), float(res[1].split(": ")[1])
        expl = float(res[4].split(": ")[1])
        check(abs(evo + evi - 100) < 0.05, f"pruning: EV OOP + EV IP = pot ({evo:.3f} + {evi:.3f})")
        check(expl < 0.05, f"pruning: river exploitability {expl:.5f}")
        u.cmd("set_pruning off")
        print("[misc]")
        check(u.cmd("show_effective_stack") == "200", "show_effective_stack")
        memory = u.cmd("show_memory")
        values = re.fullmatch(r"Total Physical Memory: (\d+) MB\nAvailable Phys Memory: (\d+) MB", memory)
        check(values is not None, "show_memory format")
        if values:
            total, available = map(int, values.groups())
            check(0 < available <= total, "show_memory positive physical memory values")
        cats = u.cmd("show_category_names").split("\n")
        check(len(cats) == 2 and "top_pair" in cats[0], "show_category_names")
        check(len(u.cmd("show_categories KhQs7d").split("\n")[0].split()) == 1326, "show_categories")
        nuts = u.cmd("show_nuts OOP KhQs7d4c2s").split("\n")
        check(all(" - " in l for l in nuts if l), "show_nuts format")
        check(u.cmd("set_display_iso sh") == "set_display_iso ok!", "set_display_iso")
        check(u.cmd("show_node r:0").split("\n")[2] == "Ks Qh 7d 4c 2h ", "display iso swaps the shown board")
        check(u.cmd("show_board_no_iso") == "Kh Qs 7d 4c 2s ", "show_board_no_iso")
        u.cmd("reset_display_iso")

        print("[truncated saves: on-demand recalc]")
        for c in ["set_range OOP AA,KK,QQ,JJ,TT,99,AK,AQ,AJ,KQ,KJ,QJ,JT,T9", "set_range IP AA,KK,QQ,JJ,TT,99,88,AK,AQ,KQ,QJ,JT",
                  "set_board Kh9h4h", "set_pot 0 0 60", "set_eff_stack 200", "clear_lines", "add_line 20 20 60 60 200",
                  "add_line 0 20 20 60 60", "add_line 0 0 40", "set_rake 0 0", "build_tree", "go 100 steps", "wait_for_solver"]:
            u.cmd(c)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "noturns.cfr")
            u.cmd(f'dump_tree "{path}" no_turns'); u.cmd("free_tree"); u.cmd(f'load_tree "{path}"')
            check("INCOMPLETE_TREE" in u.cmd("show_node r") and "FAST_LOAD" in u.cmd("show_node r"), "truncated load flags")
            check(u.cmd("rebuild_tree").endswith("please call load_all_nodes"), "rebuild_tree needs load_all_nodes")
            st = [floats(l) for l in u.cmd("show_strategy r:0:c:c:2c").split("\n")]
            check(any(x > 0 for l in st for x in l), "missing turn re-solved on demand")
            check("ALREADY_RECALCED" in u.cmd("show_node r:0:c:c:2c"), "ALREADY_RECALCED flag")
            check(float(u.cmd("explo_partial r:0:c:c:2c")) < 0.06, "recalc reaches recalc accuracy (0.1% pot)")

        print("[preflop tree]")
        for c in ["set_range OOP QQ+,AKs,AKo,KQs", "set_range IP QQ+,AK,AQs,KQs,JTs", "set_pot 10 5 0",
                  "set_eff_stack 1000", "clear_preflop_lines", "add_preflop_line 25 25", "add_preflop_line 25 100 100",
                  "set_rake 0 0"]:
            u.cmd(c)
        check(u.cmd("build_preflop_tree") == "build_preflop_tree ok!", "build_preflop_tree")
        counts = u.cmd("node_count")
        check("ROOT: 1\n  FLOP  1\n  TURN  0\n  RIVER 0" in counts,
              "preflop node_count classifies wrapper root correctly")
        n0 = u.cmd("show_node r:0").split("\n")
        check(n0[1] == "IP_DEC" and n0[2] == "---" and n0[3] == "10 5 0 ", "SB (IP) acts first preflop, board ---")
        check(u.cmd("show_node r:0:b25:c").split("\n")[1] == "END_NODE", "pure preflop exit is END_NODE")
        u.cmd("reset_subset")
        u.cmd("add_to_subset 1 As Ks Qs")
        u.cmd("add_to_subset 0.5 7c 7d 2h")
        check(u.cmd("show_subset").endswith("2 flops total"), "show_subset")
        for exit_id, pot, stack, lines in [("r:0:b25:c", "0 0 50", 975, ["0 25 25", "25 75 75"]),
                                           ("r:0:b25:b100:c", "0 0 200", 900, ["0 100 100"]),
                                           ("r:0:c:c", "0 0 20", 990, ["0 10 10"])]:
            u.cmd("clear_lines"); u.cmd("set_pot " + pot); u.cmd(f"set_eff_stack {stack}")
            for l in lines:
                u.cmd("add_line " + l)
            check(u.cmd("add_schematic_tree " + exit_id) == "add_schematic_tree ok!", f"add_schematic_tree {exit_id}")
        check(u.cmd("add_all_flops") == "add_all_flops ok!", "add_all_flops")
        kids = u.cmd("show_children r:0:b25:c")
        check("r:0:b25:c:AsKsQs" in kids and "r:0:b25:c:7c7d2h" in kids, "flop children named by subset tokens")
        check(u.cmd("show_node r:0:b25:c:AsKsQs").split("\n")[3] == "0 0 50 ", "postflop subtree uses the schematic pot")
        check(u.cmd("show_win_evs IP r:0:f") == "-5 -5 -5", "preflop fold payoff (whole hand)")
        check(u.cmd("show_win_evs OOP r:0:b25:c:AsKsQs:c:c:2c:c:c:2d:c:c") == "50 0 25", "postflop payoff (schematic-relative)")
        u.cmd("set_accuracy 0"); u.cmd("go 300 steps"); u.cmd("wait_for_solver")
        res = u.cmd("calc_results").split("\n")
        evo, evi, ex = (float(res[k].split(": ")[1]) for k in (0, 1, 4))
        # Schematic pots are literal (Pio semantics): their dead money is free money, so EVs are not
        # zero-sum here. Values verified against Pio 3.10.1 (MES 95.42 / 107.74 after 2000 steps).
        check(ex < 0.05 and abs(evo - 95.49) < 0.5 and abs(evi - 107.67) < 0.5,
              f"preflop solve ({evo:.3f}/{evi:.3f}), explo {ex:.4f}")
        pp = u.cmd("show_strategy_pp r:0").split("\n")
        check(len(pp) == 169 and pp[0].startswith("AA : "), "preflop show_strategy_pp by class")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pf.cfr")
            s_before = u.cmd("show_strategy r:0:b25:c:7c7d2h")
            u.cmd(f'dump_tree "{path}" lossless'); u.cmd("free_tree")
            check(u.cmd(f'load_tree "{path}"') == "load_tree ok!", "load preflop tree")
            check(u.cmd("show_strategy r:0:b25:c:7c7d2h") == s_before, "preflop tree strategies survive save/load")

        print("[pure preflop exit: exact equity at all-in terminals]")
        u.cmd("set_range OOP AA")
        u.cmd("set_range IP KK")
        u.cmd("set_pot 10 5 0")
        u.cmd("set_eff_stack 25")
        u.cmd("clear_preflop_lines")
        u.cmd("add_preflop_line 25 25")
        u.cmd("build_preflop_tree")
        # The exit r:0:b25:c is a preflop all-in showdown (END_NODE): EV per hand must equal
        # pot * equity - investment = 50 * eq - 25, with eq the exact table-based equity.
        eq_aa = floats(u.cmd("calc_eq_preflop OOP").split("\n")[0])[0]  # class AA
        evs = floats(u.cmd("calc_ev OOP r:0:b25:c").split("\n")[0])
        aa_idx = [i for i, h in enumerate(order) if h[0] == "A" and h[2] == "A"]
        ev_aa = sum(evs[i] for i in aa_idx) / len(aa_idx)
        check(abs(ev_aa - (50 * eq_aa - 25)) < 0.05,
              f"preflop all-in exit EV {ev_aa:.4f} == 50*{eq_aa:.6f}-25 = {50*eq_aa-25:.4f}")
        u.cmd("set_accuracy 0")
        u.cmd("go 50 steps")
        u.cmd("wait_for_solver")
        res = u.cmd("calc_results").split("\n")
        ex = float(res[4].split(": ")[1])
        check(ex < 0.05, f"pure preflop all-in tree converges (explo {ex:.4f})")

        check_legacy_params(u)  # last: it defines ICM points, which (as in Pio) stay defined
    finally:
        u.close()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "./solver", big="--big" in sys.argv))
