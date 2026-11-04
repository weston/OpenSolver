# Benchmark results

- machine: arm64 Darwin 27.2.0, 20 cores
- compiler: Apple clang version 15.0.0 (clang-1500.3.9.4)
- solver: 0.1.0 (Nov  3 2026)
- targets are exploitability as a fraction of the starting pot
- times are seconds to the first report at or below the target; `set_info_freq 5` bounds the overshoot
- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the same trees (black-box, via `tests/pio_compare`)
- "not reached" means the run hit the `--max-seconds 300` wall-clock cap before the target, not that the solve failed or stalled; raise the cap to measure that row

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| deep | 1 | 100 | 234.2 MB | 9.49 | 11.94 | 17.41 | 0.0790 |  |
| deep | 20 | 100 | 242.9 MB | 0.65 | 0.81 | 1.19 | 0.0790 |  |
| flop_3bet | 1 | 50 | 41.5 MB | 0.88 | 1.43 | 1.78 | 0.3750 |  |
| flop_3bet | 20 | 50 | 44.7 MB | 0.07 | 0.11 | 0.14 | 0.3750 |  |
| flop_srp_2 | 1 | 45 | 154.8 MB | 3.20 | 3.75 | 5.33 | 0.0870 |  |
| flop_srp_2 | 20 | 45 | 163.1 MB | 0.23 | 0.27 | 0.38 | 0.0870 |  |
| flop_srp_4 | 1 | 45 | 307.5 MB | 7.73 | 8.79 | 10.56 | 0.1000 |  |
| flop_srp_4 | 20 | 45 | 316.8 MB | 0.53 | 0.60 | 0.73 | 0.1000 |  |
| preflop | 1 | 30 | 16.1 MB | 0.02 | 0.03 | 0.04 | 0.0240 |  |
| preflop | 20 | 30 | 16.4 MB | 0.01 | 0.01 | 0.02 | 0.0240 |  |
| river | 1 | 75 | 2.9 MB | 0.00 | 0.00 | 0.01 | 0.1970 |  |
| river | 20 | 75 | 3.1 MB | 0.00 | 0.00 | 0.00 | 0.1970 |  |
| threebet_mono | 1 | 225 | 565.5 MB | 39.00 | 49.65 | 79.61 | 0.3930 |  |
| threebet_mono | 20 | 225 | 571 MB | 2.69 | 3.41 | 5.50 | 0.3930 |  |
| turn | 1 | 40 | 5.6 MB | 0.04 | 0.05 | 0.08 | 0.1460 |  |
| turn | 20 | 40 | 3.5 MB | 0.02 | 0.02 | 0.03 | 0.1460 |  |
| preflop_full | 1 | 20 | 2183.1 MB | 47.89 | 47.89 | 62.22 | 0.0240 |  |
| preflop_full | 20 | 20 | 2191.9 MB | 3.33 | 3.33 | 4.29 | 0.0240 |  |
| srp_big | 1 | 37 | 13206.1 MB | not reached | not reached | not reached | 8.4390 |  |
| srp_big | 20 | 260 | 13225.1 MB | 67.31 | 97.14 | 138.50 | 0.1190 |  |
