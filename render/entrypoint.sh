#!/bin/sh
set -e

# Headful Camoufox needs an X display. Start Xvfb in the background, point DISPLAY
# at it, then exec uvicorn so it becomes PID 1 and handles signals. xvfb-run as
# PID 1 does not reliably run its command in a container (it traps signals as a
# shell script and the command never starts), so we drive Xvfb directly.
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
export DISPLAY=:99

exec uvicorn main:create_app --factory --host 0.0.0.0 --port 8000 --workers 1
