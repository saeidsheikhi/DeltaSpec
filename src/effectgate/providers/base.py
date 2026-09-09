from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMResult:
    text: str
    metrics: dict[str, Any]
    raw: dict[str, Any]
    ok: bool = True
    error: str | None = None
    error_kind: str | None = None   # transport | http | timeout | decode | disabled
    provider: str = ""
    model: str = ""
    request: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.request.get("messages", []), sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    @property
    def response_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]

    def call_record(self, **extra: Any) -> dict[str, Any]:
        """A provider-call record suitable for results/provider_calls.jsonl."""
        rec = {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
            "error_kind": self.error_kind,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "options": self.request.get("options"),
            "format_mode": self.request.get("format_mode"),
            **{k: v for k, v in self.metrics.items()},
        }
        rec.update(extra)
        return rec


class ProviderError(RuntimeError):
    """A failure of the LLM provider, never of the agent or the task."""

    def __init__(self, message: str, kind: str = "transport"):
        super().__init__(message)
        self.kind = kind


class LLMProvider(Protocol):
    name: str

    def chat(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> LLMResult: ...
