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
- **No bump step needed.** Users run the installer using npx:

  ```bash
  npx jobgitops-installer
  ```

  Releases publish the npm package only when files under `installer/` changed
  since the previous release tag (see below). Users can pin a template version
  using the `--tag <tag>` argument.

## npm publishing

The `publish-npm` job in `.github/workflows/release-on-merge.yml` runs after a
release tag is created, but only when `git diff` between the previous `v*`
tag and HEAD touches `installer/`. Template/shell-plane-only releases skip it.

The job authenticates via **npm trusted publishing (OIDC)** — no `NPM_TOKEN`
secret is stored. The registry verifies the OIDC token's repo
(`menil/JobGitOps`) and workflow filename (`release-on-merge.yml`) against the
package's trusted-publisher config, then generates a provenance attestation
automatically.

One-time setup:

1. Create an [npmjs.com](https://www.npmjs.com) account with 2FA enabled.
2. Bootstrap the package with one manual publish (npm requires it to exist
   before a trusted publisher can be attached):

   ```bash
   cd installer && npm run build && npm login && npm publish --access public
   ```

   **Publish the version the release line actually computes, never the
   `package.json` placeholder** — npm versions are immutable. A higher
   bootstrap version (e.g. `1.0.0` when tags are at `v0.21.x`) squats that
   slot forever, feeds the bogus build to `^1` resolvers, and — because npm
   refuses implicit `latest` downgrades — permanently blocks every lower
   auto-computed version from taking over `latest` until something higher
   publishes. Recovery is *not* unpublishing: removing a package's last
   version locks the name for 24h (this repo once needed a manual bridge
   tag to escape exactly that trap). The placeholder stays `0.0.0` so an
   accidental unstamped publish can never outrank real releases, and
   `prepublishOnly` rejects it outright before anything ships.

3. On npmjs.com → package → **Settings → Trusted Publisher**, add:
   GitHub Actions · `menil/JobGitOps` (match GitHub's canonical casing —
   npm compares it strictly against the OIDC token claim) · workflow
   filename `release-on-merge.yml` · no environment. Then enable "require
   trusted publishing only" so stolen tokens cannot publish to the package.

Requirements already handled in the workflow: `id-token: write` permission and
Node 24 (ships npm ≥ 11.5.1, which implements the OIDC exchange — npm 10 on
Node 22 fails with a misleading ENEEDAUTH).

The job stamps the computed release version into `installer/package.json`
(`npm version ${VERSION#v} --no-git-tag-version`) before publishing, so
registry versions always match GitHub tags. If the job fails after the tag was
cut, republish manually from the tagged commit — do not cut a new version:

```bash
git checkout vX.Y.Z && cd installer && npm ci && \
  npm version X.Y.Z --no-git-tag-version && npm publish --access public
```

(Manual republish uses your own npm login and must omit `--provenance` — that
flag requires CI OIDC; trusted publishing only governs CI publishes.)

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
GEMINI_API_KEY=... npx --prefix installer tsx installer/src/index.ts jobgitops-e2e --yes --tag main
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
