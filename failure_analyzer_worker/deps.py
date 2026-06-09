"""Composition root: build the dependency graph from settings.

The FastAPI app (and the CLI helpers) both call :func:`build_deps` once and
then hand the returned :class:`Deps` to :func:`failure_analyzer_worker.graph.build_graph`.

This is the only place providers are wired together. Tests can build a
custom ``Deps`` with fakes instead of calling the factories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .embeddings import Embedder, create_embedder
from .filtering import Filter, create_filter
from .llm import LLMClient, create_llm
from .prompts import PromptLoader
from .vectorstore import SolutionRepository, create_solution_repository

if TYPE_CHECKING:
    from .config import WorkerSettings


@dataclass
class Deps:
    """Everything the graph + HTTP layer need, pre-wired."""

    settings: "WorkerSettings"
    llm: LLMClient
    embedder: Embedder
    solutions: SolutionRepository
    prompts: PromptLoader
    filter: Filter


def build_deps(settings: "WorkerSettings") -> Deps:
    """Construct providers once from a ``WorkerSettings`` instance."""
    embedder = create_embedder(settings)
    return Deps(
        settings=settings,
        llm=create_llm(settings),
        embedder=embedder,
        solutions=create_solution_repository(settings, embedder),
        prompts=PromptLoader(settings.prompts_dir or None),
        filter=create_filter(settings),
    )
