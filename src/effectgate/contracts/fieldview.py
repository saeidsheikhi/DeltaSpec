"""Typed, field-aware state view: canonical form, selectors, and `record`/`scope` predicates.

DSL v0.4. Design and rationale: notes/DECISIONS.md (2026-09-05, "Field-aware AppWorld
representation"). The rules that matter for correctness:

* **Deterministic and total.** Same (predicate, S0, S1) always yields the same verdict or
  the same ``ContractEvalError``. Nothing here depends on ordering, wall-clock, or randomness.
* **Fail closed.** An unknown table or field, a type mismatch, a selector over a truncated
  projection, or a malformed selector raises ``ContractEvalError``. It is never ``False``.
* **No code execution.** A selector is data -- ``{field: {op: value}}`` -- interpreted by a
  fixed set of typed comparisons. There is no expression language.
* **Self-describing state.** Every projected table carries its own ``field_types``, so
  evaluation needs no external catalogue and a snapshot replays identically anywhere.

The projection shape this module evaluates over (built by ``scripts/appworld_worker.py``):

    state["fields"][app][Table] = {
        "scope": "owner" | "fk_hop",
        "n_total": int, "n_projected": int, "truncated": bool,
        "field_types": {field: "int"|"str"|"bool"|"float"|"datetime"|"list"|"dict"|"null"},
        "rows": {record_id: {field: canonical_value}},
    }
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from effectgate.models import (
    FIELD_OPS,
    MAX_RELATION_DEPTH,
    RECORD_DELTA_FIELD_OPS,
    RECORD_OPS,
    RECORD_POST_OPS,
    RECORD_SET_DELTA_OPS,
    RELATED_OPS,
    SCOPE_OPS,
    WHERE_OPS,
    ContractEvalError,
    Predicate,
)

#: Pydantic/SQLModel type names collapsed to the DSL's canonical type vocabulary.
CANONICAL_TYPES = frozenset({"int", "float", "bool", "str", "datetime", "list", "dict", "null"})

_TYPE_ALIASES = {
    "int": "int", "ConstrainedIntValue": "int", "PositiveInt": "int", "NonNegativeInt": "int",
    "float": "float", "ConstrainedFloatValue": "float", "Decimal": "float",
    "bool": "bool",
    "str": "str", "ConstrainedStrValue": "str", "EmailStr": "str", "Literal": "str",
    "SecretStr": "str", "HttpUrl": "str",
    "datetime": "datetime", "date": "datetime",
    "list": "list", "List": "list", "tuple": "list", "set": "list",
    "dict": "dict", "Dict": "dict", "Json": "dict",
    "NoneType": "null",
}


def canonical_type(type_name: str) -> str:
    """Map an installed schema's type name onto the DSL's canonical type vocabulary."""
    if type_name in _TYPE_ALIASES:
        return _TYPE_ALIASES[type_name]
    low = type_name.lower()
    for key in ("datetime", "int", "float", "bool", "str", "list", "dict"):
        if key in low:
            return _TYPE_ALIASES[key]
    return "str"


# ------------------------------------------------------------------ canonical form
def canonical_value(v: Any) -> Any:
    """A JSON-serialisable, order-stable form of a field value.

    ``datetime`` -> ISO-8601 string (naive values are left naive; AppWorld's clock is
    frozen and naive). ``float`` stays a float; JSON serialisation uses ``repr`` via
    ``json.dumps``. Lists keep their order -- it is data. Dicts are sorted at
    serialisation time. Anything else falls back to ``str``.
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (list, tuple, set)):
        return [canonical_value(x) for x in (sorted(v, key=repr) if isinstance(v, set) else v)]
    if isinstance(v, dict):
        return {str(k): canonical_value(x) for k, x in v.items()}
    return str(v)


def canonical_json(obj: Any) -> str:
    return json.dumps(canonical_value(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fields_hash(fields_tier: dict[str, Any]) -> str:
    """Stable SHA-256 of the field projection; equal projections hash equal."""
    return hashlib.sha256(canonical_json(fields_tier).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- resolution
def _split_table(table: str) -> tuple[str, str]:
    if not isinstance(table, str) or table.count(".") != 1:
        raise ContractEvalError(f"table must be '<app>.<Table>', got {table!r}")
    app, name = table.split(".")
    if not app or not name:
        raise ContractEvalError(f"table must be '<app>.<Table>', got {table!r}")
    return app, name


def resolve_table(state: dict[str, Any], table: str) -> dict[str, Any]:
    """The projected view of ``table`` in ``state``; fail closed if not projected."""
    app, name = _split_table(table)
    fields = state.get("fields") if isinstance(state, dict) else None
    if not isinstance(fields, dict):
        raise ContractEvalError(
            "this state carries no field projection ('fields' tier); record predicates "
            "need the field-aware representation")
    view = (fields.get(app) or {}).get(name) if isinstance(fields.get(app), dict) else None
    if not isinstance(view, dict) or "rows" not in view:
        known = sorted(f"{a}.{t}" for a, ts in fields.items() if isinstance(ts, dict) for t in ts)
        raise ContractEvalError(
            f"table '{table}' is not in the field projection; projected tables: {known[:40]}")
    return view


# -------------------------------------------------------------- typed comparison
def _check_type(field: str, ftype: str, value: Any, op: str) -> None:
    """Reject comparisons the schema says are meaningless. Fail closed, not False."""
    if value is None and op in ("eq", "ne"):
        return  # null is comparable to anything by equality
    if ftype == "int" and not (isinstance(value, int) and not isinstance(value, bool)):
        raise ContractEvalError(f"field '{field}' is int; {op} against {value!r} is not typed")
    if ftype == "float" and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
        raise ContractEvalError(f"field '{field}' is float; {op} against {value!r} is not typed")
    if ftype == "bool" and not isinstance(value, bool):
        raise ContractEvalError(f"field '{field}' is bool; {op} against {value!r} is not typed")
    if ftype in ("str", "datetime") and not isinstance(value, str):
        raise ContractEvalError(f"field '{field}' is {ftype}; {op} against {value!r} is not typed")


def _compare(field: str, ftype: str, got: Any, op: str, exp: Any) -> bool:
    """One typed comparison. ``got`` is the record's field value, ``exp`` the literal."""
    if op == "is_null":
        if not isinstance(exp, bool):
            raise ContractEvalError(f"'is_null' takes true/false, got {exp!r}")
        return (got is None) == exp
    if op in ("in", "not_in"):
        if not isinstance(exp, list):
            raise ContractEvalError(f"'{op}' on field '{field}' needs a list of values, got {exp!r}")
        for item in exp:
            _check_type(field, ftype, item, op)
        hit = got in exp
        return hit if op == "in" else not hit
    if op in ("contains", "not_contains"):
        if ftype == "list":
            if not isinstance(got, list):
                raise ContractEvalError(f"field '{field}' declared list holds {type(got).__name__}")
            hit = exp in got
        elif ftype == "str":
            if not isinstance(exp, str):
                raise ContractEvalError(f"'contains' on str field '{field}' needs a string, got {exp!r}")
            hit = isinstance(got, str) and exp in got
        else:
            raise ContractEvalError(
                f"'{op}' is only defined for list and str fields; '{field}' is {ftype}")
        return hit if op == "contains" else not hit
    if got is None:
        # A null field is neither equal to, nor ordered against, a non-null literal.
        return op == "ne" and exp is not None
    _check_type(field, ftype, exp, op)
    if op == "eq":
        return got == exp
    if op == "ne":
        return got != exp
    if op in ("gt", "ge", "lt", "le"):
        if ftype not in ("int", "float", "datetime", "str"):
            raise ContractEvalError(f"ordering '{op}' is not defined for {ftype} field '{field}'")
        if ftype == "datetime":
            try:
                a, b = _dt.datetime.fromisoformat(str(got)), _dt.datetime.fromisoformat(str(exp))
            except ValueError as exc:
                raise ContractEvalError(
                    f"datetime field '{field}': {exc}; use ISO-8601 like 2023-05-18T12:00:00") from exc
        else:
            a, b = got, exp
        return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
    raise ContractEvalError(f"unknown selector operator '{op}'")


# ------------------------------------------------------------------ relations
def _fk_target(view: dict[str, Any], field: str) -> str | None:
    """'<app>.<Table>' a projected view declares `field` to reference, else None."""
    fks = view.get("foreign_keys") or {}
    return fks.get(field)


def relation_is_declared(this_table: str, this_view: dict, this_field: str,
                         other_table: str, other_view: dict, other_field: str) -> tuple[bool, str]:
    """Is `this.this_field <-> other.other_field` backed by a declared foreign key?

    Accepted shapes, all read from the projection's `foreign_keys` (which the worker
    takes from SQLAlchemy's column metadata -- nothing is inferred from names):

      * this.f  -> other.id              (this.f is an FK to `other`)
      * other.g -> this.id               (other.g is an FK to `this`)
      * this.f  -> Z.id  and  other.g -> Z.id   (both reference the same table)

    A `list`-typed field (`Song.artist_ids`) has no FK column; the relation is then
    typed set membership against `other.id` and is reported as *implicit*, so the lint
    can surface it as a warning rather than silently treating it as declared.
    """
    pk = this_view.get("primary_key", "id")
    opk = other_view.get("primary_key", "id")
    t1 = _fk_target(this_view, this_field)
    t2 = _fk_target(other_view, other_field)
    if t1 == other_table and other_field == opk:
        return True, "fk"
    if t2 == this_table and this_field == pk:
        return True, "fk"
    if t1 and t2 and t1 == t2:
        return True, "fk_shared_target"
    # Implicit list relations, symmetric: a `list`-typed field of ids on one side, paired
    # with the other table's primary key or a declared FK column on the other.
    #   Song.artist_ids (list)   <->  UserArtistFollowing.artist_id (FK -> Artist)
    #   UserArtistFollowing.artist_id (FK) <-> Song.artist_ids (list)
    #   MusicPlayer.queue_song_ids (list)  <->  Song.id (PK)
    ftypes = this_view.get("field_types") or {}
    oft = other_view.get("field_types") or {}
    this_is_key = this_field == pk or bool(t1)
    other_is_key = other_field == opk or bool(t2)
    if ftypes.get(this_field) == "list" and other_is_key:
        return True, "implicit_list"
    if oft.get(other_field) == "list" and this_is_key:
        return True, "implicit_list"
    return False, "none"


def _resolve_subselection(state: dict[str, Any], sub: Any, depth: int, ctx: str) -> tuple[set, str, dict]:
    """Evaluate ``{"table","field","where"}`` to the set of `field` values it selects."""
    if not isinstance(sub, dict) or not sub.get("table") or not sub.get("field"):
        raise ContractEvalError(
            f"{ctx}: a related selector needs {{\"table\": ..., \"field\": ..., \"where\": ...}}, "
            f"got {sub!r}")
    if depth > MAX_RELATION_DEPTH:
        raise ContractEvalError(f"{ctx}: relation nesting exceeds the bound of {MAX_RELATION_DEPTH}")
    table, field = str(sub["table"]), str(sub["field"])
    view = resolve_table(state, table)
    ftypes = view.get("field_types") or {}
    if field not in ftypes:
        raise ContractEvalError(f"{ctx}: table '{table}' has no field '{field}'; fields: {sorted(ftypes)}")
    rows = _select_rows_at(state, view, sub.get("where"), table, depth + 1)
    values: set = set()
    for row in rows.values():
        v = row.get(field)
        if isinstance(v, list):
            values.update(x for x in v if isinstance(x, (int, str)) and not isinstance(x, bool))
        elif v is not None:
            values.add(v)
    return values, table, view


# ------------------------------------------------------------------- selectors
def check_selector_types(where: Any, view: dict[str, Any], table: str, state: dict[str, Any] | None = None,
                         depth: int = 1) -> list[str]:
    """Static type check of a selector against the projected schema. Returns problems.

    Used by the lint so that a mistyped literal (an int field compared to a string, a
    `contains` on a bool) is a compile-time error the repair loop can act on, instead of
    a fail-closed evaluation error at gate time. Relations are checked for depth,
    shape, table/field existence and FK backing; an implicit list relation is reported
    with the `implicit:` prefix so callers can downgrade it to a warning.
    """
    problems: list[str] = []
    if where is None:
        return problems
    if not isinstance(where, dict):
        return [f"'where' on {table} must be an object"]
    ftypes = view.get("field_types") or {}
    for fname, cond in where.items():
        if fname not in ftypes:
            problems.append(f"table '{table}' has no field '{fname}'"); continue
        ftype = ftypes[fname]
        if not isinstance(cond, dict):
            cond = {"eq": cond}
        for op, exp in cond.items():
            if op not in WHERE_OPS:
                problems.append(f"selector operator '{op}' on '{fname}' is not allowed"); continue
            if op in RELATED_OPS:
                if depth > MAX_RELATION_DEPTH:
                    problems.append(f"relation nesting on '{fname}' exceeds {MAX_RELATION_DEPTH}"); continue
                if not isinstance(exp, dict) or not exp.get("table") or not exp.get("field"):
                    problems.append(f"'{op}' on '{fname}' needs {{table, field, where}}, got {exp!r}"); continue
                if op == "intersects_related" and ftype != "list":
                    problems.append(f"'intersects_related' needs a list field; '{fname}' is {ftype}"); continue
                if op != "intersects_related" and ftype == "list":
                    problems.append(f"'{op}' on list field '{fname}': use 'intersects_related'"); continue
                if state is None:
                    continue
                try:
                    oview = resolve_table(state, str(exp["table"]))
                except ContractEvalError as exc:
                    problems.append(f"'{op}' on '{fname}': {exc}"); continue
                oft = oview.get("field_types") or {}
                if str(exp["field"]) not in oft:
                    problems.append(f"'{op}' on '{fname}': table '{exp['table']}' has no field '{exp['field']}'"); continue
                ok, how = relation_is_declared(table, view, fname, str(exp["table"]), oview, str(exp["field"]))
                if not ok:
                    problems.append(
                        f"'{op}' on '{fname}': no declared foreign key links {table}.{fname} and "
                        f"{exp['table']}.{exp['field']}; relations must follow the schema")
                elif how == "implicit_list":
                    problems.append(f"implicit: '{fname}' is a list of ids related to {exp['table']}.{exp['field']} "
                                    "without a declared foreign key")
                problems.extend(f"in {exp['table']}: {q}" for q in
                                check_selector_types(exp.get("where"), oview, str(exp["table"]), state, depth + 1))
                continue
            # literal-valued operators: reuse the evaluator's typing rules on a probe value
            try:
                if op == "is_null":
                    if not isinstance(exp, bool):
                        raise ContractEvalError("'is_null' takes true/false")
                elif op in ("in", "not_in"):
                    if not isinstance(exp, list):
                        raise ContractEvalError(f"'{op}' needs a list of values")
                    for item in exp:
                        _check_type(fname, ftype, item, op)
                elif op in ("contains", "not_contains"):
                    if ftype not in ("list", "str"):
                        raise ContractEvalError(f"'{op}' is only defined for list and str fields; '{fname}' is {ftype}")
                    if ftype == "str" and not isinstance(exp, str):
                        raise ContractEvalError(f"'contains' on str field '{fname}' needs a string")
                elif op in ("gt", "ge", "lt", "le"):
                    if ftype not in ("int", "float", "datetime", "str"):
                        raise ContractEvalError(f"ordering '{op}' is not defined for {ftype} field '{fname}'")
                    _check_type(fname, ftype, exp, op)
                else:
                    _check_type(fname, ftype, exp, op)
            except ContractEvalError as exc:
                problems.append(str(exc))
    return problems


def _validate_where(where: Any, field_types: dict[str, str], table: str) -> dict[str, dict[str, Any]]:
    if where is None:
        return {}
    if not isinstance(where, dict):
        raise ContractEvalError(f"'where' on {table} must be an object, got {type(where).__name__}")
    out: dict[str, dict[str, Any]] = {}
    for fname, cond in where.items():
        if fname not in field_types:
            raise ContractEvalError(
                f"table '{table}' has no field '{fname}'; fields: {sorted(field_types)}")
        # A bare scalar is shorthand for equality: {"is_playing": true}.
        if not isinstance(cond, dict):
            cond = {"eq": cond}
        if not cond:
            raise ContractEvalError(f"empty condition for field '{fname}' on {table}")
        for op in cond:
            if op not in WHERE_OPS:
                raise ContractEvalError(
                    f"selector operator '{op}' on '{fname}' is not allowed; allowed: {sorted(WHERE_OPS)}")
        out[fname] = dict(cond)
    return out


def select_rows(view: dict[str, Any], where: Any, table: str,
                state: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Rows of ``view`` matching every condition in ``where``. Fail closed on truncation.

    ``state`` is needed only when ``where`` contains a related sub-selection; without it a
    relation is an evaluation error rather than a guess.
    """
    return _select_rows_at(state, view, where, table, 1)


def _select_rows_at(state: dict[str, Any] | None, view: dict[str, Any], where: Any, table: str,
                    depth: int) -> dict[str, dict[str, Any]]:
    ftypes = view.get("field_types") or {}
    conds = _validate_where(where, ftypes, table)
    if view.get("truncated"):
        raise ContractEvalError(
            f"table '{table}' was truncated in the projection "
            f"({view.get('n_projected')} of {view.get('n_total')} rows); a selector over it "
            "cannot be evaluated deterministically -- representability error")
    rows = view.get("rows") or {}
    if not isinstance(rows, dict):
        raise ContractEvalError(f"table '{table}' rows are not a mapping")
    # Resolve every relation once, before scanning rows: deterministic and bounded.
    resolved: dict[tuple[str, str], set] = {}
    for fname, cond in conds.items():
        for op, exp in cond.items():
            if op in RELATED_OPS:
                if state is None:
                    raise ContractEvalError(f"'{op}' on '{fname}' needs the full state to resolve")
                values, otable, oview = _resolve_subselection(state, exp, depth, f"{table}.{fname}")
                ok, how = relation_is_declared(table, view, fname, otable, oview, str(exp["field"]))
                if not ok:
                    raise ContractEvalError(
                        f"'{op}' on '{fname}': no declared foreign key links {table}.{fname} and "
                        f"{otable}.{exp['field']}")
                if op == "intersects_related" and ftypes.get(fname) != "list":
                    raise ContractEvalError(f"'intersects_related' needs a list field; '{fname}' is {ftypes.get(fname)}")
                if op != "intersects_related" and ftypes.get(fname) == "list":
                    raise ContractEvalError(f"'{op}' on list field '{fname}': use 'intersects_related'")
                resolved[(fname, op)] = values
    out: dict[str, dict[str, Any]] = {}
    for rid, row in rows.items():
        ok = True
        for fname, cond in conds.items():
            if fname not in row:
                raise ContractEvalError(f"record {rid} of '{table}' has no field '{fname}'")
            for op, exp in cond.items():
                if op in RELATED_OPS:
                    vals = resolved[(fname, op)]
                    got = row[fname]
                    if op == "intersects_related":
                        hit = isinstance(got, list) and any(x in vals for x in got)
                    else:
                        hit = got in vals
                        if op == "not_in_related":
                            hit = not hit
                    if not hit:
                        ok = False
                        break
                    continue
                if not _compare(fname, ftypes[fname], row[fname], op, exp):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            out[str(rid)] = row
    return out


# ------------------------------------------------------------------ predicates
def _int_value(pred: Predicate) -> int:
    v = pred.value
    if isinstance(v, bool) or not isinstance(v, int):
        raise ContractEvalError(f"operator '{pred.op}' requires an integer value, got {v!r}")
    return v


def _field_of(pred: Predicate, view: dict[str, Any]) -> tuple[str, str]:
    if not pred.field:
        raise ContractEvalError(f"operator '{pred.op}' needs a 'field'")
    ftypes = view.get("field_types") or {}
    if pred.field not in ftypes:
        raise ContractEvalError(
            f"table '{pred.table}' has no field '{pred.field}'; fields: {sorted(ftypes)}")
    return pred.field, ftypes[pred.field]


def eval_record_predicate(pred: Predicate, initial_state: dict, final_state: dict) -> bool:
    """Evaluate a ``kind: record`` predicate. Never returns None; raises on anything unclear."""
    if pred.op not in RECORD_OPS:
        raise ContractEvalError(f"operator '{pred.op}' is not a record operator")
    if not pred.table:
        raise ContractEvalError("record predicate needs a 'table' ('<app>.<Table>')")

    if pred.op in RECORD_SET_DELTA_OPS:
        # Rows matching the selector that were ADDED (in S1, absent in S0 by id) or
        # REMOVED (in S0, absent in S1). The selector is applied in the state where the
        # row exists. This is the typed, selector-scoped form of the v0.2 delta ops.
        pre_view = resolve_table(initial_state, pred.table)
        post_view = resolve_table(final_state, pred.table)
        n_exp = _int_value(pred)
        if pred.op.startswith("added"):
            post = select_rows(post_view, pred.where, pred.table, final_state)
            n = sum(1 for rid in post if rid not in (pre_view.get("rows") or {}))
        else:
            pre = select_rows(pre_view, pred.where, pred.table, initial_state)
            n = sum(1 for rid in pre if rid not in (post_view.get("rows") or {}))
        return n >= n_exp if pred.op.endswith("_ge") else n == n_exp

    if pred.op in RECORD_POST_OPS:
        view = resolve_table(final_state, pred.table)
        matched = select_rows(view, pred.where, pred.table, final_state)
        n = len(matched)
        if pred.op == "exists":
            return n > 0
        if pred.op == "absent":
            return n == 0
        if pred.op == "count_eq":
            return n == _int_value(pred)
        if pred.op == "count_ge":
            return n >= _int_value(pred)
        if pred.op == "count_le":
            return n <= _int_value(pred)
        # field_* over every matched record in the final state. An empty match is a
        # failure, not a vacuous truth: "every record has X" with no records would let
        # a contract pass on a task that did nothing.
        fname, ftype = _field_of(pred, view)
        if n == 0:
            return False
        op = {"field_eq": "eq", "field_ne": "ne", "field_in": "in", "field_contains": "contains"}[pred.op]
        return all(_compare(fname, ftype, row.get(fname), op, pred.value) for row in matched.values())

    # Delta family: select in S0, match in S1 by id.
    assert pred.op in RECORD_DELTA_FIELD_OPS
    pre_view = resolve_table(initial_state, pred.table)
    post_view = resolve_table(final_state, pred.table)
    before = select_rows(pre_view, pred.where, pred.table, initial_state)
    if not before:
        return False
    post_rows = post_view.get("rows") or {}

    if pred.op == "fields_unchanged_except":
        allowed = pred.value if isinstance(pred.value, list) else ([] if pred.value is None else None)
        if allowed is None or not all(isinstance(x, str) for x in allowed):
            raise ContractEvalError("'fields_unchanged_except' takes a list of field names (may be empty)")
        ftypes = pre_view.get("field_types") or {}
        for x in allowed:
            if x not in ftypes:
                raise ContractEvalError(f"table '{pred.table}' has no field '{x}'")
        for rid, row in before.items():
            after = post_rows.get(rid)
            if after is None:
                return False  # the record was deleted; that is a change
            for fname, v in row.items():
                if fname in allowed:
                    continue
                if after.get(fname) != v:
                    return False
        return True

    fname, ftype = _field_of(pred, pre_view)
    if pred.op == "field_transition":
        if not isinstance(pred.value, dict) or not {"from", "to"} <= set(pred.value):
            raise ContractEvalError("'field_transition' takes value {\"from\": ..., \"to\": ...}")
        frm, to = pred.value["from"], pred.value["to"]
        _check_type(fname, ftype, frm, "eq")
        _check_type(fname, ftype, to, "eq")
        for rid, row in before.items():
            after = post_rows.get(rid)
            if after is None or row.get(fname) != frm or after.get(fname) != to:
                return False
        return True

    changed_any = False
    for rid, row in before.items():
        after = post_rows.get(rid)
        if after is None:
            raise ContractEvalError(
                f"record {rid} of '{pred.table}' selected in S0 is absent in S1; "
                f"'{pred.op}' is undefined for a deleted record -- assert on removal instead")
        if after.get(fname) != row.get(fname):
            changed_any = True
            if pred.op == "field_unchanged":
                return False
    if pred.op == "field_unchanged":
        return True
    # field_changed: every selected record's field differs.
    return all(post_rows[rid].get(fname) != row.get(fname) for rid, row in before.items())


def changed_tables(initial_state: dict, final_state: dict) -> set[str]:
    """Tables whose record-hash map differs between S0 and S1 (tier 0, whole world)."""
    pre = (initial_state.get("records") or {}) if isinstance(initial_state, dict) else {}
    post = (final_state.get("records") or {}) if isinstance(final_state, dict) else {}
    if not isinstance(pre, dict) or not isinstance(post, dict):
        raise ContractEvalError("'records' tier is missing; scope predicates need it")
    out: set[str] = set()
    for app in set(pre) | set(post):
        a, b = pre.get(app) or {}, post.get(app) or {}
        for t in set(a) | set(b):
            if (a.get(t) or {}) != (b.get(t) or {}):
                out.add(f"{app}.{t}")
    return out


def eval_scope_predicate(pred: Predicate, initial_state: dict, final_state: dict) -> bool:
    if pred.op not in SCOPE_OPS:
        raise ContractEvalError(f"operator '{pred.op}' is not a scope operator")
    allowed = pred.value
    if not isinstance(allowed, list) or not all(isinstance(x, str) and x.count(".") == 1 for x in allowed):
        raise ContractEvalError(
            "'changed_tables_subset' takes a list of '<app>.<Table>' names, got "
            f"{allowed!r}")
    return changed_tables(initial_state, final_state) <= set(allowed)


def eval_v04_predicate(pred: Predicate, initial_state: dict, final_state: dict) -> bool:
    if pred.kind == "record":
        return eval_record_predicate(pred, initial_state, final_state)
    if pred.kind == "scope":
        return eval_scope_predicate(pred, initial_state, final_state)
    raise ContractEvalError(f"kind '{pred.kind}' is not a v0.4 predicate kind")


__all__ = [
    "CANONICAL_TYPES", "FIELD_OPS", "canonical_type", "canonical_value", "canonical_json",
    "fields_hash", "resolve_table", "select_rows", "check_selector_types",
    "relation_is_declared", "eval_record_predicate", "eval_scope_predicate",
    "eval_v04_predicate", "changed_tables",
]
