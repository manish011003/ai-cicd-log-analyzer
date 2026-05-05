"""Tests for the prompt loader: defaults + override directory + missing names."""

from __future__ import annotations

import os

os.environ.setdefault("WORKER_API_KEY", "test")

import pytest  # noqa: E402

from failure_analyzer_worker.prompts.loader import PromptLoader, PromptNotFoundError  # noqa: E402


def test_loader_returns_packaged_default_prompts():
    loader = PromptLoader()
    system = loader.load("system")
    assert "senior CI/CD failure analyst" in system
    fresh = loader.render("fresh", job_name="j", build_number=1, stage_name="s", filtered_logs="x")
    assert "**Job:** j #1" in fresh
    assert "**Stage:** s" in fresh


def test_loader_prefers_custom_dir(tmp_path):
    (tmp_path / "system.md").write_text("CUSTOM SYSTEM", encoding="utf-8")
    loader = PromptLoader(custom_dir=tmp_path)
    assert loader.load("system") == "CUSTOM SYSTEM"
    # Unknown name in custom dir → falls back to packaged default.
    assert "## Analysis" in loader.load("with_context")


def test_loader_raises_for_unknown_name(tmp_path):
    loader = PromptLoader(custom_dir=tmp_path)
    with pytest.raises(PromptNotFoundError):
        loader.load("totally-made-up")
