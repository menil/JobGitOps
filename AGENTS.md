# Agent Instructions

This file provides rules, instructions, and context for AI coding agents and human developers working on this project.

## Developer Environment (Nix & devenv)

This project uses `devenv` to manage system and virtual environment dependencies:
- **Enter Environment**: Run `devenv shell` (or use `direnv allow` to automatically load it).
- **Native dependencies**: WeasyPrint dependencies (`cairo`, `pango`, `glib`, `gdk-pixbuf`, etc.) are mapped cleanly inside the Nix shell.
- **Shell Recommendation**: To avoid Nix startup latency and environment issues, always enter the Nix shell once (via `devenv shell`) and run your commands inside that active shell session. Running development commands directly on the host system will fail due to missing dependencies.
- **Sandbox Execution**: When running devenv/Nix development commands in sandboxed environments (such as Antigravity run_command), set `BypassSandbox: true` so the process can dynamically load Cairo, Pango, and other shared libraries from the Nix store path (`/nix/store/...`).

## Quality Gates & Tasks (`Justfile`)

Always use the local task runner `just` to validate your changes:
- **Format code**: `just format` (uses Ruff)
- **Check formatting**: `just format-check` (verifies formatting without writing changes)
- **Lint code**: `just lint` (uses Ruff)
- **Run validations/tests**: `just validate` (runs lints, format check, and test suite via pytest with a 90% coverage threshold requirement)

### Standalone Tests
Within the Nix shell environment, you can run tests directly via `pytest` for targeted testing and debugging:
- **Run entire suite**: `pytest`
- **Run specific file**: `pytest tests/test_placeholder.py`
- **Run specific test case**: `pytest tests/test_placeholder.py::test_placeholder`

## Git & Commit Guidelines

- **Feature Branches**: Always develop and commit changes on a separate task/feature branch (e.g., `task/`, `feat/`, `fix/`) rather than committing directly to `main`.
- **Branch Creation Ref**: When creating a branch from a base branch, use `git checkout -b <new-branch> <base-branch>` without using `--` before the base branch (which incorrectly marks it as a file path).
- **Pre-commit Validation**: Run `just validate` before committing code to ensure linting, formatting, and all tests pass.
- **Conventional Commits**: Commit messages must follow standard Conventional Commit guidelines (e.g., `feat:`, `fix:`, `docs:`, `refactor:`). Keep both the commit title and all body lines strictly under 72 characters to prevent validation failures in the `commit-msg` git hook.
- **Exclude Auto-Generated Agent Instructions**: Never stage, commit, or track IDE/agent-specific auto-generated directories and files (such as `.agents/`, `.codex/`, or `.claude/`).
- **Git Checkout double-dash Syntax**: When creating a new branch off a base starting point (e.g. `main`), do not place the double-dash `--` separator before the starting point (e.g., avoid `git checkout -b <new-branch> -- main`). This causes Git to parse the base commit as a pathspec/file, leading to resolution failures. If option-termination is needed, place the `--` at the end (e.g., `git checkout -b <new-branch> main --`).
- **Commit Amending**: Prefer amending the existing commit (`git commit --amend --no-edit` and force pushing via `git push --force-with-lease` for safety, only on branches owned solely by you) when applying review feedback or bug fixes on task branches, to keep a single clean commit per PR.
- **Do Not Merge Pull Requests**: Under no circumstances should you merge a Pull Request (via `gh pr merge` or any other tool/API call). Pull Requests must always be left open for the human developer to review, approve, and merge.
- **Upstream Sync**: Always fetch (`git fetch origin`) and rebase (`git rebase origin/main`) your task branches on the latest target branch before creating or updating a Pull Request. This prevents carrying outdated or duplicate commits from merged dependency PRs.

## Python Schema & Parsing Conventions

- **Gemini SDK Model Prefixing**: When using the `google-generativeai` SDK, always use the fully qualified model name with the `models/` prefix (e.g., `models/gemini-2.0-flash` or `models/gemini-2.5-flash`) to prevent format errors on new models not recognized as shorthands by older SDK versions.
- **Parse, Don't Validate**: When implementing loaders and data models (e.g., for YAML/JSON Resume data), follow the "parse, don't validate" pattern (see [Alexis King's design post](https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/) for details):
  - Avoid writing hand-crafted type checks and validation raises for basic scalar attributes.
  - Coerce inputs safely to their target types (such as converting scalars to strings and explicit `.isoformat()` serialization for `datetime.date`/`datetime.datetime` objects).
  - Raise custom parsing/validation errors only for missing mandatory fields or structural collection mismatches (like non-list shapes where lists are expected).
- **Pandas DataFrame Sanitization**:
  - Always clean Pandas DataFrame cells against `NaN` / `float('nan')` values using `pd.isna()` or `math.isnan()`. Do not rely on `is not None` or standard string coercion `str()`, as python-coerced `nan` values evaluate to truthy strings.
  - Guard against duplicate column names returning `pd.Series` from `.get()` or row indexing by checking `isinstance(val, pd.Series)` and extracting the scalar value.

## Cache & Deduplication Resilience

- **Conditional Cache Addition**: Never update deduplication caches (e.g., calling `existing_jobs.add()`) outside of try-except blocks. Cache updates must be placed strictly inside successful execution scopes of write API calls to prevent failing requests from polluting the cache.

---

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
