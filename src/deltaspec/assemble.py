"""Assemble labelled facts into a DSL v0.5 contract; decide abstention.

Labels (from the intent labeller, one per fact):
    required     the task requires this; any correct version must reproduce it
    side_effect  a necessary consequence of doing the task; tolerated, not required
    incidental   true of this run only; must not be required
    unsure       the labeller could not tell; treated as incidental (never required)

Assembly is deterministic:
    required facts (clause required)  -> `required` predicates
    required facts (clause forbidden) -> `forbidden` predicates
    scope                             -> exactly one changed_tables_subset over the full
                                         reference change set (every changed table is
                                         tolerated; intent decides what is *required*)
Abstain (fail closed) when no table-level effect fact is labelled required, or when the
labeller output is missing/degenerate. Nothing is invented; every predicate in the
contract is a mined fact that holds on the reference by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from deltaspec.mine import Fact, scope_predicate

LABELS = ("required", "side_effect", "incidental", "unsure")
EFFECT_KINDS = {"table_added", "table_removed", "table_updated", "added_field", "added_relation",
                "updated_selector_field", "updated_field", "updated_to",
                "added_count_exact", "removed_count_exact", "updated_count_exact"}


@dataclass
class Assembled:
    status: str                            # ok | abstain
    contract: dict[str, Any] | None
    reason: str | None = None
    n_required: int = 0
    n_forbidden: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    used_facts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "n_required": self.n_required,
                "n_forbidden": self.n_forbidden, "labels": self.labels, "used_facts": self.used_facts,
                "notes": self.notes}


def assemble(task_id: str, facts: list[Fact], labels: dict[str, str], delta: dict[str, Any],
             require_table_effect: bool = True) -> Assembled:
    by_id = {f.fid: f for f in facts}
    clean = {fid: (lab if lab in LABELS else "unsure") for fid, lab in labels.items() if fid in by_id}
    required, forbidden, used = [], [], []
    for f in facts:
        if clean.get(f.fid) != "required":
            continue
        # a detail fact (field/relation) is only meaningful if its table-level effect is
        # also required; the labeller marks the parent, and the detail sharpens it
        if f.kind in ("added_field", "added_relation", "added_count_exact"):
            parent = next((g for g in facts if g.kind == "table_added" and g.table == f.table), None)
            if parent and clean.get(parent.fid) != "required":
                continue
        if f.kind in ("removed_count_exact", "updated_count_exact"):
            pk = "table_removed" if f.kind.startswith("removed") else "table_updated"
            parent = next((g for g in facts if g.kind == pk and g.table == f.table), None)
            if parent and clean.get(parent.fid) != "required":
                continue
        target = forbidden if f.clause == "forbidden" else required
        for p in f.predicates:
            target.append(dict(p))
        used.append(f.fid)
    has_effect = any(by_id[fid].kind in EFFECT_KINDS for fid in used)
    notes: list[str] = []
    if require_table_effect and not has_effect:
        # Rule A.orphan_side_effect. A side effect is a consequence *of* a required effect.
        # If no effect at all is required, "side_effect" labels on the reference's own
        # table-level changes are inconsistent: the run did something, and that something
        # can only be the task. Promote table-level effects labelled side_effect to
        # required (never incidental/unsure ones), and record it. Observed on a "send a
        # reminder" task whose only change was 7 Notification rows labelled side_effect.
        promoted = []
        for f in facts:
            if f.kind in ("table_added", "table_removed", "table_updated") and clean.get(f.fid) == "side_effect":
                for p in f.predicates:
                    required.append(dict(p))
                used.append(f.fid); promoted.append(f.fid); clean[f.fid] = "required"
        if promoted:
            notes.append(f"A.orphan_side_effect: promoted {promoted} — a side effect cannot be a consequence of nothing")
            has_effect = True
    if require_table_effect and not has_effect:
        return Assembled("abstain", None, "no effect fact was labelled required", 0, len(forbidden), clean, used)
    contract = {"contract_version": "0.5", "task_id": task_id, "required": required, "forbidden": forbidden,
                "invariants": [scope_predicate(delta)], "alternatives": [], "assumptions": [],
                "metadata": {"deltaspec": {"labels": clean, "used_facts": used, "notes": notes}}}
    a = Assembled("ok", contract, None, len(required), len(forbidden), clean, used)
    a.notes = notes
    return a
