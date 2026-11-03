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
| #137 | -26% on turn trees | `iso_turn` off to match Pio's `set_isomorphism 1 0` (changes strategies by up to 0.008, so not free) |
| #131 | -8.5% `flop_srp_4` | `prune_period` 32 activated pruning at t=64; trees reach target by t=45-100, so it never ran |
| #134 | -9 to -14% available | `set_avg_delay 32`; #80's sweep predated the alpha/gamma retune and the pruning fix |
| #86 | -13% `deep` | (not hidden - confirmed independently at 180 iterations) |

**One counterexample, and it is instructive.** #164 vectorised the regret-update max reductions for
a bit-identical 4.6-5.9% on arm64 (11-14% claimed on x86). That is a code win, not a default. It does
not contradict the gather result above: those loops are *gather-bound* and forcing them is slower,
whereas this one is a **contiguous reduction** that was blocked by float `max` semantics. The trick
generalises - non-negative floats order identically to their IEEE-754 bit patterns read as unsigned
integers, so `max(|x|)` becomes an integer max, and compilers vectorise integer max reductions
**without** `-ffast-math` where they refuse to for float max because of NaN. Worth looking for other
float reductions in hot paths that could be re-expressed this way.

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

- **Reach-based subtree pruning** never fires (**subtree level only** - per-*entry* skipping is a real 4.3% win, see #151 below). At ε=1e-3, a million times looser than useful,
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

On the realistic trees (`srp_big`, `threebet_mono`; owner's Zen 5 PC, 32 threads, srp_big at
16-bit storage), measured after #164-#168 (2026-11-03):

- **The AVX-512 showdown kernel's group loop is not latency- or query-bound.** On srp_big it is 63% of
  thread time; its group loop is 62% of the kernel (~63 strength groups per board, 6-7 of p's hands
  each). Three restructurings were bit-identical or exact and gave nothing:
  - one 16-lane query per card-sum state, serving group g's `le` and group g+1's `lt` at once (half
    the lookups): +1-2%;
  - two interleaved `CsRegs` accumulators to halve the add chain: within noise;
  - a nonzero-reach bitmask and visiting only set bits: +10% at 1 thread, +2% at 32. 75% of opponent
    entries are exactly zero at srp_big showdowns (67% already at the river chance node), but a gather
    pre-pass plus data-dependent trip counts cost more than the skipped adds. The old per-group
    trip count is fixed per board, so the predictor learns it.
  - compacting the opponent side once per river subtree (zero-reach entries dropped, group ends
    remapped): -1 to -3% on srp_big, and +10% on the scalar kernel (Mac), which already skips zeros
    with a branch. Not worth its code.
  The kernel's opponent pass appears throughput-bound on the 4 masked adds plus mask setup per entry.
- **The scalar showdown kernel is slower than AVX-512 on wide ranges too** (srp_big iterations 21-60:
  16.3 s vs 15.2 s), so AVX-512 is not losing to per-group vector overhead there.
- **Four interleaved partial sums in the fold kernels' card-mass loop** (to break the `total` and
  `cs[]` dependency chains): no change at 1 thread. The chains were not the bound; the later win was
  removing the fold pass altogether (#166, #168).
- **Regret matching (`current_strategy`) as a portable blocked loop** (16 hands at a time, all actions,
  one pass): +13% at 1 thread. GCC handles the short variable-length inner loops badly. Explicit
  AVX-512 registers do win (#175), but only with a software prefetch per row; without it, interleaved
  row reads cost +8-10% on DRAM-streaming trees at 32 threads.
- **Fusing the 16-bit decode into `current_strategy`'s first pass** (no scratch copy): within noise.
- **Memory traffic was not the limit at 16-bit, and now partly is.** Before #185/#189, turning strategy-sum
  accumulation off entirely (no S traffic, about 40% of the bytes) did not make srp_big iterations
  faster. Re-measured after #189 (2026-11-03, srp_big, iterations 11-30): no S updates make an
  iteration 16% faster at 32 threads (0.333 -> 0.281 s) but only 7% at 8 threads (0.554 -> 0.517 s),
  and scaling is 1.87x from 4 to 8 threads, 1.45x from 8 to 16 and 1.16x from 16 to 32 (1.013 / 0.543 /
  0.374 / 0.323 s). With the compute cheaper, the 13 GB tree now leans on DRAM bandwidth at high thread
  counts. float32 is bandwidth-bound above 8 threads (16 threads: 23% slower than 16-bit).
- **Software-prefetching the strategy-sum rows** at node entry, and **flush-to-zero / DAZ**: no change.
  The regret update's slowness before #164 was its scalar max reductions, not cache misses or
  denormals.
- **Lazy per-row discounting of pruned rows**: only 0.9% of rows are RBP-pruned on threebet_mono;
  pruning pays by skipping subtrees, so there is nothing to save per row.
- **Re-tuning the RBP period** on threebet_mono: 8 (the default) 3.88 s; 4 4.12 s; 16 3.92 s;
  32 4.52 s; pruning off 4.11 s.
- **Predictive CFR+ on a realistic tree** is still far worse: threebet_mono at 260 iterations, 0.76
  vs 0.28 chips (#80 measured the same on the bench trees).
- **Average delay 0 / 64 / 100 on srp_big** (vs 32): within 2-5% over iterations 125-200, not worth
  another default change.
- **Thread idle time is small at 32 threads**: about 1 s per worker in cv_wait (including the build) and 2 s
  of yields in total, against ~760 s of thread work. The gap to linear scaling (16 threads: 75% efficiency)
  is per-thread slowdown (SMT, bandwidth for float32), not scheduling.
- **Updating the strategy sums every k-th iteration** (2026-11-03), folding the skipped iterations'
  discounts into the next update so the weights stay right, to cut the S traffic above. After the
  avg_delay reset (k = 2): about the same curves on flop_srp_2 and flop_3bet, worse on turn and
  river, and srp_big needed 158 iterations instead of 123 (53.2 s vs 43.1 s); threebet_mono unchanged
  (170 vs 168 iterations). Before the reset (k = 4, iterations 1-32 only, so every later iteration
  stays bit-identical): the early exploitability reports get 2-3x worse (flop_3bet at iteration 30:
  3.89 vs 1.35), because with gamma 4 the average is dominated by the last few iterates. Same
  lesson as the sampled average in #135.
- **Deferring the decay of unreached rows** (nodes the opponent does not reach: regrets only decay,
  the strategy sum still takes the traverser's reach): `strategy_only` is 9-17% of the
  regret-update elements (threebet_mono 9%, flop_srp_4 17%) and still has to read the regrets and
  update S, so only the regret write could be deferred. Not pursued.
- **Transparent huge pages** (Linux build in WSL2 on the same PC, THP `always` vs `PR_SET_THP_DISABLE`):
  srp_big 20 iterations at 32 threads take 6.89 / 7.15 s with THP and 7.02 / 6.84 s without, so TLB
  misses are not a factor. (That GCC 13 build is ~7% slower than the mingw `-march=znver5` exe.)
- **Profile after #189** (threebet_mono, 1 thread, 30 iterations): the two-phase river walk is 93% of
  an iteration; inside it the batched showdown kernel is 47%, regret updates 17%, `current_strategy`
  12%, and the rest of the river walk (reach products, fold rows, job setup, the value sums on the
  way up) about 18%.
- **After #209** (showdown jobs as SIMD lanes; the PC, 2026-11-03). Profile, threebet_mono, 1 thread:
  an iteration is 21% cheaper than after #189 (the batched kernel's cycles per river batch 6,650 ->
  3,730); the kernel is now 32% of an iteration, regret updates 21%, `current_strategy` 14%, the
  river walk's value sums 3.7%, the traverser's node setup 3.0%, reach products ~3%, and allocation,
  recursion and job setup ~10%. Things tried on top of it, none kept:
  - **Decoding 16-bit rows inside the regret update** (one pass instead of decode-R, decode-S, update):
    bit-identical, but threebet_mono 3.20 vs 3.20 s at 32 threads, srp_big 42.8 vs 42.8 s, 1 thread
    6.150 vs 6.128 s. The decode passes are short vectorized loops, and the row update itself is the cost.
  - **Thread pinning** (worker i on logical CPU i+1): now 10% *slower* on threebet_mono at 32 threads
    (2.60 -> 2.86 s); it was -6% before #189.
  - **Prefetching the traverser's strategy-sum rows** on the way down the two-phase river walk (the
    update reads them on the way up, a whole subtree later): threebet_mono 2.644 / 2.614 (T0) / 2.589 s
    (T1), srp_big 39.6 vs 39.3 s: within noise.
  - **Terminal payoffs from a per-action-node table** instead of `terminal_payoff` per showdown job:
    no gain (within noise; if anything slower).
  - **Exploitability checks** cost 1.2 iterations each on threebet_mono (19.6 vs 16.2 ms at 32 threads)
    and 1.0 on srp_big (0.31 s); `set_info_freq auto` runs 10 and 8 of them, 6-8% of a solve. The
    check walk (`walk_evbr`) still evaluates showdowns one terminal at a time; giving it the
    two-phase river walk and the lanes kernel would save an estimated 1-2%, not worth a third copy of
    the river semantics. Separately, the auto schedule's check at iteration 33 lands right after the
    avg_delay reset (threebet_mono: 25.1 -> 40.4 chips), so it is uninformative.
- **Pio's per-iteration convergence matches ours** on threebet_mono (see bench/vs_pio.md), so no
  algorithmic gap was found there; the lead is per-iteration speed.

**The granularity rule.** Subtree-level reach pruning never fires because a node almost always has
*someone* in it; per-entry skipping pays because 55-60% of individual opponent reach entries are
exactly zero at showdown terminals (#151, verified at +4.3% bit-identical on `deep`). And skipping
**exact zeros** is safe because it changes nothing, whereas skipping **small** contributions is not:
per-hand regret pruning on the traverser's side took `deep` from 180 to 425 iterations (#151), and
sampling the strategy average took `flop_srp_2` from 75 to 970 (#135). Cheapening a per-iteration
update by dropping contributions costs more in convergence than it saves in work.
