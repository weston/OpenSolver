"""Runout lines (Beta view): how one hand plays the rest of the pot, on every runout.

The solver's `show_hand_lines` extension walks the tree below a node once and prints every line the
hand takes with its weight (src/upi_nodes.cpp). This module turns those rows into what the page
draws: per runout, the hand's own lines ("b b xc": bet, bet, check-call) and how often it plays each.

A line is the hand's own actions, street by street. The other player's actions only decide which of
them it gets to take (a check-call needs a bet) and how often each runout is seen at all, so two
paths that differ only in what the other player did (called or folded to the last bet, say) are the
same line. Each street is a string of action letters: x check, b bet, c call, f fold, r raise; with
`sizes`, a bet carries its size in % of the pot ("b33") and an all-in is "ba" / "ra". Streets are
joined by spaces, and a street where the hand had nothing to do is "-".

Frequencies are among the times the cards are actually dealt: a river's lines are those of the hands
that see that river, a turn's are those that see that turn (whatever happens later).
"""

import re

RANKS = "AKQJT98765432"
SUITS = "shdc"
CLASS_RE = re.compile(r"[AKQJT2-9]{2}[so]?")
COMBO_RE = re.compile(r"(?:[AKQJT2-9][shdc]){2}")
BET_RE = re.compile(r"b(\d+(?:\.\d+)?)")
FLOOR = 0.005  # a line under 0.5% of a runout is not worth a row (it is in "other")


def is_card(tok):
    return len(tok) == 2 and tok[0] in RANKS and tok[1] in SUITS


def summarize(text, start, hero, sizes=False, allin=None):
    """show_hand_lines rows -> {cards: {"base", "total", "lines": {label: weight}, "go": suffix}}.

    start: the node the rows hang from: {"actor": 0/1 (None at a deal), "commit": [oop, ip],
    "dead": chips}. hero: 0 (OOP) or 1 (IP). allin: [oop, ip] chips in the pot when all-in, to name
    all-in bets when `sizes` is on. cards: the tuple of cards dealt below the node (() for none).
    go: the heaviest way into the last card (a node-id suffix ending in it), for "open in Study".
    """
    levels = {}
    # state: (actor, oop chips, ip chips, facing a bet, finished streets, this street, cards, go)
    first = (start["actor"], float(start["commit"][0]), float(start["commit"][1]),
             start["commit"][0] != start["commit"][1], (), "", (), "")
    dead = float(start.get("dead") or 0)
    stack, prev = [first], []
    for row in text.split("\n"):
        parts = row.split(" ")
        if len(parts) != 3 or parts[0] == "combos:":
            continue
        toks = parts[0].split(":")
        k, m = 0, min(len(prev), len(toks))
        while k < m and prev[k] == toks[k]:
            k += 1
        del stack[k + 1:]
        st = stack[k]
        for i in range(k, len(toks)):
            st = _step(st, toks[i], toks, i, hero, sizes, dead, allin)
            stack.append(st)
        prev = toks
        weight, base = float(parts[1]), float(parts[2])
        cards = st[6]
        lv = levels.get(cards)
        if lv is None:
            lv = levels[cards] = {"base": base, "total": 0.0, "lines": {}, "ways": {}}
        label = " ".join((s or "-") for s in st[4] + (st[5],))
        lv["total"] += weight
        lv["lines"][label] = lv["lines"].get(label, 0.0) + weight
        if cards:
            lv["ways"][st[7]] = lv["ways"].get(st[7], 0.0) + weight
    for lv in levels.values():
        ways = lv.pop("ways")
        lv["go"] = max(ways, key=ways.get) if ways else ""
    return levels


def _step(st, tok, toks, i, hero, sizes, dead, allin):
    actor, c0, c1, facing, done, cur, cards, go = st
    if tok in ("~", "."):
        return st
    if is_card(tok):  # a new street: OOP first
        return (0, c0, c1, False, done + (cur,), "", cards + (tok,), ":".join(toks[:i + 1]))
    mine = actor == hero
    if tok == "f":
        return (1 - actor, c0, c1, facing, done, cur + "f" if mine else cur, cards, go)
    if tok == "c":
        if facing:
            top = max(c0, c1)
            return (1 - actor, top, top, False, done, cur + "c" if mine else cur, cards, go)
        return (1 - actor, c0, c1, False, done, cur + "x" if mine else cur, cards, go)
    m = BET_RE.fullmatch(tok)
    if not m:
        raise ValueError(f"unexpected action {tok!r} in a line")
    to = float(m.group(1))
    if mine:
        a = "r" if facing else "b"
        if sizes:
            if allin is not None and to >= allin[actor] - 1e-6:
                a += "a"
            elif not facing:
                pot = dead + c0 + c1
                a += str(int(round(100 * (to - (c0, c1)[actor]) / pot))) if pot > 0 else ""
        cur += a
    if actor == 0:
        c0 = to
    else:
        c1 = to
    return (1 - actor, c0, c1, True, done, cur, cards, go)


def card_order(card):
    return (RANKS.index(card[0]), SUITS.index(card[1]))


def to_payload(levels):
    """summarize() levels -> the nested, JSON-ready report: a line table and, per level, its lines."""
    table, index = [], {}

    def cell(cards):
        lv = levels[cards]
        total = lv["total"]
        rows = []
        for label, w in sorted(lv["lines"].items(), key=lambda kv: -kv[1]):
            f = w / total if total > 0 else 0.0
            if f < FLOOR and rows:
                break
            if label not in index:
                index[label] = len(table)
                table.append(label)
            rows.append([index[label], round(f, 4)])
        out = {"reach": round(total / lv["base"], 4) if lv["base"] > 0 else 0.0, "lines": rows}
        if cards:
            out["card"] = cards[-1]
            out["go"] = lv["go"]
        kids = sorted((c for c in levels if len(c) == len(cards) + 1 and c[:len(cards)] == cards),
                      key=lambda c: card_order(c[-1]))
        if kids:
            out["cards"] = [cell(c) for c in kids]
        return out

    if () not in levels:
        return {"lines": [], "now": None}
    return {"now": cell(()), "lines": table}


def expand_hand(vsrv, order, hand, board):
    """"JJ" / "AKs" / "AhKd" / "AhKd,AsKs" -> the combos meant (hand-order spelling), off the board."""
    hand = hand.strip()
    blocked = {board[i:i + 2] for i in range(0, len(board), 2)}
    key = lambda c: frozenset((c[:2], c[2:]))  # noqa: E731
    by_cards = {key(c): c for c in order}
    if CLASS_RE.fullmatch(hand) and not COMBO_RE.fullmatch(hand):
        a, b = hand[0], hand[1]
        if a != b and len(hand) == 2:
            raise vsrv.SolverError("suited or offsuit? (AKs, AKo)")
        if a == b and len(hand) == 3:
            raise vsrv.SolverError("a pair is just two ranks (JJ)")
        if a != b and RANKS.index(a) > RANKS.index(b):
            hand = b + a + hand[2:]
        combos = [c for c in order if vsrv.combo_class(c) == hand]
    else:
        combos = []
        for part in hand.split(","):
            part = part.strip()
            if not COMBO_RE.fullmatch(part) or key(part) not in by_cards:
                raise vsrv.SolverError("pick a hand like JJ, AKs or AhKd")
            if by_cards[key(part)] not in combos:
                combos.append(by_cards[key(part)])
    return [c for c in combos if c[:2] not in blocked and c[2:] not in blocked]


def hand_lines_at(vsrv, solver, node_id, player, hand, sizes=False):
    """GET /api/hand_lines: the report for `hand` of `player` from node `node_id` on."""
    with solver.cmd_lock:
        node = vsrv.parse_node_block(solver.cmd("show_node " + node_id))
        if node["type"] == "ROOT":     # `r` is the same place as its only child, the first decision
            node_id = "r:0"
            node = vsrv.parse_node_block(solver.cmd("show_node " + node_id))
        board = "".join(node["board"].split())
        if len(board) < 6:
            raise vsrv.SolverError("runout lines start on the flop or later")
        if node["type"] == "END_NODE":
            raise vsrv.SolverError("the hand has ended here")
        order = vsrv.hand_order(solver)
        combos = expand_hand(vsrv, order, hand, board)
        reach = vsrv.parse_floats(solver.cmd(f"show_range {player} {node_id}"), vsrv.NUM_COMBOS)
        at = {c: i for i, c in enumerate(order)}
        out = {"id": node_id, "player": player, "hand": hand, "sizes": bool(sizes),
               "board": [board[i:i + 2] for i in range(0, len(board), 2)],
               "actor": {"OOP_DEC": "OOP", "IP_DEC": "IP"}.get(node["type"]),
               "combos": [{"combo": c, "reach": round(reach[at[c]], 4)} for c in combos]}
        live = [c for c in combos if reach[at[c]] > 0]
        if not live:
            out.update({"now": None, "lines": []})
            return out
        pot = node["pot"] + [0, 0, 0]
        start = {"actor": {"OOP_DEC": 0, "IP_DEC": 1}.get(node["type"]), "commit": pot[:2], "dead": pot[2]}
        allin = None
        if sizes:
            root = vsrv.parse_node_block(solver.cmd("show_node r:0"))["pot"] + [0, 0]
            stack = vsrv.parse_num(solver.cmd("show_effective_stack").split()[0])
            allin = [root[0] + stack, root[1] + stack]
        text = solver.cmd(f"show_hand_lines {player} {node_id} " + " ".join(live), timeout=1800)
    out.update(to_payload(summarize(text, start, 0 if player == "OOP" else 1, sizes, allin)))
    return out
