"""Parser for the WSDT0001 training-dump format produced by `dump_train` (#59).

Pure stdlib. See ml/DESIGN.md for the byte-level spec. Usage:

    from loader import load_spot
    spot = load_spot("ml/tests/fixture.wsdt", strict=True)

Self-test (parses the committed fixture and checks invariants):

    python3 -I ml/loader.py
"""
from __future__ import annotations

import gzip
import os
import struct
from dataclasses import dataclass, field

MAGIC = b"WSDT0001"

# PioSOLVER card/combo encoding (src/cards.cpp): card = rank * 4 + suit with
# suits "cdhs"; combos enumerate (hi, lo) for hi in 1..51, lo in 0..hi-1,
# so combo 0 is "2d2c" and combo 1325 is "AsAh".
RANKS = "23456789TJQKA"
SUITS = "cdhs"
NUM_CARDS = 52
COMBOS: list[tuple[int, int]] = [(hi, lo) for hi in range(1, NUM_CARDS) for lo in range(hi)]
assert len(COMBOS) == 1326


def card_str(c: int) -> str:
    return RANKS[c >> 2] + SUITS[c & 3]


def combo_str(ci: int) -> str:
    hi, lo = COMBOS[ci]
    return card_str(hi) + card_str(lo)


class _Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise ValueError("truncated dump")
        b = self.buf[self.pos : self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def f32s(self, n: int) -> list[float]:
        return list(struct.unpack(f"<{n}f", self.take(4 * n)))

    def text(self) -> str:
        return self.take(self.u16()).decode("utf-8")


@dataclass
class NodeRecord:
    path: str            # display-frame node id, e.g. "r:0:c:b50"
    player: int          # 0 = OOP, 1 = IP
    actions: list[str]
    strat: list[list[float]]   # [A][H_player] average strategy
    reach_player: list[float]  # [H_player] reach under the average strategy
    reach_opp: list[float]     # [H_opp]
    ev_player: list[float]     # [H_player] per-hand subtree EV (unnormalised cfv)
    ev_opp: list[float]        # [H_opp]


@dataclass
class Spot:
    board: list[str]           # display cards, e.g. ["Kh", "Qs", "7d", "4c", "2s"]
    start_pot: float
    eff_stack: float
    iterations: int
    exploitability: float      # root best-response exploitability at dump time
    root_matchups: float       # normaliser: sum over matchups of both ranges' reach
    combos: list[list[int]]    # [player][H] display-frame Pio combo indices
    hand_weights: list[list[float]]  # [player][H] range weights
    records: list[NodeRecord] = field(default_factory=list)

    def combos_str(self, player: int) -> list[str]:
        return [combo_str(ci) for ci in self.combos[player]]


def load_spot(path: str, strict: bool = False) -> Spot:
    with open(path, "rb") as fh:
        buf = fh.read()
    r = _Reader(gzip.decompress(buf) if path.endswith(".gz") else buf)
    if r.take(8) != MAGIC:
        raise ValueError(f"{path}: bad magic (not a WSDT0001 dump)")
    board_len = r.u32()
    spot = Spot(
        board=[card_str(r.u8()) for _ in range(board_len)],
        start_pot=0.0, eff_stack=0.0, iterations=0, exploitability=0.0,
        root_matchups=0.0, combos=[[], []], hand_weights=[[], []],
    )
    (spot.start_pot,) = r.f32s(1)
    (spot.eff_stack,) = r.f32s(1)
    spot.iterations = r.u32()
    (spot.exploitability,) = r.f32s(1)
    (spot.root_matchups,) = r.f32s(1)
    for p in range(2):
        h = r.u16()
        spot.combos[p] = [r.u16() for _ in range(h)]
        spot.hand_weights[p] = r.f32s(h)
    nrec = r.u32()
    for _ in range(nrec):
        player = r.u8()
        n_act = r.u8()
        path = r.text()
        actions = [r.text() for _ in range(n_act)]
        hp, ho = len(spot.combos[player]), len(spot.combos[1 - player])
        flat = r.f32s(n_act * hp)
        rec = NodeRecord(
            path=path,
            player=player,
            actions=actions,
            strat=[flat[a * hp : (a + 1) * hp] for a in range(n_act)],
            reach_player=r.f32s(hp),
            reach_opp=r.f32s(ho),
            ev_player=r.f32s(hp),
            ev_opp=r.f32s(ho),
        )
        spot.records.append(rec)
    if r.pos != len(r.buf):
        raise ValueError(f"{path}: trailing bytes ({len(r.buf) - r.pos})")
    if strict:
        _check(spot)
    return spot


def _check(spot: Spot, tol: float = 2e-3) -> None:
    board = set()
    for c in spot.board:
        board.add(RANKS.index(c[0]) * 4 + SUITS.index(c[1]))
    for p in range(2):
        for ci in spot.combos[p]:
            hi, lo = COMBOS[ci]
            if hi in board or lo in board:
                raise ValueError(f"combo {combo_str(ci)} blocked by board")
    for rec in spot.records:
        for h in range(len(spot.combos[rec.player])):
            row = [rec.strat[a][h] for a in range(len(rec.actions))]
            if abs(sum(row) - 1.0) > tol:
                raise ValueError(f"{rec.path}: strategy row {h} sums to {sum(row)}")


if __name__ == "__main__":
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixture.wsdt")
    if os.path.exists(fixture):
        s = load_spot(fixture, strict=True)
        print(f"fixture ok: board={' '.join(s.board)} pot={s.start_pot} "
              f"H0={len(s.combos[0])} H1={len(s.combos[1])} nodes={len(s.records)} "
              f"iters={s.iterations} expl={s.exploitability:.4f}")
    else:
        print(f"no fixture at {fixture} (generate with ml/gen_river.py --fixture)")
