#!/usr/bin/env bash
# Tag a shipped version and publish it as a GitHub release.
#
# Run on up-to-date main immediately after the release PR is merged, once the
# images for that version are on Docker Hub. The notes come from CHANGELOG.md,
# so the release page and the changelog cannot disagree.
#
# Usage: scripts/publish_release.sh <version> [--dry-run]
set -euo pipefail

REPO="ttlequals0/Audicle"
VERSION="${1:?usage: publish_release.sh <version> [--dry-run]}"
DRY_RUN="${2:-}"

if [ -n "$DRY_RUN" ] && [ "$DRY_RUN" != "--dry-run" ]; then
  echo "unknown argument: $DRY_RUN (expected --dry-run)" >&2; exit 1
fi
[ "$#" -le 2 ] || { echo "too many arguments" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

# --- guards ----------------------------------------------------------------
# A tag is effectively permanent once pushed, so every precondition is checked
# before anything is created.

BRANCH=$(git branch --show-current)
[ "$BRANCH" = "main" ] || { echo "must run on main (current: $BRANCH)" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "working tree not clean" >&2; exit 1; }

git fetch origin main --quiet
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
  || { echo "HEAD is not origin/main; pull first" >&2; exit 1; }

FILE_VERSION=$(tr -d '[:space:]' < VERSION)
[ "$FILE_VERSION" = "$VERSION" ] \
  || { echo "VERSION file has $FILE_VERSION, expected $VERSION" >&2; exit 1; }

git rev-parse "v$VERSION" >/dev/null 2>&1 \
  && { echo "tag v$VERSION already exists" >&2; exit 1; }

# The images must exist before the release points at them. Skipped on a dry run
# so the notes can be previewed before anything is built.
if [ "$DRY_RUN" != "--dry-run" ]; then
  for image in audicle audicle-tts audicle-render; do
    docker manifest inspect "ttlequals0/${image}:${VERSION}" >/dev/null 2>&1 \
      || { echo "ttlequals0/${image}:${VERSION} is not on Docker Hub; push the images first" >&2; exit 1; }
  done
fi

# --- notes -----------------------------------------------------------------
# Roll up every changelog section since the previous tag, so a branch that
# bumped the version several times before merging publishes all of them rather
# than shipping the earlier ones undocumented.

PREV_TAG=$(git describe --tags --abbrev=0 --match 'v[0-9]*' HEAD 2>/dev/null || true)
if [ -n "$PREV_TAG" ]; then
  NOTES=$(python3 scripts/changelog_section.py "$VERSION" --rollup-since "${PREV_TAG#v}")
else
  NOTES=$(python3 scripts/changelog_section.py "$VERSION")
fi
[ -n "$NOTES" ] || { echo "no changelog notes found for $VERSION" >&2; exit 1; }

# --- publish ---------------------------------------------------------------

run git tag -a "v$VERSION" -m "Release $VERSION"
run git push origin "v$VERSION"

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "DRY-RUN: gh release create v$VERSION --repo $REPO --title $VERSION --notes <below>"
  echo "--- notes ---"
  echo "$NOTES"
else
  gh release create "v$VERSION" --repo "$REPO" --title "$VERSION" --notes "$NOTES"
  echo "Published release v$VERSION."
fi
