"""Feature encoding for the NN warm start (#59, stage 2).

Turns loader.Spot records into fixed-size float feature vectors. The layout is
normative: the C++ inference side (stage 3) must reproduce it bit-for-bit, so
everything here is pure stdlib with explicit constants (ml/DESIGN.md has the
rationale and the layout table).

Three vector families (dims exported below):

- global_f(node)    G_DIM  — board, pot/stack, investment, action history
- hand_f(node, h)   H_DIM  — canonical ranks/suits, own reach, opp compat mass
- action_f(action)  A_DIM  — type one-hot + size

Model input for scoring one action of one hand at a node is
global_f + hand_f + action_f (concatenation, in that order).

Canonicalization: all cards are remapped by the suit permutation that makes the
sorted board lexicographically smallest, so suit-isomorphic spots produce
identical features.
"""
from __future__ import annotations

import itertools
import math

try:
    from .loader import COMBOS, Spot, NodeRecord, card_str
except ImportError:  # run as a script / -I
    from loader import COMBOS, Spot, NodeRecord, card_str

# ---------------------------------------------------------------------------
# Layout constants (normative)

_BOARD_DIM = 52
_MONEY_DIM = 3          # log1p(pot/40), log1p(stack/40), log1p(stack/pot)
_STREET_DIM = 4         # one-hot [flop, turn, river, reserved]
_INVEST_DIM = 4         # log1p(pot_now/40), inv_p/stack, inv_o/stack, faced/pot
_HIST_MAX = 6
_HIST_DIM = _HIST_MAX * 5   # per past action: type3 + size/pot + player
G_DIM = _BOARD_DIM + _MONEY_DIM + _STREET_DIM + _INVEST_DIM + _HIST_DIM

_CARD_DIM = 13 + 4          # rank one-hot + suit one-hot, per hole card
_REACH_DIM = 2              # own reach, opp compat mass (both mean-normalised)
H_DIM = 2 * _CARD_DIM + _REACH_DIM

A_DIM = 4                   # type3 [bet/raise, check/call, fold] + size/pot

POT_SCALE = 40.0            # chip normalisation constant (typical river pot)

# ---------------------------------------------------------------------------
# Suit canonicalization

SUIT_PERMS = list(itertools.permutations(range(4)))


def _perm_card(c: int, perm: tuple[int, ...]) -> int:
    return (c & ~3) | perm[c & 3]


def canonical_perm(board: list[str]) -> tuple[int, ...]:
    """Suit permutation minimising the sorted tuple of permuted board cards.

    Deterministic (ties broken by SUIT_PERMS order), but not unique when the
    board ignores a suit (e.g. 4 clubs + 1 heart leaves d/s interchangeable);
    then hole-card canonicalisation is an arbitrary-but-fixed labeling. Board
    features are invariant regardless, since the minimised multiset is unique.
    """
    cards = ["23456789TJQKA".index(c[0]) * 4 + "cdhs".index(c[1]) for c in board]
    return min(SUIT_PERMS, key=lambda p: tuple(sorted(_perm_card(c, p) for c in cards)))


# ---------------------------------------------------------------------------
# Path / investment replay


def replay(path: str, start_pot: float) -> tuple[float, float, float, list[tuple]]:
    """Replay a node path on a single street.

    Returns (pot_now, invested_player, invested_opp, history) at the decision
    point, where the player to act is OOP if the number of history actions is
    even, IP otherwise (OOP acts first postflop). history entries are
    (player, kind, chips) with kind in {"b", "c", "f"}.
    """
    toks = path.split(":")[2:]
    inv = [0.0, 0.0]
    hist: list[tuple] = []
    actor = 0
    for t in toks:
        if t.startswith("b") or t.startswith("r"):
            chips = float(t[1:])
            inv[actor] = chips
            hist.append((actor, "b", chips))
            actor ^= 1
        elif t == "c":
            faced = inv[1 - actor] > inv[actor]
            inv[actor] = max(inv[actor], inv[1 - actor])
            hist.append((actor, "c", 0.0))
            if faced:
                break  # call closes the street (on the river: terminal)
            actor ^= 1
        elif t == "f":
            hist.append((actor, "f", 0.0))
            break
        else:
            raise ValueError(f"unrecognised path token {t!r} in {path!r}")
    return start_pot + inv[0] + inv[1], inv[0], inv[1], hist


# ---------------------------------------------------------------------------
# Encoders


def global_f(spot: Spot, rec: NodeRecord) -> list[float]:
    perm = canonical_perm(spot.board)
    out = [0.0] * G_DIM
    for c in spot.board:
        cc = "23456789TJQKA".index(c[0]) * 4 + "cdhs".index(c[1])
        out[_perm_card(cc, perm)] = 1.0
    o = _BOARD_DIM
    pot, stack = spot.start_pot, max(spot.eff_stack, 1e-9)
    out[o] = math.log1p(pot / POT_SCALE)
    out[o + 1] = math.log1p(stack / POT_SCALE)
    out[o + 2] = math.log1p(stack / max(pot, 1e-9))
    o += _MONEY_DIM
    out[o + 2] = 1.0  # river
    o += _STREET_DIM
    pot_now, ip_, io_, hist = replay(rec.path, pot)
    player = rec.player
    inv_p, inv_o = (ip_, io_) if player == 0 else (io_, ip_)
    out[o] = math.log1p(pot_now / POT_SCALE)
    out[o + 1] = inv_p / stack
    out[o + 2] = inv_o / stack
    out[o + 3] = max(0.0, inv_o - inv_p) / max(pot, 1e-9)
    o += _INVEST_DIM
    for i, (pl, kind, chips) in enumerate(hist[:_HIST_MAX]):
        b = o + 5 * i
        out[b + (0 if kind == "b" else 1 if kind == "c" else 2)] = 1.0
        out[b + 3] = chips / max(pot, 1e-9)
        out[b + 4] = float(pl != player)
    return out


def compat_masses(spot: Spot, rec: NodeRecord) -> list[float]:
    """Opponent compatibility mass per hand of the deciding player — the exact
    denominator `calc_ev` divides cfv by (distinct combos share <= 1 card)."""
    opp_combos = spot.combos[1 - rec.player]
    card_sum = [0.0] * 52
    for ci, r in zip(opp_combos, rec.reach_opp):
        hi, lo = COMBOS[ci]
        card_sum[hi] += r
        card_sum[lo] += r
    total_o = sum(rec.reach_opp)
    return [total_o - card_sum[COMBOS[ci][0]] - card_sum[COMBOS[ci][1]]
            for ci in spot.combos[rec.player]]


def hand_fs(spot: Spot, rec: NodeRecord) -> list[list[float]]:
    """One H_DIM vector per tracked hand of the deciding player."""
    perm = canonical_perm(spot.board)
    combos = spot.combos[rec.player]
    reach = rec.reach_player
    masses = compat_masses(spot, rec)
    mean_r = sum(reach) / max(len(reach), 1)
    mean_m = sum(masses) / max(len(masses), 1)
    out = []
    for ci, r, mass in zip(combos, reach, masses):
        hi, lo = COMBOS[ci]
        # Sort AFTER canonicalization: card order within a combo is meaningless,
        # and a suit permutation can flip the (hi, lo) order of a pocket pair.
        c0, c1 = sorted((_perm_card(hi, perm), _perm_card(lo, perm)))
        v = [0.0] * H_DIM
        for k, c in enumerate((c0, c1)):
            v[k * _CARD_DIM + (c >> 2)] = 1.0
            v[k * _CARD_DIM + 13 + (c & 3)] = 1.0
        v[2 * _CARD_DIM] = r / mean_r if mean_r > 0 else 0.0
        v[2 * _CARD_DIM + 1] = mass / mean_m if mean_m > 0 else 0.0
        out.append(v)
    return out


def action_f(label: str, start_pot: float) -> list[float]:
    v = [0.0] * A_DIM
    if label.startswith(("b", "r")):
        v[0] = 1.0
        v[3] = float(label[1:]) / max(start_pot, 1e-9)
    elif label == "c":
        v[1] = 1.0
    else:
        v[2] = 1.0
    return v


def encode_spot(spot: Spot) -> list[dict]:
    """All records of a spot as {path, player, actions, g, hs, as_, strat, ev}."""
    rows = []
    for rec in spot.records:
        rows.append({
            "path": rec.path,
            "player": rec.player,
            "actions": rec.actions,
            "g": global_f(spot, rec),
            "hs": hand_fs(spot, rec),
            "as_": [action_f(a, spot.start_pot) for a in rec.actions],
            "strat": rec.strat,
            "ev": rec.ev_player,
        })
    return rows
