"""Tests for the CI-source factory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_LISTENER_ROOT = Path(__file__).resolve().parent.parent
if str(_LISTENER_ROOT) not in sys.path:
    sys.path.insert(0, str(_LISTENER_ROOT))

os.environ.setdefault("JENKINS_BASE_URL", "http://example.invalid")
os.environ.setdefault("JENKINS_USER", "u")
os.environ.setdefault("JENKINS_API_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("WORKER_INGEST_URL", "http://example.invalid/ingest/failure")
os.environ.setdefault("WORKER_INGEST_API_KEY", "t")

import pytest  # noqa: E402

from app.ci import create_ci_source  # noqa: E402
from app.ci.providers.jenkins import JenkinsCISource  # noqa: E402
from app.config import Settings  # noqa: E402


def _settings(**overrides) -> Settings:
    for k, v in overrides.items():
        os.environ[k] = str(v)
    return Settings()  # reads env


def test_factory_builds_jenkins_source():
    settings = _settings(CI_PROVIDER="jenkins")
    source = create_ci_source(settings)
    assert isinstance(source, JenkinsCISource)


def test_factory_rejects_unknown_provider():
    settings = _settings(CI_PROVIDER="gitlab-ci")
    with pytest.raises(ValueError, match="Unknown CI_PROVIDER"):
        create_ci_source(settings)
