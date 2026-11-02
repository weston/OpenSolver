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

## Training harness (stage 2)

`ml/train.py` is the ONLY torch-dependent piece (CPU torch, user-site install;
every other ml/ tool stays stdlib-only so CI and stage-3 need nothing). Two
small MLPs per decision node:

- `value_net(g, hand_f) -> predicted chip EV / start pot`. Target is the
  dump's cfv **divided by the per-hand compat mass** (`calc_ev` semantics —
  the raw dump value is unnormalised and can read ~100x the chip EV) and by
  the start pot. Auxiliary loss weight 0.1.
- `strat_net(g, hand_f, action_f) -> per-(hand, action) logit`; softmax over
  the record's actions (all legal by construction). Loss is reach-weighted KL
  against the average strategy.

Encoding runs on the fly (~2 ms/spot), so epochs stream the corpus straight
from disk with no materialisation. Train/holdout split = spot-key hash mod
100, stable as the corpus grows. Metric printed during training: holdout KL
vs the uniform baseline (target ratio << 1) and value MSE. Checkpoints
(`.pt`) carry the feature-layout constants; the C++ inference PR will add the
portable weight export.

## Weight export (stage 3 input)

`ml/export.py <ckpt.pt> <out.wsnb>` writes `WSNW0001`: magic, G/H/A dims,
pot_scale, hidden width, then value_net and strat_net layer weights as f32
row-major (three Linear layers each, in train order; ReLU after layers 1-2,
linear output). Stdlib-only `--selftest` parses it back; `export.load_net` +
`export.forward` are the **reference forward** the C++ port must match
(verified `max |torch - stdlib| = 3e-6` over random inputs on this machine).

## Solver-side warm start (stage 3)

`warm_start <wsnb> [pseudo_iterations] [regret_beta]` (defaults 300, 0)
loads a `WSNW0001` net and walks a freshly built river tree (`build_tree`
with exactly 5 board cards, no chance nodes below the root, no
locked/eliminated flags), recomputing the normative features of
`ml/features.py` node by node in C++ (`src/nn_warm.cpp`).

At each decision node it evaluates the strat net on every
(hand, action), softmaxes, and writes `reach_p(hand) * sigma(action|hand)`
into the node's strategy-sum rows, then descends with the acting player's
reach replaced by that product row (seats alternate, so reach vectors swap
roles child by child; hand counts may differ between players). It finally sets
`tree.iterations = pseudo_iterations`: `average_strategy` normalises the sums
per hand, so the tree behaves exactly as if K iterations of DCFR had produced
the predicted average strategy — the "begin with a good average, zero regrets"
construction of Brown & Sandholm (AAAI 2016), minus the regret half. K is a
trust knob for later A/B sweeps: DCFR's forgetting factor decays the preload
at `(t/(t+1))^gamma`, so larger K keeps net strategies around longer.

With `regret_beta > 0` the walk additionally writes
`regret_beta * sigma(action|hand)` into the regret rows (regret-matching
then reproduces the net policy as CFR's *current* strategy). beta has the
solver's regret units: it bounds how much counterfactual evidence accrues
before the current strategy moves off the prediction. All rows are positive,
so the DCFR sign-split discounting applies uniformly from t = K+1.

The command refuses non-fresh trees (`iterations != 0`), so warm start never
blends with partially solved state. `tests/test_upi.py` pins the port against
a tiny seeded `ml/tests/fixture.wsnb` (hidden=8): C++ per-node strategies
match the stdlib reference forward to ~5e-8, on the root and a deeper node,
independent of K; with a huge beta the strategy stays pinned to the
prediction across real iterations; quantized (small_strats) regret rows
round-trip through a short solve with rows still normalising.

Results so far (river, held-out spots): the river_v1 net's strategy measures
~5-26% of pot exploitability, while cold DCFR certifies 0.3% of pot within
25-100 iterations everywhere measured (river spots AND turn roots) — the
preload(s) then anchor or wash out around a strategy that is worse than what
a few real iterations already provide, so warm starts lose on this family.
river_v2 (hidden 384, 25 epochs on the 12k corpus, holdout KL ratio 0.507 vs
v1's 0.510) changed nothing at the strategy level: 8-spot paired measurement
v1 ≈ v2 ≈ 13% of pot (4-26% range), and its 24-spot A/B matrix reproduces v1
exactly (cold median 25 iters / 3 ms; best warm arms 2x iterations and ~27x
wall, the constant being the NN walk itself). See the #59 A/B comments; the
gate is net quality + inference speed, not the construction.

## Layout

| path | what |
|---|---|
| `ml/DESIGN.md` | this document |
| `ml/gen_river.py` | sampler + batch solver + manifest (`--fixture` regenerates the test fixture) |
| `ml/loader.py` | stdlib parser for `WSDT0001` (`python3 -I ml/loader.py` self-tests against the fixture) |
| `ml/features.py` | normative feature encoder `WSDT0001` -> model inputs (stage 2; pure stdlib) |
| `ml/train.py` | training harness (stage 2; the only torch-dependent tool) |
| `ml/export.py` | `.pt` -> `WSNW0001` export + stdlib reference forward (stage 3 input) |
| `ml/make_fixture_net.py` | regenerates the tiny seeded test net (stage 3) |
| `ml/tests/fixture.wsdt` | one small solved spot (committed; used by the loader self-test) |
| `ml/tests/fixture.wsnb` | committed test net for the C++ warm_start check |
| `ml/tests/test_features.py` | encoder tests: replay, shapes, canonicalisation invariance |
| `src/nn_warm.{hpp,cpp}` | `WSNW0001` loader + C++ warm-start tree walk (stage 3) |

Data directories live OUTSIDE the repo (default `~/wsolver-data/river_v1`) — the
10k-spot corpus is GBs and regenerable from the manifest + generator.
