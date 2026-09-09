from __future__ import annotations

import hashlib
import json
from typing import Any

from effectgate.models import CONTRACT_VERSION, EffectContract, Predicate

_PRED_KEYS = {"kind", "path", "op", "value", "description", "critical",
              # v0.4 record/scope predicates
              "table", "where", "field"}
_CONTRACT_KEYS = {
    "contract_version", "task_id", "required", "forbidden",
    "invariants", "alternatives", "assumptions", "metadata",
}


def predicate_from_dict(d: dict[str, Any]) -> Predicate:
    kind = d["kind"]
    path = d.get("path")
    table = d.get("table")
    # A v0.4 predicate is addressed by `table`; its `path` is derived, never authored.
    if kind in ("record", "scope") and not path:
        path = f"/fields/{str(table).replace('.', '/')}" if table else "/records"
    return Predicate(
        kind=kind, path=path, op=d["op"],
        value=d.get("value"), description=d.get("description", ""),
        critical=bool(d.get("critical", True)),
        table=table if kind in ("record", "scope") else None,
        where=d.get("where") if kind == "record" else None,
        field=d.get("field") if kind == "record" else None,
    )


def contract_from_dict(d: dict[str, Any]) -> EffectContract:
    return EffectContract(
        contract_version=d["contract_version"],
        task_id=d["task_id"],
        required=[predicate_from_dict(x) for x in d.get("required", [])],
        forbidden=[predicate_from_dict(x) for x in d.get("forbidden", [])],
        invariants=[predicate_from_dict(x) for x in d.get("invariants", [])],
        alternatives=[[predicate_from_dict(x) for x in grp] for grp in d.get("alternatives", [])],
        assumptions=list(d.get("assumptions", [])),
        metadata=dict(d.get("metadata", {})),
    )


def normalize_raw(obj: Any, task_id: str) -> tuple[dict[str, Any], list[str]]:
    """Apply minimal, purely syntactic repairs to raw compiler output.

    These fixes never change the *meaning* of a predicate. They exist so that
    RQ1 can distinguish "the model wrote a semantically inadequate contract" from
    "the model omitted an empty optional array". Every applied fix is returned so
    it can be reported.
    """
    fixes: list[str] = []
    if not isinstance(obj, dict):
        return {}, ["output was not a JSON object"]
    out = dict(obj)

    # Some models wrap the contract, e.g. {"contract": {...}} or {"effect_contract": {...}}.
    if not (_CONTRACT_KEYS & set(out.keys())):
        for wrapper in ("contract", "effect_contract", "result", "output"):
            if isinstance(out.get(wrapper), dict):
                out = dict(out[wrapper])
                fixes.append(f"unwrapped '{wrapper}'")
                break

    # A model sometimes emits a single *predicate* as the whole response. Say so
    # plainly: without this the predicate's keys get swept into metadata and the
    # contract fails lint as "no required effects", which describes the symptom and
    # hides the cause. It is not wrapped into a contract on purpose -- inventing the
    # clause the model failed to choose would fabricate intent.
    if not (_CONTRACT_KEYS & set(out.keys())) and {"kind", "path", "op"} <= set(out.keys()):
        fixes.append("MALFORMED: the response was a single bare predicate, not a contract "
                     "object; it names no clause (required/forbidden/invariants)")

    if "contract_version" not in out:
        out["contract_version"] = CONTRACT_VERSION
        fixes.append("filled missing contract_version")
    else:
        out["contract_version"] = str(out["contract_version"])
    if not out.get("task_id"):
        out["task_id"] = task_id
        fixes.append("filled missing task_id")
    else:
        out["task_id"] = str(out["task_id"])

    for key in ("required", "forbidden", "invariants", "alternatives", "assumptions"):
        if key not in out or out[key] is None:
            out[key] = []
            fixes.append(f"filled missing {key}")
        elif not isinstance(out[key], list):
            out[key] = [out[key]]
            fixes.append(f"wrapped scalar {key} in a list")

    out["assumptions"] = [str(a) for a in out["assumptions"]]

    def fix_pred(p: Any, where: str) -> dict[str, Any] | None:
        if not isinstance(p, dict):
            fixes.append(f"dropped non-object predicate at {where}")
            return None
        q = {k: v for k, v in p.items() if k in _PRED_KEYS}
        if set(p) - _PRED_KEYS:
            fixes.append(f"dropped extra predicate keys at {where}: {sorted(set(p) - _PRED_KEYS)}")
        kind_raw = str(q.get("kind", "")).strip().lower()
        # v0.4: a record/scope predicate is addressed by `table`, and its `path` is
        # synthesised so the schema, the lint and the hash all see one canonical form.
        if kind_raw in ("record", "scope"):
            table = q.get("table")
            if isinstance(table, str) and "/" in table and "." not in table:
                q["table"] = table = table.strip("/").replace("/", ".")
                fixes.append(f"{where}: rewrote table path as '<app>.<Table>'")
            if kind_raw == "record" and not table:
                fixes.append(f"dropped record predicate without a table at {where}")
                return None
            if not q.get("path"):
                q["path"] = f"/fields/{str(table).replace('.', '/')}" if table else "/records"
                fixes.append(f"{where}: derived path from table")
        if "kind" not in q or "path" not in q or "op" not in q:
            fixes.append(f"dropped predicate missing kind/path/op at {where}")
            return None
        q["kind"] = kind_raw
        q["op"] = str(q["op"]).strip().lower()
        q["path"] = str(q["path"]).strip()
        if q["kind"] not in ("record", "scope"):
            for k in ("table", "where", "field"):
                if k in q:
                    q.pop(k)
                    fixes.append(f"{where}: dropped '{k}' on a {q['kind']} predicate")
        elif isinstance(q.get("where"), dict):
            # Selector-operator spelling is syntax, not meaning: `neq` and `ne` cannot
            # differ in intent. Observed on b0a8eae_1 (v0.4): a correct field_transition
            # contract never evaluated because one selector said `neq`.
            n = _normalise_where_ops(q["where"], f"{where}.where", fixes)
            q["where"] = n
        q.setdefault("value", None)
        q["description"] = str(q.get("description") or "")
        crit = q.get("critical", True)
        q["critical"] = crit if isinstance(crit, bool) else str(crit).strip().lower() in ("true", "1", "yes")
        # count operators need an integer; "2" and 2.0 mean the same thing and are
        # coerced. A non-integral value is left alone so the lint reports it.
        if q["op"] in ("count_eq", "count_ge", "count_le"):
            v = q.get("value")
            if isinstance(v, str) and v.strip().lstrip("+-").isdigit():
                q["value"] = int(v.strip())
                fixes.append(f"{where}: coerced string count value {v!r} to int")
            elif isinstance(v, float) and v.is_integer():
                q["value"] = int(v)
                fixes.append(f"{where}: coerced float count value {v!r} to int")
        return q

    for key in ("required", "forbidden", "invariants"):
        cleaned = [fix_pred(p, f"{key}[{i}]") for i, p in enumerate(out[key])]
        out[key] = [p for p in cleaned if p is not None]

    alts = []
    for gi, grp in enumerate(out["alternatives"]):
        if isinstance(grp, dict):
            grp = [grp]
            fixes.append(f"wrapped alternatives[{gi}] predicate in a group")
        if not isinstance(grp, list):
            fixes.append(f"dropped malformed alternatives[{gi}]")
            continue
        cleaned = [fix_pred(p, f"alternatives[{gi}][{i}]") for i, p in enumerate(grp)]
        cleaned = [p for p in cleaned if p is not None]
        if cleaned:
            alts.append(cleaned)
    out["alternatives"] = alts

    md = out.get("metadata")
    out["metadata"] = dict(md) if isinstance(md, dict) else {}
    extra = set(out) - _CONTRACT_KEYS
    if extra:
        out["metadata"]["_dropped_keys"] = sorted(extra)
        fixes.append(f"moved unknown top-level keys to metadata: {sorted(extra)}")
        for k in extra:
            out.pop(k)

    return out, fixes


_WHERE_OP_ALIASES = {
    "neq": "ne", "!=": "ne", "==": "eq", "=": "eq", "equals": "eq", "not_eq": "ne",
    "gte": "ge", ">=": "ge", "lte": "le", "<=": "le", ">": "gt", "<": "lt",
    "notin": "not_in", "not-in": "not_in", "nin": "not_in", "includes": "contains",
    "not_includes": "not_contains", "isnull": "is_null", "null": "is_null",
    "in_selected": "in_related", "not_in_selected": "not_in_related",
}


def _normalise_where_ops(where: dict[str, Any], ctx: str, fixes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fname, cond in where.items():
        if isinstance(cond, dict):
            new_cond: dict[str, Any] = {}
            for op, val in cond.items():
                op_s = str(op).strip().lower()
                canon = _WHERE_OP_ALIASES.get(op_s, op_s)
                if canon != op:
                    fixes.append(f"{ctx}.{fname}: normalised selector operator {op!r} -> {canon!r}")
                if isinstance(val, dict) and "table" in val and isinstance(val.get("where"), dict):
                    val = dict(val, where=_normalise_where_ops(val["where"], f"{ctx}.{fname}.{canon}", fixes))
                new_cond[canon] = val
            out[fname] = new_cond
        else:
            out[fname] = cond
    return out


#: Wrapper segments models prepend when they root a pointer at the request payload
#: (or at a name for the state) instead of at the state object itself.
_POINTER_WRAPPERS = frozenset({
    "initial_state", "final_state", "state", "s0", "s1", "world",
    "environment", "env", "root", "data",
})


def normalize_paths(obj: dict[str, Any], initial_state: dict[str, Any] | None) -> list[str]:
    """Strip a bogus leading pointer segment such as ``/initial_state``.

    Models frequently write ``/initial_state/config/log_level`` because that is where
    the state sat in the request payload. The referent is unambiguous, so this is
    corrected in place and reported, exactly like the other syntactic normalisations.
    Nothing is stripped unless the remainder starts with a real state root, so a task
    that genuinely has an ``/initial_state`` section is unaffected.
    """
    if not isinstance(initial_state, dict) or not initial_state:
        return []
    roots = set(initial_state.keys())
    fixes: list[str] = []

    def fix(p: dict[str, Any], where: str) -> None:
        path = p.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            return
        # "/files/inbox/* is unchanged" means exactly "/files/inbox is unchanged":
        # a subtree comparison is already recursive. Only safe for changed/unchanged;
        # for every other operator a wildcard is genuinely ambiguous and is left to
        # fail the lint.
        if path.endswith("/*") and str(p.get("op")) in ("unchanged", "changed"):
            p["path"] = path = path[:-2] or "/"
            fixes.append(f"{where}: rewrote trailing '/*' as the parent path for op "
                         f"'{p.get('op')}'")
        parts = path.split("/")
        if len(parts) < 3:
            return
        head = parts[1]
        if head in roots or head.lower() not in _POINTER_WRAPPERS:
            return
        if parts[2] in roots:
            p["path"] = "/" + "/".join(parts[2:])
            fixes.append(f"{where}: stripped pointer wrapper '/{head}'")

    # Bare table names in v0.4+ predicates: qualify deterministically when the projection
    # has exactly one table of that name. Observed on b7a9ee9_1 (v0.4): a repair round
    # returned `Song`, `PlaylistSong`, `UserArtistFollowing` and died on a schema error.
    fields = initial_state.get("fields") if isinstance(initial_state.get("fields"), dict) else {}
    by_name: dict[str, list[str]] = {}
    for app, tables in fields.items():
        if isinstance(tables, dict):
            for t in tables:
                by_name.setdefault(t, []).append(f"{app}.{t}")

    def qualify(name: Any, where: str) -> Any:
        if not isinstance(name, str) or "." in name or not name:
            return name
        cands = by_name.get(name) or by_name.get(name.strip("/").split("/")[-1]) or []
        if len(cands) == 1:
            fixes.append(f"{where}: qualified bare table {name!r} as {cands[0]!r}")
            return cands[0]
        return name

    def fix_tables(p: dict[str, Any], where: str) -> None:
        if p.get("kind") == "record":
            new = qualify(p.get("table"), where)
            if new != p.get("table"):
                p["table"] = new
                p["path"] = f"/fields/{new.replace('.', '/')}"
            if isinstance(p.get("where"), dict):
                _qualify_in_where(p["where"], where, qualify)
        if p.get("kind") == "scope" and isinstance(p.get("value"), list):
            p["value"] = [qualify(v, f"{where}.value") for v in p["value"]]

    for key in ("required", "forbidden", "invariants"):
        for i, p in enumerate(obj.get(key, []) or []):
            if isinstance(p, dict):
                fix(p, f"{key}[{i}]")
                fix_tables(p, f"{key}[{i}]")
    for gi, grp in enumerate(obj.get("alternatives", []) or []):
        for i, p in enumerate(grp or []):
            if isinstance(p, dict):
                fix(p, f"alternatives[{gi}][{i}]")
                fix_tables(p, f"alternatives[{gi}][{i}]")
    return fixes


def _qualify_in_where(where: dict[str, Any], ctx: str, qualify) -> None:
    for fname, cond in where.items():
        if isinstance(cond, dict):
            for op, val in cond.items():
                if isinstance(val, dict) and "table" in val:
                    val["table"] = qualify(val.get("table"), f"{ctx}.{fname}.{op}")
                    if isinstance(val.get("where"), dict):
                        _qualify_in_where(val["where"], f"{ctx}.{fname}.{op}", qualify)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def contract_hash(contract: EffectContract | dict[str, Any]) -> str:
    """Stable hash of a contract's *semantic* content (metadata excluded)."""
    d = contract.to_dict() if isinstance(contract, EffectContract) else dict(contract)
    d = {k: v for k, v in d.items() if k != "metadata"}
    return hashlib.sha256(canonical_json(d).encode()).hexdigest()[:16]


def state_hash(state: Any) -> str:
    return hashlib.sha256(canonical_json(state).encode()).hexdigest()[:16]
