from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

CONTRACT_VERSION = "0.5"
#: Older contracts stay valid: each version only *adds* operators.
SUPPORTED_CONTRACT_VERSIONS = ("0.1", "0.2", "0.3", "0.4", "0.5")

Operator = Literal[
    "exists","absent","eq","ne","contains","not_contains",
    "count_eq","count_ge","subset","set_eq","changed","unchanged",
    # v0.2 record-set delta operators. Added because AppWorld's official evaluators
    # assert on added/updated/removed *record sets*, a distinction that row counts
    # cannot express: adding one record and deleting another leaves every count
    # unchanged. See notes/APPWORLD_API_NOTES.md.
    "added_count_eq","removed_count_eq","updated_count_eq",
    # v0.3. Exact-count operators alone leave two things inexpressible, which between
    # them accounted for 6 of 7 AppWorld pilot failures:
    #   * open cardinality -- "follow *all* classical artists" has a count the compiler
    #     cannot know from the state it is shown, so it emitted `value: null`;
    #   * prohibitions -- "nothing may be deleted" needs a predicate that is TRUE when
    #     records were removed, so `forbidden` had no correct form at all.
    # See notes/APPWORLD_SEMANTIC_MISMATCH.md.
    "added_count_ge","removed_count_ge","updated_count_ge",
    # v0.4 `record`-kind operators over a typed, schema-grounded field projection. The
    # hash-only view could say how many records changed but never *which* or *how*:
    # the Phase B diagnostic pilot's failure mass was predicates about a record's field
    # values (`is_playing`, note `content`, `Song.genre`), which had no correct form.
    # Selectors are data interpreted by a fixed evaluator; no generated code runs.
    # See notes/DECISIONS.md (2026-09-05, field-aware representation).
    "count_le",
    "field_eq","field_ne","field_in","field_contains",
    "field_changed","field_unchanged","field_transition","fields_unchanged_except",
    # v0.4 `scope`-kind operator: AppWorld's own `changed_model_names() == {...}` idiom.
    "changed_tables_subset",
]

ALLOWED_OPS: frozenset[str] = frozenset(Operator.__args__)  # type: ignore[attr-defined]
ALLOWED_KINDS: frozenset[str] = frozenset({"state", "delta", "record", "scope"})

#: v0.4 operators that require ``kind: record`` (a `table`, optional `where`, and for
#: the `field_*` family a `field`). v0.5 adds the selector-scoped set-level effects
#: `added_count_*` / `removed_count_*` ("at least one SongLike row was ADDED whose
#: song_id is in my playlists"), so a relational effect can be stated as row creation or
#: deletion instead of being forced into a field transition -- the over-use the v0.4
#: diagnostic exposed.
RECORD_OPS: frozenset[str] = frozenset({
    "exists", "absent", "count_eq", "count_ge", "count_le",
    "field_eq", "field_ne", "field_in", "field_contains",
    "field_changed", "field_unchanged", "field_transition", "fields_unchanged_except",
    "added_count_eq", "added_count_ge", "removed_count_eq", "removed_count_ge",
})
#: Record operators over the pre/post row *sets* selected by `where`.
RECORD_SET_DELTA_OPS: frozenset[str] = frozenset({
    "added_count_eq", "added_count_ge", "removed_count_eq", "removed_count_ge",
})
#: Record operators that are a property of the final state only.
RECORD_POST_OPS: frozenset[str] = frozenset({
    "exists", "absent", "count_eq", "count_ge", "count_le",
    "field_eq", "field_ne", "field_in", "field_contains",
})
#: Record operators that compare S0 against S1 on records selected in S0, matched by id.
RECORD_DELTA_FIELD_OPS: frozenset[str] = frozenset({
    "field_changed", "field_unchanged", "field_transition", "fields_unchanged_except",
})
#: Record operators that need a `field`.
FIELD_OPS: frozenset[str] = frozenset({
    "field_eq", "field_ne", "field_in", "field_contains",
    "field_changed", "field_unchanged", "field_transition",
})
#: Operators allowed inside a `where` selector: ``{field: {op: value}}``.
#: v0.5: `in_related` / `not_in_related` / `intersects_related` take a **sub-selection**
#: ``{"table": "<app>.<Table>", "field": "<f>", "where": {...}}`` instead of a literal
#: list -- a bounded, typed, declarative relation over the projected tables (depth <= 3),
#: validated against declared foreign keys. No SQL, no code. See fieldview.py.
WHERE_OPS: frozenset[str] = frozenset({
    "eq", "ne", "in", "not_in", "contains", "not_contains", "gt", "ge", "lt", "le", "is_null",
    "in_related", "not_in_related", "intersects_related",
})
RELATED_OPS: frozenset[str] = frozenset({"in_related", "not_in_related", "intersects_related"})
#: Maximum nesting of sub-selections. Three covers "artists OF songs IN playlists OF mine".
MAX_RELATION_DEPTH = 3
#: v0.4 operators that require ``kind: scope``.
SCOPE_OPS: frozenset[str] = frozenset({"changed_tables_subset"})

#: Operators over the *record sets* of a table, comparing S0 against S1. The path
#: must resolve to a ``{record_id: record_hash}`` mapping in both states.
RECORD_DELTA_OPS: frozenset[str] = frozenset({
    "added_count_eq", "removed_count_eq", "updated_count_eq",
    "added_count_ge", "removed_count_ge", "updated_count_ge",
})

#: Operators that are only meaningful when comparing S0 against S1.
DELTA_ONLY_OPS: frozenset[str] = frozenset({"changed", "unchanged"}) | RECORD_DELTA_OPS
#: Operators that are only meaningful as a property of a single (final) state.
STATE_ONLY_OPS: frozenset[str] = (
    ALLOWED_OPS - DELTA_ONLY_OPS - RECORD_DELTA_FIELD_OPS - SCOPE_OPS
    - frozenset({"count_le", "field_eq", "field_ne", "field_in", "field_contains"})
)


class ContractError(Exception):
    """Base class for contract problems that must never be scored as PASS."""


class ContractEvalError(ContractError):
    """Raised when a predicate cannot be deterministically evaluated."""


class ContractStaticError(ContractError):
    """Raised when a contract is structurally invalid before evaluation."""


@dataclass
class Predicate:
    kind: Literal["state", "delta", "record", "scope"]
    path: str
    op: Operator
    value: Any = None
    description: str = ""
    critical: bool = True
    # v0.4, `record` kind only. `table` is "<app>.<Table>"; `where` is a typed selector
    # ``{field: {op: value}}`` (AND); `field` names the field a `field_*` op inspects.
    table: str | None = None
    where: dict[str, Any] | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "path": self.path,
            "op": self.op,
            "value": self.value,
            "description": self.description,
            "critical": self.critical,
        }
        # Only v0.4 predicates carry the extra keys. A v0.3 contract therefore
        # serialises byte-for-byte as before, so its recorded `contract_hash` in the
        # frozen baseline still matches.
        if self.kind in ("record", "scope"):
            d["table"] = self.table
            d["where"] = self.where
            d["field"] = self.field
        return d


@dataclass
class EffectContract:
    contract_version: str
    task_id: str
    required: list[Predicate] = field(default_factory=list)
    forbidden: list[Predicate] = field(default_factory=list)
    invariants: list[Predicate] = field(default_factory=list)
    alternatives: list[list[Predicate]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def all_predicates(self) -> list[Predicate]:
        out = list(self.required) + list(self.forbidden) + list(self.invariants)
        for grp in self.alternatives:
            out.extend(grp)
        return out

    def n_predicates(self) -> int:
        return len(self.all_predicates())

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "task_id": self.task_id,
            "required": [p.to_dict() for p in self.required],
            "forbidden": [p.to_dict() for p in self.forbidden],
            "invariants": [p.to_dict() for p in self.invariants],
            "alternatives": [[p.to_dict() for p in g] for g in self.alternatives],
            "assumptions": list(self.assumptions),
            "metadata": dict(self.metadata),
        }


@dataclass
class ToolCall:
    """One bounded, side-effecting tool invocation."""
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "args": dict(self.args)}


@dataclass
class ToolEvent:
    """Recorded outcome of a tool call against a mutable world."""
    index: int
    name: str
    args: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
