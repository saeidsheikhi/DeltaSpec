"""LLM compilation of a natural-language side-effecting task into an Effect Contract.

The compiler never emits executable code; it emits JSON in the restricted DSL,
which is then normalized, schema-validated and statically linted. Every stage is
reported separately so RQ1 can distinguish provider failure, parse failure,
schema failure, static-lint failure and semantic inadequacy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from effectgate.contracts.io import contract_from_dict, contract_hash, normalize_paths, normalize_raw
from effectgate.contracts.schema import LintReport, lint_contract, ollama_format_schema, validate_schema
from effectgate.models import ALLOWED_OPS, EffectContract
from effectgate.providers.base import LLMResult

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPILER_PROMPT = REPO_ROOT / "prompts" / "contract_compiler.md"
REPAIR_PROMPT = REPO_ROOT / "prompts" / "contract_repair.md"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Recover a JSON object from raw model text.

    Handles: bare JSON, fenced blocks, ``<think>`` preambles from reasoning models,
    and trailing prose. Raises ``json.JSONDecodeError`` when nothing parses.
    """
    text = _THINK.sub("", text or "").strip()
    if not text:
        raise json.JSONDecodeError("empty model output", "", 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for m in _FENCE.finditer(text):
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
    # Longest balanced {...} span.
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise json.JSONDecodeError("no JSON object found in model output", text[:200], 0)


@dataclass
class CompileOutcome:
    """Everything needed to score one contract-compilation attempt."""
    task_id: str
    model: str
    status: str                       # ok | provider_error | parse_error | schema_error | lint_error
    contract: EffectContract | None = None
    contract_dict: dict[str, Any] | None = None
    contract_hash: str | None = None
    raw_text: str = ""
    normalization_fixes: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    lint: LintReport | None = None
    llm: LLMResult | None = None
    prompt_hash: str | None = None
    error: str | None = None
    repair_rounds: int = 0
    #: Result of the LLM-free pre-flight check: does this contract accept the
    #: reference (known-good) final state? ``None`` when no reference was supplied.
    #: A contract that fails this blocks every release, so it is worth knowing at
    #: compile time, before a single mutant has been generated.
    accepts_reference: bool | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.contract is not None

    @property
    def usable(self) -> bool:
        """Evaluable *and* it does not reject correct behaviour.

        ``ok`` alone is a misleading coverage measure: a schema-valid, lint-clean
        contract that rejects the known-good state is worse than useless.
        """
        return self.ok and self.accepts_reference is not False

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "model": self.model,
            "status": self.status,
            "contract_hash": self.contract_hash,
            "n_predicates": self.contract.n_predicates() if self.contract else 0,
            "n_required": len(self.contract.required) if self.contract else 0,
            "n_forbidden": len(self.contract.forbidden) if self.contract else 0,
            "n_invariants": len(self.contract.invariants) if self.contract else 0,
            "n_alternatives": len(self.contract.alternatives) if self.contract else 0,
            "accepts_reference": self.accepts_reference,
            "usable": self.usable,
            "normalization_fixes": list(self.normalization_fixes),
            "schema_errors": list(self.schema_errors),
            "lint": self.lint.to_dict() if self.lint else None,
            "error": self.error,
            "repair_rounds": self.repair_rounds,
            "prompt_hash": self.prompt_hash,
        }


def build_compiler_messages(
    system_prompt: str,
    task_id: str,
    instruction: str,
    initial_state: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    policy: str | None = None,
) -> list[dict[str, str]]:
    user = {
        "task_id": task_id,
        "instruction": instruction,
        "initial_state": initial_state,
        "tool_schemas": tool_schemas,
        "allowed_operators": sorted(ALLOWED_OPS),
        "path_syntax": "JSON Pointer, e.g. /files/archive/report.pdf",
    }
    if policy:
        user["policy"] = policy
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user, sort_keys=True, indent=1)},
    ]


def finalize(
    task_id: str,
    model: str,
    result: LLMResult,
    initial_state: dict[str, Any] | None,
    repair_rounds: int = 0,
    reference_final_state: dict[str, Any] | None = None,
    lint_state: dict[str, Any] | None = None,
) -> CompileOutcome:
    """Turn raw model output into a validated contract, staging every failure mode."""
    out = CompileOutcome(
        task_id=task_id, model=model, status="ok", raw_text=result.text,
        llm=result, prompt_hash=result.prompt_hash, repair_rounds=repair_rounds,
    )
    if not result.ok:
        out.status = "provider_error"
        out.error = result.error
        return out
    try:
        raw = extract_json(result.text)
    except json.JSONDecodeError as exc:
        out.status = "parse_error"
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    # Paths must be normalised and linted against the state the contract will be
    # *evaluated* on, which is not always the state shown to the compiler: AppWorld
    # shows a summarised view but evaluates over `counts` + `records`.
    state_for_paths = lint_state if lint_state is not None else initial_state
    norm, fixes = normalize_raw(raw, task_id)
    fixes += normalize_paths(norm, state_for_paths)
    out.normalization_fixes = fixes
    out.contract_dict = norm

    errs = validate_schema(norm)
    out.schema_errors = errs
    if errs:
        out.status = "schema_error"
        out.error = "; ".join(errs[:5])
        return out

    try:
        contract = contract_from_dict(norm)
    except Exception as exc:
        out.status = "schema_error"
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    out.contract = contract
    out.contract_hash = contract_hash(contract)
    out.lint = lint_contract(contract, state_for_paths, task_id)
    if not out.lint.ok:
        out.status = "lint_error"
        out.error = "; ".join(out.lint.errors[:5])
        return out

    # LLM-free pre-flight: a contract that rejects the state a correct execution
    # actually produced would block every release. Costs one evaluator call.
    if reference_final_state is not None and state_for_paths is not None:
        from effectgate.contracts.evaluator import evaluate_contract
        res = evaluate_contract(contract, state_for_paths, reference_final_state)
        out.accepts_reference = res.passed
        if not res.passed:
            out.error = "rejects the known-good final state: " + "; ".join(
                f"{o.group}:{o.predicate.path} {o.predicate.op}"
                for o in res.violations()[:4]
            )
    return out


def compile_contract(
    provider: Any,
    model: str,
    task_id: str,
    instruction: str,
    initial_state: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    policy: str | None = None,
    prompt_path: str | Path = COMPILER_PROMPT,
    format_mode: str = "schema",
    temperature: float = 0.0,
    seed: int | None = 42,
    num_predict: int = 1536,
    num_ctx: int | None = 8192,
    reference_final_state: dict[str, Any] | None = None,
    lint_state: dict[str, Any] | None = None,
    vocab: dict[str, Any] | None = None,
) -> CompileOutcome:
    """One-shot contract compilation.

    ``vocab`` (v0.6) enumerates the task's projected tables and fields into the decoding
    grammar, so the model cannot emit an identifier the schema does not have.

    ``format_mode`` selects the decoding constraint: ``"schema"`` (grammar-constrained
    to the contract shape), ``"json"`` (JSON syntax only) or ``"none"`` (free text).
    """
    system = Path(prompt_path).read_text(encoding="utf-8")
    messages = build_compiler_messages(system, task_id, instruction, initial_state, tool_schemas, policy)
    kwargs: dict[str, Any] = {
        "temperature": temperature, "num_predict": num_predict, "seed": seed, "num_ctx": num_ctx,
    }
    if format_mode == "schema":
        kwargs["format"] = ollama_format_schema(vocab)
    elif format_mode == "json":
        kwargs["format"] = "json"
    result = provider.chat(model, messages, **kwargs)
    return finalize(task_id, model, result, initial_state,
                    reference_final_state=reference_final_state, lint_state=lint_state)


def schema_vocab(state: dict[str, Any]) -> dict[str, Any]:
    """Per-task vocabulary for the decoding grammar, read from the field projection."""
    tables, fields = [], set()
    for app, ts in (state.get("fields") or {}).items():
        if isinstance(ts, dict):
            for t, v in ts.items():
                tables.append(f"{app}.{t}")
                fields.update((v.get("field_types") or {}).keys())
    return {"tables": sorted(tables), "fields": sorted(fields)}
