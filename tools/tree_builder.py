#!/usr/bin/env python3
"""Turn PioViewer-style bet-size settings into the explicit add_line list the solver needs.

The solver only accepts explicit lines. PioViewer lets you describe a tree with a handful of
percentages instead (bet sizes per street, raise size, all-in rules) and expands them itself.
This module reimplements that expansion, inferred from a recorded PioViewer save file that
contains both the config header and the 266 lines it generated
(`tests/data/pio_25bbHU_2sizes.txt`).

Usage:
    python3 tools/tree_builder.py CONFIG            # print add_line commands
    python3 tools/tree_builder.py CONFIG --full     # print a complete UPI script

CONFIG is any file containing `#Key#Value` header lines; extra content (ranges, add_line
commands already present, build_tree) is ignored, so a PioViewer save file can be fed in
directly and the generated lines diffed against the ones it already carries.

The inferred rules are documented in `README.md` (section "Tree builder") and in
`SIZING_RULES` below.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

OOP, IP = 0, 1
STREETS = ("Flop", "Turn", "River")

SIZING_RULES = """\
Amounts in a line are absolute: the total chips a player has put in since the start of the
hand. A check or a call therefore repeats the amount the player is already matched to.

pot          = dead pot + invested[OOP] + invested[IP]
pot_if_called = dead pot + 2 * max(invested)          (the pot once the pending bet is called)

bet   to  invested + round(BetSize%   * pot)           (DonkBetSize% when OOP leads into
                                                        the previous street's aggressor)
raise to  facing   + round(RaiseSize% * pot_if_called)

Rounding is half-up to whole chips.

All-in rules:
  * AllinThreshold%: a bet or raise whose total reaches AllinThreshold% of the effective
    stack is replaced by the all-in. (At 67% with a 230 stack, anything >= 154 becomes 230.)
  * <Street>Config[IP].AddAllin: adds the all-in as an extra size alongside the configured
    ones, but only if AddAllinOnlyIfLessThanThisTimesThePot% of pot_if_called still exceeds
    the all-in - i.e. only once the pot is big enough relative to the remaining stack.
"""


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _pct(pct: Decimal, pot: int) -> int:
    return _round(pct * Decimal(pot) / Decimal(100))


@dataclass
class StreetConfig:
    bet: list[Decimal] = field(default_factory=list)
    donk: list[Decimal] = field(default_factory=list)
    raise_: list[Decimal] = field(default_factory=list)
    add_allin: bool = False


@dataclass
class Config:
    pot: int = 0
    stack: int = 0
    allin_threshold: Decimal = Decimal(100)
    add_allin_max_pot_mult: Decimal = Decimal("inf")
    # streets[street][player]
    streets: list[list[StreetConfig]] = field(default_factory=list)
    board: str = ""
    ranges: dict[str, str] = field(default_factory=dict)
    raw_set_range: dict[str, str] = field(default_factory=dict)
    isomorphism: str = "1 0"

    def street(self, street: int, player: int) -> StreetConfig:
        return self.streets[street][player]


def _sizes(value: str) -> list[Decimal]:
    return [Decimal(part) for part in value.replace(" ", "").split(",") if part]


def parse_config(text: str) -> Config:
    """Read the `#Key#Value` header of a PioViewer save file."""
    cfg = Config(streets=[[StreetConfig(), StreetConfig()] for _ in STREETS])
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("set_range OOP "):
            cfg.raw_set_range["OOP"] = line[len("set_range OOP "):]
            continue
        if line.startswith("set_range IP "):
            cfg.raw_set_range["IP"] = line[len("set_range IP "):]
            continue
        if line.startswith("set_isomorphism "):
            cfg.isomorphism = line[len("set_isomorphism "):].strip()
            continue
        if not line.startswith("#"):
            continue
        parts = line.split("#")
        if len(parts) < 3:
            continue
        key, value = parts[1], parts[2]

        if key == "Pot":
            cfg.pot = int(value)
        elif key == "EffectiveStacks":
            cfg.stack = int(value)
        elif key == "AllinThreshold":
            cfg.allin_threshold = Decimal(value)
        elif key == "AddAllinOnlyIfLessThanThisTimesThePot":
            cfg.add_allin_max_pot_mult = Decimal(value)
        elif key == "Board":
            cfg.board = value.replace(" ", "")
        elif key.startswith("Range"):
            cfg.ranges[key] = value
        elif "." in key:
            head, field_name = key.split(".", 1)
            player = IP if head.endswith("IP") else OOP
            name = head[:-2] if head.endswith("IP") else head
            if not name.endswith("Config"):
                continue
            street_name = name[: -len("Config")]
            if street_name not in STREETS:
                continue
            sc = cfg.streets[STREETS.index(street_name)][player]
            if field_name == "BetSize":
                sc.bet = _sizes(value)
            elif field_name == "DonkBetSize":
                sc.donk = _sizes(value)
            elif field_name == "RaiseSize":
                sc.raise_ = _sizes(value)
            elif field_name == "AddAllin":
                sc.add_allin = value.strip().lower() == "true"
    return cfg


@dataclass(frozen=True)
class Node:
    street: int
    inv: tuple[int, int]
    to_act: int
    acted: bool          # has the player to act already acted on this street?
    # the player who made the last aggressive action on the previous street, or None
    prev_aggressor: int | None


def _aggressive_totals(cfg: Config, node: Node) -> list[int]:
    """Every bet/raise total available at `node`, smallest first, de-duplicated."""
    me, opp = node.to_act, 1 - node.to_act
    mine, facing = node.inv[me], node.inv[opp]
    dead = cfg.pot
    pot = dead + mine + facing
    pot_if_called = dead + 2 * max(mine, facing)
    sc = cfg.street(node.street, me)
    allin = cfg.stack

    totals: list[int] = []
    if facing > mine:
        for pct in sc.raise_:
            totals.append(facing + _pct(pct, pot_if_called))
    else:
        donk = node.prev_aggressor is not None and node.prev_aggressor != me
        for pct in (sc.donk if donk and sc.donk else sc.bet):
            totals.append(mine + _pct(pct, pot))

    # A bet/raise that gets close enough to the stack is replaced by the all-in.
    threshold = cfg.allin_threshold * Decimal(allin) / Decimal(100)
    totals = [allin if Decimal(t) >= threshold else t for t in totals]
    totals = [min(t, allin) for t in totals]

    if sc.add_allin:
        limit = cfg.add_allin_max_pot_mult * Decimal(pot_if_called) / Decimal(100)
        if Decimal(allin) < limit:
            totals.append(allin)

    # Must actually be a raise over what we are facing, and we must have chips behind.
    totals = [t for t in totals if t > facing and t > mine]
    return sorted(set(totals))


def _children(cfg: Config, node: Node) -> list[tuple[int, Node | None, bool]]:
    """(amount emitted, next node, is this a bet/raise) for check/call and every bet/raise.

    `None` as the next node means the hand is over (showdown or an all-in that was called),
    so the line stops there.
    """
    me, opp = node.to_act, 1 - node.to_act
    mine, facing = node.inv[me], node.inv[opp]
    out: list[tuple[int, Node | None, bool]] = []

    # Check or call. Folding needs no line: the tree builder adds it implicitly.
    matched = max(mine, facing)
    inv = [node.inv[0], node.inv[1]]
    inv[me] = matched
    if facing > mine or node.acted:
        # bet called, or check behind: the street is complete
        if matched >= cfg.stack or node.street == len(STREETS) - 1:
            out.append((matched, None, False))
        else:
            out.append((matched, Node(node.street + 1, (inv[0], inv[1]), OOP, False,
                                      opp if facing > mine else None), False))
    else:
        out.append((matched, Node(node.street, (inv[0], inv[1]), opp, True,
                                  node.prev_aggressor), False))

    for total in _aggressive_totals(cfg, node):
        inv = [node.inv[0], node.inv[1]]
        inv[me] = total
        out.append((total, Node(node.street, (inv[0], inv[1]), opp, True,
                                node.prev_aggressor), True))
    return out


def build_lines(cfg: Config) -> list[list[int]]:
    """Every maximal line of the tree described by `cfg`.

    A line only has to reach the deepest bet of its branch: the solver fills in check, call
    and fold at every node it creates. So every path that ends in a bet or raise is a
    candidate, and the ones that are prefixes of a longer candidate get dropped. (With an
    all-in threshold in play the deepest action of a branch is always the all-in, but a
    config without one can bottom out on an ordinary raise, and that line is still needed.)
    """
    candidates: list[tuple[int, ...]] = []

    def walk(node: Node, history: list[int]) -> None:
        for amount, nxt, aggressive in _children(cfg, node):
            history.append(amount)
            if aggressive:
                candidates.append(tuple(history))
            if nxt is not None:
                walk(nxt, history)
            history.pop()

    walk(Node(0, (0, 0), OOP, False, None), [])

    # Drop any candidate that is a prefix of another: the longer line implies it.
    seen = set(candidates)
    prefixes = {line[:n] for line in seen for n in range(1, len(line))}
    keep = [list(line) for line in seen if line not in prefixes]
    return sorted(keep, key=lambda l: " ".join(map(str, l)))


def format_lines(lines: list[list[int]]) -> list[str]:
    return ["add_line " + " ".join(str(a) for a in line) for line in lines]


def format_script(cfg: Config, lines: list[list[int]]) -> list[str]:
    out = []
    for who in ("OOP", "IP"):
        if who in cfg.raw_set_range:
            out.append(f"set_range {who} {cfg.raw_set_range[who]}")
    if cfg.board:
        out.append(f"set_board {cfg.board}")
    out.append(f"set_eff_stack {cfg.stack}")
    out.append(f"set_isomorphism {cfg.isomorphism}")
    out.append(f"set_pot 0 0 {cfg.pot}")
    out.append("clear_lines ")
    out.extend(format_lines(lines))
    out.append("build_tree ")
    return out


def extract_lines(text: str) -> list[list[int]]:
    """The add_line commands already present in a save file, for comparison."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("add_line "):
            out.append([int(tok) for tok in line.split()[1:]])
    return sorted(out, key=lambda l: " ".join(map(str, l)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", help="file with a PioViewer #Key#Value header")
    ap.add_argument("--full", action="store_true",
                    help="emit a complete UPI script, not just the add_line commands")
    ap.add_argument("--rules", action="store_true", help="print the inferred sizing rules")
    ap.add_argument("--compare", action="store_true",
                    help="diff against the add_line commands already in the file")
    args = ap.parse_args(argv)

    if args.rules:
        sys.stdout.write(SIZING_RULES)
        return 0
    if not args.config:
        ap.error("a config file is required")

    with open(args.config) as fh:
        text = fh.read()
    cfg = parse_config(text)
    lines = build_lines(cfg)

    if args.compare:
        want = extract_lines(text)
        got = {tuple(l) for l in lines}
        expected = {tuple(l) for l in want}
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        print(f"expected {len(expected)} lines, generated {len(got)}")
        for line in missing:
            print("missing:", " ".join(map(str, line)))
        for line in extra:
            print("extra:  ", " ".join(map(str, line)))
        return 0 if not missing and not extra else 1

    out = format_script(cfg, lines) if args.full else format_lines(lines)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
