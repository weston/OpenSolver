#!/usr/bin/env python3
"""Randomised checks of the solver over UPI: invariants, concurrency, saves, preflop trees, locks, truncated saves.

Each mode runs random cases for a time budget and prints one FAIL line per failing case, with its seed
and the script that reproduces it, then a summary. Exit status is 1 if anything failed. Not part of
`make test` (they run for minutes); run them after changing the engine, the saves or the UPI layer.

Usage:
    python3 tools/fuzz.py invariants ./solver --seconds 300   # EV sum, MES >= EV, finite, 1 vs 3 threads
    python3 tools/fuzz.py concurrency ./solver --seconds 300  # commands during solves: hold/release/stop
    python3 tools/fuzz.py roundtrip ./solver --seconds 300    # dump_tree / load_tree, lossless is exact
    python3 tools/fuzz.py preflop ./solver --seconds 300      # preflop trees: finite, MES >= EV, threads
    python3 tools/fuzz.py locks ./solver --seconds 300        # locked strategies stay put, MES >= EV
    python3 tools/fuzz.py edits ./solver --seconds 300        # eliminate_path, set_mes, rebuild_tree, ... between solves
    python3 tools/fuzz.py truncated ./solver --seconds 300    # no_rivers/no_turns saves: re-solved streets
    add --seed N to replay one case, --iso11 to force set_isomorphism 1 1 (invariants)

What each mode found when it was written (2026-11-05): invariants, flop isomorphism with a flop all-in
breaking suit symmetry and the EV sum (#300); concurrency, a lost wake-up that left a solve parked for
good (#294); roundtrip, set_union_hands not recorded in saves (#302). truncated (added later): nothing
in 2,127 cases on main; checked to fail with impossible tolerances, so its comparisons are live.
"""
import argparse
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time

RANKS, SUITS = "23456789TJQKA", "cdhs"
CLASSES = ["AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "22", "AKs", "AKo", "AQs", "AQo", "AJs", "KQs",
           "KQo", "QJs", "JTs", "T9s", "98s", "87s", "76s", "65s", "A5s", "A4s", "K9s", "Q9s", "J9s", "AJo", "KJo", "QJo",
           "ATo"]
RESULTS = re.compile(r"EV OOP: (\S+)\s+EV IP: (\S+)\s+OOP's MES: (\S+)\s+IP's MES: (\S+)\s+Exploitable for: (\S+)")


def run(exe, cmds, timeout=120):
    """Run a UPI script; return (returncode, responses split at END). Raises TimeoutExpired on a hang."""
    p = subprocess.run([exe], input="\n".join(["set_end_string END"] + cmds + ["exit"]) + "\n",
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.split("END\n")


def spot(rng, iso11=False):
    """A random small postflop spot: (setup commands, dead money)."""
    deck = [r + s for r in RANKS for s in SUITS]
    rng.shuffle(deck)
    board = "".join(deck[:rng.choice([3, 4, 4, 4] if iso11 else [3, 4, 5])])
    r0 = ",".join(rng.sample(CLASSES, rng.randint(2, 8)))
    r1 = ",".join(rng.sample(CLASSES, rng.randint(2, 8)))
    if rng.random() < 0.3:
        r0 += ",AA:0.5"
    pot = rng.choice([20, 40, 60, 100, 200])
    stack = rng.choice([10, 50, 100, 200, 500])
    s = max(1, min(rng.choice([pot // 4, pot // 2, pot, 2 * pot]), stack))
    lines = [f"{s}", f"0 {s}"]
    if s < stack:
        lines += [f"{s} {stack}", f"0 {s} {stack}"]
    if rng.random() < 0.5 and 2 * s < stack:
        lines.append(f"{s} {min(3 * s, stack)}")
    pre = []
    if rng.random() < 0.3:
        pre.append("set_union_hands on")
    if rng.random() < 0.3:
        pre.append("small_strats on")
    if iso11:
        pre.append("set_isomorphism 1 1")
    elif rng.random() < 0.3:
        pre.append(f"set_isomorphism {rng.randint(0, 1)} {rng.randint(0, 1)}")
    cmds = [f"set_range OOP {r0}", f"set_range IP {r1}", f"set_board {board}", f"set_pot 0 0 {pot}",
            f"set_eff_stack {stack}"] + [f"add_line {x}" for x in lines] + pre
    return cmds, pot


def results(parts):
    m = [RESULTS.search(b) for b in parts]
    m = [x for x in m if x]
    return tuple(float(v) for v in m[-1].groups()) if m else None


def check_results(r, dead, msgs, ev_sum=True):
    ev0, ev1, m0, m1, ex = r
    if not all(map(math.isfinite, r)):
        msgs.append(f"non-finite results {r}")
        return
    if ev_sum and abs(ev0 + ev1 - dead) > 1e-2 * max(1, dead / 100):
        msgs.append(f"EV OOP + EV IP = {ev0 + ev1:.4f}, dead money {dead}")
    if m0 < ev0 - 2e-3 or m1 < ev1 - 2e-3:
        msgs.append(f"MES < EV ({m0:.4f} / {ev0:.4f}, {m1:.4f} / {ev1:.4f})")
    if ex < -2e-3:
        msgs.append(f"negative exploitability {ex}")


# ------------------------------------------------------------------ modes: each returns (setup, messages)

def case_invariants(exe, rng, args):
    setup, pot = spot(rng, args.iso11)
    iters = rng.choice([5, 20, 60])
    tail = ["build_tree", "set_accuracy 0", f"go {iters} steps", "wait_for_solver", "calc_results"]
    rc, a = run(exe, ["set_threads 1"] + setup + tail)
    if rc != 0:
        return setup, [f"exit status {rc}"]
    if any(b.startswith("ERROR") for b in a):
        return None, []  # a spot the solver refuses (e.g. ranges blocked by the board)
    msgs = []
    r = results(a)
    if r is None:
        return setup, ["no calc_results answer"]
    check_results(r, pot, msgs)
    rc3, b = run(exe, ["set_threads 3"] + setup + tail)
    if rc3 != 0 or results(b) != r:
        msgs.append(f"1 vs 3 threads differ ({r} vs {results(b) if rc3 == 0 else rc3})")
    return setup, msgs


NODES = ["r:0", "r:0:c", "r:0:b50", "r:0:b30", "r:0:b20", "r:0:c:b50", "r:0:b20:c:2s", "r:0:c:b30"]
READS = ["show_strategy {n}", "calc_ev OOP {n}", "calc_ev IP {n}", "calc_results", "show_node {n}", "show_children {n}",
         "calc_eq_node IP {n}", "show_range OOP {n}", "explo_partial {n}", "calc_line_freq {n}", "show_iters",
         "show_tree_info", "is_ready", "show_strategy_pp {n}", "calc_global_freq {n}", "lock_node {n}",
         "unlock_node {n}", "show_all_freqs local", "set_accuracy 0", "set_info_freq 3", "set_info_freq auto",
         "set_accuracy 0.001 fraction", "set_accuracy 0", "set_dcfr 1.3 0 4", "set_avg_delay 32", "set_pruning on",
         "set_first_iteration_player OOP", "small_strats auto", "small_strats on", "set_end_string END"]
CTRL = ["go {k} steps", "go 0.05 seconds", "go_partial {n} {k} steps", "go_partial {n} 0.05 seconds", "stop", "hold",
        "release", "hold", "wait_for_solver"]
SMALL = [
    ["set_range OOP AA,KK,QQ,AK", "set_range IP AA,KK,JJ,AQ", "set_board KhQs7d4c2s", "set_pot 0 0 100",
     "set_eff_stack 100", "add_line 50 100", "add_line 0 50 100"],
    ["set_range OOP AA,KK,AKs", "set_range IP QQ,JJ,AQs", "set_board KhQs7d4c", "set_pot 0 0 60", "set_eff_stack 60",
     "add_line 30 60", "add_line 0 30"],
    ["set_range OOP AA,KK", "set_range IP QQ", "set_board Kh9h4c", "set_pot 0 0 40", "set_eff_stack 40", "add_line 20"],
]


def case_concurrency(exe, rng, args):
    def line():
        pool = CTRL if rng.random() < 0.3 else READS
        return rng.choice(pool).format(n=rng.choice(NODES), k=rng.choice([1, 3, 5, 20]))
    setup = [f"set_threads {rng.choice([1, 2, 4])}"] + rng.choice(SMALL) + ["build_tree", "go 5 steps"]
    setup += [line() for _ in range(60)] + ["release", "stop", "wait_for_solver", "is_ready"]
    script = ["set_end_string END"] + setup + (["exit"] if rng.random() < 0.5 else [])
    try:
        p = subprocess.run([exe], input="\n".join(script) + "\n", capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return setup, ["hang (20 s)"]
    if p.returncode != 0:
        return setup, [f"exit status {p.returncode}"]
    return setup, ([] if "is_ready ok!" in p.stdout else ["no is_ready answer"])


def case_roundtrip(exe, rng, args):
    setup, pot = spot(rng)
    pre = ["set_threads 2"] + setup + ["build_tree", "set_accuracy 0", f"go {rng.choice([5, 20])} steps",
                                       "wait_for_solver"]
    if rng.random() < 0.4:
        pre += ["lock_node r:0"]
    mode = rng.choice(["lossless", ""])
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "t.cfr")
        post = ["calc_results", "show_strategy r:0", f'dump_tree "{path}" {mode}'.strip(), "free_tree",
                f'load_tree "{path}"', "calc_results", "show_strategy r:0"]
        rc, parts = run(exe, pre + post)
    if rc != 0:
        return setup, [f"exit status {rc}"]
    head = parts[:len(pre) + 1]
    if any(b.startswith("ERROR") for b in head):
        return None, []
    errs = [b.strip()[:100] for b in parts[len(pre) + 1:] if b.startswith("ERROR")]
    if errs:
        return setup, [f"after dump: {errs[0]}"]
    crs = [b.strip() for b in parts if RESULTS.search(b)]
    strats = [[float(x) for x in b.split()] for b in parts
              if len(b.split()) >= 1326 and all(re.match(r"^[-0-9.e]+$", t) for t in b.split()[:3])]
    if len(crs) < 2 or len(strats) < 2:
        return setup, ["missing output"]
    dev = max(abs(x - y) for x, y in zip(strats[-2], strats[-1]))
    if mode == "lossless":
        return setup, ([] if crs[-2] == crs[-1] and dev <= 1e-6 else
                       [f"lossless round trip differs (strategy by {dev:.2e})"])
    # default saves quantize frequencies to 11 bits: a step of 1/2047 (~4.9e-4). Each hand's frequency moves
    # by up to half a step, and over a few decisions with big bets the EVs move by up to about one step of
    # the stack + pot (#388: 2e-4 was under half a step; a 500-chip-bet river spot moved 0.2 of 700)
    a, b = RESULTS.search(crs[-2]).groups(), RESULTS.search(crs[-1]).groups()
    stack = int(re.search(r"set_eff_stack (\d+)", " ".join(setup)).group(1))
    tol = (stack + pot) / 2047 + 1e-3
    bad = [i for i in (0, 1) if abs(float(a[i]) - float(b[i])) > tol] if dev <= 2e-3 else [0]
    return setup, ([f"default round trip too far: EVs {a[:2]} -> {b[:2]}, strategy by {dev:.2e}"] if bad else [])


PF_RANGES = ["22+,A2s+,K9s+,Q9s+,J9s+,T9s,ATo+,KJo+", "22+,A2s+,K5s+,Q8s+,J8s+,T8s+,97s+,87s,76s,A2o+,K9o+,QTo+,JTo",
             "AA,KK,QQ,AKs,AKo", "TT+,AQ+", "22+,A2+,K2+,Q2+,J2+,T2+,92+,82+,72+,62+,52+,42+,32"]


def case_preflop(exe, rng, args):
    stack = rng.choice([100, 200, 400, 1000])
    lines = set()
    for _ in range(rng.randint(1, 5)):
        cur = rng.choice([10, 15, 20, 25])
        line = [cur]
        while rng.random() < 0.6 and cur < stack:
            cur = min(stack, cur * rng.choice([2, 3, 4]) if rng.random() < 0.8 else stack)
            line.append(cur)
        if rng.random() < 0.5 and line[-1] < stack:
            line.append(line[-1])
        lines.add(" ".join(map(str, line)))
    setup = [f"set_range OOP {rng.choice(PF_RANGES)}", f"set_range IP {rng.choice(PF_RANGES)}", "set_pot 10 5 0",
             f"set_eff_stack {stack}"] + [f"add_preflop_line {x}" for x in sorted(lines)]
    tail = ["build_preflop_tree", "set_accuracy 0", "go 6 steps", "wait_for_solver", "calc_results"]
    rc, a = run(exe, ["set_threads 1"] + setup + tail, timeout=300)
    if rc != 0:
        return setup, [f"exit status {rc}"]
    r = results(a)
    if r is None:
        return None, []  # a line set the solver refuses
    msgs = []
    check_results(r, 0, msgs)  # set_pot 10 5 0: no dead money, the blinds are the players' own chips
    rc3, b = run(exe, ["set_threads 3"] + setup + tail, timeout=300)
    if rc3 != 0 or results(b) != r:
        msgs.append("1 vs 3 threads differ")
    return setup, msgs


def case_locks(exe, rng, args):
    setup, pot = spot(rng)
    base = setup + ["build_tree", "set_accuracy 0", "go 10 steps", "wait_for_solver"]
    rc, parts = run(exe, ["set_threads 1"] + base + ["show_all_lines"])
    if rc != 0:
        return setup, [f"exit status {rc}"]
    blocks = [b for b in parts if any(x.startswith("r:0") for x in b.split())]
    if not blocks:
        return None, []
    decs = [x for x in blocks[-1].split() if x.startswith("r:0") and "?" not in x][:12]
    rc, parts = run(exe, ["set_threads 1"] + base + [f"show_node {d}" for d in decs])
    cand = []
    for d, b in zip(decs, parts[-len(decs) - 1:-1]):
        m = re.search(r"(\d+) children", b)
        if m and int(m.group(1)) >= 2 and ("OOP_DEC" in b or "IP_DEC" in b):
            cand.append((d, int(m.group(1))))
    if not cand:
        return None, []
    node, A = rng.choice(cand)
    kind = rng.choice(["set", "solved", "combo"])
    if kind == "set":
        lock = [f"set_strategy {node} " + " ".join(["1"] * 1326 + ["0"] * (1326 * (A - 1))), f"lock_node {node}"]
    elif kind == "solved":
        lock = [f"lock_node {node}"]
    else:
        lock = [f"combo_lock_node {node} " + " ".join(rng.choice("01") for _ in range(1326))]
    after = ["go 15 steps", "wait_for_solver", f"show_strategy {node}", "calc_results"]
    outs = {}
    for th in (1, 3):
        rc, parts = run(exe, [f"set_threads {th}"] + base + [f"show_strategy {node}"] + lock + after)
        if rc != 0:
            return setup + lock, [f"exit status {rc} with {th} threads"]
        strat = [[float(x) for x in b.split()] for b in parts
                 if len(b.split()) == 1326 * A and all(re.match(r"^[-0-9.e]+$", t) for t in b.split()[:3])]
        outs[th] = (strat, results(parts), any(b.startswith("ERROR") for b in parts))
    strat, r, err = outs[1]
    if err or len(strat) < 2 or r is None:
        return None, []
    msgs = []
    before, aft = strat[0], strat[-1]
    if kind == "set":
        live = [i for i in range(1326) if any(before[a * 1326 + i] > 0 for a in range(A))]
        dev = max((abs(aft[a * 1326 + i] - (1.0 if a == 0 else 0.0)) for i in live for a in range(A)), default=0)
        if dev > 1e-3:
            msgs.append(f"set_strategy + lock_node: the strategy moved by {dev:.4f}")
    elif kind == "solved":
        dev = max((abs(x - y) for x, y in zip(before, aft)), default=0)
        if dev > 2e-3:
            msgs.append(f"lock_node as solved: the strategy moved by {dev:.4f}")
    check_results(r, pot, msgs, ev_sum=False)
    if outs[3][1] != r:
        msgs.append("1 vs 3 threads differ")
    return setup + lock, msgs


def case_edits(exe, rng, args):
    """Strategy edits between solves (#421-#429): eliminate_path, set_mes, set_equal_strats, rebuild_tree,
    lock_node / unlock_node, round_up_to, in random order. No crash or unexpected error, sane results,
    and 1 and 3 threads give the same answers."""
    setup, pot = spot(rng)
    base = setup + ["build_tree", "set_accuracy 0", "go 10 steps", "wait_for_solver"]
    rc, parts = run(exe, ["set_threads 1"] + base + ["show_all_lines"])
    if rc != 0:
        return setup, [f"exit status {rc}"]
    blocks = [b for b in parts if any(x.startswith("r:0") for x in b.split())]
    if not blocks:
        return None, []
    lines = [x for x in blocks[-1].split() if x.startswith("r:0") and "?" not in x]
    if not lines:
        return None, []
    ops = []
    for _ in range(rng.randint(2, 4)):
        k = rng.choice(["elim", "mes", "equal", "rebuild", "lock", "round"])
        line = rng.choice(lines)
        parts_ = line.split(":")
        if k == "elim" and len(parts_) > 2:
            ops.append(f"eliminate_path {':'.join(parts_[:rng.randint(3, len(parts_))])}")
        elif k == "mes":
            ops.append(f"set_mes {rng.choice(['OOP', 'IP'])}")
        elif k == "equal":
            ops.append("set_equal_strats")
        elif k == "rebuild":
            ops.append("rebuild_tree")
        elif k == "lock":
            node = ":".join(parts_[:rng.randint(2, max(2, len(parts_) - 1))])
            ops += [f"lock_node {node}", "go 5 steps", "wait_for_solver", f"unlock_node {node}"]
        elif k == "round":
            ops.append(f"round_up_to {rng.choice(['OOP', 'IP'])} {rng.choice([2, 4, 10])} {rng.choice(['flop', 'turn', 'river'])}")
        ops += ["go 8 steps", "wait_for_solver"]
    tail = ops + ["calc_results"]
    outs = {}
    for th in (1, 3):
        rc, parts = run(exe, [f"set_threads {th}"] + base + tail)
        if rc != 0:
            return setup + ops, [f"exit status {rc} with {th} threads"]
        # an edit on a path that isn't a bet, raise or check, or isn't in the tree, answers an error: fine
        errs = [b for b in parts if b.startswith("ERROR") and "eliminate_path" not in b and "incorrect node" not in b
                and "incorrect path" not in b]
        outs[th] = (results(parts), errs)
    r, errs = outs[1]
    msgs = [f"unexpected error: {e[:120]}" for e in errs[:2]]
    if r is None:
        return (setup + ops, msgs) if msgs else (None, [])
    check_results(r, pot, msgs, ev_sum=False)
    if outs[3][0] != r:
        msgs.append(f"1 vs 3 threads differ: {r} vs {outs[3][0]}")
    return setup + ops, msgs


def floats(b):
    t = b.split()
    return [float(x) for x in t] if len(t) >= 1326 and all(re.match(r"^[-0-9.e]+$|^nan$|^-?inf$", x) for x in t[:3]) else None


def case_truncated(exe, rng, args):
    """dump_tree no_rivers / no_turns, load it back, browse the missing street. Ranges there depend only on the
    saved streets, so they must match the full tree (within save quantization); the re-solved strategies must be
    distributions; the loaded root must say INCOMPLETE_TREE; and the re-solve must not depend on the thread count."""
    setup, pot = spot(rng)
    board_len = len(re.search(r"set_board (\S+)", " ".join(setup)).group(1)) // 2
    if board_len == 5:
        return None, []
    stack = int(re.search(r"set_eff_stack (\d+)", " ".join(setup)).group(1))
    s = max(1, min(pot // 2, stack))
    setup += [f"add_line 0 0 {s}", f"add_line {s} {s} {min(stack, 3 * s)}"]  # bets on the next street too
    trunc = "no_rivers" if board_len == 4 or rng.random() < 0.6 else "no_turns"
    need = (5 if trunc == "no_rivers" else 4) - board_len  # deal tokens up to the missing street
    pre = setup + ["build_tree", "set_accuracy 0", f"go {rng.choice([10, 30])} steps", "wait_for_solver"]
    rc, a = run(exe, ["set_threads 2"] + pre + ["show_all_lines"], timeout=300)
    if rc != 0:
        return setup, [f"exit status {rc}"]
    if any(b.startswith("ERROR") for b in a):
        return None, []
    nodes = set()
    for line in a[-2].split():
        toks = line.split(":")
        q = [i for i, t in enumerate(toks) if t == "?"]
        if len(q) >= need:
            nodes.add(":".join(toks[:q[need - 1] + 1]))
    if not nodes:
        return None, []
    nodes = rng.sample(sorted(nodes), min(3, len(nodes)))
    rng_reads = [f"show_range {p} {n}" for n in nodes for p in ("OOP", "IP")]
    msgs = []
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "t.cfr")
        rc, full = run(exe, ["set_threads 2"] + pre + rng_reads + [f'dump_tree "{path}" {trunc}'], timeout=300)
        if rc != 0:
            return setup, [f"exit status {rc} (full)"]
        browse = ["show_node r"] + rng_reads + [f"show_strategy {n}" for n in nodes] + [f"show_node {n}" for n in nodes]
        outs = {}
        for th in ((1, 3) if rng.random() < 0.3 else (2,)):
            rc, b = run(exe, [f"set_threads {th}", f'load_tree "{path}"'] + browse + ["calc_results"], timeout=300)
            if rc != 0:
                return setup + [trunc], [f"exit status {rc} after load"]
            outs[th] = b
    # Index answers from the end: the solve's progress reports are extra END-terminated blocks earlier on.
    # The last part is what follows the final END (empty).
    full = full[-(len(rng_reads) + 2):-1]
    b = outs[min(outs)]
    errs = [x.strip()[:100] for x in b[1:] if x.startswith("ERROR")]
    if errs:
        return setup + [trunc], [f"after load: {errs[0]}"]
    b = b[-(len(browse) + 2):-1]  # browse answers, then calc_results
    want = [floats(x) for x in full[:len(rng_reads)]]
    got = [floats(x) for x in b[1:1 + len(rng_reads)]]
    if "INCOMPLETE_TREE" not in b[0]:
        msgs.append("loaded root lacks INCOMPLETE_TREE")
    for read, w, g in zip(rng_reads, want, got):
        if w is None or g is None:
            msgs.append(f"{read}: no 1326 values")
            continue
        dev = max(abs(x - y) for x, y in zip(w, g))
        if dev > 4e-3:
            msgs.append(f"{read}: range differs from the full tree by {dev:.2e}")
    k = 1 + len(rng_reads)
    for n, st in zip(nodes, b[k:k + len(nodes)]):
        rows = [[float(v) for v in l.split()] for l in st.strip().split("\n") if l.strip()]
        if not rows or any(len(r) != 1326 for r in rows):
            msgs.append(f"show_strategy {n}: bad shape")
            continue
        for h in range(1326):
            col = [r[h] for r in rows]
            if any(not math.isfinite(v) or v < -1e-6 or v > 1 + 1e-6 for v in col):
                msgs.append(f"show_strategy {n}: hand {h} has {col}")
                break
            if abs(sum(col) - 1) > 1e-3 and sum(col) > 1e-6:
                msgs.append(f"show_strategy {n}: hand {h} sums to {sum(col):.4f}")
                break
    for n, nd in zip(nodes, b[k + len(nodes):k + 2 * len(nodes)]):
        if "ALREADY_RECALCED" not in nd:
            msgs.append(f"show_node {n}: no ALREADY_RECALCED after browsing")
    r = results(b)
    if r is None:
        msgs.append("no calc_results answer after load")
    elif not all(map(math.isfinite, r)):
        msgs.append(f"non-finite results after load {r}")
    if len(outs) == 2 and outs[1][2:] != outs[3][2:]:  # all but the banner and set_threads answers
        msgs.append("re-solve differs between 1 and 3 threads")
    return setup + [trunc], msgs


MODES = {"invariants": case_invariants, "concurrency": case_concurrency, "roundtrip": case_roundtrip,
         "preflop": case_preflop, "locks": case_locks, "truncated": case_truncated,
         "edits": case_edits}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("solver")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--seed", type=int, help="run this one case only")
    ap.add_argument("--iso11", action="store_true", help="invariants: set_isomorphism 1 1 in every spot")
    args = ap.parse_args(argv)
    case = MODES[args.mode]
    t0, n, failed = time.time(), 0, 0
    while True:
        seed = args.seed if args.seed is not None else random.randrange(1 << 30)
        try:
            setup, msgs = case(args.solver, random.Random(seed), args)
        except subprocess.TimeoutExpired:
            setup, msgs = ["(see --seed)"], ["hang"]
        if setup is not None:
            n += 1
            if msgs:
                failed += 1
                print(f"FAIL seed {seed}: {'; '.join(msgs)}\n    {' ; '.join(setup)[:600]}", flush=True)
        if args.seed is not None or time.time() - t0 >= args.seconds:
            break
    print(f"{args.mode}: {n} cases, {failed} failing", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
