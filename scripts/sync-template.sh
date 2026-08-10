#!/bin/sh
# JobGitOps sync-template (spec: specs/bootstrap-installer.md §7.6).
#
# Pulls the shell-plane files (.github/labels.yml + the six runtime-core
# workflows) from the latest JobGitOps release into a target repo and opens a
# PR. Manual and optional: workflow files are the one thing a user can't tune
# in their config, but pulling them is a decision, not a background job.
# Never auto-merges and never touches config/, resumes/, status/, or README.md.
# Every mutating step is echoed and aborts on failure; --dry-run prints the
# commands without running them. The pinned release tag is resolved from the
# latest release (override with $JOBGITOPS_TAG for pre-release testing).

set -u

# ---------------------------------------------------------------------------
# Defaults / interface
# ---------------------------------------------------------------------------

TARGET=""
TOKEN=""
HAS_TOKEN="0"
DRY_RUN="0"
TMPDIR=""

# Never let git fall back to an interactive credential prompt inside a piped
# script; a missing helper must fail fast with an actionable message.
export GIT_TERMINAL_PROMPT=0

usage() {
    cat >&2 <<'EOF'
Usage: sync-template.sh <OWNER>/<REPO> [options]

  <OWNER>/<REPO>   target repository to sync .github/ into
  --token TOKEN    PAT fallback when gh is installed but not
                   auth-configured (or $GH_TOKEN)
  --dry-run        print every command, make no changes
  -h, --help       show this help

Pulls .github/labels.yml + the six runtime-core workflows from the latest
JobGitOps release onto sync/upstream-template and opens a PR (changed files +
release link). No diff -> exit 0 with no PR. Never auto-merges.
EOF
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --token)
            TOKEN="${2:-}"
            shift 2
            ;;
        --dry-run)
            DRY_RUN="1"
            shift
            ;;
        --help | -h)
            usage
            ;;
        -*)
            echo "unknown option: $1" >&2
            usage
            ;;
        *)
            if [ -z "$TARGET" ]; then
                TARGET="$1"
            else
                echo "unexpected argument: $1" >&2
                usage
            fi
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    printf '%s\n' "$*" >&2
}

cleanup() {
    if [ -n "$TMPDIR" ] && [ -d "$TMPDIR" ]; then
        rm -rf "$TMPDIR"
    fi
}

die() {
    printf 'ERROR: %s\n' "$1" >&2
    cleanup
    exit 1
}

# Echo the command, then run it; abort with the command and exit code on
# failure. Under --dry-run the command is echoed and skipped.
run() {
    log "> $*"
    [ "$DRY_RUN" = "1" ] && return 0
    "$@"
    rc=$?
    [ "$rc" -eq 0 ] || die "command failed (exit $rc): $*"
}

# Exactly one '/', both halves non-empty and made of [A-Za-z0-9._-].
validate_target() {
    OWNER_PART="${1%/*}"
    REPO_PART="${1#*/}"
    [ "$1" = "$OWNER_PART/$REPO_PART" ] || return 1
    [ -n "$OWNER_PART" ] || return 1
    [ -n "$REPO_PART" ] || return 1
    case "$OWNER_PART" in *[!A-Za-z0-9._-]* | -* | *..*) return 1 ;; esac
    case "$REPO_PART" in *[!A-Za-z0-9._-]* | -* | *..*) return 1 ;; esac
    return 0
}

# ---------------------------------------------------------------------------
# Preflight (§7.6): tools, auth, target
# ---------------------------------------------------------------------------

command -v gh >/dev/null 2>&1 ||
    die "GitHub CLI not found — install it (https://cli.github.com/) or use --token with a PAT."
command -v git >/dev/null 2>&1 || die "git not found."
command -v curl >/dev/null 2>&1 || die "curl not found."
command -v tar >/dev/null 2>&1 || die "tar not found."

[ -n "$TARGET" ] ||
    die "target repository is required — pass it as <OWNER>/<REPO> (e.g. acme/job-search)."
validate_target "$TARGET" ||
    die "invalid target '$TARGET' — expected <OWNER>/<REPO> (letters, digits, '.', '_', '-' only)."

# PAT fallback: --token or $GH_TOKEN drives every gh call; otherwise the
# script relies on gh's own configured auth.
if [ -n "$TOKEN" ] || [ -n "${GH_TOKEN:-}" ]; then
    HAS_TOKEN="1"
    export GH_TOKEN="${TOKEN:-$GH_TOKEN}"
    gh api user --jq .login >/dev/null 2>&1 ||
        die "token rejected by GitHub — check --token / \$GH_TOKEN."
else
    gh auth status >/dev/null 2>&1 ||
        die "not authenticated with gh — run 'gh auth login' (or pass --token)."
fi

USER_LOGIN="$(gh api user --jq .login 2>/dev/null)"
[ -n "$USER_LOGIN" ] || die "could not determine your GitHub login."

# contents:write + pull-requests:write (§7.6) both live inside the classic
# 'repo' scope. X-OAuth-Scopes is only returned for classic PAT auth; when it
# is absent (browser login / fine-grained token) we cannot introspect scopes,
# so warn and continue — the abort-on-failure backstop catches the gap.
SCOPES="$(gh api user --include 2>/dev/null |
    sed -n 's/^[Xx]-[Oo][Aa]uth-[Ss]copes:[[:space:]]*//p' | tr -d '\r ')"
if [ -n "$SCOPES" ]; then
    case ",$SCOPES," in
        *",repo,"*) ;;
        *) die "token is missing the 'repo' scope — add it: gh auth refresh -s repo (or use a PAT with repo)." ;;
    esac
else
    log "note: could not read token scopes (browser login or fine-grained token); continuing — any permission failure will abort."
fi

# ---------------------------------------------------------------------------
# Resolve tag + fetch the shell plane (§7.6.1-2)
# ---------------------------------------------------------------------------

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/jobgitops-sync.XXXXXX")" ||
    die "could not create a temp working directory."
# Interrupts exit immediately; the EXIT trap then removes the temp tree.
trap 'cleanup' EXIT
trap 'exit 1' HUP INT TERM

if [ -n "${JOBGITOPS_TAG:-}" ]; then
    TAG="${JOBGITOPS_TAG}"
else
    TAG="$(gh api repos/menil/jobgitops/releases/latest --jq .tag_name 2>/dev/null)"
    [ -n "$TAG" ] ||
        die "could not resolve the latest JobGitOps release (none published yet). Set JOBGITOPS_TAG=<tag-or-branch> to test a specific ref."
fi

TARBALL="$TMPDIR/jobgitops-$TAG.tgz"
download_tarball() {
    log "> curl -fsSL https://codeload.github.com/menil/jobgitops/tar.gz/refs/tags/$TAG -o $TARBALL"
    [ "$DRY_RUN" = "1" ] && return 0
    if ! curl -fsSL "https://codeload.github.com/menil/jobgitops/tar.gz/refs/tags/$TAG" -o "$TARBALL"; then
        # No such tag (pre-release, before the first release exists) — retry
        # the ref as a branch so JOBGITOPS_TAG=main works. Inert once releases
        # exist: a release tag is always a real git tag.
        log "> curl -fsSL https://codeload.github.com/menil/jobgitops/tar.gz/refs/heads/$TAG -o $TARBALL"
        curl -fsSL "https://codeload.github.com/menil/jobgitops/tar.gz/refs/heads/$TAG" -o "$TARBALL" ||
            die "failed to download the JobGitOps tarball for '$TAG' (exit $?)."
    fi
}
download_tarball
run mkdir -p "$TMPDIR/tree"
run tar -xzf "$TARBALL" -C "$TMPDIR/tree"

# codeload tarballs extract to a top-level dir named <repo>-<tag>.
SRC="$TMPDIR/tree/jobgitops-$TAG"

REPO="$TMPDIR/repo"
run gh repo clone "$TARGET" "$REPO" -- --quiet

DEFAULT="$(gh api "repos/$TARGET" --jq .default_branch 2>/dev/null)"
[ -n "$DEFAULT" ] || die "could not determine the default branch of '$TARGET'."
run git -C "$REPO" checkout "$DEFAULT"

# Shell-plane allowlist (spec §3) — labels + the six runtime-core workflows.
# Only these are ever overwritten; maintainer workflows and the user's own
# config/resumes/status/README are never touched. Copied verbatim, the same
# way install.sh assembled them, so the two never drift.
SHELL_PLANE=".github/labels.yml \
.github/workflows/scrape-jobs.yml \
.github/workflows/triage-issue.yml \
.github/workflows/respond-issue.yml \
.github/workflows/status-transition.yml \
.github/workflows/project-status-sync.yml \
.github/workflows/sync-labels.yml"

for F in $SHELL_PLANE; do
    run cp -f "$SRC/$F" "$REPO/$F"
done

# No diff against the local .github/ -> nothing to sync: exit 0 with no PR
# (§7.6.3). Under --dry-run there is no clone to diff against, so report the
# would-be outcome instead.
CHANGES="$(git -C "$REPO" status --porcelain -- .github/ 2>/dev/null || true)"
if [ "$DRY_RUN" = "1" ]; then
    cat >&2 <<EOF
(dry-run) would sync .github/ in $TARGET from JobGitOps $TAG; a diff would
be committed on sync/upstream-template and opened as a PR against $DEFAULT.
EOF
    exit 0
fi
[ -z "$CHANGES" ] && exit 0

# ---------------------------------------------------------------------------
# Diff present — open sync/upstream-template from the current default HEAD and
# commit only the shell-plane files (§7.6.4)
# ---------------------------------------------------------------------------

# Local identity for the sync commit, scoped to the clone, never global.
run git -C "$REPO" config user.name "$USER_LOGIN"
run git -C "$REPO" config user.email "$USER_LOGIN@users.noreply.github.com"

# -B resets an existing sync branch to the fresh default HEAD, so repeated
# runs stay rebased instead of stacking stale diffs.
run git -C "$REPO" checkout -B sync/upstream-template

# Stage only the allowlisted paths — a user's own workflow edits under
# .github/ are never swept into the sync commit.
run git -C "$REPO" add -- $SHELL_PLANE
run git -C "$REPO" commit -m "chore: sync .github/ from JobGitOps $TAG"

# When a PAT is in play, git cannot rely on gh's credential helper (there is
# no gh auth); feed it the token via an askpass shim that reads $JGO_GIT_TOKEN
# from the environment so the secret never touches disk.
if [ "$HAS_TOKEN" = "1" ]; then
    ASKPASS="$TMPDIR/askpass.sh"
    {
        echo '#!/bin/sh'
        echo 'case "$1" in'
        echo '  *[Uu]sername*) printf "%s\\n" "${JGO_GIT_USERNAME:-oauth2}" ;;'
        echo '  *) printf "%s\\n" "$JGO_GIT_TOKEN" ;;'
        echo 'esac'
    } >"$ASKPASS"
    chmod +x "$ASKPASS"
    export JGO_GIT_TOKEN="${TOKEN:-$GH_TOKEN}"
fi
GIT_CRED=""
[ "$HAS_TOKEN" = "1" ] && GIT_CRED="-c credential.helper= -c credential.askpass=$ASKPASS"

push_branch() {
    # This is exclusively the automation's own PR branch: fetch the remote's
    # current value so the update is a --force-with-lease with an explicit
    # expected SHA (never a bare --force), and plain push when it's new.
    EXPECTED="$(git -C "$REPO" ls-remote origin sync/upstream-template 2>/dev/null | awk '{print $1}')"
    if [ -z "$EXPECTED" ]; then
        log "> git -C $REPO $GIT_CRED push -u origin sync/upstream-template"
        [ "$DRY_RUN" = "1" ] && return 0
        git -C "$REPO" $GIT_CRED push -u origin sync/upstream-template
        rc=$?
    else
        log "> git -C $REPO $GIT_CRED push --force-with-lease=sync/upstream-template:$EXPECTED origin sync/upstream-template"
        [ "$DRY_RUN" = "1" ] && return 0
        git -C "$REPO" $GIT_CRED push --force-with-lease="sync/upstream-template:$EXPECTED" origin sync/upstream-template
        rc=$?
    fi
    [ "$rc" -eq 0 ] || die "git push failed (exit $rc)."
}
push_branch

# Changed files for the PR body — always exactly the last (sync) commit.
CHANGED="$(git -C "$REPO" diff --name-status HEAD~1..HEAD)"
RELEASE_URL="https://github.com/menil/jobgitops/releases/tag/$TAG"
BODYFILE="$TMPDIR/pr-body.md"
{
    printf 'Pulled from [JobGitOps %s](%s):\n\n' "$TAG" "$RELEASE_URL"
    printf '%s\n' "$CHANGED"
    printf '\nOpened by scripts/sync-template.sh (§7.6) — review and merge; never auto-merged.\n'
} >"$BODYFILE"

open_pr() {
    EXISTING="$(gh pr list --repo "$TARGET" --head sync/upstream-template --state open --json number --jq '.[0].number' 2>/dev/null)"
    if [ -n "$EXISTING" ]; then
        log "PR already open for sync/upstream-template (#$EXISTING) — the push above updated it."
        return 0
    fi
    log "> gh pr create --repo $TARGET --base $DEFAULT --head sync/upstream-template"
    [ "$DRY_RUN" = "1" ] && return 0
    gh pr create --repo "$TARGET" --base "$DEFAULT" --head sync/upstream-template \
        --title "chore: sync .github/ from JobGitOps $TAG" \
        --body-file "$BODYFILE"
    rc=$?
    [ "$rc" -eq 0 ] || die "PR creation failed (exit $rc)."
}
open_pr

PR_URL="$(gh pr list --repo "$TARGET" --head sync/upstream-template --state open --json url --jq '.[0].url' 2>/dev/null)"
cat >&2 <<EOF

Synced .github/ in $TARGET from JobGitOps $TAG.
Branch: sync/upstream-template${PR_URL:+" — $PR_URL"}
Never auto-merged — review and merge manually.
EOF
