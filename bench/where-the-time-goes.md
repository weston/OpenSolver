# Where the time goes

A measured map of the solver's cost, so optimization effort goes where there is something to get.
Every number here is from this repo's own benchmark suite or a profile of it on one machine
(M1 Ultra, 20 cores); absolute values are machine-relative, the ratios are not.

Measure with wall time or `make bench`. Not `solver_time` — see the warning in AGENTS.md.

## Self time by function, 1 thread

| tree | showdown | fold | walk_t | strategy (cur+avg) | compat_mass |
|---|---|---|---|---|---|
| turn | 48.8% | 0.0% | 25.5% | 10.8% | 13.0% |
| flop_srp_2 | 58.9% | 0.0% | 21.0% | 10.9% | 7.7% |
| flop_srp_4 | 58.8% | 0.0% | 21.6% | 10.7% | 8.2% |
| deep | 64.5% | 0.0% | 19.2% | 10.2% | 4.4% |
| flop_3bet | 64.8% | 0.0% | 18.5% | 10.0% | 0.5% |
| preflop | 98.2% | 0.0% | 0.9% | 0.4% | 0.4% |

`fold_value` is 0.0% everywhere. It is not a target, despite being a natural-sounding one.

## The dominant cost, characterised

`showdown_board`, on `flop_srp_2` at 0.126 s/iteration single-threaded:

```
54,730 calls/iteration        754 entries per call
41,265,447 entry-visits/iteration
1.36 us per call              1.80 ns per entry-visit  (~5.8 cycles at 3.2 GHz)
556M entry-visits/s
```

This path is close to its CPU ceiling, and the reasons are all measured rather than assumed:

- **It cannot be vectorized on ARM.** All four of its loops gather from `cs[52]` through a
  data-dependent card index, or scatter into `cs[]`/`out[]`. AArch64 NEON has no gather or scatter
  instruction. Of 123 loops in `solver.cpp`, clang already vectorizes 48 at width 4 — the ones that
  can be. (#54)
- **On x86, gathers do not help; AVX-512 permutes help a little.** GCC 13 vectorizes none of the
  four loops at `-march=x86-64-v3` or `-march=native` (Zen 5). Measured by hand on a Ryzen 9 9950X,
  1 thread: anything built on hardware gathers is slower than scalar (`cs[]` lookups by gather
  +40%, entry fields by dword gather +8%), so AVX2, whose only tool here is the gather, has nothing
  to offer. What does pay is AVX-512 with VBMI: masked 16-lane steps (a strength group averages ~6.5
  hands), `cs[]` held in 4 zmm registers and read with `vpermt2ps`, entries unpacked with
  `vpermt2b`. That kernel (`showdown_board_avx512`) is 1-4% faster single-threaded and bit-identical;
  it is compiled only when the target has AVX-512F/BW/VBMI. (#89)
- **Restructuring to remove the gathers makes it worse.** Precomputing `pair_cs[combo]` costs 1326
  entries per strength-group boundary against the `2 x np` gathers it saves; accumulating blocked
  mass per hero turns 2 scatter-adds per opponent entry into ~102. The gathers are the price of the
  O(np + no) single-pass formulation. (#57)
- **Per-call overhead is ~0.7%**, so batching boards buys under 1% on CPU. Batching only matters as
  a route to wide execution. (#92)
- **The strength orderings are already shared** — 799 `ShowdownBoard`s serve 25,906 terminal nodes
  on a flop tree, 48 serve 773 on a turn. Nothing left to amortise there. (#57)
- **5.8 cycles per entry-visit** for a gather, two adds and a store is already tight.

## What is actually left, in order of size

1. **Thread scaling (#53) — worth ~2-3x, the largest available win.** Measured 6-8x on 20 cores,
   and a net *loss* on the smallest trees (the preflop tree is slower on 20 threads than on 1).
   Platform-independent and costs no bit-stability.
2. **x86 gather (#89) — unknown, needs x86 hardware.** AVX2 `vgatherdps` / AVX-512 gather might
   vectorize the two `cs[]` lookup loops that NEON cannot. AVX2 gathers are microcoded and often no
   better than scalar, so this is empirical.
3. **GPU (#87) — no-go for now.** Unified memory removes the usual blocker on Apple Silicon and the
   parallelism is there, but arithmetic intensity is ~3 flops per 3 memory touches, each board
   carries a serial scan, bit-stability is lost, and it means two permanent backends. Revisit once
   (1) is done.
4. **Convergence — already taken.** γ 2→3 gave -11% wall time and 455 iterations against 500
   (#52, `dcfr_sweep.md`). γ=5 is faster still in total but regresses a low-SPR 3-bet pot at tight
   targets, so it is available via `set_dcfr` rather than default.

## Check the defaults before optimising the code

Every performance win found so far came from a feature that was **switched off, mis-defaulted, or
measured under stale conditions** - not from making a hot loop faster. That is worth treating as the
first place to look.

| change | win | why it was hidden |
|---|---|---|
| #111 | -42% preflop at 20 threads | inner `parallel_for` dispatch too fine to amortise; work is bounded by `NUM_COMBOS^2` |
| #137 | -26% on turn trees | `iso_turn` off to match Pio's `set_isomorphism 1 0` |
| #131 | -8.5% `flop_srp_4` | `prune_period` 32 activated pruning at t=64; trees reach target by t=45-100, so it never ran |
| #134 | -9 to -14% available | `set_avg_delay 32`; #80's sweep predated the alpha/gamma retune and the pruning fix |
| #86 | -13% `deep` | (not hidden - confirmed independently at 180 iterations) |

Two lessons that generalise:

- **A feature with an activation precondition must state it where a benchmarker will trip over it.**
  Pruning's `t > 2 * prune_period` rule is in a comment, which is the only reason I noticed my first
  two measurements of it had run below the threshold and were measuring it switched off.
- **Re-sweep a knob after anything else changes.** `avg_delay` was swept and rejected in #80, then
  found worth 9-14% once alpha/gamma (#72) and pruning (#131) had moved where the time goes.

Defaults checked and found correct: `small_strats auto` (picks float32 here, avoiding the 14% that
forcing 16-bit costs), `alpha 1.3 / gamma 4` (survives a joint sweep plus a tight-target check),
`prune_period 8` after #131. `recalc_accuracy` / `always_recalc` are stored-only Pio stubs and do
nothing.

## Things measured and ruled out

Recorded so they are not re-attempted:

- **Reach-based subtree pruning** never fires. At ε=1e-3, a million times looser than useful,
  exploitability stayed bit-identical — proof that nothing was being skipped. Mixed strategies keep
  reach strictly positive, and folds are terminal nodes rather than subtrees. (#51)
- **Interleaving the `win`/`tie` preflop tables** targets a path that is not hot: the plain preflop
  tree solves in 0.4 s, and `preflop_full`'s 66 s is dominated by its attached postflop subtrees.
  A tree's *share* of runtime is not its *amount*. (#57, #82)
- **Forcing the one cost-model-blocked loop** in `average_strategy` works and is bit-identical, and
  gives +0.31% against sd 0.33% — one sigma, indistinguishable from zero. (#54)
- **Forcing the two `cs[]` gather loops** in `showdown_board` (post-#94 they are cost-model declined
  rather than dependence-blocked) vectorises them at width 4 and is **1.0% slower**, sd 0.09%. The
  cost model is right: a synthesised width-4 gather costs more than the scalar loop. (#89)
- **Sampling the strategy average** (accumulate every k-th iteration) instead of truncating a prefix
  is catastrophic: k=8 needs 970 iterations against 75. The sum is weighted per iteration by
  `(t/(t+1))^gamma`, so dropping iterations from the middle destroys the weighting. **The average
  tolerates truncation but not decimation.** (#135)
- **An adaptive `min(avg_delay, t/4)`** is a no-op: it compares `t` against a fraction of itself, so
  it is true for every `t >= 1` and never skips anything. (#135)
