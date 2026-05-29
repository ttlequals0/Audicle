from __future__ import annotations

import shutil
from pathlib import Path

from app.core import database
from app.main import create_app
from fastapi.testclient import TestClient


def test_no_static_ui_skipped_when_dir_missing(env: Path) -> None:
    """Without ``backend/static/ui`` the SPA mount is skipped so unmatched
    routes return the project's 404 envelope."""

    database.run_migrations(env)
    # Confirm the static dir does NOT exist in the test workspace; this
    # exercises the early-return in ``_mount_static_ui``.
    static_dir = Path(__file__).resolve().parent.parent / "static" / "ui"
    assert not static_dir.exists(), (
        "expected backend/static/ui to be absent in tests; remove or "
        ".gitignore it before re-running"
    )
    with TestClient(create_app()) as client:
        response = client.get("/")
    assert response.status_code == 404


def test_static_ui_mount_serves_index_and_falls_back_for_spa_routes(
    env: Path, tmp_path: Path, monkeypatch
) -> None:
    """When ``backend/static/ui`` does exist, ``/`` serves index.html and
    deep links (``/feed``) fall back to index.html so React Router takes
    over client-side."""

    # Plant a minimal SPA in backend/static/ui for this test.
    backend_root = Path(__file__).resolve().parent.parent
    target = backend_root / "static" / "ui"
    target.mkdir(parents=True, exist_ok=True)
    (target / "assets").mkdir(exist_ok=True)
    (target / "index.html").write_text(
        "<html><body><div id='root'>SPA</div></body></html>", encoding="utf-8"
    )
    (target / "favicon.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
    )
    # A real file that is NOT on the SPA root allowlist must NOT be served
    # by the catch-all (it falls through to index.html instead).
    (target / "secret.txt").write_text("TOPSECRET", encoding="utf-8")

    def _cleanup() -> None:
        shutil.rmtree(target.parent, ignore_errors=True)

    monkeypatch.setattr(
        target,
        "__class__",
        type(target),  # no-op, just register cleanup via finalizer
    )
    request_cleanup = lambda: _cleanup()  # noqa: E731
    try:
        database.run_migrations(env)
        with TestClient(create_app()) as client:
            root = client.get("/")
            spa_route = client.get("/feed")
            favicon = client.get("/favicon.svg")
            secret = client.get("/secret.txt")
        assert root.status_code == 200
        assert b"SPA" in root.content
        # Deep link returns the same index.html (SPA router will pick it up).
        assert spa_route.status_code == 200
        assert b"SPA" in spa_route.content
        # Real static file resolves as itself.
        assert favicon.status_code == 200
        assert favicon.content.startswith(b"<svg")
        # Non-allowlisted real file is not exposed; it falls back to index.html.
        assert secret.status_code == 200
        assert b"SPA" in secret.content
        assert b"TOPSECRET" not in secret.content
    finally:
        request_cleanup()
