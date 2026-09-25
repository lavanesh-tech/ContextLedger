import { describe, expect, it } from "vitest";

import { ApiError, authHeaders, orgPath, query, request, toInstant, type Session } from "./api";

const ORG = "0b9f2c3e-0000-4000-8000-000000000001";
const session: Session = { organizationId: ORG, credentials: { kind: "dev-user", userId: "u-1" } };

describe("api client", () => {
  it("builds headers for both credential kinds", () => {
    expect(authHeaders({ kind: "bearer", token: "t" })).toEqual({ Authorization: "Bearer t" });
    expect(authHeaders({ kind: "dev-user", userId: "u" })).toEqual({ "X-ContextLedger-User-Id": "u" });
  });

  it("puts the organization in the path and refuses anything but a UUID", () => {
    expect(orgPath(session, "/search")).toBe(`/api/v1/organizations/${ORG}/search`);
    expect(() => orgPath({ ...session, organizationId: "../users" }, "/x")).toThrow();
  });

  it("drops empty query parameters", () => {
    expect(query({ status: "open", limit: 20, entity_type: "", x: undefined })).toBe("?status=open&limit=20");
    expect(query({})).toBe("");
  });

  it("converts datetime-local values to ISO instants", () => {
    expect(toInstant("")).toBeUndefined();
    expect(toInstant("not a date")).toBeUndefined();
    expect(toInstant("2026-01-15T10:30")).toMatch(/^2026-01-1\dT\d\d:30:00\.000Z$/);
  });

  it("sends JSON with credentials and returns the body", async () => {
    const calls: [string, RequestInit][] = [];
    const fetcher = (async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as unknown as typeof fetch;

    const body = await request<{ ok: boolean }>(session, "/search", {
      method: "POST",
      body: { query: "credit limit" },
      fetcher,
    });

    expect(body.ok).toBe(true);
    const [url, init] = calls[0]!;
    expect(url).toBe(`/api/v1/organizations/${ORG}/search`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"query":"credit limit"}');
    expect((init.headers as Record<string, string>)["X-ContextLedger-User-Id"]).toBe("u-1");
  });

  it("turns problem details into ApiError", async () => {
    const fetcher = (async () =>
      new Response(JSON.stringify({ status: 403, code: "permission_denied", detail: "not a member" }), {
        status: 403,
      })) as unknown as typeof fetch;

    const error = await request(session, "/revocations", { fetcher }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect((error as ApiError).message).toBe("not a member");
    expect((error as ApiError).problem.code).toBe("permission_denied");
  });
});
