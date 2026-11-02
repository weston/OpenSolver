# PCFR+ / predictive regret, alternating updates, average delay (#71)

#70 did the parameter half of #52; this is the algorithm-change half, split out for its own
correctness review. **Result: none of the options beats the default across the suite, so the
default algorithm is unchanged.** The knobs stay available at runtime for future tuning:

- `set_predictive on` — PCFR+/optimistic regret matching (Farina et al. 2019): the strategy
  at each node is matched on `R + last instantaneous regret` instead of `R` alone. Costs one
  extra float per regret slot, allocated lazily and never saved (transient solver state).
- `set_avg_delay <n>` — iterations 1..n only move regrets; the strategy average starts
  accumulating at iteration n+1 (warm-started average). 0 = from t=1 (default).
- Alternating updates need no knob: the engine already updates each player's regrets against
  the opponent's just-updated strategy within one iteration (`Solver::iterate` walks
  `first_player` first, and the second walk reads the updated regrets via
  `current_strategy`). That is the faster side of the simultaneous-vs-alternating coin in
  the literature (Burch et al. 2019), so a simultaneous mode was not implemented.

## Method

Same recipe as `bench/dcfr_sweep.md`:

```sh
for cfg in "" "set_predictive on" "set_avg_delay 25" "set_avg_delay 50" \
           "set_predictive on;set_avg_delay 25" "set_predictive on;set_avg_delay 50"; do
  python3 bench/run.py --threads 1 --max-seconds 300 --extra "$cfg"
done
```

Single-threaded: iteration counts are deterministic and machine-independent, so they are the
primary signal; the seconds columns are from a contended machine and are indicative only.
The bar for changing a default is #52's: reach every target in fewer iterations on every
tree, not just on one.

## Iterations to 0.2% of pot, 1 thread

`set_info_freq 5`, so iteration counts are multiples of 5; a ±5 difference on one tree is a tie
within the reporting granularity.

| config | deep | flop_3bet | flop_srp_2 | flop_srp_4 | preflop | river | turn | total |
|---|---|---|---|---|---|---|---|---|
| default (DCFR 1.5/0/3) | 135 | 50 | 55 | 60 | 35 | 75 | 45 | 455 |
| set_predictive on | 150 | 70 | 60 | 65 | 35 | 125 | 45 | 550 |
| set_avg_delay 25 | **130** | 50 | **50** | **50** | **30** | 75 | 45 | **430** |
| set_avg_delay 50 | **130** | 60 | 55 | 55 | **30** | 80 | 45 | 455 |
| set_predictive on;set_avg_delay 25 | 150 | 70 | 60 | 60 | **25** | 125 | 45 | 535 |
| set_predictive on;set_avg_delay 50 | 150 | 70 | 55 | 55 | **25** | 130 | 55 | 540 |

The three ties against the default (river, turn, flop_3bet under `set_avg_delay 25`) were
re-checked at `set_info_freq 1` (`--trees river,turn,flop_3bet --info-freq 1 --max-seconds 60`);
iteration counts then have ±1 cadence noise:

| config | river | turn | flop_3bet |
|---|---|---|---|
| default | 74 | 44 | 48 |
| set_avg_delay 25 | 75 | 43 | 50 |

## Seconds to 0.5% / 0.2% of pot, 1 thread (indicative)

Wall time to reach each target, `0.5% / 0.2%`. Measured on a contended shared machine; use the
iteration table above for decisions. River and preflop hit targets in milliseconds, so their
values are pure noise.

| config | deep | flop_3bet | flop_srp_2 | flop_srp_4 | preflop | river | turn |
|---|---|---|---|---|---|---|---|
| default | 16.07 / 25.11 | 1.09 / 1.57 | 4.17 / 6.57 | 9.84 / 14.54 | 0.03 / 0.04 | 0.00 / 0.00 | 0.06 / 0.09 |
| set_predictive on | 16.05 / 28.75 | 1.28 / 2.27 | 4.87 / 7.33 | 11.47 / 16.66 | 0.03 / 0.04 | 0.00 / 0.01 | 0.06 / 0.09 |
| set_avg_delay 25 | 14.76 / 22.39 | 1.05 / 1.53 | 3.93 / 5.60 | 8.20 / 11.69 | 0.02 / 0.04 | 0.00 / 0.00 | 0.06 / 0.09 |
| set_avg_delay 50 | 14.30 / 21.68 | 1.37 / 1.83 | 4.41 / 6.17 | 9.22 / 12.33 | 0.02 / 0.03 | 0.00 / 0.00 | 0.06 / 0.09 |
| set_predictive on;set_avg_delay 25 | 14.89 / 26.29 | 1.25 / 2.23 | 3.94 / 6.77 | 9.69 / 14.69 | 0.02 / 0.03 | 0.00 / 0.01 | 0.06 / 0.09 |
| set_predictive on;set_avg_delay 50 | 13.56 / 25.56 | 1.71 / 2.19 | 6.08 / 6.08 | 10.59 / 12.90 | 0.02 / 0.03 | 0.00 / 0.01 | 0.07 / 0.11 |

## Discussion

**Predictive regret loses, and it is not close.** Matching on `R + last instantaneous regret`
costs 1.09–1.67× the iterations everywhere except the two trivially-fast trees (ties on preflop
and turn). PCFR+'s bound is proven against vanilla CFR updates; here the instantaneous regret is
added on top of regrets that DCFR's γ=3 discounting already tilts toward the present, so the
match overshoots and the strategy average inherits the noise. It also costs transient memory —
one extra float per regret slot — for strictly worse convergence. Not adopted.

**Average delay is the strongest candidate, but it does not clear the bar.** `set_avg_delay 25`
never loses at the coarse granularity, wins 5–10 iterations on deep, both SRP trees, and preflop
(430 vs 455 total, −5.5%), and its seconds columns track those wins. At finer reporting it is a
hair *worse* on flop_3bet (50 vs 48), and its sibling `set_avg_delay 50` flips sign on
flop_3bet (60 vs 50) and river (80 vs 75) — the family is tree-sensitive. #70's bar for a
default change is better across the suite with no regressions, validated at tight targets; delay
25 misses the no-regression side on flop_3bet and was only measured down to 0.2% of pot. The
default stays 0. A delay is still available at runtime, and the flop-SRP savings (~1/6 fewer
iterations) would be worth re-checking if validation is extended below 0.2% of pot.

Interaction note for anyone revisiting: a delayed average skips *accumulation*, not solving —
during the delay the strategy returned to callers (e.g. `show_strategy`) is instantaneous
regret matching, and `Solver::clear_storage` re-zeroes the predictive workspace, so toggling
`set_predictive` mid-session is safe.

**Alternating updates need nothing.** `Solver::iterate` already walks the first player and then
the second against the just-updated regrets, i.e. the alternating-update form that
Burch–Johanson–Bowling (AAAI 2019) show dominates simultaneous updates for DCFR-type averages.
No knob was added.

**Determinism.** With both knobs at defaults the update math is unchanged; the restructured
loops differ from main only by float rounding (~1e-7 relative ulps in `show_range` output; EVs
and exploitability byte-identical).
