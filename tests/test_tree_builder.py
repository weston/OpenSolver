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
    check(flop_oop.bet == [tb.SizeSpec("pct", Decimal(30)), tb.SizeSpec("pct", Decimal(60))],
          "parse: FlopConfig.BetSize 30,60")
    check(flop_oop.raise_ == [tb.SizeSpec("pct", Decimal(40))], "parse: FlopConfig.RaiseSize 40")
    check(flop_oop.donk == [tb.SizeSpec("pct", Decimal(30)), tb.SizeSpec("pct", Decimal(60))],
          "parse: FlopConfig.DonkBetSize 30,60")
    check(not flop_oop.add_allin, "parse: flop has no AddAllin")

    river_oop, river_ip = cfg.street(2, tb.OOP), cfg.street(2, tb.IP)
    check(river_oop.bet == [tb.SizeSpec("pct", Decimal(65))], "parse: RiverConfig.BetSize 65")
    check(river_oop.add_allin and river_ip.add_allin,
          "parse: RiverConfig.AddAllin and RiverConfigIP.AddAllin are True")
    check(cfg.street(0, tb.IP).bet == [tb.SizeSpec("pct", Decimal(30)),
                                       tb.SizeSpec("pct", Decimal(60))],
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
          "turn bets 31 and 50 (30%/60% of 64, rounded to nearest from 19.2/38.4)")

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


GEO_SPR3 = """\
#Pot#100
#EffectiveStacks#300
#AllinThreshold#100
#FlopConfig.BetSize#e
#TurnConfig.BetSize#e
#RiverConfig.BetSize#e
#FlopConfigIP.BetSize#e
#TurnConfigIP.BetSize#e
#RiverConfigIP.BetSize#e
"""


def test_parse_size_forms():
    check(tb.parse_size_list("33, 75, 2e, 3x, a") == [
        tb.SizeSpec("pct", Decimal(33)), tb.SizeSpec("pct", Decimal(75)),
        tb.SizeSpec("geo", Decimal(2)), tb.SizeSpec("mult", Decimal(3)),
        tb.SizeSpec("allin")], "parse: mixed list `33, 75, 2e, 3x, a`")
    check(tb.parse_size_list("e") == [tb.SizeSpec("geo", None)], "parse: `e` = geometric over the rest")
    check(tb.parse_size_list("2.5x") == [tb.SizeSpec("mult", Decimal("2.5"))],
          "parse: decimal raise multiple")
    # `NeP` / `eP` are PioViewer's geometric sizes with a second parameter (`2e50`, `e50`; #313)
    check(tb.parse_size_list("2e50 e50") == [tb.SizeSpec("geo", Decimal(2), Decimal(50)),
                                             tb.SizeSpec("geo", None, Decimal(50))], "parse: NeP and eP")
    check(tb.parse_size_list("5e 3e,  2e a") == tb.parse_size_list("5e,3e,2e,a"),
          "parse: entries separated by spaces, commas or both (#313)")
    for bad in ("2q", "x3", "0e", "-3", "-2x", "xx"):
        try:
            tb.parse_size_list(bad)
            check(False, f"parse: {bad!r} is rejected")
        except ValueError:
            check(True, f"parse: {bad!r} is rejected")
    check(tb.parse_size_list("") == [] and tb.parse_size_list(" , ") == [],
          "parse: empty list stays empty")
    try:
        tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#FlopConfig.BetSize#3x\n")
        check(False, "parse: Nx in BetSize is rejected")
    except ValueError:
        check(True, "parse: Nx in BetSize is rejected")
    cfg = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#FlopConfig.RaiseCap#2\n"
                          "#TurnConfigIP.RaiseCap#1\n")
    check(cfg.street(0, tb.OOP).raise_cap == 2 and cfg.street(0, tb.IP).raise_cap is None,
          "parse: #FlopConfig.RaiseCap#2 sets OOP's cap")
    check(tb._street_cap(cfg, 0) == 2 and tb._street_cap(cfg, 1) == 1
          and tb._street_cap(cfg, 2) is None,
          "street cap: one player's value applies; unset is uncapped")
    cfg2 = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#FlopConfig.RaiseCap#3\n"
                           "#FlopConfigIP.RaiseCap#2\n")
    check(tb._street_cap(cfg2, 0) == 2, "street cap: the smaller of the two applies")


def test_geometric_bets_spr3():
    """Issue's hand example: pot 100, stack 300 (SPR 3).

    2e: f = (sqrt(7) - 1)/2 = 82.29% -> rounds to 82; that f repeated on the second
    street bets 82.29 then 217.71, 300.00 exactly (299 after per-street rounding).
    3e: f = (7^(1/3) - 1)/2 = 45.65% -> 46; repeated, 45.65 + 87.64 + 167.98 = 300.00.
    `e` recomputed each street (3, then 2, then 1 remaining) lands on the all-in exactly.
    """
    # Unrounded property: repeated f-bets put the stack in exactly (up to Decimal
    # precision; fractional powers carry ~1e-25 residue).
    for n in (2, 3):
        f = tb._geo_fraction(300, 100, n)
        pot, total = Decimal(100), Decimal(0)
        for _ in range(n):
            b = f * pot
            pot += 2 * b
            total += b
        check(abs(total - 300) < Decimal("1e-15"),
              f"{n}e unrounded cascade puts the 300 stack in exactly (error {300 - total})")
        check(abs(pot - 700) < Decimal("1e-15"),
              f"{n}e unrounded cascade grows the pot 100 -> 700")

    bets2e = GEO_SPR3.replace("#FlopConfig.BetSize#e", "#FlopConfig.BetSize#2e")
    cfg = tb.parse_config(bets2e)
    root = tb.Node(0, (0, 0), tb.OOP, False, None)
    check(tb._aggressive_totals(cfg, root) == [82],
          "2e flop bet at SPR 3 is 82 (82.29% of 100)")

    bets3e = GEO_SPR3.replace("#FlopConfig.BetSize#e", "#FlopConfig.BetSize#3e")
    cfg = tb.parse_config(bets3e)
    check(tb._aggressive_totals(cfg, root) == [46],
          "3e flop bet at SPR 3 is 46 (45.65% of 100)")

    # `e` on every street, bet called each time: 46 -> 133 -> 300 (exact all-in).
    cfg = tb.parse_config(GEO_SPR3)
    flop = tb.Node(0, (0, 0), tb.OOP, False, None)
    turn = tb.Node(1, (46, 46), tb.OOP, False, tb.OOP)
    river = tb.Node(2, (133, 133), tb.OOP, False, tb.OOP)
    check(tb._aggressive_totals(cfg, flop) == [46], "e flop bet is the 3e size, 46")
    check(tb._aggressive_totals(cfg, turn) == [133],
          "e turn bet is 87 more, 133 total (2-street geometric from pot 192, 254 behind)")
    check(tb._aggressive_totals(cfg, river) == [300],
          "e river bet is the last 167 exactly -> the all-in")
    check((46, 46, 133, 133, 300) in {tuple(l) for l in tb.build_lines(cfg)},
          "OOP bet-call down-the-streets line is 46 46 133 133 300")

    # `e` recomputed per street is the consistent staircase (46 -> 133 -> 300 above).
    # A fixed `3e` instead recomputes a three-street fraction from the current pot/stacks
    # every street and so never gets there: 46 -> 98 -> 147.
    bets3e_all = GEO_SPR3.replace("BetSize#e", "BetSize#3e")
    cfg3 = tb.parse_config(bets3e_all)
    turn3e = tb.Node(1, (46, 46), tb.OOP, False, tb.OOP)
    check(tb._aggressive_totals(cfg3, turn3e) == [98],
          "3e recomputed on the turn is a 3-street fraction from pot 192: 52 more, 98")
    river3e = tb.Node(2, (98, 98), tb.OOP, False, tb.OOP)
    check(tb._aggressive_totals(cfg3, river3e) == [147],
          "3e recomputed on the river is 49 more, 147 (no all-in: smaller each time)")


def test_geometric_raises():
    """A geometric raise is a geometric bet of the increment on the pot-if-called."""
    cfg = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#AllinThreshold#100\n"
                          "#FlopConfigIP.RaiseSize#2e\n")
    faced = tb.Node(0, (50, 0), tb.IP, True, None)
    check(tb._aggressive_totals(cfg, faced) == [137],
          "2e raise vs a 50 bet: 50 + 43.54% of pot-if-called 200 = 137")

    # Exactness property (unrounded, ~1e-25 Decimal residue): a called 2e raise leaves
    # pot and stacks where the same fraction finishes the one remaining street exactly.
    f = tb._geo_fraction(250, 200, 2)
    raised = Decimal(50) + f * 200
    behind = Decimal(300) - raised
    pot = Decimal(100) + 2 * raised
    check(abs(tb._geo_fraction(behind, pot, 1) - f) < Decimal("1e-15"),
          "after a 2e raise is called, one more street of the same fraction gets it in")

    # Rounded cascade: raise to 137 called -> pot 374, 163 behind; `e` (2 streets)
    # bets 69 more, then the river shove is exactly the last 94. IP made the raise, so
    # OOP leading the next street is a donk spot: since #152 the `e` sizes must be
    # configured as DonkBetSize (an empty donk list means OOP cannot lead at all).
    cfg_e = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#AllinThreshold#100\n"
                            "#TurnConfig.DonkBetSize#e\n#RiverConfig.DonkBetSize#e\n")
    turn = tb.Node(1, (137, 137), tb.OOP, False, tb.IP)
    check(tb._aggressive_totals(cfg_e, turn) == [206],
          "after the called raise, e turn bet totals 206 (69 more)")
    river = tb.Node(2, (206, 206), tb.OOP, False, tb.IP)
    check(tb._aggressive_totals(cfg_e, river) == [300],
          "river e shove is exactly the remaining 94")

    one_e = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#AllinThreshold#100\n"
                            "#FlopConfigIP.RaiseSize#1e\n")
    check(tb._aggressive_totals(one_e, faced) == [300], "a 1e raise is the all-in")
    three_e = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#AllinThreshold#100\n"
                              "#FlopConfigIP.RaiseSize#3e\n")
    check(tb._aggressive_totals(three_e, faced) == [102],
          "3e raise vs 50: 50 + 25.93% of 200 = 102")


def test_raise_multiples():
    """Nx raises by N times the increment faced, on top of what the raiser has in (#313)."""
    cfg = tb.parse_config("#Pot#100\n#EffectiveStacks#10000\n#AllinThreshold#100\n"
                          "#FlopConfigIP.RaiseSize#3x,2.5x\n")
    faced = tb.Node(0, (50, 0), tb.IP, True, None)
    check(tb._aggressive_totals(cfg, faced) == [125, 150],
          "2.5x/3x vs a 50 bet raise to 125 and 150")
    cfg = tb.parse_config("#Pot#100\n#EffectiveStacks#10000\n#AllinThreshold#100\n"
                          "#FlopConfig.RaiseSize#3x\n")
    rer = tb.Node(0, (12, 38), tb.OOP, True, None)
    check(tb._aggressive_totals(cfg, rer) == [90],
          "3x vs 12 -> 38 re-raises to 12 + 3 x 26 = 90 (PioViewer, #313; not 3 x 38)")
    tiny = tb.parse_config("#Pot#100\n#EffectiveStacks#200\n#AllinThreshold#100\n"
                           "#FlopConfigIP.RaiseSize#30x\n")
    check(tb._aggressive_totals(tiny, faced) == [200],
          "a multiple past the stack is clamped to the all-in")


def test_raise_cap():
    """RaiseCap limits bets+raises per street; configured all-ins still offered."""
    header = """\
#Pot#100
#EffectiveStacks#10000
#AllinThreshold#100
#FlopConfig.BetSize#50
#FlopConfig.RaiseCap#2
#FlopConfigIP.BetSize#50
#FlopConfigIP.RaiseSize#40
#FlopConfigIP.RaiseCap#2
"""
    cfg = tb.parse_config(header)
    root = tb.Node(0, (0, 0), tb.OOP, False, None)
    check(tb._aggressive_totals(cfg, root) == [50], "cap 2: the opening bet is allowed")
    after_bet = tb.Node(0, (50, 0), tb.IP, True, None, 1)
    check(tb._aggressive_totals(cfg, after_bet) == [130],
          "cap 2: the raise over the bet is allowed (50 + 40% of 200)")
    after_raise = tb.Node(0, (50, 130), tb.OOP, True, None, 2)
    check(tb._aggressive_totals(cfg, after_raise) == [],
          "cap 2: no re-raise once two aggressions have happened")

    lines = {tuple(l) for l in tb.build_lines(cfg)}
    check(lines == {(50, 130), (0, 50)},
          "whole tree under cap 2 is bet-raise and check-bet (no escalation past it)")

    # Cap 1: no raises at all, bets still allowed.
    cap1 = tb.parse_config(header.replace("#FlopConfig.RaiseCap#2", "#FlopConfig.RaiseCap#1")
                           .replace("#FlopConfigIP.RaiseCap#2", "#FlopConfigIP.RaiseCap#1"))
    check(tb._aggressive_totals(cap1, after_bet) == [], "cap 1: a bet cannot be raised")
    check(tb._street_cap(cap1, 1) is None and tb._street_cap(cap1, 2) is None,
          "caps are per street")

    # A configured all-in survives the cap: an `a` entry and AddAllin (gate passed).
    allined = tb.parse_config("#Pot#100\n#EffectiveStacks#10000\n#AllinThreshold#100\n"
                              "#FlopConfig.RaiseCap#1\n#FlopConfigIP.RaiseCap#1\n"
                              "#FlopConfig.BetSize#50\n"
                              "#FlopConfigIP.RaiseSize#40,a\n#FlopConfigIP.AddAllin#True\n")
    check(tb._aggressive_totals(allined, after_bet) == [10000],
          "at the cap the configured all-in is still offered")

    # ...but an all-in produced only by AllinThreshold is a computed size: capped out.
    thr = tb.parse_config("#Pot#100\n#EffectiveStacks#300\n#AllinThreshold#50\n"
                          "#FlopConfig.RaiseCap#1\n#FlopConfigIP.RaiseCap#1\n"
                          "#FlopConfig.BetSize#50\n#FlopConfigIP.RaiseSize#70\n")
    check(tb._aggressive_totals(thr, after_bet) == [],
          "at the cap a threshold-promoted all-in is not offered")
    check(tb._aggressive_totals(tb.parse_config(
        "#Pot#100\n#EffectiveStacks#300\n#AllinThreshold#50\n"
        "#FlopConfig.BetSize#50\n#FlopConfigIP.RaiseSize#70\n"),
        tb.Node(0, (50, 0), tb.IP, True, None)) == [300],
        "the same 70% raise promotes to the all-in when uncapped")


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
    check(got == want, "half-even rounding restored and still matches")


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


def test_donk_leads_need_donk_sizes():
    """#152 (owner's rule): after OOP called the previous street, OOP can lead the next
    street only with sizes from DonkBetSize; an empty list means check only, and AddAllin
    adds no all-in lead either. --pio-compat restores PioViewer's BetSize fallback.
    """
    base = ("#Pot#100\n#EffectiveStacks#400\n#AllinThreshold#100\n"
            "#AddAllinOnlyIfLessThanThisTimesThePot#300\n"
            "#FlopConfig.BetSize#50\n#FlopConfigIP.BetSize#50\n"
            "#TurnConfig.BetSize#50\n#TurnConfigIP.BetSize#50\n"
            "#RiverConfig.BetSize#50\n#RiverConfigIP.BetSize#50\n")
    cfg = tb.parse_config(base)

    # Flop check, IP bets 50, OOP calls: the turn opens with OOP in a donk spot (pot 200).
    turn = tb.Node(1, (50, 50), tb.OOP, False, tb.IP)
    check(tb._aggressive_totals(cfg, turn) == [],
          "empty TurnConfig.DonkBetSize: OOP has no lead into the flop aggressor")
    kids = tb._children(cfg, turn)
    check(len(kids) == 1 and kids[0][0] == 50 and not kids[0][2]
          and kids[0][1].to_act == tb.IP and kids[0][1].street == 1,
          "empty TurnConfig.DonkBetSize: OOP's only action is the check")

    # Donk sizes configured: exactly those, not the BetSize ones.
    with_donk = tb.parse_config(base + "#TurnConfig.DonkBetSize#30\n")
    check(tb._aggressive_totals(with_donk, turn) == [110],
          "TurnConfig.DonkBetSize#30: the 30% donk (50 + 0.3 x 200) is the lead")

    # River behaves the same way after a turn check-down.
    river = tb.Node(2, (50, 50), tb.OOP, False, tb.IP)
    check(tb._aggressive_totals(cfg, river) == [],
          "empty RiverConfig.DonkBetSize: no lead after a checked-down turn either")
    check(tb._aggressive_totals(tb.parse_config(base + "#RiverConfig.DonkBetSize#25\n"),
                                river) == [100],
          "RiverConfig.DonkBetSize#25: the 25% donk (50 + 0.25 x 200) is the lead")

    # AddAllin does not sneak a lead back in: the gate passes here (300% of 200 > 400),
    # but with no donk sizes the all-in must not be offered either.
    allin = tb.parse_config(base + "#TurnConfig.AddAllin#True\n")
    check(tb._aggressive_totals(allin, turn) == [],
          "TurnConfig.AddAllin with an empty DonkBetSize still offers no lead")
    allin_donk = tb.parse_config(base + "#TurnConfig.AddAllin#True\n"
                                 "#TurnConfig.DonkBetSize#30\n")
    check(tb._aggressive_totals(allin_donk, turn) == [110, 400],
          "with donk sizes set, AddAllin adds the all-in on top of the donk")
    aggr = tb.Node(1, (25, 25), tb.OOP, False, tb.OOP)  # OOP was the aggressor: no donk
    check(tb._aggressive_totals(allin, aggr) == [100, 400],
          "AddAllin still works for a normal (non-donk) lead")

    # PioViewer compatibility: with pio_compat an empty donk list falls back to BetSize.
    cfg_pio = tb.parse_config(base)
    cfg_pio.pio_compat = True
    check(tb._aggressive_totals(cfg_pio, turn) == [150],
          "pio_compat: empty DonkBetSize falls back to the 50% BetSize (50 + 0.5 x 200)")
    cfg_pio_donk = tb.parse_config(base + "#TurnConfig.DonkBetSize#30\n")
    cfg_pio_donk.pio_compat = True
    check(tb._aggressive_totals(cfg_pio_donk, turn) == [110],
          "pio_compat: a configured DonkBetSize still wins over BetSize")

    # IP keeps the old behavior: leading into OOP after OOP checked is not a donk bet and
    # still uses BetSize (the PioViewer fixture's check-bet lines depend on this).
    ip_lead = tb.Node(1, (25, 25), tb.IP, True, tb.OOP)
    check(tb._aggressive_totals(cfg, ip_lead) == [100],
          "IP betting after OOP's turn check still uses BetSize (25 + 0.5 x 150)")

    # Line level: with no donk sizes no line leads the turn after check-bet-call;
    # with a donk size, (0, 50, 50, 110) is a line.
    check(not any(len(l) >= 4 and l[:3] == [0, 50, 50] and l[3] > 50
                  for l in tb.build_lines(cfg)),
          "no built line donks the turn when DonkBetSize is empty")
    check(any(l[:4] == [0, 50, 50, 110] for l in tb.build_lines(with_donk)),
          "the check-bet-call-donk line (0 50 50 110) exists when the donk is configured")


def test_donk_pio_compat_cli():
    """The --pio-compat flag toggles the expansion for CLI diffing against PioViewer."""
    import contextlib
    import io
    import tempfile
    base = ("#Pot#100\n#EffectiveStacks#400\n#AllinThreshold#100\n"
            "#FlopConfig.BetSize#50\n#FlopConfigIP.BetSize#50\n"
            "#TurnConfig.BetSize#50\n#TurnConfigIP.BetSize#50\n")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(base)
        path = fh.name
    try:
        def run(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                tb.main(argv)
            return buf.getvalue()
        check("add_line 0 50 50 150" not in run([path]).splitlines(),
              "CLI default: no turn donk line with an empty DonkBetSize")
        check("add_line 0 50 50 150" in run([path, "--pio-compat"]).splitlines(),
              "CLI --pio-compat: the turn donk line falls back to the 50% BetSize")
    finally:
        os.unlink(path)


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


def opts(header, node):
    return tb._aggressive_totals(tb.parse_config(header), node)


def test_pioviewer_corpus_rules():
    """Rules inferred from 145 real PioViewer configs (#313), each on a small synthetic config."""
    base = "#Pot#1750\n#EffectiveStacks#9200\n#AllinThreshold#67\n"
    root = tb.Node(0, (0, 0), tb.OOP, False, None)
    check(opts(base + "#FlopConfig.BetSize#75\n", root) == [1312], "75% of 1750 = 1312.5 rounds half to even: 1312")
    raise_node = tb.Node(0, (0, 925), tb.OOP, True, None)
    check(opts("#Pot#3700\n#EffectiveStacks#8900\n#AllinThreshold#67\n#FlopConfig.RaiseSize#33\n", raise_node)
          == [2756], "the total is rounded: 925 + 33% x 5550 = 2756.5 -> 2756 (not 925 + 1832)")
    mr = tb.Node(1, (0, 2500), tb.OOP, True, None)
    check(opts("#Pot#5000\n#EffectiveStacks#7500\n#AllinThreshold#67\n#TurnConfig.RaiseSize#2e\n", mr) == [5000],
          "min-raise: facing 0 -> 2500, a 2e raise of 4571 becomes 5000")
    check(opts(base + "#FlopConfig.BetSize#2e75\n", root) == [1312], "2e75: 2e is 120% here, so 75% (1312)")
    check(opts(base + "#FlopConfig.BetSize#e200 2e\n", root) == sorted(set(opts(base + "#FlopConfig.BetSize#e 2e\n", root))),
          "eP with P above the geometric size is just the geometric size")
    strict = "#Pot#12060\n#EffectiveStacks#9000\n#AllinThreshold#67\n"
    check(opts(strict + "#FlopConfig.BetSize#50\n", root) == [6030], "exactly 67% of the stack (6030) is not an all-in")
    check(opts(strict + "#FlopConfig.BetSize#51\n", root) == [9000], "just above 67% is the all-in")
    gate = tb.Node(2, (1155, 0), tb.IP, True, None)
    check(opts(base + "#AddAllinOnlyIfLessThanThisTimesThePot#200\n#RiverConfigIP.RaiseSize#50\n"
                      "#RiverConfigIP.AddAllin#True\n", gate) == [3185, 9200],
          "AddAllin gate on the stack behind: 9200 - 1155 = 8045 < 200% x 4060")
    check(opts(base + "#AddAllinOnlyIfLessThanThisTimesThePot#500\n#FlopConfig.AddAllin#True\n", root) == [],
          "AddAllin adds nothing to an empty size list")
    merge = tb.Node(1, (1468, 1468), tb.IP, True, None)
    hdr = "#Pot#4450\n#EffectiveStacks#8000\n#AllinThreshold#67\n#TurnConfigIP.BetSize#4e, 3e, 2e\n"
    check(opts(hdr, merge) == [2539, 2961, 3920], "4e/3e/2e unmerged: 2539, 2961, 3920")
    check(opts(hdr + "#MergeSimilarBets#True\n#MergeSimilarBetsThreshold#12\n", merge) == [2539, 3920],
          "MergeSimilarBets: 2961 merges into 3920 (10.8%), then 2539 stays (16.3%)")
    cap = base + "#CapEnabled#True\n#CapPerStreet#4\\n4\\n3\n#FlopConfigIP.RaiseSize#50\n"
    three = tb.Node(0, (2099, 1262), tb.IP, True, None, aggr=3)
    check(opts(cap, three) == [9200], "CapPerStreet 4: after three bets the fourth is the all-in only, even without `a`")
    check(opts(cap, tb.Node(0, (2099, 1262), tb.IP, True, None, aggr=2)) == [5073],
          "below the cap, sizes as usual (2099 + 50% x 5948)")
    check(opts(base + "#CapEnabled#True\n#CapPerStreet#0\\n4\\n3\n#FlopConfigIP.RaiseSize#50\n", three) == [5073],
          "CapPerStreet 0 leaves the street uncapped")
    check(opts(base + "#FlopConfig.RaiseSize#2.5x\n", tb.Node(0, (96, 240), tb.OOP, True, None)) == [456],
          "2.5x facing 96 -> 240 is 96 + 2.5 x 144 = 456")
    check(opts(base + "#FlopConfig.BetSize#15,2e.a\n", root) == [262, 9200], "a malformed `2e.a` is the all-in")
    forced = tb.parse_config(base + "#ForceOOPBet#True\n#FlopConfig.BetSize#33\n")
    check(all(line[0] != 0 for line in tb.build_lines(forced)), "ForceOOPBet: no line starts with an OOP check")
    uni = ("#Pot#500\n#EffectiveStacks#9750\n#AllinThreshold#67\n#UseUnifiedRaiseAfterRaise#True\n"
           "#UnifiedRaiseAfterRaise#75\n#FlopConfigIP.RaiseSize#50\n#RiverConfigIP.RaiseSize#150\n")
    check(opts(uni, tb.Node(0, (1000, 250), tb.IP, True, None, aggr=2)) == [2875],
          "unified raise after a raise: 1000 + 75% x 2500 = 2875, not RaiseSize's 50%")
    check(opts(uni, tb.Node(2, (875, 500), tb.IP, True, None, aggr=1, raised=True)) == [2562],
          "a raise earlier in the hand makes the next street's raise unified too (75%, not 150%)")
    edits = tb.parse_config(base + "#FlopConfig.BetSize#25,75\n#FlopConfigIP.RaiseSize#50\n"
                            "#RemovedLines_2#Bet 75, Raise 50\\\\0\n#ExtraLines_2#\\\\0Check, Bet 10\\\\0\n")
    lines = {tuple(l) for l in tb.build_lines(edits)}
    check((1312,) in lines and not any(l[:2] == (1312, 3593) for l in lines),
          "RemovedLines: the raise after the 75% bet is gone, the bet stays")
    check(any(l[:2] == (438, 1751) for l in lines), "the raise after the 25% bet is untouched (438 + 50% x 2626)")
    check((0, 175) in lines or any(l[:2] == (0, 175) for l in lines), "ExtraLines: IP's 10% bet after a check is added")


def main():
    for fn in (test_parse, test_matches_pioviewer, test_no_redundant_lines, test_pioviewer_corpus_rules,
               test_sizing_arithmetic, test_add_allin_gate, test_parse_size_forms,
               test_geometric_bets_spr3, test_geometric_raises, test_raise_multiples,
               test_raise_cap, test_rounding_is_nearest,
               test_branch_ending_on_a_plain_bet,
               test_donk_leads_need_donk_sizes, test_donk_pio_compat_cli,
               test_full_script):
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
