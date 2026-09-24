"""LangChain runnables for grounded answers.

``grounded_answer_chain``:  ChatPromptTemplate (from a versioned prompt)
                              | ContextLedgerChatModel bound to the strict JSON schema
The chain takes the already-authorized, already-rendered facts as input. Parsing
and citation checking stay in deterministic code (app/ai/grounding.py).
"""

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from app.ai.orchestration.chat_model import ContextLedgerChatModel
from app.ai.prompts.registry import PromptTemplate
from app.ai.providers import OutputSchema


def chat_prompt(template: PromptTemplate) -> ChatPromptTemplate:
    """System text is literal (braces escaped); the user template keeps its placeholders."""
    system = template.system.replace("{", "{{").replace("}", "}}")
    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", template.user)])
    missing = template.placeholders - set(prompt.input_variables)
    if missing or set(prompt.input_variables) - template.placeholders:
        raise ValueError(f"prompt {template.version} placeholders do not match its template")
    return prompt


def grounded_answer_chain(
    template: PromptTemplate, model: ContextLedgerChatModel
) -> Runnable[dict[str, str], BaseMessage]:
    bound = model.bind(
        output_schema=(
            OutputSchema("grounded_answer", template.output_schema)
            if template.output_schema
            else None
        )
    )
    chain: Runnable[dict[str, str], BaseMessage] = chat_prompt(template) | bound
    return chain.with_config(
        run_name=f"grounded-answer[{template.version}]",
        tags=["contextledger", template.version],
        metadata={"prompt_version": template.version, "prompt_fingerprint": template.fingerprint},
    )
