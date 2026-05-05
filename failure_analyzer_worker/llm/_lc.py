"""Private helpers that bridge :class:`ChatMessage` to LangChain messages.

All providers currently reuse LangChain's SDK wrappers under the hood
(``langchain_groq``, ``langchain_openai``, …). Keeping the conversion in
one place means the public ``LLMClient`` / ``ChatMessage`` surface stays
LangChain-free and can be preserved if we later drop LangChain for a
subset of providers.
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from .base import ChatMessage


def to_lc_messages(messages: Sequence[ChatMessage]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in messages:
        if m.role == "system":
            out.append(SystemMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
        else:
            out.append(HumanMessage(content=m.content))
    return out


def extract_text(content: Any) -> str:
    """LangChain responses may be str or a list of content-parts; flatten to str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)
