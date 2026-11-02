# Benchmark results

- machine: arm64 Darwin 27.2.0, 20 cores
- compiler: Apple clang version 15.0.0 (clang-1500.3.9.4)
- solver: 0.1.0 (Nov  1 2026)
- targets are exploitability as a fraction of the starting pot
- times are seconds to the first report at or below the target; `set_info_freq 5` bounds the overshoot
- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the same trees (black-box, via `tests/pio_compare`)

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| deep | 1 | 145 | 232.8 MB | 16.82 | 24.24 | 36.45 | 0.0760 |  |
| deep | 20 | 145 | 237.9 MB | 2.75 | 4.01 | 6.14 | 0.0760 |  |
| flop_3bet | 1 | 50 | 41 MB | 1.13 | 1.63 | 2.36 | 0.3970 |  |
| flop_3bet | 20 | 50 | 43.7 MB | 0.36 | 0.51 | 0.73 | 0.3960 |  |
| flop_srp_2 | 1 | 60 | 154.6 MB | 5.22 | 6.98 | 10.49 | 0.1190 |  |
| flop_srp_2 | 20 | 60 | 160.7 MB | 0.62 | 0.84 | 1.26 | 0.1190 |  |
| flop_srp_4 | 1 | 75 | 307.2 MB | 14.50 | 18.01 | 26.68 | 0.1040 |  |
| flop_srp_4 | 20 | 75 | 313.7 MB | 1.88 | 2.35 | 3.51 | 0.1040 |  |
| preflop | 1 | 45 | 16.2 MB | 0.04 | 0.05 | 0.08 | 0.0260 |  |
| preflop | 20 | 45 | 16.5 MB | 0.05 | 0.07 | 0.11 | 0.0260 |  |
| river | 1 | 75 | 2.7 MB | 0.00 | 0.00 | 0.01 | 0.1930 |  |
| river | 20 | 75 | 3 MB | 0.00 | 0.00 | 0.01 | 0.1930 |  |
| turn | 1 | 50 | 5.4 MB | 0.07 | 0.10 | 0.14 | 0.1910 |  |
| turn | 20 | 50 | 6.7 MB | 0.04 | 0.06 | 0.09 | 0.1910 |  |

## Full preflop tree (opt-in, `--include-preflop`)

`preflop_full` (25-flop subset x 3 schematic postflop trees = 75 subtrees, wide 25bb-HU
ranges, 16-bit storage) is several GB and minutes per run, so it is not part of the
default `make bench`. Rows below were measured on a different machine than the table
above (arm64 Darwin 27.2.0, 10 cores, Apple clang 21.0.0, Nov 1 2026) — do not compare
absolute numbers across the two tables.

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| preflop_full | 1 | 50 | 2182.6 MB | 52.87 | 82.65 | 139.67 | 0.0300 |  |
| preflop_full | 10 | 50 | 2190.1 MB | 9.55 | 14.74 | 25.15 | 0.0300 |  |
