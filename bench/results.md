# Benchmark results

- machine: arm64 Darwin 27.2.0, 20 cores
- compiler: Apple clang version 15.0.0 (clang-1500.3.9.4)
- solver: 0.1.0 (Nov  2 2026)
- targets are exploitability as a fraction of the starting pot
- times are seconds to the first report at or below the target; `set_info_freq 5` bounds the overshoot
- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the same trees (black-box, via `tests/pio_compare`)

| tree | threads | iters | peak RSS | to 1% pot | to 0.5% pot | to 0.2% pot | final expl | Pio (same tree) |
|---|---|---|---|---|---|---|---|---|
| deep | 1 | 100 | 232.7 MB | 13.18 | 16.78 | 24.92 | 0.0730 |  |
| deep | 20 | 100 | 237.4 MB | 2.10 | 2.68 | 4.00 | 0.0730 |  |
| flop_3bet | 1 | 50 | 40.9 MB | 1.16 | 1.67 | 2.37 | 0.3610 |  |
| flop_3bet | 20 | 50 | 43.4 MB | 0.37 | 0.53 | 0.74 | 0.3610 |  |
| flop_srp_2 | 1 | 45 | 154.6 MB | 4.49 | 5.29 | 7.74 | 0.0970 |  |
| flop_srp_2 | 20 | 45 | 160.3 MB | 0.55 | 0.65 | 0.94 | 0.0970 |  |
| flop_srp_4 | 1 | 50 | 307.2 MB | 10.93 | 12.49 | 17.09 | 0.0910 |  |
| flop_srp_4 | 20 | 50 | 313.3 MB | 1.41 | 1.60 | 2.18 | 0.0910 |  |
| preflop | 1 | 30 | 16.1 MB | 0.02 | 0.03 | 0.05 | 0.0240 |  |
| preflop | 20 | 30 | 16.4 MB | 0.02 | 0.03 | 0.05 | 0.0240 |  |
| river | 1 | 70 | 2.8 MB | 0.00 | 0.00 | 0.01 | 0.1910 |  |
| river | 20 | 70 | 3 MB | 0.00 | 0.00 | 0.01 | 0.1910 |  |
| turn | 1 | 35 | 5.5 MB | 0.06 | 0.07 | 0.10 | 0.1990 |  |
| turn | 20 | 35 | 3.4 MB | 0.04 | 0.04 | 0.06 | 0.1990 |  |
| preflop_full | 1 | 20 | 2183 MB | 58.61 | 58.61 | 75.81 | 0.0240 |  |
| preflop_full | 20 | 20 | 2195.5 MB | 5.83 | 5.83 | 7.42 | 0.0240 |  |
