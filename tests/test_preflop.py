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


def quiet(*argv):
    """`run`, with anything the command line lets through as a status of its own."""
    try:
        return run(*argv)
    except BaseException as e:  # noqa: B902 (a traceback is the failure looked for)
        return -1, "", f"Traceback: {type(e).__name__}: {e}"


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
    within = preflop.tables_within
    check([within(143, 176, mb) for mb in (142, 143, 175, 176, 9999)] == [None, "small", "small", "auto", "auto"]
          and within(18, 18, 18) == "auto" and within(18, 18, 17) is None,
          "the outcome tables under a budget: the solver's choice when the tree with them is within it, "
          "`small` when only the smallest tree is, nothing when neither")
    note = preflop._tables_note
    check(note(18, 18, 100) == "" and "set_outcome_tables small" in note(143, 176, 175) and "over the 175" in note(143, 176, 175)
          and "176 MB" in note(143, 176, 176) and "small" not in note(143, 176, 176) and "nine tenths" in note(143, 176, 176),
          "and what the tool says of it: nothing where the two trees are one")
    built = preflop._built_note
    check(built(176, 143, "auto", 176).startswith("the tree built: 176 MB with outcome tables") and built(176, 143) == built(176, 143, "auto", 176)
          and built(143, 143, "auto", 176).startswith("the tree built: 143 MB without outcome tables at every level (the 176 MB")
          and built(143, 143, "small", 176) == built(143, 143) == built(18, 18, "auto", 18) == built(None, 143, "auto", 176) == ""
          and built(140, 143, "small", 176) == "the tree built: 140 MB",
          "which tree was built is said where the estimate does not say it: the tables added, the tables left out for "
          "lack of free memory, a count that is not the estimate's; nothing where the tool chose, or there is one tree")

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
    with tempfile.TemporaryDirectory() as d:
        answers = []

        def written(name, text):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
            return os.path.join(d, name)
        for name, text in (("reach.json", json.dumps(dict(result, version=1, nodes=[dict(node, reach=10 ** 400)]))),
                           ("freq.json", json.dumps(dict(result, version=1, nodes=[dict(node, hands={
                               "AA": {"reach": 1, "freq": [1e308, 1e308, 0.0], "ev_bb": 1.0}})]))),
                           ("true.json", json.dumps(dict(result, version=True))),
                           ("deep.json", "[" * 100000),
                           ("surrogate.json", json.dumps(dict(result, version=1, flops="\ud800"))),
                           ("action.json", json.dumps(dict(result, version=1, nodes=[dict(
                               node, actions=["\udfff"] + node["actions"][1:])])))):
            answers += [quiet("chart", written(name, text)), quiet("check", written(name, text))]
        ok = written("ok.json", json.dumps(dict(result, version=1)))
        check(all(status == 1 and err.startswith("error: ") and "Traceback" not in err for status, _, err in answers)
              and all("cannot read the result" in err for _, _, err in answers[6:])
              and all("is not a result of `preflop.py solve`" in err for _, _, err in answers[:6])
              and quiet("chart", ok)[0] == 0,
              "chart and check say in words that a file is no result: a number no float holds, a version that is "
              "`true`, brackets nested past reading, a string that cannot be printed (half a surrogate pair) "
              f"({[a[0] for a in answers]})")
        # what `chart` alone reads: a frequency that is no percent, in a cell or in a node's line
        answers = [quiet("chart", written(name, json.dumps(dict(result, version=1, nodes=[changed]))))
                   for name, changed in (
                       ("cell.json", dict(node, hands={"AA": {"reach": 1, "freq": [10 ** 400, 0, 0], "ev_bb": 1.0}})),
                       ("line.json", dict(node, freq=[1e308] * len(node["freq"]))),
                       ("inf.json", dict(node, freq=[float("inf")] * len(node["freq"]))),
                       ("nan.json", dict(node, reach=float("nan"))),
                       ("text.json", dict(node, reach="1")))]
        check(all(status == 1 and "is not a result of `preflop.py solve`" in err for status, _, err in answers),
              "chart refuses a frequency that is no percent (`10**400` in a cell, `1e308`, Infinity or NaN in a "
              f"decision's line, a number in quotes) ({[a[0] for a in answers]})")
        answers = [quiet(cmd, written(f"v{v}.json", json.dumps(dict(result, version=v))))
                   for v in (2, 0, -1) for cmd in ("chart", "check")]
        check(all(status == 1 and f"is a version {v} result: this `preflop.py` reads version 1" in err
                  for (status, _, err), v in zip(answers, (2, 2, 0, 0, -1, -1))),
              f"chart and check read version 1 only: 2, 0 and -1 are named and refused ({[a[0] for a in answers]})")
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
    known, totals, left = preflop._class_rows("x.json", {"id": "r:0", "hands": {"AA": {"freq": [2, 2, 0]}, "KK": {"freq": [0.0, 0, 0]}}}, 3)
    check(known == {"AA": [0.5, 0.5, 0.0]} and totals == {"AA": 4.0} and left == {"AA": 1.0}
          and preflop.locked_rows(known, 3, ["AA", "KK", "AA"]) == [[0.5, 1 / 3, 0.5], [0.5, 1 / 3, 0.5], [0.0, 1 / 3, 0.0]],
          "a class's frequencies are scaled to 1 and given to each of its combos; one the result did not bring plays evenly")
    bad = 0
    for hand in ({"freq": 5}, {"freq": True}, {"freq": [0.5, 0.5]}, {"freq": [0.5, "x", 0]}, {"freq": [1.5, -0.5, 0]},
                 {"freq": [float("nan"), 0, 1]}, {"freq": [1e308, 1e308, 1e308]}, {"freq": None}, {}, [1, 0, 0],
                 {"freq": [10 ** 400, 0, 0]}, {"freq": [10 ** 308] * 3}):  # integers: past a float, and a sum past one
        try:
            preflop._class_rows("x.json", {"id": "r:0", "hands": {"AA": hand}}, 3)
        except preflop.PreflopError as e:
            bad += "x.json: the frequencies of AA at r:0 are not a strategy" in str(e)
    check(bad == 12, f"frequencies that are no strategy are refused, by file, hand and node ({bad} of 12)")
    check([preflop._not_as_written(row) for row in ([0.5, 0.5, 0], [0.5, 0.25, 0], [0.125, 0.125, 0.125], [0.75, 0.5, 0], [2, 2, 0],
                                                    [2.0 ** 70] * 3, [1e-30, 0, 0])] == [0.0, 0.25, 0.625, 0.25, 1.0, 1.0, 1.0],
          "what a row that does not add up to 1 leaves open: the part that is missing, or over; all of it at the most")
    check(preflop._not_as_written([1.5, 0, 0]) == 0 and preflop._not_as_written([2.0 ** 70, 0, 0]) == 0
          and abs(preflop._not_as_written([1.2, 0.3, 0]) - 0.3) < 1e-12 and abs(preflop._not_as_written([1.5, 0.75, 0]) - 0.75) < 1e-12,
          "over 1 an entry counts as 1 at the most: one action written as more than always leaves nothing open")
    check([preflop._total_text(t) for t in (0.6, 1.0, 1.000004, 0.9999999, 2.0 ** 70)]
          == ["0.6", "1", "1 + 4e-06", "1 - 1e-07", "1.18059e+21"], "a row's sum is not printed as the 1 it is not")

    s = settings("--stack", "12", "--depth", "none")
    root = preflop.build_tree(s)
    nodes = decisions(root)
    # the big blind holds one of the two combos of AA; `whole`: both players hold every class whole
    classes, dealt = ["AA", "KK", "AKs", "AA"], {"SB": [1, 1, 1, 1], "BB": [1, 1, 1, 0]}
    whole = {"SB": [1, 1, 1, 1], "BB": [1, 1, 1, 1]}

    def made(change=None, dealt=dealt, swing=24.0, root=root, nodes=nodes):
        result = {"nodes": [{"id": n.id, "actions": [label for label, _ in n.children],
                             "hands": {c: {"freq": [1.0 / len(n.children)] * len(n.children)} for c in set(classes)}}
                            for n in nodes]}
        if change:
            change({n["id"]: n["hands"] for n in result["nodes"]})
        return preflop._plan("x.json", result, root, nodes, classes, dealt, dealt, swing)

    def silent(hands):
        for h in hands.values():
            h["freq"] = [0.0] * len(h["freq"])

    def refusal(change, dealt=dealt):
        try:
            made(change, dealt)
        except preflop.PreflopError as e:
            return str(e)
        return ""
    plan = made()
    check(all(sorted(plan[who]["rows"]) == sorted(n.id for n in nodes if n.actor == who) and plan[who]["free"] == []
              for who in ("SB", "BB")), "a result with every hand at every decision: all of a player's decisions are locked")
    check(all(plan[who]["rescaled_bb100"] == 0 and plan[who]["rescaled_most"] == [] for who in ("SB", "BB")),
          "rows that add up to 1 are not rescaled, and none is named")

    def scaled(node_id, cls, by):
        def change(h):
            h[node_id][cls]["freq"] = [f * by for f in h[node_id][cls]["freq"]]
        return change
    k = len(root.children)
    short, over, far = made(scaled(root.id, "KK", 0.6)), made(scaled(root.id, "KK", 1.5)), made(scaled(root.id, "KK", 2.0 ** 70))
    # KK is a quarter of the small blind's deals, every hand is at the first decision, a hand moves 24 bb
    check(short["SB"]["rows"] == plan["SB"]["rows"] and over["SB"]["rows"] == far["SB"]["rows"] == plan["SB"]["rows"]
          and abs(short["SB"]["rescaled_bb100"] - 0.25 * 0.4 * 2400) < 1e-6 and short["SB"]["rescaled_most"][:2] == ["KK", root.id]
          and abs(short["SB"]["rescaled_most"][2] - 0.6) < 1e-12
          and abs(over["SB"]["rescaled_bb100"] - 0.25 * 0.5 * 2400) < 1e-6 and abs(far["SB"]["rescaled_bb100"] - 0.25 * 2400) < 1e-6
          and far["SB"]["rescaled_most"][2] == 2.0 ** 70 and short["BB"]["rescaled_bb100"] == 0,
          "a row that does not add up to 1 is locked rescaled, and measured: the class's share of the deals, times the "
          "part of the row that is open, times what a hand can move")
    deep = next(n for n in nodes if n.actor == "SB" and n is not root)
    way = [a for a, (_, child) in enumerate(root.children) if child is deep or preflop._under(child, deep)]
    below = made(scaled(deep.id, "KK", 0.6))
    check(len(way) == 1 and abs(below["SB"]["rescaled_bb100"] - 0.25 * (1 / k) * 0.4 * 2400) < 1e-6
          and below["SB"]["rescaled_most"][:2] == ["KK", deep.id],
          "at a later decision by how often the result's own play brings the class there")
    always = made(lambda h: h[root.id]["KK"].update(freq=[1.5] + [0.0] * (k - 1)))
    check(always["SB"]["rescaled_bb100"] == 0 and always["SB"]["rescaled_most"] == []
          and all(row[1] == (1.0 if a == 0 else 0.0) for a, row in enumerate(always["SB"]["rows"][root.id])),
          "an action written as more than always is that action always: nothing is open, and no row is named")

    def several(*changes):
        def change(h):
            for one in changes:
                one(h)
        return change
    own_sb = [n for n in nodes if n.actor == "SB"]
    every = made(several(*[scaled(n.id, "KK", 2.0 ** 70) for n in own_sb]))
    check(len(own_sb) >= 3 and abs(every["SB"]["rescaled_bb100"] - 0.25 * 2400) < 1e-6 and every["SB"]["rescaled_most"][:2] == ["KK", root.id],
          "a class's rows at one decision after another count as all of the class at the most: what one hand can move")
    two = made(several(scaled(root.id, "AA", 0.8), scaled(deep.id, "KK", 0.3)))
    check(abs(two["SB"]["rescaled_bb100"] - (0.5 * 0.2 + 0.25 * (1 / k) * 0.7) * 2400) < 1e-6
          and two["SB"]["rescaled_most"][:2] == ["AA", root.id] and abs(two["SB"]["rescaled_most"][2] - 0.8) < 1e-12,
          "of several rows the one named is the one that counts the most, not the one furthest from 1")
    rounding = made(lambda h: [hand.update(freq=[f * (1 + 1e-7) for f in hand["freq"]]) for hands in h.values() for hand in hands.values()])
    check(0 < rounding["SB"]["rescaled_bb100"] < 0.005 and rounding["SB"]["rows"].keys() == plan["SB"]["rows"].keys(),
          "what the rounding of a solve leaves (1e-7 a row, the solver's single precision) does not show at 12 bb")
    first_bb = [child for _, child in root.children if child.kind == "decision"]
    first_bb.sort(key=lambda top: -sum(preflop._under(top, n) for n in nodes))  # the limp: a decision of each under it
    inner = [n for n in nodes if n.actor == "BB" and n not in first_bb and preflop._under(first_bb[0], n)]
    limp = next(a for a, (_, child) in enumerate(root.children) if child is first_bb[0])

    theirs = made(scaled(first_bb[1].id, "AKs", 0.3))   # the big blind's deals: AA once, KK, AKs
    check(abs(theirs["BB"]["rescaled_bb100"] - (1 / 3) * 0.7 * 2400) < 1e-6 and theirs["SB"]["rescaled_bb100"] == 0
          and theirs["BB"]["rescaled_most"][:2] == ["AKs", first_bb[1].id] and theirs["SB"]["rescaled_most"] == [],
          "the big blind's rows are measured for the big blind: a third of its deals, at a first decision, seven tenths open")
    twice = made(several(scaled(first_bb[0].id, "AKs", 0.3), scaled(first_bb[1].id, "AKs", 0.3), scaled(root.id, "KK", 0.6)))
    check(abs(twice["BB"]["rescaled_bb100"] - (1 / 3) * 2400) < 1e-6 and abs(twice["SB"]["rescaled_bb100"] - 0.25 * 0.4 * 2400) < 1e-6
          and twice["BB"]["rescaled_most"][0] == "AKs" and twice["SB"]["rescaled_most"][:2] == ["KK", root.id],
          "its first decisions after a limp and after a raise add up to all of the class at the most; each player's own "
          "figure and row when both have rows that are off")
    left_free = made(several(lambda h: h[first_bb[0].id].pop("AA"), scaled(first_bb[0].id, "KK", 0.3)))
    check(left_free["BB"]["free"] == [first_bb[0].id] and left_free["BB"]["rescaled_bb100"] == 0 and left_free["BB"]["rescaled_most"] == [],
          "rows at a decision left free are not measured: nothing is locked there")
    under_free = made(several(lambda h: h[first_bb[0].id].pop("AA"), scaled(inner[0].id, "KK", 0.5)))
    check(inner and under_free["BB"]["free"] == [first_bb[0].id] and inner[0].id in under_free["BB"]["rows"]
          and abs(under_free["BB"]["rescaled_bb100"] - (1 / 3) * 0.5 * 2400) < 1e-6
          and under_free["BB"]["rescaled_most"][:2] == ["KK", inner[0].id],
          "a locked row below a decision left free is measured: free at the player's first decision, all of the class")
    # AA is half of the small blind's deals with a quarter of its row open, KK a quarter with half open
    tie = made(several(lambda h: h[root.id]["AA"].update(freq=[0.75] + [0.0] * (k - 1)),
                       lambda h: h[root.id]["KK"].update(freq=[0.5] + [0.0] * (k - 1))))
    check(abs(tie["SB"]["rescaled_bb100"] - 0.25 * 2400) < 1e-6 and tie["SB"]["rescaled_most"] == ["AA", root.id, 0.75],
          "of two rows that count the same the first met is named: the class that comes first in the range")
    # the range's order, not the names' and not the larger share: AA a quarter of the deals, KK a half, AKs a quarter
    order = {"SB": [1, 2, 1, 0], "BB": dealt["BB"]}

    def only(cls, total):
        return lambda h: h[root.id][cls].update(freq=[total] + [0.0] * (k - 1))
    smaller, later = made(several(only("AA", 0.5), only("KK", 0.75)), order), made(several(only("KK", 0.75), only("AKs", 0.5)), order)
    check(abs(smaller["SB"]["rescaled_bb100"] - 0.25 * 2400) < 1e-6 and smaller["SB"]["rescaled_most"] == ["AA", root.id, 0.5]
          and abs(later["SB"]["rescaled_bb100"] - 0.25 * 2400) < 1e-6 and later["SB"]["rescaled_most"] == ["KK", root.id, 0.75],
          "the first in the range, whether or not it is the larger share of the deals or the first by name")

    # 40 bb: each player has a decision under each of its decisions, three deep (12 bb has two)
    root40 = preflop.build_tree(settings("--stack", "40", "--depth", "none"))
    nodes40 = decisions(root40)

    def made40(*changes):
        return made(several(*changes), swing=80.0, root=root40, nodes=nodes40)

    def chain(who, top):   # the player's decisions under the root's child `top`, nearest the root first
        return [n for n in nodes40 if n.actor == who and (n is top or preflop._under(top, n))]
    tops = [child for _, child in root40.children if child.kind == "decision"]
    sb_limped, bb_raised, bb_limped = chain("SB", tops[1]), chain("BB", tops[0]), chain("BB", tops[1])
    limp40 = next(a for a, (_, child) in enumerate(root40.children) if child is tops[1])
    k40 = len(root40.children)
    check([len(c) for c in (sb_limped, bb_raised, bb_limped)] == [2, 2, 2] and nodes40.index(bb_raised[1]) < nodes40.index(bb_limped[0]),
          "the 40 bb tree: two decisions of each player after a limp, two of the big blind after a raise, the raise first")
    # the small blind's decision after a limp and a raise is left free (nothing for AA there); KK's row
    # at its next one is a half: KK is a quarter of the deals and limps a third of the time
    under_later = made40(lambda h: h[sb_limped[0].id].pop("AA"), scaled(sb_limped[1].id, "KK", 0.5))
    check(under_later["SB"]["free"] == [sb_limped[0].id] and sb_limped[1].id in under_later["SB"]["rows"]
          and abs(under_later["SB"]["rescaled_bb100"] - 0.25 * (1 / k40) * 0.5 * 8000) < 1e-6
          and under_later["SB"]["rescaled_most"][:2] == ["KK", sb_limped[1].id],
          "below a decision left free the class is as often there as the result's play brought it to the free one, "
          "not always: free at a later decision, by how often the class came to that")

    def plays(node, cls, freq):
        return lambda h: h[node.id][cls].update(freq=freq + [0.0] * (len(node.children) - len(freq)))
    # the same class, a quarter of it open at each of two rows of the big blind (binary fractions: equal as floats)
    near_first = made40(plays(bb_raised[0], "KK", [0.75]), plays(bb_limped[0], "KK", [0.5, 0.5]), plays(bb_limped[1], "KK", [0.5]))
    deep_first = made40(plays(bb_raised[0], "KK", [0.5, 0.5]), plays(bb_raised[1], "KK", [0.5]), plays(bb_limped[0], "KK", [0.75]))
    check(abs(near_first["BB"]["rescaled_bb100"] - (1 / 3) * 0.5 * 8000) < 1e-6 and near_first["BB"]["rescaled_most"] == ["KK", bb_raised[0].id, 0.75]
          and abs(deep_first["BB"]["rescaled_bb100"] - (1 / 3) * 0.5 * 8000) < 1e-6 and deep_first["BB"]["rescaled_most"] == ["KK", bb_raised[1].id, 0.5],
          "of rows that count the same at two decisions the first met going down the tree is named, all under a "
          "decision's first action before its next: the nearer the root of the two in one result, the further in the other")

    # 150 bb: the big blind has three decisions on one line (after a limp, after its raise is raised,
    # after its next raise meets an all-in). The first is left free (nothing for AA there); KK's row at
    # the second adds up to 1 and goes on to the third a quarter of the time; its row there is a half
    root150 = preflop.build_tree(settings("--stack", "150", "--depth", "none"))
    nodes150 = decisions(root150)
    limped = next(child for label, child in root150.children if label == "limp")
    line = [n for n in nodes150 if n.actor == "BB" and (n is limped or preflop._under(limped, n))]
    check(len(line) == 3 and preflop._under(line[1], line[2]) and len(line[1].children) == 3,
          "the 150 bb tree: three decisions of the big blind on one line after a limp")
    on = next(a for a, (_, child) in enumerate(line[1].children) if child is line[2] or preflop._under(child, line[2]))
    onward = [0.75 if a == (on + 1) % 3 else 0.0 for a in range(3)]
    onward[on] = 0.25
    two_below = made(several(lambda h: h[line[0].id].pop("AA"), lambda h: h[line[1].id]["KK"].update(freq=onward),
                             scaled(line[2].id, "KK", 0.5)), swing=300.0, root=root150, nodes=nodes150)
    check(two_below["BB"]["free"] == [line[0].id] and line[1].id in two_below["BB"]["rows"] and line[2].id in two_below["BB"]["rows"]
          and abs(two_below["BB"]["rescaled_bb100"] - (1 / 3) * 0.25 * 0.5 * 30000) < 1e-6
          and two_below["BB"]["rescaled_most"] == ["KK", line[2].id, 0.5],
          "two locked decisions below one left free: the locked row between narrows how often the class is at the "
          "lower one as it does anywhere (a third of the deals, a quarter of the time, half open: 1,250 bb/100)")

    def no_limp(h):  # the small blind never limps, and the big blind has nothing for the decision after one
        for hand in h[root.id].values():
            hand["freq"] = [0.0 if a == limp else 1.0 / (len(root.children) - 1) for a in range(len(root.children))]
        silent(h[first_bb[0].id])
    for ranges in (dealt, whole):
        plan = made(no_limp, ranges)
        check(len(first_bb) >= 2 and inner and plan["BB"]["free"][0] == first_bb[0].id and plan["BB"]["part"] == []
              and first_bb[0].id not in plan["BB"]["rows"] and first_bb[1].id in plan["BB"]["rows"]
              and all(preflop._under(first_bb[0], n) for n in nodes if n.id in plan["BB"]["free"][1:]),
              "a decision the result's opponent never goes to is left free, not played at random; the rest is locked")
    plan = made(lambda h: silent(h[first_bb[0].id]))
    check(plan["BB"]["free"] == plan["BB"]["part"] == [first_bb[0].id],
          "one without a hand that the result's opponent does go to is left free too, and not said to be that")
    told = refusal(lambda h: silent(h[first_bb[0].id]), whole)
    check(f"has no strategy at {first_bb[0].id} for hands of the big blind's range that its own strategies bring there" in told
          and "not all of a strategy" in told,
          "with whole classes in both ranges that is no result of a solve: every hand met one there, and it is refused")
    plan = made(lambda h: h[first_bb[0].id].pop("AA"))
    check(plan["BB"]["free"] == plan["BB"]["part"] == [first_bb[0].id] and first_bb[0].id not in plan["BB"]["rows"]
          and all(n.id in plan["BB"]["rows"] for n in nodes if n.actor == "BB" and n is not first_bb[0]),
          "a first decision without a hand class the range holds in part is left free: no opponent may have left it possible")
    check("no strategy for 1 hand classes of the small blind's range at r:0 (AA)" in refusal(
        lambda h: h[root.id].pop("AA"), {"SB": [1, 1, 1, 0], "BB": [1, 1, 1, 1]}),
          "not so the small blind's first: every hand the check deals is there, a class held in part too")

    def both(h):
        h[first_bb[0].id].pop("AA")
        silent(h[inner[0].id])
    plan = made(both)
    check(plan["BB"]["free"] == plan["BB"]["part"] == [first_bb[0].id, inner[0].id],
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
    check(f"has no strategy at {inner[0].id} for hands of the big blind's range that its own strategies bring there (KK)"
          in refusal(rarely(0.5), whole) and made(rarely(1e-9), whole)["BB"]["free"] == [],
          "with whole classes in both ranges the first of the two is refused: the hand comes there and met one")
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
    # what the fourth review found. A decision emptied in a file is not one the player never passes: the
    # small blind's decision under an emptied one of the big blind is not said to be never come to
    parent = root.children[under][1]
    on = [a for a, (_, child) in enumerate(parent.children) if child in sb_inner or any(preflop._under(child, n) for n in sb_inner)]

    def emptied(h):
        silent(h[parent.id])
        for n in sb_inner:
            silent(h[n.id])

    def stays(h):  # the big blind's own frequencies say it never goes on
        for hand in h[parent.id].values():
            hand["freq"] = [0.0 if a in on else 1.0 / (len(parent.children) - len(on)) for a in range(len(parent.children))]
        for n in sb_inner:
            silent(h[n.id])
    plan, never_there = made(emptied), made(stays)
    check(parent.actor == "BB" and on and plan["SB"]["free"] == plan["SB"]["part"] == [n.id for n in sb_inner]
          and parent.id in plan["BB"]["part"]
          and never_there["SB"]["free"] == [n.id for n in sb_inner] and never_there["SB"]["part"] == []
          and never_there["BB"]["free"] == [],
          "nothing at a decision under one of the opponent's that the file has nothing for either: not said to be never "
          "come to; under one whose frequencies say the opponent does not go on, it is")

    def only_there(h):  # the small blind always goes to the emptied decision
        for hand in h[root.id].values():
            hand["freq"] = [float(a == under) for a in range(len(root.children))]
        emptied(h)
    stake = 7e-5  # all of the big blind's deals show against it (0.007 bb/100); half of them would not (0.0035)
    plan = made(only_there, swing=stake)
    check(preflop._shows(stake * 100) and not preflop._shows(stake * 50) and sb_inner[0].id in plan["SB"]["part"],
          "and it passes every hand on, not some: with a stake at which half the big blind's deals would not show, "
          "the decision under it is still one it comes to")
    # what shows is a share of the deals times the stake, 0.005 bb/100 or more, summed over the hands
    between = [n for n in nodes if n.actor == "SB" and n is not root and preflop._under(n, inner[0])]
    passed = 1.0
    for n in between:
        passed /= len(n.children)

    def limps(often, gone):  # the small blind comes to inner[0] `often`; the big blind half the time, without `gone`
        def change(h):
            for hand in h[root.id].values():
                hand["freq"] = [often / passed if a == limp else (1 - often / passed) / (len(root.children) - 1)
                                for a in range(len(root.children))]
            for cls in gone:
                h[first_bb[0].id][cls]["freq"] = [0.5 if a == step else 0.5 / (len(first_bb[0].children) - 1)
                                                 for a in range(len(first_bb[0].children))]
                h[inner[0].id].pop(cls)
        return change
    # whole ranges: AA is half the big blind's deals, KK and AKs a quarter each; 0.25 * 0.5 * 1e-5 * 2400 = 0.003
    one, two = refusal(limps(1e-5, ["KK"]), whole), refusal(limps(1e-5, ["KK", "AKs"]), whole)
    plan = None if one else made(limps(1e-5, ["KK"]), whole)
    check(between and plan and plan["BB"]["free"] == plan["BB"]["part"] == [inner[0].id]
          and f"has no strategy at {inner[0].id} for hands" in two and "KK" in two and "AKs" in two
          and not refusal(limps(1e-5, ["KK", "AKs"]), dealt),
          "hands a result's own strategies bring to a decision it has nothing for: refused where they add up to "
          "0.005 bb/100 of the stake (two classes at 0.003 each), not where one alone does not; never with a class held in part")
    plan, small = made(rarely(3e-5)), made(rarely(3e-5), swing=1.0)
    check(plan["BB"]["free"] == [inner[0].id] and small["BB"]["free"] == [] and 0 < small["BB"]["unsaid_bb100"] < 0.005
          and preflop._swing_bb(settings("--stack", "12")) == 24.0
          and preflop._swing_bb(settings("--stack", "12", "--ante", "2")) == 26.0,
          "what shows is measured against the stake, two stacks and the ante: a hand at 1 deal in 100,000 shows "
          "with 24 big blinds to move and not with one")

    def seldom_limped(often):  # a limp that rare, and a big blind with nothing after it
        def change(h):
            limps(often * passed, [])(h)
            silent(h[first_bb[0].id])
        return change
    plan, rarer = made(seldom_limped(1e-5)), made(seldom_limped(1e-7))
    check(plan["BB"]["free"][0] == first_bb[0].id and first_bb[0].id in plan["BB"]["part"]
          and rarer["BB"]["free"][0] == first_bb[0].id and rarer["BB"]["part"] == [],
          "an opponent is said never to come to a decision where it comes too rarely to show against the stake: "
          "at 1 deal in 10,000,000, not at 1 in 100,000")
    fits = settings("--stack", "12", "--depth", "none", "--ante", "2")
    its = {"version": 1, "settings": preflop.describe(fits), "chips_per_bb": fits.chips,
           "nodes": [{"id": n.id, "actions": [label for label, _ in n.children], "hands": {}}
                     for n in decisions(preflop.build_tree(fits))]}

    def unfit(**change):
        try:
            return preflop._fits("x.json", dict(its, **change), fits, decisions(preflop.build_tree(fits))) and ""
        except preflop.PreflopError as e:
            return str(e)
    check(unfit() == [] and all("x.json is not a result of `preflop.py solve`" in unfit(version=v) for v in (True, 1.0, "1", None))
          and all(f"x.json is a version {v} result: this `preflop.py` reads version 1" in unfit(version=v) for v in (2, 0, -1))
          and "another preflop game" in unfit(chips_per_bb=20),
          "a result's version is the whole number 1: `true` and 1.0 are no result; 2, 0 and -1 are ones this tool does not read")
    check(abs(preflop._sum({"EV OOP": -3.0, "EV IP": 204.0}, fits) - 1.0) < 1e-9
          and abs(preflop._sum({"EV OOP": -3.0, "EV IP": 4.0}, s) - 1.0) < 1e-9,
          "the two values' sum is taken less the ante: 2 big blinds of dead money are 200 bb/100 that are not a misdeal")
    check(preflop._uneven(classes, dealt) and not preflop._uneven(classes, {"SB": [1, 0.5, 0, 1], "BB": [0, 0, 0, 0]})
          and preflop._uneven(["AA", "AA"], {"SB": [1, 0.5]}), "a range holds a class in part: a combo missing, or weights that differ")
    check(preflop._same("2.50bb 4x", "2.5bb 4.0x") and preflop._same("sNaNbb", "sNaNbb") and not preflop._same("sNaNbb", "1bb")
          and preflop._same("NaN", "NaN") and not preflop._same("NaN", "1"), "a setting that is no number compares as text")
    side = {"lost_bb100": [0.1, 0.0, 0.2], "sum_bb100": 0.86, "free": [], "locked": 3}
    report = {"flop_count": 3, "flops": "x.txt", "exploitable_bb100": 0.01, "sum_bb100": 0.82, "settings": {"depth": "file"},
              "value_bb100": {"SB": [1.0, 0.9, 1.1], "BB": [-1.0, -1.1, -0.9]},
              "checked": [{"file": "a.json", "flop_count": 3, "depth": "file", "other_postflop": ["file"], "SB": side,
                           "BB": dict(side, free=["r:0:c", "r:0:b250"], free_part=["r:0:b250"])},
                          {"file": "b.json", "flop_count": 3, "depth": "flop", "other_postflop": ["depth"], "SB": side,
                           "BB": dict(side, locked=0)}]}
    text = " ".join("\n".join(preflop.check_text(report)).split(" "))
    check(max(len(line) for line in text.splitlines()[1:]) <= 100, "its notes are wrapped")
    def off_text(bb_moves):
        off = dict(report, checked=[dict(report["checked"][0], BB=dict(side, rescaled_bb100=bb_moves, rescaled_most=["AA", "r:0:c", 1.0000001]),
                                         SB=dict(side, rescaled_bb100=1.234, rescaled_most=["KK", "r:0", 0.6]))])
        return " ".join("\n".join(preflop.check_text(off)).split())
    said, more = off_text(0.0049), off_text(0.005)
    check("a.json has frequencies of the small blind that do not add up to 1 (the row that counts the most: KK at r:0, which adds up to 0.6)" in said
          and "as much as 1.23 bb/100 more than the figure, or less" in said and "of the big blind that do not add up" not in said
          and "do not add up to 1" not in text,
          "rows that do not add up to 1 are named with what they can move, when that shows; a report without the figure reads as before")
    check("of the big blind that do not add up to 1 (the row that counts the most: AA at r:0:c, which adds up to 1 + 1e-07)" in more
          and "as much as 0.01 bb/100 more" in more and "of the small blind that do not add up to 1" in more,
          "the note comes from 0.005 bb/100 on, the first figure that is not printed as 0.00, and for each player that has one")
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
          and text.count("0.10  (0.00 to 0.20)") == 3 and text.count("(none of it to check)") == 1
          and "added up" not in "\n".join(preflop.check_text(exact))
          and "values, less the ante, added up to 0.82 to 0.86" in " ".join("\n".join(preflop.check_text(
              dict(report, settings={"depth": "file", "ante_bb": "2"}))).split())
          and "less the ante" not in "\n".join(preflop.check_text(dict(report, settings={"depth": "file", "ante_bb": "0.0"}))),
          "the check's text: the trees by their options, the decisions left free, a player with nothing locked, "
          "and the sum where it is not zero")
    every = dict(report, flops=preflop.ALL_FLOPS, flop_count=1755)
    told = [" ".join("\n".join(preflop.check_text(r)).split())
            for r in (report, every, dict(every, settings={"depth": "file", "ante_bb": "2"}))]
    check("a flop subset is not dealt quite evenly" in told[0]
          and all("flop subset" not in t and "added up to 0.82 to 0.86" in t for t in told[1:])
          and all("the solver's own figures" in t for t in told[1:])
          and "less the ante" not in told[1] and "less the ante" in told[2],
          "a sum that is not zero is put down to the flop subset only where the flops are one")


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
        bad += [("solve", out_file, "--memory", "inf"), ("solve", out_file, "--memory", "1e308"), ("solve", out_file, "--accuracy", "inf"),
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
        check(status == 0 and "flops: the largest shipped subset within 256 MB" in err and out.rstrip().endswith("add_all_flops")
              and 25 <= sum(l.startswith("add_to_subset") for l in out.splitlines()) <= 100,
              f"without --flops the largest shipped subset under the budget is taken ({err.strip()[:60]})")
        status, out, err = run("script", "--stack", "20", "--memory", "0.01", "--solver", exe)
        check(status == 1 and "--depth" in err, "and when not even 10 flops fit, that is said")

        # the budget holds the tree that is built (#504). With turn bets there are two trees: the smallest,
        # and the one with outcome tables at every level, which the solver builds when memory is free
        turn = ("--stack", "20", "--depth", "turn", "--solver", exe)
        st = settings(*turn[:4])
        small, with_tables = preflop.estimates_mb(exe, st, flops)
        built = {}
        for mode in ("small", "fast"):
            with preflop.Solver(exe) as u:
                for line in preflop.setup_lines(st, flops, None, mode):
                    u.need(line)
                u.need("add_all_flops")
                built[mode] = preflop.built_mb(u)
        check(0 < small < with_tables and built == {"small": small, "fast": with_tables}
              and preflop.estimate_mb(exe, st, flops) == small and len(set(preflop.estimates_mb(exe, s, flops))) == 1,
              f"estimates: the two trees of a tree with turn bets as they are built ({small} and {with_tables} MB), "
              "one figure where the turn has no bets")

        def budget_solve(name, mb, free=None, checked=None, flops=flop_file, opts=turn):
            """solve under --memory of `mb` megabytes (None: without --memory), the solver told that `free`
            MB are free -> (status, stderr, the result). With `checked`, a result's name: `check` of that
            result instead, and its report. `flops`: None leaves the subset to the tool."""
            path = os.path.join(d, name)
            old = os.environ.get("WSOLVER_FREE_MB")
            if free is not None:
                os.environ["WSOLVER_FREE_MB"] = str(free)
            command = ("check", os.path.join(d, checked), "--report", path) if checked else ("solve", path)
            given = (("--flops", flops) if flops else ()) + (("--memory", repr(mb / 1024)) if mb is not None else ())
            try:
                status, _, err = run(*command, *given, "--accuracy", "50", "--time", "1", *opts)
            finally:
                if old is None:
                    os.environ.pop("WSOLVER_FREE_MB", None)
                else:
                    os.environ["WSOLVER_FREE_MB"] = old
            return status, err, json.load(open(path)) if os.path.exists(path) else {}

        between = (small + with_tables) // 2
        status, err, r = budget_solve("between.json", between, 4096)
        check(status == 0 and (r.get("estimated_mb"), r.get("built_mb"), r.get("outcome_tables")) == (small, small, "small")
              and "set_outcome_tables small" in err and "the tree built" not in err,
              f"solve --depth turn --memory: a budget of {between} MB between the two builds the smallest tree, "
              f"{r.get('built_mb')} MB by the solver's count, whatever memory is free")
        status, err, r = budget_solve("room.json", with_tables, 4096)
        check(status == 0 and (r.get("estimated_mb"), r.get("built_mb"), r.get("outcome_tables")) == (small, with_tables, "auto")
              and f"the tree built: {with_tables} MB" in err,
              f"a budget of {with_tables} MB leaves the choice to the solver, which builds the tables with memory free")
        status, err, r = budget_solve("short.json", with_tables, with_tables)   # nine tenths of it is not enough
        check(status == 0 and (r.get("built_mb"), r.get("outcome_tables")) == (small, "auto")
              and f"the tree built: {small} MB without outcome tables at every level (the {with_tables} MB" in err,
              "and the smallest tree with too little free for them, which the tool says")
        status, err, r = budget_solve("over.json", small - 1, 4096)
        check(status == 1 and f"estimated at {small} MB" in err and "--memory" in err and not r,
              "--flops over --memory is refused, with no result")
        # check takes the budget the way solve does
        status, err, r = budget_solve("between.report.json", between, 4096, checked="between.json")
        check(status == 0 and (r.get("estimated_mb"), r.get("built_mb"), r.get("outcome_tables")) == (small, small, "small")
              and "set_outcome_tables small" in err and "the tree built" not in err,
              f"check --memory: a budget between the two builds the smallest tree ({r.get('built_mb')} MB) whatever memory is free")
        status, err, r = budget_solve("room.report.json", with_tables, 4096, checked="between.json")
        check(status == 0 and (r.get("estimated_mb"), r.get("built_mb"), r.get("outcome_tables")) == (small, with_tables, "auto")
              and f"the tree built: {with_tables} MB" in err,
              "a budget that holds the tables leaves the choice to the solver, and the report has the solver's count of the tree")
        status, err, r = budget_solve("short.report.json", with_tables, with_tables, checked="between.json")
        check(status == 0 and (r.get("built_mb"), r.get("outcome_tables")) == (small, "auto")
              and f"the tree built: {small} MB without outcome tables at every level (the {with_tables} MB" in err,
              "check says it too when too little is free for the tables")
        status, err, r = budget_solve("over.report.json", small - 1, 4096, checked="between.json")
        check(status == 1 and f"estimated at {small} MB" in err and not r, "and flops over the budget are refused, with no report")
        # without a budget the choice is the solver's, and the tool says what it was
        left_out = f"the tree built: {small} MB without outcome tables at every level (the {with_tables} MB"
        both = (f"flops: estimated {small} MB; {with_tables} MB with outcome tables at every level, which the solver "
                "builds when they fit in nine tenths of the memory that is free\n")
        runs = [budget_solve("free_short.json", None, with_tables), budget_solve("free_room.json", None, 4096),
                budget_solve("free_short.report.json", None, with_tables, checked="between.json")]
        check(all(status == 0 and both in err and "set_outcome_tables small" not in err and "built without them" not in err
                  and r.get("estimated_mb") == small and r.get("outcome_tables") == "auto" for status, err, r in runs)
              and [r.get("built_mb") for _, _, r in runs] == [small, with_tables, small]
              and [left_out in err for _, err, _ in runs] == [True, False, True]
              and f"the tree built: {with_tables} MB with outcome tables at every level" in runs[1][1],
              "--flops without --memory: both sizes are estimated, with no word of a budget, and solve and check say which "
              "tree the solver built")
        status, out, err = run("script", "--flops", flop_file, "--memory", repr(between / 1024), *turn)
        lines = out.splitlines()
        status2, out2, err2 = run("script", "--flops", flop_file, "--memory", repr(with_tables / 1024), *turn)
        status3, out3, err3 = run("script", "--flops", flop_file, "--memory", "1", "--stack", "20", "--solver", exe)
        check(status == 0 and lines[-2:] == ["set_outcome_tables small", "add_all_flops"]
              and status2 == 0 and out2.splitlines() == lines[:-2] + lines[-1:]
              and "when they fit in nine tenths of the memory that is free" in err2
              and f"estimated {small} MB, within --memory's {with_tables} MB" in err2
              and status3 == 0 and "set_outcome_tables" not in out3 and "outcome tables" not in err3 and "within --memory" in err3,
              "script: the line that keeps the tree within the budget, only where it is needed")
        # the tool's own choice of a subset: by the smallest tree, the tables by the same budget
        small10, with10 = preflop.estimates_mb(exe, settings("--stack", "5", "--no-limp", "--depth", "turn"),
                                               preflop.choose_flops("10")[1])
        picks = [run("script", "--stack", "5", "--no-limp", "--depth", "turn", "--memory", repr(mb / 1024), "--solver", exe)
                 for mb in (with10 - 1, with10)]
        check(small10 < with10 and all(p[0] == 0 and f"10 flops: the largest shipped subset within {mb} MB" in p[2]
                                       and f"estimated {small10} MB; {with10} MB with outcome tables" in p[2]
                                       for p, mb in zip(picks, (with10 - 1, with10)))
              and [("set_outcome_tables small" in p[1].splitlines(), "set_outcome_tables" in p[1]) for p in picks]
              == [(True, True), (False, False)],
              f"without --flops: 10 flops by the smallest tree ({small10} MB), built without the tables one MB under "
              f"the {with10} they take and left to the solver at it")
        own5 = ("--stack", "5", "--no-limp", "--depth", "turn", "--solver", exe)
        status, err, r = budget_solve("own_small.json", with10 - 1, 4096, flops=None, opts=own5)
        check(status == 0 and "the tree built" not in err
              and [r.get(key) for key in ("flop_count", "estimated_mb", "built_mb", "outcome_tables")] == [10, small10, small10, "small"],
              "solve on the tool's own subset builds within the budget: without the tables one MB under them, whatever is free")
        status, err, r = budget_solve("own_short.json", with10, with10, flops=None, opts=own5)
        check(status == 0 and [r.get(key) for key in ("flop_count", "built_mb", "outcome_tables")] == [10, small10, "auto"]
              and f"the tree built: {small10} MB without outcome tables at every level (the {with10} MB" in err,
              "and says when the solver left the tables out of it for lack of free memory")
        status, err, r = budget_solve("own_small.report.json", with10 - 1, 4096, checked="own_small.json", flops=None, opts=own5)
        check(status == 0 and "the tree built" not in err
              and [r.get(key) for key in ("estimated_mb", "built_mb", "outcome_tables")] == [small10, small10, "small"],
              "check without --flops takes the budget the same way")
        # the budget nobody typed, half of the machine's memory, decides the same way
        machine = preflop.physical_memory_mb
        try:
            halves = []
            for mb in (with10 - 1, with10):
                preflop.physical_memory_mb = lambda mb=mb: 2 * mb
                halves.append(run("script", "--stack", "5", "--no-limp", "--depth", "turn", "--solver", exe))
        finally:
            preflop.physical_memory_mb = machine
        check(all(p[0] == 0 and f"10 flops: the largest shipped subset within {mb} MB (half of this machine)" in p[2]
                  for p, mb in zip(halves, (with10 - 1, with10)))
              and ["set_outcome_tables small" in p[1].splitlines() for p in halves] == [True, False]
              and [p[1] for p in halves] == [p[1] for p in picks],
              "and so does the default budget, half of the machine's memory: the same two scripts")

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
        limp = next(n["actions"] for n in whole["nodes"] if n["id"] == "r:0").index("limp")

        def no_limp(by):  # a small blind that folds what it limped, and a big blind with nothing after a limp
            for h in by["r:0"].values():
                h["freq"] = [0.0 if a == limp else f + (h["freq"][limp] if a == len(h["freq"]) - 1 else 0.0)
                             for a, f in enumerate(h["freq"])]
            nothing(by["r:0:c"])
        silent = edited("nolimp.json", no_limp)

        def fifths(by):  # the small blind's first decision with every row at three fifths of itself, the
            for h in by["r:0"].values():   # big blind's after a limp at a half
                h["freq"] = [0.6 * f for f in h["freq"]]
            for h in by["r:0:c"].values():
                h["freq"] = [0.5 * f for f in h["freq"]]
        status, out, err = run("check", own, silent, edited("fifths.json", fifths), "--flops", five, "--accuracy", "0.02",
                               "--report", report, *opts)
        with open(report) as f:
            rows = json.load(f)["checked"]
        was, now = ([r[who]["lost_bb100"][0] for who in ("SB", "BB")] for r in rows[:2])
        sums = [sum(h["freq"]) for n in whole["nodes"] for h in n["hands"].values()]
        check(max(abs(x - 1) for x in sums if x > 0) < 1e-12 and all(rows[0][who]["rescaled_bb100"] < 1e-9 for who in ("SB", "BB"))
              and "do not add up" not in out.split("fifths.json has")[0],
              "the rows a solve writes add up to 1 (the solver's own are 1e-7 off), and the check has nothing to say of them")
        cut = rows[2] if len(rows) > 2 else None
        text = " ".join(out.split())
        check(cut is not None and abs(cut["SB"]["rescaled_bb100"] - 0.4 * 2400) < 1e-6 and abs(cut["BB"]["rescaled_bb100"] - 0.5 * 2400) < 1e-6
              and abs(cut["SB"]["rescaled_most"][2] - 0.6) < 1e-9 and cut["SB"]["rescaled_most"][1] == "r:0"
              and abs(cut["BB"]["rescaled_most"][2] - 0.5) < 1e-9 and cut["BB"]["rescaled_most"][1] == "r:0:c"
              and "fifths.json has frequencies of the small blind that do not add up to 1 (the row that counts the most: " in text
              and "at r:0, which adds up to 0.6)" in text and "as much as 960.00 bb/100 more than the figure, or less" in text
              and "fifths.json has frequencies of the big blind that do not add up to 1" in text
              and "at r:0:c, which adds up to 0.5)" in text and "as much as 1200.00 bb/100 more" in text
              and all(abs(cut[who]["lost_bb100"][0] - rows[0][who]["lost_bb100"][0]) < 0.05 for who in ("SB", "BB")),
              "rows at three fifths of themselves (the big blind's after a limp: at a half) are checked as the rows they "
              "are a part of, and named with what they leave open, each player's own: all of the small blind's hands, "
              "two fifths, 24 bb a hand")
        has = {who: sum(n["actor"] == who for n in whole["nodes"]) for who in ("SB", "BB")}
        check(min(has.values()) >= 2 and [rows[0][who]["locked"] for who in ("SB", "BB")] == [has["SB"], has["BB"]]
              and rows[1]["BB"]["locked"] == has["BB"] - len(rows[1]["BB"]["free"]) < has["BB"],
              f"the report counts the decisions it locked: all of a whole result's ({has['SB']} and {has['BB']}), "
              f"and those not left free of the other's ({rows[1]['BB']['locked']})")
        free = [r[who]["free"] for r in rows for who in ("SB", "BB")]
        check(status == 0 and free[:3] == [[], [], []] and free[3][0] == "r:0:c" and all(n.startswith("r:0:c") for n in free[3])
              and rows[1]["BB"]["free_part"] == free[3][1:]
              and "nolimp.json has no strategy at 1 of the big blind's decisions (r:0:c): its small blind never goes there"
              in " ".join(out.split()) and now[1] < was[1] + 0.05 and now[0] > was[0] + 1,
              f"a result with nothing at the big blind's decision after a limp its small blind never makes: left free "
              f"and said, and no loss made up there ({now[1]:+.3f} for {was[1]:+.3f} with it; the small blind's "
              f"{now[0]:+.2f} for not limping)")
        for name, change, said in (
                ("five.json", lambda by: by["r:0:b250:b1200"]["AA"].update(freq=5), "the frequencies of AA at r:0:b250:b1200"),
                ("noaa.json", lambda by: by["r:0"]["AA"].update(freq=[0.0, 0.0, 0.0]),
                 "no strategy for 1 hand classes of the small blind's range at r:0 (AA)"),
                ("no72.json", lambda by: by["r:0:c"]["72o"].update(freq=[0.0] * len(by["r:0:c"]["72o"]["freq"])),
                 "no strategy for 1 hand classes of the big blind's range at r:0:c (72o)"),
                ("noroot.json", lambda by: nothing(by["r:0"]), "no strategy for the small blind's first decision"),
                # the third review's: a whole decision emptied where the result's own small blind limps, and
                # one where its own big blind shoves; an integer no float holds
                ("silent.json", lambda by: nothing(by["r:0:c"]),
                 "no strategy at r:0:c for hands of the big blind's range that its own strategies bring there"),
                ("gone.json", lambda by: by["r:0:b250:b1200"].clear(),
                 "no strategy at r:0:b250:b1200 for hands of the small blind's range that its own strategies bring there"),
                ("huge.json", lambda by: by["r:0"]["AA"].update(freq=[10 ** 400, 0, 0]), "the frequencies of AA at r:0")):
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
        with open(part) as f:
            cut = json.load(f)
        del next(n for n in cut["nodes"] if n["id"] == "r:0")["hands"]["AA"]
        with open(os.path.join(d, "cut.json"), "w") as f:
            json.dump(cut, f)
        status, _, err = run("check", os.path.join(d, "cut.json"), "--accuracy", "0.05", *suits)
        check(status == 1 and "no strategy for 1 hand classes of the small blind's range at r:0 (AA)" in err,
              "at the small blind's first decision a class held in part is there too: without it, refused")
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
        check(status == 0 and "added up to" in out and "not to zero" in out and "flop subset" in out,
              "on a flop subset the check says what the two values add up to")
        # with an ante, on all flops: the stake is two stacks and the ante, and the sums are taken less the
        # ante (left in, each would be the ante, 100 bb/100) and are zero then, with nothing to note (#505:
        # the solver's two values were 0.03 bb/100 short of the ante here)
        game = ("--stack", "8", "--ante", "1", "--depth", "none", "--range-sb", "AA,KQs,72o", "--range-bb", "QQ,T9s,32o",
                "--flops", "all", "--accuracy", "0.05", "--solver", exe)
        anted, report = os.path.join(d, "anted.json"), os.path.join(d, "anted_report.json")
        stakes, plan = [], preflop._plan

        def watched(*args):
            stakes.append(args[-1])
            return plan(*args)
        preflop._plan = watched
        try:
            solved = run("solve", anted, *game)[0]
            status, out, err = run("check", anted, "--report", report, *game)
        finally:
            preflop._plan = plan
        try:
            with open(report) as f:
                rep = json.load(f)
            sums = [rep["sum_bb100"]] + [r[who]["sum_bb100"] for r in rep["checked"] for who in ("SB", "BB")]
        except (OSError, ValueError, KeyError):
            sums = []
        check(solved == 0 and status == 0 and stakes == [17.0] and len(sums) == 3
              and not any(preflop._shows(x) for x in sums) and "added up" not in out,
              f"check with an ante of 1 bb on all flops: a stake of 17 bb, and the values less the ante add up "
              f"to zero ({', '.join(f'{x:+.3f}' for x in sums)}; {err.strip()[-80:] if status else 'ok'})")


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
