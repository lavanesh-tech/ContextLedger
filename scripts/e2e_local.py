"""End-to-end check of the running local stack (`make up`), through the public API.

    make e2e            # everything that is free (no paid API calls)
    make e2e LLM=1      # also grounded answers, the investigator agent and MCP answer_question
                        # (real OpenAI calls: needs CONTEXTLEDGER_LLM_PROVIDER=openai in .env)

Creates a fresh tenant with synthetic data and exercises every feature: users,
organizations and roles, sources, bitemporal facts, timelines and history,
hybrid temporal search (valid time and transaction time), evidence and
provenance, decision snapshots and sealed receipts, contradiction detection,
revocations and their impact, the Neo4j provenance graph, Kafka activity,
tenant isolation, agent clients with OAuth tokens, security headers, the body
limit, and the MCP server over stdio. Prints PASS/FAIL per check; exit code 1
if any check fails. Local development only (uses the development user header).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("E2E_API_URL", "http://127.0.0.1:8000") + "/api/v1"
HDR = "X-ContextLedger-User-Id"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{title}")


class Client:
    def __init__(self, http: httpx.Client, user: str | None = None) -> None:
        self.http = http
        self.user = user

    def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if self.user:
            headers.setdefault(HDR, self.user)
        return self.http.request(method, API + path, headers=headers, **kw)


def ok_json(r: httpx.Response, *codes: int) -> Any:
    if r.status_code not in (codes or (200, 201)):
        raise AssertionError(
            f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}"
        )
    return r.json() if r.content else None


def step(name: str, fn: Callable[[], Any]) -> Any:
    try:
        value = fn()
        check(name, True)
        return value
    except Exception as exc:
        check(name, False, str(exc)[:300])
        return None


def values(result: dict[str, Any]) -> list[Any]:
    return [r["version"]["value"] for r in result.get("results", [])]


def dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


async def mcp_checks(user: str, org: str, llm: bool) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    file_env = dotenv()
    env = {
        **os.environ,
        "CONTEXTLEDGER_MCP_ORGANIZATION_ID": org,
        "CONTEXTLEDGER_MCP_USER_ID": user,
        "CONTEXTLEDGER_NEO4J_PASSWORD": file_env.get("NEO4J_PASSWORD", ""),
        "CONTEXTLEDGER_EMBEDDING_PROVIDER": file_env.get(
            "CONTEXTLEDGER_EMBEDDING_PROVIDER", "deterministic"
        ),
        "CONTEXTLEDGER_LOG_LEVEL": "WARNING",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp.server"], env=env, cwd=str(ROOT / "backend")
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check("MCP: server lists 13 tools", len(tools) == 13, f"got {sorted(tools)}")
        res = await session.call_tool(
            "search_facts",
            {"query": "credit limit of customer-991", "valid_at": "2026-02-01T00:00:00Z"},
        )
        text = " ".join(getattr(c, "text", "") for c in res.content)
        check(
            "MCP: search_facts answers as of Feb 1 (2000)",
            not res.isError and "2000" in text,
            text[:200],
        )
        res = await session.call_tool(
            "get_entity_facts", {"entity_type": "customer", "external_id": "customer-991"}
        )
        check("MCP: get_entity_facts", not res.isError, str(res.content)[:200])
        res = await session.call_tool("get_contradictions", {})
        check("MCP: get_contradictions", not res.isError, str(res.content)[:200])
        if llm:
            res = await session.call_tool(
                "answer_question",
                {
                    "question": "What was the credit limit of customer-991 on February 1, 2026?",
                    "valid_at": "2026-02-01T12:00:00Z",
                },
            )
            text = " ".join(getattr(c, "text", "") for c in res.content)
            check(
                "MCP: answer_question as of Feb 1 (OpenAI) -> 2000",
                not res.isError and "2000" in text,
                text[:300],
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="include real OpenAI calls")
    parser.add_argument("--skip-mcp", action="store_true")
    args = parser.parse_args()

    http = httpx.Client(timeout=60)
    anon = Client(http)
    tag = uuid.uuid4().hex[:8]
    print(f"ContextLedger end-to-end check  (tenant tag {tag}, LLM {'on' if args.llm else 'off'})")

    section("Platform")
    r = http.get(API + "/health/ready")
    check(
        "readiness: database up, migrations current",
        r.status_code == 200 and r.json()["migrations"]["up_to_date"],
        r.text[:200],
    )
    check(
        "security headers on API responses",
        r.headers.get("x-content-type-options") == "nosniff"
        and "default-src 'none'" in r.headers.get("content-security-policy", ""),
    )
    r = http.post(
        API + "/users",
        content=b"{" + b" " * 1_100_000 + b"}",
        headers={"content-type": "application/json"},
    )
    check("oversized body rejected with 413", r.status_code == 413, str(r.status_code))
    r = http.get(API.removesuffix("/api/v1") + "/metrics")
    check(
        "Prometheus /metrics exposed locally",
        r.status_code == 200 and "contextledger_http_requests_total" in r.text,
        str(r.status_code),
    )

    section("Tenancy and roles")
    alice_id = ok_json(
        anon.call(
            "POST", "/users", json={"email": f"alice-{tag}@example.com", "display_name": "Alice"}
        )
    )["id"]
    mallory_id = ok_json(
        anon.call(
            "POST",
            "/users",
            json={"email": f"mallory-{tag}@example.com", "display_name": "Mallory"},
        )
    )["id"]
    viewer_id = ok_json(
        anon.call(
            "POST", "/users", json={"email": f"victor-{tag}@example.com", "display_name": "Victor"}
        )
    )["id"]
    alice, mallory, viewer = (
        Client(http, alice_id),
        Client(http, mallory_id),
        Client(http, viewer_id),
    )
    org = step(
        "create organization (creator becomes ADMIN)",
        lambda: ok_json(
            alice.call("POST", "/organizations", json={"name": "Acme", "slug": f"acme-{tag}"})
        ),
    )
    if not org:
        return report()
    base = f"/organizations/{org['id']}"
    step(
        "add a VIEWER member",
        lambda: ok_json(
            alice.call("POST", f"{base}/members", json={"user_id": viewer_id, "role": "VIEWER"})
        ),
    )
    r = mallory.call("GET", f"{base}/entities/customer/customer-991/facts")
    check("outsider is refused (tenant isolation)", r.status_code in (403, 404), str(r.status_code))

    section("Sources and bitemporal facts")
    billing = ok_json(
        alice.call(
            "POST",
            f"{base}/sources",
            json={"name": "billing-db", "source_type": "SYSTEM_OF_RECORD", "default_authority": 90},
        )
    )
    crm = ok_json(
        alice.call(
            "POST",
            f"{base}/sources",
            json={"name": "crm", "source_type": "SYSTEM_OF_RECORD", "default_authority": 60},
        )
    )

    def fact(
        ext: str, value: Any, valid_from: str, source: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        body = {
            "entity_type": "customer",
            "external_id": ext,
            "property": "credit_limit",
            "value": value,
            "source_id": source["id"],
            "valid_from": valid_from,
            **extra,
        }
        return ok_json(alice.call("POST", f"{base}/facts", json=body), 201)

    v1 = step(
        "record version 1 (2000 from Jan 15)",
        lambda: fact("customer-991", 2000, "2026-01-15T09:00:00Z", billing),
    )
    v2 = step(
        "record version 2 (5000 from Mar 1) supersedes v1",
        lambda: fact("customer-991", 5000, "2026-03-01T09:00:00Z", billing),
    )
    check("v2 points at v1", bool(v1 and v2 and v2["supersedes_id"] == v1["id"]))
    r = alice.call(
        "POST",
        f"{base}/facts",
        json={
            "entity_type": "customer",
            "external_id": "customer-991",
            "property": "credit_limit",
            "value": 1,
            "source_id": billing["id"],
            "valid_from": "2026-02-01T00:00:00Z",
        },
    )
    check("rewriting history is refused (409)", r.status_code == 409, str(r.status_code))
    r = alice.call("POST", f"{base}/facts", json={"entity_type": "customer"})
    check(
        "invalid body is refused (422 problem+json)",
        r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json"),
        str(r.status_code),
    )
    r = viewer.call(
        "POST",
        f"{base}/facts",
        json={
            "entity_type": "customer",
            "external_id": "c-x",
            "property": "p",
            "value": 1,
            "source_id": billing["id"],
            "valid_from": "2026-01-01T00:00:00Z",
        },
    )
    check("VIEWER cannot write facts (403)", r.status_code == 403, str(r.status_code))
    current = ok_json(alice.call("GET", f"{base}/entities/customer/customer-991/facts"))
    check(
        "current value is 5000",
        [f["version"]["value"] for f in current["facts"]] == [5000],
        json.dumps(current)[:200],
    )
    feb = ok_json(
        alice.call(
            "GET",
            f"{base}/entities/customer/customer-991/facts",
            params={"valid_at": "2026-02-01T00:00:00Z"},
        )
    )
    check(
        "value as of Feb 1 is 2000",
        [f["version"]["value"] for f in feb["facts"]] == [2000],
        json.dumps(feb)[:200],
    )
    timeline = ok_json(alice.call("GET", f"{base}/entities/customer/customer-991/timeline"))
    check("timeline has both versions", len(timeline["entries"]) == 2)
    step(
        "fact history", lambda: ok_json(alice.call("GET", f"{base}/facts/{v2['fact_id']}/history"))
    )
    step(
        "entity changes",
        lambda: ok_json(
            alice.call(
                "GET",
                f"{base}/entities/customer/customer-991/changes",
                params={"start": "2026-01-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"},
            )
        ),
    )
    step(
        "version lineage",
        lambda: ok_json(alice.call("GET", f"{base}/fact-versions/{v2['id']}/lineage")),
    )

    section("Hybrid temporal search")
    time.sleep(3)  # embedding worker
    q = {"query": "credit limit of customer-991"}
    now = ok_json(alice.call("POST", f"{base}/search", json=q))
    check("search now -> 5000", values(now)[:1] == [5000], str(values(now)))
    check(
        "vector search used (embeddings ready)",
        now.get("vector_search") == "used",
        str(now.get("vector_search")),
    )
    past = ok_json(
        alice.call("POST", f"{base}/search", json={**q, "valid_at": "2026-02-01T00:00:00Z"})
    )
    check("search valid_at Feb 1 -> 2000", values(past)[:1] == [2000], str(values(past)))
    before = ok_json(
        alice.call("POST", f"{base}/search", json={**q, "known_at": "2026-01-01T00:00:00Z"})
    )
    check("search known_at before recording -> nothing", values(before) == [], str(values(before)))
    again = ok_json(alice.call("POST", f"{base}/search", json=q))
    check(
        "repeated search served from Redis cache",
        again.get("cache") == "hit",
        str(again.get("cache")),
    )

    section("Evidence and provenance")
    ev = step(
        "capture evidence (content-addressed)",
        lambda: ok_json(
            alice.call(
                "POST",
                f"{base}/evidence",
                json={
                    "source_id": billing["id"],
                    "evidence_type": "DATABASE_RECORD",
                    "excerpt": f"customer-991 credit_limit=5000 ({tag})",
                    "captured_at": "2026-03-01T09:00:00Z",
                },
            ),
            201,
        )["evidence"],
    )
    r = alice.call(
        "POST",
        f"{base}/evidence",
        json={
            "source_id": billing["id"],
            "evidence_type": "DATABASE_RECORD",
            "excerpt": f"customer-991 credit_limit=5000 ({tag})",
            "captured_at": "2026-03-01T09:00:00Z",
        },
    )
    check("same evidence again is deduplicated (200)", r.status_code == 200, str(r.status_code))
    if ev:
        step(
            "attach evidence to version 2",
            lambda: ok_json(
                alice.call(
                    "POST",
                    f"{base}/fact-versions/{v2['id']}/evidence",
                    json={"evidence_id": ev["id"]},
                )
            ),
        )
        prov = step(
            "provenance of version 2",
            lambda: ok_json(alice.call("GET", f"{base}/fact-versions/{v2['id']}/provenance")),
        )
        check("provenance lists the evidence", bool(prov) and ev["id"] in json.dumps(prov))

    section("Decisions and sealed receipts")
    snap = step(
        "capture decision context (snapshot)",
        lambda: ok_json(alice.call("POST", f"{base}/context-snapshots", json=q), 201),
    )
    decision = None
    if snap:
        relied = [r["version"]["id"] for r in snap["retrieval"]["results"][:1]]
        decision = step(
            "record decision relying on v2",
            lambda: ok_json(
                alice.call(
                    "POST",
                    f"{base}/decisions",
                    json={
                        "snapshot_id": snap["snapshot_id"],
                        "action": "credit.approve_increase",
                        "outcome": {"approved": True, "new_limit": 7500},
                        "relied_on": relied,
                        "rationale": "e2e",
                        "agent": "e2e-check",
                    },
                ),
                201,
            ),
        )
    if decision:
        receipt = ok_json(alice.call("GET", f"{base}/decisions/{decision['decision_id']}/receipt"))
        check(
            "receipt integrity verified (hash matches)",
            receipt.get("integrity_verified") is True
            and len(receipt.get("receipt_sha256", "")) == 64,
        )
        uses = ok_json(alice.call("GET", f"{base}/fact-versions/{v2['id']}/decisions"))
        check("v2 knows which decisions relied on it", decision["decision_id"] in json.dumps(uses))

    section("Contradictions")
    fact("customer-992", 3000, "2026-01-01T00:00:00Z", billing, observed_at="2026-03-01T00:00:00Z")
    fact("customer-992", 3500, "2026-02-01T00:00:00Z", crm)
    contradictions = ok_json(alice.call("GET", f"{base}/contradictions"))
    items = (
        contradictions
        if isinstance(contradictions, list)
        else contradictions.get("items", contradictions.get("contradictions", []))
    )
    check(
        "conflicting sources detected as a contradiction",
        len(items) >= 1,
        json.dumps(contradictions)[:200],
    )
    if items:
        cid = items[0]["id"]
        step(
            "resolve the contradiction",
            lambda: ok_json(
                alice.call(
                    "PATCH",
                    f"{base}/contradictions/{cid}",
                    json={"status": "resolved", "note": "e2e"},
                )
            ),
        )

    section("Revocations")
    if v2:
        step(
            "revoke version 2",
            lambda: ok_json(
                alice.call(
                    "POST",
                    f"{base}/facts/{v2['fact_id']}/revoke",
                    json={"reason": "e2e: wrong import", "version_id": v2["id"]},
                ),
                200,
                201,
            ),
        )
        impact = step(
            "revocation impact",
            lambda: ok_json(alice.call("GET", f"{base}/facts/{v2['fact_id']}/impact")),
        )
        if decision:
            check(
                "impact lists the decision that relied on v2",
                bool(impact) and decision["decision_id"] in json.dumps(impact),
            )
        after = ok_json(alice.call("POST", f"{base}/search", json=q))
        check("revoked 5000 no longer returned", 5000 not in values(after), str(values(after)))
        revs = ok_json(alice.call("GET", f"{base}/revocations"))
        check("revocation is listed", v2["id"] in json.dumps(revs))

    section("Graph (Neo4j) and events (Kafka)")
    time.sleep(8)  # graph projector + event relay/consumers
    r = alice.call("GET", f"{base}/impact/fact-versions/{v1['id']}") if v1 else None
    check(
        "Neo4j impact analysis answers",
        r is not None and r.status_code == 200,
        "" if r is None else f"{r.status_code} {r.text[:200]}",
    )
    if decision:
        r = alice.call("GET", f"{base}/decisions/{decision['decision_id']}/lineage")
        check(
            "Neo4j decision lineage answers",
            r.status_code == 200,
            f"{r.status_code} {r.text[:200]}",
        )
    r = alice.call("GET", f"{base}/activity")
    check(
        "Kafka activity counted for this tenant",
        r.status_code == 200 and r.json() not in ({}, [], None),
        f"{r.status_code} {r.text[:200]}",
    )

    section("Agent clients and OAuth")
    created = step(
        "create agent client (secret shown once)",
        lambda: ok_json(
            alice.call(
                "POST",
                f"{base}/agent-clients",
                json={"name": f"agent-{tag}", "role": "VIEWER", "scopes": ["facts:read"]},
            ),
            201,
        ),
    )
    if created:
        tok = http.post(
            API + "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": created["client_id"],
                "client_secret": created["client_secret"],
            },
        )
        if check(
            "client credentials -> access token",
            tok.status_code == 200,
            f"{tok.status_code} {tok.text[:200]}",
        ):
            bearer = {"Authorization": f"Bearer {tok.json()['access_token']}"}
            r = http.get(API + f"{base}/entities/customer/customer-991/timeline", headers=bearer)
            check("agent token can read facts", r.status_code == 200, str(r.status_code))
            r = http.post(
                API + f"{base}/facts",
                headers=bearer,
                json={
                    "entity_type": "x",
                    "external_id": "y",
                    "property": "z",
                    "value": 1,
                    "source_id": billing["id"],
                    "valid_from": "2026-01-01T00:00:00Z",
                },
            )
            check(
                "agent token without facts:write is refused",
                r.status_code == 403,
                str(r.status_code),
            )
        bad = http.post(
            API + "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": created["client_id"],
                "client_secret": "wrong",
            },
        )
        check("wrong client secret is refused", bad.status_code in (400, 401), str(bad.status_code))

    if args.llm:
        section("LLM features (real OpenAI calls)")
        ans = alice.call(
            "POST",
            f"{base}/answers",
            json={
                "question": "What was the credit limit of customer-991 on February 1, 2026?",
                "valid_at": "2026-02-01T12:00:00Z",
            },
        )
        check(
            "grounded answer with verified citation (2000)",
            ans.status_code == 200 and "2000" in ans.text,
            f"{ans.status_code} {ans.text[:300]}",
        )
        if decision:
            inv = alice.call(
                "POST",
                f"{base}/investigations",
                json={
                    "question": f"Why was decision {decision['decision_id']} approved, and has any fact it relied on changed since?"
                },
            )
            check(
                "investigator agent completes with a trace",
                inv.status_code in (200, 201) and '"status"' in inv.text,
                f"{inv.status_code} {inv.text[:300]}",
            )

    if not args.skip_mcp:
        section("MCP server (stdio)")
        try:
            asyncio.run(mcp_checks(alice_id, org["id"], args.llm))
        except Exception as exc:
            check("MCP server starts and answers", False, repr(exc)[:300])
    return report()


def report() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    for name, _, detail in failed:
        print(f"  FAILED: {name}: {detail}")
    return 1 if failed else 0


def safe_main() -> int:
    try:
        return main()
    except Exception as exc:
        check("script ran to the end", False, repr(exc)[:300])
        return report()


if __name__ == "__main__":
    sys.exit(safe_main())
