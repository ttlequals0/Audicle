#!/bin/sh
# trivy_gate.sh -- release-time CVE gate for the Audicle images: app, tts
# (GPU and -cpu tags), render.
#
# Usage: scripts/trivy_gate.sh <version>   (e.g. 0.47.0)
#
# Runs trivy HIGH/CRITICAL on each image with the correct per-image ignorefile:
#   app   -- .trivyignore only
#   tts   -- .trivyignore + .trivyignore.tts (concatenated)
#   render -- .trivyignore + .trivyignore.render (concatenated)
#
# Per-image concatenation exists because trivy's --ignorefile applies globally:
# a single shared file silently suppresses CVEs across images that do not trigger
# them, hiding real findings in future scans. Each image gets only the suppressions
# it genuinely needs.
#
# A new finding that survives after applying the correct ignorefile means a new
# decision is required -- do not add entries without documenting why and which image.
set -eu

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "Usage: $0 <version>" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHARED="$REPO_ROOT/.trivyignore"
IGNORE_TTS="$REPO_ROOT/.trivyignore.tts"
IGNORE_RENDER="$REPO_ROOT/.trivyignore.render"

TMP_TTS=$(mktemp)
TMP_RENDER=$(mktemp)

cleanup() {
    rm -f "$TMP_TTS" "$TMP_RENDER"
}
trap cleanup EXIT

cat "$SHARED" "$IGNORE_TTS" > "$TMP_TTS"
cat "$SHARED" "$IGNORE_RENDER" > "$TMP_RENDER"

FAILED=0

scan() {
    ref="$1"
    ignorefile="$2"
    tag="ttlequals0/${ref}"
    out="$(mktemp)"
    echo "Scanning $tag ..."
    # --timeout 30m: the default 5m dies mid-analysis inside the 10 GB tts
    # image ("semaphore acquire: context deadline exceeded"). Deliberately NOT
    # passing --scanners vuln to speed this up: that would skip the image-layer
    # secret scan, which also covers secrets arriving via build args or base
    # images that the pre-build repo scan never sees.
    if trivy image --timeout 30m --severity HIGH,CRITICAL --exit-code 1 --quiet \
            --ignorefile "$ignorefile" "$tag" >"$out" 2>&1; then
        cat "$out"
        echo "$tag CLEAN"
    # trivy exits 1 for findings AND for fatal errors (cache lock held by a
    # concurrent scan, layer-analysis timeout). Distinguish them so an infra
    # hiccup is not read as a CVE failure -- and is still a gate failure.
    elif grep -q "FATAL" "$out"; then
        cat "$out" >&2
        echo "$tag SCAN ERROR (not a CVE verdict; re-run)" >&2
        FAILED=1
    else
        cat "$out" >&2
        echo "$tag FAILED" >&2
        FAILED=1
    fi
    rm -f "$out"
}

# The -cpu wrapper shares the tts ignorefile: same codebase, only the torch
# wheels differ, and a CVE suppressed for one is a decision made for both.
scan "audicle:${VERSION}"         "$SHARED"
scan "audicle-tts:${VERSION}"     "$TMP_TTS"
scan "audicle-tts:${VERSION}-cpu" "$TMP_TTS"
scan "audicle-render:${VERSION}"  "$TMP_RENDER"

if [ "$FAILED" -ne 0 ]; then
    echo "One or more images failed the CVE gate." >&2
    exit 1
fi

echo "All images CLEAN."
