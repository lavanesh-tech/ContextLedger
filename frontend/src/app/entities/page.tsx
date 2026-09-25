"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";

import { useSession } from "@/components/session";
import { ErrorBox, RequireSession } from "@/components/ui";
import { query, request, toInstant } from "@/lib/api";
import { formatInstant, formatValue, validity } from "@/lib/format";
import type { TimelineEntry } from "@/lib/types";

function Timeline() {
  const { session } = useSession();
  const params = useSearchParams();
  const [entityType, setEntityType] = useState(params.get("type") ?? "customer");
  const [externalId, setExternalId] = useState(params.get("id") ?? "customer-991");
  const [knownAt, setKnownAt] = useState("");
  const [entries, setEntries] = useState<TimelineEntry[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function load() {
    if (!session) return;
    setError(null);
    try {
      const path =
        `/entities/${encodeURIComponent(entityType)}/${encodeURIComponent(externalId)}/timeline` +
        query({ known_at: toInstant(knownAt) });
      const body = await request<{ entries: TimelineEntry[] }>(session, path);
      setEntries(body.entries);
    } catch (e) {
      setEntries(null);
      setError(e);
    }
  }

  useEffect(() => {
    if (params.get("id")) void load();
  }, [session]);

  function submit(event: FormEvent) {
    event.preventDefault();
    void load();
  }

  return (
    <>
      <h1>Entity timeline</h1>
      <section>
        <form onSubmit={submit}>
          <label>
            Entity type
            <input value={entityType} onChange={(e) => setEntityType(e.target.value)} required />
          </label>
          <label>
            External id
            <input value={externalId} onChange={(e) => setExternalId(e.target.value)} required />
          </label>
          <label>
            As known at (default latest)
            <input type="datetime-local" value={knownAt} onChange={(e) => setKnownAt(e.target.value)} />
          </label>
          <button>Load</button>
        </form>
      </section>
      <ErrorBox error={error} />
      {entries ? (
        <section className="scroll">
          <table>
            <thead>
              <tr>
                <th>Property</th>
                <th>Version</th>
                <th>Value</th>
                <th>Valid (valid time)</th>
                <th>Recorded (transaction time)</th>
                <th>Scope</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.version.id}>
                  <td>{e.property}</td>
                  <td>v{e.version.version}</td>
                  <td className="mono">{formatValue(e.version.value)}</td>
                  <td>{validity(e.version.valid_from, e.version.valid_until)}</td>
                  <td>{formatInstant(e.version.recorded_at)}</td>
                  <td>{e.version.privacy_scope}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </>
  );
}

export default function EntitiesPage() {
  return (
    <RequireSession>
      <Suspense>
        <Timeline />
      </Suspense>
    </RequireSession>
  );
}
