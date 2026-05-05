"""Provider-agnostic LLM interface.

Callers import only :class:`LLMClient` and :class:`ChatMessage`. The concrete
vendor (Groq, OpenAI, Anthropic, Ollama, …) is selected at runtime by
:func:`create_llm` based on ``LLM_PROVIDER``. Adding a new vendor is a matter
of writing one file under ``providers/`` and wiring it in ``factory.py``.
"""

from .base import ChatMessage, LLMClient
from .factory import create_llm

__all__ = ["ChatMessage", "LLMClient", "create_llm"]
