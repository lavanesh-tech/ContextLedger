// A small typed client for the ContextLedger REST API.
//
// Requests go to /api/v1 on the Next.js server, which forwards them to FastAPI
// (next.config.ts). Every tenant-scoped call carries the organization id in the
// path and the caller's credentials in a header; the backend decides everything
// else (membership, role, scopes, privacy ceiling).

import type { Problem } from "./types";

export type Credentials =
  | { kind: "dev-user"; userId: string } // local development only (X-ContextLedger-User-Id)
  | { kind: "bearer"; token: string }; // a JWT from /api/v1/auth or /api/v1/oauth/token

export interface Session {
  organizationId: string;
  credentials: Credentials;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: Problem,
  ) {
    super(problem.detail ?? problem.title ?? `HTTP ${status}`);
    this.name = "ApiError";
  }
}

export function authHeaders(credentials: Credentials): Record<string, string> {
  return credentials.kind === "bearer"
    ? { Authorization: `Bearer ${credentials.token}` }
    : { "X-ContextLedger-User-Id": credentials.userId };
}

export function orgPath(session: Session, path: string): string {
  if (!/^[0-9a-f-]{36}$/i.test(session.organizationId)) {
    throw new Error("organization id must be a UUID");
  }
  return `/api/v1/organizations/${session.organizationId}${path}`;
}

/** Query string from defined, non-empty values only. */
export function query(params: Record<string, string | number | undefined | null>): string {
  const entries = Object.entries(params).filter(
    (entry): entry is [string, string | number] => entry[1] !== undefined && entry[1] !== null && entry[1] !== "",
  );
  if (entries.length === 0) return "";
  return "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString();
}

/** A datetime-local input value ("2026-01-15T10:30") as an ISO instant in the browser's zone. */
export function toInstant(local: string): string | undefined {
  if (!local) return undefined;
  const date = new Date(local);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

export async function parseProblem(response: Response): Promise<Problem> {
  try {
    return (await response.json()) as Problem;
  } catch {
    return { status: response.status, title: response.statusText };
  }
}

export async function request<T>(
  session: Session,
  path: string,
  init: { method?: string; body?: unknown; fetcher?: typeof fetch } = {},
): Promise<T> {
  const fetcher = init.fetcher ?? fetch;
  const response = await fetcher(orgPath(session, path), {
    method: init.method ?? "GET",
    headers: {
      Accept: "application/json",
      ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...authHeaders(session.credentials),
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status, await parseProblem(response));
  return (await response.json()) as T;
}
