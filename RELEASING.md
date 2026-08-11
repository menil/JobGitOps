# Releasing JobGitOps

Releases are **automatic** — no manual tag cutting (spec
`specs/bootstrap-installer.md` §8).

## How it works

Every merge to `main` runs `.github/workflows/release-on-merge.yml`:

1. `scripts/bump-version.sh` computes the next `vX.Y.Z` from conventional
   commits since the last release tag:

   - `BREAKING CHANGE` (body) or `feat!:` → **major** (minor while pre-1.0)
   - `feat` → **minor**
   - `fix` → **patch**
   - docs/chore/refactor-only merges → **no release**

2. The workflow creates the release tag and a GitHub Release with
   auto-generated notes. The tag push triggers `build-runner.yml`, which
   builds and pushes the Docker image `:vX.Y.Z` and `:latest` to GHCR.

## Versioning

- Semver (`vX.Y.Z`).
- `vX.Y.Z` tag → image `:vX.Y.Z` and `:latest`.
- Engine changes ride `:latest` automatically; shell-plane changes are opt-in
  via `scripts/sync-template.sh` (§7.6) — releases never push workflow changes
  into consumer repos.
- **No bump step needed.** The install URL *is* the pin:

  ```
  https://raw.githubusercontent.com/menil/jobgitops/vX.Y.Z/scripts/install.sh
  ```

  Each release gets its own immutable URL; users pin a version by using the
  matching tag URL. Document the current recommended version in the README's
  Quick Start one-liner.

## One-time setup (first release only)

The repo and GHCR package must be public so users can install without
credentials:

```bash
gh repo edit menil/jobgitops --visibility public
gh api --method POST /user/packages/container/jobgitops/visibility \
  -f visibility=public
```

Verify the package flip with
`gh api /user/packages/container/jobgitops/visibility --jq .visibility` →
`public`. Until both are public, `codeload`/`raw.githubusercontent.com` return
404 and the `curl | sh` one-liner cannot be live-tested (pre-release note in
[scripts/e2e.md](scripts/e2e.md)).

## Dogfooding before the repo is public

Keep the source repo private for now and still run the installer yourself:

```bash
GEMINI_API_KEY=... JOBGITOPS_TAG=main sh scripts/install.sh jobgitops-e2e --yes
```

`JOBGITOPS_TAG=main` bypasses the release-tag lookup, and when the anonymous
codeload download fails, the installer falls back to the authenticated API
tarball (`gh api repos/menil/jobgitops/tarball/main`), which serves the private
repo using your `gh` auth. `scripts/sync-template.sh` does the same. Note: the
GHCR **package** must still be public for the consumer repo's workflows to
pull `ghcr.io/menil/jobgitops:latest` — flipping the package alone does not
expose the source repo.

## Verification

After each release: dry-run the installer, then run a live install on a
throwaway repo (see [scripts/e2e.md](scripts/e2e.md)) — static setup badge
renders, exactly one bootstrap scrape fires, and the user removes the badge.
