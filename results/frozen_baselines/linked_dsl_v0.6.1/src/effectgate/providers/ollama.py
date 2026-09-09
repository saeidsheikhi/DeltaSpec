from __future__ import annotations

import os
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from effectgate.providers.base import LLMResult


def _ns_to_s(x: Any) -> float | None:
    return (x / 1e9) if isinstance(x, (int, float)) else None


def _transport_kind(exc: Exception) -> str:
    """Distinguish a read timeout from a genuine transport failure.

    urllib3 wraps an unretried read timeout in `MaxRetryError`, which requests
    re-raises as `ConnectionError` -- not as `requests.Timeout`. Taking that at face
    value files "the model was still generating when the budget ran out" under the
    same label as "the endpoint was unreachable". Those are different findings for
    RQ5: one is a latency budget that is too small, the other is an outage.
    """
    text = f"{type(exc).__name__}: {exc}"
    return "timeout" if "ReadTimeout" in text or "read timed out" in text.lower() else "transport"


class OllamaProvider:
    """Ollama native ``/api/chat`` client with real token/timing metrics.

    Provider failures are returned as ``LLMResult(ok=False, ...)`` rather than
    raised, so that experiment code can record them distinctly from task failures
    (STATISTICS_PLAN.md: "never score provider errors as ordinary task failures").
    """

    name = "ollama"

    def __init__(self, base_url: str | None = None, timeout_s: int | None = None):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout_s = int(timeout_s or os.getenv("OLLAMA_TIMEOUT_S", "900"))
        self.session = requests.Session()
        # `read=0` is deliberate: a read timeout on `/api/chat` must NOT be retried.
        #
        # Measured 2026-09-05. With the previous `Retry(total=3)`, every generation that
        # outran `OLLAMA_TIMEOUT_S` was re-POSTed three more times, so a 180 s budget
        # surfaced as a 726 s failure (4 x 180 s + backoff) reported as
        # `ConnectionError: Max retries exceeded` -- which the classifier below files
        # under `transport` rather than `timeout`. Three separate harms:
        #
        #   1. the failure taxonomy is wrong (STATISTICS_PLAN separates provider
        #      failure kinds, and this one was mislabelled in every AppWorld v3 row);
        #   2. `/api/chat` is not idempotent in cost: the first generation keeps running
        #      on a CPU-only endpoint while three more are queued behind it, so the
        #      retry makes the timeout it is retrying *more* likely, and inflates the
        #      RQ5 inference-time accounting fourfold;
        #   3. the recorded wall time is 4x the configured budget, which is what made
        #      the AppWorld pilot look like it had a "12-minute timeout" it never had.
        #
        # Connection-level failures and retryable status codes are still retried: no
        # generation has started in those cases, so a retry is genuinely free.
        retries = Retry(
            total=3, connect=3, read=0, status=3, backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retries))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    # ---------------------------------------------------------------- discovery
    def tags(self) -> dict[str, Any]:
        r = self.session.get(f"{self.base_url}/api/tags", timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()

    def available_models(self) -> list[dict[str, Any]]:
        return list(self.tags().get("models", []))

    def model_names(self) -> list[str]:
        return [m.get("name") or m.get("model") for m in self.available_models()]

    def version(self) -> str:
        try:
            r = self.session.get(f"{self.base_url}/api/version", timeout=30)
            r.raise_for_status()
            return str(r.json().get("version", ""))
        except Exception:
            return ""

    # ------------------------------------------------------------------- chat
    def chat(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> LLMResult:
        options: dict[str, Any] = {
            "temperature": kwargs.pop("temperature", 0.0),
            "top_p": kwargs.pop("top_p", 1.0),
            "num_predict": kwargs.pop("num_predict", 2048),
        }
        seed = kwargs.pop("seed", None)
        if seed is not None:
            options["seed"] = int(seed)
        num_ctx = kwargs.pop("num_ctx", None)
        if num_ctx is not None:
            options["num_ctx"] = int(num_ctx)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }

        # Ollama >= 0.5 accepts either "json" or a full JSON Schema in `format`.
        fmt = kwargs.pop("format", None)
        json_format = kwargs.pop("json_format", False)
        format_mode = "none"
        if fmt is not None:
            payload["format"] = fmt
            format_mode = "schema" if isinstance(fmt, dict) else str(fmt)
        elif json_format:
            payload["format"] = "json"
            format_mode = "json"

        # Model-runtime control, not a compiler setting: thinking-capable models (qwen3
        # family) think by default under Ollama and would spend the fixed `num_predict`
        # budget on reasoning tokens. `OLLAMA_THINK=false` disables it for the run; when
        # the variable is unset nothing is sent, so non-thinking models (phi4) are
        # byte-for-byte unaffected. Recorded in the run manifest via the env snapshot.
        think = kwargs.pop("think", None)
        if think is None:
            env_think = os.getenv("OLLAMA_THINK")
            if env_think is not None:
                think = env_think.strip().lower() in ("1", "true", "yes")
        if think is not None:
            payload["think"] = think

        request_meta = {
            "messages": messages,
            "options": options,
            "format_mode": format_mode,
        }

        t0 = time.perf_counter()
        try:
            r = self.session.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout_s)
            wall_s = time.perf_counter() - t0
            r.raise_for_status()
            j = r.json()
        except requests.Timeout as exc:
            return self._error_result(model, request_meta, time.perf_counter() - t0, exc, "timeout")
        except requests.HTTPError as exc:
            return self._error_result(model, request_meta, time.perf_counter() - t0, exc, "http")
        except ValueError as exc:
            return self._error_result(model, request_meta, time.perf_counter() - t0, exc, "decode")
        except requests.RequestException as exc:
            return self._error_result(model, request_meta, time.perf_counter() - t0,
                                      exc, _transport_kind(exc))

        msg = j.get("message", {}) or {}
        text = (msg.get("content") or "").strip()
        metrics = {
            "wall_time_s": wall_s,
            "done_reason": j.get("done_reason"),
            "total_time_s": _ns_to_s(j.get("total_duration")),
            "load_time_s": _ns_to_s(j.get("load_duration")),
            "prompt_tokens": j.get("prompt_eval_count"),
            "prompt_eval_time_s": _ns_to_s(j.get("prompt_eval_duration")),
            "completion_tokens": j.get("eval_count"),
            "completion_eval_time_s": _ns_to_s(j.get("eval_duration")),
        }
        pe_t, pe_n = metrics["prompt_eval_time_s"], metrics["prompt_tokens"]
        ce_t, ce_n = metrics["completion_eval_time_s"], metrics["completion_tokens"]
        if isinstance(pe_n, int) and isinstance(pe_t, float) and pe_t > 0:
            metrics["prompt_tok_per_s"] = pe_n / pe_t
        if isinstance(ce_n, int) and isinstance(ce_t, float) and ce_t > 0:
            metrics["completion_tok_per_s"] = ce_n / ce_t
        # Reasoning models (deepseek-r1, qwen3) may return a separate thinking field.
        if msg.get("thinking"):
            metrics["thinking_chars"] = len(msg["thinking"])

        return LLMResult(
            text=text, metrics=metrics, raw=j, ok=True,
            provider=self.name, model=model, request=request_meta,
        )

    def _error_result(
        self, model: str, request_meta: dict[str, Any], wall_s: float, exc: Exception, kind: str
    ) -> LLMResult:
        return LLMResult(
            text="",
            metrics={"wall_time_s": wall_s},
            raw={},
            ok=False,
            error=f"{type(exc).__name__}: {exc}"[:500],
            error_kind=kind,
            provider=self.name,
            model=model,
            request=request_meta,
        )
