"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { useSession } from "@/components/session";
import { Badge, ErrorBox, RequireSession } from "@/components/ui";
import { request, toInstant } from "@/lib/api";
import { formatInstant, formatValue, validity } from "@/lib/format";
import type { RetrievalResult } from "@/lib/types";

export default function SearchPage() {
  const { session } = useSession();
  const [text, setText] = useState("credit limit of customer-991");
  const [validAt, setValidAt] = useState("");
  const [knownAt, setKnownAt] = useState("");
  const [result, setResult] = useState<RetrievalResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await request<RetrievalResult>(session, "/search", {
          method: "POST",
          body: { query: text, limit: 10, valid_at: toInstant(validAt), known_at: toInstant(knownAt) },
        }),
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <RequireSession>
      <h1>Hybrid temporal search</h1>
      <section>
        <form onSubmit={submit}>
          <label>
            Query
            <input value={text} onChange={(e) => setText(e.target.value)} required style={{ minWidth: 320 }} />
          </label>
          <label>
            Valid at (default now)
            <input type="datetime-local" value={validAt} onChange={(e) => setValidAt(e.target.value)} />
          </label>
          <label>
            As known at (default latest)
            <input type="datetime-local" value={knownAt} onChange={(e) => setKnownAt(e.target.value)} />
          </label>
          <button disabled={busy}>Search</button>
        </form>
      </section>
      <ErrorBox error={error} />
      {result ? (
        <section className="scroll">
          <p className="muted">
            Valid at {formatInstant(result.valid_at)}, known at {formatInstant(result.known_at) ?? "latest"} · vector
            search <Badge tone={result.vector_search === "used" ? "ok" : "warn"}>{result.vector_search}</Badge> ·
            cache {result.cache} · scopes {result.privacy_scopes.join(", ")}
          </p>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Entity</th>
                <th>Property</th>
                <th>Value</th>
                <th>Valid</th>
                <th>Source</th>
                <th>Score (vector / text / trust)</th>
              </tr>
            </thead>
            <tbody>
              {result.results.map((r, i) => (
                <tr key={r.version.id}>
                  <td>{i + 1}</td>
                  <td>
                    <Link
                      href={`/entities?type=${encodeURIComponent(r.entity_type)}&id=${encodeURIComponent(r.external_id)}`}
                    >
                      {r.entity_type}:{r.external_id}
                    </Link>
                  </td>
                  <td>
                    {r.property} <span className="muted">v{r.version.version}</span>
                  </td>
                  <td className="mono">{formatValue(r.version.value)}</td>
                  <td>{validity(r.version.valid_from, r.version.valid_until)}</td>
                  <td>
                    {r.source_name} <span className="muted">({r.version.authority})</span>
                  </td>
                  <td className="mono">
                    {r.ranking.score.toFixed(4)} ({r.ranking.vector_rank ?? "–"} / {r.ranking.text_rank ?? "–"} /{" "}
                    {r.ranking.trust.toFixed(2)})
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.results.length === 0 ? <p className="muted">No facts you may see match.</p> : null}
        </section>
      ) : null}
    </RequireSession>
  );
}
