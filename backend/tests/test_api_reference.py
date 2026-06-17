from __future__ import annotations

from app.main import create_app


def test_default_sample_text_is_cicero_passage():
    from app.api.v1.reference import DEFAULT_SAMPLE_TEXT

    assert DEFAULT_SAMPLE_TEXT == (
        "But I must explain to you how all this mistaken idea of denouncing "
        "of a pleasure and praising pain was born and I will give you a "
        "complete account of the system, and expound the actual teachings of "
        "the great explorer of the truth, the master-builder of human happiness."
    )
    assert 4 <= len(DEFAULT_SAMPLE_TEXT) <= 400


def test_slot_audition_uses_default_sample_text_constant():
    # The slot-audition endpoint exposes the shared sample-text constant as the
    # form default (FastAPI surfaces Form defaults via a $ref component schema).
    from app.api.v1.reference import DEFAULT_SAMPLE_TEXT

    schema = create_app().openapi()
    components = schema["components"]["schemas"]
    body = schema["paths"]["/api/v1/reference/slots/{slot}/audition"]["post"]["requestBody"][
        "content"
    ]
    body_schema = next(iter(body.values()))["schema"]
    if "$ref" in body_schema:
        body_schema = components[body_schema["$ref"].split("/")[-1]]
    assert body_schema["properties"]["sample_text"]["default"] == DEFAULT_SAMPLE_TEXT
