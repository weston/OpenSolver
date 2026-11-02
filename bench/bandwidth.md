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
per mode.

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

| mode | iters | wall s | iters/s | model GB/s | to 0.5% | to 0.2% | to 0.05% | peak RSS |
|---|---|---|---|---|---|---|---|---|
| float32 #1 | 80 | 20.80 | 3.85 | 2.95 | 9.35 | 12.85 | 19.88 | 306.9 MB |
| float32 #2 | 80 | 20.63 | 3.88 | 2.98 | 8.69 | 12.35 | 19.66 | 306.9 MB |
| 16-bit #1 | 145 | 41.38 | 3.50 | 1.35 | 9.93 | 14.42 | 39.03 | 162.1 MB |
| 16-bit #2 | 145 | 41.14 | 3.53 | 1.35 | 9.83 | 13.99 | 38.77 | 162.1 MB |

Two separate effects, both against 16-bit on a tree that fits in float32:

- **Per iteration, halving the bytes buys ~nothing**: 285 ms vs 259 ms per iteration
  (16-bit is ~10% *slower* per iteration — the quantize/dequantize arithmetic costs more
  than the saved traffic brings back). The traffic model says a bandwidth-bound loop
  would have sped up ~2×; instead the run moves ~3 GB/s on a machine that can stream
  ~120 GB/s. **The solver is compute-bound**, and stays compute-bound until per-slot
  work drops by an order of magnitude.
- **Worse, quantization degrades convergence**: identical settings reach 0.05% of pot in
  80 iterations in float32 but 145 in 16-bit (1.81×). Small instantaneous regrets round
  to zero under the per-node int16 scale, so information is lost every iteration. Net:
  to 0.05% of pot 16-bit is 1.96× (~2×) slower end-to-end; to 0.2% it is ~12% slower.

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
magnitude of headroom. Halving the storage format's bytes changes nothing per
iteration (16-bit is ~10% *slower* per iteration once its quantize/dequantize
arithmetic is paid), exactly as a compute-bound loop should behave.

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
pot). flop_srp_4 at equal accuracy (0.2% of pot): 12.6 s float32 vs 14.2 s 16-bit.
