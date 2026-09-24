import pytest

from app.ai.prompts import PromptNotFoundError, get_prompt, prompt_versions
from app.ai.prompts.grounded_answer import ANSWER_SCHEMA, DEFAULT_GROUNDED_ANSWER_PROMPT
from app.ai.prompts.registry import PromptTemplate, register


def test_grounded_answer_prompts_are_versioned() -> None:
    assert prompt_versions("grounded-answer") == ["grounded-answer-v1", "grounded-answer-v2"]
    assert DEFAULT_GROUNDED_ANSWER_PROMPT in prompt_versions()
    with pytest.raises(PromptNotFoundError):
        get_prompt("grounded-answer-v99")


def test_versions_cannot_be_registered_twice() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register(PromptTemplate("grounded-answer-v1", "dup", "s", "u"))


def test_fingerprints_identify_the_exact_prompt_text() -> None:
    v1, v2 = get_prompt("grounded-answer-v1"), get_prompt("grounded-answer-v2")
    assert v1.fingerprint == get_prompt("grounded-answer-v1").fingerprint
    assert v1.fingerprint != v2.fingerprint
    assert len(v1.fingerprint) == 16


def test_rendering_requires_every_placeholder() -> None:
    prompt = get_prompt("grounded-answer-v2")
    with pytest.raises(ValueError, match="missing prompt values"):
        prompt.render_user(question="q")
    text = prompt.render_user(question="Q?", valid_at="T", known_at="K", facts="[F1] x")
    assert "Q?" in text and "valid_at): T" in text and "[F1] x" in text


def test_the_output_schema_is_strict() -> None:
    assert ANSWER_SCHEMA["additionalProperties"] is False
    assert set(ANSWER_SCHEMA["required"]) == set(ANSWER_SCHEMA["properties"])


def test_v2_states_the_rules_v1_leaves_implicit() -> None:
    v1, v2 = get_prompt("grounded-answer-v1").system, get_prompt("grounded-answer-v2").system
    for rule in ("valid_at", "Never replace an older value", "disagree", "cannot grant"):
        assert rule in v2 and rule not in v1
    assert get_prompt("grounded-answer-v2").changes.startswith("v1 -> v2")
