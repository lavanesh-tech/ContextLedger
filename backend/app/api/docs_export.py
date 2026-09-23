"""Export the OpenAPI document and a Postman collection generated from it.

    make api-docs   # writes docs/api/openapi.json and a Postman collection next to it

Both files are committed. A unit test regenerates them in memory and fails if
the committed copies are stale, so the published contract always matches the code.
"""

import json
import re
from pathlib import Path
from typing import Any, Final

from app.core.config import Environment, Settings

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DOCS_DIR: Final = REPO_ROOT / "docs" / "api"
OPENAPI_FILE: Final = DOCS_DIR / "openapi.json"
POSTMAN_FILE: Final = DOCS_DIR / "ContextLedger.postman_collection.json"
POSTMAN_SCHEMA: Final = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
USER_HEADER: Final = "X-ContextLedger-User-Id"
_PATH_PARAM: Final = re.compile(r"\{([^}]+)\}")


def openapi_document() -> dict[str, Any]:
    from app.main import create_app  # local import: building the app configures logging

    settings = Settings(_env_file=None, environment=Environment.TEST, docs_enabled=True)
    return create_app(settings).openapi()


def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[1]
        schema = spec["components"]["schemas"][name]
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        return _resolve(options[0], spec) if options else {"type": "null"}
    return schema


def _example(name: str, schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> Any:
    schema = _resolve(schema, spec)
    if schema.get("examples"):
        return schema["examples"][0]
    if "default" in schema and schema["default"] not in ([], {}):
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        if schema.get("format") == "uuid":
            return f"{{{{{name}}}}}"  # a Postman variable, e.g. {{source_id}}
        if schema.get("format") == "date-time":
            return "2026-01-15T09:00:00Z"
        return ""
    if kind in {"integer", "number"}:
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object" and depth < 3:
        required = set(schema.get("required", []))
        return {
            prop: _example(prop, sub, spec, depth + 1)
            for prop, sub in schema.get("properties", {}).items()
            if prop in required or "examples" in _resolve(sub, spec)
        }
    return None


def postman_collection(spec: dict[str, Any]) -> dict[str, Any]:
    folders: dict[str, list[dict[str, Any]]] = {}
    variables = {"baseUrl": "http://127.0.0.1:8000", "userId": ""}
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            for param in _PATH_PARAM.findall(path):
                variables.setdefault(param, "")
            postman_path = _PATH_PARAM.sub(lambda m: "{{" + m.group(1) + "}}", path)
            request: dict[str, Any] = {
                "method": method.upper(),
                "header": [{"key": USER_HEADER, "value": "{{userId}}"}],
                "url": {
                    "raw": "{{baseUrl}}" + postman_path,
                    "host": ["{{baseUrl}}"],
                    "path": postman_path.strip("/").split("/"),
                },
                "description": operation.get("description", ""),
            }
            query = [p for p in operation.get("parameters", []) if p["in"] == "query"]
            if query:
                request["url"]["query"] = [
                    {"key": p["name"], "value": "", "disabled": not p.get("required", False)}
                    for p in query
                ]
                request["url"]["raw"] += "?" + "&".join(f"{p['name']}=" for p in query)
            body = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                request["header"].append({"key": "Content-Type", "value": "application/json"})
                request["body"] = {
                    "mode": "raw",
                    "raw": json.dumps(_example("body", body["schema"], spec), indent=2),
                    "options": {"raw": {"language": "json"}},
                }
            tag = operation.get("tags", ["other"])[0]
            folders.setdefault(tag, []).append({"name": operation["summary"], "request": request})
    return {
        "info": {
            "name": "ContextLedger API",
            "description": (
                "Generated from the OpenAPI document by app/api/docs_export.py. Set `userId` "
                "to a user id (development-headers auth), then the path variables."
            ),
            "schema": POSTMAN_SCHEMA,
        },
        "variable": [{"key": key, "value": value} for key, value in variables.items()],
        "item": [{"name": tag, "item": items} for tag, items in folders.items()],
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    spec = openapi_document()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OPENAPI_FILE.write_text(render(spec), encoding="utf-8")
    POSTMAN_FILE.write_text(render(postman_collection(spec)), encoding="utf-8")
    print(f"wrote {OPENAPI_FILE.relative_to(REPO_ROOT)} and {POSTMAN_FILE.relative_to(REPO_ROOT)}")  # noqa: T201


if __name__ == "__main__":
    main()
