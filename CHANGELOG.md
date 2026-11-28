# Changelog

All notable changes to OpenSolver. Versions follow [semantic versioning](https://semver.org/).

## 0.1.0 — unreleased

First release. A heads-up no-limit hold'em solver for postflop and full preflop trees, speaking the
PioSOLVER-compatible UPI text protocol. Independent implementation from the public UPI
documentation; contains no PioSOLVER code.

> **Not yet distributable.** The project has no licence file, so there are currently no terms under
> which this can be used or sold. See the release-readiness issue before cutting a release.

### Solver

- **GPU solving** (`set_algorithm v4_gpu`, the viewer's Compute device): whole-tree postflop solves on
  Metal (Apple Silicon), CUDA (NVIDIA) or OpenCL (AMD Radeon and other GPUs), one kernel source for
  CUDA and OpenCL, drivers loaded at run time. Matches the CPU to rounding; README "GPU solving".
  In the viewer, the Auto device picks the faster device for each tree by its size, from a benchmark
  of both devices on Settings.
  A fresh GPU solve of a large Medium tree to 0.3% of pot stores regrets in bfloat16 and strategy sums
  in fp16 between updates (#559, #561; every Medium tree since #586, also IP-first ones since #596): HUNL 3BP on an M4's GPU 12.5 -> 9.8 s.
- **CPU and GPU together** (`set_algorithm v4_hybrid`, #582): with a discrete GPU, a flop tree's turn
  subtrees are shared between the GPU and the CPU in every pass, the share set by their speeds and the
  GPU's memory. On an RX 9060 XT with 32 Zen 5 threads, the viewer's Smart trees to 0.3% of pot: HUNL 3BP
  7.1 -> 4.4 s, HUNL SRP 95 -> 62 s (it does not fit the GPU alone). The viewer's Auto device uses it
  for flop trees. On Apple Silicon too (#589), where the GPU updates the tree's own rows in place: on an
  M1 Ultra, against the GPU alone, HUNL 3BP 4.4 -> 3.4 s and 5.8 -> 2.2 GB, HUNL SRP 51 -> 37 s and 42 -> 15 GB.
- Vector-form **Discounted CFR** with exact card removal and no abstraction. Defaults α=1.3, γ=4,
  β=0 then 0.5 after iteration 50; `set_dcfr` overrides the exponents.
- **Regret-based pruning** on by default (`set_pruning`). Skips traverser actions whose current
  strategy is exactly zero for every hand the node's board allows and revisits them every `period`-th iteration (default 8,
  at least 2). Results are close to, not bit-identical with, pruning off (#380). Worth ~13% on a deep tree.
- **Suit isomorphism**, flop trees on by default to match Pio's `set_isomorphism 1 0`.
- Averaging controls `avg_delay` and `avg_reset`, which reach a given exploitability in fewer
  iterations than accumulating from the first.
- **Faster large Smart-tree solves on the CPU** (#578): the app's IP-first order and square-root
  opening now also run on 16-bit storage, and the sampled strategy average runs on every large Smart
  tree solved to 0.3% of pot, not only IP-first rainbow flops. On a Ryzen 9 9950X (32 threads): HUNL SRP
  110.7 -> 96.7 s, HUNL 3BP 8.0 -> 7.1 s. Since #591 the IP-first order and square-root opening take
  every Smart flop where OOP may check, of any suits, paired or not, also where OOP may bet: HUNL 3BP on
  AhKd3c 7.0 -> 6.7 s, Td7c5h 8.0 -> 6.7 s, HUNL SRP on AsQs5s 29.0 -> 24.6 s, KdKh5c 81.0 -> 69.2 s.
- Optional **16-bit compressed storage** (`small_strats`), roughly halving memory, with a `lossless`
  dump flag when exact records are wanted. `small_strats compact` (and `auto`, when full 16-bit rows don't
  fit) also leaves out the river rows' board-blocked hands: about 8% less memory, about 14% more time per
  iteration (M4: 0 to 15%), identical results (#418; every CPU build since #568, NEON expand/compress).
- Decisions with one action (a forced check: no donk size, or a street the lines check down) store no
  regret or strategy rows: each player's share one row that stays zero, since their strategy is 1 (#567).
  Results and saves are unchanged. Memory of flop_srp_4 298 -> 138 MB (peak RSS on Metal 665 -> 344 MB),
  deep 222 -> 70 MB, flop_3bet 35 -> 5 MB; nothing changes for trees where every decision has a choice.
- `set_collapse_checkdowns on` (off by default): a subtree in which nobody has a choice left, the checks
  down to a showdown, is built as one showdown node on the runouts, like an all-in. Same values, none of
  the nodes and rows: a preflop tree whose schematics bet on the flop only goes from 2.7 GB to 78 MB and
  solves 2.7x faster. Such a tree saves as version 6 (#484).
- Multi-threaded over turn and river cards with a nested work-stealing pool and per-thread arena
  allocators.
- Node **locking**: whole-node and per-combo (`lock_node`, `combo_lock_node`, `set_strategy`,
  `show_locked_combos`), skipped by the regret update and preserved across save/load.

### Interface and tooling

- UPI command surface documented in `docs/commands.md`.
- `dump_tree` / `load_tree` with lazy per-street decompression, truncated saves (`no_turns`,
  `no_rivers`) that re-solve later streets on demand, and `check_tree_health`.
- A browser **viewer** (`viewer/`): tree navigation, 13x13 strategy matrices, range and EV display,
  subtree re-solves, a saved-spot library, and a solve-queue job system. A Tree tab with no form of
  its own (a first launch) opens on the library's HUNL 3BP spot (#580).
- `tools/tree_builder.py` reproduces PioViewer's `add_line` sequence for a tree configuration.
- PioViewer tree files: `tools/tree_builder.py` reproduces all 145 of the owner's PioViewer configs
  line for line (merging, caps, forced bets, hand-edited lines; #314), and the viewer's Build tab imports
  them (#318).
- Aggregate **job reports** like PioViewer's aggregation report: matchup weighting (#324), any line with
  per-turn rows (#328, #335), per hand class (handsEV, #337) with a table and 13×13 grid (#340, #344), and
  per-combo Full CSVs (#347); Study's **Report…** runs one at the current node (#332).
- `bench/run.py` time-to-exploitability harness over a suite of clean-room trees, plus
  `bench/where-the-time-goes.md`, the measured cost map to read before doing performance work.
- **Flop subsets of our own** (`flops/wsolver_10.txt` to `_200.txt`) for preflop trees and the job
  queue, and the tool that makes them (`tools/flop_subsets.cpp`, `make flop-subsets`). Flops and weights
  are chosen so that who is ahead on the flop, the turn and the river, hand class against hand class,
  matches all flops together; random flops of the same number are 2 to 4 times further off on that
  error. In a solved tree 50 and 100 flops gave the 200-flop solve's overall action frequencies to
  within 1 point; 25 and 10 had the big blind fold 4 points too often. The viewer's
  Jobs tab offers them when no other flop directory is set, and the macOS app carries them (#486).
- `tools/jobs.py`: a `#` comment in a flop file may contain commas and semicolons.
- `tools/preflop.py`: **heads-up preflop solves** from one command. The stack and the sizes in big blinds
  (open, raise over a limp, 3-bet, 4-bet, 5-bet, limp or not; postflop bets and raises down to a chosen
  street) become the preflop tree and a postflop tree per flop exit in whole-hand amounts; `estimate`
  prints the memory per number of flops and postflop depth before anything is built, and the flops default
  to the largest subset of ours that fits; `solve` runs to an accuracy in bb/100 and prints 13x13 charts
  of every preflop decision (frequencies, optionally EVs in big blinds), also saved as JSON; `chart`
  prints them again. `--flops all` deals all 1,755 flop classes (#488).
- **Viewer: preflop solves from the Build tab.** A Postflop / Preflop switch at the top of Build; Preflop
  is a form over `tools/preflop.py` (same defaults, same checks, its messages in the form's words): the
  blinds' ranges, stack, ante, the preflop sizes, the limp, and the flops, last betting street and sizes
  after the flop. The tree is listed in words while you type, **Estimate memory** asks the solver for the
  chosen number of flops and then for the others, and Build refuses a tree larger than the machine's memory before touching the tree that is there. Solve to
  a target in bb/100, shown as bb/100 in the chart, the top bar and the Exploitability tile; no street
  of a preflop tree is re-solved by the fast preview. A postflop
  tree built or opened afterwards gets the solver's own settings back (the preflop script turns on 16-bit
  storage and collapsed check-downs). The macOS package ships `tools/preflop.py` (#492).
- **Viewer: Study on preflop trees** (a tree saved by `tools/preflop.py solve --save-tree`). Raises
  before the flop get their real amounts (`Raise 75% to 250`, with `2.5x` in the tooltip: the open to
  2.5 big blinds read `to 200 · 4.0x`, the blind taken off it), the text beside "to act" says small
  blind or big blind and what the call costs, the deal after a preflop call lists the tree's flops with
  a filter by rank or card (it was a dead end, "Choose the river" over a card picker with nothing to
  click), and a board card of a flop opens the same list to change to another flop on the same line.
  Sizes is off before the flop, where a re-solve is refused. EVs stay a percentage of the node's pot,
  counted from the start of the hand, and the tooltip says so. In every tree, a raise's card says
  `Raise 40% to 60` with the multiple (`3.0x`) in its tooltip, and a label that still does not fit is
  cut short with the whole of it there: `to 60 · 3.0x` ran under the combo count (#490).

### Build

- `make` builds `./solver`; `make test` runs the C++ unit tests and the Python end-to-end suites (about 810 checks on 2026-11-06); `make test-big` adds the
  25bb tree.
- `make release` builds `./solver-release` with LTO. PGO is available behind `PGO=1` but off by
  default: it measures at parity and roughly doubles build time.
- `make install` / `make uninstall` with `PREFIX`, `DESTDIR`, `BINNAME` and `INSTALL_BIN`.
- `make dist` produces a checksummed source tarball.
- `make macos-pkg` produces an installer for macOS (Apple Silicon, 13.3 or later): `OpenSolver.app` in
  `/Applications`, holding the solver, the viewer and a Python to run it, so that it needs nothing
  else installed. Opening the app starts the viewer and opens it in the browser; quitting stops it.
  Not signed with a Developer ID (#479).
- `make windows-installer` (MSYS2 UCRT64, NSIS) produces a per-user setup program for 64-bit Windows
  10 and 11: `OpenSolver.exe` in `%LOCALAPPDATA%\Programs\OpenSolver` with the static solver, the viewer and
  a Python to run it, a Start menu entry and an uninstaller. OpenSolver.exe starts the viewer and opens it
  in the browser; quitting, or the launcher ending in any way, stops the viewer and its solvers. Not
  code-signed (#565).
- **Update prompt** in the macOS app and on Windows: at startup each reads `installers/<os>/latest.txt`
  from `main` on GitHub and, when a later build is published, offers to install it (downloaded,
  checked against its SHA-256, started, and the app quits). `make publish-macos-pkg` /
  `make publish-windows-installer` put a fresh build and its manifest in `installers/` (#581).
- Cross-compiles to Windows via MinGW (`make windows`).

### Fixed late in development

- 2026-11-06, from independent reviews of every component (each with regression tests): save/load
  hardening against corrupt and crafted files, and no unsolved streets in re-dumped truncated loads (#361);
  solve-control concurrency, an out-of-memory solve no longer aborting the process (#366, #365, #368, #377);
  tree construction, including wrong preflop `calc_eq` (#372); 16-bit combo locks no longer drifting,
  `set_mes` at combo-locked nodes (#381); `set_info_freq 0` reporting by time like Pio (#379);
  `show_strategy` printing 0 for hands that contain the turn or river card, like Pio (#414); the job
  queue's runner lock and Windows file sharing (#363); and viewer fixes, including an XSS through an
  echoed board (#385), the Sail key never reaching the browser (#374), the fast preview, the re-solve
  and the locks (#358, #370, #383, #387).
- 2026-11-07, from paired runs against Pio 3.10.1, strategy edits and what follows them:
  - `eliminate_path`: one-time edit (#422). Each hand's frequency goes to its best remaining action by EV; a call
    or fold is refused.
  - `set_mes`: the best response persists under the v4 algorithms, as `set_strategy` does (#424).
  - `set_equal_strats` and `rebuild_tree`: restart the solve (#426, #428). `rebuild_tree` keeps locks and,
    until the next solve, the solved strategies.
  - `unlock_node`: the node solves on (#430). It used to stay at its locked strategy for good.
  - Incentives: counted as payoff in `calc_results`, `calc_ev` and the best responses (#440).
  - Pruning: ignores hands blocked by the node's board, so it fires where it should (#435).

- `make release` shipped a binary **12% slower** than the default build. Clang's front-end PGO
  instrumentation counts function entries before inlining, so functions the instrumented build
  inlined recorded entry count 0; clang then treated them as cold, dropped their inline threshold
  from 250 to 45, and stopped inlining the showdown kernel that is 49-65% of runtime. Switched to
  IR-level instrumentation.
- `Arena`'s 64-byte alignment is now established by construction rather than assumed.
- Preflop solving no longer dispatches the inner loop per-node, which was costing 42% at 20 threads.
- Pruning's default period dropped from 32 to 8, because at the exploitability targets the suite
  reports, period 32 never activated before the solve finished.
