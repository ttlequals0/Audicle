# Releasing

Audicle ships one channel. Every release publishes three images under its version tag, and `latest` follows the newest release that has merged to main.

Historically this was not done consistently: the changelog has far more versions than the repo has tags, and no version was ever published as a GitHub release. `scripts/publish_release.sh` exists so the tag and the release page come from one command instead of memory.

| Docker tags | What they are |
|---|---|
| `<version>` | An immutable release: `ttlequals0/audicle`, `ttlequals0/audicle-tts`, `ttlequals0/audicle-render` all carry it |
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

1. Gate locally: all three test suites (`uv run pytest`, `cd tts-wrapper && uv run pytest`, `cd render && uv run pytest`), `uv run ruff check`, `cd frontend && npm run build`, and `uv run python scripts/dump_openapi.py` if the API changed. CI runs CodeQL and dependency review; it does not run the tests, so the local gate is the gate.
2. Check disk before building. A full three-image build wants roughly 30 GB of headroom and dies at `exporting layers` when the root filesystem is near full. `docker builder prune -af` first if tight.
3. Build all three images with `--pull`, sequentially (parallel builds thrash disk I/O on a single host):

   ```bash
   docker build --pull --platform linux/amd64 -t ttlequals0/audicle:X.Y.Z .
   docker build --pull --platform linux/amd64 --build-arg WRAPPER_VERSION=$(cat VERSION) -t ttlequals0/audicle-tts:X.Y.Z tts-wrapper/
   docker build --pull --platform linux/amd64 --build-arg WRAPPER_VERSION=$(cat VERSION) -t ttlequals0/audicle-tts:X.Y.Z-cpu -f tts-wrapper/Dockerfile.cpu tts-wrapper/
   docker build --pull --platform linux/amd64 -t ttlequals0/audicle-render:X.Y.Z render/
   ```

   The wrapper builds need the version passed in: their build context is `tts-wrapper/`, which cannot read the repo-root `VERSION`, and without the arg the wrapper reports 0.0.0 in `/health/ready` (the 0.56.0 release shipped that way for about half an hour before being rebuilt).

   Never retag an old build as a new version: retagging freezes the apt-upgrade security layer.
4. Run the CVE gate. `scripts/trivy_gate.sh X.Y.Z` scans all four tags (the three release images plus the `-cpu` wrapper) with the correct per-image ignorefile. A FAILED that is really a trivy cache-lock or layer-analysis timeout looks identical to a finding at a glance; read the output before treating it as a CVE. Nothing in CI enforces this, which is exactly why it must run.
5. Smoke the images as containers, not just the code. Run the app image and hit `/health/live`; confirm `import main` works inside the wrapper image. Tests import from the source tree, so a file missing from a Dockerfile COPY line only surfaces here (0.55.0 shipped that way and could not start; a wrapper test now guards the COPY list, but the principle stands for every image).
6. Push the version tags for all three images, plus `ttlequals0/audicle-tts:X.Y.Z-cpu`. Verify the manifests exist on Docker Hub before deploying, since the stack pins `BUILD_VERSION` for all of them. (`publish_release.sh` checks only the three GPU-stack images; the `-cpu` tag is not a deploy dependency, so forgetting it breaks CPU users, not the release.)
7. Deploy by updating the stack's `BUILD_VERSION` to X.Y.Z, then verify `GET /health/live` reports the new version and `GET /health/ready` shows every component healthy.
8. Merge the release PR to main, then publish from up-to-date main:

   ```bash
   git checkout main && git pull --ff-only
   scripts/publish_release.sh X.Y.Z --dry-run   # preview the notes
   scripts/publish_release.sh X.Y.Z
   ```

   The script refuses to run off main, on a dirty tree, behind origin, when
   `VERSION` disagrees with the argument, when the tag already exists, or when
   the three images are not yet on Docker Hub. It creates the annotated tag and
   a GitHub release whose notes come from CHANGELOG.md, rolling up every section
   since the previous tag so a branch that bumped the version more than once
   publishes all of them.
9. Repoint `latest` for all three images to the just-merged version and push. This is deliberately the last step: `latest` represents main, so it never moves from an unmerged branch.

## Rolling back

Previous version tags stay on Docker Hub. Point the stack's `BUILD_VERSION` back at the last good version; the [runbook](DEPLOYMENT.md#rolling-back) covers it.

## Housekeeping

Old local images accumulate roughly 18 GB per release across the three tags. Keep the deployed version and one rollback candidate locally; everything older is still on Docker Hub. Periodically (each release is fine) run an unfiltered `trivy image --ignorefile /dev/null` scan to catch ignored CVEs that have since become fixable, and prune the ignorefiles accordingly.

[< Docs index](README.md)
