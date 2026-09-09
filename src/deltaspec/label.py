"""The intent labeller: one short LLM call that classifies mined facts.

The model never writes a predicate, a table name, a field, a value or a number. It sees
the task and a numbered list of plain-language facts (each true of the reference run)
and returns, for every fact id, one of four labels. The decoding grammar enumerates the
fact ids and the labels, so the output cannot name anything outside the menu.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deltaspec.assemble import LABELS
from deltaspec.mine import Fact

REPO = Path(__file__).resolve().parents[2]
PROMPT = REPO / "prompts" / "deltaspec_intent_labeler_v1.md"


def label_grammar(fids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "labels": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string", "enum": list(fids)},
                               "label": {"type": "string", "enum": list(LABELS)}},
                "required": ["id", "label"]}},
        },
        "required": ["labels"],
    }


def render_menu(facts: list[Fact]) -> str:
    lines = []
    for f in facts:
        tag = "PROHIBITION" if f.clause == "forbidden" else f.kind.upper()
        lines.append(f"{f.fid}. [{tag}] {f.text}")
    return "\n".join(lines)


def build_messages(system_prompt: str, task_id: str, instruction: str, facts: list[Fact],
                   changed_tables: dict[str, Any]) -> list[dict[str, str]]:
    summary = ", ".join(f"{t} (+{d.get('n_added', 0)} −{d.get('n_removed', 0)} ~{d.get('n_updated', 0)})"
                        for t, d in sorted(changed_tables.items()))
    user = {"task_id": task_id, "instruction": instruction,
            "what_the_reference_run_changed": summary,
            "facts": render_menu(facts),
            "answer_format": "Return {\"labels\": [{\"id\": \"F1\", \"label\": \"required|side_effect|incidental|unsure\"}, ...]} covering every fact id."}
    return [{"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user, indent=1, ensure_ascii=False)}]


@dataclass
class LabelResult:
    ok: bool
    labels: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    llm: Any = None
    n_missing: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "labels": self.labels, "error": self.error, "n_missing": self.n_missing}


def label_facts(provider: Any, model: str, task_id: str, instruction: str, facts: list[Fact],
                delta: dict[str, Any], prompt_path: str | Path = PROMPT, seed: int = 42,
                num_ctx: int = 8192, num_predict: int = 512) -> LabelResult:
    system = Path(prompt_path).read_text(encoding="utf-8")
    msgs = build_messages(system, task_id, instruction, facts, delta)
    fids = [f.fid for f in facts]
    r = provider.chat(model, msgs, format=label_grammar(fids), temperature=0.0, seed=seed,
                      num_ctx=num_ctx, num_predict=num_predict)
    if not r.ok:
        return LabelResult(False, {}, r.error, r)
    try:
        d = json.loads(r.text)
    except json.JSONDecodeError as exc:
        return LabelResult(False, {}, f"JSONDecodeError: {exc}", r)
    labels: dict[str, str] = {}
    for item in d.get("labels", []) if isinstance(d, dict) else []:
        if isinstance(item, dict) and item.get("id") in fids and item.get("label") in LABELS:
            labels.setdefault(item["id"], item["label"])
    missing = [f for f in fids if f not in labels]
    if not labels:
        return LabelResult(False, {}, "labeller returned no valid labels", r, len(missing))
    return LabelResult(True, labels, None, r, len(missing))
