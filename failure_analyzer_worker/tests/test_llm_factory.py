"""Unit tests for ``llm.factory`` and the message bridge.

These tests never touch a real LLM — they inject a minimal fake model into
provider classes and assert the factory routes by ``LLM_PROVIDER`` and
raises clear errors when a key is missing or an unknown provider is used.
"""

from __future__ import annotations

import os

os.environ.setdefault("WORKER_API_KEY", "test")

import pytest  # noqa: E402

from failure_analyzer_worker.config import WorkerSettings  # noqa: E402
from failure_analyzer_worker.llm import ChatMessage  # noqa: E402
from failure_analyzer_worker.llm._lc import extract_text, to_lc_messages  # noqa: E402
from failure_analyzer_worker.llm.factory import create_llm  # noqa: E402


class _FakeAIMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatGroq:
    """Drop-in replacement for ``langchain_groq.ChatGroq`` used in tests."""

    last_init_kwargs: dict | None = None
    last_invoke_messages: list | None = None

    def __init__(self, **kwargs) -> None:
        _FakeChatGroq.last_init_kwargs = kwargs

    def invoke(self, messages):
        _FakeChatGroq.last_invoke_messages = messages
        return _FakeAIMessage("hello world")


def _settings(**overrides) -> WorkerSettings:
    defaults = {
        "LLM_PROVIDER": "groq",
        "LLM_MODEL": "llama-3.3-70b-versatile",
        "LLM_API_KEY": "dummy-key",
        "LLM_TEMPERATURE": "0.1",
        "LLM_MAX_TOKENS": "128",
        "LLM_TLS_VERIFY": "1",
        "WORKER_API_KEY": "test",
        "GROQ_API_KEY": "",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        os.environ[k] = str(v)
    return WorkerSettings()


# ── factory routing ─────────────────────────────────────────────────────────


def test_factory_rejects_unknown_provider():
    settings = _settings(LLM_PROVIDER="superllm-9000")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        create_llm(settings)


def test_factory_wires_groq_provider(monkeypatch):
    import langchain_groq

    monkeypatch.setattr(langchain_groq, "ChatGroq", _FakeChatGroq)
    settings = _settings(LLM_PROVIDER="groq", LLM_API_KEY="sk-real")
    client = create_llm(settings)

    reply = client.invoke([ChatMessage(role="user", content="hi")])
    assert reply == "hello world"
    assert _FakeChatGroq.last_init_kwargs is not None
    assert _FakeChatGroq.last_init_kwargs["groq_api_key"] == "sk-real"


def test_groq_provider_falls_back_to_legacy_env(monkeypatch):
    import langchain_groq

    monkeypatch.setattr(langchain_groq, "ChatGroq", _FakeChatGroq)
    settings = _settings(LLM_PROVIDER="groq", LLM_API_KEY="", GROQ_API_KEY="legacy-key")
    create_llm(settings)
    assert _FakeChatGroq.last_init_kwargs["groq_api_key"] == "legacy-key"


def test_groq_provider_raises_when_no_key(monkeypatch):
    import langchain_groq

    monkeypatch.setattr(langchain_groq, "ChatGroq", _FakeChatGroq)
    settings = _settings(LLM_PROVIDER="groq", LLM_API_KEY="", GROQ_API_KEY="")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        create_llm(settings)


# ── message bridge ──────────────────────────────────────────────────────────


def test_to_lc_messages_maps_all_roles():
    msgs = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="u"),
        ChatMessage(role="assistant", content="a"),
    ]
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    out = to_lc_messages(msgs)
    assert isinstance(out[0], SystemMessage)
    assert isinstance(out[1], HumanMessage)
    assert isinstance(out[2], AIMessage)
    assert [m.content for m in out] == ["s", "u", "a"]


def test_extract_text_handles_string_list_and_dicts():
    assert extract_text("plain") == "plain"
    assert extract_text(["a", "b"]) == "a\nb"
    assert extract_text([{"text": "hi"}, {"text": ""}, {"no_text": 1}]) == "hi"
    assert extract_text(42) == "42"
