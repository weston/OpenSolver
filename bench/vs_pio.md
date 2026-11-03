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
