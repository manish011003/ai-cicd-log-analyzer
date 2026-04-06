import json
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


CAPTURE_FILE = Path(__file__).resolve().parent / "captured_events.jsonl"
EXPECTED_API_KEY = "replace-me"

_FAILURE_PAYLOAD_KEY_ORDER = [
    "event_type",
    "jenkins_url",
    "job_full_name",
    "build_number",
    "build_url",
    "build_result",
    "timestamp",
    "correlation_id",
    "failed_stages",
]
_STAGE_KEY_ORDER = ["stage_name", "stage_id", "status", "log_excerpt"]


def _normalize_payload_for_capture(payload: dict) -> dict:
    """Put metadata keys first so logs/excerpts do not appear before job/build fields."""
    out: dict = {}
    for key in _FAILURE_PAYLOAD_KEY_ORDER:
        if key in payload:
            out[key] = payload[key]
    for key, value in payload.items():
        if key not in out:
            out[key] = value

    stages = out.get("failed_stages")
    if isinstance(stages, list):
        normalized_stages: list = []
        for item in stages:
            if not isinstance(item, dict):
                normalized_stages.append(item)
                continue
            stage_out: dict = {}
            for sk in _STAGE_KEY_ORDER:
                if sk in item:
                    stage_out[sk] = item[sk]
            for sk, sv in item.items():
                if sk not in stage_out:
                    stage_out[sk] = sv
            normalized_stages.append(stage_out)
        out["failed_stages"] = normalized_stages

    return out


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ingest/failure":
            self.send_response(404)
            self.end_headers()
            return

        api_key = self.headers.get("X-Api-Key", "")
        if EXPECTED_API_KEY and api_key != EXPECTED_API_KEY:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}')
            return

        if isinstance(payload, dict) and isinstance(payload.get("failures"), list):
            failures_in = payload["failures"]
            normalized_failures = []
            for item in failures_in:
                if isinstance(item, dict):
                    normalized_failures.append(_normalize_payload_for_capture(item))
                else:
                    normalized_failures.append(item)
            event = {
                "received_at": datetime.now(UTC).isoformat(),
                "path": self.path,
                "headers": {"X-Api-Key": api_key},
                "batch": True,
                "count": len(normalized_failures),
                "failures": normalized_failures,
            }
        else:
            if isinstance(payload, dict):
                payload = _normalize_payload_for_capture(payload)
            event = {
                "received_at": datetime.now(UTC).isoformat(),
                "path": self.path,
                "headers": {"X-Api-Key": api_key},
                "batch": False,
                "payload": payload,
            }
        with CAPTURE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        parsed = urlparse(self.path)
        if parsed.path == "/events":
            all_lines: list[str] = []
            if CAPTURE_FILE.exists():
                all_lines = [ln for ln in CAPTURE_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
            events = [json.loads(line) for line in all_lines]
            qs = parse_qs(parsed.query)
            # Default: return every captured event. Optional ?last=N returns only the last N.
            last_raw = (qs.get("last", [""])[0] or "").strip()
            if last_raw.isdigit() and int(last_raw) > 0:
                n = int(last_raw)
                out = events[-n:] if len(events) > n else events
            else:
                out = events
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "count": len(out),
                        "events": out,
                    }
                ).encode("utf-8")
            )
            return

        self.send_response(404)
        self.end_headers()


def run() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8090), Handler)
    print("Mock worker listening at http://127.0.0.1:8090")
    print("Capture file:", CAPTURE_FILE)
    server.serve_forever()


if __name__ == "__main__":
    run()
