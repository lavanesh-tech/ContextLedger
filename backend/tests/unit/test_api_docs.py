"""The committed OpenAPI document and Postman collection must match the code."""

import json

from app.api.docs_export import (
    OPENAPI_FILE,
    POSTMAN_FILE,
    POSTMAN_SCHEMA,
    openapi_document,
    postman_collection,
    render,
)

STALE = "stale API docs: run `make api-docs` and commit docs/api/"


def test_committed_openapi_is_up_to_date() -> None:
    assert OPENAPI_FILE.read_text(encoding="utf-8") == render(openapi_document()), STALE


def test_committed_postman_collection_is_up_to_date() -> None:
    expected = render(postman_collection(openapi_document()))
    assert POSTMAN_FILE.read_text(encoding="utf-8") == expected, STALE


def test_postman_collection_covers_every_operation() -> None:
    spec = openapi_document()
    collection = postman_collection(spec)
    operations = sum(len(methods) for methods in spec["paths"].values())
    requests = [item for folder in collection["item"] for item in folder["item"]]

    assert collection["info"]["schema"] == POSTMAN_SCHEMA
    assert len(requests) == operations
    record = next(r for r in requests if r["name"] == "Record a fact version")
    body = json.loads(record["request"]["body"]["raw"])
    assert body["source_id"] == "{{source_id}}"
    assert body["entity_type"] == "customer"
    assert "{{organization_id}}" in record["request"]["url"]["raw"]
    assert {v["key"] for v in collection["variable"]} >= {"baseUrl", "userId", "organization_id"}
