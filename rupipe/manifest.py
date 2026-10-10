"""manifest.json: which steps are done for one input file, and their results."""
from __future__ import annotations

import json
import time
from pathlib import Path


class Manifest:
    def __init__(self, work: Path, src: Path):
        self.path = work / "manifest.json"
        self.src = src
        stat = src.stat()
        self.fingerprint = {"input": str(src), "size": stat.st_size, "mtime": int(stat.st_mtime)}
        self.data = {"source": self.fingerprint, "steps": {}}
        if self.path.exists():
            old = json.loads(self.path.read_text(encoding="utf-8"))
            # A different or modified input invalidates everything.
            if old.get("source") == self.fingerprint:
                self.data = old

    def done(self, step: str) -> bool:
        return step in self.data["steps"]

    def result(self, step: str) -> dict:
        return self.data["steps"][step]["result"]

    def mark(self, step: str, result: dict, seconds: float) -> None:
        self.data["steps"][step] = {
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": round(seconds, 1),
            "result": result,
        }
        self.save()

    def invalidate_from(self, steps: list[str], first: str) -> None:
        """Forget `first` and every step after it."""
        for s in steps[steps.index(first):]:
            self.data["steps"].pop(s, None)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
