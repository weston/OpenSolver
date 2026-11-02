# DCFR parameter sweep (#52)

Why the default is α=1.5, β=0, **γ=3** rather than DCFR's published 1.5/0/2.

Positive regrets are scaled by `t^α/(t^α+1)`, negative by `t^β/(t^β+1)`, the strategy sum by
`(t/(t+1))^γ`. Override at runtime with `set_dcfr <alpha> <beta> <gamma>`.

## Method

```sh
for v in "1.5 0 2" "1 1 1" "1.5 0 1" "1.5 0 3" "1 0 2" "2 0 2" "3 0 2" \
         "1.5 -1 2" "1.5 -1000 2" "2 -1000 2" "1.5 0 4" "1.5 0 5" "1.5 -1000 3" "1.5 -1 3"; do
  python3 bench/run.py --threads 1 --max-seconds 60 --extra "set_dcfr $v"
done
```

Single-threaded for determinism (iteration counts are identical at 1 and 20 threads, so thread count
only scales throughput). β = -1000 underflows `t^β` to 0, i.e. CFR+'s "discard negative regrets";
α cannot be pushed high the same way, because `t^α` overflows to `inf` and `inf/(inf+1)` is NaN.

## Seconds to 0.2% of pot, 1 thread

| α / β / γ | deep | flop_3bet | flop_srp_2 | flop_srp_4 | preflop | river | turn | total | iters |
|---|---|---|---|---|---|---|---|---|---|
| 1.5 / 0 / 2 (was default) | 37.17 | 2.37 | 10.50 | 26.79 | 0.08 | 0.01 | 0.14 | 77.05 | 500 |
| **1.5 / 0 / 3 (new default)** | 34.67 | 2.39 | 9.69 | 21.59 | 0.06 | 0.01 | 0.13 | **68.53** | **455** |
| 1.5 / 0 / 4 | 33.37 | 2.37 | 8.87 | 19.84 | 0.06 | 0.01 | 0.13 | 64.65 | 440 |
| 1.5 / 0 / 5 | 31.76 | 2.37 | 8.79 | 18.07 | 0.05 | 0.01 | 0.13 | 61.19 | 435 |
| 1.5 / -1000 / 3 | 30.79 | 2.38 | 8.78 | 19.79 | 0.06 | 0.01 | 0.13 | 61.94 | 435 |
| 1.5 / -1000 / 2 | 33.41 | 2.61 | 10.55 | 23.25 | 0.08 | 0.01 | 0.16 | 70.06 | 490 |
| 1.5 / -1 / 3 | 36.02 | 2.13 | 8.78 | 19.86 | 0.06 | 0.01 | 0.13 | 66.98 | 445 |
| 1.5 / -1 / 2 | 37.32 | 2.20 | 10.57 | 23.31 | 0.08 | 0.01 | 0.14 | 73.63 | 495 |
| 1 / 0 / 2 | 34.69 | 4.66 | 15.86 | 46.89 | 0.06 | 0.02 | 0.18 | 102.36 | 725 |
| 2 / 0 / 2 | 55.49 | 2.83 | 13.27 | 32.52 | 0.10 | 0.01 | 0.18 | 104.39 | 665 |
| 2 / -1000 / 2 | 49.22 | 3.37 | 13.27 | 28.92 | 0.10 | 0.01 | 0.20 | 95.08 | 650 |
| 1 / 1 / 1 (linear CFR) | 44.58 | 3.79 | 19.97 | 40.46 | 0.09 | 0.01 | 0.20 | 109.09 | 745 |
| 1.5 / 0 / 1 | 49.99 | 3.33 | 18.39 | 42.25 | 0.11 | 0.01 | 0.21 | 114.29 | 725 |
| 3 / 0 / 2 | not reached in 60 s | 3.39 | 15.98 | 38.49 | 0.12 | 0.01 | 0.23 | — | 231+ |

## Why γ=3 and not γ=5

γ=5 has the best total, but the bar for changing a default is *better across the suite, not just on
one tree*. At tighter targets γ=5 regresses the 3-bet pot, while γ=3 never loses to the old default:

| α / β / γ | turn (0.2 / 0.1 / 0.05 % pot) | flop_3bet | flop_srp_2 |
|---|---|---|---|
| 1.5 / 0 / 2 | 0.1 / 0.2 / 0.3 | 2.4 / 3.6 / 5.0 | 10.5 / 14.9 / 20.2 |
| 1.5 / 0 / 3 | 0.1 / 0.2 / 0.3 | 2.4 / 3.6 / 5.0 | 9.7 / 12.3 / **16.7** |
| 1.5 / 0 / 5 | 0.1 / 0.2 / 0.3 | 2.4 / **3.8** / **5.3** | 8.8 / 11.4 / **15.8** |

So γ=3 is a strict improvement, and γ=5 is a trade: clearly faster on the big trees, ~6% slower on a
low-SPR 3-bet pot at tight accuracy. γ=5 is one `set_dcfr 1.5 0 5` away for anyone who wants it.

No variant plateaued: every one still reached 0.05% of pot, so the high-γ gains are real convergence
rather than an artifact of stopping early.

## Caveats

- Single machine, single thread, seven trees. Targets down to 0.05% of pot.
- `set_info_freq 5` throughout, so comparisons here are valid against each other but not against
  numbers taken at a different report frequency.
- Not tested: a full preflop tree with flop subsets (#65), or the 25bb fixture.
