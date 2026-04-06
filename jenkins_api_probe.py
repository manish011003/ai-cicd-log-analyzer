import argparse
import json
from urllib.parse import quote

import httpx


def job_path(job_full_name: str) -> str:
    return "/".join([f"job/{quote(part, safe='')}" for part in job_full_name.split("/")])


def _sample(text: str, limit: int = 500) -> str:
    return (text[:limit] + "...") if len(text) > limit else text


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Jenkins APIs for one job/build.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--job", required=True, help="folder/job-name")
    parser.add_argument("--build", required=True, type=int)
    args = parser.parse_args()

    auth = (args.user, args.token)
    base = args.base_url.rstrip("/")
    p = job_path(args.job)

    urls = {
        "rss_failed": f"{base}/rssFailed",
        "build_api": f"{base}/{p}/{args.build}/api/json",
        "wfapi_describe": f"{base}/{p}/{args.build}/wfapi/describe",
        "console_text": f"{base}/{p}/{args.build}/consoleText",
    }

    with httpx.Client(auth=auth, timeout=30, follow_redirects=True) as client:
        results: dict = {}
        for key, url in urls.items():
            try:
                resp = client.get(url)
                results[key] = {
                    "url": url,
                    "status_code": resp.status_code,
                    "content_length": len(resp.text),
                    "sample": _sample(resp.text, 300 if key == "console_text" else 400),
                }
            except Exception as exc:  # noqa: BLE001
                results[key] = {"url": url, "error": str(exc)}

        stage_logs: list[dict] = []
        if results.get("wfapi_describe", {}).get("status_code") == 200:
            data = client.get(urls["wfapi_describe"]).json()
            for stage in data.get("stages", []):
                sid = stage.get("id")
                status = str(stage.get("status", "")).upper()
                if sid and status in {"FAILED", "ERROR", "ABORTED"}:
                    entry: dict = {
                        "stage": stage.get("name"),
                        "stage_id": sid,
                        "status": status,
                    }

                    wfapi_url = f"{base}/{p}/{args.build}/execution/node/{sid}/wfapi/log"
                    try:
                        wr = client.get(wfapi_url)
                        entry["wfapi_log_status"] = wr.status_code
                        if wr.status_code < 400:
                            try:
                                wd = wr.json()
                                wt = str(wd.get("text", "")).strip()
                                entry["wfapi_log_text_len"] = len(wt)
                                entry["wfapi_log_sample"] = _sample(wt)
                            except Exception:  # noqa: BLE001
                                entry["wfapi_log_parse_error"] = "non-JSON response"
                                entry["wfapi_log_raw_sample"] = _sample(wr.text)
                        else:
                            entry["wfapi_log_sample"] = _sample(wr.text)
                    except Exception as exc:  # noqa: BLE001
                        entry["wfapi_log_error"] = str(exc)

                    raw_url = f"{base}/{p}/{args.build}/execution/node/{sid}/log/?start=0"
                    try:
                        rr = client.get(raw_url)
                        entry["raw_log_status"] = rr.status_code
                        entry["raw_log_text_len"] = len(rr.text)
                        entry["raw_log_sample"] = _sample(rr.text)
                    except Exception as exc:  # noqa: BLE001
                        entry["raw_log_error"] = str(exc)

                    stage_logs.append(entry)

        print(json.dumps({"checks": results, "failed_stage_logs": stage_logs}, indent=2))


if __name__ == "__main__":
    main()
