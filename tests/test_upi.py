#!/usr/bin/env python3
"""End-to-end UPI tests: drives the solver binary over stdin/stdout like a PioSOLVER client.

Usage: python3 tests/test_upi.py ./solver [--big]

By default the multi-street checks run on a small tree (tens of MB) so `make test` fits in
< 4 GB of RAM. With --big they run on the real 25bb fixture tree instead (~6.4 GB float32,
~3.3 GB with small_strats) — use that on a large-RAM machine.
"""
import os
import re
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
        u.cmd(f'dump_tree "{small}" no_rivers lossless')
        check(os.path.getsize(small) < os.path.getsize(path), "no_rivers save is smaller")
        if deep_paths:
            u.cmd("free_tree")
            u.cmd(f'load_tree "{small}"')
            flags = u.cmd("show_node r")
            check("INCOMPLETE_TREE" in flags and "FAST_LOAD" in flags, "no_rivers: incomplete + fast-load flags")
            check(u.cmd("show_strategy " + strat_path) == s_before, "no_rivers: lazily loaded strategy exact")
            check(u.cmd("rebuild_tree").startswith("ERROR"), "rebuild_tree needs load_all_nodes")
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
            "ev_pp_lines": len(u.cmd("calc_ev_pp OOP r:0").split("\n")),
            "strat_pp": combos(u.cmd("show_strategy_pp r:0")),
            "deep": u.cmd("calc_ev OOP r:0:b20:b200").split("\n")[0].split(),
        }

    off, on = solve(False), solve(True)

    check(u.cmd("set_union_hands maybe").startswith("ERROR"), "set_union_hands rejects a bad argument")

    added = sorted(set(on["ev_pp"]) - set(off["ev_pp"]))
    check(len(off["ev_pp"]) == 142 and len(on["ev_pp"]) == 148,
          f"calc_ev_pp lists the union ({len(off['ev_pp'])} -> {len(on['ev_pp'])}, expected 142 -> 148)")
    check(added == ["8d8c", "8h8c", "8h8d", "8s8c", "8s8d", "8s8h"],
          f"the added hands are exactly the six 88 combos ({added})")
    check(len(on["strat_pp"]) == 148 and sorted(set(on["strat_pp"]) - set(off["strat_pp"])) == added,
          "show_strategy_pp lists the same opponent-only hands")

    # #61: recorded PioSOLVER 3.10.1 output on this exact tree. Pio always lists the union, for
    # both players: an "EV: <x>" header plus 148 rows (the "150 lines" in #9 counted more than the
    # response). With set_union_hands on we list the same hands; only the order among equal-EV
    # (suit-isomorphic) combos may differ, which depends on last-bit float noise.
    with open(os.path.join(HERE, "data", "pio_probe2_calc_ev_pp.txt")) as f:
        blocks = [b for b in f.read().split("END\n") if b.strip()]
    pio = [[l.split(":")[0] for l in b.split("\n") if re.match(r"^[2-9TJQKA][cdhs][2-9TJQKA][cdhs]:", l)]
           for b in blocks]
    check(len(pio) == 2 and len(pio[0]) == 148 and len(pio[1]) == 148, "recorded Pio calc_ev_pp: 148 rows per player")
    check(sorted(on["ev_pp"]) == sorted(pio[0]), "calc_ev_pp OOP with union_hands lists exactly Pio's hands")
    check(sorted(on["ev_pp_ip"]) == sorted(pio[1]), "calc_ev_pp IP with union_hands lists exactly Pio's hands")
    check(on["ev_pp_lines"] == 149, f"calc_ev_pp response is EV header + 148 rows like Pio ({on['ev_pp_lines']} lines)")

    # Equilibrium must not move: the extra hands enter with reach 0.
    check(off["evs"] == on["evs"], f"equilibrium unchanged by union_hands ({off['evs']} vs {on['evs']})")

    # Deep node where OOP has 20 in and faces an all-in: the opponent-only hands have a
    # negative counterfactual value, so Pio's signed -inf rather than a bare inf.
    # Look at exactly the six 88 combos: other hands can also print +-inf there whenever OOP's
    # average strategy never reaches the node with them (EV = cfv / 0, as in Pio), which depends
    # on how far the loose solve got, not on union_hands.
    order = u.cmd("show_hand_order").split()
    idx88 = [i for i, c in enumerate(order) if c[0] == "8" and c[2] == "8"]
    n_off = sorted(off["deep"][i] for i in idx88)
    n_on = sorted(on["deep"][i] for i in idx88)
    check(n_off == ["inf"] * 6, f"without union_hands the six hands print unsigned inf ({n_off[:2]}...)")
    check(n_on == ["-inf"] * 6, f"with union_hands they print -inf, the sign of their cfv ({n_on[:2]}...)")

    u.cmd("set_union_hands off")
    u.cmd("free_tree")


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

    check(u.cmd("go_partial").startswith("ERROR"), "go_partial needs an argument")
    check(u.cmd("go_partial r:0:bogus").startswith("ERROR"), "go_partial on a bad node errors")
    check(u.cmd("go_partial r:0:c 2 eons").startswith("ERROR"), "go_partial on a bad unit errors")
    u.cmd("free_tree")


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
    u.cmd("set_info_freq 25")
    u.cmd("free_tree")


def main(exe, big=False):
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

        check_union_hands(u)

        check_go_partial(u)
        check_info_freq_auto(u)

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
