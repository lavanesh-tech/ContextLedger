// Response shapes of the ContextLedger REST API (docs/api/openapi.json) that the UI reads.

export type PrivacyScope = "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED";

export interface VersionSnapshot {
  id: string;
  fact_id: string;
  version: number;
  value: unknown;
  source_id: string;
  valid_from: string;
  valid_until: string | null;
  observed_at: string;
  recorded_at: string;
  valid_until_recorded_at: string | null;
  supersedes_id: string | null;
  authority: number;
  confidence: string;
  privacy_scope: PrivacyScope;
}

export interface Ranking {
  fact_version_id: string;
  vector_rank: number | null;
  vector_distance: number | null;
  text_rank: number | null;
  text_score: number | null;
  rrf_score: number;
  trust: number;
  score: number;
}

export interface RetrievedFact {
  version: VersionSnapshot;
  entity_type: string;
  external_id: string;
  property: string;
  source_name: string;
  source_type: string;
  ranking: Ranking;
}

export interface RetrievalResult {
  query: string;
  valid_at: string;
  known_at: string | null;
  vector_search: "used" | "unavailable";
  embedding_model: string;
  privacy_scopes: PrivacyScope[];
  results: RetrievedFact[];
  cache: "hit" | "miss" | "off";
}

export interface TimelineEntry {
  property: string;
  version: VersionSnapshot;
}

export interface ReceiptFact {
  fact_version_id: string;
  position: number;
  relied_on: boolean;
  redacted: boolean;
  privacy_scope: PrivacyScope;
  entity_type: string | null;
  external_id: string | null;
  property: string | null;
  source_name: string | null;
  version: VersionSnapshot | null;
  revoked_at: string | null;
}

export interface DecisionReceipt {
  decision_id: string;
  action: string;
  outcome: unknown;
  rationale: string | null;
  agent: string | null;
  decided_at: string;
  context: { snapshot_id: string; query: string; valid_at: string; known_at: string };
  facts: ReceiptFact[];
  receipt_sha256: string;
  integrity_verified: boolean;
}

export interface ContradictionSide {
  fact_id: string;
  property: string;
  source_id: string;
  source_name: string;
  version: VersionSnapshot;
}

export interface Contradiction {
  id: string;
  entity_type: string;
  external_id: string;
  kind: "value_conflict" | "semantic";
  detector: string;
  explanation: string;
  status: "open" | "resolved" | "dismissed";
  preferred_version_id: string | null;
  left: ContradictionSide;
  right: ContradictionSide;
  detected_at: string;
  resolution_note: string | null;
}

export interface ImpactedDecision {
  decision_id: string;
  action: string;
  agent: string | null;
  decided_at: string;
  relied_on: boolean;
  decided_after_revocation: boolean;
}

export interface RevocationReport {
  revocation_id: string;
  fact_id: string;
  entity_type: string;
  external_id: string;
  property: string;
  version: VersionSnapshot;
  reason: string;
  revoked_at: string;
  decisions: ImpactedDecision[];
  decisions_relied_on: number;
  decisions_with_version_in_context: number;
}

export interface Citation {
  label: string;
  fact_version_id: string;
  source_name: string;
  entity_type: string;
  external_id: string;
  property: string;
  valid_from: string;
  valid_until: string | null;
}

export interface GroundedAnswer {
  question: string;
  status: "answered" | "insufficient_evidence" | "ungrounded";
  answer: string | null;
  citations: Citation[];
  inferences: string[];
  rejected_citations: string[];
  retrieval: { facts_supplied: number; vector_search: string; valid_at: string };
  generation: { model: string; prompt_version: string; input_tokens: number; output_tokens: number; latency_ms: number } | null;
}

/** RFC 9457 problem details, as returned by every API error. */
export interface Problem {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code?: string;
  correlation_id?: string;
}
