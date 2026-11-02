# NN warm start (#59): design and dataset format

Plan for the neural-net warm start: solve a large corpus of spots with our own solver,
train a net that predicts near-equilibrium per-hand strategies (and EVs), then let CFR
finish from that start. The final answer stays a real CFR equilibrium at the usual
accuracy targets; the net only removes iterations. This document is the stable reference
for stage 1 (data generation) and the constraints stages 2–3 must design against.

## Stages (maps to the acceptance list in #59)

1. **Data generation (this PR).** `ml/gen_river.py` samples river spots from a
   parametrised family (below), solves each to 0.3% of the pot, and stores per-node
   training records emitted by the solver's new `dump_train <file>` command.
2. **River model.** Per-node network: (public state, both reaches) -> per-hand action
   frequencies + counterfactual values. Suit-isomorphism canonicalisation on inputs.
   Target: warm-started CFR reaches 0.3% of pot in <= half the wall time of cold DCFR
   (inference included) on held-out river spots, at equal final exploitability.
3. **Warm start construction.** Brown & Sandholm, *Strategy-Based Warm Starting for
   Regret Minimization in Games* (AAAI 2016): convert predicted strategies into
   starting regrets + average-strategy weights. Verify on adversarial /
   out-of-distribution spots that CFR still converges to the same exploitability
   (the "never worse" acceptance row).
4. **Turn, then flop:** same machinery, wider trees; revisit data quantities then.

## Spot family v1 (river)

| parameter | distribution |
|---|---|
| board | uniform 5-card subset |
| pot | uniform over {20, 30, 40, 60, 80, 100, 140, 200} chips |
| effective stack | round(pot * U(0.8, 3.0)) |
| ranges | per player, k ~ U{40..250} combos, uniform without replacement over combos not blocked by the board, weight 1.0 |
| lines | 1-2 OOP leads + 1-2 IP probe bets, each `round(pot * f)`, f sampled from {0.25, 0.33, 0.5, 0.67, 0.75, 1, 1.5, 2} and < stack; each bet may be followed by a raise-to of {2, 2.5, 3}x the wager faced if <= stack. Checks/calls/folds and all-ins are implicit in the tree builder |

Known simplifications vs real solver usage (deliberate for v1; each is an easy v2 knob
because the generator is seeded and resumable):

- Ranges are uniform subsets, not strength-shaped. OOD risk is bounded by the
  "never worse" construction (CFR finishes from any start).
- Bet menus are small and river-only; no raises after raises beyond one level.
- ~8% of spots are check-down-only ([[0]] line) when the stack leaves no room for a
  listed bet; kept as legal shallow spots.

Reproducibility: the manifest (`<out>/manifest.jsonl`) records every parameter plus
per-spot solve seconds / final exploitability / convergence flag; the spot key is
sha1 of the exact solve script (dump path excluded), so runs dedupe and resume.

## Dump format `WSDT0001` (little-endian)

`dump_train <file>` writes one binary file per solved tree. All floats are IEEE f32.
Cards are Pio indices (`card = rank * 4 + suit`, suits "cdhs"); combos are Pio 1326
order (`hi` ranges 1..51, `lo` below `hi`, so combo 0 = 2d2c), in the **display**
frame (isomorphism handled solver-side). `ml/loader.py` is the reference parser.

```
header:
  char[8]  magic = "WSDT0001"
  u32      board_len; u8 board[board_len]
  f32      start_pot, eff_stack
  u32      iterations           # CFR iterations when dumped
  f32      exploitability       # root best-response exploitability, chips
  f32      root_matchups        # normaliser: sum of both ranges' compatible reach
  for p in {OOP, IP}:
    u16    H
    u16    combos[H]            # display-frame Pio combo indices of tracked hands
    f32    range_weights[H]
  u32      num_records

record (one per decision node):
  u8       player (0 = OOP)
  u8       num_actions A
  u16+     path (len + bytes), e.g. "r:0:c:b50"
  A x      action label (u16 len + bytes), e.g. "b50", "c", "f"
  f32      strat[A][H_player]   # average strategy at the node
  f32      reach_player[H_player]  # reach under the average strategy
  f32      reach_opp[H_opp]
  f32      ev_player[H_player]  # per-hand subtree cfv under the average strategy
  f32      ev_opp[H_opp]        # (unnormalised: divide by compat mass for Pio-style EV)
```

Chance nodes are not records; their paths appear in decision-node paths. Terminal
nodes carry no strategy. Nodes are in DFS preorder, so a path's parent always appears
earlier in the file.

EV semantics: `ev_p[h]` is player p's counterfactual value of hand h at the node given
both reaches (`Solver::walk(Evaluate)` — average strategy on both sides, matching
`calc_ev`'s numerator before dividing by compatible opponent mass). Cross-checked
against `show_strategy` / `calc_ev` to <= 5e-7 relative.

The files are stored gzipped on disk (`*.wsdt.gz`); `loader.load_spot` accepts both.

## Feature encoding (stage 2)

`ml/features.py` turns `loader.Spot` records into fixed-size float vectors. The
layout is **normative**: the C++ inference side (stage 3) must reproduce it
bit-for-bit, so the encoder is pure stdlib with explicit constants and no RNG.
Model input for scoring one action of one hand at a node is the concatenation
`global_f + hand_f + action_f`.

| vector | dim | contents |
|---|---|---|
| `global_f(node)` | 93 | 52 board one-hot (canonical frame) + 3 log-money + 4 street one-hot + 4 investment + 30 history |
| `hand_f(node, h)` | 36 | 2x (13 rank + 4 suit) canonical one-hots + own reach + opponent compat mass (both mean-normalised) |
| `action_f(action)` | 4 | 3 type one-hot (bet/raise, check/call, fold) + size/pot |

Details (all in the code, kept short here because the code is the spec):

- **Money**: `POT_SCALE = 40` chips; `log1p(x / POT_SCALE)` for pot/stack, and
  `log1p(stack / pot)`. Investments as fractions of the effective stack; amount
  faced over the start pot.
- **History**: up to 6 past actions this street, each (3 type one-hot, size/pot,
  player-relative-to-decider), replayed from the dump path. Pad with zeros.
- **Canonicalisation**: all cards are remapped by the suit permutation that
  minimises the sorted tuple of permuted board cards. Board features are
  invariant under every suit permutation (the minimised multiset is unique);
  hole-card features are invariant whenever the minimiser is unique — boards that
  ignore a suit (e.g. 4 clubs + a heart) leave that suit pair interchangeable,
  an arbitrary-but-deterministic labeling both sides compute identically. Hole
  cards are sorted after canonicalisation, since a permutation can flip the
  (hi, lo) order of a pocket pair.
- **Reaches**: per-hand raw reach / mean reach over the player's tracked hands;
  opponent compatibility mass per hand (`total_opp - card_sum[c1] - card_sum[c2]`,
  the exact `calc_ev` denominator) / mean mass.
- Training targets pass through unchanged: per-hand action frequencies
  (`strat`) and counterfactual values (`ev`).

`python3 -I ml/tests/test_features.py` runs in CI: replay semantics, shapes and
ranges on the fixture, and suit-permutation invariance (24 perms). The encoder
also runs clean over a 100-spot sample of the real corpus (630 nodes, ~30k
hands, ~2 ms/spot).

## Layout

| path | what |
|---|---|
| `ml/DESIGN.md` | this document |
| `ml/gen_river.py` | sampler + batch solver + manifest (`--fixture` regenerates the test fixture) |
| `ml/loader.py` | stdlib parser for `WSDT0001` (`python3 -I ml/loader.py` self-tests against the fixture) |
| `ml/features.py` | normative feature encoder `WSDT0001` -> model inputs (stage 2; pure stdlib) |
| `ml/tests/fixture.wsdt` | one small solved spot (committed; used by the loader self-test) |
| `ml/tests/test_features.py` | encoder tests: replay, shapes, canonicalisation invariance |

Data directories live OUTSIDE the repo (default `~/wsolver-data/river_v1`) — the
10k-spot corpus is GBs and regenerable from the manifest + generator.
