# Benchmark results

- machine: x86_64 Linux 5.15.167.4-microsoft-standard-WSL2, 32 cores
- compiler: c++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- solver: 0.1.0 (Nov  7 2026)
- targets are exploitability as a fraction of the starting pot
- times are seconds to the first report at or below the target; `set_info_freq 5` bounds the overshoot
- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the same trees (black-box, via `tests/pio_compare`)
- "not reached" means the run hit the `--max-seconds 300` wall-clock cap before the target, not that the solve failed or stalled; raise the cap to measure that row

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| deep | 1 | 100 | 230.7 MB | 5.66 | 7.14 | 10.46 | 0.0790 |  |
| deep | 16 | 100 | 169.2 MB | 0.54 | 0.68 | 0.99 | 0.0790 |  |
| deep | 32 | 100 | 210.4 MB | 0.60 | 0.77 | 1.14 | 0.0790 |  |
| flop_3bet | 1 | 50 | 41.5 MB | 0.58 | 0.93 | 1.15 | 0.3750 |  |
| flop_3bet | 16 | 50 | 65.6 MB | 0.09 | 0.13 | 0.15 | 0.3750 |  |
| flop_3bet | 32 | 50 | 114.4 MB | 0.14 | 0.21 | 0.25 | 0.3750 |  |
| flop_srp_2 | 1 | 45 | 157.2 MB | 1.79 | 2.07 | 2.93 | 0.0870 |  |
| flop_srp_2 | 16 | 45 | 113 MB | 0.19 | 0.21 | 0.30 | 0.0870 |  |
| flop_srp_2 | 32 | 45 | 157.3 MB | 0.20 | 0.23 | 0.33 | 0.0870 |  |
| flop_srp_4 | 1 | 45 | 306.2 MB | 4.40 | 4.98 | 5.98 | 0.1000 |  |
| flop_srp_4 | 16 | 45 | 192 MB | 0.44 | 0.49 | 0.59 | 0.1010 |  |
| flop_srp_4 | 32 | 45 | 229.5 MB | 0.46 | 0.53 | 0.65 | 0.1010 |  |
| preflop | 1 | 20 | 3.7 MB | 0.01 | 0.02 | 0.02 | 0.0210 |  |
| preflop | 16 | 20 | 3.9 MB | 0.01 | 0.01 | 0.01 | 0.0210 |  |
| preflop | 32 | 20 | 3.8 MB | 0.02 | 0.03 | 0.03 | 0.0210 |  |
| river | 1 | 75 | 3.8 MB | 0.00 | 0.00 | 0.00 | 0.1970 |  |
| river | 16 | 75 | 3.7 MB | 0.00 | 0.00 | 0.00 | 0.1970 |  |
| river | 32 | 75 | 3.8 MB | 0.00 | 0.00 | 0.00 | 0.1970 |  |
| threebet_mono | 1 | 215 | 552.9 MB | 22.27 | 28.60 | 39.79 | 0.3990 |  |
| threebet_mono | 16 | 225 | 323 MB | 1.83 | 2.44 | 3.89 | 0.3980 |  |
| threebet_mono | 32 | 225 | 371.1 MB | 1.97 | 2.62 | 4.30 | 0.3980 |  |
| turn | 1 | 40 | 3.8 MB | 0.02 | 0.02 | 0.04 | 0.1460 |  |
| turn | 16 | 40 | 21.9 MB | 0.03 | 0.04 | 0.06 | 0.1460 |  |
| turn | 32 | 40 | 80.1 MB | 0.07 | 0.08 | 0.13 | 0.1460 |  |
| preflop_full | 1 | 20 | 2124.5 MB | 28.97 | 28.97 | 36.88 | 0.0210 |  |
| preflop_full | 16 | 20 | 2134.2 MB | 3.53 | 3.53 | 4.45 | 0.0210 |  |
| preflop_full | 32 | 20 | 2144 MB | 4.25 | 4.25 | 5.31 | 0.0210 |  |
| srp_big | 1 | 75 | 6593.6 MB | not reached | not reached | not reached | 1.7510 |  |
| srp_big | 16 | 260 | 6608.9 MB | 49.83 | 71.50 | 103.26 | 0.1190 |  |
| srp_big | 32 | 260 | 6623.2 MB | 46.86 | 66.65 | 94.91 | 0.1190 |  |
