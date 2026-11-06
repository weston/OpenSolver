# WSolver vs PioSOLVER on the same machine

Black-box comparison (UPI only, no Pio internals) on the owner's PC: AMD Zen 5, 16 cores / 32 threads,
Windows 11. WSolver is a native Windows build (`make windows WIN_ARCH=-march=znver5`); Pio is
PioSOLVER 3.10 (`PioSOLVER3-edge-znver5.exe`). Both are driven through `bench/run.py --exe
tests/pio_compare/remote_exe.py`. Times are seconds until the first progress report at or below
0.2% of the starting pot. Pio reports progress only every ~0.5 s, so its sub-second times are upper bounds.

## 2026-11-02, after #144-#147

| tree | WSolver 1 thread | Pio 1 thread | WSolver 32 threads | Pio 32 threads |
|---|---|---|---|---|
| deep | **10.05** | 10.77 | **1.08** | 1.10 |
| flop_srp_4 | **5.79** | 13.11 | **0.64** | 1.15 |
| flop_srp_2 | **2.87** | 6.24 | **0.30** | ≤ 0.58 |
| flop_3bet | **1.28** | 1.85 | **0.15** | ≤ 0.54 |

WSolver: main at 4b1c078 (#147). Pio numbers are from the morning run (#50), on the same machine and trees.

## Where the morning's 32-thread gap went

The morning run (DCFR retune #72 in place) had WSolver at 13.50 / 8.62 / 3.76 / 1.50 s single-threaded but
2.67 / 1.50 / 0.62 / 0.48 s at 32 threads: at about 5x scaling to Pio's 10x. Since then:

- #144 (Codex's #69, rebased): nested parallel loops stop publishing work once enough is exposed
  (1.5-2.8x at 32 threads).
- #145: sibling actions above the river run in parallel, as width-neutral tasks (+14-16% at 32 threads
  on the big trees; 32 threads no longer slower than 16).
- #146: the exploitability check fuses its Evaluate and BestResponse walks (one check cost about two
  iterations; now about one). Bit-identical results.
- #147: `avg_delay 32` by default with a retroactive reset (cleaner average; never empty).
- Defaults changed by other agents in between (#131 prune_period).

## Notes

- Per iteration, WSolver and Pio are now about even on deep; Pio needs fewer iterations there (85 vs 100).
- At 32 threads the PC is memory-bandwidth bound: 16-bit storage (`small_strats on`) makes an iteration
  19% faster there (deep: 0.406 -> 0.329 s per 40 iterations), but it needs about 1.8x the iterations (#55),
  so float32 stays the default.
- Scaling curve (deep, 40 iterations, #144 build): 1 -> 16 threads 8.0x, flat from 16 to 32.

## Realistic trees (2026-11-02, later)

The bench trees above are small (40-307 MB). Two realistic study trees, now in `bench/trees/`
(`threebet_mono`, and `srp_big` behind `--include-big`), told a different story at first:

| tree | WSolver before #161/#162 | WSolver after | PioSOLVER 3.10 |
|---|---|---|---|
| srp_big (13 GB, 710 lines), to 1% of pot, 32 threads | 446 s | **70.4 s** (140 iterations) | 70.5 s |
| threebet_mono (542 MB), to 0.3% of pot, 32 threads | 5.39 s | **4.29 s** (170 iterations) | 6.68 s |

Two fixes: #161 (width-neutral scheduler jobs flooded the pool on trees with many turn decisions:
32 threads ran 2.6x slower than 16) and #162 (beta 0 -> 0.5 after iteration 50: with beta 0 the
average oscillated and stalled near 1% of pot on the big tree). Use these trees when tuning.

### Update after #164-#168 (same session, back to back)

The PC ran ~6-15% slower this session than for the table above (Pio too: 74.8 s vs 70.5 s on srp_big),
so both solvers were re-run back to back. 32 threads, target 0.6 chips; WSolver with the viewer's
`set_info_freq auto` (#165):

| tree | WSolver main (c893fc8) | PioSOLVER 3.10 | ratio |
|---|---|---|---|
| srp_big (13 GB, 16-bit) | **52.1 s** (127 iterations) | 74.8 s | 1.44x |
| threebet_mono (542 MB) | **3.47-3.78 s** (168 iterations) | 7.66 s | ~2.1x |

What moved since #162: vectorised max reductions in the regret update (#164), adaptive exploitability
checks (#165), river fold values taken from the showdown kernel for both players (#166, #168).
Per-iteration convergence is close to Pio's on these trees (threebet_mono, exploitability every 40
iterations: Pio 20.0 / 3.99 / 1.33 / 0.625 / 0.42 / 0.30 vs ours 11.9 / 3.02 / 1.22 / 0.634 / 0.44 / 0.31),
so the lead is per-iteration speed.

### Update after #180-#185 (2026-11-03, same session, back to back)

32 threads, target 0.6 chips; WSolver main 0efec0e with `set_info_freq auto`:

| tree | WSolver | PioSOLVER 3.10 | ratio |
|---|---|---|---|
| srp_big (13 GB, 16-bit) | **42.3 s** (123 iterations, 0.597 chips) | 70.2 s (0.509) | 1.66x |
| threebet_mono (542 MB) | **3.08 s** (168 iterations, 0.599) | 6.55 s (0.557) | 2.13x |

New since the previous table: all-in terminals before the river from precomputed outcome tables
(#185: one dot product per hand instead of ~48 showdown kernels per turn all-in), AVX-512 regret
matching (#175). Pio's own check schedule overshoots the target a little further (it stops at 0.51
/ 0.56 chips), so at equal final exploitability the ratio is slightly smaller.

Session start for reference: srp_big took 446 s against Pio's 70.5 s.

### Update after #189 (2026-11-03, same session, back to back)

32 threads, target 0.6 chips; WSolver main 1894178 (#189, two-phase river walk) with `set_info_freq auto`,
each solver run twice (srp_big: Pio once), interleaved:

| tree | WSolver | PioSOLVER 3.10 | ratio |
|---|---|---|---|
| srp_big (13 GB, 16-bit) | **41.9 s** (123 iterations, 0.597 chips; 41.95 / 41.85) | 71.5 s (0.509) | 1.71x |
| threebet_mono (542 MB) | **2.93 s** (168 iterations, 0.599; 2.925 / 2.941) | 6.70 s (0.557; 6.686 / 6.720) | 2.29x |

Times are the solver's own `running time` at the report that met the target (process wall time adds
0.1-0.8 s for WSolver and 0.9-3.2 s for Pio). At 32 threads srp_big now leans on memory bandwidth
(bench/where-the-time-goes.md).


### Update after #209 (2026-11-03, same session, back to back)

32 threads, target 0.6 chips; WSolver main 084fab7 with `set_info_freq auto`, each solver run twice,
interleaved. On this AVX-512 build the change since the previous table is #209 (showdown jobs as SIMD
lanes); #219-#226 and #234 speed up the NEON and AVX2-only builds.

| tree | WSolver | PioSOLVER 3.10 | ratio |
|---|---|---|---|
| srp_big (13 GB, 16-bit) | **39.3 s** (123 iterations, 0.597 chips; 39.10 / 39.49) | 70.9 s (0.509; 70.76 / 71.10) | 1.81x |
| threebet_mono (542 MB) | **2.59 s** (168 iterations, 0.599; 2.594 / 2.594) | 6.62 s (0.557; 6.615 / 6.619) | 2.55x |

Process wall time: WSolver 2.67 / 39.8-40.3 s, Pio 7.52 / 74.0-74.5 s. Both trees are now limited by
memory bandwidth on this PC from 8 threads up (bench/where-the-time-goes.md), so kernel speedups show
less here than at 1 thread or on the M4.

### Update after #275 and #277 (2026-11-05)

New on main since the table above:
- #275: stochastic rounding of the 16-bit strategy sums, so 16-bit no longer stalls short of tight targets;
- #277: the default thread count is timed per tree (here 16 threads for threebet_mono, 32 for srp_big);
- everything merged in between.

WSolver main `b513570` is a WSL build, not the native Windows build of the earlier tables. Pio
3.10.1 runs natively at 32 threads, driven from WSL with ranges converted to Pio's floats. Times are
seconds to the first report at or below each target, two interleaved reps:

| threebet_mono (pot 200) | to 0.5% | to 0.3% | to 0.1% |
|---|---|---|---|
| WSolver (default threads) | **2.46 / 2.47** | **3.08 / 3.06** | **5.16 / 5.09** (290 it) |
| PioSOLVER 3.10 (32 threads) | 5.66 / 5.60 | 6.86 / 6.77 | 38.68 / 38.50 (995 it) |

srp_big (13 GB, 16-bit): WSolver reaches 1.5% / 1% / 0.5% of pot in 36.5-38.0 / 45.4-46.9 /
66.6-68.4 s (185 iterations). The 1% time (0.6 chips) compares with Pio's 70.9 s in the table above.
Pio's own srp_big run this session was stopped by the host for low memory: other jobs were using the
PC at the same time, so it is not re-measured here.

On threebet_mono Pio needs 995 iterations to reach 0.1% of pot, where WSolver needs 290. At looser
targets the two converge at about the same rate per iteration (see above), so the gap at 0.1% is
convergence, not only speed per iteration.

## Tree memory (2026-11-05)

Same trees, same machine (Zen 5 9950X). Pio's figures are its own `estimate_schematic_tree` (v4_large =
float32, v4_small = 16-bit); its measured peak working set for threebet_mono after `build_tree` and two
iterations was 779 MB, exactly its v4_small estimate. WSolver's are `estimate_schematic_tree` and the
measured peak RSS (`/usr/bin/time -v`, 16 threads, build + 2 iterations).

| tree | WSolver float32 | WSolver 16-bit | Pio float32 | Pio 16-bit | 16-bit ratio |
|---|---|---|---|---|---|
| threebet_mono | 542 MB (574 MB peak) | 282 MB (316 MB peak) | 1152 MB | 779 MB (779 MB peak) | **2.5x smaller** |
| srp_big | 12937 MB | 6554 MB | 17681 MB | 10771 MB | 1.6x smaller |
| a real 3-bet-pot PioViewer config (235 lines, #313) | 2280 MB | 1169 MB | 3359 MB | 2104 MB | 1.8x smaller |

Node counts are identical (the real config: 292074 OOP and 259099 IP decisions in both), so the gap is
per-node storage. A likely part of it: WSolver stores each player's own range at a node, while Pio keeps
the union of both ranges for each player (AGENTS.md; ours does that only under `set_union_hands on`).
