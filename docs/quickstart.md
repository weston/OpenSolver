# Quickstart

Solve your first spot in about five minutes. This is the user-facing guide; the
[README](../README.md) covers building, internals and Pio compatibility, and
[commands.md](commands.md) is the full command reference.

Every command and every piece of output below was run against the solver, not written from memory.

## 1. Install

```sh
make                 # builds ./solver
make install         # installs it as `wsolver` under /usr/local (may need sudo)
```

Or keep it local, no sudo and nothing written outside your home directory:

```sh
make install PREFIX=$HOME/.local
```

You need a C++20 compiler, make, Python 3 and zlib headers. On macOS the Command Line Tools cover
all of it.

## 2. Talk to it

The solver speaks **UPI**, a line-based text protocol, on stdin/stdout. One command per line, and
each replies with `<command> ok!` or `ERROR: ...`. That means you can drive it by hand, by pipe, or
from any language that can write to a pipe — there is no library to link.

```sh
./solver             # type commands, or pipe a script in
```

Everything below is one script. Save it as `spot.upi` and run `./solver < spot.upi`.

## 3. Describe the spot

```
set_range OOP AA,KK,QQ,JJ,AKs,AQs,AJs,KQs,AKo
set_range IP 99,88,77,66,ATs,A9s,KJs,QJs,JTs,T9s,AQo,KQo
set_board Qs8d7h
set_pot 0 0 20
set_eff_stack 100
```

- **`set_range`** takes ordinary range text (`22+`, `A5s-A2s`, `KK:0.5` for weights, `AhKd:0.3` for
  a single combo). It also accepts 1326 raw floats, which is the only form Pio takes.
- **`set_pot 0 0 20`** is *(OOP invested, IP invested, dead money)*. Here a 20-chip pot that neither
  player contributed to this street.
- **`set_eff_stack 100`** is the stack behind, per player.

## 4. Describe the betting

A tree is a set of **lines**, each a sequence of cumulative amounts the acting player has invested.
`0` means check or call-for-nothing.

```
add_line 10 30
add_line 0 10 30
```

The first line is OOP betting 10, then IP raising to 30. The second is OOP checking, then IP betting
10, then OOP raising to 30. Folds and calls are implicit — you only describe the aggressive actions.

> **Watch for this.** If every one of your lines starts with `0`, OOP can only ever check, and the
> root will have a single child. That is almost never what you meant. Check with
> `show_node r:0` — it prints `2 children` for the tree above.

For realistic trees with percentage-of-pot sizing, `tools/tree_builder.py` generates the `add_line`
sequence for you from sizing settings, the same way PioViewer does.

## 5. Solve

```
set_threads 4
build_tree
go 60 steps
wait_for_solver
calc_results
```

`go` also accepts `go 10 seconds`, or bare `go` to run until stopped. `wait_for_solver` blocks until
it finishes. Output:

```
EV OOP: 10.432
EV IP: 9.568
OOP's MES: 10.436
IP's MES: 9.578
Exploitable for: 0.012
```

Reading it:

- **EV OOP / EV IP** are the expected values of the current strategies, in chips. They sum to the
  pot (10.432 + 9.568 = 20).
- **MES** is each player's value against a *maximally exploitative* opponent — a best response.
- **Exploitable for** is the gap, and is the number that tells you whether the solve is done. Here
  0.012 chips on a 20-chip pot is under 0.1%, which is tight. As a rule of thumb 1% of pot is rough,
  0.5% is usable, 0.1% is tight.

This 36,031-node tree takes well under a second. `bench/results.md` has timings and memory for
larger trees, up to a 13 GB single-raise-pot.

## 6. Read the strategy

Node ids read like the line that reaches them: `r:0` is the root, `r:0:b10` is after a bet of 10,
`r:0:c` after a check.

```
show_strategy_pp r:0
```

```
AcKs:   0.743132  0.256868
AdKc:   0.184437  0.815563
AcJc:   0.000000  1.000000
```

One row per hand, one column per action **in the order `show_children` lists them** — here bet 10,
then check. So `AcKs` bets 74% of the time; `AcJc` always checks.

`show_strategy` gives the same thing as raw rows of 1326 floats in Pio's fixed combo order
(`show_hand_order` prints that order: `2d2c 2h2c 2h2d 2s2c ...`). Hands outside the range are 0, so
expect mostly zeros — that is normal, not a bug. Use `_pp` when a human is reading and the plain form
when a program is.

To see what actions exist and where they lead:

```
show_children r:0
```

```
child 0:
r:0:b10
IP_DEC
Qs 8d 7h
10 0 20
3 children
```

## 7. Save and come back to it

```
dump_tree "spot.cfr"
free_tree
load_tree "spot.cfr"
```

A reloaded tree gives identical results — `calc_results` after the round trip returns the same
numbers. Saves are compressed and loaded lazily per street, so reopening a big tree is fast. Add
`lossless` to keep exact regrets as well as strategies, or `no_turns` / `no_rivers` to store only the
early streets and re-solve the rest on demand.

## 8. Or use the browser viewer

```sh
python3 viewer/server.py --solver ./solver --port 8000
```

Then open `http://localhost:8000`. It gives you tree navigation, 13x13 strategy and EV matrices,
range display, subgame re-solves, and a library of saved spots — the same engine, without writing
UPI by hand.

## Practical notes

- **Threads.** `set_threads` scales well on large trees and poorly on tiny ones, where there is not
  enough work to spread. It does not change results. On Apple silicon, set it to the number of
  **performance** cores: scaling saturates there, and the efficiency cores add nothing (on a 20-core
  M1 Ultra, 16 and 20 threads are the same speed). Defaults to every logical core, which is harmless
  but not useful.
- **Memory.** It is the tree that costs memory, not the solver. `small_strats on` stores strategies
  as 16-bit values and roughly halves it, at a small accuracy cost; `small_strats auto` (the default)
  decides per tree. Run `estimate_schematic_tree` after setting the ranges and lines but before
  `build_tree` to see what a tree will cost — it answers in MB.
- **Results are deterministic.** The same script gives bit-identical output on the same build,
  whatever the thread count.

## Where next

- [commands.md](commands.md) — every UPI command, and how each compares with Pio.
- [README](../README.md#known-differences-from-piosolver) — known differences from PioSOLVER.
- `bench/results.md` — time and memory per tree; regenerate with `make bench`.
