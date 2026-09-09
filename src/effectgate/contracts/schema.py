"""Schema validation, static lint, and JSON-schema generation for Effect Contracts.

Three increasingly strict checks are applied to a candidate contract:

1. ``validate_schema``  -- JSON Schema conformance (``schemas/effect_contract.schema.json``).
2. ``lint_contract``    -- static, state-free checks (kind/op compatibility, empty
   contracts, paths that cannot resolve in the declared initial state, etc.).
3. evaluation          -- see :mod:`effectgate.contracts.evaluator`.

Everything here is deterministic and involves no LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from effectgate.models import (
    ALLOWED_KINDS,
    ALLOWED_OPS,
    CONTRACT_VERSION,
    DELTA_ONLY_OPS,
    FIELD_OPS,
    RECORD_DELTA_OPS,
    RECORD_OPS,
    RECORD_SET_DELTA_OPS,
    SCOPE_OPS,
    SUPPORTED_CONTRACT_VERSIONS,
    WHERE_OPS,
    EffectContract,
    Predicate,
)
from effectgate.contracts.fieldview import check_selector_types

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "effect_contract.schema.json"

#: Operators whose ``value`` field carries no meaning.
VALUELESS_OPS = frozenset({"exists", "absent", "changed", "unchanged"})
#: Operators that require an integer ``value``.
INT_VALUE_OPS = frozenset(
    {"count_eq", "count_ge"}) | RECORD_DELTA_OPS
#: Operators that require a collection ``value``.
COLLECTION_VALUE_OPS = frozenset({"subset", "set_eq"})


@lru_cache(maxsize=1)
def contract_json_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(contract_json_schema())


def validate_schema(obj: dict[str, Any]) -> list[str]:
    """Return a list of JSON-Schema violations (empty means valid)."""
    if not isinstance(obj, dict):
        return [f"contract must be a JSON object, got {type(obj).__name__}"]
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(_validator().iter_errors(obj), key=lambda e: list(e.absolute_path))
    ]


@dataclass
class LintReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


class _MissingT:
    __slots__ = ()


_MISSING = _MissingT()


def _resolve(state: Any, path: str) -> Any:
    cur = state
    for part in [x for x in path.split("/") if x]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _lint_record_predicate(p: Predicate, where: str, out: LintReport,
                           state: dict[str, Any] | None) -> None:
    """Static checks for v0.4 `record`/`scope` predicates against the projected schema.

    Everything checked here would otherwise surface as a fail-closed evaluation error
    at gate time. Catching it at compile time is what lets the repair loop act on it.
    """
    if p.kind == "scope":
        if p.op not in SCOPE_OPS:
            out.errors.append(f"{where}: kind 'scope' only allows {sorted(SCOPE_OPS)}, got '{p.op}'")
            return
        if not isinstance(p.value, list) or not all(
                isinstance(x, str) and x.count(".") == 1 for x in p.value):
            out.errors.append(f"{where}: '{p.op}' takes a list of '<app>.<Table>' names")
        return

    if p.op not in RECORD_OPS:
        out.errors.append(
            f"{where}: kind 'record' only allows {sorted(RECORD_OPS)}, got '{p.op}'; "
            "for record-set deltas use kind 'delta' on /records/<app>/<Table>")
        return
    if not isinstance(p.table, str) or p.table.count(".") != 1:
        out.errors.append(f"{where}: record predicate needs table '<app>.<Table>', got {p.table!r}")
        return
    if p.op in FIELD_OPS and not p.field:
        out.errors.append(f"{where}: operator '{p.op}' needs a 'field'")
    if (p.op in ("count_eq", "count_ge", "count_le") or p.op in RECORD_SET_DELTA_OPS) and (
            isinstance(p.value, bool) or not isinstance(p.value, int)):
        out.errors.append(f"{where}: operator '{p.op}' requires an integer value, got {p.value!r}")
    if p.op in ("field_in",) and not isinstance(p.value, list):
        out.errors.append(f"{where}: 'field_in' requires a list value, got {p.value!r}")
    if p.op == "field_transition" and not (isinstance(p.value, dict) and {"from", "to"} <= set(p.value)):
        out.errors.append(f"{where}: 'field_transition' requires value {{\"from\":..,\"to\":..}}")
    if p.op == "fields_unchanged_except" and p.value is not None and not (
            isinstance(p.value, list) and all(isinstance(x, str) for x in p.value)):
        out.errors.append(f"{where}: 'fields_unchanged_except' takes a list of field names")
    if p.op in ("exists", "absent") and not p.where:
        out.warnings.append(f"{where}: '{p.op}' without a 'where' selector is about the whole table")

    # Schema-grounded checks need the projection's field_types.
    fields = state.get("fields") if isinstance(state, dict) else None
    if not isinstance(fields, dict):
        return
    app, name = p.table.split(".")
    view = (fields.get(app) or {}).get(name) if isinstance(fields.get(app), dict) else None
    if not isinstance(view, dict):
        known = sorted(f"{a}.{t}" for a, ts in fields.items() if isinstance(ts, dict) for t in ts)
        out.errors.append(
            f"{where}: table '{p.table}' is not in the field projection; projected: {known[:30]}")
        return
    ftypes = view.get("field_types") or {}
    if p.field and p.field not in ftypes:
        out.errors.append(f"{where}: table '{p.table}' has no field '{p.field}'; fields: {sorted(ftypes)}")
    if view.get("truncated") and p.op not in ():
        out.errors.append(
            f"{where}: table '{p.table}' is truncated in the projection "
            f"({view.get('n_projected')}/{view.get('n_total')} rows); no record predicate "
            "over it can be evaluated deterministically")
    if p.where is not None:
        if not isinstance(p.where, dict):
            out.errors.append(f"{where}: 'where' must be an object of {{field: {{op: value}}}}")
        else:
            # Full static typing of the selector against the projected schema, including
            # relations (shape, depth, declared-FK backing). v0.4 checked only field
            # names and operator names, so SQL strings inside `in` reached the evaluator.
            for problem in check_selector_types(p.where, view, p.table, state):
                if problem.startswith("implicit:"):
                    out.warnings.append(f"{where}: {problem[len('implicit: '):]}")
                else:
                    out.errors.append(f"{where}: {problem}")


def _lint_predicate(p: Predicate, where: str, out: LintReport,
                    known_roots: set[str] | None, state: dict[str, Any] | None = None) -> None:
    if p.kind not in ALLOWED_KINDS:
        out.errors.append(f"{where}: unknown kind '{p.kind}'")
        return
    if p.op not in ALLOWED_OPS:
        out.errors.append(f"{where}: unknown operator '{p.op}'")
        return
    if p.kind in ("record", "scope"):
        _lint_record_predicate(p, where, out, state)
        return
    # Only operators that exist *solely* for kind 'record'/'scope' are errors here.
    # `added_count_*` etc. are shared: a `delta` on /records/<app>/<Table> (v0.2) and a
    # selector-scoped `record` form (v0.5) are both valid.
    record_only = FIELD_OPS | {"fields_unchanged_except", "count_le"}
    if p.op in record_only or p.op in SCOPE_OPS:
        out.errors.append(
            f"{where}: operator '{p.op}' requires kind 'record' (or 'scope'), got '{p.kind}'")
        return
    # kind/operator mismatches are unambiguous and are reinterpreted by the evaluator
    # (operator decides the semantics), so they are warnings, not errors. The rate of
    # these warnings is reported as a compiler-quality metric for RQ1.
    if p.kind == "delta" and p.op not in DELTA_ONLY_OPS:
        out.warnings.append(
            f"{where}: operator '{p.op}' is a property of the final state; "
            "kind should be 'state'"
        )
    if p.kind == "state" and p.op in DELTA_ONLY_OPS:
        out.warnings.append(
            f"{where}: operator '{p.op}' compares S0 with S1; kind should be 'delta'"
        )
    if not isinstance(p.path, str) or not p.path:
        out.errors.append(f"{where}: empty path")
        return
    if not p.path.startswith("/") and "." not in p.path and p.path not in ("$",):
        out.warnings.append(f"{where}: path '{p.path}' is a bare key; prefer JSON-Pointer form")
    if p.op in INT_VALUE_OPS:
        if isinstance(p.value, bool) or not isinstance(p.value, int):
            out.errors.append(f"{where}: operator '{p.op}' requires an integer value, got {p.value!r}")
    if p.op in COLLECTION_VALUE_OPS and not isinstance(p.value, (list, dict, str)):
        out.errors.append(f"{where}: operator '{p.op}' requires a collection value, got {p.value!r}")
    if p.op in VALUELESS_OPS and p.value not in (None, True, False):
        out.warnings.append(f"{where}: operator '{p.op}' ignores value {p.value!r}")

    # Wildcards are not part of the DSL. A path containing one never resolves, so the
    # predicate is silently vacuous -- a contract weakness that must not pass unnoticed.
    if any(seg in ("*", "**") or "*" in seg for seg in p.path.split("/")):
        out.errors.append(
            f"{where}: path '{p.path}' uses a wildcard, which the DSL does not support; "
            "enumerate the concrete paths instead"
        )

    # Path root validation depends on the environment's state structure.
    # AppWorld uses /counts/ and /records/ as top-level roots (with app names nested).
    # ToyWorld uses flat roots like /config/, /db/, /files/, etc.
    # Only enforce AppWorld-style root validation when the state clearly has
    # "counts" and "records" as known roots.
    if p.path.startswith("/") and known_roots is not None:
        parts = p.path.split("/")
        if len(parts) >= 2:
            root = parts[1]
            if "counts" in known_roots and "records" in known_roots:
                # AppWorld-style state: only /counts/ and /records/ are valid roots for
                # state/delta predicates. (/fields/ is addressed by kind 'record'.)
                if root not in ("counts", "records"):
                    out.errors.append(
                        f"{where}: path '{p.path}' uses invalid root '/{root}'; "
                        "AppWorld state requires paths to start with '/counts/' or '/records/'"
                    )
            else:
                # ToyWorld-style state: check against the actual known roots
                if root not in known_roots:
                    out.errors.append(
                        f"{where}: path '{p.path}' uses unknown root '/{root}'; "
                        f"known roots: {sorted(known_roots)}"
                    )

    # A path under a valid root must resolve to something in the state the contract is
    # evaluated on. v0.4's `/records/venmo.PaymentRequest` (dot for slash) passed the root
    # check, resolved to nothing, and made `updated_count_ge 1` silently false.
    if state is not None and p.path.startswith("/") and known_roots is not None:
        parts = [x for x in p.path.split("/") if x]
        if parts and parts[0] in ("counts", "records") and len(parts) >= 2:
            if _resolve(state, "/" + "/".join(parts[:3])) is _MISSING:
                out.errors.append(
                    f"{where}: path '{p.path}' does not resolve in the state "
                    f"(no table '{'/'.join(parts[1:3])}' under /{parts[0]}); "
                    "table names are '<app>/<Table>' with a slash")

    # Record-set delta operators need a {record_id: record_hash} mapping. When the
    # state is available we can prove statically that the path holds something else --
    # a row count, say -- instead of letting it surface as a fail-closed evaluation
    # error at gate time. Observed on AppWorld: `removed_count_eq` aimed at
    # /counts/<app>/<Table> (an int) rather than /records/<app>/<Table>.
    if p.op in RECORD_DELTA_OPS and state is not None and p.path.startswith("/"):
        target = _resolve(state, p.path)
        if target is not _MISSING and not isinstance(target, dict):
            out.errors.append(
                f"{where}: operator '{p.op}' needs a record mapping, but '{p.path}' "
                f"holds {type(target).__name__}; point it at a record set instead"
            )

    # A predicate that cannot fail is not an oracle. `*_count_ge 0` is trivially true
    # for any state, so it is schema-valid, lint-clean, and worthless. Observed on
    # AppWorld `229360a_1`: `/records/spotify/UserLibraryAlbum added_count_ge 0`, inside
    # a contract that otherwise passed every static check.
    if p.op in RECORD_DELTA_OPS and p.op.endswith("_ge") and p.value == 0:
        out.errors.append(
            f"{where}: operator '{p.op}' with value 0 is trivially true and can never "
            "fail; use the matching '_eq 0' to assert that nothing changed, or a "
            "positive threshold to require a change"
        )

    # Collection operators against a record-hash mapping cannot mean what they say.
    # `/records/<app>/<Table>` holds {record_id: record_hash}, so `contains [<content>]`
    # is asking about a field value the view does not carry. Without this the mismatch
    # reaches the evaluator and raises `TypeError: unhashable type: 'list'`, caught
    # fail-closed -- correct, but a raw TypeError is not a diagnosis and arrives too
    # late for the repair loop to act on. Observed on AppWorld `cf6abd2_1`.
    if p.op in COLLECTION_VALUE_OPS | {"contains", "not_contains"} and state is not None \
            and p.path.startswith("/records/"):
        target = _resolve(state, p.path)
        if isinstance(target, dict) and isinstance(p.value, (list, dict)):
            out.errors.append(
                f"{where}: operator '{p.op}' with a collection value cannot be applied to "
                f"'{p.path}', which holds a record-id -> record-hash mapping. Field-level "
                "predicates about record contents are not expressible; constrain the "
                "record set with a *_count_* operator instead"
            )

    if known_roots is not None and p.path.startswith("/"):
        root = p.path.split("/")[1] if len(p.path.split("/")) > 1 else ""
        if root and root not in known_roots:
            out.errors.append(
                f"{where}: path root '/{root}' does not exist in the environment state schema "
                f"(known roots: {sorted(known_roots)})"
            )


def lint_contract(
    contract: EffectContract,
    initial_state: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> LintReport:
    """Static checks that do not depend on any particular final state."""
    out = LintReport()
    known_roots = set(initial_state.keys()) if isinstance(initial_state, dict) else None

    if contract.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        out.warnings.append(
            f"contract_version '{contract.contract_version}' is not one of "
            f"{list(SUPPORTED_CONTRACT_VERSIONS)} (current: {CONTRACT_VERSION})"
        )
    if task_id is not None and contract.task_id != task_id:
        out.warnings.append(f"task_id '{contract.task_id}' != expected '{task_id}'")

    if not contract.required and not contract.alternatives:
        out.errors.append("contract has no required effects and no alternatives: it accepts everything")

    for i, p in enumerate(contract.required):
        _lint_predicate(p, f"required[{i}]", out, known_roots, initial_state)
    for i, p in enumerate(contract.forbidden):
        _lint_predicate(p, f"forbidden[{i}]", out, known_roots, initial_state)
    for i, p in enumerate(contract.invariants):
        _lint_predicate(p, f"invariants[{i}]", out, known_roots, initial_state)
    for gi, grp in enumerate(contract.alternatives):
        if not grp:
            out.errors.append(f"alternatives[{gi}]: empty group")
        for i, p in enumerate(grp):
            _lint_predicate(p, f"alternatives[{gi}][{i}]", out, known_roots, initial_state)

    return out


def ollama_format_schema(vocab: dict[str, Any] | None = None) -> dict[str, Any]:
    """A decoding-constraint schema for Ollama's structured-output ``format`` field.

    This is deliberately *not* the validation schema: Ollama's grammar backend does
    not support ``$ref``/``$defs`` reliably, and ``additionalProperties: false`` plus
    an untyped ``value`` field interacts badly with constrained decoding. The emitted
    object is still validated afterwards against the real schema.
    """
    # `value` must be a scalar or a flat array. Leaving it untyped (`{}`) lets the
    # constrained decoder emit *anything* there, and local models reliably drift into
    # nesting a whole predicate object inside `value`. Restricting the type kills that
    # failure mode and costs nothing: no operator in the DSL takes an object value.
    scalar = {"type": ["string", "number", "boolean", "null"]}
    # v0.4 `where`: {field: {op: scalar|array}}. Typed two levels deep on purpose --
    # the 2026-08-11 finding was that an untyped slot in the decoding grammar becomes an
    # attractor for structural hallucination, so the selector is constrained to exactly
    # the shape the evaluator accepts.
    # v0.5: a condition value may also be a sub-selection {table, field, where}. The
    # grammar allows an object here; the lint enforces its exact shape and typing.
    condition = {"type": "object",
                 "additionalProperties": {"type": ["string", "number", "boolean", "null", "array", "object"],
                                          "items": scalar}}
    # v0.6: schema-constrained decoding. When a per-task vocabulary is given, `table`
    # and `field` are enumerated from the projected schema, so an invented table or
    # field cannot be produced at all. Selector *keys* are left open (grammar backends
    # do not reliably support propertyNames); the linker binds those.
    table_schema: dict[str, Any] = {"type": ["string", "null"]}
    field_schema: dict[str, Any] = {"type": ["string", "null"]}
    if vocab:
        if vocab.get("tables"):
            table_schema = {"type": ["string", "null"], "enum": sorted(vocab["tables"]) + [None]}
        if vocab.get("fields"):
            field_schema = {"type": ["string", "null"], "enum": sorted(vocab["fields"]) + [None]}
    predicate = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(ALLOWED_KINDS)},
            "path": {"type": "string"},
            "op": {"type": "string", "enum": sorted(ALLOWED_OPS)},
            # `value` also admits an object so `field_transition` can carry {from, to}.
            "value": {"type": ["string", "number", "boolean", "null", "array", "object"],
                      "items": scalar, "additionalProperties": scalar},
            "description": {"type": "string"},
            "critical": {"type": "boolean"},
            "table": table_schema,
            "where": {"type": ["object", "null"], "additionalProperties": condition},
            "field": field_schema,
        },
        "required": ["kind", "path", "op", "value", "description", "critical",
                     "table", "where", "field"],
    }
    return {
        "type": "object",
        "properties": {
            "contract_version": {"type": "string"},
            "task_id": {"type": "string"},
            "required": {"type": "array", "items": predicate},
            "forbidden": {"type": "array", "items": predicate},
            "invariants": {"type": "array", "items": predicate},
            "alternatives": {"type": "array", "items": {"type": "array", "items": predicate}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "contract_version", "task_id", "required",
            "forbidden", "invariants", "alternatives", "assumptions",
        ],
    }
