"""Tiny prompt loader with deployment-time overrides.

Lookup order:

1. ``<PROMPTS_DIR>/<name>.md`` (if ``PROMPTS_DIR`` is set and the file exists)
2. Packaged default under ``templates/<name>.md``

Templates use Python's ``str.format`` placeholder style (``{job_name}`` etc.)
so there is no new templating dependency.
"""

from __future__ import annotations

from pathlib import Path


class PromptNotFoundError(FileNotFoundError):
    """Raised when a requested prompt name has no file in any search path."""


class PromptLoader:
    """Resolve and cache prompt templates by name."""

    def __init__(self, custom_dir: str | Path | None = None) -> None:
        self._custom_dir = Path(custom_dir) if custom_dir else None
        self._default_dir = Path(__file__).resolve().parent / "templates"
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]

        candidates: list[Path] = []
        if self._custom_dir is not None:
            candidates.append(self._custom_dir / f"{name}.md")
        candidates.append(self._default_dir / f"{name}.md")

        for path in candidates:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self._cache[name] = text
                return text

        raise PromptNotFoundError(
            f"Prompt {name!r} not found. Searched: "
            + ", ".join(str(p) for p in candidates),
        )

    def render(self, name: str, /, **params: object) -> str:
        template = self.load(name)
        return template.format(**params)
