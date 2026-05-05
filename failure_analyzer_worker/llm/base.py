"""LLM provider-agnostic protocol.

The rest of the codebase depends only on :class:`LLMClient` and the tiny
:class:`ChatMessage` value object. Providers wrap their SDKs behind this
surface so swapping vendors is a config change, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence, runtime_checkable

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    """Minimal neutral chat message, decoupled from any SDK's message class."""

    role: Role
    content: str


@runtime_checkable
class LLMClient(Protocol):
    """Send a sequence of chat messages, receive a single string response.

    Kept intentionally narrow: anything richer (streaming, tool calls,
    structured output) is added as opt-in capabilities on provider classes
    without breaking this base protocol.
    """

    def invoke(self, messages: Sequence[ChatMessage]) -> str:  # pragma: no cover - protocol
        ...
