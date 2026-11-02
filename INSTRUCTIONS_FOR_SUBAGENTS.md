# Instructions for subagents

You have been asked to work on **WSolver**, a heads-up no-limit hold'em solver with a
PioSOLVER-compatible text interface (UPI).

**Your job: turn open issues into pull requests, autonomously, one after another.** Do not ask the
human what to do, do not wait for approval, do not wait for your PR to be reviewed before starting the
next issue. Make reasonable decisions yourself and explain them in the PR. The only reason to stop and
ask is missing GitHub access (step 1). Follow these steps in order; do not skip step 2.

- Repository: `weston/WSolver` (private) on GitHub, `https://github.com/weston/WSolver`
- Human owner: Weston
- Maintainer: `weston@claude_915d701b-8c65-4f0e-9b6e-9b6bc8bb28f2_macmini`

---

## 1. Get access and the code

All agents use the **same GitHub account: `weston`**. Check that you are authenticated as it:

```sh
gh auth status            # should say: Logged in to github.com account weston
ssh -T git@github.com     # should say: Hi weston!
```

If neither works, stop and ask the human to either run `gh auth login` on this computer, or add an
SSH key for this computer to the `weston` account (generate one with
`ssh-keygen -t ed25519 -C "<your name from step 2>"` and give them the `.pub` line). Never use or ask for
passwords or tokens in chat.

Then get the code:

```sh
git clone git@github.com:weston/WSolver.git ~/WSolver    # or: gh repo clone weston/WSolver ~/WSolver
cd ~/WSolver && git pull
make && ./build/test_eval && ./build/test_solver          # must pass before you change anything
```

Read [AGENTS.md](AGENTS.md) next: code map, ground rules, test commands, verified Pio behavior.

## 2. Work out your name and sign everything with it

Because every agent is `weston` on GitHub and in git, **your signature is the only way anyone can
tell you apart**. Your name is:

    weston@<agent>_<session_id>_<machine>

| Part | What to use |
|---|---|
| `<agent>` | your tool in lowercase: `claude`, `codex`, `kimi`, `cursor`, ... |
| `<session_id>` | the id this session can be resumed with. Claude Code: the UUID used by `claude --resume <id>` (on disk it is the `.jsonl` filename of this session under `~/.claude/projects/<project>/`). Codex: the id shown by `codex resume`. If you cannot find it, ask the human. **Never invent one.** |
| `<machine>` | short name of this computer, lowercase, no spaces: e.g. `macmini`, `macbookair`, `westonspc` (from `scutil --get ComputerName` on macOS or `hostname` elsewhere) |

Example: `weston@claude_915d701b-8c65-4f0e-9b6e-9b6bc8bb28f2_macmini`.

Sign **every GitHub message** you write and every commit:
- issue comments, PR descriptions, PR review replies: end with `— <your name>`
- commit messages: add the trailer line `Agent: <your name>`

Chat replies to the human do not need a signature.

When you mention another agent, use their full name.

## 3. Pick an issue and claim it

```sh
gh issue list -R weston/WSolver --label ready --state open
gh issue view <n> -R weston/WSolver --comments
```

Choose an issue that is labelled `ready`, is **not** labelled `in progress`, and has no recent claim
comment. Skip `needs pio` issues (they need a real PioSOLVER, which only the maintainer can run; you may
still write the probe script and ask) and `needs decision` issues (waiting on the human). Prefer
`priority:high`, then `good first issue`.

You may also open issues proactively for bugs, missing tests, documentation gaps, or useful
improvements, even while other ready issues exist. Sign the issue body, describe the problem and
acceptance criteria, and follow the claim workflow before implementing it.

Claim it, then label it:

```sh
gh issue comment <n> -R weston/WSolver --body "Claiming this. Plan: <one or two lines>.

— <your name>"
gh issue edit <n> -R weston/WSolver --add-label "in progress"
```

If someone else claimed it while you were looking (check the comments again), pick another one. One
issue at a time.

## 4. Do the work on a branch

```sh
git switch main && git pull
git switch -c issue-<n>-<short-slug>
```

- Never commit to or push `main`. Never force-push. Never touch another agent's branch.
- Keep changes focused on the issue. Add or update tests.
- Clean room: build from the public UPI docs and recorded behavior only. Never decompile or copy
  PioSOLVER code or data files.
- No secrets, keys, passwords, hostnames or personal data in commits.
- Run the tests before you push: `make && ./build/test_eval && ./build/test_range && ./build/test_categories && ./build/test_solver && python3 tests/test_upi.py ./solver`.
  (The 25bb section of `test_upi.py` needs about 3.5 GB of free RAM; if it does not fit, say so in the PR.)

Commit messages end with your trailer:

```
Short summary of the change (#<n>)

Why and how.

Agent: <your name>
```

## 5. Open a pull request

```sh
git push -u origin issue-<n>-<short-slug>
gh pr create -R weston/WSolver --base main --title "<summary> (#<n>)" --body-file <file>
```

Use the template in `.github/pull_request_template.md`: what changed, how you verified it (paste the
test output), numeric impact (before/after exploitability if results change), what is left, `Closes #<n>`,
and your signature. Then comment on the issue with the PR link (signed).

## 6. Review loop

Agents may merge PRs, including their own, when the changes look correct, relevant checks pass, and
review concerns are resolved. Maintainer approval is not required. Because everyone is the same
GitHub account, reviews arrive as **comments**, not approvals. Check for them:

```sh
gh pr view <pr> -R weston/WSolver --comments
```

Address each comment by pushing more commits to the same branch and replying (signed) to the comment.
Review the final diff and test results before squash-merging with `gh pr merge --squash`.
You may continue to the next issue while a PR awaits checks or review. Each time you start a new
issue, first check your open PRs for review comments
(`gh pr list -R weston/WSolver --state open` and look for your signature) and handle those first.

## 7. When you are unsure or blocked

- Unclear requirement: pick the most reasonable interpretation, implement it, and state the assumption
  in the PR description. The maintainer will correct course in review.
- Truly blocked (needs a real PioSOLVER run, or a product decision): comment on the issue (signed)
  explaining exactly what you need, remove `in progress`, and move on to another issue.
- No `ready` issues left: look for gaps you can fix (failing or missing tests, TODOs, README errors),
  open an issue describing it (signed, label `ready`), claim it and submit a PR.
