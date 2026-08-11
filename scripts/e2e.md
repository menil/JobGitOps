# E2E Runbook — Bootstrap Installer & Sync Template

Manual end-to-end verification for the bootstrap installer (spec
`specs/bootstrap-installer.md` §10). Run before each release, and after any
change to `scripts/install.sh`, `scripts/sync-template.sh`, or the shell-plane
allowlist (§3).

## Preconditions

- `gh` installed and authenticated with the `repo` **and** `workflow` scopes
  (`gh auth refresh -s repo,workflow` if the check fails).
- A disposable repo name, e.g. `jobgitops-e2e-<username>`.
- For the live run: a `GEMINI_API_KEY` (or `OPENROUTER_API_KEY`) to install as a
  repo secret.
- Inside the devenv shell (`devenv shell` or `direnv allow`).

> **Pre-release note:** until the first public release exists, the repo is
> private, so `raw.githubusercontent.com` and `codeload` tag URLs return 404 and
> the `curl | sh` one-liner cannot be live-tested. Run `scripts/install.sh`
> locally instead, and use `JOBGITOPS_TAG=main` with `scripts/sync-template.sh`
> to exercise the tag->branch fallback. The remote one-liner is verified once
> releases exist (release gate).

## 1. Dry-run

```bash
sh scripts/install.sh jobgitops-e2e --dry-run
```

Verify: every command is printed and *nothing runs* — no repo created, no
secrets set, no API mutations. Exit is clean. `--dry-run` never prompts for a
provider key (the trace logs the key-verification step with the key redacted).
Also spot-check argument validation:

```bash
sh scripts/install.sh bad-name! --dry-run    # fails fast on the slug
sh scripts/install.sh                        # prompts for the repo name
```

The bare interactive `sh scripts/install.sh` also prompts for the provider
(`Gemini` / `OpenRouter`) and then for exactly that one key, echoing `*` per
keystroke; the key is verified against the provider API before any repo is
created.

## 2. Live install

```bash
GEMINI_API_KEY=... sh scripts/install.sh jobgitops-e2e --yes
```

Verify, in order:

1. Repo created **private** (`gh repo view --json visibility --jq .visibility`).
2. `GEMINI_API_KEY` secret set (`gh secret list`).
3. Actions enabled with write permissions:
   `gh api repos/<owner>/<repo>/actions/permissions --jq .default_workflow_permissions`
   → `write`.
4. First push succeeds and `sync-labels.yml` runs (Actions tab).
5. Repo tree matches the shell plane + template layout: `.github/labels.yml`,
   the six runtime-core workflows, `config/settings.yaml`, the placeholder
   `resumes/resume.yaml`, renderer templates, README, `.gitignore` — and
   **no** `src/`, `tests/`, `Dockerfile`, `Justfile`, or maintainer workflows.

**2a. Rejected key fails fast**

```bash
GEMINI_API_KEY=not-a-valid-key sh scripts/install.sh jobgitops-e2e-badkey --yes
```

Verify: the installer dies with `Gemini key rejected by the API` **before** any
repo is created, secret set, or API mutation — `gh repo view jobgitops-e2e-badkey`
still reports "not found". A transport failure (e.g. network down) must instead
report `could not reach the ... API`, never a false "rejected".

## 3. Placeholder resume produces no runs (S4)

With the sentinel still in `resumes/resume.yaml`, push a trivial commit to
`main`. Verify the scraper's `check` job reports "Setup pending — replace
`resumes/resume.yaml`" and no scrape step runs. `config/**` changes never
trigger a scrape (path filter).

## 4. Exactly one bootstrap scrape (S3)

1. Replace `resumes/resume.yaml` with real content (sentinel removed), commit,
   push.
2. Verify exactly **one** scrape run fires (Actions tab).
3. Verify the `JOBGITOPS_INITIALIZED` variable is now `true`:
   `gh variable list`.
4. Push another resume edit → **no** new scrape.
5. Re-run once more with a resume edit after forcing the variable off
   (`gh variable delete JOBGITOPS_INITIALIZED`) to confirm the bootstrap
   self-heals — exactly one scrape fires again.

## 5. Static setup badge (S2)

Confirm the README ships the static setup badge
(`![Setup](https://img.shields.io/badge/setup%20required-replace%20resumes%2Fresume.yaml-orange)`).
After the first successful scrape, remove the badge manually and push.

## 6. sync-template.sh produces a PR (§7.6)

From the maintainer repo, against the throwaway repo (or any target):

```bash
sh scripts/sync-template.sh <owner>/jobgitops-e2e
```

Verify:

1. Branch `sync/upstream-template` is created from the default branch HEAD.
2. A PR is opened titled `chore: sync .github/ from JobGitOps <tag>`, body
   lists the changed files and links the release.
3. Only the shell-plane allowlist paths can appear in the diff — never
   `config/`, `resumes/`, `status/`, or `README.md` — and never maintainer
   workflows.
4. The PR is **not** auto-merged.
5. Re-run the script immediately: no diff → exit 0 with **no** new PR (the
   existing open PR is reused if the diff changed).

Pre-release: `JOBGITOPS_TAG=main sh scripts/sync-template.sh <owner>/repo`
exercises the tag→branch fallback.

## 7. Cleanup

```bash
gh repo delete <owner>/jobgitops-e2e --yes --confirm   # delete the throwaway
gh pr close <number> --repo <owner>/repo               # if a sync PR was left open
gh branch -D sync/upstream-template                    # after the PR is closed
```

Leave no throwaway repos, PRs, or branches behind after a release pass.
