"""CSRF token helpers using the double-submit cookie pattern.

The login endpoint issues a token (cookie ``audicle_csrf`` + JSON response
field). The client echoes the token on every mutating request via the
``X-CSRF-Token`` header. The dependency in ``api.deps.require_csrf``
compares the header to the cookie under ``hmac.compare_digest`` so a
forged request from an attacker site (which can't read the cookie via
Same-Origin) is rejected even if the browser sends the session cookie
automatically.
"""

from __future__ import annotations

import hmac
import secrets

CSRF_COOKIE_NAME = "audicle_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
_TOKEN_BYTES = 32


def issue_token() -> str:
    """Return a fresh URL-safe CSRF token."""

    return secrets.token_urlsafe(_TOKEN_BYTES)


def verify_token(header_value: str | None, cookie_value: str | None) -> bool:
    """Constant-time compare ``header_value`` against ``cookie_value``.

    Returns False if either side is absent. The cookie is the
    server-issued source of truth; the header value is the echo that
    proves the client is same-origin (an attacker page cannot read the
    cookie to populate the header).
    """

    if not header_value or not cookie_value:
        return False
    return hmac.compare_digest(header_value, cookie_value)
