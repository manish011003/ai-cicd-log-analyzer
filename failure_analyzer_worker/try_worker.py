"""End-to-end pipeline check: filter → fingerprint → ES kNN → LLM analysis.

Runs the worker's LangGraph **in-process** (no FastAPI server needed) so you can
preview what the worker would return for a given log without going through the
listener / dashboard.

Usage (from repo root)::

    python failure_analyzer_worker/try_worker.py path/to/log.txt
    python failure_analyzer_worker/try_worker.py --stage Build --job demo/web --build 42 path/to/log.txt
    type log.txt | python failure_analyzer_worker/try_worker.py
    python failure_analyzer_worker/try_worker.py --filter-only path/to/log.txt
    python failure_analyzer_worker/try_worker.py --no-llm path/to/log.txt   # skip Groq, show filter+fingerprint+ES only
    python failure_analyzer_worker/try_worker.py --json path/to/log.txt     # raw JSON dump

Requirements:
    LLM_API_KEY (or legacy GROQ_API_KEY) in failure_analyzer_worker/.env
        (skip with --no-llm or --filter-only)
    Elasticsearch reachable at ELASTICSEARCH_URL
        (optional; failures degrade gracefully to no matches)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SAMPLE = """\
[Pipeline] { (Build)
[INFO] Scanning for projects...
[INFO] Building demo 1.0
[INFO] Downloading from central: https://repo.maven.apache.org/foo.jar
[INFO] Downloading from central: https://repo.maven.apache.org/bar.jar
2026-04-15T10:00:00Z INFO   noisy line
2026-04-15T10:00:00Z INFO   noisy line
ERROR Application failed to start
Caused by: java.net.ConnectException: Connection refused
\tat com.example.Client.call(Client.java:42)
\tat com.example.Runner.main(Runner.java:10)
[INFO] BUILD FAILURE
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


def _hr(title: str) -> str:
    bar = "─" * max(0, 76 - len(title) - 2)
    return f"\n── {title} {bar}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the worker pipeline (filter → fingerprint → ES → LLM) on a log.",
    )
    parser.add_argument("path", nargs="?", default=None,
                        help="log file to read (omit to use stdin or built-in sample)")
    parser.add_argument("--stage", default="Build", help="stage name (default: Build)")
    parser.add_argument("--job", default="manual/check", help="job full name (default: manual/check)")
    parser.add_argument("--build", type=int, default=0, help="build number (default: 0)")
    parser.add_argument("--filter-only", action="store_true",
                        help="run only the filter (same as try_filter.py)")
    parser.add_argument("--no-llm", action="store_true",
                        help="run filter + fingerprint + ES search but SKIP the LLM")
    parser.add_argument("--json", action="store_true",
                        help="dump the full pipeline state as JSON instead of pretty output")
    args = parser.parse_args()

    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

    raw, used_sample = _read_input(args.path)
    if used_sample:
        sys.stderr.write(
            "(no file and stdin is a TTY — using built-in sample; "
            "pass a file path or pipe a log)\n",
        )

    from failure_analyzer_worker import log_processor
    from failure_analyzer_worker.log_processor import (
        LogProcessor,
        filter_logs,
        generate_fingerprint,
        search_similar_solutions,
    )

    body = filter_logs(raw)
    filtered_with_meta = LogProcessor().format_with_metadata(raw, body)

    if args.filter_only:
        if args.json:
            print(json.dumps({"filtered_logs": filtered_with_meta, "body_only": body}, indent=2))
            return
        print(_hr("FILTERED LOG (with metadata)"))
        print(filtered_with_meta)
        return

    fingerprint = generate_fingerprint(body, stage_name=args.stage)

    es_matches: list[dict] = []
    es_error: str | None = None
    try:
        es_matches = search_similar_solutions(fingerprint)
    except Exception as exc:  # noqa: BLE001
        es_error = f"{type(exc).__name__}: {exc}"

    state: dict = {
        "filtered_logs": filtered_with_meta,
        "fingerprint": fingerprint,
        "es_matches": es_matches,
        "es_error": es_error,
    }

    if not args.no_llm:
        s = log_processor.settings
        if not (s.llm_api_key or s.groq_api_key):
            sys.stderr.write(
                f"error: no API key set for LLM_PROVIDER={s.llm_provider!r}. "
                "Set LLM_API_KEY (or legacy GROQ_API_KEY for groq) in "
                "failure_analyzer_worker/.env, or rerun with --no-llm / --filter-only.\n",
            )
            sys.exit(2)

        from failure_analyzer_worker.graph import get_default_graph

        result = get_default_graph().invoke({
            "raw_logs": raw,
            "stage_name": args.stage,
            "job_name": args.job,
            "build_number": args.build,
        })
        state.update({
            "analysis": result.get("analysis", ""),
            "suggested_fix": result.get("suggested_fix", ""),
            "recommendation": result.get("recommendation", ""),
            "matched_solution": result.get("matched_solution", ""),
            "match_score": result.get("match_score", 0.0),
        })

    if args.json:
        print(json.dumps(state, indent=2, default=str))
        return

    print(_hr("FILTERED LOG (with metadata)"))
    print(filtered_with_meta)

    print(_hr("FINGERPRINT (used for ES kNN)"))
    print(fingerprint)

    print(_hr(f"ES MATCHES (top {len(es_matches)})"))
    if es_error:
        print(f"[ES unreachable: {es_error}]")
        print("(no matches — analyze_fresh path will be taken)")
    elif not es_matches:
        print("(no matches above SIMILARITY_THRESHOLD — analyze_fresh path will be taken)")
    else:
        for i, m in enumerate(es_matches, 1):
            print(f"  {i}. score={m.get('score', 0):.3f}  job={m.get('job_name', '?')} "
                  f"#{m.get('build_number', '?')}  stage={m.get('stage_name', '?')}")
            sol = (m.get("solution") or "").strip().splitlines()
            if sol:
                preview = sol[0][:140]
                print(f"     solution: {preview}{'…' if len(sol[0]) > 140 else ''}")

    if args.no_llm:
        print(_hr("LLM"))
        print("(skipped because --no-llm was set)")
        return

    route = "analyze_with_context" if es_matches else "analyze_fresh"
    print(_hr(f"LLM ANALYSIS  (route: {route},  recommendation: {state.get('recommendation', '?')})"))
    analysis = state.get("analysis", "")
    if analysis:
        print(analysis)
    else:
        print("(empty)")

    print(_hr("SUGGESTED FIX"))
    fix = state.get("suggested_fix", "")
    if fix:
        print(fix)
    else:
        print("(empty — splitter did not find a Step-by-Step Fix marker)")

    if state.get("matched_solution"):
        print(_hr(f"MATCHED PAST SOLUTION (score={state.get('match_score', 0):.3f})"))
        print(state["matched_solution"])


if __name__ == "__main__":
    main()
