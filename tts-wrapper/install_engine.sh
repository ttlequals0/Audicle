#!/bin/sh
# Install the Chatterbox TTS engine plus the optional ASR-verify backend from
# the reviewed lock, shared by Dockerfile and Dockerfile.cpu.
#
# Torch is preinstalled by both Dockerfiles from the required CUDA or CPU index.
set -eu

# Chatterbox declares Gradio for its standalone demo scripts, but the engine
# package never imports it. The uv lock excludes that unused dependency so the
# wrapper can run a patched Starlette. Torch is installed by the Dockerfile from
# the platform-specific CUDA or CPU index. Install Perth separately because it
# is pinned to an immutable Git commit and cannot use pip's hash mode.
uv export --locked --no-dev --extra chatterbox --extra whisper \
  --no-emit-project --no-emit-package torch --no-emit-package torchaudio \
  --output-file /tmp/requirements.txt
grep '^resemble-perth @ git+' /tmp/requirements.txt > /tmp/requirements-vcs.txt
[ "$(wc -l < /tmp/requirements-vcs.txt)" -eq 1 ]
grep -v '^resemble-perth @ git+' /tmp/requirements.txt > /tmp/requirements-hashed.txt
uv pip install --system --no-cache --no-deps --no-config -r /tmp/requirements-vcs.txt
uv pip install --system --no-cache --no-deps --require-hashes --no-config \
  -r /tmp/requirements-hashed.txt
uv pip install --system --no-cache --no-deps --no-config .
rm /tmp/requirements.txt /tmp/requirements-vcs.txt /tmp/requirements-hashed.txt
# Runtime installs use uv; remove the unused pip distribution and bundled copy.
uv pip uninstall --system pip
python - <<'PY'
import shutil
import sysconfig
from pathlib import Path

shutil.rmtree(Path(sysconfig.get_path("stdlib")) / "ensurepip")
PY
