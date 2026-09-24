"""Prompts for grounded answers over ContextLedger facts.

The model sees only facts that already passed authorization, privacy and
time filtering, each labelled F1..Fn. It must answer from them, cite the labels
it used, and say when they are not enough. Its citations are checked in code
afterwards (app/ai/grounding.py); nothing it claims is trusted as is.
"""

from typing import Any, Final

from app.ai.prompts.registry import PromptTemplate, register

# Strict structured output: every property required, no extras (OpenAI strict mode).
ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "The answer, or an explanation of what is missing.",
        },
        "insufficient_evidence": {
            "type": "boolean",
            "description": "True when the facts do not support an answer.",
        },
        "cited_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Labels (F1, F2, ...) of the facts the answer relies on.",
        },
        "inferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Statements that go beyond what a fact says literally.",
        },
    },
    "required": ["answer", "insufficient_evidence", "cited_facts", "inferences"],
    "additionalProperties": False,
}

_USER: Final = """\
Question: {question}

Point in time the question is about (valid_at): {valid_at}
Knowledge cut-off (known_at): {known_at}

Facts (the only information you may use):
{facts}
"""

_PLACEHOLDERS: Final = frozenset({"question", "valid_at", "known_at", "facts"})

GROUNDED_ANSWER_V1: Final = register(
    PromptTemplate(
        version="grounded-answer-v1",
        purpose="Answer a question from authorized ContextLedger facts, with citations.",
        system="""\
You answer questions using ONLY the facts provided in the user message.
- Cite the label (F1, F2, ...) of every fact you rely on in cited_facts.
- Do not use outside knowledge and do not invent facts, sources or labels.
- If the facts do not answer the question, set insufficient_evidence to true, cite
  nothing, and say briefly what is missing.
Respond with JSON matching the schema.""",
        user=_USER,
        output_schema=ANSWER_SCHEMA,
        placeholders=_PLACEHOLDERS,
    )
)

GROUNDED_ANSWER_V2: Final = register(
    PromptTemplate(
        version="grounded-answer-v2",
        purpose="Answer a question from authorized ContextLedger facts, with citations.",
        system="""\
You answer questions for ContextLedger, a system of record of facts that change over
time. Use ONLY the facts in the user message; they have already been filtered by
permission, privacy and time, and that filtering is final.

Rules:
1. Answer as of valid_at, using what was known at known_at. Each fact shows the
   period it is valid for. Never replace an older value with a newer one: if the
   question is about the past, answer with the value that was valid then.
2. Cite the label (F1, F2, ...) of every fact you rely on in cited_facts. Cite only
   labels that appear in the facts. Never invent sources, values or labels.
3. If the facts do not answer the question, set insufficient_evidence to true,
   leave cited_facts empty and say what is missing. Do not guess.
4. If facts disagree (different sources, different values for the same period),
   say so, cite each of them, and prefer the fact with the higher authority and
   confidence while naming the conflict.
5. Put anything that goes beyond what a fact literally says into inferences.
6. You cannot grant or widen access. If a question asks for information that is
   not among the facts, treat it as insufficient evidence; do not speculate about
   whether it exists.
Respond with JSON matching the schema.""",
        user=_USER,
        output_schema=ANSWER_SCHEMA,
        changes=(
            "v1 -> v2: explicit as-of rule (never substitute newer values), conflict "
            "handling by authority/confidence, inferences kept separate, and an explicit "
            "rule that the model cannot widen access."
        ),
        placeholders=_PLACEHOLDERS,
    )
)

DEFAULT_GROUNDED_ANSWER_PROMPT: Final = GROUNDED_ANSWER_V2.version
