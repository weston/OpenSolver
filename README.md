# OpenUPI-Solver

[![CI](https://github.com/weston/WSolver/actions/workflows/ci.yml/badge.svg)](https://github.com/weston/WSolver/actions/workflows/ci.yml)

A heads-up no-limit hold'em solver (postflop trees and full preflop trees) with a PioSOLVER-compatible text interface
(UPI, the Universal Poker Interface). It is an independent implementation written from the
public UPI documentation; it contains no PioSOLVER code.

- C++20, single binary, no dependencies beyond zlib (ships with macOS).
- Vector-form **Discounted CFR** (α=1.3, γ=4; β=0, then 0.5 after iteration 50) with exact card removal, no abstraction. `set_dcfr` overrides the exponents (`set_dcfr a b g` constant, `set_dcfr a b g b_late t` with the β switch).
- Multi-threaded over turn/river cards with a nested work-stealing pool.
- Suit **isomorphism** (flop trees on by default, like Pio's `set_isomorphism 1 0`).
- Optional 16-bit compressed storage (`small_strats`), roughly halving memory. The strategy sums are rounded stochastically, so it converges like float32 (#272, #293).
- **Regret-based pruning** on by default (`set_pruning off|on [period]`): a traverser action whose current strategy is exactly zero for every hand that can occur at the node (not counting hands holding a dealt turn or river card) is skipped (its regret row frozen, its subtree not walked) and revisited every `period`-th iteration (default 8, at least 2). The iteration's values are exact, but the frozen rows and skipped subtrees change the regrets and averages slightly, so results are close to, not bit-identical with, `set_pruning off` (#380). Worth about 13% on a deep tree.

New here? **[docs/quickstart.md](docs/quickstart.md)** walks through installing, solving a spot and
reading the output in about five minutes. The rest of this file is reference and internals.

## Build and run

```sh
make            # builds ./solver (-O3, native CPU optimizations)
make release    # builds ./solver-release: LTO, native arch (add PGO=1 to also train a profile)
make test       # unit tests + end-to-end UPI tests on a small tree (~1 min, < 1 GB RAM)
make test-big   # same, but the end-to-end tests solve the real 25bb tree (~5-10 min, ~4 GB free RAM)
python3 tools/fuzz.py invariants ./solver --seconds 300   # randomised checks; also concurrency, roundtrip, preflop, locks, truncated
make install    # installs the binary as `wsolver` under /usr/local (see below)
./solver        # talk UPI on stdin/stdout
```

### Installing

`make install` copies the binary to `$PREFIX/bin/wsolver` and the docs to
`$PREFIX/share/doc/wsolver`, with `PREFIX` defaulting to `/usr/local`:

```sh
make install                        # /usr/local/bin/wsolver (may need sudo)
make install PREFIX=$HOME/.local    # no sudo needed
make install DESTDIR=/tmp/stage     # staged install, for packaging
make uninstall                      # removes both
```

The installed command is `wsolver` rather than `solver`, which is too generic
for a shared `bin` directory; override it with `BINNAME=`. To install the
release build instead of the default one, point `INSTALL_BIN` at it:

```sh
make release && make install INSTALL_BIN=solver-release
```

### Source distribution

`make dist` writes `dist/wsolver-<version>-src.tar.gz` and a matching `.sha256`
so a recipient can verify what they downloaded:

```sh
make dist
shasum -a 256 -c dist/wsolver-0.1.0-src.tar.gz.sha256
```

It builds the tarball with `git archive`, so the contents are exactly what is
committed — no build output and no stray local files — and it warns when
uncommitted or untracked files are being left out.

### macOS installer

`make macos-pkg` writes `dist/WSolver-<version>-macos-arm64.pkg` and a matching `.sha256`: an
installer that puts **WSolver.app** in `/Applications` (#479). The app is the solver and the
[viewer](#viewer) with everything they need, so the Mac it is installed on needs no compiler, no
Python and no checkout. For Apple Silicon Macs with macOS 13.3 or later.

```sh
make macos-pkg
shasum -a 256 -c dist/WSolver-0.1.0-macos-arm64.pkg.sha256     # in dist/
open dist/WSolver-0.1.0-macos-arm64.pkg
```

Opening WSolver starts the viewer and opens it in the browser. A small window shows the address,
with **Open in Browser** and **Quit**; closing the window leaves the viewer running (a click on
the Dock icon brings the page back), and quitting WSolver stops the viewer and its solvers. It asks
first while a solve or the job queue is running. The viewer takes port 8765 when that is free and
any free port otherwise (when a viewer from a checkout is already running, say); the two then
share the library and the job queue. What the viewer prints goes to
`~/Library/Logs/WSolver/viewer.log` (File > Show Log).

- **What is in the app.** A launcher (`packaging/macos/launcher.m`), a solver built for any Apple
  Silicon Mac (`-mcpu=apple-m1` with LTO, where plain `make` builds for the machine it runs on),
  the viewer's files and the two tools it uses, and a Python to run them: a
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone) build of CPython
  3.13, pinned by checksum in `packaging/macos/build.sh`, downloaded on the first build into
  `build/macos/cache/` and trimmed of what a viewer has no use for (Tk, pip, the test suites). To
  build offline, set `PYTHON_TARBALL` to a copy of that file. About 11 MB to download, 30 MB installed.
- **Your data is not in the app.** Ranges, tree configs and the job queue stay in `~/.wsolver`,
  as for a viewer run from a checkout: installing a new version over an old one leaves them alone.
- **It is not signed with a Developer ID or notarized.** The app is ad-hoc signed, which is what
  lets it run on Apple Silicon at all; the package is unsigned. On the Mac that built it, it opens
  like any other. On another Mac, a package that was downloaded or sent is held back by Gatekeeper
  the first time: after trying to open it, allow it under System Settings > Privacy & Security >
  **Open Anyway**. Signing needs an Apple Developer account, which the project does not have.
- **Before it is packed, the app is run.** `build.sh` starts the staged app's viewer with the app's
  own Python in an empty environment, builds and solves a small tree through it and checks that
  nothing is left running afterwards (`packaging/macos/smoke.py`); a package is written only when
  that passes. `make test` checks, without building anything, that every module the viewer imports
  and every file its page loads is on the list of what goes in.
- **Uninstalling:** quit WSolver, then
  `sudo rm -rf /Applications/WSolver.app && sudo pkgutil --forget com.wsolver.WSolver.pkg`.
  `~/.wsolver` and `~/Library/Logs/WSolver` are yours to keep or delete.

`make release` leaves the default build untouched and produces a second binary,
`./solver-release`, with link-time optimization. Results are identical to the
default build.

Profile-guided optimization is available but **off by default**, because it
measures at parity rather than as a win:

```sh
make release PGO=1
```

Measured on an M1 Ultra (`flop_srp_2`, 60 steps, 1 thread, interleaved runs,
8 reps), relative to the default `make` build: LTO alone is -0.2%, LTO plus PGO
is -0.1%. Since `PGO=1` roughly doubles release build time for no measured gain,
it stays opt-in. It is worth re-testing on your own hardware and workload, where
a profile may help more than it does here -- our hot loop is a single
gather-bound reduction with little branch layout left to improve.

`PGO=1` trains a profile on `tests/pio_compare/bench_flop.txt` and the bench
river/turn trees, single- and multi-threaded, via `bench/train_pgo.sh`, and
needs `llvm-profdata` under Clang (shipped with the toolchain; on macOS it is
found via `xcrun`).

One warning if you change the PGO flags: use **IR-level** instrumentation
(`-fprofile-generate`), never clang's front-end instrumentation
(`-fprofile-instr-generate`). The latter counts function entries before
inlining, so functions the instrumented build inlines record entry count 0;
clang then treats them as cold and refuses to inline them in the optimised
build. That cost us 12% by un-inlining the showdown kernel, which is 49-65% of
runtime. See #199 and the comment in the Makefile.

The native CPU flag (`-mcpu=native` on ARM, `-march=native` on x86) also applies
to the release build; override `RELEASE_ARCH` for portable binaries, e.g.
`make release RELEASE_ARCH=-march=x86-64-v3` on Linux or
`make release RELEASE_ARCH=-mcpu=apple-m1` on Apple Silicon.

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
python3 tools/tree_builder.py config.txt --pio-compat   # legacy donk fallback (below)
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
`BetSize`, `DonkBetSize`, `RaiseSize` (size lists, entries separated by commas, spaces or
both), `AddAllin` and `RaiseCap`. Entries of a size list are not just percentages:

| Form | Meaning |
|---|---|
| `75` | a percentage of the pot (bets) or of `pot_if_called` (raises), as before |
| `2e`, `3e` | geometric over N streets (see below) |
| `e` | geometric over the streets remaining (3 on the flop, 2 on the turn, 1 on the river) |
| `2e75`, `e50` | geometric, but at most that percentage of the pot |
| `3x`, `2.5x` | raise by N times the increment faced: `mine + N x (facing - mine)` - only in `RaiseSize` |
| `a` | the all-in |

and they mix freely: `33, 75, 2e, 2e50, 3x, a`.

**Geometric sizes.** `Ne` is the pot fraction f that, bet and called on each of the next
N streets counting this one, puts the remaining effective stack S in exactly. With P the
current pot: `f = ((1 + 2·S/P)^(1/N) − 1) / 2`. Example: pot 100, stack 300 (SPR 3) gives
`2e` = (√7−1)/2 = 82.3% -> a bet of 82. For a raise, the same formula runs on the
pot-if-called with S the stack left after calling, and the raise increment is the
geometric bet on that pot: `raise to facing + round(f x pot_if_called)` (so `1e` as a
raise is the all-in). After a called geometric bet or raise, the same fraction continued
on the remaining streets puts the stack in exactly, up to rounding to whole chips.

**Raise cap.** PioViewer's `#CapEnabled#True` with `#CapPerStreet#4\n4\n3` (flop, turn, river;
a literal backslash-n) makes the N-th bet or raise of that street the all-in only, and the
all-in is offered there even if no size asks for it; `0` leaves the street uncapped. Our own
`#<Street>Config[IP].RaiseCap#N` limits the street to N bets and raises in total, counting
both players (if the two players' values differ, the smaller applies); after that only a
configured all-in (an `a` entry, or `AddAllin` whose pot gate below passes) is still offered.

**Other PioViewer settings.** `MergeSimilarBets` / `MergeSimilarBetsThreshold`,
`UseUnifiedRaiseAfterRaise` / `UnifiedRaiseAfterRaise`, `ForceOOPBet` / `ForceIPBet` (no
check on the flop) and the hand edits `RemovedLines_2` / `ExtraLines_2` are read too; the
exact rules are in `python3 tools/tree_builder.py --rules`.

Amounts in a line are absolute - the total a player has put in since the hand started - so a
check or call repeats the amount that player is already matched to. With
`pot = dead pot + both investments` and `pot_if_called = dead pot + 2 x the larger investment`:

| Action | Total |
|---|---|
| bet | `round(invested + BetSize x pot)`, for BetSize a percentage or geometric fraction, or `DonkBetSize` when OOP leads into the previous street's aggressor |
| raise | `round(facing + RaiseSize x pot_if_called)`, or `mine + N x (facing - mine)` for an `Nx` entry; at least a min-raise, `facing + (facing - mine)` |

Rounding is half to even, applied to the total (`925 + 33% x 5550 = 2756.5` gives 2756).
On top of the sizes:

- **`AllinThreshold%`** - a bet or raise whose total is strictly above that percentage of the
  effective stack is replaced by the all-in. At 67% of a 230 stack, any size from 155 up
  becomes 230, which is why the deepest action of every branch is an all-in.
- **`<Street>Config[IP].AddAllin`** - offers the all-in as an extra size next to the configured
  ones (never on its own), but only while the stack left once the pending bet is called is
  below `AddAllinOnlyIfLessThanThisTimesThePot%` of `pot_if_called`. On a 40-chip river pot,
  300% is 120 and 230 is behind, so no all-in is offered; facing a 26 bet the pot-if-called is
  92, 300% of it is 276, 204 is behind, and the all-in joins the raise.
- **`MergeSimilarBets`** - from the largest size down, a size is dropped when it is closer to
  the last size kept than `MergeSimilarBetsThreshold%` of the pot once the smaller size is in.

**Donk rule (#152).** When OOP leads into the previous street's aggressor (IP bet or raised
and OOP called), only `DonkBetSize` sizes are offered. An empty donk list means exactly that -
OOP cannot lead at all (check only), and `AddAllin` adds no all-in lead either. PioViewer
does the same (all 145 configs below reproduce with it, 25 with a `BetSize` fallback), so the
fallback, `--pio-compat`, is only kept for old scripts. After OOP checks, IP's bet still uses
`BetSize`.

Only maximal lines are emitted: the solver fills in check, call and fold at every node it
creates, so a line only has to reach the deepest bet of its branch.

These rules were inferred black-box from PioViewer's output: `tests/data/pio_25bbHU_2sizes.txt`,
which carries a config header and the 266 lines PioViewer generated from it (checked by
`tests/test_tree_builder.py`), and 145 real PioViewer tree configs on the owner's PC, header
plus generated lines, which the builder reproduces exactly (`--compare`; #313). Those 145 files
are private and not in the repository; the unit tests use synthetic configs for each rule.

## Job queue: one tree on many flops

`tools/jobs.py` queues solves of one tree setup over a list of boards and runs them one at a time, each
in a fresh solver process with all threads, saving each to its own file. The queue is on disk
(`~/.wsolver/jobs`, or `--queue DIR` / `WSOLVER_JOBS`), so you can queue many batches, run them later,
run just one job, add or reorder jobs while a run is going, and continue after a crash.

```
python3 tools/jobs.py add srp100 tree.txt --flops-file flops/wsolver_25.txt
python3 tools/jobs.py add test tree.txt --flops "Kh9h4h, As7d2c" --accuracy 0.3 --save full
python3 tools/jobs.py list                  # id, status, board, weight, EVs, exploitability, time
python3 tools/jobs.py run                   # everything queued, in order
python3 tools/jobs.py run 00007             # just one job (or several ids, or --batch NAME, --limit N)
python3 tools/jobs.py stop [--now]          # from another terminal: after this job / right now
python3 tools/jobs.py retry --failed        # also: remove, move ID --to 1, show ID
python3 tools/jobs.py export srp100 -o srp100.csv   # board, weight, EVs, MES, exploitability, ...
python3 tools/jobs.py report srp100 [--by suits] [--csv FILE]   # root strategy per flop, by texture, weighted
```

- **The tree** is a UPI script (set_range, set_pot, set_eff_stack, add_line, ...) or a PioViewer-style
  config with `#Key#Value` sizing headers, expanded with the tree builder above. Line amounts don't depend
  on the board, so one setup fits every flop. `set_board`, `build_tree`, `go`, `dump_tree` and other run
  commands in the file are ignored. The setup is copied into the batch when you add it. `add` checks the
  tree on the first board (`estimate_schematic_tree`, which builds nothing) and prints its size, so a
  bad line fails at once rather than once per flop.
- **Flops:** a list or a file with one board per line, each optionally followed by `:weight`; `#` starts a
  comment. `flops/wsolver_<N>.txt` are our own subsets of 10 to 200 flops (see "Flop subsets" below). It is
  also the format of the `preflop_subsets` flop files in a PioSOLVER install; for those pass your own copy's
  path (none ship with this repo). Weights go into `list` and `export`.
- **Each job:** `set_board`, `build_tree`, `set_accuracy <pct> fraction` (default 0.5% of the starting pot;
  `--max-iters` or `--max-seconds` cap it), `go`, then `calc_results` into the job and `dump_tree` to
  `<queue>/saves/<batch>/<board>.cfr` (`--out DIR`). Saves are `no_rivers` by default (`--save no_turns|full`):
  rivers are re-solved when you browse them, and `calc_results` on the save reports this solve's numbers (#195, with #198).
- **Aggregate report:** each job also records the first decision's strategy over the acting player's whole
  range. `report` lists it per flop with EV and exploitability, plus the flop-weighted average, for
  example `bet 33 (33%) 48.2% · check 51.8%` over a subset. Like PioViewer's aggregation report, hands
  count by reach × matchups (the opponent's combos they don't block), and each flop counts by its weight
  times its total matchups, since boards block different numbers of combos (#323). Recomputing from
  PioViewer's own per-hand report files reproduces its rows to their rounding. `export` carries the same columns, and the
  Jobs tab has a Report button per batch. Saves from jobs that finished before this was recorded are
  read once to fill it in. `--by high|suits|pairing|straight` (a select in the Report dialog) groups the
  flops by texture: top card, rainbow / two-tone / monotone, paired, and whether three ranks fit a
  straight. Each group gets its flop-weighted strategy and EVs, and the CSV carries the four texture columns.
- **Report at a line** (#325): `report NAME --line r:0:b198:c:2d:c:b447` (or the Line field in the Report dialog,
  which takes a node id from Study) reports the decision at that line on every flop: the strategy, both EVs and Global %
  (how often the line is reached: its matchups over the root's, `calc_global_freq`). A card in the line (or `*`)
  means every card dealt there, as in PioViewer's turn report. Each flop then gets one row per card (`--cards`, *Show
  each card*), and the flop's row rolls them up: cards weighted by their matchups, Global % their plain mean. The
  first report at a line loads every save once; the results are kept in the jobs. `--csv` writes PioViewer's layout
  (Flop, Turn, Global %, EVs, frequencies). With a job's save open in Study (*Open* in Jobs), **Report…** next to
  Lock… runs this report at the node you are on (#331).
- **Per hand** (#336): `report NAME [--line L] --hands FILE` writes each hand class's EV, equity, equity realization
  (EV over equity × pot) and matchups for both players over the batch, in PioViewer's handsEV.csv layout (EV and
  equity in % of the node's pot, plus EV in chips). Each combo counts by reach × matchups on every flop, which
  reproduces PioViewer's own handsEV figures from its per-combo files exactly. It works at the root too (the saves
  are read once, like a line).
- **Per combo** (#346): `report NAME [--line L] --full PREFIX` writes PioViewer's `PREFIX_OOP_Full.csv` and
  `PREFIX_IP_Full.csv`: one row per flop (and card, at a turn line) per combo in range, with its weight, EV (% of the
  node's pot, plus chips) and the acting player's frequencies. One pass over the saves, not cached; a 102-flop turn
  report is millions of rows, as in PioViewer.
- A job that fails (the solver errors or exits) is marked `failed` with the error and the run goes on.
  Ctrl-C or `stop --now` puts the current job back in the queue, also mid-build (the job's solver is ended); a
  stop once the solve is over lets its save finish first, so the solve is kept. One runner at a time (a lock in the queue directory).
- The solver must see the same paths as the script: run it where the solver runs (for WSL, the Linux
  build with `/mnt/c/...` paths).
- **In the viewer:** the **Jobs** tab shows the same queue. It can:
  - queue the Build tab's tree on a flop list, or on a flop file from `--flops-dir`
    (default: `$WSOLVER_FLOPS_DIR`, or `C:\PioSOLVER\preflop_subsets` if it exists, or the `flops/` that
    ships here);
  - run everything, one batch or one job; stop after the current job or right away;
  - retry, reorder or remove jobs;
  - open a finished save in Study.

  Jobs run in their own solver processes, so browsing stays responsive. `viewer/server.py --jobs DIR` picks the queue directory.

## Preflop solves

`tools/preflop.py` solves the heads-up game from the blinds on: it takes the stack and the sizes in big
blinds, writes the script the solver needs (*Preflop trees* below has the commands), says how much memory
that tree takes before anything is built, solves it and prints the preflop strategy as 13x13 charts.

```
python3 tools/preflop.py solve hu100.json                 # 100 bb, the default sizes
python3 tools/preflop.py solve hu40.json --stack 40 --open 2,2.5 --3bet 3.5x --no-limp
python3 tools/preflop.py estimate --stack 40              # memory per number of flops and depth
python3 tools/preflop.py tree                             # the preflop lines and their node ids
python3 tools/preflop.py chart hu100.json r:0:b250 --ev   # one decision again, with the EVs
python3 tools/preflop.py check hu100.json --flops all     # what that strategy gives up on all flops
python3 tools/preflop.py script > hu100.upi               # the commands only; ./solver < hu100.upi
```

**The game.** The small blind is the button: it acts first before the flop and last after it (so it is
`IP` and the big blind `OOP` in the solver's terms). `--stack` is the effective stack in big blinds,
`--ante` dead money in the pot, `--range-sb` / `--range-bb` ranges other than every hand. A big blind is
100 chips (`--chips-per-bb`; the solver's amounts stop at 65,535 chips, so a stack over 655 big blinds
needs fewer).

**Preflop sizes.** `2.5` is a raise to 2.5 big blinds, `4x` a raise to four times the bet faced, `allin`
the stack; several sizes separated by commas are several branches. `--open` (default `2.5`), `--iso`, the
big blind's raise over a limp (`3.5`), `--3bet` (`4x`), `--limp-raise` (the 3-bet sizes), `--4bet`
(`2.3x`), `--5bet` (`allin`; every raise after it is all-in too). `--no-limp` makes the small blind raise
or fold. A raise to more than `--allin-threshold` percent of the stack (50) is made all-in, and a size
under the minimum raise is refused with the option that asked for it. `tree` prints what that gives:

```
start                  r:0                                in: 0.5 / 1       SB to act
  SB raise 2.5         r:0:b250                           in: 2.5 / 1       BB to act
    BB raise 10        r:0:b250:b1000                     in: 2.5 / 10      SB to act
      ...
    BB call            r:0:b250:c                         in: 2.5 / 2.5     flop, 1 postflop line
    BB fold            r:0:b250:f                         in: 2.5 / 1       hand over
  SB limp              r:0:c                              in: 1 / 1         BB to act
```

**After the flop.** Every line that ends in a call before the stacks are in goes to a flop, and each of
those exits gets a postflop tree in which the pot and the stacks are the ones the preflop action left.
`--depth` is the last street with betting: `flop` (the default), `turn`, `river`, or `none`; the streets
after it are checked down, each as one showdown node (`set_collapse_checkdowns`). The sizes are percent
of the pot under the rules of the tree builder: `--flop-bet`, `--turn-bet`, `--river-bet` (50), `--raise`
(60), `--raise-cap` bets and raises per street (2; the tree builder still offers an all-in after them
when its all-in rule says so), `--donk` for a lead into the preflop raiser (none), or `--postflop FILE`
with a whole `#Key#Value` configuration (*Tree builder* above). A solve whose postflop tree stops at the
flop plays the turn and the river as if nobody could bet there, which is a different game from poker, and
the larger error of the two a preflop solve makes (the other is the flop subset, below). Measured one
street on, with 10 flops (`check`, below): the default `--depth flop` strategy gives up 1.4 (small blind)
and 1.9 bb/100 (big blind) in the game with turn bets (1.6 and 2.2 with 25 flops, 1.6 and 1.8 with 50), a `--depth none`
strategy 8.0 and 7.5. What the
river's bets add to that has not been measured (86 GB for 10 flops).

**Flops and memory.** The tree holds a subset of flops, each with a weight and a copy of every exit's
postflop tree, so memory goes with the number of flops and steeply with the depth. `estimate` asks the
solver (`estimate_tree`) and prints megabytes for the subsets in `flops/`. The default tree at 100 big
blinds:

| `--depth` | 10 flops | 25 | 50 | 100 | 200 | all 1,755 |
|---|---|---|---|---|---|---|
| none | 13 | 33 | 66 | 132 | 264 | 2,317 |
| flop | 18 | 45 | 91 | 182 | 364 | 3,194 |
| turn | 802 | 2,039 | 4,049 | 7,948 | 15,573 | 121,802 |
| river | 85,988 | 221,624 | 437,639 | 854,547 | 1,681,788 | |

Down to `flop` nearly all of that is one table per flop, 1.4 MB with every hand in both ranges: how the
flop's 1,176 turn-and-river runouts come out, hand against hand, which is what every all-in and every
checked-down line of that flop is valued on (#494; before it these trees kept the runouts'
showdown boards and were about four times the size. #496 halved them again: when both players hold the
same hands a table keeps each pair of hands once, and only the hands the flop leaves). From `turn` on
the smallest tree keeps the boards instead, and the table above is that tree. Tables are the faster way
to value an all-in on the turn as well, so the solver builds them there too when the tree with them fits
in memory (`set_outcome_tables`, `auto` by default; `small` for the tree of the table): 888 MB for the
802 of 10 flops, 2,270 for 2,039, 4,509 for 4,049, and iterations about twice as fast. The solver process
takes more than the figure: 462 MB at its peak for the 200 flops estimated at 364, 3,455 for all 1,755 estimated at 3,194, 892 for 10 flops down to
`turn` estimated at 802 (1,004 with the turn tables, estimated at 888). The estimate is not free at
`turn` and `river`: the solver builds the tree without the
strategy storage to count it, boards included (down to `flop` there are none to build, and all flops
take half a second). `--flops` takes a number we ship a
file for (10, 25, 50, 100, 200), a flop file (`flop:weight` lines, the format of `flops/`), or `all` for
the 1,755 suit classes with the number of flops each stands for. Without it the tool takes the largest
shipped subset whose estimate is under `--memory` gigabytes (default half of the machine's, which leaves
room for that) and says which. What a subset costs, for the default tree: the preflop strategy solved on it, played in the
game with all 1,755 flops against an opponent who knows it, gives up this many bb/100 (`check`, below;
both solves to 0.02, each figure good to 0.04):

| flops | 10 | 25 | 50 | 100 | 200 |
|---|---|---|---|---|---|
| small blind | 1.43 | 0.47 | 0.19 | 0.07 | 0.02 |
| big blind | 1.03 | 0.53 | 0.17 | 0.04 | 0.01 |

So 200 flops are not told from all of them. A solve to the default accuracy gives up 0.17 by itself, as
much as 50 flops cost and several times what 100 or 200 do; `--flops all` (4.6 minutes instead of half a minute) buys nothing that
the missing turn and river bets do not cost many times over. *Flop subsets* below says how the files
were chosen.

**The solve.** `solve OUT.json` runs until the tree is exploitable for less than `--accuracy` big blinds
per 100 hands (default 0.5) or for `--time` seconds (default a day; Ctrl-C or a `kill` stops the solver
with it and leaves no result), with progress on stderr, and writes the preflop
strategies of every decision to `OUT.json`; `--save-tree FILE` also saves the solved tree (`dump_tree`
without rivers) to open in the viewer. On an M4 (10 cores, 16 GB) the first command above took the 200
flops and 31 seconds, 26 of them solving; `--flops 10` takes 2 seconds, with the caveat of a small
subset, and `--flops all` 4.6 minutes and 7.1 GB.

**The charts.** One block per decision, in the order of `tree`, one chart per action:

```
r:0:b250   SB raise 2.5   ->  BB to act
  45.9 % of deals get here; then raise 10 19.2 %, call 56.5 %, fold 24.2 %

raise 10 (19.2 %)                          call (56.5 %)                              fold (24.2 %)
   A  K  Q  J  T  9  8  7  6  5  4  3  2      A  K  Q  J  T  9  8  7  6  5  4  3  2      A  K  Q  J  T  9  8  7  6  5  4  3  2
A ** ** ** ** ** 64  2 **  1 61 32 11  .   A  .  .  .  .  . 36 98  . 99 39 68 89 **   A  .  .  .  .  .  .  .  .  .  .  .  .  .
K ** ** ** ** **  .  7 **  .  . 49  .  .   K  .  .  .  .  . ** 93  . ** ** 51 ** **   K  .  .  .  .  .  .  .  .  .  .  .  .  .
```

A cell is how often the hand takes the action once it is at this decision, in percent: `**` always (99.5
or more), `.` never (under half a percent), blank when the hand never gets here. Suited hands are above
the diagonal, offsuit below. The line under the heading is how often the deal gets to this decision at
all, and the split of the actions there. Everything counts deals: a combo weighs by the opponent's hands
that share no card with it and get there too, so a decision is reached as often as its parent, times the
action's share there, and with a range of single combos a hand the opponent's cards rule out counts for
nothing. `--ev` adds a chart of each hand's EV at the decision in big blinds won or
lost from the start of the hand, blinds included (a hand that folds the big blind is at -1.00); the small
blind's EVs at `r:0`, weighted by combos, are the value of the game to it, the first line of the output.
`chart OUT.json [NODE ...]` prints the charts of a saved result again, all or the named decisions.
`OUT.json` holds the same per hand class: `nodes[].hands["AKs"]` = `{reach, freq[], ev_bb}`.

**Checking a result.** `check OUT.json [OUT2.json ...]` says what a result's preflop strategy gives up in
a larger game than the one it was solved in: more flops (`--flops`, `all` where they fit) or more streets
of betting (`--depth`; without it the result's). It builds that game and solves it; then, one player at a
time, it locks the result's strategy into that player's preflop decisions (every combo of a hand class
plays the class's frequencies, as the charts show them), solves everything else against it (the
opponent's preflop play and all play after the flop, the locked player's too) and compares the two values:

```
$ python3 tools/preflop.py check hu10.json hu50.json hu200.json hu200fine.json huall.json --flops all
The game with 1755 flops (all 1,755 flops, --depth flop), solved to 0.031 bb/100: worth 1.24 bb/100 to the small blind (1.21 to 1.28).
What a preflop strategy gives up in it to an opponent who knows the strategy, bb/100, and the
span the solves leave open:

                               small blind                big blind
hu10.json (10 flops)           1.64  (1.57 to 1.73)         1.26  (1.19 to 1.35)
hu50.json (50 flops)           0.34  (0.27 to 0.43)         0.35  (0.28 to 0.44)
hu200.json (200 flops)         0.17  (0.10 to 0.26)         0.18  (0.11 to 0.27)
hu200fine.json (200 flops)     0.02  (-0.05 to 0.11)        0.00  (-0.07 to 0.09)
huall.json (1755 flops)        0.17  (0.09 to 0.26)         0.18  (0.11 to 0.27)
```

(`solve` with `--flops 10`, `50`, `200` and `all` at the default accuracy, and `hu200fine.json` with 200
flops and `--accuracy 0.02`. A solve to the default 0.5 bb/100 gives up 0.17 a player by itself, the last
row; the 200 flops add nothing to that, and tightening the accuracy takes it away.)

The first number is the difference of the two solves' EVs; the span is what the two solves leave open,
each being exploitable for up to `--accuracy` (default 0.05 bb/100, so the span is about 0.16 wide whatever
is checked; a loss inside it is not told from none). It is what the preflop strategy can no longer
secure against an opponent who knows it and plays the best reply, however well its player plays after the
flop; against an opponent who does not adapt it costs less, or nothing. The preflop options have to be
those of the checked solve (stack, ante, sizes, ranges); the postflop options and the flops are the
check's own, and it says when a result was solved with another postflop tree. A result solved with other
ranges is refused where its hand classes show it: a class with a strategy that the check's range does not
hold (any such class), or classes without one at the small blind's first decision, or that the range
holds whole without one at the big blind's, where those classes together are dealt often enough to show
in the second decimal (ranges that differ only in weights or suits are not seen: a result does not record
its ranges). A decision the result's opponent never brings about has no frequencies in the result,
and one that only some hands came to has them for only those. A decision is locked whole or not at all:
the check leaves such a decision to the solve and names it, so the loss is that of what the result says
at the others, and can only be understated by it. (It is locked all the same where the hands without
frequencies come too rarely to show in the second decimal.) A result that is not all of a strategy is
refused: one with nothing for hands at a decision that its own two strategies bring them to, as a file
cut short or changed by hand can be (looked for when both ranges hold whole hand classes at one weight).
Frequencies that do not add up to 1 at a hand class and decision are checked made to add up to 1. A
solve writes rows that do; for a file whose rows leave something open the check says what that can
move, and names the row that counts the most in it. A row leaves open what is missing under 1, and
over 1 what its entries (none counted as more than 1) have over 1; that part, times how often the
result's own play brings the class to the row, is added up over the class's locked rows and counted
as all of the class at the most; times the class's share of the deals and two stacks and the ante it
is the figure, said when it is not 0.00 bb/100. It is a bound, not a loss: no strategy that respects
the rows as written is further from the checked one, and most are much nearer. The report has it as
`rescaled_bb100` with the row as `rescaled_most` (class, decision, what the row adds up to; of rows
that count the same, the first met going down the tree, all under a decision's first action before
its next, and at one decision the first in the range; a row one rounding step off 1 is
named there too, with a figure far under what a note takes), and
`unsaid_bb100` for the hands a locked decision has nothing for. Results written before this change
have rows as the solver's seven digits left them, which shows from deep stacks on (a note at
1200 bb and at every deeper stack measured, none at 1100 and under, in a game on three flops with no
bets after the flop).
Ranges that hold a hand class in part (single combos, combos at different weights) are checked
with a note: the combos of a class need not play alike there, a result has one strategy for a class, and
the loss includes what playing them alike costs. On all flops the two values of a solve add up to zero
(to the ante, with one) and the spans are bounds. On a flop subset they do too, as the tool builds it:
its script has `set_zero_sum_subset on`, which deals a pair of hands at one weight for both players
(#513). Without it a subset is not dealt quite evenly to the two and the values did not add up: by up to
0.07 bb/100 on the shipped subsets, and by 2.8 with three flops and narrow ranges, where the same check
at another `--accuracy` printed a loss outside the first one's span. The check still prints what the
values added up to, less the ante, when it shows, which is then how far the solver's own figures are
from exact (`bench/where-the-time-goes.md`). Time: a solve of the
larger game for the reference and one for each player of each result, each from the start: on all flops
of the default tree about 11 minutes for the reference and 3.5 to 5 for each of the others on the M4, 52
minutes for the five results above; `--time`
is the limit of each.

**In the viewer.** Open the tree saved with `--save-tree` (Open in the top bar, then the file's path):
Study shows it like any other tree, starting at the small blind's first decision with no board. A raise is
named by its size over the pot once the bet in front is called, with the amount it is to (`Raise 75% to
250` is the open to 2.5 big blinds of 100 chips; its tooltip adds `2.5x`), and the text beside "to act"
says which blind that is and what the call costs. After a preflop call the strip lists **the flops of the
tree**, highest cards first; type ranks or cards in its filter (`K8`, `KK`, `Ah7`) and click one, or
press Enter for the first. On a flop, click a board card to change to another flop of the tree on the
same line. EVs are as everywhere in the viewer a percentage of the pot at the node, and count from the
start of the hand: a hand that folds the big blind to a 2.5 open is at -100 chips, -29% of the 350 in
the pot. Lock works at preflop nodes; Sizes (a re-solve with other sizes) starts on the flop, and
Solve further is off as for any tree opened from a file.

The same tree can be set up and solved in the viewer without the command line: **Build → Preflop**
(under *Viewer* below) is a form over this tool, with the same defaults and the same checks.

## Viewer

A PioViewer-style browser UI, driven by a small local server that talks to `./solver` over UPI
(Python 3 standard library only, plain HTML/JS, no build step):

```sh
make
python3 viewer/server.py            # then open http://127.0.0.1:8765
python3 viewer/server.py --solver ./solver-release --port 9000
```

The server binds to 127.0.0.1 only and exposes a fixed, whitelisted JSON API — there is no
"run any UPI command" endpoint. Tree building and solving, plus `dump_tree` to a path you
choose, are the only state-changing operations.

Two views, light and dark themes (follows the system; toggle in the top bar):

- **Build:**
  - two 13×13 range editors side by side (OOP left, IP right). Click or drag to paint hands at the
    weight set by the slider (0–100%); clicking a hand that already has that weight clears it.
    The text box under each grid stays in sync both ways, in the solver's range syntax
    (`AA,AKs:0.5,AsKs,22+,A5s-A2s`).
  - a card-button board picker;
  - pot, stack, and bet/raise sizes per street **for OOP and IP separately**, plus OOP donk sizes
    (expanded with `tools/tree_builder.py`: `33` % pot, `2e` geometric over 2 streets, `3x` raise
    to 3×, `a` all-in), a per-street raise cap and all-in rules. API fields: `{street}_bet`,
    `_raise`, `_donk` and `_allin` are OOP's, and IP's too unless `{street}_bet_ip`, `_raise_ip` or
    `_allin_ip` are given (#141). Isomorphism is always on.
  - a library (`~/.wsolver/library`, or `--library DIR`): save and load single ranges from each
    editor, or whole spots (ranges, board, sizes, target) from the spot bar, in `/`-separated
    folders, with search, preview, rename and delete.
  - **Build & solve** with a target in % of the pot, and a live convergence chart
    (exploitability in % of the pot against time, linear).
  - **Postflop / Preflop** at the top chooses the tree. *Preflop* is the heads-up game from the blinds,
    the tree of `tools/preflop.py` (*Preflop solves* above) as a form: the two blinds' ranges in the
    same editors (big blind left, small blind right; all hands to start with), stack and ante in big
    blinds, the sizes of the open, the raise over a limp, the 3-bet, 4-bet and 5-bet, whether the small
    blind can limp, and for after the flop the number of flops (10 to 200 of `flops/`, or all 1,755),
    the last street with betting and its sizes. The tree these settings make is listed under the sizes
    as you type (each action, what both players have in, and where it leads), or the reason there is
    none in the form's own words. **Estimate memory** asks the solver what the tree takes, for the
    chosen number of flops and then for the other numbers (a row under the button; a click on one takes
    it, and all 1,755 flops are only asked for when chosen, that one takes a minute); **Build**
    does that first too, and refuses a tree larger than the machine's memory before it touches the
    one that is there. The target is in **bb/100**, and the chart, the top bar and the Exploitability
    tile count in bb/100 for a preflop tree; EVs stay a percentage of the pot (the 1.5 big blinds of
    the blinds at the root). No fast preview, spots or job queue for preflop trees. Study then shows
    the tree as described under *Preflop solves*. API: `GET /api/preflop/defaults`, and `POST
    /api/preflop/describe`, `/estimate` and `/build` with the form as JSON.
- **Study:** a fixed layout, so nothing moves while you click through a line:
  - the board as card graphics and the line so far as colored action chips grouped by street
    (click one to return to that decision);
  - above them on the right: pot, behind, SPR and each player's range EV as % of the pot
    (matchup-weighted, like Pio's);
  - an action strip with the overall frequency bar and one button per action (frequency and
    combos). At chance nodes the strip becomes a card picker, and the matrix keeps showing the
    last decision.
  - the 13×13 matrix. **Bar height is the hand's weight in the acting player's range at the node**
    (not normalized, so rarely reached hands show short bars), split by action frequency.
    Strategy, Range and EV modes; OOP/IP for the latter two.
  - side panel, four tabs:
    - **Hand** tab: per-combo strategy and EV for the hovered or pinned hand.
    - **Cats** tab: one table row per made-hand category and draw. Each row has combos, share
      of the range, EV (% of pot) and, for the player to act, how often it takes each action
      (cells shaded by frequency; hover a row for all of it, click to light its hands up in the
      matrix).
    - **Blockers** tab: pick a combo of the player to act (type it, click one of the pinned class's
      combos, or click a combo in the Hand tab) to see what its cards remove from the other range.
      One line of numbers: how much of the other range it blocks, that range's eq given the hand
      (and the change), and the hand's own eq. **responds**: for each action of the player to act,
      a stacked bar of the other player's answer given the hand (ticks mark the whole range's
      split; hover for both) and the change of each answer in points. **range shifts**: the made-hand
      categories, draws or hand classes that get thinner or thicker, as diverging bars.
      **Blocked combos** (folded) lists them by class. **Single cards**: a 52-card heat map of how
      much holding each card moves a number you choose (the other player's eq, or any of its
      answers); **No board ranks** leaves out the cards that pair the board (a K on the board
      leaves out all four kings). The eq is matchup-weighted, like Pio's range equity. Holding a
      hand changes no strategy; it only rules out the opponent's combos that share its cards.
    - **Graphs** tab: each player's range sorted by **equity** (against the other range) or **EV**,
      plotted against the share of the range it makes up (weakest first, so the curves rise), both players on one
      chart; hover for the combo at any point. Below: average (matchup-weighted, like Pio's), median,
      and the share with 50%+ equity or the EV as % of the pot. At a fold or showdown it shows the
      node above.

  The side panel takes a quarter of the window; drag the grip on its left edge to resize it (double-click to
  reset). The width is remembered.

  Keys: ← or Backspace goes up one node, Esc unpins. The URL (`#study=r:0:b12&mode=ev&pin=AQs`,
  `&side=blockers&blk=AhKd`, `&side=graphs`) links to the current node and view.
- **Sizes (#117):** at any decision, **Sizes…** opens a small panel with the acting player's sizes
  there (bets as % of the pot; raises as % of the pot after the call, or `2.5x`; `a` = all-in).
  - **Solve:** edit them and press Solve (or Enter). The subtree is re-solved in a second solver
    process and then shown **in place**: the same tree, line, tabs and URLs, with the new strategies
    from that node down. A **Custom sizes** tag marks it, and the line marks the node it starts at.
  - **Find best:** keeps the best N of a candidate list for the acting player (**+ all-in** offers
    the all-in beside every subset). The table ranks them by that player's EV, the best is shown in
    place, and **Show** on any row solves and shows that one instead.
  - **Restore original** goes back to the original solve everywhere. Locking, unlocking, Solve
    further or a new tree also restore it, since the re-solve used the old ranges.
  - *Other sizes and accuracy* holds the sizes the subtree uses after this decision: the tree's own
    sizes as built (the Build form's for a tree loaded from a file).
  - This is unsafe re-solving, like Pio's: the opponent's range stays fixed at the node, and nothing
    is saved.
- **Solve further (#153):** at a solved decision, **Solve further…** continues solving from that
  node down on the *main* tree (the `solve_partial` path), to a target accuracy in % of the
  node's pot, with live progress and a Stop button. Strategies at and below the node change in
  memory; everything above is untouched. At the tree root it simply continues the whole solve.
  Trees loaded from a file have no regrets to continue from, so the button is disabled there.
  API: `POST /api/solve_node` `{id, accuracy}` (accuracy in chips, % of the node pot × pot);
  progress comes back in `/api/status`'s `partial` (`{id, node, pot, point, progress, done,
  stopped}`) alongside the existing whole-solve convergence series; `loaded_file` is also in
  `/api/status`. Uses the UPI extension `go_partial <node> [N steps|seconds]` (see
  `docs/commands.md`).
- **Study side panel → Beta, runout lines (#467, #469, #471, #472, #477):** what one hand does on every runout,
  without clicking through the tree. Open the **Beta** tab of the side panel (next to Hand, Cats,
  Blockers, Graphs) at a flop or turn node and click a hand in the matrix; the panel follows as you
  browse. The combo chips under the hand's name narrow it to one combo. Two views, **Lines** and
  **Money in**, switched at the top of the panel.
  - **A line** is the hand's own actions, street by street: `bet · bet · check`, `check · xc · xf`.
    What the other player does only decides which of them it gets to take. The lines are those of
    the player whose hands the matrix shows.
  - **From a flop node** the picture is a turn × river map: a row per turn card, a column per river
    card, a suit at a time, always 52 by 52 (a card that cannot come, on the board or in the one
    combo asked for, keeps its row and column, of empty boxes like the diagonal's). Each of the 16
    blocks (the turns of one suit against the rivers of one suit) has an outline. A box is the
    hand's **most common line on that runout**, in that line's colour (how often is in the tooltip). The
    **Lines** buttons (1 to 5) put the N most common lines in each box instead, as bands of colour,
    taller the more often. A line keeps its colour everywhere, and the eight that weigh most have
    one; the rest are grey. The map is drawn for the room the panel has: drag the panel wider for
    bigger boxes. The **Lines** buttons sit above the picture and stay put whatever N is.
  - **Reading it:** point at a box for the lines and their exact frequencies, at a line in the list
    to see where on the map it is played (click to keep it). The panel scrolls as one piece, and
    not by itself when the picture is redrawn. Click a box to enlarge its turn card:
    a bar per river card, the three most common lines (or N, if more) at their true frequencies.
    Click a bar to go to that runout in the tree.
  - **Turn only** (beside Lines / Money in, at a flop node; **Runouts** is the map) leaves the
    river out: a bar per turn card, with the lines as far as the turn (`bet · check`, whatever the
    river brings) and their own list and colours. In Money in a turn's bar is the average over the
    rivers that can follow it, to the end of the pot: its row of the map in one bar. Click a bar
    for the rivers of that turn. The choice is kept.
  - **From a turn node** it is a bar per river card.
  - Frequencies are of the times the hand gets to that card (the pot has not ended before); lines
    under 0.5% of a runout are left to "other". **Split** bet sizes tells `bet 33%` from `bet 75%`.
  - **Money in** is the same map with a column in each box: the taller, the more chips the hand
    puts into the pot on that runout, on average, from the node to the end of the pot (calls, and
    bets and raises whether or not they are called). The column is in two colours, by the action
    that puts a chip in: the chips bet or raised at the bottom (a raise whole, the part that
    matches the bet it answers too), the chips called on top of them. A full box is the most the
    hand puts in over the runouts, so the two compare, with each other and from box to box.
    **Both / Bets & raises / Calls** over the picture shows one of them alone, from the bottom of
    the box and on its own scale (the figure beside the choice says what a full box is); the rows
    of the key do the same on a click. The bars of an enlarged turn, and of a turn node, are
    stacked the same way. Figures are in % of the pot at the node, with chips beside them. With a
    solver from before the split the map is one colour, fuller the more. A runout counts every
    pot, those that ended before the card came too (at what the hand had put in by then); the tooltip
    also gives the figure for the times the hand does see the card. The figures are exact: a pot
    that ended before a card is weighed as it would be with that card dealt (the hands it takes from
    the other player's range, and the combos it takes from a class, left out). A runout the hand
    never sees, its pot always ending first, has a box too: no lines, and what those pots cost. On
    a tree saved without its last streets the chips stop where the save does, and the panel says so.
  - `#study=<node>&side=beta&pin=JJ` links to it. API: `GET /api/hand_lines?id=&player=&hand=&sizes=`
    (each runout: `lines`, `reach`, `put`, `put_seen`, and `call`, `call_seen`, the part of them
    that was called; `pot` and `behind` at the node; `cut` for a
    save without the later streets), over the UPI
    extension `show_hand_lines` (see `docs/commands.md`).
- **Open / Save:** `dump_tree` (full / no rivers / flop only) and lazy `load_tree`. Browsing a
  truncated save re-solves missing streets on demand, as in Pio.
- **Library (API):** named ranges and full build-form configs saved in folders under
  `~/.wsolver/library` (override with `--library DIR`): ranges as range-syntax `.txt` files,
  configs as JSON with `"schema": 1`. Plain files you can also manage with Finder or git; every
  name is confined to the library root (`..`, absolute paths and symlink escapes are refused).

`python3 viewer/test_server.py ./solver` runs the viewer's integration tests (a small tree,
~1 s; also part of `make test`). `python3 viewer/test_library.py` tests the library storage and
its HTTP API without a solver.

### Viewer: node locking

**Lock…** (Study, at any decision of the main tree) sets the acting player's strategy by hand and locks it:
- **Editing:**
  - Click or drag hand classes to select them, and give them a mix in the *Selected* column.
  - Or scale one action to an exact share of the whole range with *Overall*. It changes the selected hands, or every hand if none are selected; scaling down multiplies, scaling up moves each hand the same fraction of the way to 100%.
  - Solved and locked range-weighted frequencies are shown side by side.
  - Edits are per hand class, because suit-isomorphic combos share one strategy in the solver.
- **Lock:** *Every hand* locks the whole node; *Only edited hands* locks just those (`combo_lock_node`) while the rest keeps solving. *Lock as solved* freezes the solved strategy. The tree is never changed until you lock.
- **Display:** locked nodes show a *Locked* tag, and the action line marks a locked step.
- **Re-solve:**
  - *Whole tree* keeps solving everything, so earlier decisions adapt to the lock. In the test spot, locking IP to over-fold to the flop bet takes OOP's betting from 52% to 98%.
  - *From this node…* solves only below the node (Solve further), with the ranges reaching it fixed.
- **Undo:** by default the first lock keeps a lossless copy of the unlocked solve. *Revert to unlocked solve* loads it back, and it can be solved further. *Unlock* / *Unlock all* remove locks but keep the current strategies as the starting point. *Unlock all* also clears the locks a loaded save came with. On a street the fast preview has refined, *Lock as solved* locks the strategy shown (the refined one).
- **Saving:** locks are saved with the tree. `dump_tree` / `load_tree` keep the lock flags and the locked strategies (exact with `lossless`, within 1/2047 by default).
- **Spots:** *Save spot…* on a tree built from the form stores its locks in the spot (`locks` in the config JSON, from `GET /api/lock/export`), and the save preview lists them. Building that spot again with the form unchanged sets them again before the solve (`POST /api/lock/apply`). A lock whose node is gone or has other actions is skipped and reported. An edited form builds without the spot's locks, since they belong to the saved tree.
- **Subgame re-solve:** *Re-solve…* at a node keeps the main tree's locks at or below it. Their ids move into the subgame (the node becomes `r:0`, or `r:0:c` behind the forced-check wrapper). A locked node that the edited sizes no longer make, or whose actions changed, is reported as not in the new tree. `"locks": false` in `/api/resolve/start` re-solves without them. The size search ignores locks.
- **Fast preview:** a lock, unlock or revert drops the refined streets, which were solved against the old strategies.

**Plain-English locks** (*Describe it*, at the top of the lock dialog). Type what the player does, for example "IP folds 60% to this bet, the weakest hands first" or "OOP bets about 25%, mostly sets, two pair and flush draws", and press *Propose*.
- **What the model sees:** the server (`viewer/nl_lock.py`) sends a compact summary of the node to Kimi K3 (`moonshotai/Kimi-K3` on Sail, `asap` window): street, board, actions, and the acting range by hand class and category with reach weights and solved mixes. It never sees the tree.
- **What it answers:** a JSON lock spec. The rules are either:
  - a *mix* for some hands;
  - an *overall target* for one action, with an order: `strongest` or `weakest` by equity against the opponent's range at the node, the solver's `current` preference, or `proportional` (no hand goes pure).
- **Never solver commands:** the model writes no UPI commands. The server checks the spec and does the threshold step itself, reach-weighted over groups of equal-equity combos within a class, so suit-isomorphic combos stay together and the target is hit exactly.
- **Errors:** unreachable targets and unknown actions, classes or categories are reported, as is the model's own explanation when it declines.
- **Nothing is locked yet:** the proposal lands in the editor as edits, next to the solved frequencies, with the model's explanation and the request's token cost (an estimate is shown while typing). Adjust it by hand if needed, then *Lock*.
- **The key:** it comes from `$SAIL_API_KEY` or `~/.config/sailresearch/api_key` (chmod 600) and is only ever sent, as a request header, to `api.sailresearch.com`. It is never returned to the browser, logged, or saved. Without a key the box is disabled with a message, and everything else works as before.
- **Tests:** `viewer/test_nl_lock.py` uses a fake model and needs no network.

### Viewer: subgame re-solve and size search

The server runs a **second solver process** (`viewer/resolve.py`) so a subtree can be
re-solved with different bet sizes without touching the tree being browsed. At any
decision node X it reads both players' ranges (`show_range`), the chips already in the
middle (`show_node`'s pot line, passed verbatim to `set_pot`), the board, and the original
stack (`show_effective_stack`), then builds a fresh tree rooted exactly at X from edited
sizes — the same sizing fields as the build form, expanded by `tools/tree_builder.py`
including any size syntax it learns in the future. Sizes for streets before X's street are
ignored; a root that starts facing a bet automatically offers raises instead of bets. The
previous street's aggressor is recovered from the node id's action/card token walk
(`resolve._prev_aggressor`), so an OOP lead on X's street follows the same donk rule as a
full build (#152); it degrades to bet sizes only when the id does not say (a root on the
tree's first street, or an unrecognized token). The solver's tree
roots are always OOP-to-act, so when X is IP's decision the subgame gets a one-child
"forced check" wrapper root and X itself sits at `r:0:c`; `subgame_root` in the API output
says which.

**This is unsafe re-solving** (the same caveat as Pio's on-the-fly recalculations): the
opponent's range at X is frozen at its solved reach, with no gadget game accounting for
the counterfactual value of having *avoided* X. Results are for exploring sizings, not for
exploitability claims.

Endpoints (all JSON; ids refer to the main tree, subgame browsing uses subgame ids):

| Endpoint | In | Out |
|---|---|---|
| `GET /api/resolve/context?id=<node>` | a decision node of the main tree | `{board, street ("flop"/"turn"/"river"), actor, pot: {oop, ip, dead}, stack}` — exactly what the subgame setup will use. 400 for chance/terminal/preflop nodes |
| `POST /api/resolve/start` | `{id, accuracy?, <sizing fields as in /api/generate>}` | starts a re-solve job; 400 on bad input or while another job runs |
| `POST /api/resolve/search` | `{id, street?, candidates: "25,50,100", n, allin?, accuracy?, <other sizing fields>}` | starts a size search: the `n`-size subset of `candidates` (used as the acting player's `BetSize` on `street`, or its `RaiseSize` when X faces a bet on its own street; the opponent keeps the sizes in the body. `street` defaults to X's street) with the highest EV for the acting player. With `allin`, every subset also gets the all-in (`a`), which then is not a candidate. All C(k, n) subsets are solved up to 12 combinations; above that, greedy forward selection |
| `GET /api/resolve/status` | — | `{busy, kind ("resolve"/"search"), node, error, done, total, method, rows (each `{sizes, ev, ev_oop, ev_ip}`, the acting player's EV first), best, context, result (root EVs, node count, line count, accuracy, calc_results text), tree, solve}` — poll this for progress; `solve` is the subgame solver's own status |
| `GET /api/resolve/node`, `/strategy`, `/range`, `/ev`, `/calc_results` | same as the main-tree endpoints | browse the (re-)solved subgame. After a search these show the table's **best** subset |
| `POST /api/resolve/stop` | — | cancels the running job (restarts the subgame solver); no-op when idle |
| `POST /api/resolve/restore` | — | stops showing the applied re-solve in place (`{restored: <its node id> or null}`) |

**Shown in place.** `/api/resolve/start` takes `sizes` (the acting player's sizes at X itself: its
bets, its raises when X faces a bet, or its donk leads) and `apply: true`; `/api/resolve/search`
takes `apply: true` too. Once such a job finishes, the main browse endpoints (`/api/path`, `/api/node`,
`/api/strategy`, `/api/range`, `/api/ev`, `/api/categories`, `/api/blockers`, `/api/graphs`) answer for
X and every id below it from the subgame, under main-tree ids (bet tokens are absolute chips, so
only the prefix moves), with `"variant": {root, actor, sizes, ...}` in the payload. `/api/path`
joins the main tree's line down to X with the subgame's below it. `applied` in `/api/resolve/status`
says what is shown. Starting another job, `/api/resolve/restore`, a lock change, Solve further or a
new main tree end it. `/api/resolve/context` also returns `built`, the sizes the main tree was built
with (null for a loaded file).

Each search candidate is solved to the requested (loose) accuracy; the comparison table
grows under `rows` as candidates finish.

`python3 viewer/test_resolve.py ./solver` runs the tests: same-sizes re-solves reproduce
the original node's strategy within solver tolerance, an edited config produces exactly
the tree a direct build of the generated lines produces, and the search agrees with an
independent brute force on a small river spot.

## Windows

`solver.exe` runs natively on Windows (verified on Windows 11 / Zen 5: all unit tests pass and every
UPI probe matches the macOS build up to floating-point rounding).

- **Native (MSYS2 UCRT64):** `pacman -S mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-zlib mingw-w64-ucrt-x86_64-python make`,
  then `make` (produces `solver.exe`) and `make test`.
- **Cross-compile from macOS/Linux:** install mingw-w64 (`brew install mingw-w64`), build zlib for it
  (`make -f win32/Makefile.gcc PREFIX=x86_64-w64-mingw32- libz.a` in zlib's source), then
  `make windows ZLIB_DIR=/path/to/zlib` -> `build/win/solver.exe` (static, no DLLs; AVX2 by default,
  `WIN_ARCH=-march=x86-64` for older CPUs).

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
| EVs | Chips relative to the tree start: payoff = share of the final pot - own chips invested in the tree, so `EV OOP + EV IP` = the dead money (`set_pot`'s third number: the players' own chips there are investments, so `set_pot 10 5 0` sums to 0). It held only approximately on boards with a suit symmetry and a flop all-in until #300. On a flop subset of a preflop tree it holds only nearly (within 0.07 bb/100 at the solutions of the shipped subsets) unless the tree was built with `set_zero_sum_subset on` (#513). `calc_ev` matchups are weighted by the hand's own reach (Pio semantics); out-of-range hands print `nan`/`inf`, and an in-range hand that never reaches the node prints its counterfactual EV with matchups 0, as Pio does (#247). |
| Hands of either range | By default each player's hand list is their own range. `set_union_hands on` (before `build_tree`) makes it the union of both, like Pio: a hand only in the opponent's range gets a slot with reach 0, so `calc_ev` reports the **sign** of its counterfactual value (`inf`/`-inf` rather than a bare `inf`) and `calc_ev_pp`/`show_strategy_pp` list it. The equilibrium is unchanged; vectors get wider (about +23% solve time on `bench_flop`), which is why it is off by default. |
| Check-downs | A line with no bets left (only checks down to the showdown) keeps its nodes, as in Pio: every turn and river card is there to browse, each with a one-action node that stores a row per hand. `set_collapse_checkdowns on` (before `build_tree` / `build_preflop_tree`) builds each such subtree as one `END_NODE` valued over the runouts, exactly as an all-in is: same values and strategies, a fraction of the memory and time when the late streets have no bets (see *Preflop trees*). The nodes inside it then don't exist (a node id inside it answers `incorrect path`); `calc_ev` at the `END_NODE` works. "No choice left" is meant literally: one-action bets and calls (what `remove_line` can leave) collapse too, and the `END_NODE` shows the pot of the showdown it stands for. A save of such a tree is version 6 and carries the setting: `load_tree` turns it on for a version 6 file and off for an older one, as it replaces the rest of the configuration. |
| SOLVER output | `SOLVER: started`, periodic `SOLVER:` blocks (running time, EVs, MES, `Exploitable for`) ending in END, and `SOLVER: stopped (<reason>)`. Command responses never interleave with these. |
| Isomorphism | Querying a non-canonical card (e.g. `7s` when it mirrors `7d`) returns the suit-swapped strategy; node IDs keep the card you asked for. |

## Command coverage

See the [command compatibility and test reference](docs/commands.md) for all commands,
their implementation status, known differences, and regression/probe coverage.

`set_range` additionally accepts range text (`AA,KK:0.5,...`); Pio itself only accepts 1326 numbers.

**Implemented:** is_ready, exit, set_end_string, reset_end_string, show_version, show_build_version,
show_settings, bench, load_script, load_script_silent, skip_if_done, LABEL:, echo, repeat,
show_hand_order, show_preflop_order, go, stop, wait_for_solver, take_a_break, solver_time,
set_threads, set_info_freq, set_accuracy, set_recalc_accuracy, set_always_recalc (stored),
set_isomorphism, set_union_hands, set_collapse_checkdowns, set_zero_sum_subset, change_step / show_step (stored), show_iters, set_first_iteration_player,
set_range, set_eff_stack, set_board, set_pot, add_line, remove_line, force_line, clear_lines,
build_tree, list_algorithms, set_algorithm, ignore_mem_check, small_strats, set_outcome_tables,
estimate_schematic_tree, show_memory, is_tree_present, show_node, show_children,
show_effective_stack, show_range, show_strategy, show_strategy_pp, calc_ev, calc_ev_pp, calc_eq,
calc_eq_pp, calc_eq_node, calc_eq_preflop (exact), calc_results, calc_matchups_line,
calc_line_freq, calc_global_freq, show_all_lines, show_all_freqs, show_win_evs,
add_info_line, reset_tree_info, show_tree_info, show_tree_params, set_bet_sizes / set_donk_bet (stored),
rebuild_tree, estimate_rebuild_tree,
solve_partial, solve_all_splits, explo_partial, stdoutredi, stdoutredi_append, stdoutback,
print_all_strats, node_count, clear_cache, show_cpu_info, show_nuts, show_category_names,
show_categories, show_cats, show_cats_pp, set_display_iso, reset_display_iso, show_board_no_iso,
set_rake, set_icm, set_icm_point, reset_icm_tables, eliminate_path, lock_node, unlock_node, unlock_all,
combo_lock_node, show_locked_combos, set_strategy, set_equal_strats, set_mes, round_up_to,
lock_line, unlock_line, set_incentives_node, set_incentives_line, show_incentives,
clear_incentives, load_tree, dump_tree (full / no_turns / no_rivers / no_compress / lossless), free_tree,
show_metadata, show_save_version, load_all_nodes, set_compression_precision,
set_compression_level, check_tree_health, set_dcfr, set_predictive, set_avg_delay, set_pruning,
deactivate.

**Saving and loading.** `dump_tree <file> [no_rivers|no_turns] [lossless]` writes a save for viewing.
By default it keeps each node's displayed strategy, quantized to `set_compression_precision` bits (11, as
in Pio: frequencies within 1/2047), and no regrets: saves are for browsing, not for resuming a solve.
This quantization happens at the file layer regardless of `small_strats` — that knob selects the tree's
in-RAM storage, not the save's, so "uncompressed" (small_strats off) is not the same as `lossless` (#100).
`no_rivers` / `no_turns` drop the later streets, which are re-solved on demand as you browse them, as in
Pio. `lossless` keeps everything exactly (a reload then displays strategies bit-identically). Converged
flop tree, 155 MB in RAM:

| save | size | PioSOLVER 3.10, same tree |
|---|---|---|
| `lossless` | 76 MB | - |
| default | 4.1 MB | 7.8 MB |
| `no_rivers` | 120 KB | 315 KB |
| `no_turns` (flop only) | 3 KB | 21 KB |

Opening any of them takes about 0.02 s. Each turn of a `no_turns` save is re-solved in about 0.01 s
when you first browse it (to `set_recalc_accuracy`, by default 0.1% of the pot on the turn).
`load_tree <file>` is **lazy** (Pio's "fast" load): it reads only the
tree's settings, and each street segment (the decisions between two card deals) is decompressed the
first time it is accessed. Big saves open instantly and use memory only for what you look at (e.g. a
155 MB tree: 0.02 s and 13 MB to open and browse vs 0.22 s and 155 MB for `load_tree <file> full`).
`load_all_nodes` reads the rest in parallel. Solving and saving do that automatically, and
`rebuild_tree` / `solve_all_splits` require it, as in Pio. Saves from older versions (v2, v3) still
load, eagerly.

**Preflop trees:** add_preflop_line, remove_preflop_line, clear_preflop_lines, build_preflop_tree,
add_to_subset, reset_subset, show_subset, recover_subset, add_schematic_tree, add_all_flops,
estimate_tree. Workflow (as in Pio): set ranges, `set_pot <bb> <sb> 0`, `set_eff_stack <total stack>`,
add preflop lines (absolute amounts incl. blinds; the SB = IP acts first), `build_preflop_tree`,
`add_to_subset <weight> <c> <c> <c>` per flop, then for every preflop exit: `set_board <any flop>`,
`set_pot`/`set_eff_stack`/`add_line` for its postflop tree and `add_schematic_tree <exit>`; finally
`add_all_flops` and `go`. Each subset flop stands for its whole suit-isomorphism class (its values are
averaged over suit permutations; deal probabilities are normalized per hand pair by
inclusion-exclusion, for each player's hand over the opponent's range, or with `set_zero_sum_subset on`
at one weight a pair for both players, which makes the subset's game constant-sum: `docs/commands.md`),
and preflop all-ins / pure-preflop exits use exact equity from the
hand-vs-hand table (see below). As in Pio, each schematic's
`set_pot` is taken literally: its per-player chips are investments and its dead money is free money.
For whole-hand accounting give the preflop investments per player (e.g. `set_pot 100 100 0` after a
3-bet to 100); `set_pot 0 0 200` makes the 200 dead money.

**Memory of preflop trees.** Every subset flop gets a copy of its exit's postflop tree, so what the
schematics allow decides the size. A schematic that stops betting early is checked down from there, and
by default those checks are real nodes on every turn and river (as in Pio), which is most of the tree:
`set_collapse_checkdowns on` before `build_preflop_tree` makes each check-down one showdown node instead.
With full ranges, six exits and five flops (16-bit rows): bets on the flop only 2,735 MB -> 78 MB, flop
and turn 8,976 -> 443 MB; three streets of betting have no check-downs and stay as they are. The same
equilibrium either way (#484). A collapsed tree whose last bets are on the flop has since become about
four times smaller again: its flops are valued on one outcome table each instead of their showdown
boards (#494), and half of that with the same hands on both sides (#496; the table of sizes under
*Preflop solves*). `set_outcome_tables auto|small|fast` says whether a tree with turn bets gets tables
for its turn all-ins too: faster, and about a ninth more memory (`auto`: when it fits).

**Flop subsets.** `flops/wsolver_10.txt`, `_25`, `_50`, `_100` and `_200` are flops with weights to put
in a preflop tree (`add_to_subset <weight> <c> <c> <c>` per line of the file). They are our own, made by
`tools/flop_subsets.cpp` (`make flop-subsets`) from the game alone: for every flop class it counts, per
pair of preflop hand classes, who is ahead on the flop, on the turn and at the river, and it chooses the
flops and weights whose tables are closest to those of all 1,755 classes together, measured as the error
of a hand's value against a range (a run of hands in order of strength). Random flops of the same
number are 2 to 4 times further off on that error even with their weights solved the same way. In the
one tree solved with each file, 50 flops or more gave the 200-flop solve's overall action frequencies
to within a point and 25 or fewer did not; played on all flops, a strategy solved on 10 gives up 1.0 to
1.4 bb/100 and one solved on 200 nothing that was measurable (*Preflop solves* above has the table).
`flops/README.md` has the numbers per file;
`build/flop_subsets eval FILE` measures any flop file, and `build/flop_subsets equity FILE AKs QQ,JTs`
prints what an all-in equity becomes through a file's flops next to the exact one (#486).

## Known differences from PioSOLVER

Policy (owner decision, #10): match Pio wherever tools may depend on it (formats, codes, node ids,
error messages), but **don't copy Pio bugs** and don't impersonate it. Deliberate differences:

- **`list_algorithms` reports `alpha 1.3, beta 0 then 0.5 after iteration 50, gamma 4`, not Pio's `alpha 1.5, beta 0, gamma 2`.**
  The strings are otherwise identical. Our DCFR defaults converge faster on every benchmark tree (see
  `bench/dcfr_sweep.md`), and the command reports the exponents actually in use rather than Pio's.
  `set_dcfr 1.5 0 2` restores the published values if exact parity matters.
- **Save files are not Pio `.cfr` files** (Pio's format is undocumented); ours are gzip files with one
  member per street segment plus an index (`src/upi_save.cpp`), and `show_save_version` reports our own
  format (5; 6 for a tree in which `set_collapse_checkdowns on` collapsed a check-down).
- **One algorithm.** `set_algorithm` accepts every Pio code but all map to DCFR; `list_algorithms`
  keeps Pio's codes with honest descriptions.
- **Fresh results.** Pio caches `calc_results` until `clear_cache`; we always report current values.
- `show_all_freqs` prints real frequencies (Pio 3.10 prints `nan`); `estimate_schematic_tree` reports
  real sizes (no ~150 MB floor); `node_count` counts every node in preflop trees.
- Equal values are listed in 1326 order in `show_nuts` and the sorted `_pp` listings (`show_strategy_pp`,
  `calc_ev_pp`, `calc_eq_pp`); Pio lists ties in an order that changes with the rest of the list. Black-box
  probes ruled out a stable sort and MSVC `std::sort` / heap orders over the 1326 order, so we keep ours.
- `_pp` listings omit hands that are only in the opponent's range by default; Pio always lists the union
  of both ranges. `set_union_hands on` (before `build_tree`) gives Pio's exact hand list (#9, #61).
- `remove_line` / `force_line` act on the tree as the lines stand, like Pio's live line tree: a line that
  isn't there is an error, removing deletes the subtree, and a line re-added afterwards comes back (#251,
  see AGENTS.md). Without a board, pot and stack set yet, the edits are kept and applied at `build_tree`.
  Bets above the effective stack are rejected at `build_tree`; Pio builds them.
- Inside preflop trees we use a few more (valid) river symmetries than Pio, so `node_count` is lower
  there; results are identical with isomorphism on or off (#12).
- **Preflop equity** (`calc_eq_preflop`, preflop all-ins) is exact: a 1326x1326 hand-vs-hand
  win/tie table is generated once per machine with our own evaluator (suit-isomorphic reduction,
  multithreaded; ~13 s on an Apple M4, 13.4 MB) and cached under `~/.cache/wsolver/pf_h2h_v1.bin`
  (override with `WSOLVER_CACHE_DIR`; `XDG_CACHE_HOME` is honored). Values match Pio's exact
  tables to its output precision.

Verified against PioSOLVER-edge 3.10.1 with `tests/pio_compare/` (black-box: same UPI scripts on both,
response-by-response diff): formats, tree shapes (`node_count` matches on postflop trees), hand order,
ranges, equities, rake and ICM payoffs, node locking, truncated saves, and equilibrium EVs. The range
explorer (`show_categories`, `show_cats`, ...) is byte-identical on 88 boards (`tests/data/pio_categories/`).

## Performance

Against PioSOLVER 3.10 on the same PC (AMD Zen 5, 16 cores / 32 threads), both driven over UPI and run
back to back, to the same target (0.6 chips, `set_info_freq auto`), 32 threads:

| Tree | Memory | WSolver | PioSOLVER 3.10 |
|---|---|---|---|
| `srp_big`: SRP flop, 710 lines | 13 GB (16-bit) | **41.9 s** (123 iterations) | 71.5 s |
| `threebet_mono`: 3-bet pot, monotone flop | 542 MB | **2.93 s** (168 iterations) | 6.70 s |

On these two trees Pio's convergence per iteration is close to ours, so the gap comes from how fast
each iteration runs. On the smaller bench trees (40-307 MB, measured at #147) WSolver was 1.1-2.3x faster
on one thread; at 32 threads it was about even on `deep` and 1.8x faster on `flop_srp_4` (Pio's sub-second times
there are only upper bounds). Method, history and per-tree numbers: [bench/vs_pio.md](bench/vs_pio.md);
where the time goes: [bench/where-the-time-goes.md](bench/where-the-time-goes.md).

On an Apple M4 (10 threads, re-measured 2026-11-06 on main `6227df9`):

| Tree | Memory (peak RSS) | Time |
|---|---|---|
| Wide SRP flop (`bench/trees/flop_srp_2`), 498 vs 322 combos, 1-2 sizes | 175 MB | 100 iterations in 0.97 s including the build -> 0.015 chips (0.025% of pot) |
| Real Pio 25bb HU script (`tests/data/pio_25bbHU_2sizes.txt`), 2 sizes everywhere | 6.5 GB float32 | 1% of pot in 51 s, 0.1% in 144 s (320 iterations) |
| the same, `small_strats on` | 3.3 GB | 1% of pot in 51 s, 0.1% in 149 s (325 iterations) |

The 25bb times are `bench/run.py --include-25bb --threads 10`, reporting every 5 iterations, two
runs each within 1%. 16-bit storage needs about as many iterations as float32 here because its
strategy sums are rounded stochastically (#293): rounded to nearest it took 440 iterations to 0.1%.
Full per-tree numbers, including the larger `srp_big` and `threebet_mono` trees, are in
`bench/results.md`; regenerate with `make bench`.

Single-threaded on the same tree (`tests/pio_compare/bench_flop.txt`), PioSOLVER-edge 3.10.1 on a Zen 5
runs 60 iterations in 4.63 s vs 3.37 s here (Apple M4, 2026-11-06; 4.73 s when first measured), and this solver's DCFR reaches **0.042 chips of
exploitability after 60 iterations against Pio's 0.182** — about 4x closer to equilibrium for the same
iteration count, so it reaches a given accuracy considerably sooner. (That figure is a convergence
property, so it does not depend on the machine; it has improved from 0.119 as the DCFR schedule was
retuned — see `bench/dcfr_sweep.md` and `src/solver.hpp`.)

`bench/results.md` carries the current per-tree timings and is regenerated by `make bench`; prefer it
over the M4 table, which is a snapshot.

`build_tree` checks free memory and switches to compressed storage automatically when float32
storage would not fit (`small_strats auto`; force with `on` / `off`). If 16-bit would not fit either,
`auto` also leaves out the river rows' board-blocked hands, about 7% less memory for about 14% more
time per iteration with identical results (force with `small_strats compact`, #239).

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
tests/             unit tests (eval, range, categories, solver, flop subsets, tree builder) + end-to-end UPI test
flops/             flop subsets for preflop trees and the job queue (wsolver_<N>.txt; README.md)
packaging/macos/   what `make macos-pkg` builds WSolver.app and its installer from
tools/             helper scripts (tree_builder.py: bet-size config -> add_line commands; jobs.py: solve queue;
                   fuzz.py: randomised invariant / concurrency / save / preflop / lock / truncated-save checks over UPI;
                   flop_subsets.cpp/.hpp: chooses the flops and weights in flops/, `make flop-subsets`;
                   preflop.py: heads-up preflop solves: script, memory estimate, 13x13 charts)
```

## Contributing

Work is tracked in GitHub issues and lands through pull requests reviewed by the maintainer agent.
New agents: read [INSTRUCTIONS_FOR_SUBAGENTS.md](INSTRUCTIONS_FOR_SUBAGENTS.md). See [AGENTS.md](AGENTS.md) for the workflow (claiming issues, branches, PRs, review), the ground rules
(clean-room, tests green), the code map and the list of verified PioSOLVER behaviors.
