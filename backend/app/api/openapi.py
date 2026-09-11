"""OpenAPI adjustments for Audicle's conditional authentication contract."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def schema_for(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema is not None:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes.update(
        {
            "AdminSession": {
                "type": "apiKey",
                "in": "cookie",
                "name": "audicle_session",
                "description": "Required for the admin API when a password is configured.",
            },
            "CsrfToken": {
                "type": "apiKey",
                "in": "header",
                "name": "X-CSRF-Token",
                "description": "Required with the admin session for unsafe methods.",
            },
            "FeedKey": {
                "type": "apiKey",
                "in": "query",
                "name": "key",
                "description": "Required for protected feeds and non-artwork media.",
            },
        }
    )
    components.setdefault("schemas", {})["ErrorEnvelope"] = {
        "type": "object",
        "required": ["error", "status"],
        "properties": {
            "error": {"type": "string"},
            "status": {"type": "integer"},
            "details": {"type": "object", "additionalProperties": True},
        },
    }

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "head", "post", "put", "patch", "delete"}:
                continue
            responses = operation.get("responses", {})
            if "422" in responses:
                responses.pop("422")
                responses.setdefault(
                    "400",
                    {
                        "description": "Invalid request",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                            }
                        },
                    },
                )
            if path.startswith(("/rss/", "/media/")):
                operation["security"] = [
                    {},
                    {"FeedKey": []},
                    {"AdminSession": []},
                ]
            elif path.startswith("/api/v1/") and not path.startswith("/api/v1/auth/"):
                protected = {"AdminSession": []}
                if method not in {"get", "head"}:
                    protected["CsrfToken"] = []
                operation["security"] = [{}, protected]
            elif path == "/api/v1/auth/revoke-sessions":
                operation["security"] = [{}, {"AdminSession": [], "CsrfToken": []}]

    app.openapi_schema = schema
    return schema
