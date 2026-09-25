"""Prompt for the optional LLM review of semantic contradictions within one entity."""

from typing import Any, Final

from app.ai.prompts.registry import PromptTemplate, register

REVIEW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["left", "right", "explanation"],
                "properties": {
                    "left": {"type": "string", "description": "A fact label, e.g. F1"},
                    "right": {"type": "string", "description": "Another fact label, e.g. F3"},
                    "explanation": {"type": "string"},
                },
            },
        }
    },
}

CONTRADICTION_REVIEW_V1: Final = register(
    PromptTemplate(
        version="contradiction-review-v1",
        purpose="Suggest pairs of current facts about one entity that cannot both be true.",
        system="""You review the current facts about one entity in ContextLedger.
Find pairs of facts about DIFFERENT properties that cannot both be true at the
same time (for example customer_status = ACTIVE and account_closed = true).

Rules:
1. Use only the facts given. Refer to them by label (F1, F2, ...).
2. Report a pair only if the two values are logically incompatible. Different
   values of unrelated properties are not contradictions. Do not guess.
3. Explain each finding in one or two sentences, naming both values.
4. If there are no contradictions, return an empty list.

Respond with JSON matching the schema.""",
        user="Entity: {entity}\n\nFacts:\n{facts}",
        output_schema=REVIEW_SCHEMA,
        placeholders=frozenset({"entity", "facts"}),
    )
)
