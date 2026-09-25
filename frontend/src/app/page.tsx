"use client";

import { useEffect, useState, type FormEvent } from "react";

import { useSession } from "@/components/session";
import { Badge } from "@/components/ui";

interface Readiness {
  status: string;
}

export default function ConnectionPage() {
  const { session, save } = useSession();
  const [organizationId, setOrganizationId] = useState("");
  const [kind, setKind] = useState<"dev-user" | "bearer">("dev-user");
  const [secret, setSecret] = useState("");
  const [health, setHealth] = useState<Readiness | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  useEffect(() => {
    if (!session) return;
    setOrganizationId(session.organizationId);
    setKind(session.credentials.kind);
    setSecret(session.credentials.kind === "bearer" ? session.credentials.token : session.credentials.userId);
  }, [session]);

  useEffect(() => {
    fetch("/api/v1/health/ready", { cache: "no-store" })
      .then(async (r) => setHealth((await r.json()) as Readiness))
      .catch(() => setHealthError("The API is not reachable. Is `make run` (or `make up`) running?"));
  }, []);

  function submit(event: FormEvent) {
    event.preventDefault();
    save({
      organizationId: organizationId.trim(),
      credentials: kind === "bearer" ? { kind, token: secret.trim() } : { kind, userId: secret.trim() },
    });
  }

  return (
    <>
      <h1>Connection</h1>
      <section>
        <p className="muted">
          API:{" "}
          {health ? (
            <Badge tone={health.status === "ready" ? "ok" : "warn"}>{health.status}</Badge>
          ) : (
            <span>{healthError ?? "checking…"}</span>
          )}
        </p>
        <form onSubmit={submit}>
          <label>
            Organization id
            <input value={organizationId} onChange={(e) => setOrganizationId(e.target.value)} required />
          </label>
          <label>
            Credentials
            <select value={kind} onChange={(e) => setKind(e.target.value as "dev-user" | "bearer")}>
              <option value="dev-user">User id header (local development)</option>
              <option value="bearer">Bearer token (JWT)</option>
            </select>
          </label>
          <label>
            {kind === "bearer" ? "Access token" : "User id"}
            <input
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              type={kind === "bearer" ? "password" : "text"}
              required
            />
          </label>
          <button type="submit">Save</button>
          {session ? (
            <button type="button" className="secondary" onClick={() => save(null)}>
              Forget
            </button>
          ) : null}
        </form>
        <p className="muted">
          Stored only in this browser. The API decides what you may see: membership, role, token scopes and
          privacy ceiling are checked on every request.
        </p>
      </section>
    </>
  );
}
