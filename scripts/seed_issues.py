#!/usr/bin/env python3
"""Create the initial labels and issues for weston/OpenSolver (run once, needs `gh auth login`)."""
import json
import subprocess
import sys

REPO = "weston/OpenSolver"
GH = "/opt/homebrew/bin/gh"

LABELS = {
    "ready": ("0e8a16", "Can be picked up"),
    "in progress": ("fbca04", "Claimed by an agent (see issue comments)"),
    "needs decision": ("d93f0b", "Needs a decision from the human owner"),
    "needs pio": ("5319e7", "Requires running a real PioSOLVER (maintainer can run probes)"),
    "good first issue": ("7057ff", "Small and well scoped"),
    "area:engine": ("1d76db", "CFR, tree, evaluation"),
    "area:upi": ("1d76db", "Command interface / output formats"),
    "area:preflop": ("1d76db", "Preflop trees"),
    "area:perf": ("1d76db", "Speed / memory"),
    "area:infra": ("1d76db", "Build, CI, portability"),
    "area:tools": ("1d76db", "Scripts and helpers"),
    "area:docs": ("1d76db", "Documentation"),
    "priority:high": ("b60205", ""),
    "priority:medium": ("e99695", ""),
    "priority:low": ("f9d0c4", ""),
}

def body(goal, context, criteria, verify):
    crit = "\n".join(f"- [ ] {c}" for c in criteria)
    return f"## Goal\n{goal}\n\n## Context\n{context}\n\n## Acceptance criteria\n{crit}\n\n## How to verify\n{verify}\n"

ISSUES = [
  ("CI: build and unit tests on every PR (GitHub Actions, macOS)", ["ready", "area:infra", "priority:high"], body(
    "Every PR gets an automatic build + test run so reviews can rely on it.",
    "`make` builds `./solver`; unit tests are `build/test_eval`, `build/test_range`, `build/test_categories`, "
    "`build/test_solver`. `tests/test_upi.py` contains a section that solves a real 25bb tree (~3.3 GB RAM, several "
    "minutes) which is too heavy for CI runners; split it out (see the memory/fixture issue) or skip it via a flag. "
    "zlib is the only dependency.",
    ["Workflow in `.github/workflows/ci.yml` runs on pull_request and push to main (macos-latest)",
     "Runs make, all unit tests and test_upi.py without the 25bb section", "Fails the check on any test failure",
     "README gets a status badge"],
    "Open a PR; the check must run and pass. Break a test locally on a scratch branch to confirm it fails.")),

  ("Linux portability (gcc/clang)", ["ready", "area:infra", "priority:high", "good first issue"], body(
    "Build and pass the tests on Ubuntu with both gcc and clang.",
    "Known blockers: `src/upi.cpp` includes `<sys/sysctl.h>` and Mach APIs (`host_statistics64`) for memory info "
    "(Linux has a `sysconf` fallback but the includes are unconditional); the Makefile uses `-mcpu=native`, which "
    "x86 compilers reject (use `-march=native` there). Check for any other Apple-only assumptions.",
    ["`make && make test` works on Ubuntu 24.04 with gcc 13 and clang 18", "macOS build unchanged",
     "CI matrix (if the CI issue is merged) includes ubuntu-latest"],
    "`docker run --rm -v $PWD:/w -w /w gcc:13 make` (or a Linux VM), then run the unit tests.")),

  ("Windows build so the engine can run where PioViewer runs", ["ready", "area:infra", "priority:high"], body(
    "Produce a working `solver.exe` on Windows (x64).",
    "PioSOLVER users are on Windows; to ever use this engine from PioViewer it must run there. Needs: a Windows "
    "build path (MSVC, clang-cl or MinGW-w64), zlib, Windows memory APIs for `show_memory`/memory checks, CRLF-safe "
    "stdin handling (already tolerated), `__builtin_*` intrinsics replaced or guarded, no POSIX-only headers.",
    ["Documented build (CMake or Makefile target) that produces solver.exe", "Unit tests pass on Windows",
     "`tests/test_upi.py` passes on Windows (minus the 25bb section if RAM is short)"],
    "Build on a Windows machine or CI windows-latest runner; run the tests.")),

  ("Use the engine from PioViewer (drop-in investigation)", ["needs pio", "needs decision", "area:upi", "priority:medium"], body(
    "Find out whether PioViewer can be pointed at a third-party UPI engine and what it needs from it.",
    "Depends on the Windows build. PioViewer talks UPI to the solver process; we already match most formats "
    "(see AGENTS.md, 'Verified Pio behavior'). Investigation must stay black-box: configure PioViewer, enable its "
    "command log (Tools -> Configuration -> Debugging/Logging), and compare the commands it sends with what we "
    "support. Do not modify or patch PioViewer binaries; check its license terms first and flag anything unclear "
    "with `needs decision`.",
    ["Written report in the issue: can the engine path be configured, which commands the viewer uses, which fail",
     "Follow-up issues filed for every failing command / format"],
    "Maintainer-run (needs a licensed Pio install).")),

  ("Exact preflop equities (calc_eq_preflop, preflop all-ins)", ["ready", "area:preflop", "area:engine", "priority:medium"], body(
    "Replace board sampling with exact preflop equity.",
    "Today `calc_eq_preflop` samples 20k boards and preflop all-in / pure-preflop exits use a fixed 3000-board "
    "sample (`GameTree::allin_runouts_id` with an empty board, `src/equity.cpp`). Pio is exact. Recorded Pio "
    "output (OOP `22+,A2s+,K9s+,Q9s+,J9s+,T9s,ATo+,KJo+` vs IP `22+,A2s+,K5s+,Q8s+,J8s+,T8s+,97s+,87s,76s,A2o+,"
    "K9o+,QTo+,JTo`, `calc_eq_preflop OOP`): first values `0.850457072 0.653672099 0.636298418 0.760216177 "
    "0.627568841 0.608110905 0.491490602 0.517899454`, total `0.550421774` (we print 0.851160 ... 0.550733). "
    "Approach suggestion: generate an exact 1326x1326 hand-vs-hand win/tie table with our own evaluator (use suit "
    "isomorphism to cut work), cache it on disk (e.g. ~/.cache) and use it for both features. Do NOT use Pio's "
    "pf_tables.dat.",
    ["calc_eq_preflop matches the recorded values to 1e-6", "Preflop all-in terminals use exact equity",
     "Table generation time and cache size documented; first-run cost acceptable (< 2 min on 10 cores)"],
    "Add the recorded case to tests/test_upi.py and compare.")),

  ("Single-thread speed: match PioSOLVER per iteration", ["ready", "area:perf", "priority:high"], body(
    "Close the per-iteration gap with Pio without changing results.",
    "`tests/pio_compare/bench_flop.txt` (wide SRP flop tree, `set_threads 1`, 60 iterations): Pio 3.10.1 on Zen 5 "
    "4.63 s, ours 6.94 s on an M4 (our exploitability is lower after 60 iterations: 0.119 vs 0.182). Profile "
    "(macOS `sample`): showdown_value ~44%, compat_mass (fold nodes) ~15%, 16-bit encode/decode ~14%, "
    "current_strategy ~9%. Ideas: SIMD-friendly showdown layout, cheaper fold values, fusing regret matching with "
    "the update loop, fewer memsets/allocations, better branch layout.",
    ["bench_flop single-thread <= 5.0 s on the same machine", "Exploitability/EVs unchanged (relative diff < 1e-5)",
     "`./build/test_solver big` and test_upi still pass", "Before/after numbers in the PR"],
    "`./solver < tests/pio_compare/bench_flop.txt` style run (see compare.py for the script format); report wall time.")),

  ("Memory footprint + make the 25bb end-to-end test optional", ["ready", "area:perf", "area:infra", "priority:medium", "good first issue"], body(
    "Let `make test` run on 8-16 GB machines and in CI.",
    "`tests/test_upi.py` builds `tests/data/pio_25bbHU_2sizes.txt` (6.4 GB float32, 3.3 GB with small_strats). "
    "Move that section behind a `--big` flag (default off) and use a smaller tree for the default run. "
    "Separately, look for cheap memory wins: GNode is 32 bytes, ShowdownBoard entries 8 bytes/hand plus sweep "
    "bounds, ChanceInfo keeps 104-byte card maps per chance node.",
    ["`make test` passes in < 4 GB RAM by default", "`python3 tests/test_upi.py ./solver --big` still runs the 25bb fixture",
     "Any structural memory saving measured (estimate_schematic_tree before/after)"],
    "Run make test; run with --big on a large-RAM machine.")),

  ("Incomplete trees: auto-recalc of missing streets", ["needs pio", "area:upi", "priority:medium"], body(
    "Match Pio when browsing saves made with `dump_tree ... no_turns|no_rivers`.",
    "We can save/load truncated trees (`INCOMPLETE_TREE` flag) and `solve_all_splits` re-solves later streets, "
    "but browsing a missing street does not re-solve on demand. Pio: `set_always_recalc <street> <ms>`, "
    "`set_recalc_accuracy f t r` (defaults 0.0025 0.001 0.0005, fraction of pot), `solve_partial`, `rebuild_tree`. "
    "Probes needed for exact Pio outputs (maintainer can run them; write the probe script in "
    "tests/pio_compare/ and ask).",
    ["Querying a node on a missing street re-solves that subtree to recalc accuracy (or the timeout)",
     "set_always_recalc semantics match Pio's probe output", "Tests for no_turns and no_rivers saves"],
    "New probe script + test_upi cases.")),

  ("Track hands of either range (Pio's union hand lists)", ["ready", "area:engine", "area:upi", "priority:low"], body(
    "Optionally keep per-player hand lists as the union of both ranges, like Pio.",
    "Pio computes values for hands that are only in the opponent's range: `calc_ev` prints `inf`/`-inf` for "
    "them (sign of their counterfactual value) and `calc_ev_pp`/`show_strategy_pp` list them (e.g. probe2: Pio "
    "150 vs our 143 lines in calc_ev_pp). We currently print `inf` without a sign and omit them from `_pp`. "
    "Cost: wider vectors. Make it a setting (default to whatever keeps performance; decide in the PR).",
    ["Opponent-only hands get the correct inf sign and appear in _pp outputs with their EV",
     "Benchmark impact reported", "No change to equilibrium results"],
    "Re-run tests/pio_compare/probe2_flop.txt (maintainer) or compare against the recorded behavior above.")),

  ("Remaining PioSOLVER output differences", ["needs decision", "area:upi", "priority:low"], body(
    "Decide and fix (or document) the remaining known differences.",
    "From the probes: (1) `list_algorithms` text (ours is honest about DCFR; Pio lists v4_small/v4_large/pio_cfr/"
    "original_pio descriptions); (2) `show_all_freqs` prints `nan` in Pio 3.10 for every line, we print real "
    "values; (3) `show_save_version` prints `3.10\\n<build hash>` in Pio; (4) `estimate_schematic_tree` has a "
    "~150 MB floor in Pio; (5) `show_iters` after solve_partial is 0 in Pio; (6) `show_nuts` tie order; "
    "(7) `node_count` in preflop trees counts differently (Pio: H_DEC/V_DEC/END totals differ, ROOT 0).",
    ["Owner decision recorded for each item", "Implemented or documented in README 'Known differences'"],
    "pio_compare probes 1-6.")),

  ("Verify preflop-tree numbers against Pio", ["needs pio", "area:preflop", "priority:medium"], body(
    "Understand and reconcile Pio's numeric reporting in preflop trees.",
    "Probe (tests/pio_compare/probe5_preflop_full.txt setup): Pio's `calc_results` stayed at EV OOP 12.411 / "
    "EV IP 15.714 (sum 28.125 with blinds 10/5) before and after 300 iterations, `calc_ev` at preflop nodes printed "
    "nan, ranges at exits were 0. Ours converges to a zero-sum result (EV OOP -1.799 / IP 1.799, exploitability "
    "0.004). Possible causes: result caching (`clear_cache`), different EV conventions, or a probe mistake. "
    "Our structure and show_win_evs already match.",
    ["Explanation of Pio's preflop EV/matchup conventions", "Our outputs match (or documented differences)",
     "Probe script committed"],
    "Maintainer runs the probe; agent designs it and implements the result.")),

  ("Isomorphism on river deals inside preflop subtrees", ["needs pio", "area:preflop", "area:engine", "priority:low"], body(
    "Match Pio's node counts exactly in preflop trees.",
    "Postflop trees match Pio's node_count exactly on monotone, paired and trips boards (probe6). In the probe5 "
    "preflop tree Pio has 17208 river decision nodes per player vs our 17016 (turn counts equal), so Pio uses a "
    "weaker symmetry somewhere on river deals in preflop subtrees (flops AsKsQs and 7c7d2h). Only performance/"
    "count differs, not correctness.",
    ["Rule identified from probes", "node_count matches Pio on the probe5 tree"],
    "Probe with single flops to isolate which subtree differs.")),

  ("Verify ICM and rake against Pio", ["needs pio", "area:engine", "priority:medium"], body(
    "Confirm set_icm / set_icm_point / set_rake payoffs match Pio.",
    "Our model: rake = min(frac * pot, cap) taken from showdown and fold pots (matched part); ICM payoff = "
    "icm(final stack) - icm(start stack) with piecewise-linear points. Pio docs example: "
    "`show_win_evs OOP r:0:b99:b200:c` -> `380 -200 90` without ICM, `29.2733116 -20.3105125 7.67309189` with ICM. "
    "Rake/ICM have never been compared with Pio.",
    ["Probe script comparing show_win_evs, calc_results with rake and ICM", "Differences fixed or documented"],
    "Maintainer runs the probe.")),

  ("Tree builder: bet-size config -> add_line commands", ["ready", "area:tools", "priority:high", "good first issue"], body(
    "A helper that turns PioViewer-style bet-size settings into the explicit add_line list the solver needs.",
    "The solver only accepts explicit lines. `tests/data/pio_25bbHU_2sizes.txt` contains BOTH a viewer config "
    "header (`#FlopConfig.BetSize#30,60`, `#FlopConfig.RaiseSize#40`, `#AllinThreshold#67`, "
    "`#AddAllinOnlyIfLessThanThisTimesThePot#300`, `#RiverConfig.AddAllin#True`, donk sizes, IP configs, pot 40, "
    "stacks 230) and the 266 add_line commands PioViewer generated from it: a perfect oracle. Implement "
    "(Python in tools/ or a C++ command) and reproduce that exact set; document the inferred rounding/all-in rules.",
    ["Generated line set equals the fixture's 266 lines exactly (order-insensitive)", "Unit test in tests/",
     "README section with an example"],
    "`python3 tools/tree_builder.py tests/data/pio_25bbHU_2sizes.txt` and diff with the fixture's add_line lines.")),

  ("Node-locking semantics vs Pio (set_strategy / lock_node under v4)", ["needs pio", "area:upi", "priority:medium"], body(
    "Decide and match how strategy edits behave per algorithm.",
    "pyosolver's notes on Pio 3.10: under the default v4 algorithm `set_strategy` answers ok but does not stick "
    "(strategies derive from regrets); users must `set_algorithm original_pio` + `rebuild_tree` first. Ours "
    "always sticks. Also untested against Pio: combo_lock_node, lock_line/unlock_line over runouts, "
    "solve after locking.",
    ["Probe results for set_strategy/lock_node/combo_lock_node per algorithm", "Decision (match Pio or keep ours) recorded",
     "Tests for the locking workflow"],
    "Maintainer runs the probe.")),

  ("UPI command reference with verified status", ["ready", "area:docs", "priority:low", "good first issue"], body(
    "One table of every UPI command: implemented?, matches Pio exactly / semantically / differs, and which test covers it.",
    "Sources: README command list, AGENTS.md 'Verified Pio behavior', tests/test_upi.py, tests/pio_compare/probe*.txt.",
    ["docs/commands.md with all ~130 commands", "Linked from README"],
    "Review only.")),
]


def run(args, check=True):
    p = subprocess.run([GH] + args, capture_output=True, text=True)
    if check and p.returncode != 0:
        print(p.stderr, file=sys.stderr)
    return p


def main():
    existing = {l["name"] for l in json.loads(run(["label", "list", "-R", REPO, "--json", "name", "-L", "200"]).stdout or "[]")}
    for name, (color, desc) in LABELS.items():
        if name in existing:
            run(["label", "edit", name, "-R", REPO, "--color", color, "--description", desc], check=False)
        else:
            run(["label", "create", name, "-R", REPO, "--color", color, "--description", desc])
    titles = {i["title"] for i in json.loads(run(["issue", "list", "-R", REPO, "--state", "all", "--json", "title", "-L", "500"]).stdout or "[]")}
    for title, labels, text in ISSUES:
        if title in titles:
            print("exists:", title)
            continue
        args = ["issue", "create", "-R", REPO, "--title", title, "--body", text]
        for l in labels:
            args += ["--label", l]
        p = run(args)
        print(p.stdout.strip() or p.stderr.strip())


if __name__ == "__main__":
    main()
