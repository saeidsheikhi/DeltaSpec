from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def short_hash(obj: Any) -> str:
    return stable_hash(obj)[:16]


def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "not-a-git-repo"
    except Exception:
        return "not-a-git-repo"


class JsonlRecorder:
    """Append-only JSONL sink with resume support keyed on a stable record id."""

    def __init__(self, path: str | Path, key: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = key
        self._seen: set[str] = set()
        if key and self.path.exists():
            for rec in self.read():
                if key in rec:
                    self._seen.add(str(rec[key]))

    def done(self, key_value: str) -> bool:
        return str(key_value) in self._seen

    def append(self, record: dict[str, Any]) -> None:
        rec = dict(record)
        rec.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        if self.key and self.key in rec:
            self._seen.add(str(rec[self.key]))

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())

        def gen() -> Iterator[dict[str, Any]]:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
        return gen()


def run_manifest(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Environment provenance recorded once per experiment run."""
    man = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL", ""),
        "seed": int(os.getenv("EFFECTGATE_SEED", "42")),
        "fail_closed": os.getenv("EFFECTGATE_FAIL_CLOSED", "true").lower() == "true",
    }
    if extra:
        man.update(extra)
    return man
