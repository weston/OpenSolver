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
