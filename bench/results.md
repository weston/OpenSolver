# Benchmark results

- machine: arm64 Darwin 27.2.0, 20 cores
- compiler: Apple clang version 15.0.0 (clang-1500.3.9.4)
- solver: 0.1.0 (Nov  1 2026)
- targets are exploitability as a fraction of the starting pot
- times are seconds to the first report at or below the target; `set_info_freq 5` bounds the overshoot
- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the same trees (black-box, via `tests/pio_compare`)

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| deep | 1 | 135 | 232.8 MB | 16.46 | 23.13 | 35.55 | 0.0740 |  |
| deep | 20 | 135 | 237.6 MB | 2.60 | 3.67 | 5.79 | 0.0740 |  |
| flop_3bet | 1 | 50 | 41 MB | 1.17 | 1.69 | 2.41 | 0.3550 |  |
| flop_3bet | 20 | 50 | 43.7 MB | 0.39 | 0.56 | 0.78 | 0.3550 |  |
| flop_srp_2 | 1 | 55 | 154.3 MB | 5.38 | 6.29 | 9.90 | 0.1000 |  |
| flop_srp_2 | 20 | 55 | 160.4 MB | 0.65 | 0.76 | 1.18 | 0.1000 |  |
| flop_srp_4 | 1 | 60 | 307.2 MB | 12.77 | 14.59 | 21.65 | 0.1090 |  |
| flop_srp_4 | 20 | 60 | 313.6 MB | 1.69 | 1.93 | 2.85 | 0.1090 |  |
| preflop | 1 | 35 | 16.2 MB | 0.04 | 0.04 | 0.06 | 0.0280 |  |
| preflop | 20 | 35 | 16.5 MB | 0.04 | 0.06 | 0.08 | 0.0280 |  |
| river | 1 | 75 | 2.7 MB | 0.00 | 0.00 | 0.01 | 0.1950 |  |
| river | 20 | 75 | 3 MB | 0.00 | 0.00 | 0.01 | 0.1950 |  |
| turn | 1 | 45 | 5.5 MB | 0.07 | 0.09 | 0.13 | 0.1840 |  |
| turn | 20 | 45 | 6.8 MB | 0.04 | 0.05 | 0.08 | 0.1840 |  |

## Full preflop tree (opt-in, `--include-preflop`)

`preflop_full` (25-flop subset x 3 schematic postflop trees = 75 subtrees, wide 25bb-HU
ranges, 16-bit storage) is several GB and minutes per run, so it is not part of the
default `make bench`. Rows below were measured on a different machine than the table
above (arm64 Darwin 27.2.0, 10 cores, Apple clang 21.0.0, Nov 1 2026) — do not compare
absolute numbers across the two tables. (This section was dropped by #70's regeneration
and is restored with fresh post-#70, DCFR γ=3 numbers; the γ=2-era run took 50
iterations and 139.67 s / 25.15 s to 0.2% of pot.)

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| preflop_full | 1 | 25 | 2182.7 MB | 38.09 | 52.42 | 66.74 | 0.0280 |  |
| preflop_full | 10 | 25 | 2190.1 MB | 6.93 | 9.51 | 12.10 | 0.0280 |  |
