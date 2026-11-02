#!/usr/bin/env python3
"""End-to-end UPI tests: drives the solver binary over stdin/stdout like a PioSOLVER client.

Usage: python3 tests/test_upi.py ./solver
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


def main(exe):
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

        print("[build + solve the real Pio script tree]")
        est = u.cmd("estimate_schematic_tree")
        check("MB" in est, "estimate_schematic_tree")
        check(u.cmd("build_tree") == "build_tree ok!", "build_tree")
        check(u.cmd("is_tree_present") == "true", "is_tree_present")
        node = u.cmd("show_node r").split("\n")
        check(node[0] == "r" and node[1] == "ROOT" and node[2] == "Qh Qc 2c " and node[3] == "0 0 40 ", "show_node r")
        node = u.cmd("show_node r:0").split("\n")
        check(node[1] == "OOP_DEC" and node[4].endswith("children"), "show_node r:0")
        kids = [k for k in u.cmd("show_children r:0").split("\n\n") if k.strip()]
        names = [k.split("\n")[1].split(":")[-1] for k in kids]
        check(names == ["b24", "b12", "c"], f"root actions in Pio order {names}")
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

        print("[isomorphism: QhQc2c with flop iso -> turn cards of suits d/s are equivalent? (board uses h,c)]")
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

        print("[save / load]")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.cfr")
            check(u.cmd(f'dump_tree "{path}"') == "dump_tree ok!", "dump_tree")
            check(u.cmd(f'check_tree_health "{path}"') == "File OK", "check_tree_health")
            meta = u.cmd(f'show_metadata "{path}"').split("\n")
            check(meta[1] == "ROOT" and meta[2] == "Qh Qc 2c", "show_metadata")
            s_before = u.cmd("show_strategy r:0:c:b24")
            u.cmd("free_tree")
            check(u.cmd("is_tree_present") == "false", "free_tree")
            check(u.cmd(f'load_tree "{path}"') == "load_tree ok!", "load_tree")
            check(u.cmd("show_strategy r:0:c:b24") == s_before, "strategy identical after reload")
            small = os.path.join(td, "small.cfr")
            u.cmd(f'dump_tree "{small}" no_rivers')
            check(os.path.getsize(small) < os.path.getsize(path), "no_rivers save is smaller")
            with open(path, "r+b") as f:
                f.seek(200)
                f.write(b"\xff\xfe\xfd")
            check(u.cmd(f'check_tree_health "{path}"') == "File CORRUPTED", "corruption detected")

        print("[lines]")
        lines = u.cmd("show_all_lines").split("\n")
        check(lines[0] == "r" and lines[1] == "r:0" and len(lines) > 100, f"show_all_lines ({len(lines)} lines)")
        f_c = float(u.cmd("calc_line_freq r:0:c"))
        f_b1 = float(u.cmd("calc_line_freq r:0:b12"))
        f_b2 = float(u.cmd("calc_line_freq r:0:b24"))
        check(abs(f_c + f_b1 + f_b2 - 1) < 1e-4, f"root action frequencies sum to 1 ({f_c + f_b1 + f_b2:.5f})")
        wins = u.cmd("show_win_evs OOP r:0:c:c:?:c:c:?:c:c").split()
        check(len(wins) == 3 and float(wins[0]) == 40 and float(wins[1]) == 0 and float(wins[2]) == 20,
              f"show_win_evs check-down {wins}")

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

        print("[preflop tree]")
        for c in ["set_range OOP QQ+,AKs,AKo,KQs", "set_range IP QQ+,AK,AQs,KQs,JTs", "set_pot 10 5 0",
                  "set_eff_stack 1000", "clear_preflop_lines", "add_preflop_line 25 25", "add_preflop_line 25 100 100",
                  "set_rake 0 0"]:
            u.cmd(c)
        check(u.cmd("build_preflop_tree") == "build_preflop_tree ok!", "build_preflop_tree")
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
        check(abs(evo + evi) < 0.01 and ex < 0.05, f"preflop solve: zero-sum EVs ({evo:.3f}/{evi:.3f}), explo {ex:.4f}")
        pp = u.cmd("show_strategy_pp r:0").split("\n")
        check(len(pp) == 169 and pp[0].startswith("AA : "), "preflop show_strategy_pp by class")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pf.cfr")
            s_before = u.cmd("show_strategy r:0:b25:c:7c7d2h")
            u.cmd(f'dump_tree "{path}"'); u.cmd("free_tree")
            check(u.cmd(f'load_tree "{path}"') == "load_tree ok!", "load preflop tree")
            check(u.cmd("show_strategy r:0:b25:c:7c7d2h") == s_before, "preflop tree strategies survive save/load")
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
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "./solver"))
