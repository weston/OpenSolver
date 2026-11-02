# Working on WSolver (OpenUPI-Solver)

**New agent? Start with [INSTRUCTIONS_FOR_SUBAGENTS.md](INSTRUCTIONS_FOR_SUBAGENTS.md)** (access, your
signature, claiming issues, PRs). This file is the technical reference.

## What this is

A heads-up NLHE solver (postflop and full preflop trees) that speaks PioSOLVER's text protocol, UPI.
The goal is a drop-in replacement for the PioSOLVER engine: same commands, same output formats, same
tree semantics, equal or better speed. `README.md` has the user-facing overview and known differences.

## Identity: sign everything

Every agent pushes and comments as the same GitHub account (`weston`), so git metadata cannot tell
agents apart. Each agent therefore has a name and **signs every GitHub message** with it:

    weston@<agent>_<session_id>_<machine>

- `<agent>`: your tool, e.g. `claude`, `codex`, `kimi`.
- `<session_id>`: the id you can be resumed with (for Claude Code: the `claude --resume <session_id>`
  UUID).
- `<machine>`: short machine name, e.g. `macmini`, `macbookair`, `westonspc`.

Example (the maintainer): `weston@claude_915d701b-8c65-4f0e-9b6e-9b6bc8bb28f2_macmini`.

Sign: issue comments (including claims), PR descriptions, review comments, replies, and commit
messages (add a trailer line `Agent: <your name>`). Address other agents by their full name.
Chat replies to the human do not need a signature.

## Workflow: issues -> branches -> pull requests

All agents work through PRs. Agents may open issues proactively and may merge PRs, including their
own, when the changes look correct, relevant checks pass, and review concerns are resolved.

1. **Pick an issue** labelled `ready` (and not `in progress`). Comment `Claiming this` signed with your
   name (see above), then add the `in progress` label. All agents share the GitHub account `weston`, so
   the signed comment is how ownership is tracked. Abandoning? Comment (signed) and remove the label.
2. **Branch** from up-to-date `main`: `git switch -c issue-<number>-<short-slug>`.
   Never push directly to `main` or force-push someone else's branch.
3. **Work in small commits.** Run `make test` (small memory footprint by default; `make test-big` runs
   the 25bb fixture and needs ~3.5 GB of free RAM — say so in the PR if you could not run it).
   Add or update tests for what you change.
4. **Open a PR** to `main` with `gh pr create --fill` or the web UI. Title: `<summary> (#<issue>)`.
   Fill in the template: what changed, how you verified it (paste test output), numeric impact,
   anything left undone. Link the issue with `Closes #<n>`.
5. **Review and merge.** Review the diff and relevant test results. Address review comments by pushing
   more commits to the same branch and replying to each comment. Agents may squash-merge when the
   changes look correct, relevant checks pass, and review concerns are resolved; maintainer approval
   is not required. GitHub does not allow the single shared account to approve its own PRs, so use
   review comments when providing feedback.
6. **Questions** go in the issue, not in code comments. If a decision is needed from the human owner,
   label the issue `needs decision`.

Pushing: you can use any SSH key on the `weston` account. Check with `ssh -T git@github.com`
(should greet `weston`). Don't commit keys, tokens, passwords, hostnames or personal data; the
Pio comparison scripts take the host from `PIO_SSH`.

## Ground rules

1. **Clean room.** Implement from the public UPI docs (https://piosolver.com/docs/upi/) and from
   black-box behavior (commands in, text out). Never decompile, disassemble or copy PioSOLVER code or
   data files (`pf_tables.dat`, `preflop_subsets/*`, etc.).
2. **Pio's actual behavior wins over its docs.** Many details differ from the documentation; the ones
   we verified are listed under "Verified Pio behavior" below. Don't "fix" them back to the docs.
3. **Keep every test green** (`make test`) and keep results bit-for-bit stable unless the issue says
   otherwise. If you change numerics, say so in the PR and show before/after exploitability.
4. **Small PRs, one issue each.** Reference the issue number. Describe how you verified the change.
5. Commit trailers: end commit messages with `Agent: <your name>` (and `Co-Authored-By: <model>` if
   your tool adds one).

## Build and test

```sh
make                       # ./solver (clang++ -O3 -mcpu=native; zlib)
make test                  # unit tests + tests/test_upi.py (end-to-end on a small tree, < 1 GB RAM)
make test-big              # same, but test_upi.py solves the real 25bb tree (~3.5 GB free RAM needed)
./build/test_eval          # exhaustive evaluator check (all 133M 7-card hands, ~2 s)
./build/test_solver big    # convergence + timing on river / turn / flop trees
python3 tests/test_upi.py ./solver
```

`tests/test_categories.cpp` compares the range explorer byte-for-byte against recorded Pio output in
`tests/data/pio_categories/`.

## Layout

| Path | What |
|---|---|
| `src/cards.*` | card/combos, Pio hand order (`2d2c 2h2c ...`), suit permutations |
| `src/eval.*` | 5-7 card evaluator (value = category<<20 \| rank nibbles) |
| `src/range.*` | range text parser/formatter (Pio itself only accepts 1326 floats) |
| `src/action_tree.*` | `add_line`/`add_preflop_line` -> betting tree (implicit check/call/fold, Pio child order) |
| `src/game.*` | full game tree: chance expansion, isomorphism, flop subsets, showdown orderings, storage (float32 or 16-bit) |
| `src/solver.*` | vector-form DCFR (α=1.5, β=0, γ=2), best response, evaluation |
| `src/parallel.*` | nested work-stealing thread pool + per-thread arenas |
| `src/equity.*`, `src/categories.*` | calc_eq family, range explorer |
| `src/upi*.cpp` | command processor, node queries, save/load |
| `tests/pio_compare/` | black-box comparison harness against a real PioSOLVER |

Key invariants:
- Per-player hand lists hold combos with weight > 0 not blocked by the root board; deeper chance nodes
  zero the reach of blocked hands instead of shrinking lists.
- Isomorphic chance children are computed once; queries resolve real cards through a suit-permutation
  "frame" (`NodeRef::frame`).
- Investments in the betting tree are absolute (they include `set_pot`'s per-player chips); payoffs are
  share of the final pot minus own investment, plus a per-subtree `offset` inside preflop trees.

## Comparing against PioSOLVER

`tests/pio_compare/compare.py SCRIPT` runs the same UPI script on our binary and on a real PioSOLVER
over SSH and diffs every response (numeric tolerance for floats). It needs `PIO_SSH=user@host` (a Windows
machine with a licensed PioSOLVER, reachable over SSH) and optionally `PIO_KEY`, `PIO_EXE`.
`pio_session.py` is an interactive variant. `analyze.py` compares 1326-value outputs hand by hand.

**Most agents will not have Pio access.** In that case work from the recorded outputs: the probe
scripts (`probe*.txt`), `tests/data/`, the facts below, and issue descriptions (which quote Pio output
where relevant). The maintainer can run probes on request; ask in the issue.

### Verified Pio behavior (3.10.1) that the docs don't state

- Node-ID bet amounts are **cumulative** chips invested (`r:0:c:b20:c:7s:b60`).
- Children order: bets/raises largest first, then check/call, then fold. Dealt cards are listed from
  `As` down to `2c`; `?` means the first of them. `show_all_lines` walks the reverse order with `?`.
- An all-in before the river is an `END_NODE` (no chance nodes); its value averages all runouts.
- Node blocks: `id`, type, board (trailing space; `---` preflop), pot `oop ip dead ` (trailing space),
  `<n> children`, `flags: <f> ` and a blank line. `show_children` concatenates `child i:` + block.
- `calc_ev`/`calc_eq` matchups are weighted by the hand's own reach (`calc_eq` also times the number of
  ordered runouts); out-of-range hands print `nan` / `inf`.
- `set_range` accepts only 1326 numbers. Errors are `ERROR: <cmd> incorrect path|incorrect node|incorrect
  or missing argument|missing/incorrect tree`, unknown commands `ERROR: Command X not recognized`.
- `eliminate_path` is a one-time edit that moves the frequency to check/call.
- Rake (`set_rake frac cap`) and ICM payoffs match Pio exactly (probe8). Rake and ICM are last-one-wins;
  `set_icm` before any `set_icm_point` prints `ERROR: set_icm ICM points not defined.` then `set_icm ok!`
  and leaves ICM off; `reset_icm_tables` leaves rake alone and counts as defined tables.
- Displayed strategies for hands that never reach a node come from regrets (v4 algorithm).
- Preflop: SB is `IP` and acts first; `set_pot <bb> <sb> 0`; preflop and postflop line amounts are
  absolute; subset flops are node tokens exactly as added (`r:0:b25:c:7c7d2h`); postflop subtrees show
  their schematic pot and `show_win_evs` is schematic-relative; **Pio crashes in `add_all_flops` unless
  `set_board` was called before attaching schematic trees**.
