# Benchmark results

- machine: arm64 Darwin 27.2.0, 20 cores
- compiler: Apple clang version 15.0.0 (clang-1500.3.9.4)
- solver: 0.1.0 (Nov  2 2026)
- targets are exploitability as a fraction of the starting pot
- times are seconds to the first report at or below the target; `set_info_freq 5` bounds the overshoot
- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the same trees (black-box, via `tests/pio_compare`)

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| deep | 1 | 100 | 232.7 MB | 13.50 | 17.45 | 25.42 | 0.0730 |  |
| deep | 20 | 100 | 237.6 MB | 2.19 | 2.83 | 4.13 | 0.0730 |  |
| flop_3bet | 1 | 50 | 41 MB | 1.17 | 1.68 | 2.41 | 0.3610 |  |
| flop_3bet | 20 | 50 | 43.8 MB | 0.36 | 0.52 | 0.75 | 0.3610 |  |
| flop_srp_2 | 1 | 45 | 154.4 MB | 4.55 | 5.47 | 8.24 | 0.0970 |  |
| flop_srp_2 | 20 | 45 | 160.4 MB | 0.56 | 0.67 | 1.01 | 0.0970 |  |
| flop_srp_4 | 1 | 50 | 307.1 MB | 11.31 | 13.15 | 18.67 | 0.0910 |  |
| flop_srp_4 | 20 | 50 | 313.4 MB | 1.46 | 1.69 | 2.40 | 0.0910 |  |
| preflop | 1 | 30 | 16.1 MB | 0.03 | 0.03 | 0.05 | 0.0240 |  |
| preflop | 20 | 30 | 16.5 MB | 0.02 | 0.03 | 0.05 | 0.0240 |  |
| river | 1 | 70 | 2.7 MB | 0.00 | 0.00 | 0.01 | 0.1910 |  |
| river | 20 | 70 | 3 MB | 0.00 | 0.00 | 0.01 | 0.1910 |  |
| turn | 1 | 35 | 5.5 MB | 0.06 | 0.07 | 0.10 | 0.1990 |  |
| turn | 20 | 35 | 6.8 MB | 0.04 | 0.04 | 0.06 | 0.1990 |  |
| preflop_full | 1 | 20 | 2181 MB | 59.92 | 59.92 | 82.48 | 0.0240 |  |
| preflop_full | 20 | 20 | 2197.6 MB | 5.65 | 5.65 | 7.69 | 0.0240 |  |
