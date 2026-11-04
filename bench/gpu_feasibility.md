# GPU solving: feasibility analysis (#58)

**Recommendation: no-go for now.** Revisit after #53 (CPU thread scaling), #54 (SIMD) and #57
(showdown). The reasoning is below, and it is mostly about the CPU having 2-3x of unexploited
headroom rather than about the GPU being unsuitable.

This is the design-note half of #58. **The prototype river kernel is not written**, so the issue
should stay open for it; what follows is the measurement and design work that determines whether
writing it is worth anyone's time.

## Measured starting point

All on this machine (M1 Ultra, 20 CPU cores, 64 GPU cores, unified memory), `flop_srp_2`,
wall-clock, isolated with a two-point slope so startup and tree build are excluded:

```
0 iterations:  0.04 s        <- build + startup
80 iterations: 10.13 s
               ----------
per iteration: 0.126 s single-threaded
```

Tree and range shape: 61,114 nodes (365 chance, 25,906 terminal), 498 hero hands, 322 villain hands,
155 MB of strategy storage.

From the profile in #57, `showdown_board` is **58.9%** of that on this tree (49-65% across the
postflop suite), so about **0.074 s per iteration** is showdown evaluation. Each entry-visit does
roughly three flops and three L1/shared accesses, which puts the single-thread showdown throughput
in the order of **300-600 M entry-visits/s** (the range is because the terminal-node count mixes
folds, showdowns and all-ins, and only showdowns run this kernel).

## What is favorable

- **Unified memory removes the usual blocker.** On Apple Silicon there is no PCIe copy: the GPU can
  read the strategy arrays in place. 155 MB (307 MB for `flop_srp_4`) fits comfortably. On a
  discrete CUDA card the tree also fits in VRAM, but strategies are rewritten every iteration, so
  the *whole* CFR loop would have to live on the GPU — a host-device round trip per iteration would
  erase any gain.
- **Parallelism is abundant at the right granularity.** Rivers are independent: 44-45 river cards per
  turn, 47-48 turns, times the betting nodes. That is thousands of independent showdown evaluations
  per iteration — plenty to fill 64 GPU cores without needing parallelism *within* one board.
- **The kernel is small and self-contained.** `showdown_board` takes a precomputed strength ordering
  and a reach vector and writes counterfactual values. It has no allocation, no recursion and no
  branching beyond one group loop.

## What is hostile

- **Low arithmetic intensity.** This is a scatter-add and prefix-scan workload, not a GEMM. Roughly
  three flops per three memory touches. GPUs win on arithmetic intensity; here the only lever is raw
  thread count, so expect a speedup far below the ~21 TFLOPS/800 GB/s headline ratios.
- **A serial dependency inside each board.** The group loop is a scan over strength groups carrying
  `cum` and a 52-wide per-card accumulator `cs[]`. Within a group the opponent additions commute, but
  groups must be processed in order. That is a segmented scan with a barrier per group, not a flat
  map.
- **`cs[]` placement decides the design.** 52 floats is 208 bytes. Per-thread that spills to private
  (device-backed) memory on Apple GPUs and would be slow, so the mapping has to be *one threadgroup
  per board* with `cs[]` in threadgroup shared memory and threads cooperating within a group — not
  one thread per board, which is the tempting first attempt.
- **Bit-stability is lost.** A different reduction order changes results. AGENTS.md requires results
  stay bit-for-bit stable unless a change is explicitly algorithmic, and `tests/test_upi.py` has
  exact-match assertions that would need tolerances. That is a product decision, not a bug, but it
  has to be made deliberately.
- **Two backends, permanently.** Metal for macOS and CUDA for Linux, both shadowing a CPU path that
  must keep working. That is a large standing maintenance surface for one hot function.

## Why "not yet" rather than "no"

The decisive argument is opportunity cost. From `bench/results.md`, CPU scaling on this 20-core
machine is **6-8x**, and on the smallest trees threading is a net *loss* (the preflop tree is slower
on 20 threads than on 1). So the existing parallelism is not saturated: there is roughly 2-3x
available from #53 alone, on every platform, with no new backend and no loss of bit-stability.
Spending that effort on a GPU backend first would be optimizing the harder axis while the easier one
is still open.

Revisit when: CPU scaling is near-linear, SIMD (#54) has taken what it can from the inner loops, and
showdown is still the dominant cost. At that point the ceiling is clearer and a prototype is worth
building.

## Update: the toolchain is fine, the recommended mapping is not (measured)

Three things checked on this machine since the above was written. Together they **strengthen the
no-go**, and one of them invalidates the design this document recommends.

### 1. The CPU bar is roughly 18x higher than the 556 M figure above

Re-measured on `flop_srp_2` with a two-point slope (0 vs 40 iterations, 3 runs each, so startup and
tree build cancel):

```
 1 thread:   0.0925 s / iteration
20 threads:  0.00662 s / iteration      (14.0x speedup)
```

Re-sampled the showdown share rather than reusing the 58.9% above, because #151, #164 and #209 all
targeted showdown since. It did **not** drop — `sample` puts `showdown_board_scalar` +
`showdown_value` at 2,864 of 4,433 non-waiting samples, about 65%, and that is an upper bound
because the listing truncates frames under 5 samples. Call it 59–65%.

At 41,265,447 entry-visits/iteration that gives:

```
 1 thread:   ~690-760 M entry-visits/s
20 threads:  ~9.6-10.6 G entry-visits/s
```

**Any GPU prototype has to beat ~10 G entry-visits/s to be worth having on this machine**, not the
556 M single-thread number quoted earlier. The CPU work since this document was written moved the
goalposts a long way.

### 2. The runtime-compile path works — the GPU was never tooling-blocked

Confirmed by building against the Metal framework and calling `newLibraryWithSource:options:error:`:
the shader compiles at runtime with only Command Line Tools installed, no offline `metal` compiler
and no full Xcode. Device limits on the M1 Ultra: 1024 max threads per threadgroup, **32,768 bytes**
of threadgroup memory, `threadExecutionWidth` 32, unified memory yes.

So "#58 needs Xcode" is wrong and should not be used as a reason to defer it.

### 3. Float atomics do not exist in `threadgroup` memory, which breaks the mapping above

The design in this document says to put `cs[52]` in threadgroup shared memory and have threads
cooperate within a strength group. That requires accumulating floats into `cs[]` from many threads.
Metal rejects it:

```
error: no matching function for call to 'atomic_fetch_add_explicit'
note: candidate not viable: cannot pass pointer to address space 'threadgroup'
      as a pointer to address space 'device' in 1st argument
```

`atomic_fetch_add` for `float` is **`device`-address-space only**. Threadgroup float atomics are not
available, so the recommended mapping cannot be written as specified. The options are all worse:

- **Card-parallel** (thread `c` owns `cs[c]`, scans the group's opponent entries, adds where
  `c1==c || c2==c`). Needs no atomics and **compiles** — verified. But every opponent entry is then
  read by all 52 threads, roughly **52x the memory traffic** of the CPU's single read. For a kernel
  this document already characterises as throughput-bound on memory rather than arithmetic, that is
  the wrong direction.
- **Device-memory atomics**: works, but moves the hottest accumulator off-chip.
- **Per-thread private partials then reduce**: 52 floats per thread spills to device-backed private
  memory, which is the problem this document already flagged.

None of these is fatal on its own, but combined with a 10 G entry-visits/s bar on a gather-bound
scan, the expected outcome is clearly negative. Revisit only if CPU scaling saturates (#53) and
somebody has a mapping that avoids both the atomics gap and the read amplification.

## If someone does build the prototype

Spec for the measurement #58 asks for, so it does not have to be re-derived:

- **Mapping:** one threadgroup per river board; `cs[52]` and `cum` in threadgroup memory; threads
  split the hero and villain entry ranges; one barrier per strength group.
- **Baseline:** `showdown_board` on one CPU thread and on 20, same boards, same reach vectors. Report
  entry-visits/s for all three.
- **Batch size:** feed thousands of boards in one dispatch. A single board will lose to the CPU on
  launch latency alone and would be a misleading measurement.
- **Toolchain note:** this machine has the Metal *framework* but not the offline `metal` shader
  compiler, which needs full Xcode rather than Command Line Tools. Compile the shader at runtime from
  a source string via `newLibraryWithSource:options:error:`, which needs no extra tooling.
- **Honesty check:** compare against the CPU at the same precision and verify the GPU result matches
  the CPU within float tolerance on a fixed input before timing anything.

## 2026-11-03: the 2026 GPU-CFR papers

The owner asked whether two recent papers change this: Kim & Sandholm, "Parallelizing Counterfactual
Regret Minimization" (arXiv:2605.14277), and Li & Huang, "GPU-CFR" (arXiv:2609.11923). Neither
changes the update rule. Both run ordinary CFR / CFR+ / DCFR level by level over a generic game tree
(sparse matrix-vector products; GPU-CFR also compiles the tree to static index arrays and replays
one iteration as a CUDA graph). Their speedups (up to ~10^4x, 30-80x) are against OpenSpiel and
LiteEFG, which enumerate every hand pair at a showdown. Range-vectorised poker solvers (this one,
Pio) evaluate a showdown in time linear in the hands per player, which the generic formulation
cannot do.

Their poker instances are small: 100 and 12 hands per player for the river and turn subgames, and a
full-range Libratus endgame (96,159 infosets, 36.1M states) at 13.9 ms per iteration on an A100. For
scale, a full-range river spot here (58 nodes, ~1,080 hands per player, KhQs7d4c2s) runs at 0.19 ms
per iteration on one M4 core. That spot is several times smaller than the endgame, so this is not
like for like, but the gap is far larger than the size difference.

What carries over is the schedule, not the code: a GPU port of this solver would walk the public
tree one depth level at a time with every node's range vectors batched (strategies and reaches
top-down, all terminals of all runouts in one dispatch, values and regret updates bottom-up), with
regrets and strategy sums resident on the device. The hardware available to the project at that
point: an AMD RX 9060 XT (16 GB GDDR6, ~320 GB/s, no CUDA; OpenCL and Vulkan drivers present, no
ROCm) next to a 9950X on dual-channel DDR5-4800, and the M4's integrated GPU on the CPU's own
120 GB/s. Not pursued further (owner, 2026-11-03).

