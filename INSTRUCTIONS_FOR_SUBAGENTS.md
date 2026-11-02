# Instructions for subagents

You have been asked to work on **WSolver**, a heads-up no-limit hold'em solver with a
PioSOLVER-compatible text interface (UPI). Follow these steps in order. Do not skip step 2.

- Repository: `weston/WSolver` (private) on GitHub, `https://github.com/weston/WSolver`
- Human owner: Weston
- Maintainer (reviews and merges every PR): `weston@claude_915d701b-8c65-4f0e-9b6e-9b6bc8bb28f2_macmini`

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

Sign **every** message you write:
- issue comments, PR descriptions, PR review replies: end with `— <your name>`
- commit messages: add the trailer line `Agent: <your name>`
- chat replies to the human: end with your name too

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

The maintainer reviews every PR and merges it (squash). Because everyone is the same GitHub account,
reviews arrive as **comments**, not approvals. Check for them:

```sh
gh pr view <pr> -R weston/WSolver --comments
```

Address each comment by pushing more commits to the same branch and replying (signed) to the comment.
**Do not merge your own PR.** When it is merged, delete your local branch, comment on the issue if
anything is left over, and go back to step 3.

## 7. When you are blocked

Ask in the issue (signed), mentioning the maintainer by name. If the question is for the human owner,
add the `needs decision` label. If you stop working on an issue, comment why and remove `in progress`.
