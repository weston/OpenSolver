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
