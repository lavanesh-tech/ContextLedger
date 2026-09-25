"use client";

import { useState, type FormEvent } from "react";

import { useSession } from "@/components/session";
import { Badge, ErrorBox, RequireSession } from "@/components/ui";
import { request, toInstant } from "@/lib/api";
import { validity } from "@/lib/format";
import type { GroundedAnswer } from "@/lib/types";

export default function AskPage() {
  const { session } = useSession();
  const [question, setQuestion] = useState("What is the credit limit of customer-991?");
  const [validAt, setValidAt] = useState("");
  const [answer, setAnswer] = useState<GroundedAnswer | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      setAnswer(
        await request<GroundedAnswer>(session, "/answers", {
          method: "POST",
          body: { question, valid_at: toInstant(validAt) },
        }),
      );
    } catch (e) {
      setAnswer(null);
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const tone = answer?.status === "answered" ? "ok" : answer?.status === "ungrounded" ? "bad" : "warn";

  return (
    <RequireSession>
      <h1>Ask (grounded answer)</h1>
      <section>
        <form onSubmit={submit}>
          <label>
            Question
            <input value={question} onChange={(e) => setQuestion(e.target.value)} required style={{ minWidth: 380 }} />
          </label>
          <label>
            As of (default now)
            <input type="datetime-local" value={validAt} onChange={(e) => setValidAt(e.target.value)} />
          </label>
          <button disabled={busy}>{busy ? "Asking…" : "Ask"}</button>
        </form>
        <p className="muted">
          Needs the LLM enabled on the server (503 otherwise). The model only sees facts you may see, and every
          citation is checked.
        </p>
      </section>
      <ErrorBox error={error} />
      {answer ? (
        <section>
          <p>
            <Badge tone={tone}>{answer.status}</Badge>
          </p>
          {answer.answer ? <p>{answer.answer}</p> : <p className="muted">No answer shown.</p>}
          {answer.citations.length ? (
            <table>
              <tbody>
                {answer.citations.map((c) => (
                  <tr key={c.label}>
                    <td>[{c.label}]</td>
                    <td>
                      {c.entity_type}:{c.external_id} {c.property}
                    </td>
                    <td>{c.source_name}</td>
                    <td>{validity(c.valid_from, c.valid_until)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          <p className="muted">
            {answer.retrieval.facts_supplied} fact(s) supplied
            {answer.generation
              ? ` · ${answer.generation.model} · ${answer.generation.prompt_version} · ${answer.generation.input_tokens}+${answer.generation.output_tokens} tokens`
              : " · no model call"}
          </p>
        </section>
      ) : null}
    </RequireSession>
  );
}
