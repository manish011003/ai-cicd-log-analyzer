"""Run ``filter_logs`` on a file or stdin (for quick manual checks).

Usage (from repo root)::

    python failure_analyzer_worker/try_filter.py path/to/log.txt
    python failure_analyzer_worker/try_filter.py --legacy path/to/log.txt
    type log.txt | python failure_analyzer_worker/try_filter.py

``--legacy`` sets ``LOG_FILTER_LEGACY=1`` before loading settings (old regex-only path).

``--metadata`` prepends the ``LogProcessor`` metadata header + trims body to ``LOG_BODY_MAX_CHARS``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SAMPLE = """\
[INFO] Building foo 1.0
[INFO] Downloading from central: https://repo.maven.apache.org/artifact.jar
[INFO] Downloading from central: https://repo.maven.apache.org/artifact2.jar
2026-04-15T10:00:00Z Same noise line
2026-04-15T10:00:01Z Same noise line
2026-04-15T10:00:02Z Same noise line
ERROR Application failed to start
Caused by: java.net.ConnectException: Connection refused
\tat com.example.Client.call(Client.java:42)
\tat com.example.Runner.main(Runner.java:10)
"""


def _read_input(path: str | None) -> tuple[str, bool]:
    """Returns (content, used_builtin_sample)."""
    if path:
        p = Path(path)
        if not p.is_file():
            sys.stderr.write(f"error: not a file: {p}\n")
            sys.exit(1)
        return p.read_text(encoding="utf-8", errors="replace"), False
    if not sys.stdin.isatty():
        return sys.stdin.read(), False
    return _SAMPLE, True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print filter_logs() output for manual pipeline checks.",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="use legacy global-regex-only filter (same as LOG_FILTER_LEGACY=1)",
    )
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="prepend [METADATA SUMMARY] block (same as worker graph / LogProcessor)",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="log file to read (omit: use stdin when piped, else a tiny sample)",
    )
    args = parser.parse_args()

    if args.legacy:
        os.environ["LOG_FILTER_LEGACY"] = "1"

    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

    raw, used_sample = _read_input(args.path)
    if used_sample:
        sys.stderr.write(
            "(no file and stdin is a TTY — using built-in sample; "
            "pass a file path or pipe a log)\n\n",
        )

    if args.metadata:
        from failure_analyzer_worker.log_processor import LogProcessor

        out = LogProcessor().process(raw)
    else:
        from failure_analyzer_worker.log_processor import filter_logs

        out = filter_logs(raw)
    sys.stdout.write(out)
    if out and not out.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
