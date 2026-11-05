# Memory bandwidth: is the solver bandwidth-bound, and should 16-bit storage be the default? (#55)

Baseline measurements behind the compact-storage / cache-blocking ideas in #55. Related:
#49 added the 16-bit `small_strats` mode; this doc decides when it is the *fast* mode,
not just the small one.

## Method

Two storage modes, A/B'd with `bench/run.py --pre-build` (inserts `small_strats on|off`
before `build_tree`; the 25bb fixture's built-in `small_strats on` is overridden when
`--pre-build small_strats ...` is given):

- **float32** (`small_strats off`): regrets and strategy sums stored as float32,
  8 bytes per (node, action, hand) slot.
- **16-bit** (`small_strats on`): int16 regrets + uint16 strategies with a per-node
  scale, 4 bytes per slot. Lossy (~1e-4 in strategies), so iteration counts can drift a
  little from the float32 trajectory; exploitability targets are unchanged.

Single thread, `set_info_freq 5`, targets down to 0.05% of pot, two interleaved
repetitions per mode (f32, 16b, f32, 16b) to cancel machine drift. The machine is a
shared Apple M4 (16 GB), so seconds are indicative; iteration counts are deterministic
per mode. Solver builds measured: flop_srp_4 on main @ ebc914e (DCFR 1.3/0/4, pruning
on); the 25bb fixture on main @ 7fe4372 (DCFR 1.3/0/4, pre-pruning — its absolute
throughput changed with #86; the policy conclusion did not).

### Traffic estimate

`storage_floats` counts the per-array slots (regrets array + strategy array, each of
that size; `memory_bytes` = 2 × storage_floats × bytes-per-value). A lower bound on the
node-storage traffic of one iteration is:

- `current_strategy` reads regrets once as the traversing player's own strategy and
  once as the opponent's during the two walks of `iterate` (≈2 reads),
- the CFR update reads and writes regrets (1 R + 1 W),
- the strategy sum is read and written (1 R + 1 W),

i.e. ≈ 5 slot-passes per iteration ≈ `storage_floats × 5 × value-bytes` — 20 B/slot in
float32, 10 B/slot in 16-bit — plus per-street evaluator arrays (reach/cfv) that live
outside node storage and are comparable in size on river-heavy trees. Reported GB/s
uses the slot-pass model only, so it is a lower bound on achieved bandwidth.

## flop_srp_4 (307 MB float32, 38.4M slots), 1 thread

Solver: main @ ebc914e (DCFR 1.3/0/4, pruning on). The same A/B on main @ 7fe4372
(DCFR 1.3/0/4, pruning off) gives the same iteration counts (80 / 145) with 16-bit
~10% slower per iteration — the conclusions below are not version-sensitive.

| mode | iters | wall s | iters/s | model GB/s | to 0.5% | to 0.2% | to 0.05% | peak RSS |
|---|---|---|---|---|---|---|---|---|
| float32 #1 | 80 | 20.84 | 3.84 | 2.95 | 9.10 | 12.93 | 19.88 | 306.9 MB |
| float32 #2 | 80 | 20.48 | 3.91 | 3.00 | 8.98 | 12.74 | 19.53 | 306.9 MB |
| 16-bit #1 | 145 | 36.95 | 3.92 | 1.51 | 9.73 | 13.89 | 34.87 | 162.1 MB |
| 16-bit #2 | 145 | 36.81 | 3.94 | 1.51 | 9.75 | 13.89 | 34.74 | 162.1 MB |

Two separate effects, both arguing for float32 on a tree that fits:

- **Per iteration, halving the bytes buys nothing**: 254 ms vs 258 ms per iteration —
  statistically identical. The traffic model says a bandwidth-bound loop would speed
  up ~2×; instead the run moves ~1.5–3 GB/s on a machine that can stream ~120 GB/s.
  **The solver is compute-bound**, with or without pruning (before #86, 16-bit was
  ~10% *slower* per iteration from the quantize arithmetic; pruning trims compute
  roughly in proportion for both modes, leaving a wash).
- **Worse, quantization degrades convergence**: identical settings reach 0.05% of pot
  in 80 iterations in float32 but 145 in 16-bit (1.81×). Small instantaneous regrets
  round to zero under the per-node int16 scale, so information is lost every
  iteration. Net: to 0.05% of pot 16-bit is 1.77× slower end-to-end; to 0.2% it is
  ~9% slower.

## 25bb fixture, 1 thread

The 25bb fixture (wide ranges, pot 40, QhQc2c) is 6.44 GB in float32 and 3.25 GB in
16-bit; `small_strats auto` picks 16-bit on this 16 GB machine (float32 would exceed
90% of available memory). Even so, float32 built and ran resident (6.67 GB peak RSS,
no swapouts) for an honest same-machine comparison. Neither mode reached the targets
within the 900 s per-run cap, so the comparison is iteration throughput and
exploitability at the cap — the regime a user of this machine is actually in.

| mode | iters | wall s | iters/s | model GB/s | reached 1%/0.5%/0.2% | final expl | peak RSS |
|---|---|---|---|---|---|---|---|
| 16-bit | 122 | 926.2 | 0.132 | 1.07 | none (900 s cap) | 0.472 | 3325 MB |
| float32 | 135 | 923.6 | 0.146 | 2.35 | none (900 s cap) | 0.446 | 6516 MB |

Consistent with flop_srp_4: float32 does ~11% more iterations in the same wall time
(dequantize arithmetic again costs 16-bit more than the traffic saves), **and**
converges to better exploitability at the cap (0.446 vs 0.472 chips on a pot of 40)
because its regrets are unquantized. Model bandwidth stays at ~1–2.4 GB/s, so the
3 GB tree is no more bandwidth-bound than the 300 MB one.

## Conclusions

**The solver is compute-bound, at every tree size measured.** Achieved node-storage
bandwidth is ~1–3 GB/s against an Apple M4 that streams ~120 GB/s — two orders of
magnitude of headroom. Halving the storage format's bytes buys nothing per iteration
(254 vs 258 ms on current main; before #86's pruning it was a small *loss* — 16-bit
ran ~10% slower per iteration once its quantize/dequantize arithmetic was paid),
exactly as a compute-bound loop should behave.

**16-bit quantization also slows convergence.** Small instantaneous regrets round to
zero under the per-node int16 scale, so information is lost every iteration:
1.81× the iterations to 0.05% of pot on flop_srp_4, and worse exploitability at equal
wall time on the 25bb tree. Smaller formats (fp16/bf16 regrets, 8-bit strategy sums)
share the mechanism and would make this worse, not better.

**The default storage policy is already right.** `small_strats auto` keeps float32
whenever the tree fits in 90% of available memory — which is also the fast mode — and
falls back to 16-bit only when float32 genuinely does not fit, where it is the
difference between solving slowly and not solving at all. No change to defaults.

**Prefetch, cache blocking, and street-contiguous layouts target a bottleneck this
profile does not show.** They become worth their complexity only if per-slot
arithmetic drops far enough to expose the memory system — e.g. after SIMD kernels
(#54) or regret-based pruning (#51). Revisit then with this same A/B.

Acceptance numbers (issue #55): 25bb fixture, 1 thread, 900 s cap — 0.132 iters/s at
1.07 GB/s model bandwidth and 3325 MB peak RSS in 16-bit (the mode this machine must
use), vs 0.146 iters/s at 2.35 GB/s and 6516 MB in float32; exploitability unchanged
in kind (both far above the 1% target at the cap, 0.47 vs 0.45 chips of a 40-chip
pot). flop_srp_4 at equal accuracy: 0.2% of pot in 12.8 s float32 vs 13.9 s 16-bit;
0.05% in 19.7 s vs 34.8 s.

## Update (#272, 2026-11-05): the 16-bit floor was rounding the strategy sums; stochastic rounding removes it

This doc is from an M4 at 1 thread, before the SIMD kernels. On the owner's Zen 5 PC at 32 threads
(WSL, `bench/run.py`, two interleaved reps), things look different:
- **It is bandwidth-bound.** Full 16-bit is 37% faster per iteration than float32 on threebet_mono.
- **16-bit no longer costs iterations on the small bench trees.** flop_srp_4 reaches 0.05% of pot in
  80 iterations either way.
- **What remained was a floor, on threebet_mono.** 16-bit got to 0.1% of pot, then stalled at
  0.71 chips (0.35%), and needed #182's switch to float32: 48 s to 0.05% under `auto`.

**The floor came from the strategy sums, not the regrets.** Splitting the two halves shows it:
- float32 regrets with 16-bit sums also stall, at 0.86 chips;
- int16 regrets with float32 sums reach 0.05% in 435 iterations, against 450 for float32.

The sums are discounted and added to every iteration. Late in a solve, a low-reach hand's increment is
below its node's 16-bit step (1/65535 of the node's largest sum). Rounded to nearest, it is lost
every time.

**Stochastic rounding fixes it.** The sums are now committed as floor(x + u), with u uniform in [0, 1)
from a hash of the slot and the iteration. Every increment then counts on average, and results stay
identical for any thread count. Seconds to 0.05% of pot, 32 threads, two reps:

| tree | float32 | 16-bit, nearest | 16-bit, stochastic | int16 regrets + f32 sums |
|---|---|---|---|---|
| threebet_mono | 13.9 (450 it) | never (0.71 chips) | **9.0 / 9.3** (440 it) | 10.9 / 11.0 |
| flop_srp_4 | 1.39 | 1.00 | 0.98 / 1.01 | 1.10 |
| deep | 2.82 | 2.30 | 2.25 / 2.23 | 2.40 |
| flop_srp_2 | 0.66 | 0.53 | 0.52 / 0.52 | 0.57 |
| flop_3bet | 0.50 | 0.51 | 0.48 / 0.52 | 0.49 |

Other measurements:
- **srp_big (13 GB, `auto`).** 71.5 s to 0.5% of pot either way. To 0.3%, 86.7 s against 88.6 s
  (225 iterations against 230).
- **Cost.** The hash costs 1.5-2.5% at 1 thread (deep to 0.1% of pot: 15.2 s against 15.0 s), the
  only place it shows.

**Apple silicon (#293, M4, 10 threads, threebet_mono forced to 16-bit, two interleaved reps).** Rounding
to nearest stalls there too: 0.1% of pot in 14.5 s, and 0.05% not in 1,740 iterations. The per-slot hash
reaches 0.05% in 17.8 s (470 iterations) but costs ~9% per iteration; a fixed table of uniforms read at a
hashed offset costs ~2% and reaches it in 16.7 s, against 17.0 s (515 iterations) for float32. So every
build now rounds the sums stochastically, with the table. On deep and flop_srp_4 it is within 1% of
nearest rounding at every target down to 0.02%. Apple silicon still uses 16-bit only when float32 does
not fit, but those trees now converge instead of stalling.

The separate int16-regrets layout was dropped: it streams 1.5x the bytes of full 16-bit (srp_big
93 s against 71 s to 0.5%), and stochastic rounding makes it unnecessary.
