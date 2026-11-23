#!/usr/bin/env python3
"""Heads-up preflop solves: settings in big blinds -> the script the solver needs, a memory table,
13x13 charts of the result.

    python3 tools/preflop.py script   [settings]            print the UPI script
    python3 tools/preflop.py tree     [settings]            print the preflop tree and its flop exits
    python3 tools/preflop.py estimate [settings]            memory per number of flops and postflop depth
    python3 tools/preflop.py solve OUT.json [settings]      solve, save the preflop strategies, print charts
    python3 tools/preflop.py chart OUT.json [NODE ...]      print the charts of a saved result again
    python3 tools/preflop.py check OUT.json ... [settings]  what a result's strategy loses in a game with
                                                            more flops (--flops) or streets (--depth), bb/100

The small blind is the button: it acts first before the flop (IP in the solver's terms) and last
after it. Sizes are in big blinds: `2.5` is a raise to 2.5 big blinds, `4x` four times the bet faced,
`allin` the stack. A preflop solve deals a subset of flops (`flops/`, our own files) and gives every
flop exit a postflop tree; how deep that tree bets (`--depth`) and how many flops there are decide
the memory, which `estimate` prints before anything is built. `check` takes the settings of the
solve it checks, the flops to check it on (`--flops all` where they fit) and, to check it in a game
with more betting after the flop, the `--depth`. README, "Preflop solves".
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import textwrap
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_builder as tb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RANKS = "AKQJT98765432"
SB, BB = "SB", "BB"
DEPTHS = ("none", "flop", "turn", "river")
SHIPPED = (10, 25, 50, 100, 200)
ALL_HANDS = ("AA-22,AKs-A2s,KQs-K2s,QJs-Q2s,JTs-J2s,T9s-T2s,98s-92s,87s-82s,76s-72s,65s-62s,54s-52s,43s-42s,32s,"
             "AKo-A2o,KQo-K2o,QJo-Q2o,JTo-J2o,T9o-T2o,98o-92o,87o-82o,76o-72o,65o-62o,54o-52o,43o-42o,32o")
MAX_CHIPS = 65535  # the solver's limit on a stack or an investment


class PreflopError(Exception):
    """Settings that do not describe a legal tree, or a solver that did not answer as expected."""


# ---- settings -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Size:
    kind: str  # "bb" (raise to that many big blinds), "x" (times the bet faced), "allin"
    value: Decimal = Decimal(0)
    text: str = ""


def parse_sizes(text: str, name: str) -> list[Size]:
    out = []
    for part in text.replace(",", " ").split():
        low = part.lower()
        try:
            if low in ("allin", "all-in", "a"):
                size = Size("allin", text="allin")
            elif low.endswith("x"):
                size = Size("x", Decimal(low[:-1]), low)
            else:
                size = Size("bb", Decimal(low[:-2] if low.endswith("bb") else low), low)
            if not size.value.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise PreflopError(f"--{name}: {part!r} is not a size (2.5 = to 2.5 big blinds, 4x = four times "
                               "the bet faced, allin)")
        if size.kind != "allin" and size.value <= 0:
            raise PreflopError(f"--{name}: {part!r} is not a positive size")
        out.append(size)
    return out


@dataclass
class Settings:
    stack: Decimal = Decimal(100)        # big blinds
    chips: int = 100                     # chips per big blind
    ante: Decimal = Decimal(0)           # dead money in the pot before the blinds, big blinds
    limp: bool = True
    open: list[Size] = field(default_factory=lambda: parse_sizes("2.5", "open"))
    iso: list[Size] = field(default_factory=lambda: parse_sizes("3.5", "iso"))
    threebet: list[Size] = field(default_factory=lambda: parse_sizes("4x", "3bet"))
    limp_raise: list[Size] | None = None  # the small blind's raise after limping; None: the 3-bet sizes
    fourbet: list[Size] = field(default_factory=lambda: parse_sizes("2.3x", "4bet"))
    fivebet: list[Size] = field(default_factory=lambda: parse_sizes("allin", "5bet"))
    allin_threshold: Decimal = Decimal(50)  # a raise to more than this % of the stack is all-in
    depth: str = "flop"
    bet: dict[str, str] = field(default_factory=lambda: {"Flop": "50", "Turn": "50", "River": "50"})
    raise_: str = "60"
    donk: str = ""
    raise_cap: int = 2
    postflop_allin_threshold: Decimal = Decimal(67)
    postflop_allin_pot: Decimal = Decimal(150)  # an all-in is offered when the stack left is under this % of the pot
    postflop_config: str | None = None   # text of a `#Key#Value` file that replaces the options above
    range_sb: str = ALL_HANDS
    range_bb: str = ALL_HANDS

    @property
    def stack_chips(self) -> int:
        return _chips(self.stack, self.chips)


def _chips(bb: Decimal, per_bb: int) -> int:
    return int((bb * per_bb).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _bb(chips: int | float, per_bb: int) -> str:
    """Chips as big blinds, without trailing zeros: 250 at 100 a blind is `2.5`."""
    text = f"{chips / per_bb:.2f}".rstrip("0").rstrip(".")
    return text or "0"


# ---- the preflop tree -----------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    kind: str                 # "decision", "fold", "flop" (a flop exit), "allin" (called all-in)
    inv: tuple[int, int]      # chips in: (small blind, big blind)
    actor: str | None = None  # who acts, for a decision
    line: tuple[int, ...] = ()            # the amounts of add_preflop_line that lead here
    said: tuple[str, ...] = ()            # the actions in words
    aggressor: str | None = None          # who raised last
    children: list[tuple[str, "Node"]] = field(default_factory=list)   # (label, child), solver's order


def _raise_sizes(s: Settings, raises: int, limped: bool) -> tuple[str, list[Size]]:
    """The option that sizes raise number `raises + 1` of the hand, and its sizes."""
    if raises == 0:
        return ("iso", s.iso) if limped else ("open", s.open)
    if raises == 1:
        if limped and s.limp_raise is not None:
            return "limp-raise", s.limp_raise
        return "3bet", s.threebet
    if raises == 2:
        return "4bet", s.fourbet
    if raises == 3:
        return "5bet", s.fivebet
    return "5bet", [Size("allin", text="allin")]  # past the 5-bet the only raise is all-in


def build_tree(s: Settings) -> Node:
    """The preflop tree the settings describe. Children are in the solver's order: raises from the
    largest down, then call or check, then fold."""
    stack, per_bb = s.stack_chips, s.chips
    if per_bb < 2 or per_bb % 2:
        raise PreflopError("--chips-per-bb must be an even number (the small blind is half of it)")
    if stack > MAX_CHIPS:
        raise PreflopError(f"a stack of {s.stack} big blinds is {stack} chips, over the solver's {MAX_CHIPS}: "
                           f"lower --chips-per-bb (now {per_bb})")
    if stack <= per_bb:
        raise PreflopError("the stack must be more than one big blind")
    if not s.limp and not s.open:
        raise PreflopError("the small blind can neither limp nor raise: give --open sizes or allow the limp")

    def amounts(node_inv, me, raises, limped, last_raise) -> list[tuple[int, str]]:
        facing = max(node_inv)
        if facing >= stack:
            return []
        name, sizes = _raise_sizes(s, raises, limped)
        out: dict[int, str] = {}
        for size in sizes:
            if size.kind == "allin":
                to = stack
            elif size.kind == "x":
                to = int((size.value * facing).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            else:
                to = _chips(size.value, per_bb)
            if to * 100 > s.allin_threshold * stack:
                to = stack
            if to < stack and to < facing + last_raise:
                raise PreflopError(
                    f"--{name} {size.text}: a raise to {_bb(to, per_bb)} big blinds over {_bb(facing, per_bb)} is "
                    f"below the minimum raise ({_bb(facing + last_raise, per_bb)})")
            out.setdefault(to, size.text)
        return sorted(out.items(), reverse=True)

    def grow(node: Node, raises: int, limped: bool, last_raise: int, first: bool) -> None:
        me = 0 if node.actor == SB else 1
        other = BB if node.actor == SB else SB
        mine, facing = node.inv[me], max(node.inv)
        for to, _ in amounts(node.inv, me, raises, limped, last_raise):
            inv = list(node.inv)
            inv[me] = to
            word = ("all-in" if to >= stack else f"raise {_bb(to, per_bb)}")
            child = Node(f"{node.id}:b{to}", "decision", (inv[0], inv[1]), other, node.line + (to,),
                         node.said + (f"{node.actor} {word}",), node.actor)
            node.children.append((word, child))
            grow(child, raises + 1, limped, to - facing, False)
        if facing > mine or not first:
            inv = list(node.inv)
            inv[me] = facing
            word = "call" if facing > mine else "check"
            if first and node.actor == SB:
                # the limp: the big blind still has the option
                if s.limp:
                    child = Node(f"{node.id}:c", "decision", (inv[0], inv[1]), BB, node.line + (facing,),
                                 node.said + ("SB limp",), None)
                    node.children.append(("limp", child))
                    grow(child, 0, True, per_bb, False)
            else:
                kind = "allin" if facing >= stack else "flop"
                child = Node(f"{node.id}:c", kind, (inv[0], inv[1]), None, node.line + (facing,),
                             node.said + (f"{node.actor} {word}",), node.aggressor)
                node.children.append((word, child))
        if facing > mine:
            child = Node(f"{node.id}:f", "fold", node.inv, None, node.line, node.said + (f"{node.actor} fold",),
                         node.aggressor)
            node.children.append(("fold", child))

    root = Node("r:0", "decision", (per_bb // 2, per_bb), SB)
    grow(root, 0, False, per_bb, True)
    return root


def walk(node: Node):
    yield node
    for _, child in node.children:
        yield from walk(child)


# ---- the postflop tree of a flop exit --------------------------------------------------------------

def postflop_config(s: Settings) -> tb.Config:
    """The tree builder's config for the postflop betting: the options, or the `#Key#Value` file."""
    if s.postflop_config is not None:
        return tb.parse_config(s.postflop_config)
    streets_on = DEPTHS.index(s.depth)
    head = [f"#AllinThreshold#{s.postflop_allin_threshold}",
            f"#AddAllinOnlyIfLessThanThisTimesThePot#{s.postflop_allin_pot}"]
    for i, street in enumerate(tb.STREETS):
        on = i < streets_on
        for who in ("", "IP"):
            head += [f"#{street}Config{who}.BetSize#{s.bet[street] if on else ''}",
                     f"#{street}Config{who}.RaiseSize#{s.raise_ if on and s.raise_cap > 1 else ''}",
                     f"#{street}Config{who}.AddAllin#{on}",
                     f"#{street}Config{who}.RaiseCap#{s.raise_cap}"]
        head.append(f"#{street}Config.DonkBetSize#{s.donk if on else ''}")
    try:
        return tb.parse_config("\n".join(head))
    except (ValueError, InvalidOperation) as e:
        raise PreflopError(f"postflop sizes: {e}")


def exit_lines(s: Settings, cfg: tb.Config, node: Node) -> list[list[int]]:
    """The `add_line`s of a flop exit, in whole-hand amounts: what each player has put in since the
    blinds, so a check on the flop repeats the amount the preflop action ended on."""
    put_in = node.inv[0]
    dead = _chips(s.ante, s.chips)
    cfg = tb.dataclasses.replace(cfg, pot=2 * put_in + dead, stack=s.stack_chips - put_in)
    aggressor = {SB: tb.IP, BB: tb.OOP, None: None}[node.aggressor]
    start = tb.Node(0, (0, 0), tb.OOP, False, aggressor,
                    removed=tuple(cfg.removed_lines), extra=tuple(cfg.extra_lines))
    try:
        lines = tb.build_lines(cfg, start)
    except ValueError as e:
        raise PreflopError(f"the postflop tree after {', '.join(node.said)}: {e}")
    return [[a + put_in for a in line] for line in lines]


# ---- flops ----------------------------------------------------------------------------------------

def read_flop_file(path: str) -> list[tuple[str, float]]:
    """`flop:weight` lines (the format of flops/); `#` starts a comment."""
    out = []
    try:
        with open(path) as f:
            for n, raw in enumerate(f, 1):
                line = raw.split("#")[0].strip()
                if not line:
                    continue
                cards, sep, weight = line.partition(":")
                cards = cards.replace(" ", "")
                try:
                    w = float(weight) if sep else 1.0
                except ValueError:
                    w = -1.0
                if not 0 <= w < float("inf"):
                    raise PreflopError(f"{path}:{n}: {weight.strip()!r} is not a weight")
                if (len(cards) != 6 or any(c not in "AKQJT98765432" for c in cards[0::2])
                        or any(c not in "cdhs" for c in cards[1::2]) or len({cards[0:2], cards[2:4], cards[4:6]}) != 3):
                    raise PreflopError(f"{path}:{n}: {cards!r} is not a flop")
                if w > 0:
                    out.append((cards, w))
    except OSError as e:
        raise PreflopError(f"cannot read the flop file: {e}")
    if not out:
        raise PreflopError(f"{path}: no flops")
    return out


def all_flops() -> list[tuple[str, float]]:
    """Every flop up to a renaming of the suits (1,755), each with the number of flops it stands for."""
    count: dict[str, int] = {}
    suits = "shdc"
    cards = [(r, x) for r in range(13) for x in range(4)]
    for i in range(52):
        for j in range(i + 1, 52):
            for k in range(j + 1, 52):
                flop = sorted((cards[i], cards[j], cards[k]))
                # name the suits in order of first appearance (s, h, d); cards of one rank can go in
                # either order, and the one that reuses the earliest suit is kept (AsAhTs, not AsAhTh),
                # which is how the files in flops/ name a class
                best = None
                for order in _orders(flop):
                    seen: dict[int, int] = {}
                    key = tuple(seen.setdefault(x, len(seen)) for _, x in order)
                    if best is None or key < best:
                        best = key
                name = "".join(RANKS[r] + suits[x] for (r, _), x in zip(flop, best))
                count[name] = count.get(name, 0) + 1
    return [(name, float(n)) for name, n in sorted(count.items())]


def _orders(flop):
    """The orderings of three cards that keep the ranks sorted (cards of one rank in any order)."""
    a, b, c = flop
    out = [(a, b, c)]
    if a[0] == b[0]:
        out.append((b, a, c))
    if b[0] == c[0]:
        out += [(x, z, y) for x, y, z in list(out)]
    if a[0] == b[0] == c[0]:
        out += [(c, a, b), (c, b, a)]
    return out


def flop_path(count: int) -> str:
    return os.path.join(ROOT, "flops", f"wsolver_{count}.txt")


ALL_FLOPS = "all 1,755 flops"


def choose_flops(spec: str) -> tuple[str, list[tuple[str, float]]]:
    """`--flops`: a number of flops we ship a file for, `all`, or a flop file. (name, flops)."""
    if spec == "all":
        return ALL_FLOPS, all_flops()
    if spec.isdigit():
        path = flop_path(int(spec))
        if not os.path.exists(path):
            raise PreflopError(f"no shipped subset of {spec} flops (there are {', '.join(map(str, SHIPPED))}; "
                               "`make flop-subsets FLOP_SIZES=N` makes another, or give a flop file)")
        return f"flops/wsolver_{spec}.txt", read_flop_file(path)
    return spec, read_flop_file(spec)


# ---- the script -----------------------------------------------------------------------------------

def script(s: Settings, flops: list[tuple[str, float]], root: Node | None = None) -> list[str]:
    """The UPI commands that set the tree up, to `add_all_flops` (which builds it) but not including it."""
    root = root or build_tree(s)
    per_bb, stack, dead = s.chips, s.stack_chips, _chips(s.ante, s.chips)
    out = [f"set_range OOP {s.range_bb}", f"set_range IP {s.range_sb}",
           f"set_pot {per_bb} {per_bb // 2} {dead}", f"set_eff_stack {stack}",
           "small_strats on", "set_collapse_checkdowns on", "clear_preflop_lines"]
    closing = [n for n in walk(root) if n.kind in ("flop", "allin")]
    out += ["add_preflop_line " + " ".join(map(str, n.line)) for n in closing]
    if not s.limp:
        out.append(f"remove_preflop_line {per_bb}")
    out += ["build_preflop_tree", "reset_subset", "set_board " + flops[0][0]]
    out += [f"add_to_subset {w:g} {f[0:2]} {f[2:4]} {f[4:6]}" for f, w in flops]
    cfg = postflop_config(s)
    for node in closing:
        if node.kind != "flop":
            continue
        out += ["clear_lines", f"set_pot {node.inv[1]} {node.inv[0]} {dead}", f"set_eff_stack {stack}"]
        out += tb.format_lines(exit_lines(s, cfg, node))
        out.append(f"add_schematic_tree {node.id}")
    return out


# ---- talking to the solver -------------------------------------------------------------------------

class Solver:
    END = "PREFLOP_PY_END"
    HELLO_SECONDS = 10  # for the first exchange: a program that is not the solver may never answer

    def __init__(self, exe: str):
        if not os.path.exists(exe):
            raise PreflopError(f"no solver at {exe} (run `make`, or give --solver)")
        try:
            self.p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            raise PreflopError(f"cannot run {exe}: {e}")
        watchdog = threading.Timer(self.HELLO_SECONDS, self.p.kill)
        watchdog.start()
        try:
            self.cmd("set_end_string " + self.END)
        except PreflopError:
            self.kill()
            raise PreflopError(f"{exe} does not answer like the solver")
        finally:
            watchdog.cancel()

    def __enter__(self) -> "Solver":
        return self

    def __exit__(self, kind, value, trace) -> None:
        # after an error or an interrupt nothing is worth waiting for, and a solve may run for hours
        if kind is None:
            self.close()
        else:
            self.kill()

    def kill(self) -> None:
        self.p.kill()
        self.p.wait()

    def _line(self, during: str) -> str:
        got = self.p.stdout.readline()
        if not got:
            raise PreflopError(f"the solver went away during `{during}` (out of memory?)")
        return got.rstrip("\n")

    def cmd(self, line: str, on_report=None) -> list[str]:
        """Send a command, return the lines of its answer. A solve's progress reports arrive in between
        (`SOLVER:` and a block of lines closed by the end string, or one `SOLVER: ...` line); they go to
        `on_report`, line by line, and are not part of the answer."""
        name = line.split()[0]
        try:
            self.p.stdin.write(line + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise PreflopError(f"the solver went away before `{name}`")
        out: list[str] = []
        while True:
            got = self._line(name)
            if got.startswith("SOLVER:") and not out:
                if got == "SOLVER:":
                    while True:
                        report = self._line(name)
                        if report == self.END:
                            break
                        if on_report:
                            on_report(report)
                continue
            if got == self.END:
                return out
            out.append(got)

    def need(self, line: str) -> list[str]:
        """A set-up command: anything but its `ok!` is an error."""
        out = self.cmd(line)
        if not any(l.rstrip().endswith("ok!") for l in out):
            raise PreflopError(f"the solver refused `{line[:70]}`: {' '.join(out)[:200]}")
        return out

    def close(self) -> None:
        try:
            self.p.stdin.write("exit\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.p.kill()


def default_solver() -> str:
    return os.path.join(ROOT, "solver.exe" if os.name == "nt" else "solver")


def _estimate(u: Solver) -> int:
    out = u.cmd("estimate_tree")
    for l in out:
        if l.startswith("estimated tree size:"):
            return int(l.split()[3])
    raise PreflopError(f"estimate_tree answered {' '.join(out)[:200]}")


def estimates_mb(exe: str, s: Settings, flops: list[tuple[str, float]]) -> tuple[int, int]:
    """What the solver says the tree takes (16-bit storage), without building it: the smallest tree, and
    the tree with outcome tables at every level (`set_outcome_tables fast`), which is the one the solver
    builds on its own when that fits in the memory that is free (`auto`, #496). One figure twice where
    the smallest tree has the tables already (no bets on the turn). The second costs no time to speak
    of: what the first one built to count is still there."""
    with Solver(exe) as u:
        for line in script(s, flops):
            u.need(line)
        small = _estimate(u)
        u.need("set_outcome_tables fast")
        return small, _estimate(u)


def estimate_mb(exe: str, s: Settings, flops: list[tuple[str, float]]) -> int:
    """The smallest tree's figure of `estimates_mb`."""
    return estimates_mb(exe, s, flops)[0]


def tables_within(small: int, with_tables: int, budget: int) -> str | None:
    """The `set_outcome_tables` under which the tree that is built is within `budget` MB whatever memory
    is free: `auto` when the tree with the tables is (the solver's own choice stands), `small` when only
    the smallest tree is, None when not even that one."""
    return "auto" if with_tables <= budget else "small" if small <= budget else None


def built_mb(u: Solver) -> int | None:
    """What the solver counts for the tree it holds (`estimate_rebuild_tree`)."""
    for l in u.cmd("estimate_rebuild_tree"):
        words = l.split()
        if l.startswith("Rebuild tree. Memory needed:") and len(words) > 4 and words[4].isdigit():
            return int(words[4])
    return None


def _built_note(built: int, estimated: int) -> str:
    # the estimate is the smallest tree's: a larger one has the tables that `auto` adds when they fit
    return f"the tree built: {built} MB" + (" with outcome tables at every level (they fit in the memory that is free)"
                                            if built > estimated else "")


def setup_lines(s: Settings, flops: list[tuple[str, float]], root: Node | None = None,
                outcome_tables: str = "auto") -> list[str]:
    """`script`, and the choice of outcome tables where it is not the solver's own."""
    return script(s, flops, root) + ([] if outcome_tables == "auto" else [f"set_outcome_tables {outcome_tables}"])


def physical_memory_mb() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") >> 20
    except (ValueError, OSError, AttributeError):
        return None


# ---- results --------------------------------------------------------------------------------------

def hand_class(hand: str) -> str:
    """`AhKh` -> `AKs`."""
    r1, s1, r2, s2 = hand
    if r1 == r2:
        return r1 + r2
    if RANKS.index(r1) > RANKS.index(r2):
        r1, r2 = r2, r1
    return r1 + r2 + ("s" if s1 == s2 else "o")


def cell_class(row: int, col: int) -> str:
    """The class in a chart's cell: pairs on the diagonal, suited above it, offsuit below."""
    if row == col:
        return RANKS[row] * 2
    return RANKS[min(row, col)] + RANKS[max(row, col)] + ("s" if row < col else "o")


def _floats(lines: list[str], want: int, what: str) -> list[list[float]]:
    rows = []
    for l in lines:
        parts = l.split()
        if len(parts) == want:
            try:
                rows.append([float(x) for x in parts])
            except ValueError:
                pass
    if not rows:
        raise PreflopError(f"{what} answered {' '.join(lines)[:200]}")
    return rows


def _facing(order: list[str], theirs: list[float]) -> list[float]:
    """For each hand, the weight of the opponent's hands that share no card with it."""
    by_card: dict[str, float] = {}
    for hand, y in zip(order, theirs):
        for card in (hand[0:2], hand[2:4]):
            by_card[card] = by_card.get(card, 0.0) + y
    total = sum(theirs)
    return [max(0.0, total - by_card[h[0:2]] - by_card[h[2:4]] + y) for h, y in zip(order, theirs)]


SOLVE_ACCURACY, CHECK_ACCURACY = 0.5, 0.05  # bb/100

NO_DEAL = "the two ranges leave no deal: every hand of one shares a card with every hand of the other"


def ranges_meet(order: list[str], one: list[float], other: list[float]) -> bool:
    """Whether some hand of one range can be dealt against some hand of the other (weights in `order`)."""
    return sum(x * y for x, y in zip(one, _facing(order, other))) > 0


def collect(u: Solver, s: Settings, root: Node) -> list[dict]:
    """Every preflop decision as hand classes: how often each class gets there, what it does, its EV.
    A combo counts by the deals it is in: its own reach times the opponent's reach in the hands that
    share no card with it. So a combo the opponent's range rules out counts for nothing, and the split
    of the actions at a node is the split of the deals that get there."""
    order = " ".join(u.cmd("show_hand_order")).split()
    if len(order) != 1326:
        raise PreflopError("show_hand_order did not list 1326 hands")
    classes = [hand_class(h) for h in order]
    out = []
    for node in walk(root):
        if node.kind != "decision":
            continue
        who = "IP" if node.actor == SB else "OOP"
        kids = [l.strip() for l in u.cmd(f"show_children {node.id}") if l.startswith("r:")]
        mine = [child.id for _, child in node.children]
        if kids != mine:
            raise PreflopError(f"the solver's tree differs at {node.id}: it has {kids}, expected {mine}")
        strat = _floats(u.cmd(f"show_strategy {node.id}"), 1326, f"show_strategy {node.id}")
        if len(strat) != len(mine):
            raise PreflopError(f"show_strategy {node.id}: {len(strat)} rows for {len(mine)} actions")
        reach = _floats(u.cmd(f"show_range {who} {node.id}"), 1326, f"show_range {node.id}")[0]
        other = "OOP" if who == "IP" else "IP"
        theirs = _floats(u.cmd(f"show_range {other} {node.id}"), 1326, f"show_range {node.id}")[0]
        theirs0 = _floats(u.cmd(f"show_range {other} r:0"), 1326, "show_range r:0")[0]
        ev = _floats(u.cmd(f"calc_ev {who} {node.id}"), 1326, f"calc_ev {node.id}")[0]
        start = _floats(u.cmd(f"show_range {who} r:0"), 1326, "show_range r:0")[0]
        here = [x * y for x, y in zip(reach, _facing(order, theirs))]     # deals at this node, by combo
        dealt = [x * y for x, y in zip(start, _facing(order, theirs0))]   # deals at the start
        per: dict[str, dict] = {}
        for i, cls in enumerate(classes):
            c = per.setdefault(cls, {"dealt": 0.0, "here": 0.0, "ev": 0.0, "ev_of": 0.0, "freq": [0.0] * len(mine)})
            c["dealt"] += dealt[i]
            if here[i] <= 0:
                continue
            c["here"] += here[i]
            if math.isfinite(ev[i]):
                c["ev"] += here[i] * ev[i]
                c["ev_of"] += here[i]
            for a in range(len(mine)):
                c["freq"][a] += here[i] * strat[a][i]
        total = sum(c["here"] for c in per.values())
        hands = {}
        for cls, c in per.items():
            if c["dealt"] <= 0:
                continue  # not in the player's range, or never dealt against the opponent's
            got = c["here"] > 1e-9 * c["dealt"]
            hands[cls] = {"reach": c["here"] / c["dealt"],
                          "freq": [f / c["here"] if got else 0.0 for f in c["freq"]],
                          "ev_bb": c["ev"] / c["ev_of"] / s.chips if got and c["ev_of"] > 0 else None}
        out.append({
            "id": node.id, "actor": node.actor, "line": list(node.said),
            "pot_bb": [node.inv[0] / s.chips, node.inv[1] / s.chips],
            "actions": [label for label, _ in node.children],
            "reach": total / sum(dealt) if sum(dealt) > 0 else 0.0,
            "freq": [sum(c["freq"][a] for c in per.values()) / total if total > 0 else 0.0 for a in range(len(mine))],
            "hands": hands,
        })
    return out


def solve_on(u: Solver, s: Settings, accuracy_bb100: float, seconds: int, log: bool) -> dict[str, float]:
    """Solve the tree in `u` to the accuracy or for the seconds; the figures of `calc_results` at the end
    (chips: the two EVs, each player's best response against the other, the exploitability)."""
    u.cmd(f"set_accuracy {accuracy_bb100 / 100 * s.chips:.6g}")
    u.cmd("set_info_freq auto")
    last: dict[str, float] = {}
    quiet = not log

    def progress(line: str) -> None:
        key, _, value = line.partition(":")
        if key in ("running time", "EV OOP", "EV IP", "OOP's MES", "IP's MES", "Exploitable for"):
            try:
                last[key] = float(value)
            except ValueError:
                return
            if key == "Exploitable for" and not quiet:
                print(f"  {time.time() - began:7.0f} s   exploitable for "
                      f"{last[key] / s.chips * 100:.2f} bb/100 (target {accuracy_bb100:g})", file=sys.stderr,
                      flush=True)

    began = time.time()  # the solver's own running time goes on from one solve of a check to the next
    u.cmd(f"go {seconds} seconds")
    u.cmd("wait_for_solver", progress)
    quiet = True  # the final figures are in the result, not another progress line
    for l in u.cmd("calc_results"):
        progress(l)
    return last


def solve(exe: str, s: Settings, flop_name: str, flops: list[tuple[str, float]], accuracy_bb100: float,
          seconds: int, threads: int | None, save_tree: str | None, estimated_mb: int | None = None,
          log: bool = True, outcome_tables: str = "auto") -> dict:
    root = build_tree(s)
    started = time.time()
    if estimated_mb is None:
        # in a process of its own: the solver builds a whole second tree to answer, and in the solving
        # process that would stand next to the real one
        estimated_mb = estimate_mb(exe, s, flops)
        if log:
            mem = physical_memory_mb()
            print(f"{len(flops)} flops: estimated {estimated_mb} MB"
                  + (f", more than this machine's {mem} MB" if mem and estimated_mb > mem else ""),
                  file=sys.stderr, flush=True)
    tree_error = None
    with Solver(exe) as u:
        for line in setup_lines(s, flops, root, outcome_tables):
            u.need(line)
        if threads:
            u.cmd(f"set_threads {threads}")
        u.need("add_all_flops")
        built = built_mb(u)
        if log and built is not None and built != estimated_mb:
            print(_built_note(built, estimated_mb), file=sys.stderr, flush=True)
        order = " ".join(u.cmd("show_hand_order")).split()
        sb, bb = (_floats(u.cmd(f"show_range {who} r:0"), 1326, "show_range r:0")[0] for who in ("IP", "OOP"))
        if len(order) != 1326 or not ranges_meet(order, sb, bb):
            raise PreflopError(NO_DEAL)
        last = solve_on(u, s, accuracy_bb100, seconds, log)
        nodes = collect(u, s, root)
        if save_tree:
            try:
                u.need(f'dump_tree "{save_tree}" no_rivers')
            except PreflopError as e:
                tree_error = f"--save-tree: {e}"  # the result is still worth having
    return {
        "version": 1,
        "settings": describe(s),
        "flops": flop_name, "flop_count": len(flops),
        "chips_per_bb": s.chips,
        "ev_sb_bb": last.get("EV IP", 0.0) / s.chips, "ev_bb_bb": last.get("EV OOP", 0.0) / s.chips,
        "exploitable_bb100": last.get("Exploitable for", 0.0) / s.chips * 100,
        "target_bb100": accuracy_bb100,
        "seconds": round(time.time() - started, 1),
        "estimated_mb": estimated_mb,
        "built_mb": built, "outcome_tables": outcome_tables,
        "save_tree_error": tree_error,
        "nodes": nodes,
    }


# ---- what a result loses in a larger game ------------------------------------------------------------

PREFLOP_SETTINGS = ("stack_bb", "ante_bb", "limp", "open", "iso", "3bet", "limp_raise", "4bet", "5bet",
                    "allin_threshold")


def _same(a, b) -> bool:
    """Two recorded settings say the same: amounts and sizes compare as numbers where they are numbers,
    so 2.50 is 2.5 and a stack of 12.0 is one of 12."""
    def parts(v):
        out = []
        for word in v.split():
            unit = "x" if word.endswith("x") else "bb"
            digits = word[:-1] if word.endswith("x") else word[:-2] if word.endswith("bb") else word
            try:
                number = Decimal(digits)
            except InvalidOperation:
                number = None
            out.append((unit, number) if number is not None and number.is_finite() else word)
        return out
    return parts(a) == parts(b) if isinstance(a, str) and isinstance(b, str) else a == b


def _shows(x: float) -> bool:
    """A figure in bb/100 that is not 0.00 as the check prints it."""
    return f"{abs(x):.2f}" != "0.00"


def _f2(x: float) -> str:
    return f"{x:.2f}" if _shows(x) else "0.00"  # not -0.00


OTHER_VERSION = "{name} is a version {version} result: this `preflop.py` reads version 1"


def _fits(name: str, result: dict, s: Settings, decisions: list[Node]) -> list[str]:
    """A result can be checked on a tree with its preflop game: the same stack, sizes and decisions. The
    flops and the postflop tree are the check's own; returns the postflop settings that are not the
    result's (`file`: both have a postflop file, and a result does not say which)."""
    try:
        mine, its = describe(s), result["settings"]

        def said(d: dict, k: str):  # no limp-raise sizes: the 3-bet's
            return d["3bet"] if k == "limp_raise" and d[k] is None else d[k]
        version = result.get("version")
        if type(version) is not int:  # not true, not 1.0
            raise TypeError
        if version != 1:
            raise PreflopError(OTHER_VERSION.format(name=name, version=version))
        ok = (all(_same(said(its, k), said(mine, k)) for k in PREFLOP_SETTINGS)
              and result["chips_per_bb"] == s.chips
              and [(n["id"], n["actions"]) for n in result["nodes"]]
              == [(n.id, [label for label, _ in n.children]) for n in decisions])
        if not isinstance(its.get("depth"), str) or not all(isinstance(n["hands"], dict) for n in result["nodes"]):
            raise TypeError
        other = [k for k in mine if k not in PREFLOP_SETTINGS and not _same(its.get(k), mine[k])]
    except (KeyError, TypeError, AttributeError):
        raise PreflopError(f"{name} is not a result of `preflop.py solve`")
    if not ok:
        raise PreflopError(f"{name} was solved with another preflop game: give `check` the options of that solve "
                           "(stack, ante, sizes, --chips-per-bb; the flops and the postflop tree are the check's own)")
    return ["file"] if mine["depth"] == its["depth"] == "file" else other


def _class_rows(name: str, node: dict, actions: int) -> dict[str, list[float]]:
    """What a result says at one decision: the frequencies of every hand class it brought there, summing
    to 1. A class with no frequency was not brought there (by the player, or by the opponent)."""
    def share(f) -> bool:
        try:
            return isinstance(f, (int, float)) and not isinstance(f, bool) and math.isfinite(f) and f >= 0
        except OverflowError:  # an integer no float holds
            return False
    out = {}
    for cls, h in node["hands"].items():
        freq = h.get("freq") if isinstance(h, dict) else None
        if not isinstance(freq, list) or len(freq) != actions or not all(share(f) for f in freq):
            raise PreflopError(f"{name}: the frequencies of {cls} at {node['id']} are not a strategy")
        freq = [float(f) for f in freq]
        total = sum(freq)
        if not math.isfinite(total):
            raise PreflopError(f"{name}: the frequencies of {cls} at {node['id']} are not a strategy")
        if total > 0:
            out[cls] = [f / total for f in freq]
    return out


def locked_rows(known: dict[str, list[float]], actions: int, classes: list[str]) -> list[list[float]]:
    """A result's strategy at one decision as the solver takes it, a row of 1326 per action: every combo
    of a hand class plays the class's frequencies. A class the result has nothing for plays every action
    equally: `_plan` locks a decision only where that cannot show in the loss."""
    return [[known[cls][a] if cls in known else 1.0 / actions for cls in classes] for a in range(actions)]


def _plan(name: str, result: dict, root: Node, decisions: list[Node], classes: list[str],
          dealt: dict[str, list[float]], ranges: dict[str, list[float]], swing_bb: float) -> dict[str, dict]:
    """What the check locks of a result, per player: `rows` by node id, and `free`, the decisions it
    leaves to the solve, which plays them as well as they can be played (`part`: those of them that are
    not simply decisions the result's opponent never goes to). `dealt`: per combo, over 0 for one the
    check deals; `ranges`: the weights; `swing_bb`: the most one hand can move a player's result, two
    stacks and the ante.

    The solver locks a decision whole. A result has no frequencies for a hand class at a decision the
    class did not come to in its solve: by the player's own play, or because the opponent never went
    there with a hand that leaves it possible. Locked, such a class plays evenly, and an opponent who
    is free in the check can make that cost what it likes. So a decision is locked only where the hands
    the result is silent on cannot show: their share of the player's deals, times how often the result's
    own play before it brings them there, times the swing, is 0.00 bb/100 (`unsaid_bb100`, summed over
    the locked decisions). Below a decision left free every hand may come.

    A result that cannot be one of these ranges' solve is refused where its hand classes show it:
    - a class with frequencies that the player's range does not hold;
    - a class the check deals without them at the small blind's first decision, where every hand is;
    - a class with all its combos in the player's range without them at a first decision that has them
      for others (no hand shares a card with every combo of a class, so an opponent who comes there
      meets it);
    - where both ranges hold whole classes at one weight: hands without frequencies at a decision the
      result's own strategies bring them to, the player's by the opponent's, often enough to show (a
      result changed so that hands go where it has nothing for them is refused by this too). That
      counts a class's combos as playing alike and as meeting any hand of the opponent, which ranges
      that name suits or weights undo: a combo the opponent's cards rule out need not be there.
    What is left free after that is named: `part` unless the result's own opponent comes to the
    decision too rarely to show, which is then the reason it has nothing there. How often the opponent
    comes is taken from the opponent's frequencies in the result, and a decision on the way that has none
    for a hand counts as passed by it, so nothing is said to be never come to because a file is silent
    higher up."""
    by_id = {n["id"]: n for n in result["nodes"]}
    known = {n.id: _class_rows(name, by_id[n.id], len(n.children)) for n in decisions}
    even = not _uneven(classes, ranges)
    share: dict[str, dict[str, float]] = {}
    for who in (SB, BB):
        total = sum(dealt[who])
        share[who] = {}
        for cls, x in zip(classes, dealt[who]):
            if x > 0:
                share[who][cls] = share[who].get(cls, 0.0) + x / total

    def reaches(who: str) -> dict[str, dict[str, float]]:
        """Per decision, how often the result's play brings each hand class of the player there at the
        most: the product of its frequencies on the way. Where the result has none for the class it goes
        on as it came: the result does not say that it stops, and a decision emptied in a file must not
        read as one the player never passes."""
        at: dict[str, dict[str, float]] = {}

        def go(n: Node, reach: dict[str, float]) -> None:
            if n.kind != "decision":
                return
            at[n.id] = reach
            for a, (_, child) in enumerate(n.children):
                go(child, reach if n.actor != who else
                   {cls: p * (known[n.id][cls][a] if cls in known[n.id] else 1.0) for cls, p in reach.items()})
        go(root, {cls: 1.0 for cls in share[who]})
        return at
    own = {who: reaches(who) for who in (SB, BB)}
    out = {}
    for who, them in ((SB, BB), (BB, SB)):
        blind = f"the {'small' if who == SB else 'big'} blind's"
        mine = share[who]
        whole = set(mine) - {cls for cls, w in zip(classes, ranges[who]) if w <= 0}

        def refuse(text: str, some: list[str], why: str = "give `check` the ranges of that solve") -> None:
            raise PreflopError(f"{name} {text} ({', '.join(some[:5])}{', ...' if len(some) > 5 else ''}): {why}")
        if who == SB and not known[root.id]:
            raise PreflopError(f"{name} has no strategy for the small blind's first decision")
        extra = sorted({cls for n in decisions if n.actor == who for cls in known[n.id]} - set(mine))
        if extra:
            refuse(f"has a strategy for {len(extra)} hand classes that are not in {blind} range", extra)
        rows: dict[str, list[list[float]]] = {}
        free: list[str] = []
        part: list[str] = []
        unsaid = 0.0

        def visit(n: Node, comes: dict[str, float], first: bool, loose: bool = False) -> None:
            nonlocal unsaid
            if n.kind != "decision":
                return
            if n.actor != who:
                for _, child in n.children:
                    visit(child, comes, first, loose)
                return
            here, k = known[n.id], len(n.children)
            gap = sum(mine[cls] * p for cls, p in comes.items() if cls not in here) * swing_bb * 100
            if not _shows(unsaid + gap):
                if not here:   # nothing to lock, and nothing that comes here to speak of
                    for _, child in n.children:
                        visit(child, comes, False, loose)
                    return
                unsaid += gap
                rows[n.id] = locked_rows(here, k, classes)
                for a, (_, child) in enumerate(n.children):
                    visit(child, {cls: p * (here[cls][a] if cls in here else 1.0 / k) for cls, p in comes.items()},
                          False, loose)
                return
            lacking = sorted(cls for cls in (mine if n is root else whole) if cls not in here)
            if first and here and lacking:
                refuse(f"has no strategy for {len(lacking)} hand classes of {blind} range at {n.id}", lacking)
            opponent = sum(share[them][cls] * p for cls, p in own[them][n.id].items())  # how often it comes here
            if even:
                told = {cls: mine[cls] * p * opponent for cls, p in own[who][n.id].items() if cls not in here and p > 0}
                if _shows(sum(told.values()) * swing_bb * 100):
                    refuse(f"has no strategy at {n.id} for hands of {blind} range that its own strategies bring there",
                           sorted(told, key=lambda cls: -told[cls]),
                           "it is not all of a strategy (a result of other ranges, or one changed since its solve)")
            free.append(n.id)
            if here or loose or _shows(opponent * swing_bb * 100):
                part.append(n.id)
            for _, child in n.children:
                visit(child, comes, False, loose or bool(here))

        visit(root, {cls: 1.0 for cls in mine}, True)
        out[who] = {"rows": rows, "free": free, "part": part, "unsaid_bb100": unsaid}
    return out


def _swing_bb(s: Settings) -> float:
    """The most one hand can move a player's result, in big blinds: two stacks and the ante."""
    return float(2 * s.stack + s.ante)


def _uneven(classes: list[str], ranges: dict[str, list[float]]) -> bool:
    """A range holds a hand class in part: some of its combos, or its combos at different weights."""
    for weights in ranges.values():
        low: dict[str, float] = {}
        high: dict[str, float] = {}
        for cls, w in zip(classes, weights):
            low[cls], high[cls] = min(low.get(cls, w), w), max(high.get(cls, w), w)
        if any(low[cls] != high[cls] for cls in low):
            return True
    return False


def _under(top: Node, n: Node) -> bool:
    return any(child is n or _under(child, n) for _, child in top.children)


def _value(r: dict[str, float], who: str, chips: int) -> list[float]:
    """The value of the game for a player from one solve, bb/100: [the solve's EV, low, high]. The
    value is no more than the player's best response makes against the opponent as solved, and no less
    than what the opponent's best response leaves; the two meet as the solve gets exact. (What the
    opponent's best response leaves is the two EVs' sum less the opponent's best: exact where the game is
    zero-sum but for an ante, which it is on all flops; on a subset the sum moves with the strategies.)"""
    try:
        total = r["EV OOP"] + r["EV IP"]
        mine, theirs, ev = (("IP's MES", "OOP's MES", "EV IP") if who == SB else ("OOP's MES", "IP's MES", "EV OOP"))
        out = [r[ev], total - r[theirs], r[mine]]
    except KeyError:
        raise PreflopError("the solver gave no results for the check (calc_results)")
    return [x / chips * 100 for x in out]


def _lost(base: list[float], locked: list[float]) -> list[float]:
    """What the locked strategy gives up, from the two values [EV, low, high]: the difference of the EVs,
    and the least and the most the two spans allow."""
    return [base[0] - locked[0], base[1] - locked[2], base[2] - locked[1]]


def _sum(r: dict[str, float], s: Settings) -> float:
    """The two players' values added up, less the ante they play for, bb/100: zero in a game whose flops
    are dealt evenly to the two (on all flops)."""
    return (r.get("EV OOP", 0.0) + r.get("EV IP", 0.0) - _chips(s.ante, s.chips)) / s.chips * 100


def check(exe: str, s: Settings, flop_name: str, flops: list[tuple[str, float]], results: list[tuple[str, dict]],
          accuracy_bb100: float, seconds: int, threads: int | None, estimated_mb: int | None = None,
          log: bool = True, outcome_tables: str = "auto") -> dict:
    """What each result's preflop strategy gives up in the game with these flops. One player at a time:
    the player's preflop decisions are the result's, locked; everything else (the opponent's preflop
    play, all play after the flop) is solved against them; the value of that game for the player, under
    the value of the game as it is, is the loss against an opponent who knows the strategy."""
    root = build_tree(s)
    decisions = [n for n in walk(root) if n.kind == "decision"]
    other = {name: _fits(name, r, s, decisions) for name, r in results}
    started = time.time()

    def say(text: str) -> None:
        if log:
            print(text, file=sys.stderr, flush=True)

    if estimated_mb is None:
        estimated_mb = estimate_mb(exe, s, flops)
        mem = physical_memory_mb()
        say(f"{len(flops)} flops: estimated {estimated_mb} MB"
            + (f", more than this machine's {mem} MB" if mem and estimated_mb > mem else ""))
    with Solver(exe) as u:
        for line in setup_lines(s, flops, root, outcome_tables):
            u.need(line)
        if threads:
            u.cmd(f"set_threads {threads}")
        u.need("add_all_flops")
        built = built_mb(u)
        if built is not None and built != estimated_mb:
            say(_built_note(built, estimated_mb))
        order = " ".join(u.cmd("show_hand_order")).split()
        sb, bb = (_floats(u.cmd(f"show_range {who} r:0"), 1326, "show_range r:0")[0] for who in ("IP", "OOP"))
        if len(order) != 1326 or not ranges_meet(order, sb, bb):
            raise PreflopError(NO_DEAL)
        classes = [hand_class(h) for h in order]
        dealt = {SB: [x * y for x, y in zip(sb, _facing(order, bb))], BB: [x * y for x, y in zip(bb, _facing(order, sb))]}
        ranges = {SB: sb, BB: bb}
        plans = [_plan(name, r, root, decisions, classes, dealt, ranges, _swing_bb(s))
                 for name, r in results]  # all read before any solve
        say("the game as it is:")
        free = solve_on(u, s, accuracy_bb100, seconds, log)
        base = {who: _value(free, who, s.chips) for who in (SB, BB)}
        checked = []
        for (name, r), plan in zip(results, plans):
            row: dict = {"file": name, "flops": r.get("flops"), "flop_count": r.get("flop_count"),
                         "depth": r["settings"].get("depth"), "other_postflop": other[name]}
            for who in (SB, BB):
                say(f"{os.path.basename(name)}, the {'small' if who == SB else 'big'} blind's strategy:")
                u.need("set_equal_strats")  # a fresh solve, and no lock left from the last one
                for node, rows in plan[who]["rows"].items():
                    u.need(f"set_strategy {node} " + " ".join(f"{x:.6g}" for line in rows for x in line))
                    u.need(f"lock_node {node}")
                got = solve_on(u, s, accuracy_bb100, seconds, log)
                v = _value(got, who, s.chips)
                row[who] = {"value_bb100": v, "lost_bb100": _lost(base[who], v),
                            "exploitable_bb100": got.get("Exploitable for", 0.0) / s.chips * 100,
                            "sum_bb100": _sum(got, s), "locked": len(plan[who]["rows"]),
                            "free": plan[who]["free"], "free_part": plan[who]["part"],
                            "unsaid_bb100": plan[who]["unsaid_bb100"]}
            checked.append(row)
    return {
        "version": 1, "kind": "check",
        "settings": describe(s),
        "flops": flop_name, "flop_count": len(flops),
        "target_bb100": accuracy_bb100,
        "exploitable_bb100": free.get("Exploitable for", 0.0) / s.chips * 100,
        "value_bb100": base, "sum_bb100": _sum(free, s),
        "uneven_ranges": _uneven(classes, ranges),
        "seconds": round(time.time() - started, 1),
        "estimated_mb": estimated_mb,
        "built_mb": built, "outcome_tables": outcome_tables,
        "checked": checked,
    }


def check_text(report: dict) -> list[str]:
    def cell(side: dict) -> str:
        if side.get("locked") == 0:
            return "     —  (none of it to check)"
        x = side["lost_bb100"]
        return f"{_f2(x[0]):>6}  ({_f2(x[1])} to {_f2(x[2])})"

    def tree_of(depth: str) -> str:
        return "--postflop FILE" if depth == "file" else f"--depth {depth}"
    v = report["value_bb100"][SB]
    depth = report["settings"]["depth"]
    out = [f"The game with {report['flop_count']} flops ({report['flops']}, {tree_of(depth)}), solved to "
           f"{report['exploitable_bb100']:.3f} bb/100: worth {v[0]:.2f} bb/100 to the small blind "
           f"({v[1]:.2f} to {v[2]:.2f}).",
           "What a preflop strategy gives up in it to an opponent who knows the strategy, bb/100, and the",
           "span the solves leave open:", ""]
    names = [f"{os.path.basename(str(r['file']))} ({r['flop_count']} flops"
             + (f", {tree_of(r['depth'])}" if r["depth"] != depth else "") + ")" for r in report["checked"]]
    width = max([len(n) for n in names] + [8])
    out.append(f"{'':{width}}     {'small blind':<26} big blind")
    for name, r in zip(names, report["checked"]):
        out.append(f"{name:{width}}   {cell(r[SB]):<28} {cell(r[BB])}")
    notes: list[str] = []
    for r in report["checked"]:
        file = os.path.basename(str(r["file"]))
        if r["other_postflop"] == ["file"]:
            notes += [f"{file} was solved with a postflop file, and a result does not say which: if it was not "
                    "the check's, its loss is what its preflop strategy gives up in the check's tree."]
        elif r["other_postflop"]:
            notes += [f"{file} was solved with another postflop tree than the check's ({tree_of(depth)}; "
                    f"different: {', '.join(r['other_postflop'])}): its loss is what its preflop strategy gives up "
                    "in this one."]
        for who, word, their in ((SB, "small", "big"), (BB, "big", "small")):
            part = r[who].get("free_part", [])
            none = [n for n in r[who]["free"] if n not in part]

            def ids(nodes: list[str]) -> str:
                return f"{len(nodes)} of the {word} blind's decisions ({', '.join(nodes[:4])}{', ...' if len(nodes) > 4 else ''})"
            if none:
                notes += [f"{file} has no strategy at {ids(none)}: its {their} blind never goes there. The check "
                        "plays them as well as they can be played, so the loss is that of what the result does say."]
            if part:
                notes += [f"{file} has a strategy for only some of the hands that can come to {ids(part)} (for "
                        "none of them at some). A decision is locked whole or not at all, so the check plays these "
                        "as well as they can be played, and the loss is that of what the result says at the others. "
                        "(A result leaves out the hands its solve did not bring to a decision: by their own play, or "
                        f"because no hand of the {their} blind met them there. Under a decision left to the solve, "
                        "more hands come than the result has.)"]
    if report.get("uneven_ranges"):
        notes += ["The ranges of this check hold hand classes in part (some of a class's combos, or combos at "
                "different weights). The combos of a class need not play alike then, and a result has one strategy "
                "for a class: the loss includes what playing them alike costs, which can be much."]
    sums = [report["sum_bb100"]] + [r[who]["sum_bb100"] for r in report["checked"] for who in (SB, BB)]
    if any(_shows(x) for x in sums):
        ante = Decimal(str(report["settings"].get("ante_bb") or 0)) > 0   # `describe` wrote it
        why = ("a flop subset is not dealt quite evenly to the two" if report["flops"] != ALL_FLOPS
               else "the solver's own figures are that far from exact")
        notes += [f"The two players' values{', less the ante,' if ante else ''} added up to {_f2(min(sums))} to "
                f"{_f2(max(sums))} bb/100 in these solves, "
                f"not to zero: {why}. Where that sum moves with the "
                f"strategies ({max(sums) - min(sums):.2f} here) the losses and the ends of their spans are that "
                "uncertain, and a loss can come out under zero."]
    return out + [line for note in notes for line in [""] + textwrap.wrap(note, 100)]


def describe(s: Settings) -> dict:
    def sizes(v):
        return None if v is None else " ".join(x.text for x in v)
    return {"stack_bb": str(s.stack), "ante_bb": str(s.ante), "limp": s.limp, "open": sizes(s.open),
            "iso": sizes(s.iso), "3bet": sizes(s.threebet), "limp_raise": sizes(s.limp_raise),
            "4bet": sizes(s.fourbet), "5bet": sizes(s.fivebet), "allin_threshold": str(s.allin_threshold),
            "depth": s.depth if s.postflop_config is None else "file", "bet": dict(s.bet), "raise": s.raise_,
            "donk": s.donk, "raise_cap": s.raise_cap}


# ---- charts ---------------------------------------------------------------------------------------

def _percent(value) -> float:
    """A frequency as a percent. What is no number, or is none as a percent (`10**400`, `1e308`, NaN), is
    no frequency of a result."""
    if type(value) not in (int, float):
        raise TypeError
    pct = 100 * float(value)
    if not math.isfinite(pct):
        raise ValueError
    return pct


def _cell(value: float | None) -> str:
    """A frequency in two characters: blank for a hand that never gets here, `.` for never, `**` for
    always, otherwise the percent."""
    if value is None:
        return "  "
    pct = int(round(_percent(value)))
    return " ." if pct <= 0 else "**" if pct >= 100 else f"{pct:2d}"


def grid(title: str, value, cell: int = 2) -> list[str]:
    """A 13x13 chart, 40 characters wide with cells of two. `value(class)` gives a cell's characters."""
    width = 1 + 13 * (cell + 1)
    out = [title[:width].ljust(width), "  " + " ".join(r.rjust(cell) for r in RANKS)]
    for r in range(13):
        out.append(RANKS[r] + " " + " ".join(value(cell_class(r, c)).rjust(cell) for c in range(13)))
    return out


def chart_node(node: dict, width: int, ev: bool = False) -> list[str]:
    hands = node["hands"]
    head = f"{node['id']}   {' / '.join(node['line']) or 'first to act'}   ->  {node['actor']} to act"
    freqs = ", ".join(f"{a} {_percent(f):.1f} %" for a, f in zip(node["actions"], node["freq"]))
    out = [head, f"  {_percent(node['reach']):.1f} % of deals get here; then {freqs}", ""]
    grids = []
    for a, label in enumerate(node["actions"]):
        def value(cls, a=a):
            h = hands.get(cls)
            return _cell(None if h is None or h["ev_bb"] is None else h["freq"][a])
        grids.append(grid(f"{label} ({_percent(node['freq'][a]):.1f} %)", value))
    if ev:
        def value(cls):
            h = hands.get(cls)
            if h is None or h["ev_bb"] is None:
                return ""
            v = h["ev_bb"]
            return f"{v:+.2f}" if abs(v) < 9.995 else f"{v:+.1f}" if abs(v) < 99.95 else f"{v:+.0f}"
        grids.append(grid(f"EV of the hand here, big blinds won from the start of the hand", value, 5))
    row: list[list[str]] = []  # as many charts side by side as the width holds
    for g in grids + [None]:
        if row and (g is None or sum(len(x[0]) + 3 for x in row) + len(g[0]) > width):
            out += ["   ".join(parts).rstrip() for parts in zip(*row)] + [""]
            row = []
        if g is not None:
            row.append(g)
    return out


def charts(result: dict, only: list[str] | None = None, width: int | None = None, ev: bool = False) -> list[str]:
    width = width or shutil.get_terminal_size((80, 24)).columns
    out = [f"Heads-up, {result['settings']['stack_bb']} big blinds, {result['flops']} ({result['flop_count']} flops): "
           f"small blind {result['ev_sb_bb']:+.3f} bb a hand, big blind {result['ev_bb_bb']:+.3f}; "
           f"exploitable for {result['exploitable_bb100']:.2f} bb/100",
           "Each chart: how often the hand takes the action, in percent (** always, . never, blank: the hand "
           "never gets here). Suited hands above the diagonal, offsuit below.", ""]
    if not result["nodes"]:
        out.append("(no decisions in this result)")
    known = {n["id"] for n in result["nodes"]}
    for want in only or []:
        if want not in known:
            raise PreflopError(f"no decision {want} in this result (there are {', '.join(sorted(known))})")
    for node in result["nodes"]:
        if only and node["id"] not in only:
            continue
        out += chart_node(node, width, ev)
    return out


# ---- command line ---------------------------------------------------------------------------------

def tree_rows(s: Settings, root: Node) -> list[dict]:
    """The preflop tree, a node a row: how deep it is, the action that leads to it, what each player has
    in (big blinds, small blind first) and what happens there. `tree` prints these; the viewer lists them."""
    cfg = postflop_config(s)
    out = []
    for node in walk(root):
        if node.kind == "decision":
            tail = f"{node.actor} to act"
        elif node.kind == "fold":
            tail = "hand over"
        elif node.kind == "allin":
            tail = "all-in, showdown"
        else:
            lines = exit_lines(s, cfg, node)
            tail = f"flop, {len(lines)} postflop line{'s' if len(lines) != 1 else ''}"
        out.append({"depth": node.id.count(":") - 1, "what": node.said[-1] if node.said else "start",
                    "id": node.id, "kind": node.kind,
                    "in": [_bb(node.inv[0], s.chips), _bb(node.inv[1], s.chips)], "then": tail})
    return out


def tree_text(s: Settings, root: Node) -> list[str]:
    out = []
    for r in tree_rows(s, root):
        depth, pot = r["depth"], " / ".join(r["in"])
        out.append(f"{'  ' * depth}{r['what']:<{max(1, 22 - 2 * depth)}} {r['id']:<34} in: {pot:<13} {r['then']}")
    return out


def settings_from(a: argparse.Namespace) -> Settings:
    def number(text, name):
        try:
            value = Decimal(text)
        except InvalidOperation:
            value = None
        if value is None or not value.is_finite():
            raise PreflopError(f"--{name}: {text!r} is not a number")
        return value
    s = Settings()
    s.stack, s.chips, s.ante = number(a.stack, "stack"), a.chips_per_bb, number(a.ante, "ante")
    if s.ante < 0:
        raise PreflopError("--ante cannot be negative")
    s.limp = not a.no_limp
    s.open, s.iso = parse_sizes(a.open, "open"), parse_sizes(a.iso, "iso")
    s.threebet, s.fourbet, s.fivebet = (parse_sizes(a.threebet, "3bet"), parse_sizes(a.fourbet, "4bet"),
                                        parse_sizes(a.fivebet, "5bet"))
    s.limp_raise = None if a.limp_raise is None else parse_sizes(a.limp_raise, "limp-raise")
    s.allin_threshold = number(a.allin_threshold, "allin-threshold")
    if not 0 <= s.allin_threshold <= 100:
        raise PreflopError("--allin-threshold is a percent of the stack, 0 to 100")
    s.depth = a.depth or "flop"
    s.bet = {"Flop": a.flop_bet, "Turn": a.turn_bet, "River": a.river_bet}
    s.raise_, s.donk, s.raise_cap = a.raise_, a.donk, a.raise_cap
    s.postflop_allin_threshold = number(a.postflop_allin_threshold, "postflop-allin-threshold")
    s.postflop_allin_pot = number(a.postflop_allin_pot, "postflop-allin-pot")
    if a.postflop:
        try:
            with open(a.postflop) as f:
                s.postflop_config = f.read()
        except OSError as e:
            raise PreflopError(f"cannot read --postflop: {e}")
    if a.range_sb:
        s.range_sb = a.range_sb
    if a.range_bb:
        s.range_bb = a.range_bb
    return s


def _tables_note(small: int, with_tables: int, budget: int) -> str:
    """What a budget does to the outcome tables, for the note of a choice."""
    if with_tables == small:
        return ""
    if with_tables <= budget:
        return f"; {with_tables} MB with outcome tables at every level, which the solver builds when they fit in free memory"
    return (f"; {with_tables} MB with outcome tables at every level, over the {budget}: built without them "
            "(set_outcome_tables small)")


def pick_flops(a: argparse.Namespace, s: Settings) -> tuple[str, list[tuple[str, float]], list[str], int | None, str]:
    """`--flops`, or the largest shipped subset the memory budget holds. (name, flops, notes, the
    smallest tree's estimate in MB when one was made, the `set_outcome_tables` to build under.) The
    budget holds the tree that is built: the solver adds outcome tables to a tree with turn bets when
    there is memory free for them (#496), so a tree that only fits without them is built `small`."""
    if a.flops:
        name, flops = choose_flops(a.flops)
        if not a.memory:
            return name, flops, [], None, "auto"
        budget = int(a.memory * 1024)
        small, with_tables = estimates_mb(a.solver, s, flops)
        tables = tables_within(small, with_tables, budget)
        if tables is None:
            raise PreflopError(f"{len(flops)} flops are estimated at {small} MB with this postflop tree, over "
                               f"--memory's {budget} MB: take fewer flops, bet on fewer streets (--depth), or raise --memory")
        note = f"{len(flops)} flops: estimated {small} MB, under --memory's {budget} MB" + _tables_note(small, with_tables, budget)
        return name, flops, [note], small, tables
    budget = int(a.memory * 1024) if a.memory else (physical_memory_mb() or 16384) // 2
    # the budget is for the estimate; the process takes more (README), which the default's half leaves room for
    best = None
    for count in SHIPPED:
        if not os.path.exists(flop_path(count)):
            continue
        small, with_tables = estimates_mb(a.solver, s, read_flop_file(flop_path(count)))
        if small <= budget:
            best = (count, small, with_tables)
        else:
            break
    if best is None:
        raise PreflopError(f"not even {SHIPPED[0]} flops fit in {budget} MB with this postflop tree: "
                           "bet on fewer streets (--depth), or raise --memory")
    count, small, with_tables = best
    note = (f"{count} flops: the largest shipped subset under {budget} MB "
            f"({'--memory' if a.memory else 'half of this machine'}); estimated {small} MB"
            + _tables_note(small, with_tables, budget))
    return f"flops/wsolver_{count}.txt", read_flop_file(flop_path(count)), [note], small, tables_within(small, with_tables, budget)


def estimate_table(a: argparse.Namespace, s: Settings) -> list[str]:
    if a.flops:  # one column: the flops asked for
        name, flops = choose_flops(a.flops)
        sets = [(str(len(flops)), flops)]
    else:
        sets = [(str(c), read_flop_file(flop_path(c))) for c in SHIPPED if os.path.exists(flop_path(c))]
    depths = [s.depth] if s.postflop_config is not None or a.depth else list(DEPTHS)
    out = ["Estimated memory of the tree, MB (16-bit storage; the solver's estimate_tree). Rows: the last street "
           "with betting.", ""]
    out.append(f"{'':<8}" + "".join(f"{c:>11}" for c, _ in sets) + "   flops")
    mem = physical_memory_mb()
    limit = a.limit * 1024 if a.limit else mem
    for depth in depths:
        row: list[int | None] = []
        over = False
        sd = tb.dataclasses.replace(s, depth=depth)
        for _, flops in sets:
            if over:
                row.append(None)  # more flops only cost more
                continue
            row.append(estimate_mb(a.solver, sd, flops))
            over = bool(limit) and row[-1] > limit
        name = "file" if s.postflop_config is not None else depth
        out.append(f"{name:<8}" + "".join(f"{'-':>11}" if v is None else f"{v:>11,}" for v in row))
    if mem:
        out += ["", f"This machine has {mem:,} MB. A row stops at the first size over "
                    + (f"--limit {a.limit:g} GB." if a.limit else "that (--limit GB for another bound).")]
    return out


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("script", "tree", "estimate", "solve", "chart", "check"))
    p.add_argument("args", nargs="*", help="solve: the result file; chart: the result file, then node ids; "
                   "check: the result files to check")
    g = p.add_argument_group("the game (big blinds)")
    g.add_argument("--stack", default="100", help="effective stack (default 100)")
    g.add_argument("--ante", default="0", help="dead money in the pot before the blinds (default 0)")
    g.add_argument("--chips-per-bb", type=int, default=100, help="chips in a big blind (default 100)")
    g.add_argument("--range-sb", help="the small blind's range (default every hand)")
    g.add_argument("--range-bb", help="the big blind's range (default every hand)")
    g = p.add_argument_group("preflop sizes (2.5 = raise to 2.5 big blinds, 4x = four times the bet faced, allin; "
                             "several sizes separated by commas)")
    g.add_argument("--no-limp", action="store_true", help="the small blind raises or folds")
    g.add_argument("--open", default="2.5", help="small blind's first raise (default 2.5)")
    g.add_argument("--iso", default="3.5", help="big blind's raise over a limp (default 3.5; empty: none)")
    g.add_argument("--3bet", dest="threebet", default="4x", help="the second raise (default 4x)")
    g.add_argument("--limp-raise", help="small blind's raise after limping (default: the --3bet sizes)")
    g.add_argument("--4bet", dest="fourbet", default="2.3x", help="the third raise (default 2.3x)")
    g.add_argument("--5bet", dest="fivebet", default="allin", help="the fourth raise (default allin); "
                   "any raise after it is all-in")
    g.add_argument("--allin-threshold", default="50", help="a raise to more than this percent of the stack "
                   "is made all-in (default 50)")
    g = p.add_argument_group("postflop (percent of the pot, the sizing rules of tools/tree_builder.py)")
    g.add_argument("--depth", choices=DEPTHS, help="the last street with betting; later streets are checked "
                   "down (default flop; estimate: every depth unless one is given)")
    g.add_argument("--flop-bet", default="50")
    g.add_argument("--turn-bet", default="50")
    g.add_argument("--river-bet", default="50")
    g.add_argument("--raise", dest="raise_", default="60", help="raise sizes on every street (default 60)")
    g.add_argument("--donk", default="", help="big blind's lead into the preflop raiser (default none)")
    g.add_argument("--raise-cap", type=int, default=2, help="bets and raises per street (default 2)")
    g.add_argument("--postflop-allin-threshold", default="67", help="a bet or raise to more than this percent "
                   "of the stack left at the flop is made all-in (default 67)")
    g.add_argument("--postflop-allin-pot", default="150", help="an all-in is offered next to the sizes when the "
                   "stack left is under this percent of the pot (default 150)")
    g.add_argument("--postflop", metavar="FILE", help="a `#Key#Value` tree config (a PioViewer save's header) for "
                   "the postflop betting, instead of the options above; its pot and stack are ignored")
    g = p.add_argument_group("flops and the solve")
    g.add_argument("--flops", help=f"a number we ship a subset for ({', '.join(map(str, SHIPPED))}), a flop file, "
                   "or `all`; default: the largest shipped subset that fits --memory")
    g.add_argument("--memory", type=float, help="GB the estimate of the tree that is built may take: the tool "
                   "chooses the subset under it (default: half of this machine's), --flops over it is refused, "
                   "and the outcome tables the solver adds to a tree with turn bets are left out when they "
                   "would pass it (the solver process takes about 1.1 times the estimate)")
    g.add_argument("--limit", type=float, help="estimate: stop a row once a cell is over this many GB")
    g.add_argument("--accuracy", type=float, help="solve until exploitable for less than this many big blinds "
                   f"per 100 hands (default {SOLVE_ACCURACY:g}; check: {CHECK_ACCURACY:g}; with full ranges the "
                   "losses it prints are known to about four times this, in a game of a few hands they can "
                   "move by much more)")
    g.add_argument("--time", type=int, default=86400, help="solve for at most this many seconds (default a day)")
    g.add_argument("--threads", type=int)
    g.add_argument("--save-tree", metavar="FILE", help="solve: also save the solved tree (dump_tree, no rivers)")
    g.add_argument("--report", metavar="FILE", help="check: also write the numbers as JSON")
    g.add_argument("--ev", action="store_true", help="charts: add a chart of each hand's EV")
    g.add_argument("--width", type=int, help="charts: characters across (default: the terminal's)")
    g.add_argument("--solver", default=default_solver(), help="the solver binary (default ./solver)")
    return p


def _can_write(path: str, what: str, for_solver: bool = False) -> None:
    """Refuse a file that cannot be written before a solve is spent on it."""
    if for_solver and ('"' in path or "\n" in path):
        raise PreflopError(f"{what}: a file name with a quote in it cannot be given to the solver")
    where = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(where):
        raise PreflopError(f"{what}: there is no directory {where}")
    if os.path.isdir(path) or not os.access(path if os.path.exists(path) else where, os.W_OK):
        raise PreflopError(f"{what}: cannot write {path}")


def read_result(path: str) -> object:
    """A result file as JSON. A string that cannot be written out again (a lone surrogate escape) is
    refused here, in words, and not where it would be printed."""
    with open(path) as f:
        result = json.load(f)
    json.dumps(result, ensure_ascii=False).encode("utf-8")  # UnicodeEncodeError is a ValueError
    return result


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_intermixed_args(argv)
    try:
        takes = {"chart": (1, None), "solve": (1, 1), "check": (1, None)}.get(a.command, (0, 0))
        if len(a.args) < takes[0]:
            raise PreflopError(f"{a.command} needs a result file" if a.command in ("chart", "check")
                               else "solve needs a file for the result")
        if takes[1] is not None and len(a.args) > takes[1]:
            raise PreflopError(f"{a.command}: do not know what to do with {' '.join(a.args[takes[1]:])!r} "
                               "(options are separate words: --stack 20, not \"--stack 20\")")
        if a.command == "chart":
            try:
                result = read_result(a.args[0])
            except (OSError, ValueError, RecursionError) as e:
                raise PreflopError(f"cannot read the result: {e}")
            try:
                if not isinstance(result, dict) or type(result.get("version")) is not int:
                    raise KeyError("version")
                if result["version"] != 1:
                    raise PreflopError(OTHER_VERSION.format(name=a.args[0], version=result["version"]))
                text = charts(result, a.args[1:], a.width, a.ev)
            except (KeyError, TypeError, IndexError, AttributeError, ValueError, ArithmeticError):  # a number no float holds
                raise PreflopError(f"{a.args[0]} is not a result of `preflop.py solve`")
            print("\n".join(text))
            return 0
        if a.time < 1:
            raise PreflopError("--time is a number of seconds, 1 or more")
        if a.threads is not None and a.threads < 1:
            raise PreflopError("--threads is 1 or more")
        for name in ("memory", "limit"):
            value = getattr(a, name)
            if value is not None and not (value > 0 and math.isfinite(value)):
                raise PreflopError(f"--{name} is a number of gigabytes over 0")
        if a.accuracy is None:
            a.accuracy = CHECK_ACCURACY if a.command == "check" else SOLVE_ACCURACY
        if not (a.accuracy >= 0 and math.isfinite(a.accuracy)):
            raise PreflopError("--accuracy is a number of big blinds per 100 hands, 0 or more")
        results = []
        if a.command == "check":
            for path in a.args:
                try:
                    results.append((path, read_result(path)))
                except (OSError, ValueError, RecursionError) as e:
                    raise PreflopError(f"cannot read the result {path}: {e}")
                if not isinstance(results[-1][1], dict) or not isinstance(results[-1][1].get("settings"), dict):
                    raise PreflopError(f"{path} is not a result of `preflop.py solve`")
                version = results[-1][1].get("version")
                if type(version) is int and version != 1:
                    raise PreflopError(OTHER_VERSION.format(name=path, version=version))
            if a.depth is None and a.postflop is None:
                depths = [r["settings"].get("depth") for _, r in results]
                for (path, _), depth in zip(results, depths):
                    if depth == "file":
                        raise PreflopError(f"{path} was solved with a postflop file: give `check` that file "
                                           "(--postflop FILE), or a --depth")
                    if depth not in DEPTHS:
                        raise PreflopError(f"{path} is not a result of `preflop.py solve`")
                if len(set(depths)) > 1:
                    raise PreflopError("the results were solved with different postflop trees: say which the check "
                                       "is on (--depth, or --postflop FILE)")
                a.depth = depths[0]
        s = settings_from(a)
        root = build_tree(s)
        if a.command == "tree":
            print("\n".join(tree_text(s, root)))
            return 0
        if a.command == "estimate":
            print("\n".join(estimate_table(a, s)))
            return 0
        if a.command == "solve":
            _can_write(a.args[0], "the result")
            if a.save_tree:
                _can_write(a.save_tree, "--save-tree", for_solver=True)
        if a.command == "check":
            for path, r in results:
                _fits(path, r, s, [n for n in walk(root) if n.kind == "decision"])
            if a.report:
                _can_write(a.report, "--report")
        name, flops, notes, estimated, tables = pick_flops(a, s)
        for note in notes:
            print(note, file=sys.stderr)
        if a.command == "script":
            print("\n".join(setup_lines(s, flops, root, tables) + ["add_all_flops"]))
            return 0
        if a.command == "check":
            report = check(a.solver, s, name, flops, results, a.accuracy, a.time, a.threads, estimated,
                           outcome_tables=tables)
            if a.report:
                try:
                    with open(a.report, "w") as f:
                        json.dump(report, f)
                except OSError as e:
                    raise PreflopError(f"cannot write --report: {e}")
            print("\n".join(check_text(report)))
            return 0
        result = solve(a.solver, s, name, flops, a.accuracy, a.time, a.threads, a.save_tree, estimated,
                       outcome_tables=tables)
        try:
            with open(a.args[0], "w") as f:
                json.dump(result, f)
        except OSError as e:
            raise PreflopError(f"cannot write the result: {e}")
        print("\n".join(charts(result, None, a.width, a.ev)))
        if result["save_tree_error"]:
            raise PreflopError(result["save_tree_error"])
        return 0
    except PreflopError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


def _stop(signum, frame):
    raise KeyboardInterrupt  # so the solver process is taken down on the way out


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _stop)
    sys.exit(main())
