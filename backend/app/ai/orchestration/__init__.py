"""LangChain orchestration over ContextLedger's own components.

LangChain (``langchain-core``) provides message types, prompt templates, runnable
composition, callbacks and (for the agent) tool abstractions. It does NOT own
model access (``GenerationProvider`` does: timeouts, retries, typed errors), and
it never sees anything the trusted services have not already authorized.
LangGraph is intentionally not used.
"""
