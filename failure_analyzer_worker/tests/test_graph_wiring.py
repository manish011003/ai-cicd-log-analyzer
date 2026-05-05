"""End-to-end graph test with in-memory fakes for LLM + vector store.

Exercises ``AnalysisGraph`` via the public API (``invoke`` /
``chat_clarification``) without any network, embeddings, or elasticsearch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Sequence

os.environ.setdefault("WORKER_API_KEY", "test")

from failure_analyzer_worker.config import WorkerSettings  # noqa: E402
from failure_analyzer_worker.deps import Deps  # noqa: E402
from failure_analyzer_worker.graph import build_graph  # noqa: E402
from failure_analyzer_worker.llm import ChatMessage  # noqa: E402
from failure_analyzer_worker.prompts import PromptLoader  # noqa: E402
from failure_analyzer_worker.vectorstore.base import Solution, SolutionMatch  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────


@dataclass
class FakeLLM:
    reply: str = ""
    calls: list[Sequence[ChatMessage]] = field(default_factory=list)

    def invoke(self, messages: Sequence[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.reply


@dataclass
class FakeEmbedder:
    dimensions_value: int = 8

    @property
    def dimensions(self) -> int:
        return self.dimensions_value

    def embed(self, text: str) -> list[float]:  # pragma: no cover - unused here
        return [0.0] * self.dimensions_value

    def embed_batch(self, texts):  # pragma: no cover - unused here
        return [[0.0] * self.dimensions_value for _ in texts]


@dataclass
class FakeSolutionRepo:
    matches: list[SolutionMatch] = field(default_factory=list)
    stored: list[Solution] = field(default_factory=list)
    last_doc_id: str | None = None

    def ensure_ready(self) -> None:
        pass

    def search(self, fingerprint: str) -> list[SolutionMatch]:
        return list(self.matches)

    def store(self, solution: Solution, *, doc_id: str | None = None) -> str:
        self.stored.append(solution)
        self.last_doc_id = doc_id
        return doc_id or "fake-id"

    def prune(self, older_than_days: int) -> None:
        pass


def _build_deps(llm: FakeLLM, repo: FakeSolutionRepo) -> Deps:
    return Deps(
        settings=WorkerSettings(),
        llm=llm,
        embedder=FakeEmbedder(),
        solutions=repo,
        prompts=PromptLoader(),
    )


# ── Tests ────────────────────────────────────────────────────────────────────


SAMPLE_RAW = """\
[Pipeline] stage (Build)
[INFO] Scanning for projects...
ERROR Application failed to start
Caused by: java.net.ConnectException: Connection refused
\tat com.example.Client.call(Client.java:42)
[INFO] BUILD FAILURE
"""

SAMPLE_REPLY = """\
## Analysis
Something went wrong because a downstream service was unreachable.

## Step-by-Step Fix
1. Check the connection.
2. Restart the service.

## Verify
curl http://example.invalid
"""


def test_analyze_fresh_route_when_no_matches():
    llm = FakeLLM(reply=SAMPLE_REPLY)
    repo = FakeSolutionRepo(matches=[])
    graph = build_graph(_build_deps(llm, repo))

    out = graph.invoke(
        {
            "raw_logs": SAMPLE_RAW,
            "stage_name": "Build",
            "job_name": "team/demo",
            "build_number": 42,
        },
    )

    assert out["recommendation"] == "fresh_analysis"
    assert "Something went wrong" in out["analysis"]
    assert "Step-by-Step Fix" in out["suggested_fix"]
    assert out["matched_solution"] == ""
    assert out["match_score"] == 0.0
    # The LLM was called exactly once in the fresh path.
    assert len(llm.calls) == 1


def test_analyze_with_context_route_when_strong_match():
    llm = FakeLLM(reply=SAMPLE_REPLY)
    match = SolutionMatch(
        score=0.95,
        solution="Restart the downstream service and retry.",
        fingerprint_text="stage: Build | exceptions: ConnectException",
    )
    repo = FakeSolutionRepo(matches=[match])
    graph = build_graph(_build_deps(llm, repo))

    out = graph.invoke(
        {
            "raw_logs": SAMPLE_RAW,
            "stage_name": "Build",
            "job_name": "team/demo",
            "build_number": 42,
        },
    )

    assert out["recommendation"] == "verified_past_solution"
    assert out["matched_solution"] == "Restart the downstream service and retry."
    assert out["match_score"] == 0.95


def test_chat_clarification_passes_history_to_llm():
    llm = FakeLLM(reply="follow-up answer")
    graph = build_graph(_build_deps(llm, FakeSolutionRepo()))

    answer = graph.chat_clarification(
        [
            {"role": "user", "content": "Why did it fail?"},
            {"role": "assistant", "content": "Because of X."},
            {"role": "user", "content": "Give me a different fix."},
        ],
        fingerprint="fp",
        log_excerpt="excerpt",
        job_name="team/demo",
        stage_name="Build",
        build_number=42,
        analysis="old analysis",
        suggested_fix="old fix",
    )
    assert answer == "follow-up answer"
    assert len(llm.calls) == 1
    sent = llm.calls[0]
    assert sent[0].role == "system"
    # System prompt should include the context blocks we passed in.
    assert "Original Analysis" in sent[0].content
    assert "Filtered log excerpt" in sent[0].content
    # The history is forwarded verbatim.
    assert [m.role for m in sent[1:]] == ["user", "assistant", "user"]
