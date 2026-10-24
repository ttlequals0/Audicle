from __future__ import annotations

from pathlib import Path

import yaml
from app.main import create_app
from app.version import __version__


def test_schema_documents_runtime_error_and_auth_contracts():
    schema = create_app().openapi()

    assert set(schema["components"]["securitySchemes"]) == {
        "AdminSession",
        "CsrfToken",
        "FeedKey",
    }
    assert schema["paths"]["/api/v1/submit"]["post"]["security"] == [
        {},
        {"AdminSession": [], "CsrfToken": []},
    ]
    assert schema["paths"]["/rss/{slug}.xml"]["get"]["security"] == [
        {},
        {"FeedKey": []},
        {"AdminSession": []},
    ]
    assert schema["paths"]["/api/v1/auth/login"]["post"].get("security") is None

    operations = [
        value
        for path_item in schema["paths"].values()
        for method, value in path_item.items()
        if method in {"get", "head", "post", "put", "patch", "delete"}
    ]
    assert all("422" not in operation.get("responses", {}) for operation in operations)
    assert any("400" in operation.get("responses", {}) for operation in operations)


def test_operation_ids_are_unique():
    schema = create_app().openapi()
    operation_ids = [
        value["operationId"]
        for path_item in schema["paths"].values()
        for method, value in path_item.items()
        if method in {"get", "head", "post", "put", "patch", "delete"}
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_committed_openapi_matches_release_version():
    """openapi.yaml is generated from app.version; a release bump that skips
    scripts/dump_openapi.py would publish a contract advertising the old version."""

    root = Path(__file__).resolve().parents[2]
    document = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    assert document["info"]["version"] == __version__
