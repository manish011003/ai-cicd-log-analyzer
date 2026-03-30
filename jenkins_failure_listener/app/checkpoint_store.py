import json
from pathlib import Path


class CheckpointStore:
    def __init__(self, checkpoint_file: str) -> None:
        self.path = Path(checkpoint_file)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict[str, int]:
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        return json.loads(raw)

    def _save(self, data: dict[str, int]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_last_build(self, job_full_name: str) -> int:
        data = self._load()
        return int(data.get(job_full_name, 0))

    def mark_build(self, job_full_name: str, build_number: int) -> None:
        data = self._load()
        if build_number > int(data.get(job_full_name, 0)):
            data[job_full_name] = build_number
            self._save(data)
