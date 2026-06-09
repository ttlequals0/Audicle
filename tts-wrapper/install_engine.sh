#!/bin/sh
# Install the Chatterbox TTS engine plus the optional ASR-verify backend and the
# security patch upgrades, shared by Dockerfile and Dockerfile.cpu so the install
# logic lives in one place.
#
# chatterbox-tts pins transformers==5.2.0 itself, so no pre-pin is needed. torch is
# provided by the image (CUDA base or CPU pre-install); chatterbox-tts pins
# torch==2.6.0, satisfied by that build.
set -eu

pip install --no-cache-dir ".[chatterbox]"

# faster-whisper (CTranslate2) for the optional post-TTS ASR verification pass.
# Installed as its own step so it resolves against the already-installed engine;
# CTranslate2 is independent of the engine's torch stack. The image always ships
# it, but the model loads only when an operator sets WHISPER_ENABLED=true.
pip install --no-cache-dir ".[whisper]"

# Patch transitive deps the resolver pins to vulnerable versions. setuptools is
# capped <81: >=78.1.1 clears CVE-2025-47273, but setuptools 81 removed the
# bundled pkg_resources, which resemble-perth (chatterbox's watermarker) imports
# at load time -- without the cap the wrapper crash-loops on model load.
pip install --no-cache-dir -U \
  "urllib3>=2.7.0" "cryptography>=46.0.5" "pillow>=12.2.0" \
  "Brotli>=1.2.0" "setuptools>=78.1.1,<81" "wheel>=0.46.2"
