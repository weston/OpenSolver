# Where the time goes

A measured map of the solver's cost, so optimization effort goes where there is something to get.
Every number here is from this repo's own benchmark suite or a profile of it on one machine
(M1 Ultra, 20 cores); absolute values are machine-relative, the ratios are not.

Measure with wall time or `make bench`. Not `solver_time` — see the warning in AGENTS.md.

## Self time by function, 1 thread

Re-sampled after the lanes kernels (#209, #219) and the fused fold (#168). `sample` for 8-10 s at
1 thread, 2.5K-7.4K working samples per tree, waiting threads excluded and frames under 5 samples
truncated by the tool. Pre-#209 figures in brackets for comparison:

| tree | showdown | fold | walk_t | strategy (cur+avg) | compat_mass |
|---|---|---|---|---|---|
| turn | **66.0%** (48.8) | 0.0% | **8.8%** (25.5) | **14.4%** (10.8) | **0.0%** (13.0) |
| flop_srp_2 | **63.3%** (58.9) | 0.0% | **9.9%** (21.0) | **16.7%** (10.9) | **0.3%** (7.7) |
| flop_srp_4 | **65.1%** (58.8) | 0.0% | **10.4%** (21.6) | **14.4%** (10.7) | **0.3%** (8.2) |
| deep | **61.2%** (64.5) | 0.0% | **11.8%** (19.2) | **14.2%** (10.2) | **0.2%** (4.4) |
| flop_3bet | **51.8%** (64.8) | 0.0% | **17.7%** (18.5) | **13.8%** (10.0) | **0.7%** (0.5) |
| preflop | not re-sampled — see below | | | | |

`fold_value` is 0.0% everywhere, still. It is not a target, despite being a natural-sounding one.

The structural change is **`compat_mass` has essentially vanished** — 4.4-13.0% down to 0.0-0.7% —
because #168 folds the opponent's fold mass into the same showdown kernel call rather than a
separate pass. `walk_t` roughly halved on every tree but `flop_3bet`. `strategy` rising is relative:
the same work against a smaller total.

`preflop` cannot be sampled this way: it converges so fast that 20,000 steps complete in **0.03 s**,
so the process exits before `sample` can attach. Its old 98.2% showdown row is left out rather than
carried forward unverified — and per the share-is-not-amount rule below, a tree that solves in 0.03 s
is not a target whatever its profile says.

**One caveat on comparing these to the bracketed numbers.** Pruning is inactive until
`t > 2 * prune_period` = 16, so a long sampling run is mostly pruned iterations while a short one is
not, and that changes the mix as well as the absolute cost. These runs are 400-4,000 iterations; the
original window is unrecorded. Treat large shifts (`compat_mass`, `walk_t`) as real and single-digit
ones as possibly just the window.

## The dominant cost, characterised

`showdown_board`, on `flop_srp_2` single-threaded. Re-measured with a two-point slope (0 vs 40
iterations, best of 3) at **0.0915 s/iteration**, down from 0.126 s when this was first written:

```
54,730 calls/iteration        754 entries per call
41,265,447 entry-visits/iteration
                              1.40 ns per entry-visit  (~4.5 cycles at 3.2 GHz)
713M entry-visits/s
```

(Entry-visits counts every structural entry, so it is unchanged by #151's zero-reach skipping; the
throughput figure is per structural entry, the same basis as the 556M it replaces. The per-entry cost
is 22% better than the 1.80 ns recorded before the lanes kernels.)

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
- **4.5 cycles per entry-visit** for a gather, two adds and a store is already tight (5.8 before the lanes kernels).

## What is actually left, in order of size

1. **Thread scaling (#53) — now ~1.4x left on postflop trees, no longer the largest win.** The
   "6-8x on 20 cores, 2-3x available" figure this list used to carry is **stale**; the work since
   (#144-#146, #161, #111) has largely spent it. Re-measured with a two-point slope (0 vs **40**
   iterations, best of 3, so startup and tree build cancel):

   | tree | 1 thread s/iter | 20 threads s/iter | speedup |
   |---|---|---|---|
   | `deep` | 0.15006 | 0.01044 | **14.38x** |
   | `flop_srp_2` | 0.09168 | 0.00655 | **13.99x** |
   | `flop_3bet` | 0.02865 | 0.00233 | **12.30x** |
   | `turn` | 0.00149 | 0.00072 | 2.05x |

   **State the iteration window when you quote a per-iteration cost.** It is not a constant: pruning
   is inactive until `t > 2 * prune_period` = 16, so a 20-iteration slope is mostly unpruned and a
   40-iteration slope is half pruned. `flop_srp_2` measures 0.110 s/iter over 20 iterations and
   0.0917 over 40 — a 20% spread from the window alone, with nothing else changed. The *speedups*
   are stable (14.1x vs 14.0x) because both sides shift together, which is why ratios survive this
   and absolute numbers do not.

   **Scaling saturates at the performance-core count, not the core count.** Full curve on
   `flop_srp_2`, two-point slope over 40 iterations, best of 3 (this machine is 16 P-cores + 4
   E-cores):

   | threads | s/iter | speedup | efficiency |
   |---|---|---|---|
   | 1 | 0.08679 | 1.00x | 100% |
   | 2 | 0.04731 | 1.83x | 92% |
   | 4 | 0.02453 | 3.54x | 88% |
   | 8 | 0.01272 | 6.82x | 85% |
   | 12 | 0.00882 | 9.85x | 82% |
   | 16 | 0.00697 | 12.45x | 78% |
   | 18 | 0.00696 | 12.47x | 69% |
   | 20 | 0.00696 | 12.47x | 62% |

   16, 18 and 20 threads are **identical to three significant figures**: the four efficiency cores
   contribute nothing at all. So quote scaling against **P-cores**, not logical cores — the real
   figure is 12.45x on 16 usable cores (78% efficiency), and the headroom is about **1.29x**, not
   1.4x of 20. Using all 20 threads is harmless (peak RSS differs by 2 MB of 314) but buys nothing.

   This also means a benchmark that reports "Nx on 20 cores" on Apple silicon is reporting against a
   denominator the hardware cannot deliver. Small trees still scale badly (`turn` at 2.01x), but they
   are cheap in absolute terms — the share-is-not-amount rule applies. Note also that the old claim
   of a net *loss* on the smallest trees no longer holds: `turn` is a 2x gain.
2. **x86 gather (#89) — answered and already taken.** This entry used to say "unknown, needs x86
   hardware", which contradicted the measured section above (#270). #89 settled it on a Ryzen 9
   9950X: hardware gathers **lose** (`cs[]` lookups by gather +40%, entry fields by dword gather
   +8%), so AVX2 has nothing to offer here; AVX-512 with VBMI permutes wins 1-4% single-threaded and
   **ships** as `showdown_board_avx512`, compiled when the target has AVX-512F/BW/VBMI. Nothing left
   to try on this axis.
3. **GPU (#58) — no-go, and the bar went up.** Unified memory removes the usual blocker on Apple
   Silicon and the parallelism is there, but arithmetic intensity is ~3 flops per 3 memory touches,
   each board carries a serial scan, bit-stability is lost, and it means two permanent backends.
   This entry used to say "revisit once (1) is done"; (1) is now largely done, and the measurements
   in `gpu_feasibility.md` (#217) make the answer *more* negative, not less:
   - the CPU target is now **~10 G entry-visits/s** at 20 threads, not the 556 M single-thread figure
     the case was originally argued against;
   - Metal has **no float atomics in `threadgroup` address space** (`device` only), so the mapping
     that document recommends — `cs[52]` in threadgroup memory with threads cooperating — cannot be
     written as specified;
   - the atomics-free alternative compiles but reads every opponent entry from all 52 threads, ~52x
     the CPU's memory traffic, on a kernel that is already throughput-bound on memory.

   It was never blocked on tooling, though: a shader compiles at runtime via `newLibraryWithSource`
   with only Command Line Tools.
4. **Convergence — already taken.** The current defaults are **α=1.3, γ=4** after the #72 retune.
   The earlier γ 2→3 step gave -11% wall time and 455 iterations against 500 (#52,
   `dcfr_sweep.md`), and #72 then moved past it; γ=5 is faster still in total but regresses a low-SPR
   3-bet pot at tight targets, so it is available via `set_dcfr` rather than default. Re-sweeping is
   cheap and has paid before — see the `avg_delay` entry below — so do it after anything that moves
   where the time goes, rather than trusting a sweep from an older default.

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

- **Cheapening the scheduler's spin at a join.** A 20-thread profile of `flop_srp_2` puts **25.1% of
  all samples in synchronisation waits** (`__psynch_mutexwait` 9.8%, `__psynch_cvwait` 7.6%,
  `__ulock_wait` 4.8% — that last one is the main thread in `wait_for_solver` — `__psynch_mutexdrop`
  2.8%). The mechanism is real: a thread waiting at a join takes the one scheduler mutex, finds
  nothing in `pick_job`, drops it, yields, and repeats, so every waiting thread hammers `m_`.

  Fixing it does **not** help. A `publish_epoch_` atomic bumped on each job publish lets the spin
  skip the mutex entirely until something new is published (sound, because a job's `next` only ever
  increases, so an empty `pick_job` stays empty until a publish). Bit-identical, suite green, and
  worth **-0.95% at 0.6x sd** — not significant; per-iteration it was -1.2% on `flop_srp_2` and
  **+1.4%** on `deep`, signs disagreeing.

  **The lesson: that 25% is threads with nothing to do, not throughput lost to lock traffic.** Wall
  clock at a join is bounded by the straggler finishing its chunk, and making the other threads idle
  more cheaply does not shorten it. A profile showing a quarter of samples in mutex wait reads as a
  quarter recoverable, and it is not. So the remaining ~1.4x of scaling (#53) is a **load-balance /
  critical-path** problem — fewer threads sitting at the join — not a scheduler-contention one.

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
    (T1), srp_big 39.6 vs 39.3 s: within noise. *Superseded by #238*: re-measured on main after #237,
    prefetching every line of the node's rows (write intent) is -6% on the M4 at 1 and 10 threads and
    -7 to -10% on Zen 5 from 1 to 32 threads on threebet_mono; only srp_big at 32 threads, which is
    bandwidth-bound, is unchanged. Prefetching the children's regret rows as well is slower (+4-10%).
  - **Terminal payoffs from a per-action-node table** instead of `terminal_payoff` per showdown job:
    no gain (within noise; if anything slower).
  - **Exploitability checks** cost 1.2 iterations each on threebet_mono (19.6 vs 16.2 ms at 32 threads)
    and 1.0 on srp_big (0.31 s); `set_info_freq auto` runs 10 and 8 of them, 6-8% of a solve. The
    check walk (`walk_evbr`) still evaluates showdowns one terminal at a time; giving it the
    two-phase river walk and the lanes kernel would save an estimated 1-2%, not worth a third copy of
    the river semantics. Separately, the auto schedule's check at iteration 33 lands right after the
    avg_delay reset (threebet_mono: 25.1 -> 40.4 chips), so it is uninformative.
- **The PC is memory-bound from 8 threads up, even at 16-bit** (2026-11-03, threebet_mono, 60 iterations,
  after #226). 1 / 4 / 8 / 16 / 32 threads: 9.58 / 2.65 / 1.45 / 1.02 / 0.90 s (10.6x at 32). float32
  takes 1.46 s at both 16 and 32 threads, against 0.99 / 0.88 s for 16-bit, and two 8-thread solves run
  side by side take 1.83 s each (1.45 s alone), so 16 cores of independent work add only ~10% over one
  32-thread solve. Per element and iteration the walk moves ~10 bytes at 16-bit (the owner's pass reads
  and writes R and S, the other pass reads R); that is ~48 GB/s at 32 threads against ~58 GB/s for
  float32. Compute savings therefore show at 1 thread and on the Mac, and only partly on the PC at 32.
  Neither zero-reach shortcut is available at cache-line granularity: 54-55% of reach entries at river
  nodes are exactly zero, but only 6-7% of 32-hand lines are.
- **Half-width lanes for packs of up to 4 lanes** (#234). 3-4 lanes are 50-65% of the lanes kernel's
  work on srp_big and threebet_mono, so the NEON kernel now has a 4-lane width: M4, 10 threads,
  threebet_mono to 0.6 chips 5.89 -> 5.54 s (-6%), srp_big 12 iterations 8.21 -> 7.78 s (-5%),
  bit-identical. The same split on x86 (a 128-bit `showdown_lanes_256`) is no faster on Zen 5, even at
  1 thread (3.13 vs 3.11 s), so the x86 builds keep one width. Also measured: 47% (threebet_mono) and
  60% (srp_big) of the opponent entries the kernel adds are zero in every lane, but the opponent loop
  is the smaller half of the kernel (~64 entries per call against 128 hero entries done three times).
- **More NEON kernel variants after #234, none kept** (M4, threebet_mono, 1 thread, 30 iterations, all
  bit-identical). Inside the kernel the strength-group loop is 46-57% of its time, the final pass 14-18%,
  the output scatter 15-25%, the reach transpose 6-16% (54-64 groups per call, about 2 hero and 2.5
  opponent hands each).
  - Output in hand order (4 hands' rows transposed in registers, vector stores into each job's row,
    instead of one scalar scatter per job): 4.62 -> 4.68 s.
  - Skipping opponent entries whose reach is zero in every lane (47% of them): 4.71 -> 4.74 s.
  - Hero queries in fixed chunks of 1 / 2 / 4 per group (one loop trip for most groups instead of a
    data-dependent 0-3): +3% / +5% / +9%. Branch mispredictions are not what the group loop costs.
- **Walking the tree in reverse on alternate passes**, so the next pass starts on what the last one
  left in L3: PC, threebet_mono at 32 threads, float32 1.497 vs 1.491 s, 16-bit within noise.
- **After #237 / #238** (M4, threebet_mono, 1 and 10 threads; bit-identical, none kept). Profile at 1 thread
  with #238 in: the river walk is 91% of an iteration; inside it the lanes kernel 41%, `current_strategy`
  17.5%, `traverser_update` 9.2% (was 19.5% before #238's prefetch), the value sums ~7%.
  - **More prefetching**: the next traverser node's regret rows during the bottom-up pass (they are still
    cached from `current_strategy`): within noise. Regret rows a fixed 2 / 8 / 32 KB ahead in storage
    order at each node (river subtrees are stored in visit order): +5% / +7% / +10%. With #238's child-row
    result, software prefetch of regrets only gets in the hardware prefetcher's way.
  - **Regret-matching fallback for only the hands that need it** in `average_strategy` (every river node
    has some, the board-blocked hands with zero sums, so the whole `current_strategy` ran there): check
    cost +10%, or unchanged when restricted to nodes where at most 1/8 of the hands need it. The full
    vectorised call is cheaper than a scalar per-hand loop.
  - **Board-blocked hands are 8.05% of river storage** on every tree (river rows are 98-99% of all
    storage; turn rows 1-3%, 4% of them blocked), so 8% of the bytes streamed per iteration are dead. Compacting them is #239.
- **Per-street DCFR parameters** (2026-11-05, M4; an env-var prototype giving nodes with 3 / 4 / 5 board
  cards their own alpha / beta / gamma). Iterations to 1 / 0.5 / 0.3 / 0.1 / 0.05 / 0.02% of the pot
  (deterministic counts, checked every 2 iterations; `-` = not reached within 800), one street changed
  at a time from the default 1.3 / 0 (0.5 after 50) / 4:

  | change | threebet_mono | deep | flop_srp_4 |
  |---|---|---|---|
  | default | 108 142 174 296 514 - | 46 64 82 140 190 286 | 28 36 38 56 78 116 |
  | river alpha 1.0 | 110 148 190 - - - | 46 62 80 136 178 276 | 28 36 36 86 106 140 |
  | river alpha 1.15 | 100 136 168 500 666 - | 46 64 82 138 182 276 | 28 36 38 54 76 112 |
  | river alpha 1.6 | 112 150 190 358 562 - | 48 68 86 144 204 306 | 28 36 40 60 86 132 |
  | river gamma 3 / 5 | 110 146 182 326 568 - / 110 146 180 320 562 - | ~default | ~default |
  | turn alpha 1.1 | 102 138 196 484 - - | 46 64 80 134 176 272 | 26 32 38 54 74 128 |
  | flop alpha 1.0 | 124 290 334 - - - | **42 50 58 94 122 180** | 24 30 58 82 108 156 |
  | flop alpha 1.8 | 104 140 172 306 574 - | 66 100 134 230 344 566 | 32 36 42 72 106 164 |
  | flop gamma 5 | 108 142 176 304 534 - | 46 64 84 142 200 294 | 26 32 38 56 76 114 |

  Nothing wins on every tree: flop alpha 1.0 takes 37% off `deep` and never reaches 0.1% on
  `threebet_mono`; river alpha 1.15 is 5% faster early on `threebet_mono` and 70% slower at 0.1%. The
  global defaults stay. The size of the `deep` effect says some trees have a much faster schedule
  than ours; a per-tree choice would need a way to pick it without solving twice.
- **Sampling chance cards in the first iterations** (2026-11-05, M4; prototype: through iteration T, each
  turn-to-river chance node visits one river card in m, stratified by iteration and a per-node hash,
  the kept cards reweighted to the full deal weight; T <= 16 so it ends before the average restarts
  at 33). Iterations to 1 / 0.5 / 0.3 / 0.1% of the pot, m = 4, T = 8, four hash seeds:

  | tree | default | sampled (4 seeds) |
  |---|---|---|
  | threebet_mono | 108 141 174 295 | 91-93, 123-125, 157-174, **294 / 309 / 366 / not in 400** |
  | deep | 46 64 82 139 | 38, 48-49, 60, 114-115 (-18%) |
  | fixture25bb (16-bit) | 110 152 190 386 | 100 132 166 338 (one seed) |
  | flop_srp_2 | 23 30 37 52 | 28 36 41 64-65 (+25%) |
  | flop_srp_4 | 27 35 37 56 | 28-29 36 39 59-62 |

  The sampled iterations are also cheaper, so on `threebet_mono` (10 threads, m = 4, T = 16, two
  interleaved reps each) it is 16% / 11% / 9% faster to 1 / 0.5 / 0.3% and 2% slower to 0.1%.
  Not kept: it loses on the simple SRP trees at every target, and on `threebet_mono` the tight-target
  result depends on the seed (one never reaches 0.1% in 400 iterations). m = 2 / 3 and T = 12 / 16
  behave the same way. Like the per-street result above, it gives fewer iterations, not only cheaper
  ones, on `deep` and the 25bb tree: the early regret dynamics matter more than the cost of the early iterations.
- **Pio's per-iteration convergence matches ours** on threebet_mono (see bench/vs_pio.md), so no
  algorithmic gap was found there; the lead is per-iteration speed.

**The granularity rule.** Subtree-level reach pruning never fires because a node almost always has
*someone* in it; per-entry skipping pays because 55-60% of individual opponent reach entries are
exactly zero at showdown terminals (#151, verified at +4.3% bit-identical on `deep`). And skipping
**exact zeros** is safe because it changes nothing, whereas skipping **small** contributions is not:
per-hand regret pruning on the traverser's side took `deep` from 180 to 425 iterations (#151), and
sampling the strategy average took `flop_srp_2` from 75 to 970 (#135). Cheapening a per-iteration
update by dropping contributions costs more in convergence than it saves in work.

On convergence, measured 2026-11-04 on an Apple M4 (10 threads) with one viewer tree: an SRP
Qs8d7s flop, 541K nodes, two sizes per street, solved to 0.3% of the pot (0.165 chips). The
default takes 198 iterations, about 91 s. Iteration counts are deterministic, so they are the
signal; the seconds are interleaved runs on a quiet machine.

- **PDCFR+** (Xu et al. 2024: predictive regret matching plus DCFR at α 2.3, γ 5, negative regrets
  dropped) loses badly:

  | configuration | iterations |
  |---|---|
  | default | 198 |
  | DCFR 2.3 / -inf / 5 alone | 448 |
  | `set_predictive on` alone | 297 |
  | PDCFR+ (both) | 559 |

  This is the #71 result again, now with the published parameters. The predictive runs were also
  about 4x slower per iteration: one extra float per regret slot pushed this 16 GB machine toward
  swap.
- **Averaging delay on a small tree** makes no difference. On a 115K-node flop tree, every
  `set_avg_delay` from 0 to 48 reaches 1.65, 0.5 and 0.165 chips within one or two iterations of
  the others. γ=4 already weights the late iterates.
- **Warm-starting from a smaller tree.** The idea: solve a sketch first (one turn/river size, no
  turn/river raises; it shares the full tree's flop) and copy its flop regrets into the full tree
  before it starts. That does save iterations on the full tree, but the sketch's own cost takes it
  back. Times are when the solve crosses the target, interpolated; the sketch is solved to 10x
  the target:

  | tree | cold | sketch + warm-started tree | change |
  |---|---|---|---|
  | SRP Qs8d7s, 541K nodes | 87 s, 198 iterations | 4.5 + 76 s, 169 iterations | -7.5% |
  | SRP Kh8h3h, 256K nodes | 37.9 s, 181 iterations | 2.2 + 35.2 s, 168 iterations | -1% |
  | 3-bet Jd7c4s, 161K nodes | 3.4 s, 187 iterations | 0.5 + 3.3 s, 182 iterations | +12% |

  Seeding the sketch's flop *strategy* as regrets (`set_strategy` with a chosen scale) depends on
  the scale picked. On Qs8d7s:

  | scale | full-tree solve to target |
  |---|---|
  | 1e2-1e3 | about the cold time |
  | 1e4 | 81 s |
  | 1e5 | 75 s |
  | 1e6 | 80 s |
  | 1e7 | 190 s (this locks the flop to the sketch) |

  Copying the real regrets reaches about the same result without picking a scale.
- **Running a second solve alongside the main one saves nothing.** The viewer's flop sketch was
  run concurrently, starting the main solve once the sketch reached 10x its target: the main
  solve finished at 101.3 / 101.5 s, against 101.0 / 101.5 s run one after the other. Two
  processes share the same 10 cores and do the same work.
- **Stochastic rounding for 16-bit storage** (round up with probability equal to the fraction,
  from a hash of iteration and slot, so updates smaller than one quantum survive on average).
  Forcing `small_strats on`:

  | 16-bit storage | iterations | time |
  |---|---|---|
  | round to nearest | 214 | 97.9 s |
  | stochastic rounding | 204 | 116.7 s |

  It recovers part of the gap to float32 (198 iterations), but the hash per stored value costs
  about 24% per iteration. At this target, 16-bit storage only costs 8% more iterations; the 1.8x
  of `bench/bandwidth.md` is at 0.05% of the pot. A cheaper generator might change the answer for
  very tight targets. **On x86 it does (#272, 2026-11-05):** on the Zen 5 PC the hash costs
  1.5-2.5% per iteration at 1 thread and nothing measurable at 32 threads, where the walk is
  bandwidth-bound. Rounding to nearest is what makes full 16-bit stall at tight targets (threebet_mono
  never reaches 0.05% of pot), so x86 builds now round the sums stochastically: threebet_mono
  reaches 0.05% in 9.0-9.3 s, against 13.9 s with float32 and 48 s under main's `auto`.
  **And on ARM (#291):** with the uniforms read from a fixed table instead of hashed per slot, the M4
  pays ~2% per iteration (the hash was ~9%), and 16-bit threebet_mono reaches 0.05% of pot in 16.7 s
  against 17.0 s for float32 (nearest rounding never gets there). All builds round stochastically now.
- **Thread scaling on the M4** (4 performance + 6 efficiency cores, same tree, after the first 20
  iterations): 1 -> 4 threads gives 3.57x, 4 -> 10 threads gives 1.6x more, 5.7x in all, about 89%
  of what this core mix can deliver.
- **Sustained load does not throttle it:** 45 back-to-back 30-iteration runs over 10 minutes stayed
  at 13.2-13.4 s. Twice-as-slow runs seen earlier that day were outside load, so check `uptime`
  before timing anything.

## Showdown terminals per river subtree, measured on arm64 (#186)

#186 wants a river subtree's showdown terminals evaluated in one sweep over the board's strength
groups, and estimates 15-20% per iteration from roughly 6.2 kernels per river subtree visit. That
6.2 came from the owner's x86 PC. Measured here by instrumenting `showdown_batch` with `river_batch`
forced on (1 thread, 8 iterations; **T** is jobs per call, all sharing one `ShowdownBoard`):

| tree | mean T | biggest T buckets (calls) |
|---|---|---|
| `srp_big` | **5.76** | T=3: 1.17M, T=1: 437K, T=7: 608K, T=9: 260K, T=16: 109K |
| `threebet_mono` | **4.67** | T=3: 198K, T=1: 81K, T=7: 79K, T=11: 32K |
| `flop_srp_2` | 1.39 | only T=1 (130K) and T=2 (84K) |
| `deep` | 1.15 | only T=1 (528K) and T=2 (96K) |

**The premise holds, but only on the realistic trees.** `srp_big` at 5.76 is close to the 6.2
reported on x86, so the batching opportunity is real on arm64 too.

**The trap: do not measure #186 on the default bench trees.** `flop_srp_2` and `deep` have T of 1.15
to 1.39 — there is essentially nothing to batch there, so a correct implementation will measure as
*no change or slightly worse* on exactly the trees people reach for first. It has to be measured on
`srp_big` and `threebet_mono`.

Also note T=1 is 14% of calls on `srp_big` and T=3 is the single largest bucket, so a multi-terminal
sweep has to stay fast at small T rather than only winning at T=8+.

### What the two-phase walk costs on arm64

`river_batch` (the two-phase walk `river_cfr` / `river_down`) is compiled on arm64 but **off by
default**, because without a batched kernel it is pure overhead. Re-measured on this machine
(M1 Ultra, 20 threads, interleaved, alternating order):

```
threebet_mono   +3.4%   (T = 4.67)
srp_big         +1.1%   (T = 5.76)
```

Consistent with the +2-3.5% recorded on an M4, and the overhead is smaller on the higher-T tree, as
expected. So the ARM half of #186 has a low bar: a scalar multi-terminal sweep needs to beat about
1-3.5% of total runtime to break even, against showdown being ~60% of it. That is a favourable
ratio, and it means #186 is worth doing on arm64 and not only on AVX-512 builds.

### Ruled out: a *scalar* multi-terminal sweep (#186 on arm64) -- but SIMD lanes win (#219)

I implemented the multi-terminal sweep for the **scalar** kernel — one pass over the board's entries
for all T jobs, each job keeping its own `cs[]`/`cum`/`ltv`/`lev` and accumulating in exactly the
order its standalone call would. It is **bit-identical** and it **buys nothing**.

Measured on `threebet_mono` (T=4.67), 9 interleaved reps with alternating order:

```
river_batch off (current default)        min 0.209  median 0.215   --
river_batch on + batched scalar sweep    min 0.217  median 0.222   +3.2 to +3.5%
batched sweep vs standalone loop,
  same two-phase walk                                              -0.7% to +2.1% (noise)
```

The whole +3.4% two-phase walk overhead survives, because the kernel change saved ~0%.

**Why, and this is the part worth keeping:** #186's estimate comes from sharing "the opponent entry
load and **mask construction**" across terminals. Mask construction is an **AVX-512 concept** — it is
the `bit(c1)|bit(c2)` build plus 4 shifts and a `kmov` feeding four masked adds. The scalar kernel
has no mask construction at all; it does `cs[q.c1] += r` directly. So the shared work the estimate
is built on **does not exist in the scalar formulation**. What is left to amortise is one 6-byte
`SdEntry` load that is already in L1, while the per-job `ro[q.idx]` gather and the three adds are
irreducibly per-job. Batching also costs locality: `cs + j*52` and `ltv + j*np` are strided where the
standalone kernel has one contiguous `cs[52]`.

**Correction.** I first wrote here that this made #186 "genuinely AVX-512-specific". That is wrong,
and #219 proves it: a **NEON lanes** kernel batches the same terminals on ARM and is a large win.
Re-measured on this machine (`threebet_mono`, 20 threads, interleaved, best 10 of 14 reps, sd 0.002):

```
NEON lanes (current default)   min 0.185  median 0.190
-DPIO_NO_NEON_LANES            min 0.214  median 0.220
                                          -13.5% to -13.7%
```

(#219 reports -22% on an M4.) My measurements above were taken against a pre-#219 tree, where
`river_batch` still read `PIO_SHOWDOWN_AVX512 != 0`.

So the useful conclusion is narrower than "AVX-512 only", and sharper than the original estimate:

> The batching win comes from **SIMD lane parallelism across terminals**, not from amortising shared
> scalar work.

The negative result above is what isolates that. Amortising the shared scalar part — the `SdEntry`
load and decode — buys **zero**, which is why a scalar sweep fails. The win is in executing several
terminals' adds *simultaneously* as lanes, which is why both AVX-512 (#209) and NEON (#219) succeed.
Anyone extending this should be chasing lanes, not shared loads.

The implementation is not committed. A note on validating any future attempt: `test_solver`'s
`[river batch]` check passes **vacuously** for this kernel, because its tree only ever has T=1, so
the batched path is never entered. I confirmed that with a call counter before trusting it. Validate
instead by A/B-ing the two kernels under the same two-phase walk on `threebet_mono` or `srp_big` and
comparing solver output byte for byte.
