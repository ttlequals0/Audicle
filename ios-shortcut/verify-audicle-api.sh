#!/usr/bin/env bash
# Prove the server-side auth + submit flow the iOS shortcut depends on:
#   POST /api/v1/auth/login  -> sets audicle_session + audicle_csrf cookies, returns csrf_token
#   POST /api/v1/submit       -> with the session cookie + X-CSRF-Token header
#
# Run this against your live server BEFORE blaming the shortcut. If this passes
# but the shortcut fails, the problem is iOS Shortcuts cookie handling, not the
# server or the request shape.
#
# Usage:
#   AUDICLE_SERVER=https://audicle.example.com \
#   AUDICLE_PASSWORD=hunter2 \
#   AUDICLE_TEST_URL=https://example.com/some-article \
#   ./verify-audicle-api.sh
#
# Or pass flags: ./verify-audicle-api.sh -s URL -p PASS -u ARTICLE_URL
set -euo pipefail

SERVER="${AUDICLE_SERVER:-}"
PASSWORD="${AUDICLE_PASSWORD:-}"
TEST_URL="${AUDICLE_TEST_URL:-https://example.com/}"

while getopts "s:p:u:" opt; do
  case "$opt" in
    s) SERVER="$OPTARG" ;;
    p) PASSWORD="$OPTARG" ;;
    u) TEST_URL="$OPTARG" ;;
    *) echo "usage: $0 -s SERVER -p PASSWORD [-u ARTICLE_URL]" >&2; exit 2 ;;
  esac
done

if [[ -z "$SERVER" || -z "$PASSWORD" ]]; then
  echo "error: SERVER and PASSWORD are required (env AUDICLE_SERVER/AUDICLE_PASSWORD or -s/-p)" >&2
  exit 2
fi
SERVER="${SERVER%/}"  # strip trailing slash

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

json_get() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get(sys.argv[1],"") if isinstance(d,dict) else "")' "$1"; }

echo "== 1. login =="
LOGIN_BODY="$(curl -sS -c "$JAR" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$PASSWORD")" \
  "$SERVER/api/v1/auth/login")"
echo "  response: $LOGIN_BODY"

CSRF="$(printf '%s' "$LOGIN_BODY" | json_get csrf_token)"
if [[ -z "$CSRF" ]]; then
  echo "  FAIL: no csrf_token in login response (wrong password, or server not in password mode)" >&2
  exit 1
fi
echo "  csrf_token: ${CSRF:0:12}... (ok)"
echo "  cookies stored:"; grep -E 'audicle_(session|csrf)' "$JAR" | awk '{print "    "$6}' || true

echo "== 2. submit =="
SUBMIT_RESP="$(curl -sS -b "$JAR" -w $'\n%{http_code}' \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"url": sys.argv[1]}))' "$TEST_URL")" \
  "$SERVER/api/v1/submit")"
SUBMIT_CODE="$(printf '%s' "$SUBMIT_RESP" | tail -n1)"
SUBMIT_BODY="$(printf '%s' "$SUBMIT_RESP" | sed '$d')"
echo "  http $SUBMIT_CODE"
echo "  response: $SUBMIT_BODY"

JOB_ID="$(printf '%s' "$SUBMIT_BODY" | json_get job_id)"
if [[ "$SUBMIT_CODE" == "201" && -n "$JOB_ID" ]]; then
  echo "PASS: submitted, job_id=$JOB_ID"
elif [[ "$SUBMIT_CODE" == "409" ]]; then
  echo "PASS (auth ok): 409 means the URL already has an episode -- auth + CSRF worked."
elif [[ "$SUBMIT_CODE" == "401" || "$SUBMIT_CODE" == "403" ]]; then
  echo "FAIL: $SUBMIT_CODE -- session cookie or CSRF header rejected." >&2
  exit 1
else
  echo "FAIL: unexpected status $SUBMIT_CODE" >&2
  exit 1
fi
