"""Heads-up preflop trees from the Build tab (#492).

The form's fields go to tools/preflop.py, which checks them, builds the preflop tree and writes the
solver's script: the tool is imported here, so the viewer builds the tree the command line builds.

    GET  /api/preflop/defaults    the form's defaults (the tool's), the flop subsets, this machine's memory
    POST /api/preflop/describe    form -> the preflop tree, a node a row (no solver involved)
    POST /api/preflop/estimate    form -> megabytes, from a solver process of its own
    POST /api/preflop/build       form -> the tree, built in the viewer's solver

A form is `{stack, ante, limp, open, iso, threebet, limp_raise, fourbet, fivebet, allin_threshold,
depth, flop_bet, turn_bet, river_bet, raise, donk, raise_cap, postflop_allin_threshold,
postflop_allin_pot, range_sb, range_bb, flops}`: numbers in big blinds, preflop sizes as the tool
reads them (`2.5`, `4x`, `allin`), postflop sizes as the Build form reads them, `flops` one of the
shipped counts or `all`. A field left out has the tool's default.
"""
import collections
import hashlib
import importlib.util
import math
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

CHIPS = 100            # chips in a big blind: the tool's default, and what its flop files were made for
DEFAULT_FLOPS = "25"   # the form's first choice: a build of seconds; the tool's own default takes the machine's half

_tool = None
_tool_lock = threading.Lock()


def tool():
    """tools/preflop.py, imported from the repo (no package layout to rely on)."""
    global _tool
    with _tool_lock:
        if _tool is None:
            spec = importlib.util.spec_from_file_location("preflop_tool", os.path.join(REPO, "tools", "preflop.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules["preflop_tool"] = mod  # dataclasses look modules up here
            spec.loader.exec_module(mod)
            _tool = mod
    return _tool


# form field -> (the tool's argument, the option name in the tool's messages, what the form calls it)
TEXT_FIELDS = {
    "stack": ("stack", "stack", "Stack"),
    "ante": ("ante", "ante", "Ante"),
    "open": ("open", "open", "Open"),
    "iso": ("iso", "iso", "Raise over a limp"),
    "threebet": ("threebet", "3bet", "3-bet"),
    "limp_raise": ("limp_raise", "limp-raise", "Limp-raise"),
    "fourbet": ("fourbet", "4bet", "4-bet"),
    "fivebet": ("fivebet", "5bet", "5-bet"),
    "allin_threshold": ("allin_threshold", "allin-threshold", "All-in threshold"),
    "postflop_allin_threshold": ("postflop_allin_threshold", "postflop-allin-threshold", "Postflop all-in threshold"),
    "postflop_allin_pot": ("postflop_allin_pot", "postflop-allin-pot", "Add all-in below"),
}
# postflop sizes: form field -> (the tool's argument, raise multiples allowed, what the form calls it)
SIZE_FIELDS = {
    "flop_bet": ("flop_bet", False, "Flop bet"),
    "turn_bet": ("turn_bet", False, "Turn bet"),
    "river_bet": ("river_bet", False, "River bet"),
    "raise": ("raise_", True, "Raise"),
    "donk": ("donk", False, "Donk"),
}
OPTION_NAMES = {opt: label for _, opt, label in TEXT_FIELDS.values()}
OPTION_NAMES["chips-per-bb"] = "chips per big blind"
MAX_TEXT = 200000  # a range of 1326 weighted combos is under 20,000 characters


def _reword(vsrv, e):
    """The tool's message, with its command-line options named as the form names them."""
    text = re.sub(r"--([a-z0-9]+(?:-[a-z0-9]+)*)",
                  lambda m: OPTION_NAMES.get(m.group(1), m.group(1).replace("-", " ")), str(e))
    return vsrv.SolverError(text[:1].upper() + text[1:])


def _text(vsrv, b, key, label):
    v = b[key]
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise vsrv.SolverError(f"{label}: text or a number is expected")
    v = str(v).strip()
    # plain ASCII without control characters: all a range or a size is written in, and what goes to the
    # solver's command line as it is
    if len(v) > MAX_TEXT or any(not 32 <= ord(c) < 127 for c in v):
        raise vsrv.SolverError(f"{label}: not usable (a line break or another character that is not plain text, or too long)")
    return v


def defaults():
    pf = tool()
    a = pf.parser().parse_args(["tree"])
    form = {key: getattr(a, attr) or "" for key, (attr, _, _) in TEXT_FIELDS.items()}
    form.update({key: getattr(a, attr) for key, (attr, _, _) in SIZE_FIELDS.items()})
    form.update({"limp": not a.no_limp, "depth": a.depth or pf.Settings().depth, "raise_cap": a.raise_cap,
                 "range_sb": pf.ALL_HANDS, "range_bb": pf.ALL_HANDS, "flops": DEFAULT_FLOPS, "accuracy": pf.SOLVE_ACCURACY})
    shipped = [c for c in pf.SHIPPED if os.path.exists(pf.flop_path(c))]
    if DEFAULT_FLOPS not in map(str, shipped):
        form["flops"] = str(shipped[0]) if shipped else "all"
    return {"form": form, "depths": list(pf.DEPTHS), "flops": [str(c) for c in shipped] + ["all"],
            "chips": CHIPS, "memory_mb": pf.physical_memory_mb()}


def setup(vsrv, b):
    """The form -> (the tool's settings, its preflop tree, the tree's rows, the flops' name, the flops)."""
    pf = tool()
    a = pf.parser().parse_args(["tree"])
    a.chips_per_bb = CHIPS
    for key, (attr, _, label) in TEXT_FIELDS.items():
        if b.get(key) is not None:
            setattr(a, attr, _text(vsrv, b, key, label))
    if b.get("limp_raise") is not None:
        # empty: the 3-bet sizes (the tool's default); `none`: the small blind cannot raise after limping
        a.limp_raise = {"": None, "none": ""}.get(a.limp_raise.lower(), a.limp_raise)
    tb = vsrv.load_tree_builder()
    for key, (attr, mult, label) in SIZE_FIELDS.items():
        if b.get(key) is not None:
            try:
                setattr(a, attr, vsrv._check_sizes(tb, {key: _text(vsrv, b, key, label)}, key, allow_mult=mult))
            except vsrv.SolverError:
                raise vsrv.SolverError(f"{label}: sizes are percents of the pot (33), geometric (2e), all-in (a)"
                                       + (", or a multiple of the bet for a raise (3x)" if mult else ""))
    if b.get("raise_cap") is not None:
        cap = b["raise_cap"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) \
                or cap != int(cap) or not 1 <= cap <= 99:
            raise vsrv.SolverError("Cap: a whole number of bets and raises a street, 1 or more")
        a.raise_cap = int(cap)
    if "limp" in b:
        if not isinstance(b["limp"], bool):
            raise vsrv.SolverError("Limp: true or false is expected")
        a.no_limp = not b["limp"]
    if b.get("depth") is not None:
        if b["depth"] not in pf.DEPTHS:
            raise vsrv.SolverError(f"Betting through: one of {', '.join(pf.DEPTHS)}")
        a.depth = b["depth"]
    for key, label in (("range_sb", "The small blind's range"), ("range_bb", "The big blind's range")):
        if key in b:
            value = _text(vsrv, b, key, label) if b[key] is not None else ""
            if not value:
                raise vsrv.SolverError(f"{label} is empty")
            setattr(a, key, value)
    spec = str(b.get("flops", DEFAULT_FLOPS))
    if spec != "all" and spec not in map(str, pf.SHIPPED):  # never a path: the tool would read that file
        raise vsrv.SolverError(f"Flops: one of {', '.join(map(str, pf.SHIPPED))} or all")
    try:
        s = pf.settings_from(a)
        if s.stack * CHIPS > pf.MAX_CHIPS:
            raise vsrv.SolverError(f"Stack: at most {pf.MAX_CHIPS // CHIPS} big blinds "
                                   f"(the solver counts {CHIPS} chips a big blind, up to {pf.MAX_CHIPS:,})")
        if s.ante * CHIPS > pf.MAX_CHIPS:
            raise vsrv.SolverError(f"Ante: at most {pf.MAX_CHIPS // CHIPS} big blinds")
        root = pf.build_tree(s)
        rows = pf.tree_rows(s, root)  # with every flop exit's postflop lines: a size that cannot be built is refused here
        name, flops = pf.choose_flops(spec)
    except pf.PreflopError as e:
        raise _reword(vsrv, e)
    except ArithmeticError:
        raise vsrv.SolverError("A number in the form is not usable")
    return s, root, rows, name, flops


def describe(vsrv, b):
    s, root, rows, name, flops = setup(vsrv, b)
    return {"rows": rows, "chips": s.chips, "flops": len(flops), "flops_name": name,
            "exits": sum(1 for r in rows if r["kind"] == "flop")}


class Estimates:
    """Megabytes of a preflop tree, asked of a solver process of its own: with a preflop tree set up,
    `estimate_tree` builds the whole tree but its strategies to count it, and in the viewer's solver that
    would stand beside the tree being looked at. Two figures (#504): the smallest tree, and the tree with
    outcome tables at every level, which is the one the solver builds when there is memory free for it
    (`set_outcome_tables auto`, the state a build here runs in). The same process says whether the two ranges can be dealt
    against each other, so a build is refused before the viewer's solver is touched. Of the form's own
    estimates one runs at a time, a newer request ends the older one; the estimate a build waits for is
    not one of those, nothing ends it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = None                      # the tool's Solver of the request being answered
        self.cache = collections.OrderedDict()   # script -> (MB, MB with the tables, the ranges meet)

    def get(self, vsrv, exe, s, flops, root, replace=True):
        """replace: this request takes the place of the form's estimate that is running (False: a
        build's, which runs beside it and is not ended by a later one)."""
        pf = tool()
        lines = pf.script(s, flops, root)
        key = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        with self.lock:
            got = self.cache.get(key)
        if got is None:
            got = self._ask(vsrv, exe, lines, replace)
            with self.lock:
                self.cache[key] = got
                while len(self.cache) > 64:
                    self.cache.popitem(last=False)
        if not got[2]:
            raise vsrv.SolverError(pf.NO_DEAL[:1].upper() + pf.NO_DEAL[1:])
        return got[0], got[1]

    def _ask(self, vsrv, exe, lines, replace):
        pf = tool()
        u = None
        try:
            u = pf.Solver(exe)
            if replace:
                with self.lock:
                    if self.running is not None:
                        self.running.kill()
                    self.running = u
            for line in lines:
                u.need(line)
            mb = pf._estimate(u)
            u.need("set_outcome_tables fast")   # the second count is of what the first one built: no time to speak of
            with_tables = pf._estimate(u)
            order = " ".join(u.cmd("show_hand_order")).split()
            sb, bb = (pf._floats(u.cmd(f"show_range {who} r:0"), vsrv.NUM_COMBOS, "show_range r:0")[0]
                      for who in ("IP", "OOP"))
            meet = len(order) == vsrv.NUM_COMBOS and pf.ranges_meet(order, sb, bb)
        except pf.PreflopError as e:
            with self.lock:
                replaced = replace and u is not None and self.running is not u
            if replaced:
                raise vsrv.SolverError("a newer estimate replaced this one")
            raise _reword(vsrv, e)
        finally:
            with self.lock:
                if self.running is u:
                    self.running = None
            if u is not None:
                u.kill()
        return mb, with_tables, meet


ESTIMATES = Estimates()


def estimate(vsrv, solver, b):
    pf = tool()
    s, root, _, name, flops = setup(vsrv, b)
    mb, with_tables = ESTIMATES.get(vsrv, solver.exe, s, flops, root)
    return {"mb": mb, "mb_tables": with_tables, "memory_mb": pf.physical_memory_mb(),
            "flops": len(flops), "flops_name": name, "depth": s.depth}


def build(vsrv, solver, b):
    """Check everything first, the size included, in other processes: only a tree that can be built
    replaces the one in the viewer's solver."""
    pf = tool()
    s, root, _, name, flops = setup(vsrv, b)
    mb, with_tables = ESTIMATES.get(vsrv, solver.exe, s, flops, root, replace=False)
    memory = pf.physical_memory_mb()
    if memory and mb > memory:
        raise vsrv.SolverError(f"This tree is about {mb:,} MB and this machine has {memory:,} MB: "
                               "take fewer flops, or stop the betting a street earlier")
    # The tool's script expects a solver as it starts. The Build form's postflop setup turns turn-card
    # isomorphism on (no effect on a tree that starts before the flop, but it is not the tool's state),
    # and op_generate undoes what this script switches on (16-bit storage, collapsed check-downs).
    cmds = ["set_isomorphism 1 0"] + pf.script(s, flops, root) + ["add_all_flops"]
    # The form is accepted: from here the tree that is there goes, and what was made for it with it
    # (a re-solve, the fast preview's refined streets), as on /api/generate and /api/build.
    if vsrv._resolver is not None:
        vsrv._resolver.main_tree_changed()  # #357
    if vsrv._refiner is not None:
        vsrv._refiner.invalidate()
    vsrv._preview = None
    with solver.cmd_lock:
        try:
            solver.cmds(["stop", "wait_for_solver"])
        finally:
            solver.drop_holds()   # the solver let go of any hold with the stop (as the server does on /api/build)
        # the old tree's state first: if the build below fails there is no tree, and nothing of the old one
        solver.preflop_touched = True
        solver.tree_present = False
        solver.preflop = None
        solver.forget_locks()
        solver.loaded_file = False
        solver.built_sizes = None
        solver.reset_series()
        solver.clear_partial()
        solver.cmds(cmds, timeout=7200)
        solver.tree_present = True
        solver.preflop = {"chips": s.chips, "flops": len(flops)}
        count = solver.cmd("node_count")
        # what the solver counts for the tree it built: `mb`, or `mb_tables` when the tables were added
        try:
            built = re.search(r"Memory needed: (\d+) MB", str(solver.cmd("estimate_rebuild_tree")))
        except vsrv.SolverError:
            built = None   # the tree is built all the same
    return {"node_count": count, "mb": mb, "mb_tables": with_tables, "built_mb": int(built.group(1)) if built else None,
            "memory_mb": memory, "chips": s.chips, "flops": len(flops), "flops_name": name}


def api(vsrv, solver, method, path, body):
    if path == "/api/preflop/defaults":
        return 200, defaults()
    vsrv.require_post(method)
    b = vsrv.need_body(body)
    if path == "/api/preflop/describe":
        return 200, describe(vsrv, b)
    if path == "/api/preflop/estimate":
        return 200, estimate(vsrv, solver, b)
    if path == "/api/preflop/build":
        return 200, build(vsrv, solver, b)
    return 404, {"error": "unknown endpoint"}
