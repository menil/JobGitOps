#!/bin/sh
# Print the next semantic version tag (e.g. v1.2.3) derived from conventional
# commits since the last release tag — or nothing when there is nothing to
# release (only docs/chore/refactor commits). The auto-release workflow on
# merge to main consumes stdout as the next version.
#
# Bump rules (conventional commits):
#   BREAKING CHANGE (body) or feat!:/fix(scope)!:  -> major (minor pre-1.0)
#   feat                                            -> minor
#   fix                                             -> patch
#   anything else                                   -> no release
#
# Needs full history and tags, so CI checks out with fetch-depth: 0.

set -u

# Newest v* tag, sorted semver-aware (v1.10.0 > v1.9.0). -v:refname reverses
# the version:refname ordering so the first line is the highest version.
LAST_TAG="$(git tag -l 'v[0-9]*' --sort=-v:refname | head -n 1)"

MAJOR="0"
MINOR="0"
PATCH="0"
if [ -n "$LAST_TAG" ]; then
    VERSION="${LAST_TAG#v}"
    MAJOR="${VERSION%%.*}"
    REST="${VERSION#*.}"
    MINOR="${REST%%.*}"
    REST="${REST#*.}"
    PATCH="${REST%%.*}"
fi

# Commits to consider: everything since the last tag; from the initial commit
# when there is no tag yet (rev-list --max-parents=1 walks the first-parent
# line, so the last line is the oldest commit).
if [ -n "$LAST_TAG" ]; then
    RANGE="$LAST_TAG..HEAD"
else
    FIRST="$(git rev-list --max-parents=1 HEAD | tail -n 1)"
    [ -n "$FIRST" ] || exit 0
    RANGE="$FIRST..HEAD"
fi

HAS_BREAKING="0"
HAS_FEAT="0"
HAS_FIX="0"

# Breaking changes: 'BREAKING CHANGE:' in the body, or '!' after the type or
# scope (feat!:, fix(scope)!:).
git log "$RANGE" --format=%B 2>/dev/null | grep -q 'BREAKING CHANGE' && HAS_BREAKING="1"
git log "$RANGE" --format=%s 2>/dev/null | grep -Eq '^[a-z]+(\([^)]*\))?!:' && HAS_BREAKING="1"

# Anchored so 'feature' / 'fixed' alone don't bump.
git log "$RANGE" --format=%s 2>/dev/null | grep -Eq '^feat([:(]|$)' && HAS_FEAT="1"
git log "$RANGE" --format=%s 2>/dev/null | grep -Eq '^fix([:(]|$)' && HAS_FIX="1"

if [ "$HAS_BREAKING" = "1" ]; then
    # Pre-1.0, breaking changes bump minor rather than major (semver §4).
    if [ "$MAJOR" = "0" ]; then
        MINOR=$((MINOR + 1))
    else
        MAJOR=$((MAJOR + 1))
        MINOR="0"
    fi
    PATCH="0"
elif [ "$HAS_FEAT" = "1" ]; then
    MINOR=$((MINOR + 1))
    PATCH="0"
elif [ "$HAS_FIX" = "1" ]; then
    PATCH=$((PATCH + 1))
else
    # Nothing user-facing changed since the last release — skip.
    exit 0
fi

printf 'v%s.%s.%s\n' "$MAJOR" "$MINOR" "$PATCH"
