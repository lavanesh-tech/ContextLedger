"use client";

import { useState, type FormEvent } from "react";

import { useSession } from "@/components/session";
import { Badge, ErrorBox, RequireSession } from "@/components/ui";
import { request } from "@/lib/api";
import { formatInstant, formatValue, validity } from "@/lib/format";
import type { DecisionReceipt } from "@/lib/types";

export default function DecisionsPage() {
  const { session } = useSession();
  const [decisionId, setDecisionId] = useState("");
  const [receipt, setReceipt] = useState<DecisionReceipt | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!session) return;
    setError(null);
    try {
      setReceipt(await request<DecisionReceipt>(session, `/decisions/${encodeURIComponent(decisionId.trim())}/receipt`));
    } catch (e) {
      setReceipt(null);
      setError(e);
    }
  }

  return (
    <RequireSession>
      <h1>Decision receipt</h1>
      <section>
        <form onSubmit={submit}>
          <label>
            Decision id
            <input value={decisionId} onChange={(e) => setDecisionId(e.target.value)} required style={{ minWidth: 340 }} />
          </label>
          <button>Open</button>
        </form>
      </section>
      <ErrorBox error={error} />
      {receipt ? (
        <>
          <section>
            <h2>
              {receipt.action}{" "}
              {receipt.integrity_verified ? (
                <Badge tone="ok">hash verified</Badge>
              ) : (
                <Badge tone="bad">hash mismatch</Badge>
              )}
            </h2>
            <div className="grid">
              <div>
                <div className="muted">Decided</div>
                {formatInstant(receipt.decided_at)} {receipt.agent ? `by ${receipt.agent}` : ""}
              </div>
              <div>
                <div className="muted">Outcome</div>
                <span className="mono">{formatValue(receipt.outcome)}</span>
              </div>
              <div>
                <div className="muted">Context</div>
                valid at {formatInstant(receipt.context.valid_at)}, known at {formatInstant(receipt.context.known_at)}
              </div>
              <div>
                <div className="muted">Receipt SHA-256</div>
                <span className="mono">{receipt.receipt_sha256.slice(0, 16)}…</span>
              </div>
            </div>
            {receipt.rationale ? <p>{receipt.rationale}</p> : null}
          </section>
          <section className="scroll">
            <h2>Facts as known when the decision was made</h2>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Fact</th>
                  <th>Value</th>
                  <th>Valid</th>
                  <th>Source</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {receipt.facts.map((f) => (
                  <tr key={f.fact_version_id}>
                    <td>{f.position}</td>
                    <td>
                      {f.redacted ? (
                        <span className="muted">redacted ({f.privacy_scope})</span>
                      ) : (
                        `${f.entity_type}:${f.external_id} ${f.property}`
                      )}
                    </td>
                    <td className="mono">{f.version ? formatValue(f.version.value) : "—"}</td>
                    <td>{f.version ? validity(f.version.valid_from, f.version.valid_until) : "—"}</td>
                    <td>{f.source_name ?? "—"}</td>
                    <td>
                      {f.relied_on ? <Badge tone="info">relied on</Badge> : null}{" "}
                      {f.revoked_at ? <Badge tone="bad">revoked {formatInstant(f.revoked_at)}</Badge> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      ) : null}
    </RequireSession>
  );
}
