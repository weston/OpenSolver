#!/usr/bin/env python3
"""Turn PioViewer-style bet-size settings into the explicit add_line list the solver needs.

The solver only accepts explicit lines. PioViewer lets you describe a tree with a handful of
percentages instead (bet sizes per street, raise size, all-in rules) and expands them itself.
This module reimplements that expansion, inferred black-box from PioViewer's own output: the
recorded save `tests/data/pio_25bbHU_2sizes.txt` (header plus the 266 lines it generated), and
145 real PioViewer tree configs on the owner's PC, which it reproduces exactly (#313; those
files are not in the repository).

Usage:
    python3 tools/tree_builder.py CONFIG            # print add_line commands
    python3 tools/tree_builder.py CONFIG --full     # print a complete UPI script
    python3 tools/tree_builder.py CONFIG --compare  # diff against the add_lines in CONFIG
    python3 tools/tree_builder.py CONFIG --pio-compat   # legacy donk fallback (below)

CONFIG is any file containing `#Key#Value` header lines; extra content (ranges, add_line
commands already present, build_tree) is ignored, so a PioViewer save file can be fed in
directly and the generated lines diffed against the ones it already carries.

The inferred rules are documented in `README.md` (section "Tree builder") and in
`SIZING_RULES` below.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

OOP, IP = 0, 1
STREETS = ("Flop", "Turn", "River")

SIZING_RULES = """\
Amounts in a line are absolute: the total chips a player has put in since the start of the
hand. A check or a call therefore repeats the amount the player is already matched to.

pot          = dead pot + invested[OOP] + invested[IP]
pot_if_called = dead pot + 2 * max(invested)          (the pot once the pending bet is called)

bet   to  round(invested + BetSize%   * pot)           (DonkBetSize% when OOP leads into
                                                        the previous street's aggressor)
raise to  round(facing   + RaiseSize% * pot_if_called)

Rounding is half to even, on the total (`925 + 33% x 5550 = 2756.5 -> 2756`; #21, #313).

Donk rule (#152): when OOP leads into the previous street's aggressor, only DonkBetSize sizes
are offered - an empty DonkBetSize means OOP cannot lead at all (check only), and AddAllin
adds no all-in lead either. PioViewer does the same: all 145 corpus configs reproduce with
this rule, and only 25 with --pio-compat's BetSize fallback, which is kept for old scripts.

Size lists separate entries with commas, spaces or both, and mix these forms:

  <number>    a percentage of the pot (bets) or of pot_if_called (raises), as above.
  Ne          geometric over N streets, counting this one: the pot fraction f that, bet
              and called on each of the N streets, puts the remaining effective stack S
              in exactly, with P the current pot:

                  f = ((1 + 2*S/P)^(1/N) - 1) / 2

              For a raise, P is pot_if_called and S the stack left after calling, and the
              raise total is facing + f * P - i.e. the raise increment is a geometric bet
              on the pot you would make by calling; a `1e` raise is all-in. `e` alone means
              geometric over the streets remaining (3 on the flop, 2 on the turn; on the
              river it is the shove).
  NeP, eP     Ne, but at most P% of the pot (`2e75`: 2e or 75%, whichever is smaller).
  Nx          raise by N times the increment faced: mine + N * (facing - mine). Facing a
              bet that is N times the bet; facing 96 -> 240, `2.5x` is 456. Raise lists only.
  a           the all-in. (A malformed entry ending in `a`, such as `2e.a`, is also read
              as the all-in, as PioViewer does.)

  e.g. `33, 75, 2e, 2e50, 3x, a`.

Applied in this order at each decision:
  1. the sizes above (at a raise cap, only the `a` entries);
  2. min-raise: a raise is at least facing + (facing - mine);
  3. CapPerStreet's all-in (below);
  4. AllinThreshold%: a total strictly above AllinThreshold% of the effective stack becomes
     the all-in (exactly 67% of 9000 = 6030 stays);
  5. <Street>Config[IP].AddAllin: adds the all-in to a non-empty size list when the stack
     left once the pending bet is called is below AddAllinOnlyIfLessThanThisTimesThePot% of
     pot_if_called;
  6. MergeSimilarBets: from the largest size down, a size is dropped when the gap to the
     last size kept is under MergeSimilarBetsThreshold% of the pot once the smaller size is
     put in (pot + size - invested).

Other settings:
  * UseUnifiedRaiseAfterRaise#True with UnifiedRaiseAfterRaise: once anyone has raised (a bet
    over a bet) anywhere earlier in the hand, raises use these sizes instead of RaiseSize.
  * CapEnabled#True with CapPerStreet#F\\nT\\nR: on that street the N-th bet or raise can
    only be the all-in, and it is offered even if no size asks for it; 0 means uncapped.
  * <Street>Config[IP].RaiseCap#N (ours): N bets and raises in total, counting both players
    (the smaller of the two players' values); after that only a configured all-in (an `a`
    entry, or AddAllin whose gate passes) is still offered.
  * ForceOOPBet / ForceIPBet: that player has no check on the flop.
  * RemovedLines_2 / ExtraLines_2: PioViewer's hand edits, action paths such as
    `Check, Bet 2e50, Raise 50` joined by a literal `\\\\0`. A removed path drops the action it
    ends on (and its subtree); an extra path adds its last action, sized like a config entry
    (`Bet 2479c` = 2479 chips).
"""


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _pct(pct: Decimal, pot: int) -> int:
    return _round(pct * Decimal(pot) / Decimal(100))


@dataclass(frozen=True)
class SizeSpec:
    """One entry of a size list. kind:
    pct    value% of the pot (bet) or of pot_if_called (raise)
    geo    geometric over `value` streets (None: the streets remaining), counting this one
    mult   raise to `value` times the bet faced (raise lists only)
    allin  the all-in
    """
    kind: str
    value: Decimal | None = None
    param: Decimal | None = None  # geo only: the P of `NeP`
    # the entry as written (`2e50`, `75`; `allin` for `a`): PioViewer's hand edits name sizes this way
    text: str = field(default="", compare=False)


def parse_size_list(value: str) -> list[SizeSpec]:
    """Parse a comma-separated size list like `33, 75, 2e, e, 3x, a`.

    Raises ValueError on malformed entries.
    """
    # PioViewer separates entries with commas, spaces or both (`5e 3e 2e a`, `33,50, 75, a`).
    return [parse_size(part) for part in re.split(r"[,\s]+", value) if part]


def parse_size(part: str, chips: bool = False) -> SizeSpec:
    """One size-list entry. `chips` also accepts `Nc` (a bet of N chips), which only PioViewer's hand
    edits use (`Bet 2479c`). Raises ValueError on a malformed entry."""
    token = part.lower()
    m = re.fullmatch(r"(\d*)e(\d+(?:\.\d+)?)", token)
    if token in ("a", "allin"):
        return SizeSpec("allin", text="allin")
    if m:
        # `NeP`: geometric over N streets, at most P% of the pot (see SIZING_RULES).
        if m.group(1) and int(m.group(1)) < 1:
            raise ValueError(f"bad geometric size {part!r} (want N >= 1)")
        return SizeSpec("geo", Decimal(m.group(1)) if m.group(1) else None, Decimal(m.group(2)), text=token)
    if token.endswith("e"):
        n = token[:-1]
        try:
            n = int(n) if n else None
        except ValueError:
            raise ValueError(f"bad geometric size {part!r} (want Ne, N >= 1)")
        if n is not None and n < 1:
            raise ValueError(f"bad geometric size {part!r} (want N >= 1)")
        return SizeSpec("geo", None if n is None else Decimal(n), text=token)
    if token.endswith("x"):
        try:
            mult = Decimal(token[:-1])
        except InvalidOperation:
            raise ValueError(f"bad raise multiple {part!r} (want Nx)")
        if mult <= 0:
            raise ValueError(f"bad raise multiple {part!r} (want N > 0)")
        return SizeSpec("mult", mult, text=token)
    if chips and re.fullmatch(r"\d+c", token):
        return SizeSpec("chips", Decimal(token[:-1]), text=token)
    try:
        pct = Decimal(token)
    except InvalidOperation:
        if re.fullmatch(r"[0-9e.]+a", token):
            # A malformed entry ending in `a` is the all-in to PioViewer: one corpus config's `15,2e.a`
            # builds exactly as `15,a` (#313).
            return SizeSpec("allin", text="allin")
        raise ValueError(f"bad size {part!r} (want a percentage, Ne, Nx or a)")
    if pct < 0 or not pct.is_finite():
        raise ValueError(f"bad size {part!r} (want a percentage, Ne, Nx or a)")
    return SizeSpec("pct", pct, text=token)


def _geo_fraction(stack: int, pot: int, streets: int) -> Decimal | None:
    """f = ((1 + 2*S/P)^(1/streets) - 1) / 2; None when the pot is empty."""
    if pot <= 0:
        return None
    base = Decimal(1) + 2 * Decimal(stack) / Decimal(pot)
    return (base ** (Decimal(1) / streets) - 1) / 2


@dataclass
class StreetConfig:
    bet: list[SizeSpec] = field(default_factory=list)
    donk: list[SizeSpec] = field(default_factory=list)
    raise_: list[SizeSpec] = field(default_factory=list)
    add_allin: bool = False
    # Max bets+raises on this street (counting both players); None = uncapped.
    raise_cap: int | None = None
    # PioViewer's CapPerStreet: at the cap the all-in is offered even if no size asks for it
    cap_allin: bool = False


@dataclass
class Config:
    pot: int = 0
    stack: int = 0
    allin_threshold: Decimal = Decimal(100)
    add_allin_max_pot_mult: Decimal = Decimal("inf")
    # MergeSimilarBets: percentage points of the pot within which sizes merge (None: off)
    merge_threshold: Decimal | None = None
    # ForceOOPBet / ForceIPBet: [OOP, IP] may not check on the flop
    force_bet: list[bool] = field(default_factory=lambda: [False, False])
    # UseUnifiedRaiseAfterRaise / UnifiedRaiseAfterRaise: sizes for raising a raise (None: off)
    unified_raise_after_raise: list[SizeSpec] | None = None
    # PioViewer hand edits: action-label paths from the root (`("check", "bet 2e50", "raise 50")`)
    removed_lines: list[tuple[str, ...]] = field(default_factory=list)
    extra_lines: list[tuple[str, ...]] = field(default_factory=list)
    # streets[street][player]
    streets: list[list[StreetConfig]] = field(default_factory=list)
    board: str = ""
    ranges: dict[str, str] = field(default_factory=dict)
    raw_set_range: dict[str, str] = field(default_factory=dict)
    isomorphism: str = "1 0"
    # Legacy: an empty DonkBetSize falls back to BetSize for an OOP lead into the previous
    # street's aggressor. The default (False) is the owner's rule (#152), which is also what
    # PioViewer does (#313: 145/145 configs vs 25 with the fallback): an empty donk list means
    # OOP cannot lead at all. Set only by the CLI's --pio-compat flag; the viewer never sets it.
    pio_compat: bool = False

    def street(self, street: int, player: int) -> StreetConfig:
        return self.streets[street][player]


def _sizes(value: str, field_name: str) -> list[SizeSpec]:
    specs = parse_size_list(value)
    if field_name in ("BetSize", "DonkBetSize"):
        for spec in specs:
            if spec.kind == "mult":
                raise ValueError(f"{field_name} does not take raise multiples (Nx)")
    return specs


def parse_config(text: str) -> Config:
    """Read the `#Key#Value` header of a PioViewer save file."""
    cfg = Config(streets=[[StreetConfig(), StreetConfig()] for _ in STREETS])
    merge_on, merge_threshold = False, Decimal(12)  # 12: the threshold most PioViewer configs carry
    cap_on, caps = False, []
    unified_on, unified = False, None
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
        elif key.startswith(("RemovedLines", "ExtraLines")):
            # `Check, Bet 2e50, Raise 50\\0Check, Check, Bet 75` - paths joined by a literal `\\0`
            paths = [tuple(a.strip().lower() for a in p.split(",") if a.strip())
                     for p in re.split(r"\\+0", value)]
            dest = cfg.removed_lines if key.startswith("Removed") else cfg.extra_lines
            dest.extend(p for p in paths if p)
        elif key in ("ForceOOPBet", "ForceIPBet"):
            cfg.force_bet[OOP if key == "ForceOOPBet" else IP] = value.strip().lower() == "true"
        elif key == "UseUnifiedRaiseAfterRaise":
            unified_on = value.strip().lower() == "true"
        elif key == "UnifiedRaiseAfterRaise":
            unified = _sizes(value, "RaiseSize")
        elif key == "CapEnabled":
            cap_on = value.strip().lower() == "true"
        elif key == "CapPerStreet":
            # PioViewer writes the three street caps with a literal backslash-n between them: `4\n4\n3`
            caps = [int(x) for x in re.split(r"(?:\\n|\s|,)+", value.strip()) if x]
        elif key == "MergeSimilarBets":
            merge_on = value.strip().lower() == "true"
            cfg.merge_threshold = (merge_threshold if merge_on else None)
        elif key == "MergeSimilarBetsThreshold":
            merge_threshold = Decimal(value)
            if merge_on:
                cfg.merge_threshold = merge_threshold
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
                sc.bet = _sizes(value, field_name)
            elif field_name == "DonkBetSize":
                sc.donk = _sizes(value, field_name)
            elif field_name == "RaiseSize":
                sc.raise_ = _sizes(value, field_name)
            elif field_name == "AddAllin":
                sc.add_allin = value.strip().lower() == "true"
            elif field_name == "RaiseCap":
                try:
                    sc.raise_cap = int(value)
                    if sc.raise_cap < 0:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"bad RaiseCap {value!r} (want a non-negative integer)")
    if unified_on and unified is not None:
        cfg.unified_raise_after_raise = unified
    if cap_on:
        # CapPerStreet N: the N-th bet of a street can only be the all-in (#313: with a flop cap of 4,
        # bet / raise / 3-bet leave the all-in alone), i.e. RaiseCap N - 1 in this module's terms.
        for street, cap in enumerate(caps[:len(STREETS)]):
            if cap <= 0:
                continue  # 0: that street is uncapped (a corpus config with `0\n4\n3` bets freely on the flop)
            cap -= 1
            for sc in cfg.streets[street]:
                sc.raise_cap = cap if sc.raise_cap is None else min(sc.raise_cap, cap)
                sc.cap_allin = True  # PioViewer offers the all-in at its cap even without an `a` size
    return cfg


@dataclass(frozen=True)
class Node:
    street: int
    inv: tuple[int, int]
    to_act: int
    acted: bool          # has the player to act already acted on this street?
    # the player who made the last aggressive action on the previous street, or None
    prev_aggressor: int | None
    # bets + raises so far on this street (both players), for RaiseCap
    aggr: int = 0
    # PioViewer hand edits still in play below this node: the remaining action labels of each
    # RemovedLines / ExtraLines path whose start matched the way here (see _children)
    removed: tuple[tuple[str, ...], ...] = ()
    extra: tuple[tuple[str, ...], ...] = ()
    # has anyone raised (bet over a bet) earlier in the hand, on any street (UnifiedRaiseAfterRaise)
    raised: bool = False


def _street_cap(cfg: Config, street: int) -> int | None:
    """The street's raise cap: the smaller of the two players' RaiseCap values, if any."""
    caps = [sc.raise_cap for sc in cfg.streets[street] if sc.raise_cap is not None]
    return min(caps) if caps else None


def _size_total(spec: SizeSpec, *, geo_streets: int, mine: int, facing: int,
                pot: int, pot_if_called: int, allin: int) -> int | None:
    """The not-yet-clamped total a size entry asks for (None: not computable here)."""
    # The total is rounded, not the increment (#313: 925 + 33% x 5550 = 2756.5 -> 2756).
    if spec.kind == "pct":
        base = pot_if_called if facing > mine else pot
        return _round((facing if facing > mine else mine) + spec.value * Decimal(base) / 100)
    if spec.kind == "geo":
        n = int(spec.value) if spec.value is not None else geo_streets
        if facing > mine:
            # The raise increment is a geometric bet on the pot you make by calling;
            # S is what remains after calling.
            f = _geo_fraction(allin - facing, pot_if_called, n)
            base, ref = facing, pot_if_called
        else:
            f = _geo_fraction(allin - max(mine, facing), pot, n)
            base, ref = mine, pot
        if f is None:
            return None
        if spec.param is not None:
            # `NeP`: the geometric size, but at most P% of the pot (#313: `2e75` on a 1750 pot with
            # 9200 behind is 75%, not 2e's 120%; `2e50` with 2e at 57% is 50%).
            f = min(f, spec.param / 100)
        return _round(base + f * ref)
    if spec.kind == "mult":
        if facing <= mine:
            return None  # rejected at parse time; defensive
        # N times the increment faced, on top of what I have in (#313: facing 96 -> 240, PioViewer's
        # 2.5x is 96 + 2.5 x 144 = 456, not 2.5 x 240)
        return _round(mine + spec.value * (facing - mine))
    if spec.kind == "allin":
        return allin
    if spec.kind == "chips":
        return _round(max(mine, facing) + spec.value)  # a hand edit's `Nc`: N chips on top
    raise ValueError(f"unknown size kind {spec.kind!r}")


def _aggressive_totals(cfg: Config, node: Node) -> list[int]:
    """Every bet/raise total available at `node`, smallest first, de-duplicated."""
    return [t for t, _ in _aggressive_options(cfg, node)]


def _aggressive_options(cfg: Config, node: Node) -> list[tuple[int, frozenset[str]]]:
    """Like _aggressive_totals, with the size entries behind each total (`allin` for the all-in, and
    the entries of sizes merged into it): PioViewer's hand edits name actions by these labels."""
    me, opp = node.to_act, 1 - node.to_act
    mine, facing = node.inv[me], node.inv[opp]
    dead = cfg.pot
    pot = dead + mine + facing
    pot_if_called = dead + 2 * max(mine, facing)
    sc = cfg.street(node.street, me)
    allin = cfg.stack

    no_donk_lead = False
    if facing > mine and cfg.unified_raise_after_raise is not None and (node.aggr >= 2 or node.raised):
        # UseUnifiedRaiseAfterRaise: once anyone has raised in the hand, these sizes replace RaiseSize
        # (#313: 75% facing 250 -> 1000 on a 500 pot re-raises to 1000 + 75% x 2500 = 2875; after a turn
        # raise, a river raise is the unified 75% only)
        specs = cfg.unified_raise_after_raise
    elif facing > mine:
        specs = sc.raise_
    else:
        # Leading into the previous street's aggressor is a donk bet. Under the owner's
        # rule (#152), which PioViewer follows too (#313), an empty OOP DonkBetSize means OOP
        # cannot lead at all: no fallback to BetSize, and AddAllin below adds no all-in lead
        # either. (The legacy --pio-compat CLI flag / cfg.pio_compat falls back to BetSize.)
        # IP keeps the old behavior: structurally the same spot - leading into OOP after
        # OOP was the previous aggressor, which postflop only happens after OOP checked -
        # is not a donk bet, and PioViewer gives IP its BetSize there (the recorded
        # fixture's check-bet lines depend on it).
        donk = node.prev_aggressor is not None and node.prev_aggressor != me
        strict = donk and me == OOP and not cfg.pio_compat
        specs = sc.donk if donk and (sc.donk or strict) else sc.bet

        # OOP leading with no donk sizes configured: not only are no donk bets offered,
        # the AddAllin gate below must not add an all-in lead either (explicit
        # decision, #152). A non-empty donk list enables it as usual.
        no_donk_lead = strict and not sc.donk

    cap = _street_cap(cfg, node.street)
    capped = cap is not None and node.aggr >= cap

    opts: list[tuple[int, str]] = []  # (total, label)
    for spec in specs:
        if capped and spec.kind != "allin":
            continue  # at the cap only a configured all-in survives (plus call/fold)
        total = _size_total(spec, geo_streets=len(STREETS) - node.street, mine=mine,
                            facing=facing, pot=pot, pot_if_called=pot_if_called,
                            allin=allin)
        if total is not None:
            opts.append((total, spec.text))

    # A raise is at least a min-raise: the previous increment again (#313: facing 2500 from 0, a 2e
    # raise of 4571 becomes 5000; facing 776 -> 3702, a 2e of 5869 becomes 6628, then the all-in).
    if facing > mine:
        opts = [(max(t, 2 * facing - mine), lab) for t, lab in opts]

    if capped and sc.cap_allin:
        opts.append((allin, "allin"))

    # A bet/raise that gets past the threshold is replaced by the all-in. Strictly past: a 5e river
    # bet of exactly 6030 = 67% of 9000 stays (#313).
    threshold = cfg.allin_threshold * Decimal(allin) / Decimal(100)
    opts = [(allin if Decimal(t) > threshold else min(t, allin), lab) for t, lab in opts]

    if sc.add_allin and not no_donk_lead and specs:  # AddAllin adds to sizes; it doesn't create a bet
        # The gate compares the stack left once the pending bet is called (#313; the 145-config
        # corpus): facing a 1155 river bet with 9200 behind, 8045 < 200% x 4060 adds the all-in.
        limit = cfg.add_allin_max_pot_mult * Decimal(pot_if_called) / Decimal(100)
        if Decimal(allin - max(mine, facing)) < limit:
            opts.append((allin, "allin"))

    # Must actually be a raise over what we are facing, and we must have chips behind.
    labels: dict[int, set[str]] = {}
    for t, lab in opts:
        if t > facing and t > mine:
            labels.setdefault(t, set()).add(lab)
    labels.get(allin, set()).add("allin")
    out = sorted(labels.items())
    if cfg.merge_threshold is not None:
        out = _merge_similar(out, cfg.merge_threshold, pot, mine)
    return [(t, frozenset(lab)) for t, lab in out]


def _merge_similar(opts: list[tuple[int, set[str]]], threshold: Decimal, pot: int,
                   mine: int) -> list[tuple[int, set[str]]]:
    """MergeSimilarBets, inferred from 145 PioViewer configs (#313). From the largest size down, a size
    is dropped when the gap to the last size kept is under `threshold` percent of the pot once the
    smaller size is put in (pot + t - mine). Over the corpus's 9640 adjacent size pairs this never
    merges a pair PioViewer keeps. E.g. 4e/3e/2e at 2539/2961/3920 on a 7386 pot: 2961 merges into
    3920 (959 / 8880 = 10.8%), then 2539 stays (1381 / 8457 = 16.3%). A dropped size's labels go to
    the size it merged into."""
    kept: list[tuple[int, set[str]]] = []
    for t, lab in reversed(opts):  # largest first
        if kept and Decimal(kept[-1][0] - t) * 100 < threshold * (pot + t - mine):
            kept[-1][1].update(lab)
            continue
        kept.append((t, set(lab)))
    return sorted(kept)


def _children(cfg: Config, node: Node) -> list[tuple[int, Node | None, bool]]:
    """(amount emitted, next node, is this a bet/raise) for check/call and every bet/raise.

    `None` as the next node means the hand is over (showdown or an all-in that was called),
    so the line stops there.
    """
    me, opp = node.to_act, 1 - node.to_act
    mine, facing = node.inv[me], node.inv[opp]
    out: list[tuple[int, Node | None, bool, frozenset[str]]] = []

    # Check or call. Folding needs no line: the tree builder adds it implicitly.
    matched = max(mine, facing)
    inv = [node.inv[0], node.inv[1]]
    inv[me] = matched
    passive = frozenset({"call" if facing > mine else "check"})
    if node.street == 0 and facing == mine and cfg.force_bet[me] and _aggressive_totals(cfg, node):
        # ForceOOPBet / ForceIPBet: on the flop that player has no check (#313: the corpus's 9 such
        # configs differ from the plain build only at OOP's first decision and IP's after a check).
        pass
    elif facing > mine or node.acted:
        # bet called, or check behind: the street is complete
        if matched >= cfg.stack or node.street == len(STREETS) - 1:
            out.append((matched, None, False, passive))
        else:
            out.append((matched, Node(node.street + 1, (inv[0], inv[1]), OOP, False,
                                      opp if facing > mine else None, raised=node.raised), False, passive))
    else:
        out.append((matched, Node(node.street, (inv[0], inv[1]), opp, True,
                                  node.prev_aggressor, node.aggr, raised=node.raised), False, passive))

    verb = "raise " if facing > mine else "bet "
    options = _aggressive_options(cfg, node)
    for extra in {p[0] for p in node.extra if p[0].startswith(verb)}:
        # ExtraLines: an action PioViewer's user added by hand, sized like a config entry
        if not any(extra in {verb + lab for lab in labs} for _, labs in options):
            spec = parse_size(extra[len(verb):], chips=True)
            total = _size_total(spec, geo_streets=len(STREETS) - node.street, mine=mine, facing=facing,
                                pot=cfg.pot + mine + facing, pot_if_called=cfg.pot + 2 * matched,
                                allin=cfg.stack)
            if total is not None and total > matched:
                options.append((min(total, cfg.stack), frozenset({spec.text})))
    for total, labs in sorted(options):
        inv = [node.inv[0], node.inv[1]]
        inv[me] = total
        out.append((total, Node(node.street, (inv[0], inv[1]), opp, True,
                                node.prev_aggressor, node.aggr + 1, raised=node.raised or facing > mine), True,
                    frozenset(verb + lab for lab in labs)))

    if not node.removed and not node.extra:
        return [(a, n, g) for a, n, g, _ in out]
    # RemovedLines: drop the action a path ends on; pass the rest of each path down to the child
    kept: list[tuple[int, Node | None, bool]] = []
    for amount, child, aggressive, labs in out:
        if any(len(p) == 1 and p[0] in labs for p in node.removed):
            continue
        if child is not None:
            child = dataclasses.replace(child, removed=tuple(p[1:] for p in node.removed if len(p) > 1 and p[0] in labs),
                                        extra=tuple(p[1:] for p in node.extra if len(p) > 1 and p[0] in labs))
        kept.append((amount, child, aggressive))
    return kept


# Upper bound on the actions build_lines walks (#354): the largest real PioViewer config here walks a few
# thousand; a runaway one (two sizes everywhere, 1000:1 stack-to-pot) would otherwise run for minutes.
MAX_TREE_ACTIONS = 2_000_000


def build_lines(cfg: Config, start: Node | None = None) -> list[list[int]]:
    """Every maximal line of the tree described by `cfg`.

    A line only has to reach the deepest bet of its branch: the solver fills in check, call
    and fold at every node it creates. So every path that ends in a bet or raise is a
    candidate, and the ones that are prefixes of a longer candidate get dropped. (With an
    all-in threshold in play the deepest action of a branch is always the all-in, but a
    config without one can bottom out on an ordinary raise, and that line is still needed.)

    `start` overrides the root node, for trees whose root sits mid-hand (the viewer's
    subgame re-solve, viewer/resolve.py): it carries the street index, the chips each
    player has already invested (so a root that starts facing a bet has unequal
    investments), the player to act, and whether that player is closing the action
    (`acted`). The default is the usual fresh flop root.
    """
    candidates: list[tuple[int, ...]] = []

    visits = [0]

    def walk(node: Node, history: list[int]) -> None:
        for amount, nxt, aggressive in _children(cfg, node):
            visits[0] += 1
            if visits[0] > MAX_TREE_ACTIONS:
                raise ValueError(f"tree too large: over {MAX_TREE_ACTIONS} actions (fewer sizes, or a "
                                 "lower all-in threshold)")
            history.append(amount)
            if aggressive:
                candidates.append(tuple(history))
            if nxt is not None:
                walk(nxt, history)
            history.pop()

    walk(start if start is not None else Node(0, (0, 0), OOP, False, None,
                                              removed=tuple(cfg.removed_lines), extra=tuple(cfg.extra_lines)), [])

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
    ap.add_argument("--pio-compat", action="store_true",
                    help="legacy: an empty DonkBetSize falls back to BetSize (default, as "
                         "in PioViewer: an empty donk list means OOP cannot lead)")
    args = ap.parse_args(argv)

    if args.rules:
        sys.stdout.write(SIZING_RULES)
        return 0
    if not args.config:
        ap.error("a config file is required")

    with open(args.config, encoding="utf-8", errors="replace") as fh:  # PioViewer saves can carry non-ASCII
        text = fh.read()
    cfg = parse_config(text)
    cfg.pio_compat = args.pio_compat
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
