"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { useSession } from "@/components/session";
import { Badge, ErrorBox, RequireSession } from "@/components/ui";
import { query, request } from "@/lib/api";
import { formatInstant, formatValue } from "@/lib/format";
import type { RevocationReport } from "@/lib/types";

function Report({ report }: { report: RevocationReport }) {
  return (
    <section>
      <h2>
        {report.entity_type}:{report.external_id} {report.property} v{report.version.version}{" "}
        <span className="mono">= {formatValue(report.version.value)}</span>
      </h2>
      <p className="muted">
        Revoked {formatInstant(report.revoked_at)}: {report.reason}
      </p>
      <p>
        {report.decisions_with_version_in_context} decision(s) had this version in context,{" "}
        {report.decisions_relied_on} relied on it.
      </p>
      {report.decisions.length ? (
        <table>
          <thead>
            <tr>
              <th>Decision</th>
              <th>Action</th>
              <th>Decided</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {report.decisions.map((d) => (
              <tr key={d.decision_id}>
                <td className="mono">{d.decision_id}</td>
                <td>{d.action}</td>
                <td>{formatInstant(d.decided_at)}</td>
                <td>
                  {d.relied_on ? <Badge tone="bad">relied on</Badge> : <Badge tone="info">in context</Badge>}{" "}
                  {d.decided_after_revocation ? <Badge tone="warn">decided after revocation</Badge> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}

export default function RevocationsPage() {
  const { session } = useSession();
  const [reports, setReports] = useState<RevocationReport[]>([]);
  const [factId, setFactId] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    if (!session) return;
    try {
      setReports(await request<RevocationReport[]>(session, "/revocations" + query({ limit: 20 })));
    } catch (e) {
      setError(e);
    }
  }, [session]);

  useEffect(() => {
    void load();
  }, [load]);

  async function revoke(event: FormEvent) {
    event.preventDefault();
    if (!session) return;
    setError(null);
    try {
      await request<RevocationReport>(session, `/facts/${encodeURIComponent(factId.trim())}/revoke`, {
        method: "POST",
        body: { reason },
      });
      setFactId("");
      setReason("");
      await load();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <RequireSession>
      <h1>Revocations</h1>
      <section>
        <form onSubmit={revoke}>
          <label>
            Fact id (revokes its latest version)
            <input value={factId} onChange={(e) => setFactId(e.target.value)} required style={{ minWidth: 340 }} />
          </label>
          <label>
            Reason
            <input value={reason} onChange={(e) => setReason(e.target.value)} required style={{ minWidth: 320 }} />
          </label>
          <button>Revoke</button>
        </form>
        <p className="muted">Needs facts:write and decisions:read. Nothing is deleted; the version leaves current answers.</p>
      </section>
      <ErrorBox error={error} />
      {reports.map((r) => (
        <Report key={r.revocation_id} report={r} />
      ))}
    </RequireSession>
  );
}
