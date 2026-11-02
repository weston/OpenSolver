#!/usr/bin/env python3
"""Tests for ml/features.py. Stdlib only; runs in CI: python3 -I ml/tests/test_features.py"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import features
from loader import COMBOS, load_spot

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture.wsdt")


RANKS = "23456789TJQKA"
SUITS = "cdhs"


def combo_index(hi: int, lo: int) -> int:
    return hi * (hi - 1) // 2 + lo


def permuted_spot(spot, perm):
    """Copy with board + combos remapped by a suit permutation."""
    s = copy.deepcopy(spot)

    def pc(c):
        return (c & ~3) | perm[c & 3]

    def card_of(cs):  # "Qh" -> internal index
        return RANKS.index(cs[0]) * 4 + SUITS.index(cs[1])

    def str_of(ci):
        return RANKS[ci >> 2] + SUITS[ci & 3]

    s.board = [str_of(pc(card_of(c))) for c in s.board]
    for p in range(2):
        s.combos[p] = [
            combo_index(*sorted((pc(COMBOS[ci][0]), pc(COMBOS[ci][1])), reverse=True))
            for ci in s.combos[p]
        ]
    return s


class TestReplay(unittest.TestCase):
    def test_root(self):
        pot, i0, i1, hist = features.replay("r:0", 40.0)
        self.assertEqual((pot, i0, i1, hist), (40.0, 0.0, 0.0, []))

    def test_check_then_bet(self):
        pot, i0, i1, hist = features.replay("r:0:c:b200", 40.0)
        self.assertEqual(pot, 240.0)
        self.assertEqual(hist, [(0, "c", 0.0), (1, "b", 200.0)])

    def test_bet_call_closes(self):
        pot, i0, i1, hist = features.replay("r:0:b50:c:b75", 40.0)
        # OOP bets 50, IP calls (street closes), token after the call is not replayed
        self.assertEqual(pot, 140.0)
        self.assertEqual(hist, [(0, "b", 50.0), (1, "c", 0.0)])

    def test_raise(self):
        pot, i0, i1, _ = features.replay("r:0:b50:b150", 40.0)
        self.assertEqual(pot, 240.0)

    def test_bad_token(self):
        with self.assertRaises(ValueError):
            features.replay("r:0:c:9h", 40.0)


class TestEncodeFixture(unittest.TestCase):
    def setUp(self):
        self.spot = load_spot(FIXTURE, strict=True)
        self.rows = features.encode_spot(self.spot)

    def test_shapes_and_ranges(self):
        self.assertEqual(len(self.rows), len(self.spot.records))
        for row, rec in zip(self.rows, self.spot.records):
            self.assertEqual(len(row["g"]), features.G_DIM)
            self.assertEqual(len(row["hs"]), len(self.spot.combos[rec.player]))
            self.assertTrue(all(len(h) == features.H_DIM for h in row["hs"]))
            self.assertEqual(len(row["as_"]), len(rec.actions))
            self.assertTrue(all(len(a) == features.A_DIM for a in row["as_"]))
            self.assertTrue(all(0.0 <= x <= 12.0 for x in row["g"]))
            # action one-hots are exactly one-hot
            for a in row["as_"]:
                self.assertEqual(sum(a[:3]), 1.0)
            # strategy rows and targets line up
            self.assertEqual(len(row["strat"]), len(rec.actions))

    def test_root_has_start_pot(self):
        root = next(r for r in self.rows if r["path"] == "r:0")
        import math
        o = features._BOARD_DIM + features._MONEY_DIM + features._STREET_DIM
        pot_now = math.expm1(root["g"][o]) * features.POT_SCALE
        self.assertAlmostEqual(pot_now, self.spot.start_pot, places=4)

    def test_board_and_money_invariance(self):
        # Board/money/street features are invariant under ALL suit permutations,
        # because canonical_perm minimises the permuted board *multiset*.
        prefix = features._BOARD_DIM + features._MONEY_DIM + features._STREET_DIM
        for perm in features.SUIT_PERMS:
            rows2 = features.encode_spot(permuted_spot(self.spot, perm))
            for r1, r2 in zip(self.rows, rows2):
                self.assertEqual(r1["g"][:prefix], r2["g"][:prefix])

    def _minimiser_count(self, board):
        cards = [RANKS.index(c[0]) * 4 + SUITS.index(c[1]) for c in board]

        def key(p):
            return tuple(sorted(features._perm_card(c, p) for c in cards))

        best = min(key(p) for p in features.SUIT_PERMS)
        return sum(1 for p in features.SUIT_PERMS if key(p) == best)

    def test_hand_invariance_when_tiebreak_unique(self):
        # Hole-card canonicalisation is invariant whenever canonical_perm has a
        # UNIQUE minimiser (a suit unused by the board leaves ties no rule can
        # resolve). The fixture board (4 clubs + Th) is tied, so build a spot
        # from it with a board touching 3 suits + pair coverage above.
        spot = copy.deepcopy(self.spot)
        spot.board = ["Ac", "Qd", "7c", "3c", "Th"]
        self.assertEqual(self._minimiser_count(spot.board), 1)
        rows1 = features.encode_spot(spot)
        for perm in features.SUIT_PERMS:
            rows2 = features.encode_spot(permuted_spot(spot, perm))
            for r1, r2 in zip(rows1, rows2):
                self.assertEqual(r1["g"], r2["g"])
                self.assertEqual(r1["hs"], r2["hs"])
                self.assertEqual(r1["as_"], r2["as_"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
