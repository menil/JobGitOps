# AI Decision Record: Restore working tree after Gmail sync cursor commit

## Context & Goal

Discovered via live, first-time production use of Gmail Sync on a real
repository (`menil/job-search`): a manually-triggered `gmail-sync.yml` run
completed its actual sync work successfully (Projects V2 statuses updated,
cursor advanced) but the GitHub Actions job still reported failed. The
`Update Badge (Passed)` step errored with:

```
python: can't open file '.../.github/scripts/update_badge.py': [Errno 2] No such file or directory
```

Goal: find the root cause and fix it so a successful sync run reports success,
for every repo with Gmail Sync enabled, not just this one.

## Root Cause

`_finalize_cursor` (src/jobgitops/cli/gmail_sync.py) commits the advanced
sync cursor onto `gmail-sync-state`, a disconnected orphan branch with no
shared history with `main`. It checks that branch out in place and never
checks back out afterward. `gmail-sync.yml` runs `Update Badge (Passed)` as
a *later step in the same job*, inheriting whatever the previous step left
checked out. Since `.github/scripts/update_badge.py` only exists on the
original branch (`main`), not on the orphan branch, every successful sync
run broke the badge step. This affects every repo with Gmail Sync enabled,
not just an old or unusual one — it's a design gap in `gmail-sync.yml` /
`_finalize_cursor`'s division of responsibility, not repo-specific state.

Notably, this exact risk class was already flagged in `load_cursor`'s
docstring ("an in-place checkout here would replace every other file in the
working directory... for the rest of the run") — but that reasoning assumed
the checkout was safe because it happens "right at the end" of *this
script's own execution*. It didn't account for the surrounding CI job
running further steps against the same checkout afterward.

## Architecture & Key Decisions

Capture `git rev-parse HEAD` before any state-branch work, then restore it
in a `finally` block after `_finalize_cursor_on_state_branch` runs (a new
helper split out of `_finalize_cursor` specifically so the `finally` can
wrap the whole risky region). The restore uses `checkout --force`, not a
plain `checkout`, because on a genuine first-ever run `_clear_working_tree`
stages the deletion of every file inherited from the original branch (via
`git rm -rf`); if a later step then fails before that's committed, a plain
`checkout <ref> --` would be refused ("local changes would be
overwritten"). Any local state on the orphan branch is disposable by
design at that point, so forcing past it is correct and is the only way to
reliably guarantee later workflow steps get the original branch's files
back.

Every new failure path (can't resolve HEAD, can't restore) is logged, not
raised, consistent with the function's existing contract: a cursor-commit
problem must never roll back GitHub issue side effects already applied
this run.

## Alternatives Considered & Rejected

1. **Fix only in `gmail-sync.yml`** (e.g. `git checkout ${{ github.sha }}`
   before the badge steps, `if: always()`). Smaller diff, but only patches
   the one CI caller — leaves the function's own contract broken for any
   other caller (local runs, future workflow steps added after "Run Gmail
   Sync", tests), with no unit-test coverage of the invariant. Rejected:
   fixing it at the function boundary is a better tradeoff despite being a
   few more lines.
2. **Git worktree** for the orphan-branch operations, avoiding any
   checkout of the job's own working directory. Considered more "correct"
   in isolation, but not actually simpler: needs its own fallible cleanup
   (`git worktree remove`, same log-don't-raise handling), threads a
   worktree path through `_checkout_state_branch` /`_write_cursor_file` /
   `_commit_cursor` / `_push_state_branch`, and adds a new failure surface
   on a constrained containerized CI runner (disk/permissions). Rejected
   as disproportionate effort for a targeted bug fix.

## Verification

- `just validate` (ruff lint, ruff format check, pytest with 90% coverage
  gate, installer vitest suite): all green. `gmail_sync.py` is now at 100%
  line coverage.
- New/updated tests cover: successful restore after state-branch work,
  restore still runs when checking out the state branch fails, restore is
  skipped entirely if HEAD can't be resolved (never touches the orphan
  branch with no way back), and a restore-checkout failure itself is
  logged, not raised.
- Full 8-pass self-review panel run before commit (Code Reviewer, Security,
  Quality & Style, Test Quality, Performance, Deployment Safety,
  Simplification, PII) — two real findings (missing `--force`, missing
  restore-failure test) applied; all other passes clean.
