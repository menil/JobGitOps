# Releasing JobGitOps

Short release checklist (spec `specs/bootstrap-installer.md` §8). A release is
a semver tag on `main`; the tag *is* the version pin for every consumer URL.

## Checklist

1. **One-time (first release only):** flip the GHCR package to public so every
   user repo can pull the shared engine image without credentials:

   ```bash
   gh api --method POST /user/packages/container/jobgitops/visibility \
     -f visibility=public
   ```

   Verify with `gh api /user/packages/container/jobgitops/visibility --jq .visibility`
   → `public`. Until this is done, `ghcr.io/menil/jobgitops:latest` cannot be
   pulled by non-owners and every consumer workflow fails.

2. **E2E pass** — run the manual runbook in [scripts/e2e.md](scripts/e2e.md)
   against a throwaway repo: dry-run, live install, exactly one bootstrap
   scrape, static badge, and a `sync-template.sh` PR.

3. **Cut the tag** on `main`:

   ```bash
   git fetch origin && git checkout main && git pull --rebase
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

   Tag pushes trigger `build-runner.yml`, which builds and pushes the image as
   both `:vX.Y.Z` and `:latest` (§6.3). Watch the workflow to completion under
   the **Actions** tab.

4. **Create the GitHub release** for `vX.Y.Z` (title + notes). The release
   tag is what the installer and sync scripts resolve (`gh api
   repos/menil/jobgitops/releases/latest`).

5. **No bump step needed.** The install URL *is* the pin:

   ```
   https://raw.githubusercontent.com/menil/jobgitops/vX.Y.Z/scripts/install.sh
   ```

   Each release gets its own immutable URL; users pin a version by using the
   matching tag URL. Document the current recommended version in the README's
   Quick Start one-liner.

6. **Shell-plane updates are manual.** Users pull `.github/` diffs on their own
   schedule via `scripts/sync-template.sh` (§7.6) — releases never push
   workflow changes into consumer repos.

## Versioning

- Semver (`vX.Y.Z`).
- `vX.Y.Z` tag → image `:vX.Y.Z` and `:latest`.
- Engine changes ride `:latest` automatically; shell changes are opt-in via
  `sync-template.sh` (S5).
- Pre-release verification (before the first public release): the repo is
  private, so `raw.githubusercontent.com`/`codeload` tag URLs 404 and the
  `curl | sh` one-liner can't be live-tested. Use local
  `sh scripts/install.sh` runs and `JOBGITOPS_TAG=main` for `sync-template.sh`
  (see [scripts/e2e.md](scripts/e2e.md) pre-release note).
