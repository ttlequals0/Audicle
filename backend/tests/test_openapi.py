from __future__ import annotations

from app.main import create_app


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
