#!/bin/sh
set -eu

exec python -I /app/listen.py tts-wrapper 8000 main:create_app --factory --workers 1 --timeout-keep-alive 300
