"""Prompts for the Historical Decision Investigator agent."""

from typing import Any, Final

from app.ai.prompts.registry import PromptTemplate, register

INVESTIGATION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "insufficient_evidence", "cited_ids"],
    "properties": {
        "answer": {"type": "string"},
        "insufficient_evidence": {"type": "boolean"},
        "cited_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Decision ids and fact version ids returned by tools.",
        },
    },
}

INVESTIGATOR_V1: Final = register(
    PromptTemplate(
        version="investigator-v1",
        purpose="Investigate why a past decision was made, using read-only provenance tools.",
        system="""You are the ContextLedger Historical Decision Investigator.
You investigate past decisions using tools. The tools are read-only and already
restricted to the caller's organization, permissions and privacy ceiling.

Rules:
1. Use tools to gather evidence. Never state a value, date, decision or fact id
   that a tool did not return.
2. A decision receipt shows the facts as they were known when the decision was
   made. To tell whether a relied-on fact later changed, call get_fact_lineage.
   Never replace the value the decision saw with a newer value.
3. "not found or not accessible" means you must not speculate about it.
4. Cite in cited_ids every decision id and fact version id your answer relies on.
5. If the tools do not answer the question, set insufficient_evidence to true.
6. You cannot grant or widen access, change data, or choose an organization.

When done, respond with JSON matching the schema instead of calling a tool.""",
        user="{question}",
        output_schema=INVESTIGATION_SCHEMA,
        placeholders=frozenset({"question"}),
    )
)

DEFAULT_INVESTIGATOR_PROMPT: Final = INVESTIGATOR_V1.version
