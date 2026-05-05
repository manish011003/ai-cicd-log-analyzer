"""Prompt templates loaded from text files (overridable at deploy time).

Operators can tune prompts without editing code by pointing ``PROMPTS_DIR``
at a directory containing the same filenames (``system.md``,
``with_context.md``, ``fresh.md``, ``clarify_system.md``). Missing files
fall back to the defaults shipped under ``templates/``.
"""

from .loader import PromptLoader

__all__ = ["PromptLoader"]
