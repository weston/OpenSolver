#!/usr/bin/env python3
"""Tests for tools/tree_builder.py (bet-size config -> add_line commands).

The main test is an oracle test: tests/data/pio_25bbHU_2sizes.txt holds both a PioViewer
config header and the 266 add_line commands PioViewer generated from it, so the builder has
to reproduce that set exactly.

Usage: python3 tests/test_tree_builder.py
"""
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import tree_builder as tb  # noqa: E402

FIXTURE = os.path.join(HERE, "data", "pio_25bbHU_2sizes.txt")

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def fixture_text():
    with open(FIXTURE) as f:
        return f.read()


def test_parse():
    cfg = tb.parse_config(fixture_text())
    check(cfg.pot == 40 and cfg.stack == 230, "parse: pot 40, effective stack 230")
    check(cfg.allin_threshold == Decimal(67), "parse: AllinThreshold 67")
    check(cfg.add_allin_max_pot_mult == Decimal(300),
          "parse: AddAllinOnlyIfLessThanThisTimesThePot 300")
    check(cfg.board == "QhQc2c", "parse: board QhQc2c")

    flop_oop = cfg.street(0, tb.OOP)
    check(flop_oop.bet == [Decimal(30), Decimal(60)], "parse: FlopConfig.BetSize 30,60")
    check(flop_oop.raise_ == [Decimal(40)], "parse: FlopConfig.RaiseSize 40")
    check(flop_oop.donk == [Decimal(30), Decimal(60)], "parse: FlopConfig.DonkBetSize 30,60")
    check(not flop_oop.add_allin, "parse: flop has no AddAllin")

    river_oop, river_ip = cfg.street(2, tb.OOP), cfg.street(2, tb.IP)
    check(river_oop.bet == [Decimal(65)], "parse: RiverConfig.BetSize 65")
    check(river_oop.add_allin and river_ip.add_allin,
          "parse: RiverConfig.AddAllin and RiverConfigIP.AddAllin are True")
    check(cfg.street(0, tb.IP).bet == [Decimal(30), Decimal(60)],
          "parse: FlopConfigIP.BetSize 30,60 lands on the IP config")


def test_matches_pioviewer():
    text = fixture_text()
    cfg = tb.parse_config(text)
    got = [tuple(l) for l in tb.build_lines(cfg)]
    want = [tuple(l) for l in tb.extract_lines(text)]

    check(len(want) == 266, f"fixture carries 266 recorded lines (got {len(want)})")
    check(len(got) == len(set(got)), "generated lines contain no duplicates")

    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    check(not missing and not extra,
          "generated line set equals PioViewer's exactly"
          + (f" (missing {len(missing)}, extra {len(extra)})" if missing or extra else ""))
    for line in missing[:5]:
        print("      missing:", " ".join(map(str, line)))
    for line in extra[:5]:
        print("      extra:  ", " ".join(map(str, line)))


def test_no_redundant_lines():
    """No generated line may be a prefix of another: the solver implies the shorter one."""
    cfg = tb.parse_config(fixture_text())
    lines = [tuple(l) for l in tb.build_lines(cfg)]
    bad = [l for l in lines if any(o != l and o[: len(l)] == l for o in lines)]
    check(not bad, f"no line is a prefix of another ({len(bad)} redundant)")
    check(all(l[-1] == cfg.stack for l in lines),
          "every line ends in the all-in (the deepest action of its branch)")


def test_sizing_arithmetic():
    """Hand-computed spots, straight from the rules in tools/tree_builder.py."""
    cfg = tb.parse_config(fixture_text())

    # Flop, nobody has invested: pot 40, bets are 30% and 60%.
    root = tb.Node(0, (0, 0), tb.OOP, False, None)
    check(tb._aggressive_totals(cfg, root) == [12, 24], "flop OOP bets 12 and 24 (30%/60% of 40)")

    # OOP bet 12, IP raises: 40% of the pot once called (40+12+12=64) on top of the 12.
    faced = tb.Node(0, (12, 0), tb.IP, True, None)
    check(tb._aggressive_totals(cfg, faced) == [38], "flop IP raises 12 -> 38 (12 + 40% of 64)")

    # IP raised to 38, OOP re-raises: 38 + 40% of (40+38+38=116) = 38 + 46.
    faced3 = tb.Node(0, (12, 38), tb.OOP, True, None)
    check(tb._aggressive_totals(cfg, faced3) == [84], "flop OOP re-raises 38 -> 84 (38 + 40% of 116)")

    # Facing 84 the configured raise is 84 + 40% of 208 = 167, which is >= 67% of 230,
    # so AllinThreshold replaces it with the all-in.
    faced4 = tb.Node(0, (84, 38), tb.IP, True, None)
    check(tb._aggressive_totals(cfg, faced4) == [230],
          "AllinThreshold turns the 167 raise into the 230 all-in")

    # Turn after flop bet-call: pot 64, so bets are 12 + 19 and 12 + 38.
    turn = tb.Node(1, (12, 12), tb.OOP, False, tb.OOP)
    check(tb._aggressive_totals(cfg, turn) == [31, 50],
          "turn bets 31 and 50 (30%/60% of 64, rounded half-up from 19.2/38.4)")

    # River bets 65% of the pot.
    river = tb.Node(2, (0, 0), tb.OOP, False, None)
    check(tb._aggressive_totals(cfg, river) == [26], "river OOP bets 26 (65% of 40)")


def test_add_allin_gate():
    """AddAllin only fires once the pot is big enough relative to the stack."""
    cfg = tb.parse_config(fixture_text())

    # River, pot 40, no bet yet: 300% of 40 = 120 < 230, so no all-in size is offered and
    # 65% is the only bet. (PioViewer likewise records no `0 0 0 0 230` line.)
    first = tb.Node(2, (0, 0), tb.OOP, False, None)
    check(tb._aggressive_totals(cfg, first) == [26],
          "AddAllin skipped on a 40-chip river pot (300% of 40 < 230)")

    # Facing the 26 bet, the pot once called is 92 and 300% of that is 276 > 230, so the
    # all-in joins the configured 63 raise.
    facing = tb.Node(2, (0, 26), tb.OOP, True, None)
    check(tb._aggressive_totals(cfg, facing) == [63, 230],
          "AddAllin offers 230 next to the 63 raise (300% of 92 > 230)")

    lines = {tuple(l) for l in tb.build_lines(cfg)}
    check((0, 0, 0, 0, 230) not in lines, "no all-in river donk line on the smallest pot")
    check((0, 0, 0, 0, 0, 26, 230) in lines, "river check, bet 26, all-in line is present")


def test_rounding_is_nearest():
    """Truncating or always rounding up both break the fixture; nearest is required."""
    import decimal

    text = fixture_text()
    want = {tuple(l) for l in tb.extract_lines(text)}
    orig = tb._round
    try:
        for name, mode in (("ROUND_DOWN", decimal.ROUND_DOWN),
                           ("ROUND_CEILING", decimal.ROUND_CEILING)):
            tb._round = lambda x, m=mode: int(x.quantize(Decimal(1), rounding=m))
            got = {tuple(l) for l in tb.build_lines(tb.parse_config(text))}
            check(got != want, f"{name} rounding does not reproduce the fixture (as expected)")
    finally:
        tb._round = orig
    got = {tuple(l) for l in tb.build_lines(tb.parse_config(text))}
    check(got == want, "half-up rounding restored and still matches")


SYNTHETIC = """\
#Pot#100
#EffectiveStacks#10000
#AllinThreshold#100
#FlopConfig.BetSize#50
#TurnConfig.BetSize#50
#RiverConfig.BetSize#50
#FlopConfigIP.BetSize#50
#TurnConfigIP.BetSize#50
#RiverConfigIP.BetSize#50
"""


def test_branch_ending_on_a_plain_bet():
    """A branch need not bottom out on an all-in.

    With no RaiseSize, no AddAllin and a stack nothing can reach, the deepest action of every
    branch is an ordinary bet. Those lines still have to be emitted.
    """
    cfg = tb.parse_config(SYNTHETIC)
    lines = [tuple(l) for l in tb.build_lines(cfg)]

    check(lines, "synthetic config produces lines at all")
    check(all(cfg.stack not in l for l in lines),
          "no line reaches the 10000 stack (nothing is an all-in here)")
    # OOP leads every street: bet 50 (50% of the 100 pot), called; bet 150 (50% of 200),
    # called; bet 350 (50% of 400).
    check((50, 50, 150, 150, 350) in lines,
          "bet-call, bet-call, bet line is present and ends on the river bet")
    # The longest branch is OOP checking and calling on every street: three checks, three
    # bets and two calls, ending on IP's river bet.
    check((0, 50, 50, 50, 150, 150, 150, 350) in lines,
          "check/bet/call on all three streets, ending on IP's river bet")
    check(max(len(l) for l in lines) == 8, "longest line is that eight-action chain")
    bad = [l for l in lines if any(o != l and o[: len(l)] == l for o in lines)]
    check(not bad, f"synthetic lines carry no prefix duplicates ({len(bad)})")


def test_full_script():
    text = fixture_text()
    cfg = tb.parse_config(text)
    script = tb.format_script(cfg, tb.build_lines(cfg))
    check(script[-1] == "build_tree ", "--full script ends with build_tree")
    check("set_pot 0 0 40" in script, "--full script sets the pot")
    check("set_board QhQc2c" in script, "--full script sets the board")
    check("set_eff_stack 230" in script, "--full script sets the effective stack")
    check(sum(1 for s in script if s.startswith("add_line ")) == 266,
          "--full script carries all 266 add_line commands")
    check(script[0].startswith("set_range OOP ") and script[1].startswith("set_range IP "),
          "--full script forwards both ranges verbatim")


def main():
    for fn in (test_parse, test_matches_pioviewer, test_no_redundant_lines,
               test_sizing_arithmetic, test_add_allin_gate, test_rounding_is_nearest,
               test_branch_ending_on_a_plain_bet, test_full_script):
        print(fn.__name__)
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
