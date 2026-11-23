#!/usr/bin/env python3
"""Tests for tools/preflop.py, heads-up preflop solves (#488).  usage: python3 tests/test_preflop.py [./solver]"""

import io
import json
import os
import random
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
import preflop  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def settings(*opts):
    return preflop.settings_from(preflop.parser().parse_intermixed_args(["tree", *opts]))


def refused(*opts, mention=""):
    """The settings are refused, with a message that names what is wrong."""
    try:
        preflop.build_tree(settings(*opts))
    except preflop.PreflopError as e:
        return mention in str(e)
    return False


def run(*argv):
    """The command line: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = preflop.main(list(argv))
    return status, out.getvalue(), err.getvalue()


def decisions(root):
    return [n for n in preflop.walk(root) if n.kind == "decision"]


def unit():
    print("sizes and the preflop tree")
    kinds = [(x.kind, str(x.value)) for x in preflop.parse_sizes("2.5, 3bb 4x allin", "open")]
    check(kinds == [("bb", "2.5"), ("bb", "3"), ("x", "4"), ("allin", "0")], f"sizes: to an amount, times the bet, all-in {kinds}")
    bad = 0
    for text in ("abc", "0", "-2", "x", "2.5.1"):
        try:
            preflop.parse_sizes(text, "open")
        except preflop.PreflopError:
            bad += 1
    check(bad == 5, "a size that is not a positive number is refused")

    root = preflop.build_tree(settings())
    ids = [n.id for n in decisions(root)]
    check(ids == ["r:0", "r:0:b250", "r:0:b250:b1000", "r:0:b250:b1000:b2300", "r:0:b250:b1000:b2300:b10000",
                  "r:0:c", "r:0:c:b350", "r:0:c:b350:b1400", "r:0:c:b350:b1400:b3220",
                  "r:0:c:b350:b1400:b3220:b10000"],
          f"100 bb, default sizes: raise 2.5 / 3-bet 4x / 4-bet 2.3x / all-in, and the limp with its raises ({len(ids)} decisions)")
    check([label for label, _ in root.children] == ["raise 2.5", "limp", "fold"]
          and [c.kind for _, c in root.children] == ["decision", "decision", "fold"],
          "the small blind's first decision: raises, then the limp, then the fold (the solver's order)")
    limped = dict(root.children)["limp"]
    check([label for label, _ in limped.children] == ["raise 3.5", "check"] and limped.actor == "BB",
          "after the limp the big blind raises or checks; it has nothing to fold to")
    exits = [n for n in preflop.walk(root) if n.kind == "flop"]
    allins = [n for n in preflop.walk(root) if n.kind == "allin"]
    check(len(exits) == 7 and all(n.inv[0] == n.inv[1] < 10000 for n in exits)
          and len(allins) == 2 and all(n.inv == (10000, 10000) for n in allins),
          "7 flop exits with both players in for the same amount, 2 called all-ins for the stacks")
    lines = sorted(" ".join(map(str, n.line)) for n in exits + allins)
    check(lines == sorted(["100 100", "250 250", "250 1000 1000", "250 1000 2300 2300", "250 1000 2300 10000 10000",
                           "100 350 350", "100 350 1400 1400", "100 350 1400 3220 3220",
                           "100 350 1400 3220 10000 10000"]),
          "add_preflop_line: whole investments with the blinds, each line ending on its call")
    check(dict(root.children)["raise 2.5"].aggressor == "SB" and exits[0].aggressor in ("SB", "BB")
          and [n for n in exits if n.line == (100, 100)][0].aggressor is None,
          "an exit knows who raised last (nobody after limp, check)")

    r2 = preflop.build_tree(settings("--no-limp"))
    check([label for label, _ in r2.children] == ["raise 2.5", "fold"] and len(decisions(r2)) == 5,
          "--no-limp: raise or fold")
    text = preflop.script(settings("--no-limp"), [("Ks5h2s", 1.0)])
    check("remove_preflop_line 100" in text and "add_preflop_line 100 100" not in text,
          "--no-limp takes the limp the solver adds by itself out again")
    r3 = preflop.build_tree(settings("--open", "2,3", "--3bet", "3x,4x"))
    check([label for label, _ in r3.children] == ["raise 3", "raise 2", "limp", "fold"]
          and [label for label, _ in r3.children[1][1].children][:2] == ["raise 8", "raise 6"],
          "several sizes: every one a branch, the largest first")
    check([label for label, _ in preflop.build_tree(settings("--open", "2.5,2.50,5x")).children] ==
          ["raise 5", "raise 2.5", "limp", "fold"] and
          [label for label, _ in preflop.build_tree(settings("--open", "2.5,2.5x")).children] ==
          ["raise 2.5", "limp", "fold"], "two sizes that come to the same amount are one branch")

    short = preflop.build_tree(settings("--stack", "20"))
    words = [n.said[-1] for n in decisions(short) if n.said]
    check("BB raise 10" in words and "SB all-in" in words and "SB raise 23" not in words,
          "20 bb: the 3-bet to 10 is half the stack and stays, the 4-bet to 23 is over it and is all-in")
    check([label for label, _ in preflop.build_tree(settings("--stack", "20", "--allin-threshold", "40")).children[0][1]
           .children] == ["all-in", "call", "fold"], "--allin-threshold 40: a raise to half the stack becomes all-in")
    check([label for label, _ in preflop.build_tree(settings("--stack", "8", "--open", "allin", "--no-limp")).children]
          == ["all-in", "fold"], "push or fold")

    print("what is refused")
    check(refused("--open", "1.5", mention="--open 1.5"), "an open to 1.5 big blinds is under the minimum raise, and --open is named")
    check(refused("--3bet", "1.5x", mention="--3bet 1.5x"), "a 3-bet of 1.5 times the open likewise (--3bet)")
    check(refused("--4bet", "1.2x", mention="--4bet"), "and a 4-bet that small (--4bet)")
    check(not refused("--open", "2") and not refused("--3bet", "1.6x", "--open", "2.5", "--no-limp"),
          "the minimum raise itself is allowed (to 2 big blinds; 2.5 -> 4)")
    check(refused("--chips-per-bb", "25", mention="even"), "an odd number of chips in a big blind (no small blind)")
    check(refused("--stack", "700", mention="--chips-per-bb") and not refused("--stack", "700", "--chips-per-bb", "20"),
          "a stack over the solver's 65535 chips, with the way out")
    check(refused("--stack", "1") and refused("--no-limp", "--open", ""), "a stack of one blind; neither limp nor raise")
    for opts in (("--stack", "x"), ("--ante", "-1"), ("--flop-bet", "abc")):
        try:
            s = settings(*opts)
            preflop.script(s, [("Ks5h2s", 1.0)])
            check(False, f"{opts} refused")
        except preflop.PreflopError:
            check(True, f"{' '.join(opts)} refused")

    print("the postflop tree of an exit, in whole-hand amounts")
    s = settings()
    cfg = preflop.postflop_config(s)
    by_line = {n.line: n for n in exits}
    got = preflop.exit_lines(s, cfg, by_line[(250, 250)])
    check(got == [[250, 500, 1100]],
          f"raise 2.5, call (5 bb in the pot): no lead, bet half the pot = to 5, raise 60 % = to 11 {got}")
    got = preflop.exit_lines(s, cfg, by_line[(100, 100)])
    check(got == [[100, 200, 440], [200, 440]],
          f"limp, check (2 bb in the pot): nobody raised, so the big blind may bet 1 = to 2, or check to the same bet {got}")
    got = preflop.exit_lines(s, cfg, by_line[(250, 1000, 1000)])
    check(got == [[1000, 2000, 4400, 10000], [2000, 4400, 10000]],
          f"3-bet pot (20 bb): the big blind raised last and may bet 10 = to 20; raises to 44, then all-in {got}")
    sa = settings("--ante", "0.5")
    got = preflop.exit_lines(sa, preflop.postflop_config(sa), by_line[(250, 250)])
    check(got == [[250, 525, 1185]], f"with half a blind of dead money the pot is 5.5: bet to 5.25, raise to 11.85 {got}")
    ok = True
    count = 0
    for opts in ((), ("--depth", "river", "--raise-cap", "1"), ("--depth", "turn", "--donk", "33"),
                 ("--stack", "20", "--depth", "turn"), ("--ante", "1", "--open", "2,3")):
        sx = settings(*opts)
        cx = preflop.postflop_config(sx)
        for n in preflop.walk(preflop.build_tree(sx)):
            if n.kind != "flop":
                continue
            for line in preflop.exit_lines(sx, cx, n):
                count += 1
                ok &= line[0] >= n.inv[0] and all(a <= b for a, b in zip(line, line[1:])) and line[-1] <= sx.stack_chips
                ok &= line[-1] > n.inv[0]
    check(ok and count > 50, f"{count} lines of 5 settings: start at what is in already, never go down, never pass the stack")
    check(preflop.exit_lines(settings("--depth", "none"), preflop.postflop_config(settings("--depth", "none")),
                             by_line[(250, 250)]) == [], "--depth none: no lines, a check-down")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "post.txt")
        with open(path, "w") as f:
            f.write("#Pot#55\n#EffectiveStacks#975\n#FlopConfig.BetSize#50\n#FlopConfigIP.BetSize#33, 75\n"
                    "#FlopConfigIP.RaiseSize#60\n#AllinThreshold#67\n")
        sp = settings("--postflop", path)
        got = preflop.exit_lines(sp, preflop.postflop_config(sp), by_line[(250, 250)])
        check(sorted({line[1] for line in got}) == [415, 625] and all(line[0] == 250 for line in got),
              f"--postflop FILE: the file's sizes (33 % and 75 % of 5 bb: to 4.15 and 6.25), not its pot and stack {got}")
        check(run("script", "--postflop", os.path.join(d, "none.txt"))[0] == 1, "a missing --postflop file is an error")
    lines = preflop.script(s, [("Ks5h2s", 15.76), ("Ts7h2s", 13.3)])
    at = lines.index("add_schematic_tree r:0:b250:c")
    check(lines[at - 4:at] == ["clear_lines", "set_pot 250 250 0", "set_eff_stack 10000", "add_line 250 500 1100"]
          and lines[:4] == [f"set_range OOP {preflop.ALL_HANDS}", f"set_range IP {preflop.ALL_HANDS}",
                            "set_pot 100 50 0", "set_eff_stack 10000"]
          and "add_to_subset 15.76 Ks 5h 2s" in lines and "set_board Ks5h2s" in lines
          and sum(l.startswith("add_schematic_tree") for l in lines) == 7,
          "the script: blinds as the pot, the subset, then per exit its pot, the whole stack and its lines")

    print("flops")
    flops = preflop.all_flops()
    names = {f for f, _ in flops}
    check(len(flops) == 1755 and len(names) == 1755 and sum(w for _, w in flops) == 22100
          and {w for _, w in flops} == {4, 12, 24},
          "all flops: 1,755 suit classes of 4, 12 or 24 flops, 22,100 together")
    shipped = preflop.read_flop_file(preflop.flop_path(200))
    check(len(shipped) == 200 and all(f in names for f, _ in shipped) and abs(sum(w for _, w in shipped) - 100) < 0.01,
          "the 200 shipped flops are named the way all_flops names a class")
    with tempfile.TemporaryDirectory() as d:
        def reads(text):
            path = os.path.join(d, "f.txt")
            with open(path, "w") as f:
                f.write(text)
            try:
                return preflop.read_flop_file(path)
            except preflop.PreflopError:
                return None
        check(reads("Ks5h2s:1.5\n# note\n\nAh 7d 2c: 2 \nQsJs4d\nTs9s8s:0\n") ==
              [("Ks5h2s", 1.5), ("Ah7d2c", 2.0), ("QsJs4d", 1.0)],
              "a flop file: weights, comments, spaces, no weight, weight 0 left out")
        check(all(reads(t) is None for t in ("Ks5h2s:abc\n", "Ks5h2s:\n", "Ks5h2s:-1\n", "Ks5h2s:inf\n", "Ks5h2s:nan\n",
                                             "Ks5h:1\n", "Ks5h2x:1\n", "KsKs2d:1\n", "# nothing\n")),
              "a weight that is not a number, a flop that is not one, and a file without flops are refused")
        check(reads("") is None and run("script", "--flops", os.path.join(d, "missing.txt"))[0] == 1
              and run("script", "--flops", "7")[0] == 1, "a missing flop file or a size we do not ship is an error")

    print("results and charts")
    check([preflop.hand_class(h) for h in ("AhKh", "KdAs", "7c7d", "2s3s")] == ["AKs", "AKo", "77", "32s"]
          and (preflop.cell_class(0, 0), preflop.cell_class(0, 1), preflop.cell_class(1, 0), preflop.cell_class(12, 12))
          == ("AA", "AKs", "AKo", "22"), "hand classes; suited above the diagonal")
    rng = random.Random(488)
    cards = [r + x for r in "AKQJT9" for x in "shdc"]
    hands = [a + b for i, a in enumerate(cards) for b in cards[i + 1:]]
    wa, wb = [rng.random() for _ in hands], [rng.random() if rng.random() < 0.7 else 0.0 for _ in hands]
    brute = [sum(y for g, y in zip(hands, wb) if len({h[0:2], h[2:4], g[0:2], g[2:4]}) == 4) for h in hands]
    facing = preflop._facing(hands, wb)
    check(max(abs(x - y) for x, y in zip(facing, brute)) < 1e-9 and sum(x * y for x, y in zip(wa, facing)) > 0,
          f"the opponent's hands that share no card with a hand: by card sums against hand by hand ({len(hands)} hands)")
    # collect, on canned answers: the big blind holds AhAs only, so of the small blind's AKs two combos
    # are live (they fold here) and two cannot be dealt (they would push, with no EV)
    s = settings("--no-limp", "--open", "allin", "--stack", "10")
    root = preflop.build_tree(s)
    deck = [r + x for r in "23456789TJQKA" for x in "cdhs"]
    order = [b + a for i, a in enumerate(deck) for b in deck[i + 1:]]
    live = [not {"Ah", "As"} & {h[:2], h[2:]} for h in order]
    push = [0.0 if h in ("AdKd", "KdAd", "AcKc", "KcAc") else 1.0 for h in order]
    by_id = {n.id: n for n in preflop.walk(root)}
    push_id = root.children[0][1].id

    class Canned:
        def cmd(self, line, on_report=None):
            word, *rest = line.split()
            text = lambda row: [" ".join(str(x) for x in row)]  # noqa: E731
            if word == "show_hand_order":
                return [" ".join(order)]
            if word == "show_children":
                return [child.id for _, child in by_id[rest[0]].children]
            if word == "show_strategy":
                return text(push) + text([1 - x for x in push]) if rest[0] == "r:0" else text([1.0] * 1326) + text([0.0] * 1326)
            if word == "show_range":
                if rest[0] == "OOP":
                    return text([1.0 if {h[:2], h[2:]} == {"Ah", "As"} else 0.0 for h in order])
                return text([1.0] * 1326 if rest[1] == "r:0" else push)
            if word == "calc_ev":
                return text([50.0 if ok else float("nan") for ok in live])
            raise AssertionError(line)
    got = {n["id"]: n for n in preflop.collect(Canned(), s, root)}
    aks, top = got["r:0"]["hands"]["AKs"], got[push_id]
    check(aks["freq"] == [0.0, 1.0] and aks["ev_bb"] == 0.5 and abs(top["reach"] - 1223 / 1225) < 1e-12
          and abs(got["r:0"]["freq"][0] - 1223 / 1225) < 1e-12 and set(top["hands"]) == {"AA"}
          and len(got["r:0"]["hands"]) == 169 and got["r:0"]["hands"]["AA"]["ev_bb"] == 0.5,
          "a combo the opponent's cards rule out counts for nothing: AKs is its two live combos, the push is "
          "1,223 of the 1,225 deals, the EVs skip the hands that cannot be dealt")
    node = {"id": "r:0", "actor": "SB", "line": [], "pot_bb": [0.5, 1], "actions": ["raise 2.5", "limp", "fold"],
            "reach": 1.0, "freq": [0.5, 0.25, 0.25],
            "hands": {"AA": {"reach": 1, "freq": [1.0, 0.0, 0.0], "ev_bb": 9.234},
                      "AKs": {"reach": 1, "freq": [0.004, 0.996, 0.0], "ev_bb": -0.5},
                      "72o": {"reach": 0, "freq": [0.0, 0.0, 0.0], "ev_bb": None},
                      "32s": {"reach": 1, "freq": [0.5, 0.074, 0.426], "ev_bb": -12.0}}}
    result = {"settings": {"stack_bb": "100"}, "flops": "f", "flop_count": 3, "ev_sb_bb": 0.02, "ev_bb_bb": -0.02,
              "exploitable_bb100": 0.5, "nodes": [node]}
    out = preflop.chart_node(node, 132)
    a_row, two_row, three_row = [[l for l in out if l.startswith(r + " ")][0] for r in "A23"]
    check(a_row[2:4] == "**" and a_row[45:47] == " ." and a_row[5:7] == " ." and a_row[48:50] == "**",
          "cells: ** always, . never or under half a percent (AA raises; AKs limps 99.6 %)")
    check(two_row.ljust(132)[2 + 3 * 7:4 + 3 * 7] == "  " and three_row[2 + 3 * 12:4 + 3 * 12] == "50"
          and three_row[43 + 2 + 3 * 12:43 + 4 + 3 * 12] == " 7" and three_row[86 + 2 + 3 * 12:86 + 4 + 3 * 12] == "43",
          "a hand that never gets here is blank (72o); 32s shows 50 / 7 / 43")
    check(all(len(l) <= 132 for l in out) and sum(l.startswith("A ") for l in out) == 1
          and out[3].startswith("raise 2.5 (50.0 %)") and "fold (25.0 %)" in out[3]
          and sum(l.startswith("A ") for l in preflop.chart_node(node, 80)) == 3,
          "three charts side by side in 132 columns, one under the other in 80")
    ev = [l for l in preflop.chart_node(node, 132, ev=True) if l.startswith("A ") and "+9.23" in l]
    check(len(ev) == 1 and "-0.50" in ev[0] and any("-12.0" in l for l in preflop.chart_node(node, 132, ev=True)),
          "--ev: a chart of EVs in big blinds, signed")
    try:
        preflop.charts(result, ["r:9"])
        check(False, "an unknown node is refused")
    except preflop.PreflopError as e:
        check("r:0" in str(e), "chart: a node the result lacks is refused, with the ones it has")
    tree = "\n".join(preflop.tree_text(settings(), preflop.build_tree(settings())))
    check("r:0:b250:c" in tree and "flop" in tree and "all-in" in tree, "tree: the lines in words with their node ids")

    print("reading a result for a check")
    check(preflop._same("2.50 4x allin", "2.5 4.0x allin") and preflop._same("12.0", "12") and preflop._same(True, True)
          and not preflop._same("2.5", "2.5x") and not preflop._same("2.5", "3") and not preflop._same("2.5", "2.5 3")
          and not preflop._same(None, "2.5"), "settings compare as numbers: 2.50 is 2.5, 12.0 is 12, 2.5x is not 2.5")
    solved = {"EV OOP": -3.0, "EV IP": 3.5, "OOP's MES": -2.0, "IP's MES": 4.0}

    def near(got, want):
        return len(got) == len(want) and all(abs(x - y) < 1e-9 for x, y in zip(got, want))
    check(near(preflop._lost([1.0, 0.9, 1.2], [0.5, 0.45, 0.6]), [0.5, 0.3, 0.75])
          and near(preflop._value(solved, "SB", 100), [3.5, 2.5, 4.0])
          and near(preflop._value(solved, "BB", 100), [-3.0, -3.5, -2.0]) and near(preflop._value(solved, "BB", 50), [-6, -7, -4]),
          "a value's span: the player's best response above, the sum less the opponent's below; a loss's: the "
          "least and the most the two values' spans allow")
    known = preflop._class_rows("x.json", {"id": "r:0", "hands": {"AA": {"freq": [2, 2, 0]}, "KK": {"freq": [0.0, 0, 0]}}}, 3)
    check(known == {"AA": [0.5, 0.5, 0.0]}
          and preflop.locked_rows(known, 3, ["AA", "KK", "AA"]) == [[0.5, 1 / 3, 0.5], [0.5, 1 / 3, 0.5], [0.0, 1 / 3, 0.0]],
          "a class's frequencies are scaled to 1 and given to each of its combos; one the result did not bring plays evenly")
    bad = 0
    for hand in ({"freq": 5}, {"freq": True}, {"freq": [0.5, 0.5]}, {"freq": [0.5, "x", 0]}, {"freq": [1.5, -0.5, 0]},
                 {"freq": [float("nan"), 0, 1]}, {"freq": [1e308, 1e308, 1e308]}, {"freq": None}, {}, [1, 0, 0]):
        try:
            preflop._class_rows("x.json", {"id": "r:0", "hands": {"AA": hand}}, 3)
        except preflop.PreflopError as e:
            bad += "x.json: the frequencies of AA at r:0 are not a strategy" in str(e)
    check(bad == 10, f"frequencies that are no strategy are refused, by file, hand and node ({bad} of 10)")

    s = settings("--stack", "12", "--depth", "none")
    root = preflop.build_tree(s)
    nodes = decisions(root)
    classes, dealt = ["AA", "KK", "AKs", "AA"], {"SB": [1, 1, 1, 0], "BB": [1, 1, 1, 1]}

    def made(change=None):
        result = {"nodes": [{"id": n.id, "actions": [label for label, _ in n.children],
                             "hands": {c: {"freq": [1.0 / len(n.children)] * len(n.children)} for c in set(classes)}}
                            for n in nodes]}
        if change:
            change({n["id"]: n["hands"] for n in result["nodes"]})
        return preflop._plan("x.json", result, root, nodes, classes, dealt, dealt, 12.0)

    def silent(hands):
        for h in hands.values():
            h["freq"] = [0.0] * len(h["freq"])

    def refusal(change):
        try:
            made(change)
        except preflop.PreflopError as e:
            return str(e)
        return ""
    plan = made()
    check(all(sorted(plan[who]["rows"]) == sorted(n.id for n in nodes if n.actor == who) and plan[who]["free"] == []
              for who in ("SB", "BB")), "a result with every hand at every decision: all of a player's decisions are locked")
    first_bb = [child for _, child in root.children if child.kind == "decision"]
    first_bb.sort(key=lambda top: -sum(preflop._under(top, n) for n in nodes))  # the limp: a decision of each under it
    inner = [n for n in nodes if n.actor == "BB" and n not in first_bb and preflop._under(first_bb[0], n)]
    plan = made(lambda h: silent(h[first_bb[0].id]))
    check(len(first_bb) >= 2 and inner and plan["BB"]["free"] == [first_bb[0].id] and first_bb[0].id not in plan["BB"]["rows"]
          and inner[0].id in plan["BB"]["rows"] and first_bb[1].id in plan["BB"]["rows"] and plan["SB"]["free"] == [],
          "a decision the opponent never brought the player to is left free, not played at random; the rest is locked")
    plan = made(lambda h: h[root.id].pop("AA"))  # the small blind has one of the two combos of AA here
    check(plan["SB"]["free"] == plan["SB"]["part"] == [root.id] and root.id not in plan["SB"]["rows"]
          and all(n.id in plan["SB"]["rows"] for n in nodes if n.actor == "SB" and n is not root),
          "so is a first decision without a hand class the range holds in part: no opponent may have left it possible")
    later = next(n for n in nodes if n.actor == "SB" and n is not root)

    def both(h):
        h[root.id].pop("AA")
        silent(h[later.id])
    plan = made(both)
    check(plan["SB"]["free"] == plan["SB"]["part"] == [root.id, later.id],
          "a decision with nothing below one left free like that is not said to be one the opponent never goes to")
    step = next(a for a, (_, child) in enumerate(first_bb[0].children) if child is inner[0] or preflop._under(child, inner[0]))

    def rarely(often):
        def change(h):
            h[first_bb[0].id]["KK"]["freq"] = [often if a == step else (1 - often) / (len(first_bb[0].children) - 1)
                                              for a in range(len(first_bb[0].children))]
            h[inner[0].id].pop("KK")
        return change
    plan, seldom = made(rarely(0.5)), made(rarely(1e-9))
    check(plan["BB"]["free"] == plan["BB"]["part"] == [inner[0].id] and inner[0].id not in plan["BB"]["rows"]
          and plan["BB"]["unsaid_bb100"] == 0
          and seldom["BB"]["free"] == [] and inner[0].id in seldom["BB"]["rows"] and 0 < seldom["BB"]["unsaid_bb100"] < 0.005,
          "a later decision without a hand the result's own play brings there is left free; locked where that hand "
          "comes too rarely to show in the loss")
    under = next(a for a, (_, child) in enumerate(root.children) if child in first_bb and any(
        n.actor == "SB" and preflop._under(child, n) for n in nodes))
    sb_inner = [n for n in nodes if n.actor == "SB" and preflop._under(root.children[under][1], n)]

    def never(h):
        for hand in h[root.id].values():
            hand["freq"][under] = 0.0
        for n in sb_inner:
            silent(h[n.id])
    plan = made(never)
    check(plan["SB"]["free"] == [] and not {n.id for n in sb_inner} & set(plan["SB"]["rows"]),
          "a decision the player's own strategy never reaches is neither locked nor reported")
    check("no strategy for 1 hand classes of the small blind's range at r:0 (KK)" in refusal(
        lambda h: h[root.id]["KK"].update(freq=[0.0] * len(root.children)))
          and "first decision" in refusal(lambda h: silent(h[root.id]))
          and "no strategy for 1 hand classes of the big blind's range at r:0:" in refusal(
              lambda h: [h[n.id].pop("AKs") for n in first_bb])
          and f"big blind's range at {first_bb[0].id} (KK)" in refusal(
              lambda h: h[first_bb[0].id]["KK"].update(freq=[0.0] * len(first_bb[0].children)))
          and "has a strategy for 1 hand classes that are not in the big blind's range (QQ)" in refusal(
              lambda h: [h[n.id].update(QQ={"freq": [1.0] + [0.0] * (len(n.children) - 1)}) for n in first_bb])
          and "ranges of that solve" in refusal(lambda h: h[root.id].pop("AKs")),
          "a result with other hands than the check deals is refused: a whole hand class missing at a first decision "
          "the opponent comes to, a hand too many, no first decision")
    check(preflop._uneven(classes, dealt) and not preflop._uneven(classes, {"SB": [1, 0.5, 0, 1], "BB": [0, 0, 0, 0]})
          and preflop._uneven(["AA", "AA"], {"SB": [1, 0.5]}), "a range holds a class in part: a combo missing, or weights that differ")
    check(preflop._same("2.50bb 4x", "2.5bb 4.0x") and preflop._same("sNaNbb", "sNaNbb") and not preflop._same("sNaNbb", "1bb")
          and preflop._same("NaN", "NaN") and not preflop._same("NaN", "1"), "a setting that is no number compares as text")
    side = {"lost_bb100": [0.1, 0.0, 0.2], "sum_bb100": 0.86, "free": []}
    report = {"flop_count": 3, "flops": "x.txt", "exploitable_bb100": 0.01, "sum_bb100": 0.82, "settings": {"depth": "file"},
              "value_bb100": {"SB": [1.0, 0.9, 1.1], "BB": [-1.0, -1.1, -0.9]},
              "checked": [{"file": "a.json", "flop_count": 3, "depth": "file", "other_postflop": ["file"], "SB": side,
                           "BB": dict(side, free=["r:0:c", "r:0:b250"], free_part=["r:0:b250"])},
                          {"file": "b.json", "flop_count": 3, "depth": "flop", "other_postflop": ["depth"], "SB": side, "BB": side}]}
    text = " ".join("\n".join(preflop.check_text(report)).split(" "))
    check(max(len(line) for line in text.splitlines()[1:]) <= 100, "its notes are wrapped")
    check("hand classes in part" not in text and "hand classes in part" in "\n".join(preflop.check_text(dict(report, uneven_ranges=True))),
          "a check with ranges that hold a class in part says what a chart cannot do there")
    text = text.replace("\n", " ")
    exact = dict(report, sum_bb100=0.001, checked=[dict(report["checked"][1], SB=dict(side, sum_bb100=-0.004),
                                                         BB=dict(side, sum_bb100=0.0))])
    check("--postflop FILE)" in preflop.check_text(report)[0] and "a.json (3 flops) " in text and "b.json (3 flops, --depth flop)" in text
          and "a.json was solved with a postflop file, and a result does not say which" in text
          and "b.json was solved with another postflop tree than the check's (--postflop FILE; different: depth)" in text
          and "a.json has no strategy at 1 of the big blind's decisions (r:0:c): its small blind never goes there" in text
          and "a.json has a strategy for only some of the hands that can come to 1 of the big blind's decisions (r:0:b250)" in text
          and "added up to 0.82 to 0.86 bb/100" in text and "(0.04 here)" in text
          and "added up" not in "\n".join(preflop.check_text(exact)),
          "the check's text: the trees by their options, the decisions left free, and the sum where it is not zero")


def integration(exe):
    print("against the solver")
    with tempfile.TemporaryDirectory() as d:
        flop_file = os.path.join(d, "three.txt")
        with open(flop_file, "w") as f:
            f.write("Ks8s7h:40\nQsJh4d:35\n9s9h3d:25\n")
        flops = preflop.read_flop_file(flop_file)

        # every setting builds, the solver's tree is the tool's, and an exit starts where preflop ended
        for opts in (("--stack", "20"), ("--stack", "20", "--no-limp", "--ante", "0.5"),
                     ("--stack", "40", "--open", "2,3", "--depth", "turn", "--donk", "33"),
                     ("--stack", "20", "--depth", "none"), ("--stack", "8", "--open", "allin", "--no-limp")):
            s = settings(*opts)
            root = preflop.build_tree(s)
            dead = int(s.ante * s.chips)
            u = preflop.Solver(exe)
            try:
                for line in preflop.script(s, flops, root):
                    u.need(line)
                u.need("add_all_flops")
                nodes = preflop.collect(u, s, root)   # compares show_children with the tool's tree at every decision
                good = len(nodes) == len(decisions(root))
                bets = []
                for n in preflop.walk(root):
                    if n.kind == "decision":
                        continue
                    shown = u.cmd(f"show_node {n.id}")
                    kind = "SPLIT_NODE" if n.kind == "flop" else "END_NODE"
                    good &= len(shown) > 3 and shown[1].strip() == kind and shown[3].split() == [
                        str(n.inv[1]), str(n.inv[0]), str(dead)]
                    if n.kind == "flop":
                        first = u.cmd(f"show_node {n.id}:Ks8s7h")
                        good &= len(first) > 3 and first[3].split() == [str(n.inv[1]), str(n.inv[0]), str(dead)]
                        kids = [l.split()[0].rsplit(":", 1)[-1] for l in u.cmd(f"show_children {n.id}:Ks8s7h:c")
                                if l.startswith("r:")]
                        sizes = [int(k[1:]) for k in kids if k.startswith("b")]
                        if s.depth == "none":
                            good &= not sizes
                            continue
                        # after a check the small blind bets half the pot, on top of what is in already;
                        # all-in instead when that is most of what is left
                        half, left = (2 * n.inv[0] + dead) // 2, s.stack_chips - n.inv[0]
                        want = n.inv[0] + half if half * 100 <= left * s.postflop_allin_threshold else s.stack_chips
                        good &= bool(sizes) and min(sizes) == want and max(sizes) <= s.stack_chips
                        bets.append(want)
            finally:
                u.close()
            check(good, f"{' '.join(opts)}: builds; {len(nodes)} decisions and every exit as the tool has them"
                        + (f"; half-pot bets to {sorted(bets)}" if bets else ""))

        # a solve, and the charts against the solver's own answers
        s = settings("--stack", "20")
        root = preflop.build_tree(s)
        before = preflop.estimate_mb(exe, s, flops)
        u = preflop.Solver(exe)
        try:
            for line in preflop.script(s, flops, root):
                u.need(line)
            u.need("add_all_flops")
            after = [l for l in u.cmd("estimate_tree") if l.startswith("estimated tree size:")]
            u.cmd(f"set_accuracy {s.chips * 0.05:g}")
            u.cmd("go 20 seconds")
            u.cmd("wait_for_solver")
            results = dict(l.split(": ") for l in u.cmd("calc_results") if ": " in l)
            nodes = {n["id"]: n for n in preflop.collect(u, s, root)}
            order = " ".join(u.cmd("show_hand_order")).split()
            rows = lambda cmd: [[float(x) for x in l.split()] for l in u.cmd(cmd) if l.strip()]  # noqa: E731
            strat0, strat1 = rows("show_strategy r:0"), rows("show_strategy r:0:b250")
            strat2 = rows("show_strategy r:0:b250:b1000")
            bb_start, bb_3bet = rows("show_range OOP r:0")[0], rows("show_range OOP r:0:b250:b1000")[0]
            ev0 = rows("calc_ev IP r:0")[0]
        finally:
            u.close()
        check(after and int(after[0].split()[3]) == before and before > 0,
              f"estimate: the figure before building is the figure of the built tree ({before} MB)")
        worst = 0.0
        for cls in ("AA", "AKs", "T9s", "72o", "K5o", "33"):
            idx = [i for i, h in enumerate(order) if preflop.hand_class(h) == cls]
            for a in range(len(strat0)):
                worst = max(worst, abs(sum(strat0[a][i] for i in idx) / len(idx) - nodes["r:0"]["hands"][cls]["freq"][a]))
            for a in range(len(strat1)):
                worst = max(worst, abs(sum(strat1[a][i] for i in idx) / len(idx) - nodes["r:0:b250"]["hands"][cls]["freq"][a]))
            # facing the 3-bet a combo counts by the deals that get there: it raised, and the big blind,
            # in a hand that shares no card with it, raised again (pair by pair here, not the tool's sums)
            apart = lambda i, w: sum(y for h, y in zip(order, w) if not set((h[:2], h[2:])) & set((order[i][:2], order[i][2:])))  # noqa: E731
            reach = [strat0[0][i] * apart(i, bb_3bet) for i in idx]
            dealt = sum(apart(i, bb_start) for i in idx)
            if sum(reach) > 1e-6 * dealt:
                for a in range(len(strat2)):
                    mine = sum(r * strat2[a][i] for r, i in zip(reach, idx)) / sum(reach)
                    worst = max(worst, abs(mine - nodes["r:0:b250:b1000"]["hands"][cls]["freq"][a]))
                worst = max(worst, abs(sum(reach) / dealt - nodes["r:0:b250:b1000"]["hands"][cls]["reach"]))
        check(worst < 1e-5, f"a class's chart cell is show_strategy averaged over its combos, weighted by the deals "
                            f"each is in at the node (worst difference {worst:.1e})")
        worst, pairs = 0.0, 0
        for n in decisions(root):
            for a, (_, child) in enumerate(n.children):
                if child.id in nodes:
                    pairs += 1
                    worst = max(worst, abs(nodes[child.id]["reach"] - nodes[n.id]["reach"] * nodes[n.id]["freq"][a]))
        check(pairs >= 5 and worst < 1e-6, f"at every depth a decision is reached as often as its parent is, times "
                                           f"the action's share there ({pairs} decisions, worst difference {worst:.1e})")
        n0 = nodes["r:0"]
        check(abs(sum(n0["freq"]) - 1) < 1e-6 and all(abs(sum(h["freq"]) - 1) < 1e-6 for h in n0["hands"].values())
              and len(n0["hands"]) == 169 and abs(n0["reach"] - 1) < 1e-9, "169 classes; frequencies sum to 1")
        check(abs(nodes["r:0:b250"]["reach"] - n0["freq"][0]) < 1e-6 and abs(nodes["r:0:c"]["reach"] - n0["freq"][1]) < 1e-6
              and nodes["r:0:b250:b1000"]["reach"] < nodes["r:0:b250"]["reach"],
              f"how often a node is reached: the big blind faces the raise as often as the small blind makes it "
              f"({100 * n0['freq'][0]:.1f} %)")
        mean_ev = sum(ev0) / len(ev0) / s.chips
        cls_ev = sum(h["ev_bb"] * (6 if c[0] == c[1] else 4 if c.endswith("s") else 12) for c, h in n0["hands"].items()) / 1326
        check(abs(mean_ev - float(results["EV IP"]) / s.chips) < 2e-3 and abs(cls_ev - mean_ev) < 1e-6,
              f"EVs are big blinds won from the start of the hand: the small blind's hands average the game's value "
              f"({mean_ev:+.3f} bb)")
        folded = [h["ev_bb"] for c, h in nodes["r:0:b250"]["hands"].items() if h["freq"][-1] > 0.999]
        check(folded and all(abs(v + 1) < 0.02 for v in folded),
              f"a big blind hand that folds to the raise is at -1 ({len(folded)} classes)")

        # the command line, start to end
        out_file = os.path.join(d, "out.json")
        status, out, err = run("solve", out_file, "--stack", "20", "--flops", flop_file, "--accuracy", "2",
                               "--time", "20", "--solver", exe, "--width", "132",
                               "--save-tree", os.path.join(d, "tree.cfr"))
        saved = json.load(open(out_file)) if status == 0 else {}
        check(status == 0 and saved.get("flop_count") == 3 and saved.get("exploitable_bb100", 99) <= 2.0
              and len(saved.get("nodes", [])) == len(decisions(root)) and "exploitable for" in err
              and saved.get("estimated_mb") == before,
              f"solve: to under 2 bb/100 in {saved.get('seconds')} s, progress on stderr, the result saved")
        check(os.path.getsize(os.path.join(d, "tree.cfr")) > 10000, "--save-tree: the solved tree for the viewer")
        check("r:0   first to act" in out and "raise 2.5 (" in out and out.count("\nA ") >= len(saved.get("nodes", [])),
              "solve prints a chart per action of every decision")
        status2, again, _ = run("chart", out_file, "--width", "132")
        status3, one, _ = run("chart", out_file, "r:0:c", "--ev")
        check(status2 == 0 and again == out and status3 == 0 and "r:0:c " in one and "r:0:b250 " not in one
              and "EV of the hand" in one, "chart: the saved result prints the same charts again, or one node's")
        status, out, err = run("solve", os.path.join(d, "no.json"), "--solver", os.path.join(d, "nothing"), "--flops", "10")
        check(status == 1 and "no solver" in err and not os.path.exists(os.path.join(d, "no.json")),
              "without a solver: an error, no result file")

        # what can go wrong is said before the solve, in one line
        t0 = time.time()
        bad = [("solve", os.path.join(d, "nowhere", "o.json")), ("solve", out_file, "--save-tree", os.path.join(d, "nowhere", "t.cfr")),
               ("solve", out_file, "--stack 20 --flops 10"), ("solve",), ("solve", out_file, "--time", "0"),
               ("solve", out_file, "--threads", "0"), ("solve", out_file, "--memory", "0"), ("tree", "extra"),
               ("tree", "--stack", "nan"), ("tree", "--open", "inf"), ("tree", "--3bet", "nanx"), ("tree", "--ante", "inf"),
               ("tree", "--allin-threshold", "200"), ("tree", "--allin-threshold", "-1")]
        bad += [("solve", out_file, "--memory", "inf"), ("solve", out_file, "--accuracy", "inf"),
                ("solve", out_file, "--range-sb", "AhAs", "--range-bb", "AhAs", "--depth", "none"),
                ("estimate", "--limit", "nan", "--flops", flop_file)]
        # a short --time everywhere: a check that stopped refusing would otherwise start a real solve
        got = [run(argv[0], "--stack", "20", "--flops", flop_file, "--time", "2", "--solver", exe, *argv[1:])
               if argv[0] == "solve" and len(argv) > 1 and argv[-1] != "--stack 20 --flops 10"
               else run(argv[0], "--time", "2", "--solver", exe, *argv[1:]) for argv in bad]
        wrong = [argv for argv, (status, _, err) in zip(bad, got)
                 if status != 1 or not err.splitlines()[-1].startswith("error: ") or "Traceback" in err
                 or sum(l.startswith("error: ") for l in err.splitlines()) != 1]
        check(not wrong and time.time() - t0 < 10 and json.load(open(out_file)) == saved,
              f"{len(bad)} mistakes (no directory for the result or the tree, options glued into one word, no result "
              f"file, nan and inf, limits, ranges that cannot be dealt together) are one line each, before anything is solved" + (f": {wrong}" if wrong else ""))
        junk = os.path.join(d, "junk.json")
        answers = []
        for text in ("{}", "[1]", '{"version": 1}', "not json"):
            with open(junk, "w") as f:
                f.write(text)
            answers.append(run("chart", junk))
        check(all(status == 1 and err.startswith("error: ") and "Traceback" not in err for status, _, err in answers),
              "chart: a file that is not a result is said to be none")
        preflop.Solver.HELLO_SECONDS = 1
        try:
            status, _, err = run("solve", os.path.join(d, "no.json"), "--flops", flop_file, "--time", "2", "--solver", "/bin/cat")
            status2, _, err2 = run("solve", os.path.join(d, "no.json"), "--flops", flop_file, "--time", "2", "--solver", flop_file)
        finally:
            preflop.Solver.HELLO_SECONDS = 10
        check(status == 1 and "does not answer like the solver" in err and status2 == 1 and "cannot run" in err2
              and not os.path.exists(os.path.join(d, "no.json")),
              "--solver with a program that is not the solver, or a file that is no program: an error, no hang")

        # a range of single combos: hands the opponent's cards rule out count for nothing
        combo_file = os.path.join(d, "combo.json")
        status, out, err = run("solve", combo_file, "--stack", "20", "--flops", flop_file, "--depth", "none", "--time", "3",
                               "--range-bb", "AhAs", "--solver", exe, "--ev", "--width", "132")
        def strict(x):
            raise ValueError(x)
        try:
            combo = json.load(open(combo_file), parse_constant=strict)
        except (OSError, ValueError):
            combo = {"nodes": []}
        cn = {n["id"]: n for n in combo["nodes"]}
        check(status == 0 and "nan" not in out.lower() and len(cn.get("r:0", {}).get("hands", {})) == 169
              and all(h["ev_bb"] is not None for h in cn["r:0"]["hands"].values())
              and set(cn["r:0:b250"]["hands"]) == {"AA"},
              "--range-bb AhAs: every small blind class has an EV, the big blind's chart is the one hand, no nan")
        status, out, _ = run("estimate", "--stack", "20", "--flops", flop_file, "--depth", "flop", "--solver", exe)
        check(status == 0 and f"{before:,}" in out, "estimate prints that figure")
        # a quarter of a gigabyte: more than the smallest file and less than the largest (#494 made the
        # flop-depth tree a quarter of what it was, so half a gigabyte holds 100 flops now)
        status, out, err = run("script", "--stack", "20", "--memory", "0.25", "--solver", exe)
        check(status == 0 and "flops: the largest shipped subset under 256 MB" in err and out.rstrip().endswith("add_all_flops")
              and 25 <= sum(l.startswith("add_to_subset") for l in out.splitlines()) <= 100,
              f"without --flops the largest shipped subset under the budget is taken ({err.strip()[:60]})")
        status, out, err = run("script", "--stack", "20", "--memory", "0.01", "--solver", exe)
        check(status == 1 and "--depth" in err, "and when not even 10 flops fit, that is said")

        # check: what a result's preflop strategy loses on other flops. Three results for five flops: one
        # solved on the five, one on three of them, and the first with the small blind folding every hand
        opts = ("--stack", "12", "--depth", "none", "--solver", exe)
        five = os.path.join(d, "five.txt")
        with open(five, "w") as f:
            f.write("Ks8s7h:30\nQsJh4d:25\n9s9h3d:15\nAh7d2c:20\nTh6h5h:10\n")
        own, three, folds, report = (os.path.join(d, n) for n in ("own.json", "three.json", "folds.json", "report.json"))
        solved = [run("solve", own, "--flops", five, "--accuracy", "0.02", *opts)[0],
                  run("solve", three, "--flops", flop_file, "--accuracy", "0.02", *opts)[0]]
        with open(own) as f:
            folding = json.load(f)
        for n in folding["nodes"]:
            if n["actor"] == "SB":
                for h in n["hands"].values():
                    h["freq"] = [float(a == "fold") for a in n["actions"]]
        with open(folds, "w") as f:
            json.dump(folding, f)
        status, out, err = run("check", own, three, folds, "--flops", five, "--accuracy", "0.02", "--report", report, *opts)
        try:
            with open(report) as f:
                rep = json.load(f)
            rows = rep["checked"]
            lost = [[r[who]["lost_bb100"] for who in ("SB", "BB")] for r in rows]
        except (OSError, ValueError, KeyError):
            rep, rows, lost = {}, [], []
        check(solved == [0, 0] and status == 0 and len(rows) == 3
              and all(n in out for n in ("own.json (5 flops)", "three.json (3 flops)", "folds.json (5 flops)")),
              f"check: three results on five flops, a row each ({err.strip()[-80:] if status else 'ok'})")
        if len(lost) == 3:
            spans = [x for r in lost for x in r]
            check(all(lo <= at <= hi and hi - lo < 0.25 for at, lo, hi in spans),
                  f"each loss lies in its span, and at 0.02 bb/100 the spans are narrow (widest "
                  f"{max(hi - lo for _, lo, hi in spans):.3f})")
            check(all(abs(at) < 0.1 for at, _, _ in lost[0]),
                  f"the strategy solved on these flops loses nothing on them ({lost[0][0][0]:+.3f}, {lost[0][1][0]:+.3f})")
            check(all(lo > 0.1 for _, lo, _ in lost[1]),
                  f"the one solved on three of them loses, both players' ({lost[1][0][0]:.2f}, {lost[1][1][0]:.2f} bb/100)")
            # folding every hand gives up the small blind, half a big blind a hand, and what the game was worth
            value = rep["value_bb100"]["SB"][0]
            check(abs(lost[2][0][0] - (value + 50)) < 0.1 and abs(lost[2][1][0] - lost[0][1][0]) < 0.1,
                  f"a small blind that folds every hand loses the game's value and 50 bb/100 "
                  f"({lost[2][0][0]:.2f} for {value:.2f} + 50); the big blind's side of that file is the first's")
        status, _, err = run("check", own, "--flops", five, "--stack", "20", "--depth", "none", "--solver", exe)
        check(status == 1 and "another preflop game" in err, "check refuses a result of another preflop tree before it solves")
        # the postflop tree is the check's own: without --depth the result's, with it what the result's
        # preflop strategy loses in that game, and the output says the result was solved in another
        narrow = os.path.join(d, "narrow.json")
        status, out, err = run("check", own, "--flops", flop_file, "--accuracy", "0.05", "--stack", "12", "--solver", exe)
        check(status == 0 and "--depth none)" in out.splitlines()[0] and "another postflop tree" not in out,
              f"check without --depth is on the result's postflop tree ({out.splitlines()[0][:60] if out else err.strip()[-60:]})")
        status, out, err = run("check", own, "--flops", flop_file, "--accuracy", "0.05", "--stack", "12", "--depth", "flop",
                               "--report", report, "--solver", exe)
        try:
            with open(report) as f:
                deeper = json.load(f)["checked"][0]
        except (OSError, ValueError, KeyError, IndexError):
            deeper = {}
        check(status == 0 and "--depth flop)" in out.splitlines()[0] and "own.json (5 flops, --depth none)" in out
              and "another postflop tree" in " ".join(out.split()) and deeper.get("other_postflop") == ["depth"]
              and all(deeper[who]["lost_bb100"][2] > 0 for who in ("SB", "BB")),
              "check --depth flop on a result solved without postflop bets: said, and a loss for both players")
        status = run("solve", narrow, "--flops", flop_file, "--range-bb", "TT+,AK", *opts)[0]
        with open(narrow) as f:
            check(json.load(f).get("target_bb100") == 0.5, "solve without --accuracy is to 0.5 bb/100")
        status2, _, err = run("check", narrow, "--flops", flop_file, *opts)
        check(status == 0 and status2 == 1 and "no strategy for" in err and "ranges of that solve" in err,
              f"check refuses a result solved with a narrower range ({err.strip()[:70]})")
        status, out, err = run("check", narrow, "--flops", flop_file, "--range-bb", "TT+,AK", "--accuracy", "0.05", *opts)
        check(status == 0 and "narrow.json (3 flops)" in out, "and takes it with that range")
        status, _, err = run("check", own, three, "--flops", flop_file, "--stack", "12.0", "--limp-raise", "4.0x", "--report", report,
                             "--solver", exe)
        with open(report) as f:
            target = json.load(f).get("target_bb100")
        check(status == 0 and target == 0.05, "two results of one depth need no --depth; a stack of 12.0 is the "
              f"results' 12, a limp-raise of 4x the 3-bet's they had; check without --accuracy is to 0.05 bb/100 ({target})")
        status, _, err = run("check", five, "--flops", five, *opts)
        check(status == 1 and "cannot read the result" in err, "and a file that is no result")
        status, _, err = run("check", "--flops", five, *opts)
        check(status == 1 and "needs a result file" in err, "and no file at all")

        # what a review of the check found (#499). A decision the opponent never brings about has no
        # frequencies in a result: it is left to the solve, and not played at random for the opponent to find
        with open(own) as f:
            whole = json.load(f)

        def edited(name, change):
            result = json.loads(json.dumps(whole))
            change({n["id"]: n["hands"] for n in result["nodes"]})
            with open(os.path.join(d, name), "w") as f:
                json.dump(result, f)
            return os.path.join(d, name)

        def nothing(hands):
            for h in hands.values():
                h["freq"] = [0.0] * len(h["freq"])
        silent = edited("silent.json", lambda by: nothing(by["r:0:c"]))
        status, out, err = run("check", own, silent, "--flops", five, "--accuracy", "0.02", "--report", report, *opts)
        with open(report) as f:
            rows = json.load(f)["checked"]
        was, now = ([r[who]["lost_bb100"][0] for who in ("SB", "BB")] for r in rows)
        free = [r[who]["free"] for r in rows for who in ("SB", "BB")]
        check(status == 0 and free[:3] == [[], [], []] and free[3][0] == "r:0:c" and all(n.startswith("r:0:c") for n in free[3])
              and rows[1]["BB"]["free_part"] == free[3][1:]
              and "silent.json has no strategy at 1 of the big blind's decisions (r:0:c)" in " ".join(out.split())
              and now[1] < was[1] + 0.05 and abs(now[0] - was[0]) < 0.05,
              f"a result with nothing at the big blind's decision after a limp: left free and said, and no loss made "
              f"up there ({now[1]:+.3f} for {was[1]:+.3f} with it)")
        for name, change, said in (
                ("five.json", lambda by: by["r:0:b250:b1200"]["AA"].update(freq=5), "the frequencies of AA at r:0:b250:b1200"),
                ("noaa.json", lambda by: by["r:0"]["AA"].update(freq=[0.0, 0.0, 0.0]),
                 "no strategy for 1 hand classes of the small blind's range at r:0 (AA)"),
                ("no72.json", lambda by: by["r:0:c"]["72o"].update(freq=[0.0] * len(by["r:0:c"]["72o"]["freq"])),
                 "no strategy for 1 hand classes of the big blind's range at r:0:c (72o)"),
                ("noroot.json", lambda by: nothing(by["r:0"]), "no strategy for the small blind's first decision")):
            status, _, err = run("check", edited(name, change), "--flops", five, *opts)
            check(status == 1 and said in err and "the game as it is" not in err and "Traceback" not in err,
                  f"refused before anything is solved: {said}")
        nan = json.loads(json.dumps(whole))
        nan["settings"]["open"] = "sNaNbb"
        with open(os.path.join(d, "nan.json"), "w") as f:
            json.dump(nan, f)
        status, _, err = run("check", os.path.join(d, "nan.json"), "--flops", five, *opts)
        check(status == 1 and "another preflop game" in err and "Traceback" not in err,
              "a size that is no number is another game, said in words")
        status, _, err = run("check", own, "--flops", five, "--range-bb", "AhAs", *opts)
        check(status == 1 and "that are not in the big blind's range" in err and "the game as it is" not in err,
              "and a result solved with a wider range than the check's")
        # ranges that hold a class in part: a class can be without a hand to meet at a first decision (here
        # the big blind's AhAs: AhAd blocks it and 72o folds), which is no other range and not refused
        part = os.path.join(d, "part.json")
        suits = ("--flops", flop_file, "--range-sb", "AhAd,72o", "--range-bb", "AhAs,KK", "--time", "20", *opts)
        run("solve", part, "--accuracy", "0.005", *suits)
        status, out, err = run("check", part, "--accuracy", "0.05", *suits)
        check(status == 0 and "hand classes in part" in out and "Traceback" not in err,
              f"a result of ranges with single combos is checked with them, and the check says what a chart "
              f"cannot do there ({err.strip()[-80:] if status else 'ok'})")
        # a postflop file: a result does not say which, so the check asks for it and says what it cannot know
        post, filed = os.path.join(d, "post.txt"), os.path.join(d, "filed.json")
        with open(post, "w") as f:
            f.write("#Pot#55\n#EffectiveStacks#975\n#FlopConfig.BetSize#50\n#FlopConfigIP.BetSize#33, 75\n"
                    "#FlopConfigIP.RaiseSize#60\n#AllinThreshold#67\n")
        base = ("--flops", flop_file, "--stack", "12", "--solver", exe)
        solved = run("solve", filed, "--postflop", post, *base)[0]
        status, _, err = run("check", filed, *base)
        check(solved == 0 and status == 1 and "was solved with a postflop file" in err and "--postflop FILE" in err,
              "check of a result solved with a postflop file asks for the file or a depth")
        status, out, _ = run("check", filed, "--postflop", post, "--accuracy", "0.5", *base)
        status2, out2, _ = run("check", filed, "--depth", "flop", "--accuracy", "0.5", *base)
        check(status == 0 and "--postflop FILE)" in out.splitlines()[0] and "does not say which" in " ".join(out.split())
              and status2 == 0 and "filed.json (3 flops, --postflop FILE)" in out2,
              "with the file it says a result does not name its file; with a depth the row names the result's tree")
        status, out, _ = run("check", own, "--flops", flop_file, "--accuracy", "0.5", *opts)
        check(status == 0 and "added up to" in out and "not to zero" in out,
              "on a flop subset the check says what the two values add up to")


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "solver")
    unit()
    integration(exe)
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
