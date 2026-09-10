#!/bin/sh
# Install the Chatterbox TTS engine plus the optional ASR-verify backend from
# the reviewed lock, shared by Dockerfile and Dockerfile.cpu.
#
# Torch is preinstalled by both Dockerfiles from the required CUDA or CPU index.
set -eu

# Chatterbox declares Gradio for its standalone demo scripts, but the engine
# package never imports it. The uv lock excludes that unused dependency so the
# wrapper can run a patched Starlette. Torch is installed by the Dockerfile from
# the platform-specific CUDA or CPU index, so omit the lock's development CPU
# source and retain the already-installed matching build.
uv export --locked --no-dev --extra chatterbox --extra whisper \
  --no-hashes --no-emit-project --output-file /tmp/requirements.txt
sed -i '/^torch==/d; /^torchaudio==/d' /tmp/requirements.txt
uv pip install --system --no-cache -r /tmp/requirements.txt
uv pip install --system --no-cache --no-deps .
