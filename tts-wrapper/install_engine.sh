#!/bin/sh
# Install exactly one TTS engine extra plus the security patch upgrades, shared by
# Dockerfile and Dockerfile.cpu so the engine-selection logic lives in one place.
#
# Usage: install_engine.sh <xtts|styletts2|chatterbox>
#
# Engines are MUTUALLY EXCLUSIVE: their transformers pins conflict. coqui-tts 0.24
# requires transformers>=4.43.0 unbounded but breaks on 5.x, so xtts/styletts2 get
# the 4.48.x pre-pin (>=4.48.0 clears CVE-2024-11392/-11393/-11394); chatterbox-tts
# pins transformers==5.2.0 itself, so it gets no pre-pin. torch is provided by the
# image (CUDA base or CPU pre-install); chatterbox-tts pins torch==2.6.0, satisfied
# by that build.
set -eu

backend="${1:?usage: install_engine.sh <xtts|styletts2|chatterbox>}"

case "$backend" in
  chatterbox)
    pip install --no-cache-dir ".[chatterbox]"
    ;;
  styletts2)
    pip install --no-cache-dir "transformers>=4.48.0,<4.49"
    pip install --no-cache-dir ".[styletts2]"
    ;;
  xtts)
    pip install --no-cache-dir "transformers>=4.48.0,<4.49"
    pip install --no-cache-dir ".[xtts]"
    ;;
  *)
    echo "unknown TTS_BACKEND: $backend" >&2
    exit 1
    ;;
esac

# Patch transitive deps the resolver pins to vulnerable versions. setuptools is
# capped <81: >=78.1.1 clears CVE-2025-47273, but setuptools 81 removed the
# bundled pkg_resources, which resemble-perth (chatterbox's watermarker) imports
# at load time -- without the cap the wrapper crash-loops on model load.
pip install --no-cache-dir -U \
  "urllib3>=2.7.0" "cryptography>=46.0.5" "pillow>=12.2.0" \
  "Brotli>=1.2.0" "setuptools>=78.1.1,<81" "wheel>=0.46.2"
