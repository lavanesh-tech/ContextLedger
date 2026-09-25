"use client";

import { useCallback, useEffect, useState } from "react";

import { useSession } from "@/components/session";
import { Badge, ErrorBox, RequireSession } from "@/components/ui";
import { query, request } from "@/lib/api";
import { formatInstant, formatValue } from "@/lib/format";
import type { Contradiction } from "@/lib/types";

type Status = "open" | "resolved" | "dismissed" | "";

export default function ContradictionsPage() {
  const { session } = useSession();
  const [status, setStatus] = useState<Status>("open");
  const [items, setItems] = useState<Contradiction[]>([]);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    if (!session) return;
    setError(null);
    try {
      setItems(await request<Contradiction[]>(session, "/contradictions" + query({ status, limit: 50 })));
    } catch (e) {
      setError(e);
    }
  }, [session, status]);

  useEffect(() => {
    void load();
  }, [load]);

  async function close(id: string, next: "resolved" | "dismissed") {
    if (!session) return;
    const note = window.prompt(`Why is this ${next}?`) ?? undefined;
    try {
      await request(session, `/contradictions/${id}`, { method: "PATCH", body: { status: next, note } });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <RequireSession>
      <h1>Contradictions</h1>
      <section>
        <form onSubmit={(e) => e.preventDefault()}>
          <label>
            Status
            <select value={status} onChange={(e) => setStatus(e.target.value as Status)}>
              <option value="open">open</option>
              <option value="resolved">resolved</option>
              <option value="dismissed">dismissed</option>
              <option value="">all</option>
            </select>
          </label>
        </form>
      </section>
      <ErrorBox error={error} />
      {items.length === 0 ? <p className="muted">None you may see.</p> : null}
      {items.map((c) => (
        <section key={c.id}>
          <h2>
            {c.entity_type}:{c.external_id} <Badge tone={c.kind === "value_conflict" ? "warn" : "info"}>{c.kind}</Badge>{" "}
            <Badge tone={c.status === "open" ? "bad" : "ok"}>{c.status}</Badge>
          </h2>
          <p className="muted">
            {c.detector} · detected {formatInstant(c.detected_at)}
          </p>
          <p>{c.explanation}</p>
          <table>
            <tbody>
              {[c.left, c.right].map((side) => (
                <tr key={side.version.id}>
                  <td>{side.property}</td>
                  <td className="mono">{formatValue(side.version.value)}</td>
                  <td>
                    {side.source_name} <span className="muted">(authority {side.version.authority})</span>
                  </td>
                  <td>observed {formatInstant(side.version.observed_at)}</td>
                  <td>{c.preferred_version_id === side.version.id ? <Badge tone="ok">preferred by rules</Badge> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {c.status === "open" ? (
            <p>
              <button onClick={() => void close(c.id, "resolved")}>Resolve</button>{" "}
              <button className="secondary" onClick={() => void close(c.id, "dismissed")}>
                Dismiss
              </button>
            </p>
          ) : (
            <p className="muted">{c.resolution_note}</p>
          )}
        </section>
      ))}
    </RequireSession>
  );
}
