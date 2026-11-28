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
  update S, so only the regret write could be deferred. Not pursued. **Its ceiling is zero on the M4**
  (2026-11-06, threebet_mono, 10 threads, 150 iterations, 4 interleaved runs): a build that skips the
  unreached nodes' regret decay and regret store altogether (wrong results, valid timing) runs in
  4.008 s median against 4.009 s for main.
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
    storage; turn rows 1-3%, 4% of them blocked), so 8% of the bytes streamed per iteration are dead.
    **Compacting them (#239) is slower: built, measured, not kept** (branch `compact-rivers`). River
    nodes stored A x H' rows, every river board got its own compact hand space, and the walks gathered
    reaches into it at each turn->river chance node and scattered values back; float32 bit-identical
    to full rows, storage -7.7%. Time: M4 10 threads threebet_mono +2.1%, flop_srp_4 +7.2%, deep
    +11.8%; Zen 5 32 threads -1.4% (noise), +6.6%, +12.5%. The river subtree gained only 1% (deep,
    thread-seconds 19.76 -> 19.56): the showdown kernels, its largest part, never touched blocked
    hands, so only the row loops shrink. The gather and scatter (2.16 + 1.08 s against 0.55 + 0.14 s
    for the memcpy and zeroing they replace) cost more than that at every river root, though they
    already copy the runs between blocked hands in 4-float blocks (3x an index loop). What is left is
    8% less memory at a 2-12% time cost, for trees within 8% of the RAM limit.
- **Profile at 10 threads** (2026-11-06, M4, threebet_mono float32, `sample` for 8 s; the main thread's
  wait in `wait_for_solver` left out): the lanes kernel 35%, `current_strategy` 20%, `river_down` 8%,
  `traverser_update` 8%, `river_cfr` 6%, `strategy_only` 6%, `memset` 2.6%, `Arena::alloc` 2%,
  `terminal_payoff` 2%. Workers barely wait (`cv_wait` + `swtch_pri` ~1.7%).
  - **`terminal_payoff` per visit -> a table built with the tree** (#339): kept, bit-identical,
    -0.7% threebet_mono, -1.4% deep, -1.0% flop_srp_4.
  - **`Arena::alloc` inline** (the bump in the header, the new-block path out of line): 4.000 vs
    4.002 s, nothing, so its samples were not call overhead.
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
  **And on ARM (#293):** with the uniforms read from a fixed table instead of hashed per slot, the M4
  pays ~2% per iteration (the hash was ~9%), and 16-bit threebet_mono reaches 0.05% of pot in 16.7 s
  against 17.0 s for float32 (nearest rounding never gets there). All builds round stochastically now.
- **Thread scaling on the M4** (4 performance + 6 efficiency cores, same tree, after the first 20
  iterations): 1 -> 4 threads gives 3.57x, 4 -> 10 threads gives 1.6x more, 5.7x in all, about 89%
  of what this core mix can deliver.
- **Sustained load does not throttle it:** 45 back-to-back 30-iteration runs over 10 minutes stayed
  at 13.2-13.4 s. Twice-as-slow runs seen earlier that day were outside load, so check `uptime`
  before timing anything.

## The app's default trees on the Zen 5 PC (2026-11-27)

Ryzen 9 9950X (32 threads, dual-channel DDR5), RX 9060 XT (16 GB), Windows native static builds. The
viewer's Smart tree (`build_tree smart_rivers auto_flop_order sqrt_flop_opening`), `set_info_freq
auto`, to 0.3% of pot, after #579:

| tree | storage | iterations | time | per iteration |
|---|---|---|---|---|
| HUNL 3BP | 16-bit, 1.3 GB | 129 | 7.1 s | ~55 ms |
| HUNL SRP | 16-bit, 9.3 GB | 226 | 96.7 s | ~0.43 s |

- **The CPU is at the DRAM wall.** About 10 bytes move per stored value per iteration (the owner's pass
  reads and writes R and S, the other reads R), so 3BP is ~3.3 GB an iteration, ~53 GB/s against ~60
  practical. Everything that changes bytes per value shows up; nothing that changes instructions does:
  the installer's `-march=x86-64-v3` build and `-march=native` (AVX-512) take the same time on 3BP and
  SRP, and 16 threads the same as 32. float32 is 2x slower on SRP (34.4 vs 18.2 s for 40 iterations).
  The floor for 3BP to 0.3% at this bandwidth is ~400 GB of traffic, ~6.7 s; any faster needs fewer
  iterations or fewer bytes per value, not faster loops.
- **Building is not the time:** `build_tree` on 3BP takes 0.03 s of the 8.0 s.
- **Checked and ruled out on 3BP (sampled averaging on, #579):**
  - re-sweeping DCFR under the sampled average: 1.3 / 0 / 4 / (0.5 after 50) 129 iterations; gamma 3
    133, gamma 5 128, gamma 6 128, alpha 1.2 129, alpha 1.5 150, beta_late 1 171, beta 0.5 151. Still
    the default.
  - sampling river cards in the warm-up (each turn->river deal visits a hashed 1 in k cards and scales
    their values by k, for t <= T): k 2 / 4 / 8 to T 32: 6.85 / 7.54 / 8.48 s against 7.01 s; k 2 to
    16, 4 to 16, 4 to 8, 2 to 24: 6.90 / 6.86 / 6.97 / 6.99 s. Within 2%, so not worth a code path.
- **The OpenCL kernels on the RX 9060 XT** (3BP, 60 iterations, `WSOLVER_GPU_PROFILE=1`, float32 state):
  `river_two_bets` 36% of kernel time, `river_betting_round` 26%, `update_values` 12%. Ablating
  `river_two_bets` (ms of 1,098): no prefix scans 1,039; no per-hand loop 598; neither 264; no regret
  and sum update 696; no fold count 1,087. Bytes moved per dispatch over its time is ~177 GB/s, about
  55% of the card's peak; the compact bfloat16 + fp16 state halves the bytes and is only 14% faster
  (latency-bound at ~100 GB/s). None of these changed it: scanning the N channels of a prefix at once
  (bit-identical; a quarter of the barriers), `restrict` on every kernel pointer, the narrow kernels
  (slower), 8 KB more work-group memory (+5%, so occupancy by local memory is not the limit).
  `-save-temps` in the build options writes the ISA: 52-64 VGPRs, wave32, 88 bytes of scratch (the
  arguments), no spills.

## A hybrid solve's time (#582, #591)

The same PC, `set_algorithm v4_hybrid`, the viewer's Smart trees to 0.3% of pot (`WSOLVER_HYBRID_DEBUG=1`):

| tree | total | iterations | reports | setup | copies to the tree |
|---|---|---|---|---|---|
| HUNL 3BP | 4.19 s | 3.66 s (130, ~28 ms) | 0.46 s (10) | 0.12 s | 0.11 s |
| HUNL SRP | 63.8 s | 57.4 s (228, ~0.25 s) | 4.38 s (11) | 1.57 s (with one rebuild) | 1.29 s |

- **A report is CPU-bound, then waits on the GPU's four passes** (EV and best response for each player):
  SRP at a GPU share of 42%, 0.24 s on the CPU's turn roots, then 0.13 s waiting. One fused EV + best
  response pass per player would save ~0.12 s a report, ~2% of the solve.
- **Where the even share is moves between iterations.** Timed one device after the other, 3BP is even at
  45% at iteration 2 and 53% at iterations 19-20 (pruning on, one of them adding to the sampled
  average); SRP at 44% and 42%. Iterations 19-20 are the ones timed now; neither choice was measurably
  faster end to end (3BP 4.38 vs 4.19 s, SRP 62.5 vs 63.8 s), so a one-off share is as good as this goes
  without the GPU's own completion times.
- **Thinning the reports before the average's restart** (iteration 33, where no large tree reaches its
  target) would save 3-5 reports, ~4%, but leaves a large tree with no progress shown for its first
  10 s. Not done.

## HUNL SRP to 0.3% of pot: what is left, and what was ruled out (#598, #599)

The saved SRP spot (Medium Smart tree, 9.1 GB at 16-bit), Ryzen 9 9950X + RX 9060 XT, after #599: 55.4 s
on CPU and GPU (225 iterations: 49.3 s of iterations, 3.8 s of reports, 1.8 s of setup, 1.4 s of copies),
86.6 s on the CPU. At a fixed GPU share of 45% it is 53.6 s, so the share's one rebuild costs ~2 s.

**The iterations are fixed by the algorithm.** Re-swept on SRP itself (CPU and GPU at a fixed 44%):
alpha 1.3 / gamma 4 (the defaults) 226 iterations; gamma 3 228, gamma 5 229; alpha 1.2 328, alpha 1.5 282;
beta 0.25 after 50 273. `avg_delay` 24 / 40 on 3BP: 121 / 128 against 124. The Strong tree (single river
size, 5.6 GB) takes 175 iterations and 25.6 s on CPU and GPU itself, so it is no cheap warm start.
**A warm start for the flop and turn does not help either**: a CPU solve whose river rows (regrets and
sums, 1.2M decisions) are zeroed at iteration W, so its flop and turn are as good as they get, needs 461
iterations to 0.3% at W 120 and 575 at W 200, against 226 from nothing: the rivers are what takes the
iterations, and under a late DCFR clock (mild discounts) their first regrets linger.

**Bytes per value, ruled out:**
- *No strategy sums on the river* (its average falls back to the current strategy): 3BP 124 -> 858
  iterations. The river's average is needed.
- *Fewer bits of regret* (emulated in the 16-bit format, 3BP): 8-bit linear, nearest 303 iterations, with
  stochastic rounding 194; ~10 bits stochastic 127 (no loss, but a 12-bit format would save ~15% of the
  bytes for packed rows in every loop); FP8-like (2-4 mantissa bits) 485-1240.
- *Sampled sums for longer* (stride 4 to elapsed 128): 3BP 144 iterations against 124. Stride 4
  throughout: SRP 250. Pairs to the end only for the 0.3% default (#599); to 0.1% they cost 263
  against 208.

**Chance sampling for the whole solve** (each turn deal visits some of its river cards, values scaled to
match; the exact exploitability still decides when to stop): 3BP to 0.3% of pot, all cards 126 iterations
(6.4 s); a hashed half of them 1,595 iterations (44 s); half by rotation (every card every second
iteration) stalled at 18 chips after 225 s. With gamma 4 the average leans on the last iterations, so
their sampling noise is not averaged away.

**The GPU on SRP, ruled out or not worth it:**
- *A work-group per river subtree*, walked depth first so its reach and value rows stay in the cache
  (`river_tree`, a prototype): 10.2 s of kernel time at width 256, 8.8 s at 64, 8.6 s at 128, against
  ~6.0 s for the level path's river kernels. One work-group's walk is a chain of scans, each waiting on
  the last; a level runs thousands side by side.
- *The GPU's compact state in the hybrid's share* (bfloat16 regrets, fp16 sums from iteration 9, rows
  rewritten in place through the host): the share's kernels -16%, but the solve 52.9 s against 53.6 s at
  the same 45% share. A finer rebalance (2 points) rebuilt the device at iteration 20 and the copy into
  the 16-bit tree cost precision: 232 iterations, 58.7 s.
- **Where the GPU's time goes on SRP**: Medium rivers have node shapes the fused kernels do not cover
  (OOP DDD, IP DDDS, OOP DDSF, IP DDS..., by child kind), so most river nodes take `update_values` level
  by level and every showdown its own `terminal` work-group. Per pass ~17 GB move against ~4.5 GB of
  state: the rest is value and reach rows and board tables. Folding a node's showdown and fold children
  into its own work-group would save at most the `terminal` kernel's ~14%. Built and measured
  (`river_decision`: a river decision with its 1-3 showdown and fold children in one work-group, their values
  kept in registers): bit-identical, and 6381 ms of kernel time against 6358 ms. The kernel replaced ~2000 ms
  of `terminal` and `update_values` with 1884 ms: the time is the prefix scans themselves (one per showdown,
  each over the board's ~3,000-entry table and the opponent's 963 reaches, ~250,000 a pass), not the value
  rows around them.

## CPU and GPU together on Apple Silicon (#589)

M1 Ultra (20 threads: 16 P + 4 E cores, 64-core GPU, 128 GB), the viewer's Smart trees to 0.3% of pot,
2026-11-27, after #587. Memory is the peak footprint (`/usr/bin/time -l`). Each is a second run of the binary:
the first compiles the Metal kernels (4BP on the GPU alone: 0.83 s first, 0.63 s after).

| tree | CPU (`v4_large`) | GPU (`v4_gpu`) | CPU and GPU (`v4_hybrid`) |
|---|---|---|---|
| HUNL 4BP | 1.15 s, 63 it, 0.48 GB | 0.63 s, 64 it, 1.06 GB | **0.60 s**, 63 it, **0.45 GB** |
| HUNL 3BP | 9.39 s, 123 it, 2.67 GB | 4.37 s, 123 it, 5.80 GB | **3.40 s**, 123 it, **2.21 GB** |
| HUNL SRP | 122.8 s, 227 it, 19.0 GB | 50.7 s, 224 it, 41.8 GB | **36.8 s**, 221 it, **15.3 GB** |

- **The memory is the GPU path's double buffer, not the workspace.** `v4_gpu` keeps a second full copy of the
  regrets and sums (`MetalRows` staging, so the CPU can measure a report while the GPU runs the next
  iteration): 2.2-2.7x the tree. The hybrid binds the tree's own rows and updates them in place.
- **Both devices at once cost each other little.** Two processes, the CPU solve (16 threads) and the GPU solve
  (4 threads) of the `hunl_3bp` preset side by side: CPU 14.4 -> 15.6 s, GPU 10.1 -> 11.3 s (+8-12%).
- **The share is best measured with both at work, from above.** Per pass at a fixed share (`bench/run.py`'s
  `hunl_3bp` preset, plain `build_tree`, 3.56 GB; iterations 20-120): at 0.5 the CPU's turns take 0.025 s and the GPU is waiting on it; at 0.7 the two end together
  (0.016 s); at 0.8 the GPU is 0.005 s behind. Whole solves: share 0.6 6.27 s, 0.7 5.09 s, 0.8 5.22 s, 0.9
  5.90 s. #582's calibration (iteration 2, the device's passes alone, then the CPU's) said 0.58: the Apple
  GPU's passes took about twice as long early and alone, and its time per pass also grows when it idles part
  of each pass at a low share (clocks follow load). Measured from the command buffers' GPU times over
  iterations 5-8, starting at 0.75 (where the GPU is the slower side), the calibration lands on 0.74 there (4.95-5.10 s
  against 7.7 s on the GPU alone) and on 0.75 for the Smart SRP.
- Not done: a 16-bit tree solves on the CPU. On ARM `small_strats auto` keeps float32 wherever it fits, and a
  float32 copy of the GPU's share beside a 16-bit tree costs more memory than the float32 tree in place.

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

## Check-downs are most of a tree whose late streets have no bets (#484)

A line with no bets left is checked down: on every turn and river card there is a decision node with
one action, for each player, and each stores a regret row and a strategy row per hand although there is
nothing to decide. In a postflop tree with three streets of betting that is a small share. In a
**preflop** tree it is nearly everything as soon as the schematics stop betting early, because every
subset flop carries the check-downs of every exit: 49 turns x 48 rivers x 2 nodes per check-down.

`set_collapse_checkdowns on` (off by default: Pio keeps those nodes, and they can be browsed per card)
builds such a subtree as one showdown node valued over the runouts, which is what an all-in at that
point already is (`showdown_value` over `AllinRunouts`). Same values; no nodes, no rows, no walk.

Memory, `estimate_tree`, heads-up preflop, full ranges, 6 exits, 5 subset flops, 16-bit rows:

| Postflop betting in the schematics | off | on |
|---|---|---|
| none | 631 MB | 76 MB |
| flop only (bet, raise) | 2,735 MB | 78 MB |
| flop and turn | 8,976 MB | 443 MB |
| flop, turn and river | 53,896 MB | 53,896 MB (nothing is checked down without a choice) |

The ~76 MB floor is the showdown boards themselves: 1,176 runouts per flop at about 16 KB each, 19 MB
per subset flop. That is the next thing in the way of many flops (below).

Time, the "flop only" tree above solved to `set_accuracy 0.05` (both stop at the report of iteration
70), M4 (4P+6E), default threads, wall time of the whole run with `/usr/bin/time -l`, two runs each:

```
                         wall            user     peak RSS    EV OOP / IP       exploitable for
off                      41.03, 41.33 s  401 s    2,970 MB    -0.069 / 0.748    0.043
set_collapse_checkdowns  15.43, 15.42 s  148 s      148 MB    -0.069 / 0.748    0.043
                         2.7x
```

A postflop tree with flop bets only (Qs8d7s, 5 lines) goes from 32 MB to 3 MB and from about 0.123 s to
about 0.001 s per 25 iterations: there the collapsed nodes also get the hand-vs-hand all-in tables.

**Not bit-identical to the default, by nature**, and not expected to be: the value of a check-down is
the same sum taken in another order, so regrets differ in the last bit after one iteration (measured:
4.9e-3 on 2.4e3 at a root). CFR then amplifies that wherever a hand's regret is exactly zero -- a hand
that wins the same pot by betting into folds as by checking -- because rounding decides whether it bets
or checks next. On a small turn tree the two solves were 1.5 chips apart at iteration 2 and 1.2e-4 apart
at iteration 400, both inside their own exploitability all the way. Nothing to fix; it means a test has
to compare the collapsed node with the subtree it replaces (`test_solver`'s `[checkdowns]`, worst
relative difference 1.6e-6 over 600+ node/player pairs), not two solves at a fixed tolerance.

With the setting off nothing changes: `turn`, `flop_srp_2` and the preflop tree above print the same
results and root strategy as before the change, character for character, after 24 / 24 / 12 iterations.

**Left on the table.**
- (Done for flop exits in #494, last section.) Preflop trees get no all-in tables (`allin_table_plan`
  returns 0 for them), so a collapsed check-down
  there still sums over its 48 or 1,176 showdown boards each time it is valued. A table per flop is
  1326 x 1326 x 2 bytes = 3.5 MB, less than the 19 MB of showdown boards it would replace when nothing
  else on that flop needs them. Expected to be the larger part of what is left of the 15 s above.
- A forced node with real decisions beneath it (OOP's check in a tree without donk bets) still stores
  its rows. Dropping those changes 16-bit results and the save layout; not done.

## Flop subsets for preflop trees (#486)

A preflop tree deals a subset of flops. `flops/wsolver_<N>.txt` are ours, made by
`tools/flop_subsets.cpp` (`make flop-subsets`); what is chosen and how close the files are to the full
deck is in `flops/README.md`. This section is what was measured on the way, including what did not work.

**What a subset is fitted to.** Per flop class (1,755) and street, a 169 x 169 table of who is ahead:
on the flop, over the 45 turns, over the 990 runouts. The error of a set of weights is the squared error
of a hand's value against a range, added over the streets (equal weight) and averaged over hands and ranges, where a range
is a run of hand classes in order of preflop strength, every run equally likely. With the classes in
that order the mean over the 14,365 runs is `2/169 * sum of (P_k - mean P)^2` over the 170 partial sums
`P_k` of a hand's errors, so a flop's row is 169 x 170 numbers and the error of weights is a quadratic
form in the rows' dot products (`test_flop_subsets` counts the runs one by one against it). The weights
of a chosen set are solved exactly on the simplex. Nothing is tuned.

**Three objectives that were tried first and dropped** (negative results):

- *River equity only* (the 990-runout table alone). Small subsets came out about 46 % monotone, with
  trips flops at 6.6 % of the weight: a monotone flop is a cheap stand-in for "a flush is possible by
  the river", and nothing in the objective says the flop has to look like the flops that are dealt.
  Hence the flop and turn tables.
- *Matchup by matchup only* (the sum of squared errors over class pairs). 25 flops came out 12.5 %
  monotone against 5.2 % in the deck: errors that are small pair by pair but all of one sign add up
  over a range.
- *Every class in or out of the range independently* (matchup by matchup, plus each hand against all
  hands at once). Textures settled, the error against random subsets looked as good as it does now, and
  the files were shipped to the strategy study below, where 10 and 25 flops did worse than random flops
  with solved weights on the frequencies: the big blind folded 12.7 % and 14.2 % to a raise against 21 %
  with 100 or 200 flops, and the small blind's EV was 0.12 and 0.09 chips low. The cause was a tilt
  this model of a range cannot see. Equity of the best half of the hands against the worst half, through
  the subset minus through the deck (flop / turn / river; the deck's are 75.1 / 70.1 / 65.7 %):

  ```
                                10 flops                  25 flops                  3 x 25 random, solved weights
  classes in or out             -0.2 / -0.9 / -1.4 pp     -0.3 / -0.4 / -0.7 pp     +2.2 / +1.7 / +1.3   +0.8 / +0.6 / +0.3   +0.4 / -0.1 / -0.5
  runs in order of strength     +0.7 / +0.5 / +0.2 pp     +0.4 / +0.1 / -0.0 pp
  ```

  Every one of the 18 splits measured for the two files (best 25 / 50 / 75 % against the rest, three
  streets) was negative: a least-squares fit over few flops pulls matchups towards even, which is
  under 1 % per hand and invisible against a range that is a random half of the hands, but a real
  preflop range is the strong hands or the weak ones. Counting ranges as runs in order of strength
  makes that tilt the first thing the error sees. Also ruled out on the way: the *spread* of a hand's
  value across the subset's flops (suspected of handing weak hands option value) was within 0.5 % of the
  deck's for that objective's 25-flop file, so it was not that.

**Against random flops** (the fitted error: root of the three streets' mean squared errors added, 1.0 = the
pot; median of 200 random draws; `std::discrete_distribution`, so the draws are libc++'s):

| Flops | generated | random, weights solved | random, weights = how often dealt |
|---|---|---|---|
| 10 | 0.0142 | 0.0304 | 0.0404 |
| 25 | 0.0055 | 0.0134 | 0.0260 |
| 50 | 0.0024 | 0.0071 | 0.0180 |
| 100 | 0.0008 | 0.0034 | 0.0126 |

Both parts count, and more as the subset grows: solved weights alone take a random subset down by a
factor of 1.3 (10 flops) to 3.8 (100), and choosing the flops takes it down by another 2.1 to 4.4.
The first run computes every flop's tables and their dot products (under a minute on the M4's 10 cores,
then cached); the search is one thread and deterministic, 4 min 40 s for the five shipped sizes.

**The engine values a preflop exit the way the generator assumes.** `tests/test_upi.py` builds a limp
check-down tree over `wsolver_10.txt` and compares `calc_ev` at the exit with `flop_subsets equity`
(a different program path: class tables against the engine's per-combo boards). Largest difference
6.9e-7 of the pot with the shipped file (2.1e-6 with an earlier one), single precision; `calc_eq_node` at a preflop node is the exact equity, not the one
through the subset, and is checked to stay so.

### What the subset does to a solved preflop strategy

One tree: heads-up, blinds 5/10, 1000 chips, full ranges; the small blind limps or raises to 25, then
raise to 35 over a limp, re-raise to 100 or 110, then 230, then all-in; six flop exits, each with a bet
and raises on the flop and a check-down after (`set_collapse_checkdowns on`). `set_accuracy 0.05`
(chips; the solves stop at 0.037 to 0.050), M4, default threads. The reference is the 200-flop file;
compared are the small blind's EV, the largest difference in any action's overall frequency at three
nodes (first decision, big blind against the raise, big blind against the limp), and at each of those
nodes the RMS over the 1,326 hands of the distance between the two strategies of a hand (0 the same,
1.41 opposite pure actions; every hand counts the same, also one that rarely gets there).

```
                                 flops   EV SB (difference from the reference; lower rows: the difference only)
                                                           largest freq. diff   per-hand RMS, three nodes   wall      peak RSS
200 flops (reference)            200     +0.113                                                             862 s     3,525 MB
200 flops, third objective above 200     +0.120 (+0.007)    0.75 pp             0.093  0.078  0.075         978 s     3,545 MB
100 flops                        100     +0.111 (-0.002)    0.91 pp             0.124  0.092  0.063         390 s     1,898 MB
50 flops                          50     +0.109 (-0.004)    0.69 pp             0.218  0.181  0.134         217 s     1,002 MB
25 flops                          25     +0.143 (+0.030)    3.77 pp             0.332  0.336  0.200         120 s       526 MB
10 flops                          10     +0.170 (+0.057)    4.48 pp             0.459  0.443  0.293          39 s       236 MB
5 flops (not shipped)              5     +0.232 (+0.119)    8.83 pp             0.515  0.557  0.361          18 s       142 MB
25 random, solved weights (3)   15-21    +0.093 -0.009 -0.003   5.14  2.89  3.96 pp   0.49-0.56  0.49-0.57  0.31-0.36
25 random, dealt weights (3)      25     +0.313 +0.102 +0.117  12.82  3.60  4.91 pp   0.60-0.67  0.59-0.61  0.31-0.45
third objective, 25 flops         25     -0.088             6.85 pp             0.366  0.445  0.243
third objective, 10 flops         10     -0.124            14.67 pp             0.593  0.581  0.431
```

(Solving the weights of 25 random flops leaves 15 to 21 of them with any weight. Times are single runs,
some with a one-thread job beside them: good to perhaps 10 %, as the two 200-flop rows show.)

How to read it:

- **The noise floor.** Two different 200-flop files (different flops, chosen by different objectives)
  agree to 0.007 chips, 0.75 pp and a per-hand RMS of 0.08 to 0.09. The same 25 flops solved to 0.05 and
  to 0.0125 differ by 0.005 chips, 0.7 pp and 0.09 to 0.11. So a per-hand RMS near 0.1 is two solves of
  the same game: many hands are indifferent between two preflop actions, and which mix a solve lands on
  is not determined to better than that. The frequencies and the EV are the firmer numbers.
- **100 and 50 flops are near the floor on the frequencies and the EV** (0.9 and 0.7 pp, 0.002 and 0.004
  chips); per hand 100 flops are about at it (0.06 to 0.12) and 50 flops about twice it. The
  floor itself is two comparisons, so "near" is as much as can be said.
- **25 and 10 flops** keep the small blind's opening strategy (limp, raise and fold each within 1.6 pp)
  and have the big blind fold 24.5 % and 24.8 % to the raise against 20.7 %; the small blind's EV is
  0.03 and 0.06 chips high. Per hand the 25 flops are clearly closer to the reference than 25 random
  flops with solved weights (0.33 against 0.49 to 0.56 at the first decision), and on the frequencies
  and the EV they are inside the spread of the three random draws, not better than it. Three draws are
  few; the claim that survives is the per-hand one, for 25 flops. No random 10-flop subsets were solved.
- **Per flop** the tree costs about 17 MB and about 4 s at this accuracy, in step with the number of flops.

**Not measured, and what it would take.** The reference is still a subset: the two 200-flop files
agreeing says the answer has stopped moving, not that it is the answer for all 1,755 flop classes
(about 30 GB for this tree; the 63 GB PC can hold it). Strategy distance is also the wrong unit for a
solver: the question is how much a preflop strategy from N flops loses when it is played in the
many-flop game, which needs the preflop strategy locked into the reference tree. Both belong to the
accuracy study of #483. One tree with flop betting only was solved; with turn and river betting the
flop's texture matters for longer and the picture may differ.

## Preflop solves from `tools/preflop.py`: memory and time (#488)

The tool's default tree: heads-up, 100 bb, full ranges, the small blind limps or raises to 2.5, 3-bet 4x,
4-bet 2.3x, all-in; 7 flop exits, each with a postflop tree (bet 50 %, raise 60 %) down to `--depth` and
checked down after it (`set_collapse_checkdowns on`), 16-bit rows. M4, 10 threads, 16 GB.

**Memory by flops and depth** (`estimate_tree`, MB):

| `--depth` | 10 flops | 25 | 50 | 100 | 200 | all 1,755 classes |
|---|---|---|---|---|---|---|
| none | 154 | 383 | 757 | 1,463 | 2,737 | 12,279 |
| flop | 159 | 396 | 782 | 1,513 | 2,837 | 13,156 |
| turn | 802 | 2,039 | 4,049 | 7,948 | 15,573 | 121,802 |
| river | 85,988 | 221,624 | 437,639 | 854,547 | 1,681,788 | not run |

- **All 1,755 flop classes with flop betting are 13 GB**: about 7 MB per class against 14 to 16 MB per
  flop of our subsets, so multiplying a subset's cost per flop by 1,755 (the "about 30 GB" of the section
  above, for its similar tree) is twice too much. Why a class costs half as much on average was not
  looked into. With the 1.3 below that is about 17 GB in the process: over this machine, well inside the
  PC's 63 GB, so the all-flops reference of #483 can be held there at this depth. The estimate itself
  took 67 s; no all-flops tree was built or solved.
- **The flop's decisions are 3 % of a flop-depth tree** (159 against 154 MB with no postflop betting at
  all). The rest is what the check-downs and all-ins are valued on: the showdown boards of every runout
  of every flop. `GameTree::allin_table_plan` returns 0 for a preflop tree, so none of them uses the
  hand-against-hand outcome tables a postflop all-in has (3.5 MB for a flop, and one table would serve
  every exit of that flop). That is the first thing to try for the memory and the time of these trees.
  Not done here; done in #494 (last section), which also takes the 30 % and the cost of the estimate
  below away from trees that stop at the flop.
- **The process takes 1.3 to 1.4 times the estimate.** A 200-flop build and solve: 2,837 MB estimated,
  3,686 MB maximum resident size of the solver (`/usr/bin/time -l ./solver`); 10 flops: 159 MB estimated,
  220 MB footprint once built. `heap` on the 200-flop solve: 1.47 million allocations, 3.75 GB, which
  fits 6 vectors for each of the 235,200 runouts of 200 flops (`ShowdownBoard`: two entry arrays, two
  group arrays, two blocked-hand lists). `memory_bytes_for` counts the entry arrays and the groups at
  their sizes; it leaves out the blocked-hand lists, whatever capacity the vectors hold over their size,
  the allocator's rounding and the board index. Which of those is the 30 % was not measured. Not changed
  here.
- **`estimate_tree` builds a whole second tree even when the tree is built** (preflop configs only; a
  postflop tree answers from the built one). The tool first asked after `add_all_flops`, and the 200-flop
  solve peaked at 7,163 MB instead of 3,686. It now asks in a solver process of its own before the solve
  starts. The solver could answer from the built tree; not changed here.
- **The estimate costs most of a flop-depth tree.** `estimate_tree` builds the tree without its strategy
  storage and counts it: 22 s and a 3,379 MB peak for the 200 flops, since the showdown boards are built.
  Choosing the subset by `--memory` estimates the shipped sizes upward until one is over, so it builds
  (and frees) one tree larger than the budget's worth of boards. An estimate that counted without
  building would remove both costs. Not done here.
- Measuring: `vmmap`'s physical footprint under-reports after a large free (1.2 GB shown for the built
  200-flop tree once a throwaway build had been freed in the same process); the maximum resident size
  and `heap`'s total are the figures above.

**Time.** The default command (200 flops, the largest subset under half of 16 GB): 717 s of solving to
0.49 bb/100, 810 s for the whole command with the estimates, the build and the charts; exploitability by
time 33.6 bb/100 at 61 s, 17.1 at 193 s, 1.21 at 538 s, 0.57 at 675 s. 10 flops: 35 s to 0.93 bb/100. The
values differ with the subset as in the section above: the small blind wins 0.013 bb a hand with 200
flops and 0.021 with 10.

## Outcome tables for the flop exits of a preflop tree (#494)

A preflop tree values every all-in and every checked-down line of a flop on that flop's 1,176 turn and
river runouts. Until here it did so on stored showdown boards, one per runout, 16 MB a flop with every
hand; a postflop tree has had a hand-against-hand table of `2 * wins + ties` for the same job since the
all-in tables. All figures: M4, 10 threads, the tree of `tools/preflop.py` at 100 bb, 16-bit rows.

**One evaluation, table against boards** (a scratch program calling `Solver::showdown_value` both ways
on one node; random reaches with 0 %, 30 % and 90 % zeros; H = hands per player):

| set | H | table | boards | one evaluation, table | boards, 1 thread | boards, 10 threads |
|---|---|---|---|---|---|---|
| flop, 1,176 runouts | 1,176 | 2.8 MB | 16.0 MB | 27 to 79 us | 2.9 to 4.4 ms (38 to 112x) | 8 to 23x |
| flop | 840 | | | | 53 to 169x | |
| flop | 317 | | | | 107 to 344x | |
| flop | 41 | | | | 345 to 609x | |
| turn, 48 runouts | 1,128 | 1.27 MB | 0.65 MB | 29 to 81 us | 120 to 168 us (1.6 to 4.2x) | 0.8 to 2.2x |
| turn | 818 | | | | 2.2 to 4.9x | |
| turn | 313 | | | | 4.8 to 12.6x | |
| turn | 41 | | | | 14 to 24x | |

A flop's table is both smaller and faster than its boards. A turn's is faster and, with every hand, twice
the size of its 48 boards; with 41 hands the 10-thread boards path is slower than the serial one (17 us
against 5 us), which nothing here changes.

**Which sets get a table** is decided by the tree's size, with no constant: `GameTree::plan_allins`
adds up the tree for the four choices (tables for the flop sets, for the turn sets, for both, for
neither): the tables, plus the boards that a level without tables still reads and no river showdown of
the tree already stores. The smallest wins, tables on a tie. Boards of a set with a table are never
built. What that gives for the default tree:

- last bet on the flop, or no postflop betting: flop tables, no boards at all;
- last bet on the turn: boards only. The flop sets' runouts are the turn sets' boards, and turn tables
  for a flop are 49 x 1.76 = 86 MB against 16 MB of boards, so tables cost more whichever level has them;
- river betting: every board is a river showdown's anyway, so a table is only an addition. None.

ICM values a tie differently from the middle of a win and a loss, so the tables cannot be used and the
boards are kept (stored when ICM is switched on after the build; `set_icm` counts them first and refuses
when they do not fit: 11,750 MB on top of the 6.8 GB tree for all flops). Postflop trees are as before: boards
and a table for every set (at most one flop set and 49 turn sets, 71 MB with every hand); the cap of
512 MB that could never be reached there is gone.

**Results** (same script, old binary at `9568e92` against this change; `go N steps`, whole command =
build + solve + `calc_results`; resident = `/usr/bin/time -l`'s maximum, in MiB as the estimates are):

| tree | | old | new | |
|---|---|---|---|---|
| 10 flops, flop bets, 50 steps | solve | 18.63 s | 0.84 s | 22x |
| | whole command | 20.94 s | 1.10 s | 19x |
| | maximum resident | 236 MiB | 77 MiB | 3.1x |
| 200 flops, flop bets, 20 steps | solve | 132.2 s | 4.98 s | 27x |
| | whole command | 180.4 s | 9.9 s | 18x |
| | maximum resident | 3,522 MiB | 836 MiB | 4.2x |
| 10 flops, no postflop bets, 20 steps | solve | 1.73 s | 0.16 s | 10.7x |
| | whole command | 2.84 s | 0.38 s | 7.5x |
| | maximum resident | 227 MiB | 69 MiB | 3.3x |
| 10 flops, turn bets, 20 steps | solve | 35.80 s | 35.91 s | unchanged, as planned |
| | maximum resident | 891 MiB | 889 MiB | |
| all 1,755 flops, flop bets, 20 steps | solve | did not fit (13 GB estimated) | 42.8 s | |
| | whole command | | 85.5 s | |
| | maximum resident | | 7,037 MiB | |

Time to accuracy with `tools/preflop.py solve` (0.5 bb/100): the default command (200 flops) 31 s, 26 of
them solving, where it was 810 s and 717 s; 10 flops 2.1 s where it was 35 s; **all 1,755 flops 275 s
(229 solving) in 7.1 GB**, a solve this machine could not hold before. The small blind's value: 0.0198
bb a hand with 10 flops, 0.0128 with 200, 0.0131 with all of them.

`estimate_tree` (preflop configs build a tree without storage to count it): 200 flops at flop depth 0.10
s where it was 12.7 s, all flops 0.56 s where it was 67 s; nothing is built for a set with a table.
Estimates (MiB): flop depth 38 / 96 / 192 / 385 / 770 for 10 to 200 flops and 6,763 for all (159 to 2,837
and 13,156 before); no postflop betting 33 to 670 and 5,886. Turn and river depths are unchanged (802 for
10 flops, 85,988). The process against the estimate at flop depth: 842 MiB for 770 and 7,111 for 6,763 in
whole solves with the tool (1.09 and 1.05), where the old tree took 3,522 for 2,837 (1.24): most of what
the estimate left out went with the boards. A 10-flop tree takes 77 MiB for 38, about 40 MiB that does
not grow with the flops. At turn depth: 889 MiB for 802 (1.11).

**Filling the tables** is now what the build of such a tree spends its time on: 0.33 CPU-seconds a flop
(one rank comparison for each of 1,326 x 1,326 hand pairs on 1,176 runouts), 7.0 s for 200 flops and
about a minute for all of them on 10 threads. Taking the runouts 16 at a time, so that a row of the table
gets the ranks of all 16 while it is in the cache instead of the 3.5 MB table being streamed once per
runout: whole 200-flop build 9.5 s (1 at a time), 7.3 (4), 7.1 (8), 7.0 (16), 7.2 (32), 7.6 (64). Flat
from 4 to 32; 16 is in the code.

**Same values?** The counts in a table built without boards equal those of a table built from the
boards exactly (`test_solver`'s `[preflop runouts]`: 3/3 and 125/125 tables). The values are the same
sum in another order, as with the check-downs above: on a flop all-in for 100 bb with random reaches,
against the sum taken in double precision, the table is off by at most 4.7 and 1.5 rms on values up to
3.7 million, the boards by 3.1 and 1.0. Neither is the exact one. Two solves therefore differ in the
last digits from the first iteration (10 flops, flop bets: exploitability 528.109 against 528.119 before
any iteration, 65.192 against 65.191 after 5, 2.586 against 2.628 after 50) and agree to 3 decimals in
EV. A trap for whoever tests this: the two ways differ wildly on hands that hold a card of the flop,
whose values nobody reads (`flop_deal` zeroes them and their reaches); a test must zero those reaches as
the solver does.

Postflop trees: the nine bench trees print the same `calc_results`, root strategy and root EVs as before
after 12 iterations, character for character, and so does `preflop_full` (river betting, so no tables)
after 4.

**Tried and not kept: tables in a tree with turn bets** (env switches in a scratch build; 10 flops, 20
steps; in that build a set with a table kept its boards too):

| | solve | exploitability |
|---|---|---|
| boards only (what is merged) | 36.2 s | 31.617 |
| + flop tables | 34.4 s (-5 %) | 31.605 |
| + flop and turn tables | 19.0 s (1.9x) | 31.591 |

Flop tables there are 3.5 MB a flop on top of a tree of 80 MB a flop: 5 % of the time for 4 % of the
memory. Turn tables are 86 MB a flop (49 turn cards, a byte per hand pair) and would replace the 16 MB
of boards: 1.9x for about twice the tree. (The memory of these three runs was measured with the tree
built twice in the process, a mistake in the script, so only the arithmetic is given.) A rule that
trades memory for time needs a price, and none is known here that is not a made-up constant, so the
merged rule is the smallest tree. Left for a follow-up, with the ways the tables could shrink until
they are the smaller side: only the hands that are live on the turn card (1,128 of 1,326), half the
table when both players hold the same hands (who wins is antisymmetric), one table for turn cards that
are the same up to suits.

**Left on the table.**
- The turn-level trade above, and flop tables as an extra in trees with turn or river bets (5 % for 4 %).
- `add_all_flops` on a tree that already has its flops builds the tree again while the first is held
  (found by the script mistake above: the old 200-flop tree peaked at 7.2 GB that way instead of 3.7).
- A ties table next to the wins would let ICM use tables.
- The table fill: about a minute for all flops. Suit-isomorphic runouts of a flop have the same ranks
  up to a permutation of hands; both players holding the same hands halves the comparisons.
- `tools/preflop.py` still picks its default among the shipped subsets; all 1,755 flops now fit in half
  of a 16 GB machine at flop depth. Whether the default should be all flops is the accuracy study's
  question (#483).


## Compact outcome tables, and tables for the turn all-ins when they fit (#496)

Two of the ways #494 left for the tables to shrink, and the turn-level trade it left open. M4, 10
threads, the tree of `tools/preflop.py` at 100 bb, 16-bit rows, every hand in both ranges. **The machine
was not quiet for any of this** (load 8 to 14: a review's test runs next to it), so times are for
comparing runs taken together, not figures to quote; memory does not depend on it.

**The compact table.** When both players hold the same hands in the same order (a preflop tree with one
range, the usual case) a table of `2 * wins + ties` is antisymmetric about the number of runouts the two
hands leave: what one hand has over that number, the other has under it. So a set keeps, for the hands
its cards leave (1,176 of 1,326 on a flop, 1,128 on a turn), each pair once, as the count minus that
number: a triangle of signed 16-bit entries for a flop set, signed bytes for a turn set (48 runouts).
One function values both players on it (`Solver::allin_tri_value`): a row of the triangle is the part of
its own hand's sum under the diagonal and, negated, a column of the sums of the hands before it. Trees
whose players hold different hands keep the full tables of #494, and postflop trees are untouched.

| | full (#494) | compact |
|---|---|---|
| a flop's table | 3.52 MB | 1.38 MB |
| a flop's 36 turn sets (the turns with an all-in in the scratch tree) | 63.30 MB | 22.96 MB |
| `estimate_tree`, flop bets, 10 / 200 / all 1,755 flops | 38 / 770 / 6,763 | 18 / 364 / 3,194 |
| no postflop bets | 33 / 670 / 5,886 | 13 / 264 / 2,317 |

**One evaluation** (a scratch program on `Solver::showdown_value`, one flop, random reaches, best of 9
rounds; microseconds a value, OOP / IP):

| opponent hands without reach | flop set: compact | full | boards | turn set: compact | full | boards |
|---|---|---|---|---|---|---|
| 0 % | 93 / 89 | 110 / 89 | 929 / 904 | 83 / 82 | 111 / 105 | 99 / 97 |
| 30 % | 87 / 84 | 110 / 69 | 881 / 953 | 74 / 73 | 111 / 78 | 103 / 104 |
| 90 % | 59 / 58 | 110 / 18 | 763 / 654 | 52 / 52 | 113 / 18 | 86 / 92 |

The full table's two sides differ (OOP takes a product per row whatever the reaches, IP adds a row per
opponent hand with reach and skips the others); the compact one costs the same for both. Added over the two players it is the faster at every density
here (flop: 182 against 199, 171 against 179, 117 against 128), and it is the slower one for IP when
most hands have no reach (58 against 18). The three ways agree to 3e-6 of the largest value.

**Solves at flop depth** (old = `ed7a32f`; whole command = build + `go N steps` + `calc_results`,
maximum resident of `/usr/bin/time -l`; two runs each, taken in turn):

| tree | old | new |
|---|---|---|
| 200 flops, flop bets, 60 steps | 21.8, 23.9 s; 840 MiB | 15.2, 17.5 s; 462, 466 MiB |
| all 1,755 flops, flop bets, 5 steps | (7,037 MiB with 20 steps, #494) | 45 s; 3,455 MiB |

1.4 times as fast, which is more than the single evaluations say; the smaller tables are the likely
reason and that was not looked into. The build of the 200-flop tree alone: 4.0 s both ways (the fill is
the same work), 335 MiB at its peak where it was 712. After 60 steps: EV -1.328 against -1.329, exploitable 1.266
against 1.276 (the same sums in another order, as with #494).

**Turn depth: which sets get a table.** With turn bets the smallest tree keeps boards (#494: tables for
the turn sets are more bytes than the boards, and a flop's table then replaces nothing). The compact
tables change the sizes, not that order. The four choices on the 10-flop tree, 20 steps, estimates
802 MiB (boards) and 888 (tables at both levels):

| tables for | whole command | maximum resident | built in (0 steps) |
|---|---|---|---|
| none (`small`; the old binary is the same: 62.1 s, 891 MiB) | 54.8, 46.9 s; later 62.2 | 890 MiB | 0.7 s |
| the flop sets only | 51.9 s | 918 MiB | 0.9 s |
| the turn sets only | 25.0 s | 1,159 MiB | 2.4 s |
| both (`fast`) | 25.7, 22.5 s; later 29.1 (`auto`) | 1,004 MiB | 2.1 s |

EV OOP -7.413 / -7.414 and exploitable 31.57 to 31.62 across them. So:

- **the turn tables are the time**: twice as fast, with or without the flop's;
- **flop tables alone buy nothing that shows** (51.9 s between two runs of none at 54.8 and 46.9), which
  takes back #494's "5 % for 4 %" as a reason to have them on their own;
- **turn tables without the flop's are the worst buy**: the boards all stay, for the flop sets, and the
  tables come on top (1,159 MiB). With both levels no board is left and the tree is 11 % over the
  smallest (888 for 802; 2,270 for 2,039 with 25 flops; 4,509 for 4,049 with 50).

**To an accuracy with the tool** (`tools/preflop.py solve --depth turn --flops 25`, to its default
0.5 bb/100; one run each, one after the other, the machine at load 20 for both, so the ratio is the
figure and not the minutes): the old binary 1,567 s (8,925 s of processor time) and 2,193 MiB at its
peak; the new one, under `auto`, 584 s (3,255 s) and 2,473 MiB. 2.7 times, to the same answer (small
blind +0.067 bb a hand both, exploitable 0.47 and 0.48 bb/100). The steps were not counted, so how much
of that is the time per step and how much a different number of steps is not known.

**The rule** (`set_outcome_tables auto`, the default): tables at both levels if the tree with them
fits in the memory available, by the margin `build_tree` already keeps for its other automatic storage
choices (nine tenths of what is free); otherwise the smallest tree, as before. Nothing in between: the
first version here fell back to "flop tables only, then turn tables only", which the table above gives
no reason for, and it went. This is not the price #494 asked for (memory against time); it only spends
memory that is there. `small` is the smallest tree whatever the memory, and what `estimate_tree` counts
under `auto` too, so a tree can come out over its estimate when it has the room: by what tables at
both levels take less the boards they make needless, 86 MB net for 10 flops at `--depth turn` whatever
the sizes (the turn tables alone add 227 MB, by the non-author run of PR #500). That is 11 % with the tool's
default sizes (888 for 802), 19 % with `--stack 20` (540 for 454), 23 % with `--no-limp` (463 for
377), 46 % on the two flops of the test (51 for 35 MB), 0.2 % at `--depth river` (86,184 for 85,988),
and nothing at `--depth flop`, the tool's
default, where the smallest tree has the tables already. `fast` is tables whatever the memory. ICM keeps the boards as before.

**Not done.**
- Quiet-machine figures.
- `auto` through the commands (`add_all_flops`, `load_tree`) is tested only where memory is plentiful:
  nothing here can make a test machine short of it, so the limit itself is tested on `GameTree` alone
  (to the byte).
- The tool and the viewer choose a number of flops by `estimate_tree`, which under `auto` is the smallest
  tree: with room on the machine a `--depth turn` tree can be built over a `--memory` the user typed
  (when the smallest tree is within the budget and the tree with the tables is not: 11 % apart with the
  default sizes, more on a smaller tree, see above). #504 closed this: the tool asks for both figures
  and sends `set_outcome_tables small` when the tables would pass the budget, and the viewer's memory
  row shows both.
- A compact table is filled as a full one and then folded, so the fill is no faster and holds the full
  table of one set per thread while it runs. Still about a minute for all flops.
- Trees with two different ranges keep the full tables. One table for turn cards that are the same up
  to suits (#494's third way) is not here.
- Under `auto`, `estimate_tree` says the smallest tree (under `fast`, the one with the tables); it does
  not say what `auto` will build (`estimate_rebuild_tree` says what it did build).

## What a preflop strategy from N flops gives up on all flops, and what the depth costs (#497)

The accuracy question of #483 in the solver's own unit. The tree of `tools/preflop.py` at 100 bb (the
small blind limps or raises to 2.5, 3-bet 4x, 4-bet 2.3x, all-in; bet 50 %, raise 60 % on the flop, checked
down after: `--depth flop`), full ranges, M4, 10 threads unless said.

**Method** (`tools/preflop.py check`, same commands by hand here). Solve the large game: its value V for a
player lies between what that player's best response makes against the opponent as solved (`MES`) and
what the opponent's best response leaves (`EV OOP + EV IP` minus the opponent's `MES`). Then, for one
player at a time: `set_equal_strats` (a fresh start, no locks), `set_strategy` and `lock_node` at each of
that player's preflop decisions with the strategy under test, and solve again. Everything not locked is
solved against it: the opponent's preflop play and both players' play after the flop. The best responses
of `calc_results` keep to the locks, so the same two bounds hold for the value V' of the game in which the
player is tied to that preflop strategy. V - V' is what the strategy gives up against an opponent who
knows it and plays the best reply. Figures are the difference of the two solves' EVs and, in brackets,
the span the four bounds leave; bb/100 (chips x 1 at 100 chips a big blind).

**Results on all 1,755 flops**, every solve to 0.02 bb/100, strategies under test solved to 0.02 on their
own flops and locked per hand class (every combo of a class plays the class's frequencies):

| strategy solved on | its own solve | SB's EV there | small blind gives up | big blind gives up |
|---|---|---|---|---|
| all flops (control) | 930 s | 1.24 | 0.004 [-0.033, 0.046] | 0.001 [-0.035, 0.044] |
| `wsolver_200.txt` | 121 s | 1.21 | 0.021 [-0.017, 0.062] | 0.008 [-0.028, 0.051] |
| `wsolver_100.txt` | 50 s | 1.20 | 0.067 [0.029, 0.108] | 0.041 [0.005, 0.083] |
| `wsolver_50.txt` | 39 s | 1.24 | 0.191 [0.153, 0.232] | 0.165 [0.128, 0.206] |
| `wsolver_25.txt` | 14 s | 1.54 | 0.473 [0.436, 0.514] | 0.531 [0.495, 0.574] |
| `wsolver_10.txt` | 6 s | 1.91 | 1.426 [1.388, 1.467] | 1.031 [0.996, 1.074] |

- **The loss falls about as 1/N, or faster**: by 3.0, 2.5, 2.9 and 3.2 for the small blind and by 1.9,
  3.2, 4.0 and 5 for the big blind as the flops go 10, 25, 50, 100, 200 (2.5, 2, 2 and 2 times as many).
  The big blind's first step is the one that falls slower than the flops rise; the last two ratios are of
  numbers with spans as wide as themselves.
  **200 flops are not told from all of them at this accuracy**: both spans include zero, and the point
  estimates are a few times the control's.
- **A solve to the default accuracy, 0.5 bb/100, gives up 0.17 to 0.18 a player by itself**, and the two
  errors add. The shipped command on results of `solve` at its defaults (`check ... --flops all`, 0.05,
  spans 0.16 wide): all flops 0.17 / 0.18; 200 flops 0.17 / 0.18, the same; 50 flops 0.34 / 0.35 (0.17
  and the 0.17 to 0.19 of the table); 10 flops 1.64 / 1.26; and 200 flops solved to 0.02 instead, 0.02 /
  0.00, which is the table's row again from the tool's own result file. So at the default accuracy the
  subset is the smaller error from 100 flops on and equal to it at 50.
- **The control** is the all-flops strategy locked back into its own game: 0.004 and 0.001. That is the
  method's own error (class averaging, the 16-bit rows, the second solve), 20 times under the spans.
- **The 200-flop solve as the reference** gives the same picture, so the study does not need the all-flops
  tree (10 flops 1.379 / 1.029, 25 flops 0.443 / 0.540, 50 flops 0.182 / 0.164, 100 flops 0.046 / 0.028;
  spans 0.08 wide as above): each within 0.05 of the all-flops figure.
- **Classes against combos.** Locking the solver's own 1,326 rows instead of the class averages changes
  no figure by more than 0.002 (10, 25, 50 and 100 flops and the control against the 200-flop reference).
  A chart of full ranges loses nothing measurable against the combo strategy it was read from, also with
  a shipped subset, whose flops do not treat the suits quite alike. A range that names suits is another
  matter: against a big blind holding one combo (`--range-bb AhAs`, 12 bb, three flops) the class rows
  lost 0.29 bb/100 where the solver's own rows lost 0.00 (the review's run on PR #499), and with single
  combos in a range the average can be most of the figure: `--range-sb AhAs --range-bb KK` printed 10.89
  for the big blind where the solver's own combo rows give -0.003, `--range-sb AhAd,KK --range-bb
  AhAs,QQ` 7.79 and 32.34 where they give -7.16 and -1.36 (the second review's runs; 12 bb, three flops,
  where the sums of the two values are far from zero as well). `check` locks classes, which is what a
  chart is; when a range holds a class in part (some of its combos, or combos at different weights) it
  now says that its figure includes that average.
- **What a result does not say.** A decision the opponent never brings about (reach exactly zero) has no
  frequencies in a result. Locking even play there, as the first version of `check` did, hands the free
  opponent a line to walk into: 235 bb/100 "lost" by a big blind whose small blind, holding only aces,
  never limped (12 bb, three flops; the review's run). Leaving every such decision to the solve, as the
  second version did, errs the other way: a file with one class's frequencies removed at a decision had
  the whole decision played for it (a true loss of 10.07 shown as -0.03; the second review's run). The
  rule now: a decision is locked whole or not at all. It is locked when the hands the result is silent on
  cannot show in the printed loss: their share of the range, times the chance the result's own play
  brings them there, times two stacks, added up along the way, is 0.00 bb/100 as printed (they play
  evenly there, which nobody can see). Otherwise it is left to the solve and named, and so is everything
  the hands below it might then do differently; that can only understate the loss. (The stake is two
  stacks and the ante, the most one hand can move a player's result.) Where silence cannot be honest the
  file is refused before anything is solved:
  - no frequencies at the small blind's first decision, or a class the check deals without them there:
    every hand is at the first decision, a class the range holds in part too (the third review's: such a
    file passed, under a note whose reasons cannot hold at the root);
  - a class of the range without them at a first decision that has them for others (no hand of the
    opponent's shares a card with every combo of a class, so a whole class cannot be unmet). A class the
    range holds only in part can be unmet there (the big blind's AhAs when the small blind's only raising
    hand is AhAd), and is not refused;
  - a class with frequencies that the range does not hold;
  - where both ranges hold whole classes at one weight: hands without frequencies at a decision that the
    result's own strategies bring them to. The measure is the one above with the opponent's side in it:
    the hands' share of the deals, times the chance the result's own play brings them there, times the
    chance its opponent's play comes there, times the stake, 0.00 bb/100 as printed or not. The third
    review's case: the big blind's whole decision after the limp emptied in a file whose small blind limps
    78.5 % of the time. The second rule left it to the solve and printed -0.09 under a note that "its
    small blind never goes there"; with that decision played one way instead the same file loses 77.59.
    It also refuses a result changed so that hands go where it has nothing for them (every hand of the big
    blind made to raise the limp, and nothing for most of them against the shove that can follow), which
    the second rule left free and named. It counts a class's combos as playing alike and as meeting any
    hand of the opponent, which is why it is not applied when a range names suits or weights: there a
    combo can be ruled out by the opponent's cards. **How near a result of `solve` comes to it**: 0 in the
    nine result files of this section, and at most 1.1e-6 bb/100 in the review's 22 small games with such
    ranges (three flops; stacks of 5 to 200 bb, an ante, other sizes, no limp, ranges down to one class a
    player), against the 0.005 that refuses; none of the 33 files, the 2 with suits or weights included,
    is refused, and all have the plan they had under the second rule.

  The notes say what was looked at and no more. "Its small blind never goes there" is printed for a
  decision without frequencies when the result's own rows bring the opponent there too rarely to show;
  any other decision left free is listed as one the result has only some hands at, without a reason (the
  second rule's note gave reasons it had not checked). How often the opponent comes is read from the
  opponent's rows on the way, and the third rule read a decision without rows as one the hand stops at:
  with the big blind's rows against the raise emptied in a file as well as the small blind's against the
  shove, in ranges that name suits (where nothing is refused for being silent), it printed "its big blind
  never goes there" over a big blind that shoves 8.85 % of the time (the fourth review's run). A hand
  without rows on the way now passes as it came, so the note is printed only where rows that are there
  say the opponent does not come, and that file gets the list without a reason. The plans do not change
  by this, only which of the two notes a decision left free gets: of 70 results of `solve` in small games
  (the reviews' files) 2 changed note, of 300 more solved for this (random narrow ranges, one to three
  flops, stacks of 3 to 300 bb) 3 did and none was refused, and none of the nine files of this section
  changed. A player none of
  whose decisions is locked gets no figure. An integer no float holds (`10**400` as a frequency) is refused by `check` like any other frequency that
  is no strategy; it was a traceback. (`chart` drew it as `**`, and printed a decision's frequencies of
  `1e308` as "inf %", until the sixth commit: the fifth review's.) None of the result files of this section is touched by any of
  this: all ten decisions are locked, with the same rows as before. They do have hand classes without
  frequencies, 66 to 794 class-and-decision entries a file (32o against the 3-bet of an open it never
  makes): every one a hand the result's own play does not bring to that decision (nine files, read
  again with each rule).
- **Where the two values' sum moves, the loss is that uncertain, not only its span.** A small blind with
  `AA,KK:0.001,72o` against `KK,QQ` (12 bb, three flops, reference to 0.006 bb/100) printed 0.32 (0.31
  to 0.39) for a big blind whose locked rows were the equilibrium's (every KK combo calls 0.883 in
  the solver's own strategy, as the review read it): a true loss of about nothing, in a game worth 0.46.
  That was at the default `--accuracy 0.05`; the same check at 0.02 and at 0.005 printed 0.02 (0.01 to
  0.03) and 0.02 (0.02 to 0.03), so in a game this small the figure moves with the accuracy of the check
  by more than its span, and a span is no bound there. A worse one (the fourth review's): `AA,72o`
  against `AKs`, 12 bb, three flops, one result (solved to 0.005) checked three times: the big blind's
  loss 1.31 (1.30 to 1.41) at 0.05, -0.54 (-0.55 to -0.53) at 0.005, -0.20 (-0.21 to -0.20) at 0.001, the
  small blind's -0.15, -0.16 and 2.03 (2.03 to 2.03), with the sums moving by 1.55, 0.54 and 2.53. The
  reference game itself was worth -9.31 at 0.05 and at 0.005 and -7.13 at 0.001, each with a span 0.01
  wide (the fifth review's reading): the value of the game moved by 200 times its span.
  In the first example the two values added up to -0.42 to -0.02 over the
  three solves; the free opponent of the locked solve does not hold the checked player's value down, it
  pushes its own up, and off all flops those are not the same thing. The check prints the range of the
  sums and now says the losses are that uncertain. With full ranges on three flops the sums moved by
  0.18 (100 bb: small blind -0.21, span -0.24 to -0.07, the review's run); in the one check of this
  section that recorded them, 50 shipped flops with turn bets, by 0.004, and on all flops without an
  ante the two values add up to zero (0.000 chips). Not corrected for: no rule for it that is not a guess.
  With an ante the two values add up to the ante and not to zero, and the sums are taken less it (the
  note used to count an ante of 2 as 200 bb/100 of uneven dealing, on all flops too; the fourth
  review's). Not quite to the ante when the fifth review ran: on all flops the solver's two values came
  out 0.01 to 0.03 % of it short (99.988 of 100 chips at uniform play, 99.970 solved; 999.883 and
  999.689 of 1,000: `--stack 8 --depth none --range-sb AA,KQs,72o --range-bb QQ,T9s,32o --flops all`),
  which was the solver's and not the check's (#505: the weights of the 1,755 flops in their 24 suit
  variants were summed as floats and came out 3.5e-4 over, so every value that went through a flop was
  that much too small, with or without an ante; fixed in #506, and the same game now adds up to the
  ante to 0.001 chips). The figures of this section are from before that fix and were not run again:
  it changes what is won and lost after a flop by 0.035 % in an all-flops game, the references here,
  and by 0.001 % or less on the shipped subsets. On all flops the note no longer blames a subset for
  a sum that is not zero.
- **Rows that do not add up to 1** (#502). `check` used to rescale them without a word: `[0.2, 0.2, 0.2]`
  was checked as thirds, a single 1.5 or 2**70 as 1. It still makes them add up to 1, and now measures
  what that leaves open the way it measures the hands a locked decision is silent on: the class's share
  of the player's deals, times how often the result's own play brings it there, times the part of the
  row that is not as written (what is missing under 1; over 1, what the entries have over 1 with none
  counted as more than 1: two strategies that play no action more than the row says, and none more
  than always, differ by no more than that, so `[1.5, 0, 0]` leaves nothing open and `[1.2, 0.3]`
  leaves 0.3), times the swing. A class's rows at one decision after another add up, to all of the
  class at the most: without that, rows of nonsense at every decision came to 4,604 bb/100 for a
  player whose whole result spans 2,400 (found in the PR's verification). The note comes when the
  figure is not 0.00 bb/100, with the row that counts the most in it (the row furthest from 1 can be
  one the class never comes to); nothing is refused for it, because the measure grows with the stack and
  a refusal would in the end meet a file nobody touched, as the note does: a result of the tool before
  this change gets it at 1200 bb and at every deeper stack measured, and not at 1100 and under, on
  three flops with no bets after the flop (the third non-author run's figures, small blind and big
  blind in bb/100; a player is named in the note from 0.005):

  | Stack, bb | Small blind | Big blind | Note |
  |---|---|---|---|
  | 100 | 0.0001 | 0.0002 | none |
  | 500 | 0.0017 | 0.0028 | none |
  | 1,000 | 0.0024 | 0.0025 | none |
  | 1,100 | 0.0027 | 0.0029 | none |
  | 1,200 | 0.0061 | 0.0084 | both |
  | 1,300 | 0.0045 | 0.0053 | big blind |
  | 1,400 | 0.0039 | 0.0052 | big blind |
  | 1,500 | 0.0052 | 0.0065 | both |
  | 2,000 | 0.0073 | 0.0074 | both |
  | 3,000 | 0.0128 | 0.0153 | both |
  | 6,000 | 0.0185 | 0.0250 | both |

  The figure does not only grow with the stack (1,300 and 1,400 measure less than 1,200): it follows
  which rows a solve's rounding left furthest off.
  The ten results of this section (100 bb, rows as the
  solver's seven digits left them, 1.2e-7 off at the most) measure 0.0002 to 0.0008 bb/100 a player,
  6 to 25 times under the line, and their plans are the same rows as before, entry for entry
  (compared with `_plan` of the commit before). `solve` now divides a class's row by its own sum, so a
  new result's rows add up to 1 as doubles (1e-16) at any stack. The class dealt too rarely to show
  (weight 1e-6) that may be missing at a first decision gets no note, by the same rule: what it can
  move is 0.00 bb/100 or the decision would not have been locked, and the figure is in the report
  (`unsaid_bb100`).
- **The figure of a game too small for it** (#502). Asked for: a loss that does not move with
  `--accuracy` by more than its span in the two small cases above, or the decision that the note is
  what there is. The note is what there is at the tool's level: a loss is the difference of two
  values, the locked solve's and the reference's, and where the two players' values do not add up to
  the same thing at every pair of strategies neither is the value of anything; a best reply computed
  some other way is a reply in the same game. The cause is in the solver's deal, which gives one pair
  of hands two weights, one for each player (`Solver::flop_deal`), and is taken up there (#513).
- **What the fifth review's mutations got past the tests** (seven of its thirteen): no test ran `check`
  with an ante, so the ante could go back into either sum, or out of the stake, unseen; versions 0 and
  -1 were read as results by a `_fits` that refused only "over 1", and `chart` could take any whole
  number; a decision the file has nothing for could pass half its hands on instead of all. Each now
  fails a test: the six run again and five more for the sixth commit's own rules, 11 of 11 stopped by a
  `FAIL:` line with the unchanged copy passing. The seventh was a branch nothing could reach (a report whose ante is
  no number: `check` writes the report itself), now gone. And one traceback was left on a broken file: a
  string holding half a surrogate pair (`"\ud800"`) died at the `print`, after the solves in `check`;
  such a file is now refused when it is read, in one line.
- **A fresh start is the faster one.** The locked solve from `set_equal_strats` took 5 to 6 s where the
  same solve continued from the reference's solved state took 20 to 25 s (50-flop reference; the fresh
  ones to 0.1 bb/100, the continued ones only to 0.5);
  the values agreed within their spans. A strategy read back after `set_strategy` differs from the one
  sent by at most 3e-5 (the 16-bit rows).
- **Time.** On all flops: 930 s for the reference to 0.02 (275 s to 0.5), 370 to 470 s for each locked
  solve, 100 minutes for the whole table; to 0.05, 668 s for the reference and 207 to 287 s for each locked solve, 52
  minutes for the five results of the bullet above. Against 200 flops: 95 s and 36 to 57 s.

**A subset's game is not exactly zero-sum.** `EV OOP + EV IP` is 0.000 on all flops, 0.001 with 200
flops, -0.005 with 100, 0.004 with 50, 0.035 with 25 and -0.034 with 10 (chips; the solves' own figures). `Solver::flop_deal`
divides a hand's value by the weight of the flops the hand can see, averaged over the opponent's hands
by reach; the weight the flops have for the *pair* of hands differs from that average unless every flop
is in the tree. Exact pair weights cannot go through a walk that values a hand against a whole range
at once; not changed.

For the spans this matters as follows. The low end of a value is the solve's sum less the opponent's
best response, which is what the opponent's best reply would leave the player only if the sum did not
move with the strategies. On all flops it does not (0.000; no ante in any solve of this section), and with 200 it is 0.001, so the two tables
above stand as bounds; the depth table below is on 10 flops (0.03 in the reference, against spans 0.8
wide). On a small, uneven subset the span is not a bound: with three flops and 12 bb the sum was 0.82 in
one locked solve and 0.86 in the other, and a loss came out as -0.029 with the span -0.030 to -0.023,
all of it under zero (the review's run). A game that is not zero-sum can also truly reward a known
strategy. `check` prints the sums of its solves whenever they show at two decimals.

**Depth: the flop-depth strategy in the turn-depth game.** The same check with `--depth turn` (bets on
the turn too) on the 10 flops of `wsolver_10.txt`, the smallest shipped subset and the first tried
(802 MB; 5 threads next to another job, 72 minutes for the five solves to 0.2 bb/100). The
strategies under test were solved on the same 10 flops, so the subset costs them nothing here:

| strategy solved with | SB's EV in its own game | small blind gives up | big blind gives up |
|---|---|---|---|
| the turn-depth game itself | 6.92 | | |
| `--depth flop` | 1.9 | 1.36 [1.01, 1.79] | 1.91 [1.58, 2.36] |
| `--depth none` | -7.1 | 7.96 [7.62, 8.40] | 7.51 [7.20, 7.98] |

The same on the 25 flops of `wsolver_25.txt` (2,039 MB by the tool's estimate; next to the tests of a review, 48
minutes for the three solves to 0.2: 1,417 s for the reference, 689 and 767 s locked). The turn-depth
game is worth 6.68 to the button (6.53 to 6.90); the `--depth flop` strategy of the same 25 flops (1.55
in its own game, solved to 0.05) gives up **1.65 [1.32, 2.09]** (small blind) and **2.16 [1.82, 2.58]**
(big blind). So the 10-flop figure was not an effect of the few flops: with 2.5 times as many the loss is
the same or a little more. The reference's two values added up to 0.045 here and 0.030 with 10 flops,
against spans 0.8 wide; the sums of the locked solves of these two tables were not recorded (`check` did
not print them yet), so how far they moved is not known.

And on the 50 flops of `wsolver_50.txt` (4,049 MB by the tool's estimate; next to a review's tests and other
measurements, 93 minutes: 2,290 s for the reference, 1,470 and 1,685 s locked, all to 0.2). The game is
worth 6.56 to the button (6.39 to 6.79); the `--depth flop` strategy of the same 50 flops (solved to
0.03) gives up **1.59 [1.25, 2.05]** (small blind) and **1.75 [1.41, 2.19]** (big blind). The three
solves' values added up to 0.001, 0.003 and 0.005: the sum moved by 0.004, a two-hundredth of the spans.
Three subsets, three overlapping spans: what stopping the bets at the flop costs does not go away with
more flops, and at 1.4 to 2.2 bb/100 it is some ten times what the 50 flops themselves cost at flop
depth (0.19 / 0.17 above). The next step down, the turn-depth strategy in the river-depth game, does
not fit this machine (#498).

- **Each street of betting is worth about 5 to 9 bb/100 to the button** (-7.1, 1.9, 6.9 for none, flop,
  turn), so the games differ far more than their preflop strategies' losses: a strategy solved with no
  postflop bets gives up 7.5 to 8 in the turn-depth game, one solved with flop bets 1.4 to 1.9.
- **The depth is the larger error from 25 flops on.** Not betting the turn costs a flop-depth preflop
  strategy as much as solving on 10 flops instead of all does (1.0 to 1.4), three to four times what 25
  flops cost and seven to twelve times what 50 do. More flops than 50 at `--depth flop` buy accuracy in a game that is
  itself 1.4 to 1.9 away from the one with turn bets, as far as 10 flops show it.
- **Not measured:** what the turn-depth strategy gives up in the game with river bets (86 GB for 10
  flops), which is the same question one street on and may be as large; more flops at turn depth than
  25 (50 are 4 GB here; the PC holds 200); which decisions carry the loss; other stacks and sizes. One
  tree, two subsets, one accuracy (#498).

## The turn sets' byte tables summed on whole numbers (#507)

At turn depth, with tables at both levels (#496), the time is one loop: a row of a turn set's triangle
of signed bytes against the opponent's reaches (`tri_dot` in `Solver::allin_tri_value`). Counted in a
10-flop solve of the tool's tree, it is about 80 % of the threads' time, and 74 % of the opponent hands
it meets have no reach. A float times a byte has to be widened and converted, sixteen bytes to four
vectors, before it is a product.

**What changed.** The reaches of one value are turned once into whole numbers: in units of the largest
of them over `0x7f7f7f`, rounded, and each written as three signed digits of base 256
(`src/reach_digits.hpp`). A row's sum is then three sums of byte times byte, sixteen products to an
instruction (ARM `SDOT`), put together as `a + 256 b + 65536 c`: exact on the whole numbers, so all the
error is the rounding of the reaches, half a unit each, 2^-24 of the largest. Reaches with none above
zero, one below, one that is not a number, or a float sum without end have no such form and are summed
in float as before. The
other half of a row (the row times its own hand's reach, added into the sums of the hands before it)
is still float, and is skipped for a hand without reach as before. Flop sets (16-bit entries) and the
full tables are untouched. On when the build has the instruction (AArch64 with `dotprod`: every Apple
M-series), float elsewhere; `-DPIO_NO_ALLIN_DIGITS` builds the float sum here too, and
`Solver::allin_digits` chooses in a test.

M4, 10 threads, the machine quiet but for the runs themselves.

**One evaluation** (a scratch program on `Solver::showdown_value`, one flop of the tool's tree at turn
depth, random reaches, best of 15 rounds, microseconds a value):

| opponent hands without reach | turn set: float | whole numbers | | flop set (unchanged), the two builds |
|---|---|---|---|---|
| 0 % | 81.6 | 57.9 | 1.41x | 87.1 / 87.2 |
| 30 % | 70.3 | 49.5 | 1.42x | 75.9 / 75.7 |
| 74 % | 55.2 | 35.4 | 1.56x | 59.0 / 59.0 |
| 90 % | 49.5 | 29.4 | 1.68x | 52.1 / 52.0 |

The two ways differ by 2.7e-7 to 3.8e-7 of the largest value here; the non-author run of PR #512
measured 4.1e-7 with 90 % of the hands without reach and 3.0e-6 with one reach far above all the
others. Those are measurements and no bound: the bound is half a unit a term, relative to the largest
reach, so a reach under 2^-24 of the largest counts as none in the sums. The gain grows with the hands without
reach because the float half that is left shrinks with them.

**Solves** (old = `3fea596`; whole command = build + `go N steps` + `calc_results`; taken in turn, old
after new):

| tree | old | new |
|---|---|---|
| 10 flops, turn depth, 100 steps | 93.5, 92.9 s (911, 910 s of processor); 992, 1,004 MiB | 68.6, 67.3 s (656, 658 s); 1,002 MiB |
| 200 flops, flop depth, 60 steps | 14.9, 14.9 s | 14.9, 15.0 s |

1.37 times as fast at turn depth (1.39 in processor time, which is what 80 % of the time at 1.56
gives), and nothing at flop depth, where the sets are flop sets. After the 100 steps: EV OOP -6.897 old
and -6.899 new, exploitable 0.624 and 0.626.

**Does the rounding cost convergence?** The 10-flop tree, 300 steps, the exploitable figure of
`calc_results` along the way. A solve is the same to the last digit from run to run, so the rows differ
by what they sum and nothing else:

| steps | 25 | 50 | 75 | 100 | 150 | 200 | 300 |
|---|---|---|---|---|---|---|---|
| float (`-DPIO_NO_ALLIN_DIGITS`; `3fea596` is the same at every report) | 19.995 | 3.649 | 1.291 | 0.624 | 0.248 | 0.128 | 0.054 |
| whole numbers | 19.992 | 3.685 | 1.304 | 0.626 | 0.249 | 0.129 | 0.054 |
| whole numbers, and the flop sets' float sum taken in 8 parts for 16 | 19.993 | 3.651 | 1.289 | 0.624 | 0.248 | 0.129 | 0.055 |
| units of the largest over `0x7f7f7e` (one fewer) | 19.997 | 3.711 | 1.319 | 0.632 | 0.249 | 0.129 | 0.054 |
| over `0x3f7f7f` (half as fine) | 19.995 | 3.670 | 1.293 | 0.624 | 0.249 | 0.128 | 0.054 |
| over `0x7f7f` (256 times as coarse, two digits) | 20.003 | 3.721 | 1.325 | 0.633 | 0.250 | 0.130 | 0.055 |

The third row is the measure of what an order of summing does: a float sum elsewhere in the same solve,
taken in another order, moves the figure at 50 steps by as much (0.034) as the whole numbers moved it
(0.036), and a grid one unit different moves it by 0.026 more. So nothing here tells the whole numbers
from the floats, and a grid 256 times as coarse costs 0.001 at the end. EV OOP after 300 steps: -6.885
float, -6.884 whole numbers.

**Do the tests notice?** `tests/test_solver.cpp` checks the header alone (every sum of the instruction
path and of the plain loop against the whole numbers summed in 64 bits, every reach within half a unit,
rows of every length from 1 to 300, the 32 longest and thirteen between, the forms refused) and the
solver with the switch on and off (to 2e-6 of the values, and every value again while many threads
take theirs at once). 23 changes to the kernel and its use, one at a time, against
`build/test_solver`; the last seven are the two reviews'. The first review's five, and the one part in
100,000 before them, the tests as first written (twenty row lengths, 2e-5, one thread) did not notice
or noticed only in the solver's comparison; the second review's two were found with every length to
130 and twenty longer ones:

| change | noticed by |
|---|---|
| a reach cut to its whole number, not rounded | the header's checks |
| the unit one part in 8 million small | the header's |
| the unit one part in 10,000 large | both |
| the end of a row (under 16 bytes) left out | both |
| the middle digit counted 255, in the instruction path; in the plain loop | the header's (both) |
| the second block of a round meets the first block's row | both |
| the sixteen digits to spare not cleared | the header's |
| a negative reach taken | both |
| a reach that is not a number taken; reaches with none above zero taken | the header's (both) |
| the solver ignores its switch; adds the float sums on top; never uses the whole numbers | the solver's (all three) |
| the solver's sum one part in 10,000 large | the solver's |
| the solver's sum one part in 100,000 large | the solver's (not noticed at 2e-5) |
| one set of whole numbers shared by all threads | the solver's |
| the last byte of a row's end dropped when the end is 5, 6 or 9 to 12 bytes | both |
| no lowest digit for rows 101 to 1,126 long; in the last block of those rows only | both; the header's |
| the float half of a row skipped for a hand whose reach is under half a unit | **not noticed**: the tests' reaches have no such hand (counted: none), so it does nothing there |
| no lowest digit for rows 131 to 199 long | the header's (nothing, with no length between 131 and 199) |
| the last byte of an end of 5 bytes dropped, in rows over 130 only | both (the solver's only, before) |

What the comparison in the solver does not see, by the second review's measurements: its sum one part
in a million large (1.2e-6 of a value; one in 500,000 is seen), the lowest 12 bits of its sum dropped
(1.0e-6; 16 bits are seen), and the float half skipped for reaches under 3e-5 of the largest (1.0e-6;
1e-4 is seen). The 2e-6 is 4.7 times the largest difference the review produced with a correct kernel
(4.25e-7 with `-ffp-contract=off`; 3.2e-7 as built, 20 other seeds 3.0e-7 to 3.5e-7), and it belongs
to the test's reaches: one reach far above the others gave 3.0e-6 in the first review, so another
shape of reaches needs its own figure. The check of many threads at once says nothing on a pool of one
thread, and a race with a narrow window can pass a run: a scratch array made static gave between 0
and 492 values off a spot, and one spot of five none, in one run of three.

**Not done.**
- x86. AVX2 has byte products only as unsigned times signed, pairs added with saturation; VNNI has the
  instruction. Neither is written, and there is no x86 machine here to time one on, so x86 keeps the
  float sum (#511).
- The half of a row that is still float (taken for the hands with reach only). How the time that is
  left splits between it and the three sums was not measured.
- The flop sets' 16-bit rows, which are the time at flop depth (the 200-flop row above).
- The solver's comparison of the two ways is to 2e-6 of the values (the two differ by 3.2e-7 at the
  most in the test, and on ordinary reaches a float sum is within 2e-6 of the same sum in doubles): an
  error in the solver's use of the kernel smaller than that passes. The kernel's own sums are checked
  exactly, at the row lengths the header's test has: 1 to 300, thirteen between, and 1,295 to 1,326.
  At the other lengths up to 1,127 (the longest row a solve or the solver's test has) only the 2e-6
  comparison looks, and a lowest digit lost at some of them passes it (found in the third non-author
  run: rows 301 to 410, or length 412 alone); from 1,129 to 1,294 nothing looks, and no solve has a
  row that long.
- Nothing pins how much of the work takes the new path: whole numbers for one player only, or for half
  of the rows, pass every test. That is speed and not values; the timings above are the check.
- One tree, one machine. To an accuracy with the tool was not timed; by the steps above it is the
  same number of steps.
