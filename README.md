# OpenUPI-Solver

A heads-up no-limit hold'em solver (postflop trees and full preflop trees) with a PioSOLVER-compatible text interface
(UPI, the Universal Poker Interface). It is an independent implementation written from the
public UPI documentation; it contains no PioSOLVER code.

- C++20, single binary, no dependencies beyond zlib (ships with macOS).
- Vector-form **Discounted CFR** (α=1.5, β=0, γ=2) with exact card removal, no abstraction.
- Multi-threaded over turn/river cards with a nested work-stealing pool.
- Suit **isomorphism** (flop trees on by default, like Pio's `set_isomorphism 1 0`).
- Optional 16-bit compressed storage (`small_strats`), roughly halving memory.

## Build and run

```sh
make            # builds ./solver (-O3, native CPU optimizations)
make test       # unit tests + end-to-end UPI tests on a small tree (~1 min, < 1 GB RAM)
make test-big   # same, but the end-to-end tests solve the real 25bb tree (~5-10 min, ~4 GB free RAM)
./solver        # talk UPI on stdin/stdout
```

A C++20 compiler (GCC or Clang), make, Python 3 (for tests), and zlib headers are
required. On Ubuntu 24.04, install them with
`sudo apt install build-essential zlib1g-dev python3`; use `make CXX=clang++-18`
to build with Clang after installing `clang-18`. The native CPU flag is
`-mcpu=native` on ARM and `-march=native` on x86; `CXXFLAGS` can override it.

A minimal session:

```
set_end_string END
set_range OOP AA,KK,QQ,AK,AQs,KQs,JTs,T9s,98s
set_range IP 22+,A2s+,KTs+,QTs+,JTs,ATo+,KQo
set_board Qs8d7s
set_pot 0 0 60
set_eff_stack 940
add_line 0 20 20 60 60 160
add_line 20 60 60 160 160 940
build_tree
set_accuracy 0.3
go
wait_for_solver
calc_results
show_strategy r:0
```

`tests/data/pio_25bbHU_2sizes.txt` is a real PioViewer-generated tree script (266 `add_line`s);
it runs unmodified: `./solver < tests/data/pio_25bbHU_2sizes.txt`.

## Tree builder

The engine only accepts explicit lines. PioViewer instead lets you describe a tree with a few
percentages and expands them itself; `tools/tree_builder.py` does that expansion, so you can
write the short form and get the lines:

```sh
python3 tools/tree_builder.py config.txt            # print the add_line commands
python3 tools/tree_builder.py config.txt --full     # a complete UPI script, ready to pipe in
python3 tools/tree_builder.py config.txt --compare  # diff against add_lines already in the file
python3 tools/tree_builder.py --rules               # print the sizing rules
```

The input is any file with PioViewer's `#Key#Value` header; everything else in the file is
ignored, so a PioViewer save file works directly:

```
#Pot#40
#EffectiveStacks#230
#AllinThreshold#67
#AddAllinOnlyIfLessThanThisTimesThePot#300
#FlopConfig.BetSize#30,60
#FlopConfig.RaiseSize#40
#RiverConfig.BetSize#65
#RiverConfig.AddAllin#True
```

`#<Street>Config.<Field>#` sets OOP, `#<Street>ConfigIP.<Field>#` sets IP; fields are
`BetSize`, `DonkBetSize`, `RaiseSize` (comma-separated percentages) and `AddAllin`.

Amounts in a line are absolute - the total a player has put in since the hand started - so a
check or call repeats the amount that player is already matched to. With
`pot = dead pot + both investments` and `pot_if_called = dead pot + 2 x the larger investment`:

| Action | Total |
|---|---|
| bet | `invested + round(BetSize% x pot)`, or `DonkBetSize%` when OOP leads into the previous street's aggressor |
| raise | `facing + round(RaiseSize% x pot_if_called)` |

Rounding is to the nearest chip. Two all-in rules sit on top:

- **`AllinThreshold%`** - a bet or raise whose total reaches that percentage of the effective
  stack is replaced by the all-in. At 67% of a 230 stack, any size from 154 up becomes 230,
  which is why the deepest action of every branch is an all-in.
- **`<Street>Config[IP].AddAllin`** - offers the all-in as an extra size next to the configured
  ones, but only while `AddAllinOnlyIfLessThanThisTimesThePot%` of `pot_if_called` still exceeds
  the all-in. On a 40-chip river pot, 300% is 120 and the stack is 230, so no all-in is offered;
  facing a 26 bet the pot-if-called is 92, 300% of it is 276, and the all-in joins the raise.

Only maximal lines are emitted: the solver fills in check, call and fold at every node it
creates, so a line only has to reach the deepest bet of its branch.

These rules were inferred from `tests/data/pio_25bbHU_2sizes.txt`, which carries both a config
header and the 266 lines PioViewer generated from it; `tests/test_tree_builder.py` checks that
the builder reproduces that set exactly. Two details the fixture cannot pin down: the tie-break
when a size lands exactly on .5 (half-up and half-even both reproduce it), and `DonkBetSize`,
which the fixture sets equal to `BetSize` on every street - donk nodes are reached, but their
size is indistinguishable from an ordinary bet there.

## Interface conventions (as implemented)

| Topic | Behavior |
|---|---|
| Protocol | One command per line, `#` comments and blank lines ignored, `set_end_string` appends the END string to every response. |
| Hand order | Pio order: `2d2c 2h2c 2h2d 2s2c ...` (`show_hand_order`). Verified against a real Pio script: range text and Pio's 1326 floats match exactly. |
| Ranges | `set_range` accepts 1326 floats or range text (`AA,KK:0.5,AKs,A5s-A2s,22+,AhKd:0.3`). |
| `add_line` | Numbers are cumulative chips invested by the acting player. Checks, calls and folds are implicit; `remove_line` removes them (`0 30 0` removes OOP's fold to a c-bet). |
| Node IDs | `r`, `r:0`, then `c` / `f` / `b<amount>` where the amount is the **cumulative** chips invested by the bettor (e.g. `r:0:c:b20:c:7s:b60`), and card tokens at chance nodes. `?` means the first runout in display order (the highest card). |
| Node types | `ROOT`, `OOP_DEC`, `IP_DEC`, `SPLIT_NODE`, `END_NODE`. All-ins before the river are `END_NODE`s whose value averages over every runout (no chance nodes), exactly like Pio. |
| Children order | Bets/raises largest first, then check/call, then fold; dealt cards from `As` down to `2c`. `show_all_lines` walks the reverse order with `?` card markers. |
| EVs | Chips relative to the tree start: payoff = share of the final pot - own chips invested in the tree, so `EV OOP + EV IP = starting pot`. `calc_ev` matchups are weighted by the hand's own reach (Pio semantics); out-of-range hands print `nan`/`inf`. |
| SOLVER output | `SOLVER: started`, periodic `SOLVER:` blocks (running time, EVs, MES, `Exploitable for`) ending in END, and `SOLVER: stopped (<reason>)`. Command responses never interleave with these. |
| Isomorphism | Querying a non-canonical card (e.g. `7s` when it mirrors `7d`) returns the suit-swapped strategy; node IDs keep the card you asked for. |

## Command coverage

`set_range` additionally accepts range text (`AA,KK:0.5,...`); Pio itself only accepts 1326 numbers.

**Implemented:** is_ready, exit, set_end_string, reset_end_string, show_version, show_build_version,
show_settings, bench, load_script, load_script_silent, skip_if_done, LABEL:, echo, repeat,
show_hand_order, show_preflop_order, go, stop, wait_for_solver, take_a_break, solver_time,
set_threads, set_info_freq, set_accuracy, set_recalc_accuracy, set_always_recalc (stored),
set_isomorphism, change_step / show_step (stored), show_iters, set_first_iteration_player,
set_range, set_eff_stack, set_board, set_pot, add_line, remove_line, force_line, clear_lines,
build_tree, list_algorithms, set_algorithm, ignore_mem_check, small_strats,
estimate_schematic_tree, show_memory, is_tree_present, show_node, show_children,
show_effective_stack, show_range, show_strategy, show_strategy_pp, calc_ev, calc_ev_pp, calc_eq,
calc_eq_pp, calc_eq_node, calc_eq_preflop (Monte Carlo), calc_results, calc_matchups_line,
calc_line_freq, calc_global_freq, show_all_lines, show_all_freqs, show_win_evs,
add_info_line, reset_tree_info, show_tree_info, rebuild_tree, estimate_rebuild_tree,
solve_partial, solve_all_splits, explo_partial, stdoutredi, stdoutredi_append, stdoutback,
print_all_strats, node_count, clear_cache, show_cpu_info, show_nuts, show_category_names,
show_categories, show_cats, show_cats_pp, set_display_iso, reset_display_iso, show_board_no_iso,
set_rake, set_icm, set_icm_point, reset_icm_tables, eliminate_path, lock_node, unlock_node,
combo_lock_node, show_locked_combos, set_strategy, set_equal_strats, set_mes, round_up_to,
lock_line, unlock_line, set_incentives_node, set_incentives_line, show_incentives,
clear_incentives, load_tree, dump_tree (full / no_turns / no_rivers / no_compress), free_tree,
show_metadata, show_save_version, load_all_nodes, set_compression_precision,
set_compression_level, check_tree_health.

**Preflop trees:** add_preflop_line, remove_preflop_line, clear_preflop_lines, build_preflop_tree,
add_to_subset, reset_subset, show_subset, recover_subset, add_schematic_tree, add_all_flops,
estimate_tree. Workflow (as in Pio): set ranges, `set_pot <bb> <sb> 0`, `set_eff_stack <total stack>`,
add preflop lines (absolute amounts incl. blinds; the SB = IP acts first), `build_preflop_tree`,
`add_to_subset <weight> <c> <c> <c>` per flop, then for every preflop exit: `set_board <any flop>`,
`set_pot`/`set_eff_stack`/`add_line` for its postflop tree and `add_schematic_tree <exit>`; finally
`add_all_flops` and `go`. Each subset flop stands for its whole suit-isomorphism class (its values are
averaged over suit permutations; deal probabilities are normalized per hand pair by
inclusion-exclusion), preflop all-ins use a fixed sample of 3000 boards. As in Pio, each schematic's
`set_pot` is taken literally: its per-player chips are investments and its dead money is free money.
For whole-hand accounting give the preflop investments per player (e.g. `set_pot 100 100 0` after a
3-bet to 100); `set_pot 0 0 200` makes the 200 dead money.

## Known differences from PioSOLVER

- **Save files are not Pio `.cfr` files.** Pio's format is undocumented; ours is a gzip stream
  (see `src/upi_save.cpp`). `load_tree` cannot read Pio saves.
- **One algorithm.** `set_algorithm` accepts every Pio code but all map to DCFR;
  `list_algorithms` says so.
- **Verified against PioSOLVER-edge 3.10.1** with `tests/pio_compare/` (black-box: same UPI
  scripts on both, response-by-response diff). Formats, tree shapes (`node_count` totals match),
  hand order, ranges, equities (exact) and equilibrium EVs (to ~0.01 chips) agree. Remaining
  known differences: `list_algorithms` text, `show_all_freqs` (Pio prints `nan`), tie order in
  `show_nuts`, and `_pp` listings omit hands that are only in the opponent's range.
- **Range explorer** (`show_category_names`, `show_categories`, `show_cats`, `show_cats_pp`) is
  byte-identical to Pio 3.10 on 88 boards (48 fitting + 40 held-out); fixtures in
  `tests/data/pio_categories/`.
- **Preflop equity** (`calc_eq_preflop`, preflop all-ins) uses board sampling (about 0.3% from Pio's
  exact tables); matchups match exactly.
- `set_always_recalc`, `change_step`, `set_recalc_accuracy` are accepted and stored but only
  `solve_all_splits` uses recalc accuracy.

## Performance (Apple M4, 10 threads)

| Tree | Memory | Time |
|---|---|---|
| Wide SRP flop, 498 vs 322 combos, 1-2 sizes | 149 MB | 100 iterations in 2.3 s -> 0.06% pot |
| Real Pio 25bb HU script, 2 sizes everywhere | 6.4 GB (3.3 GB with `small_strats`) | ~1 s / iteration |

Single-threaded on the same tree (`tests/pio_compare/bench_flop.txt`), PioSOLVER-edge 3.10.1 on a Zen 5
runs 60 iterations in 4.6 s vs 6.9 s here, but this solver's DCFR reaches lower exploitability per
iteration (0.119 vs 0.182 chips after 60), so time-to-accuracy is within ~10%.

`build_tree` checks free memory and switches to compressed storage automatically when float32
storage would not fit (`small_strats auto`; force with `on` / `off`).

## Layout

```
src/cards.*        cards, Pio hand order, suit permutations
src/eval.*         5-7 card evaluator (exhaustively tested)
src/range.*        range text parser/formatter
src/action_tree.*  add_line/remove_line/force_line -> betting tree
src/game.*         full game tree (chance expansion, isomorphism, showdown orderings, storage)
src/solver.*       DCFR, best response, evaluation
src/parallel.*     nested work-stealing thread pool, per-thread arenas
src/equity.*       calc_eq family
src/categories.*   range explorer
src/upi*.cpp       command processor, node queries, save/load
tests/             unit tests (eval, range, categories, solver, tree builder) + end-to-end UPI test
tools/             helper scripts (tree_builder.py: bet-size config -> add_line commands)
```

## Contributing

Work is tracked in GitHub issues and lands through pull requests reviewed by the maintainer agent.
New agents: read [INSTRUCTIONS_FOR_SUBAGENTS.md](INSTRUCTIONS_FOR_SUBAGENTS.md). See [AGENTS.md](AGENTS.md) for the workflow (claiming issues, branches, PRs, review), the ground rules
(clean-room, tests green), the code map and the list of verified PioSOLVER behaviors.
