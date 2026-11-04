# Changelog

All notable changes to OpenUPI-Solver. Versions follow [semantic versioning](https://semver.org/).

## 0.1.0 — unreleased

First release. A heads-up no-limit hold'em solver for postflop and full preflop trees, speaking the
PioSOLVER-compatible UPI text protocol. Independent implementation from the public UPI
documentation; contains no PioSOLVER code.

> **Not yet distributable.** The project has no licence file, so there are currently no terms under
> which this can be used or sold. See the release-readiness issue before cutting a release.

### Solver

- Vector-form **Discounted CFR** with exact card removal and no abstraction. Defaults α=1.3, γ=4,
  β=0 then 0.5 after iteration 50; `set_dcfr` overrides the exponents.
- **Regret-based pruning** on by default (`set_pruning`). Skips traverser actions whose current
  strategy is exactly zero for every hand, so results stay bit-identical, and revisits them every
  `period`-th iteration (default 8). Worth ~13% on a deep tree.
- **Suit isomorphism**, flop trees on by default to match Pio's `set_isomorphism 1 0`.
- Averaging controls `avg_delay` and `avg_reset`, which reach a given exploitability in fewer
  iterations than accumulating from the first.
- Optional **16-bit compressed storage** (`small_strats`), roughly halving memory, with a `lossless`
  dump flag when exact records are wanted.
- Multi-threaded over turn and river cards with a nested work-stealing pool and per-thread arena
  allocators.
- Node **locking**: whole-node and per-combo (`lock_node`, `combo_lock_node`, `set_strategy`,
  `show_locked_combos`), skipped by the regret update and preserved across save/load.

### Interface and tooling

- UPI command surface documented in `docs/commands.md`.
- `dump_tree` / `load_tree` with lazy per-street decompression, truncated saves (`no_turns`,
  `no_rivers`) that re-solve later streets on demand, and `check_tree_health`.
- A browser **viewer** (`viewer/`): tree navigation, 13x13 strategy matrices, range and EV display,
  subtree re-solves, a saved-spot library, and a solve-queue job system.
- `tools/tree_builder.py` reproduces PioViewer's `add_line` sequence for a tree configuration.
- `bench/run.py` time-to-exploitability harness over a suite of clean-room trees, plus
  `bench/where-the-time-goes.md`, the measured cost map to read before doing performance work.

### Build

- `make` builds `./solver`; `make test` runs 271 unit and end-to-end checks; `make test-big` adds the
  25bb tree.
- `make release` builds `./solver-release` with LTO. PGO is available behind `PGO=1` but off by
  default: it measures at parity and roughly doubles build time.
- `make install` / `make uninstall` with `PREFIX`, `DESTDIR`, `BINNAME` and `INSTALL_BIN`.
- `make dist` produces a checksummed source tarball.
- Cross-compiles to Windows via MinGW (`make windows`).

### Fixed late in development

- `make release` shipped a binary **12% slower** than the default build. Clang's front-end PGO
  instrumentation counts function entries before inlining, so functions the instrumented build
  inlined recorded entry count 0; clang then treated them as cold, dropped their inline threshold
  from 250 to 45, and stopped inlining the showdown kernel that is 49-65% of runtime. Switched to
  IR-level instrumentation.
- `Arena`'s 64-byte alignment is now established by construction rather than assumed.
- Preflop solving no longer dispatches the inner loop per-node, which was costing 42% at 20 threads.
- Pruning's default period dropped from 32 to 8, because at the exploitability targets the suite
  reports, period 32 never activated before the solve finished.
