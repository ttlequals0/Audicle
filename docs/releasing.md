# Releasing

Audicle ships one channel. Every release publishes four stack images under its version tag, and `latest` follows the newest release that has merged to main.

Historically this was not done consistently: the changelog has far more versions than the repo has tags, and no version was ever published as a GitHub release. `scripts/publish_release.sh` exists so the tag and the release page come from one command instead of memory.

| Docker tags | What they are |
|---|---|
| `<version>` | An immutable release: `ttlequals0/audicle`, `ttlequals0/audicle-tts`, `ttlequals0/audicle-render`, and `ttlequals0/audicle-render-egress` all carry it |
| `<version>-cpu` | The CPU-only wrapper build (`ttlequals0/audicle-tts` only), for deployments without a CUDA GPU |
| `latest` | The newest released version on main; repointed after merge, never from an unmerged branch |

Deployments should pin `<version>` (the stack passes it as `BUILD_VERSION`), so repointing `latest` never changes a running deployment.

## Versioning

The repo-root `VERSION` file is the single source of truth and the only file a human edits. Bump it, then propagate:

```bash
echo "X.Y.Z" > VERSION
uv run python scripts/sync_version.py        # writes the three pyprojects
(cd tts-wrapper && uv lock) && (cd render && uv lock)
uv run python scripts/sync_version.py --check  # a test fails on drift too
```

Every release gets a CHANGELOG.md section. If a branch bumps the version more than once before merging, each bump keeps its own section so nothing ships undocumented.

## Per-release flow

1. Gate locally with all three test suites, `uv run ruff check`, and `cd frontend && npm run build`. The suites are `uv run pytest`, `cd tts-wrapper && uv run pytest`, and `cd render && uv run pytest`. Run `uv run python scripts/dump_openapi.py` after API changes. CI repeats the tests and lint, then builds the app and renderer egress proxy. CodeQL and dependency review run separately.
2. Check disk before building. Run `df -h /` and `docker system df`. Prune only when the reported usage requires it.
3. Build all four stack images with `--pull`, sequentially:

   ```bash
   docker build --pull --platform linux/amd64 -t ttlequals0/audicle:X.Y.Z .
   docker build --pull --platform linux/amd64 --build-arg WRAPPER_VERSION=$(cat VERSION) -t ttlequals0/audicle-tts:X.Y.Z tts-wrapper/
   docker build --pull --platform linux/amd64 --build-arg WRAPPER_VERSION=$(cat VERSION) -t ttlequals0/audicle-tts:X.Y.Z-cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
   docker build --pull --platform linux/amd64 -t ttlequals0/audicle-render:X.Y.Z render/
   docker build --pull --platform linux/amd64 -t ttlequals0/audicle-render-egress:X.Y.Z render/egress-proxy/
   ```

   The wrapper builds need the version passed in: their build context is `tts-wrapper/`, which cannot read the repo-root `VERSION`, and without the arg the wrapper reports 0.0.0 in `/health/ready` (the 0.56.0 release shipped that way for about half an hour before being rebuilt).
   The GPU wrapper uses CUDA 12.6 wheels. Confirm the deployment host driver
   exposes CUDA 12.6 or newer before promoting the GPU tag.

   Never retag an old build as a new version: retagging freezes the apt-upgrade security layer.
4. Run the CVE gate. `scripts/trivy_gate.sh X.Y.Z` scans all five tags (the four stack images plus the `-cpu` wrapper) with the correct per-image ignorefile. Read the output before treating a failure as a CVE because Trivy also returns a failure for scan errors. This gate runs locally at release time.
5. Smoke the images as containers, not just the code. Run the app image and hit `/health/live`; confirm `import main` works inside the wrapper image. Tests import from the source tree, so a file missing from a Dockerfile COPY line only surfaces here (0.55.0 shipped that way and could not start; a wrapper test now guards the COPY list, but the principle stands for every image).
6. Push the version tags for all four stack images, plus `ttlequals0/audicle-tts:X.Y.Z-cpu`. Verify the manifests exist on Docker Hub before deploying. `publish_release.sh` checks the four stack images; the CPU tag is not a stack dependency.
7. Deploy by updating the stack's `BUILD_VERSION` to X.Y.Z. Verify the version through `GET /health/live`, podcast delivery through `/health/ready`, and selected processing dependencies through `/health/ingestion`.
8. Merge the release PR to main, then publish from up-to-date main:

   ```bash
   git checkout main && git pull --ff-only
   scripts/publish_release.sh X.Y.Z --dry-run   # preview the notes
   scripts/publish_release.sh X.Y.Z
   ```

   The script refuses to run off main, on a dirty tree, behind origin, when
   `VERSION` disagrees with the argument, when the tag already exists, or when
   the four stack images are not yet on Docker Hub. It creates the annotated tag and
   a GitHub release whose notes come from CHANGELOG.md, rolling up every section
   since the previous tag so a branch that bumped the version more than once
   publishes all of them.
9. Repoint `latest` for all four stack images to the just-merged version and push. This is the last step because `latest` represents main.

## Rolling back

Previous version tags stay on Docker Hub. Point the stack's `BUILD_VERSION` back at the last good version; the [runbook](DEPLOYMENT.md#rolling-back) covers it.

## Housekeeping

Keep the deployed version and one rollback candidate locally; everything older remains on Docker Hub. Run an unfiltered `trivy image --ignorefile /dev/null` scan for each image during releases, then remove ignore entries for vulnerabilities that are now fixed.

[< Docs index](README.md)
