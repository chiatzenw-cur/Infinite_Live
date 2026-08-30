"""Journal: one JSONL line per prompt (audience text -> director text -> prompt -> clip)."""
import json
import time
from pathlib import Path


class Journal:
    """Append-only per-beat log of the text conversation behind each generated clip."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields) -> None:
        fields.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n")

    def latest(self, n: int = 1):
        """Return the most recent n records (for a UI/observability panel)."""
        if not self.path.exists():
            return []
        lines = [l for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [json.loads(l) for l in lines[-n:]]
