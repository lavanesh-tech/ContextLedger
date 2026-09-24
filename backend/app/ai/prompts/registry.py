"""Prompt registry: stable ids -> immutable prompt templates.

A prompt is identified by a version string (``grounded-answer-v2``) that is
recorded with every generation, so an answer, an evaluation result or a trace
always says exactly which prompt produced it. Prompts are never edited in place:
a change is a new version, and the evaluation suite compares versions.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any, Final


class PromptNotFoundError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    version: str
    purpose: str
    system: str
    user: str  # str.format template
    output_schema: dict[str, Any] | None = None
    changes: str = ""  # what changed compared with the previous version, and why
    placeholders: frozenset[str] = field(default_factory=frozenset)

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the prompt text: detects an accidental in-place edit."""
        material = "\x00".join((self.version, self.system, self.user, repr(self.output_schema)))
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def render_user(self, **values: str) -> str:
        missing = self.placeholders - values.keys()
        if missing:
            raise ValueError(f"missing prompt values: {sorted(missing)}")
        return self.user.format(**values)


_REGISTRY: dict[str, PromptTemplate] = {}


def register(template: PromptTemplate) -> PromptTemplate:
    if template.version in _REGISTRY:
        raise ValueError(f"prompt {template.version!r} is already registered")
    _REGISTRY[template.version] = template
    return template


def get_prompt(version: str) -> PromptTemplate:
    _load()
    try:
        return _REGISTRY[version]
    except KeyError:
        raise PromptNotFoundError(version) from None


def prompt_versions(prefix: str = "") -> list[str]:
    _load()
    return sorted(v for v in _REGISTRY if v.startswith(prefix))


_LOADED: Final[list[bool]] = [False]


def _load() -> None:
    if not _LOADED[0]:
        from app.ai.prompts import grounded_answer  # noqa: F401  (registers on import)

        _LOADED[0] = True
