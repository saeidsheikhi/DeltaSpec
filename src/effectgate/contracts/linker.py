"""Deterministic contract linker — EffectGate v0.6 (v0.6.1: rules A.root and B.vacuous_required).

Sits between LLM contract generation and validation/freezing, and replaces the LLM repair
round. The compiler is treated as a *semantic front-end*: it names effects, tables and
filter intent. The linker is the *back-end*: it binds identifiers to the schema, derives
relation paths from the declared foreign-key graph, removes what cannot be evaluated or
what the known-good execution contradicts, and derives collateral scope from what the
known-good execution actually changed.

Grounding, and nothing else:
  * the projected schema (tables, fields, canonical types, declared FKs, primary keys);
  * the known-good transition (pre-state, post-state, per-table delta) that the pipeline
    already executes before compilation;
  * deterministic lint/evaluator feedback.

Invariants of every rule:
  * deterministic (fixed rule order, stable iteration);
  * remove-or-weaken only, with two named exceptions that are *derived* from the known-good
    changed-table set (scope) and from tier-0 visibility (tier lift) — never invented;
  * every transformation logged with the rule id;
  * fail-closed: a contract that ends with no required effect is rejected, not emitted.

No SQL, no generated code, no task-specific rules. AppWorld's official evaluator is never
consulted; only the reference execution's states, which any release gate has by
construction (the currently-shipped version's behaviour).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from effectgate.contracts.evaluator import evaluate_contract
from effectgate.contracts.fieldview import (
    _check_type, check_selector_types, relation_is_declared, resolve_table, select_rows,
)
from effectgate.contracts.io import contract_from_dict, normalize_paths, normalize_raw
from effectgate.contracts.schema import lint_contract
from effectgate.models import (
    FIELD_OPS, MAX_RELATION_DEPTH, RECORD_DELTA_FIELD_OPS, RECORD_DELTA_OPS, RECORD_OPS,
    RECORD_SET_DELTA_OPS, RELATED_OPS, WHERE_OPS, ContractEvalError,
)

_DOTTED_PATH = re.compile(r"^(/(?:records|counts)/[a-z_]+)\.([A-Za-z_]+)$")


@dataclass
class LinkResult:
    status: str                       # ok | empty | error
    contract: dict[str, Any] | None
    transformations: list[str] = field(default_factory=list)
    n_before: int = 0
    n_after: int = 0
    lint_errors: list[str] = field(default_factory=list)
    accepts_known_good: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "n_before": self.n_before, "n_after": self.n_after,
                "n_transformations": len(self.transformations),
                "transformations": self.transformations, "lint_errors": self.lint_errors,
                "accepts_known_good": self.accepts_known_good, "error": self.error}


# ----------------------------------------------------------------- schema helpers
def _fields_tier(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for app, tables in (state.get("fields") or {}).items():
        if isinstance(tables, dict):
            for t, v in tables.items():
                out[f"{app}.{t}"] = v
    return out


def _tier0_tables(state: dict[str, Any]) -> set[str]:
    return {f"{app}.{t}" for app, tables in (state.get("records") or {}).items()
            if isinstance(tables, dict) for t in tables}


def _owner_columns(view: dict[str, Any]) -> set[str]:
    """Columns whose declared FK targets a User table: rows are already owner-scoped."""
    return {c for c, tgt in (view.get("foreign_keys") or {}).items()
            if str(tgt).endswith(".User") or str(tgt).endswith("users")}


def _n_preds(c: dict[str, Any]) -> int:
    return sum(len(c.get(k, []) or []) for k in ("required", "forbidden", "invariants")) + \
        sum(len(g) for g in (c.get("alternatives") or []))


# --------------------------------------------------------- stage A: binding
def _bind_paths(c: dict[str, Any], log: list[str], pre: dict[str, Any] | None = None) -> None:
    tier0 = _tier0_tables(pre) if pre is not None else set()
    for clause in ("required", "forbidden", "invariants"):
        for i, p in enumerate(c.get(clause, []) or []):
            path = p.get("path")
            if isinstance(path, str):
                m = _DOTTED_PATH.match(path)
                if m:
                    p["path"] = f"{m.group(1)}/{m.group(2)}"
                    log.append(f"A.path {clause}[{i}]: '{path}' -> '{p['path']}'")
                    path = p["path"]
            # v0.6.1 rule A.root. A record-delta operator (added/removed/updated_count_*) is
            # defined over the {record_id: record_hash} map, which lives under /records/;
            # under /counts/ the same table is an integer and the operator is an
            # evaluation error. When the table exists in the record tier, the operator
            # itself names the intended root, so the rebinding is type-driven, not a guess.
            # Found by the frozen model-axis probe: qwen3.5 located the effect on
            # b7a9ee9_1 and wrote it as /counts/spotify/UserArtistFollowing.
            if (p.get("kind") == "delta" and str(p.get("op", "")) in RECORD_DELTA_OPS
                    and isinstance(path, str) and path.startswith("/counts/")):
                parts = [x for x in path.split("/") if x]
                if len(parts) == 3 and f"{parts[1]}.{parts[2]}" in tier0:
                    p["path"] = f"/records/{parts[1]}/{parts[2]}"
                    log.append(f"A.root {clause}[{i}]: '{op_str(p)}' is a record-delta operator; "
                               f"'{path}' -> '{p['path']}'")


def op_str(p: dict[str, Any]) -> str:
    return str(p.get("op", ""))


def _bind_selector(where: Any, table: str, view: dict[str, Any], schema: dict[str, dict],
                   ctx: str, log: list[str], depth: int = 1) -> dict[str, Any] | None:
    """Return a selector with every unbindable condition removed. None = no selector."""
    if not isinstance(where, dict):
        return None
    ftypes = view.get("field_types") or {}
    owner = _owner_columns(view)
    out: dict[str, Any] = {}
    for fname, cond in where.items():
        if fname in owner:
            log.append(f"A.owner {ctx}: dropped '{fname}' condition (rows are already the supervisor's)"); continue
        if fname not in ftypes:
            log.append(f"A.field {ctx}: dropped condition on unknown field '{fname}'"); continue
        if not isinstance(cond, dict):
            cond = {"eq": cond}
        if not cond:
            log.append(f"A.empty {ctx}: dropped empty condition on '{fname}'"); continue
        new_cond: dict[str, Any] = {}
        for op, val in cond.items():
            # `in`/`not_in` carrying a sub-selection object is a relation spelled wrong
            if op in ("in", "not_in") and isinstance(val, dict) and "table" in val:
                op = op + "_related"
                log.append(f"A.op {ctx}.{fname}: '{op[:-8]}' with a sub-selection -> '{op}'")
            if op not in WHERE_OPS:
                log.append(f"A.op {ctx}.{fname}: dropped unknown selector operator '{op}'"); continue
            if op in RELATED_OPS:
                bound = _bind_relation(fname, op, val, table, view, schema, f"{ctx}.{fname}", log, depth)
                if bound is None:
                    continue
                op, val = bound
                new_cond[op] = val
                continue
            # literal typing
            try:
                if op == "is_null":
                    if not isinstance(val, bool):
                        raise ContractEvalError("is_null takes true/false")
                elif op in ("in", "not_in"):
                    if not isinstance(val, list):
                        raise ContractEvalError("needs a list")
                    for item in val:
                        _check_type(fname, ftypes[fname], item, op)
                elif op in ("contains", "not_contains"):
                    if ftypes[fname] not in ("list", "str"):
                        raise ContractEvalError("contains needs list/str")
                    if ftypes[fname] == "str" and not isinstance(val, str):
                        raise ContractEvalError("contains on str needs a string")
                elif op in ("gt", "ge", "lt", "le"):
                    if ftypes[fname] not in ("int", "float", "datetime", "str"):
                        raise ContractEvalError("no ordering")
                    _check_type(fname, ftypes[fname], val, op)
                else:
                    _check_type(fname, ftypes[fname], val, op)
            except ContractEvalError as exc:
                log.append(f"A.type {ctx}.{fname}: dropped '{op}' {val!r} ({exc})"); continue
            new_cond[op] = val
        if new_cond:
            out[fname] = new_cond
        else:
            log.append(f"A.cond {ctx}: dropped '{fname}' (no valid condition left)")
    return out or None


# ------------------------------------------------------- stage B: FK linking
def _fk_edges(schema: dict[str, dict]) -> dict[str, dict[str, tuple[str, str]]]:
    """table -> {column: (target_table, target_pk)} from declared FKs only."""
    edges: dict[str, dict[str, tuple[str, str]]] = {}
    for t, v in schema.items():
        for col, tgt in (v.get("foreign_keys") or {}).items():
            if tgt in schema and not (str(tgt).endswith(".User")):
                edges.setdefault(t, {})[col] = (tgt, schema[tgt].get("primary_key", "id"))
    return edges


def _derive_path(src: str, src_field: str, dst: str, schema: dict[str, dict],
                 max_depth: int = MAX_RELATION_DEPTH) -> list[tuple[str, str, str, str]] | None:
    """Shortest declared-FK path from (src.src_field) to a field of dst.

    Returns hops [(from_table, from_field, to_table, to_field), ...] such that
    from_field in from_table relates to to_field in to_table by a declared FK in either
    direction. BFS over tables; deterministic by sorted iteration.
    """
    edges = _fk_edges(schema)
    # adjacency: table -> list of (via_field_here, other_table, other_field)
    adj: dict[str, list[tuple[str, str, str]]] = {}
    for t, cols in edges.items():
        for col, (tgt, pk) in cols.items():
            adj.setdefault(t, []).append((col, tgt, pk))       # t.col -> tgt.pk
            adj.setdefault(tgt, []).append((pk, t, col))       # tgt.pk <- t.col
    # the first hop must leave src through src_field
    starts = [(f, t, g) for (f, t, g) in adj.get(src, []) if f == src_field]
    if not starts:
        return None
    from collections import deque
    best: list[tuple[str, str, str, str]] | None = None
    q = deque()
    for f, t, g in sorted(starts):
        q.append((t, g, [(src, f, t, g)]))
    seen = {src}
    while q:
        table, arrived_field, path = q.popleft()
        if table == dst:
            return path
        if len(path) >= max_depth or table in seen:
            continue
        seen.add(table)
        for f, t, g in sorted(adj.get(table, [])):
            if t in seen and t != dst:
                continue
            q.append((t, g, path + [(table, f, t, g)]))
    return best


def _bind_relation(fname: str, op: str, sub: Any, table: str, view: dict[str, Any],
                   schema: dict[str, dict], ctx: str, log: list[str], depth: int) -> tuple[str, Any] | None:
    if depth > MAX_RELATION_DEPTH:
        log.append(f"B.depth {ctx}: dropped relation (nesting > {MAX_RELATION_DEPTH})"); return None
    if not isinstance(sub, dict) or not sub.get("table"):
        log.append(f"B.shape {ctx}: dropped '{op}' (not a {{table, field, where}} sub-selection)"); return None
    otable = str(sub.get("table"))
    if otable not in schema:
        # qualify a bare name if unique
        cands = [t for t in schema if t.split(".")[1] == otable.split(".")[-1]]
        if len(cands) == 1:
            log.append(f"B.table {ctx}: qualified '{otable}' -> '{cands[0]}'"); otable = cands[0]
        else:
            log.append(f"B.table {ctx}: dropped relation to unknown table '{otable}'"); return None
    oview = schema[otable]
    oft = oview.get("field_types") or {}
    ofield = str(sub.get("field") or "")
    ftypes = view.get("field_types") or {}
    # direct declared relation?
    ok, how = (False, "none")
    if ofield in oft:
        ok, how = relation_is_declared(table, view, fname, otable, oview, ofield)
    if not ok:
        # try to bind the endpoint: this.fname -> otable via a declared FK path
        path = _derive_path(table, fname, otable, schema)
        if path is None:
            log.append(f"B.link {ctx}: dropped relation {table}.{fname} -> {otable}.{ofield or '?'} "
                       "(no declared foreign-key path)"); return None
        if len(path) == 1:
            _, _, _, tfield = path[0]
            log.append(f"B.link {ctx}: bound relation endpoint {otable}.{ofield or '?'} -> {otable}.{tfield}")
            ofield = tfield
        else:
            # nest the intermediate hops: this.f in_related(T1.g1 where g1' in_related(T2.g2 where ...))
            inner_where = sub.get("where")
            chain = None
            for (ft, ff, tt, tf) in reversed(path[1:]):
                chain = {"table": tt, "field": tf, "where": inner_where if chain is None else chain}
                inner_where = {ff: {"in_related": chain}}
                chain = inner_where
            first = path[0]
            new_sub = {"table": first[2], "field": first[3], "where": inner_where}
            log.append(f"B.link {ctx}: derived {len(path)}-hop path " +
                       " -> ".join(f"{h[0]}.{h[1]}" for h in path) + f" -> {path[-1][2]}.{path[-1][3]}")
            sub = new_sub
            otable, ofield, oview = sub["table"], sub["field"], schema[sub["table"]]
    if ofield not in (oview.get("field_types") or {}):
        log.append(f"B.field {ctx}: dropped relation ({otable} has no field '{ofield}')"); return None
    # list-vs-scalar op form
    if ftypes.get(fname) == "list" and op != "intersects_related":
        log.append(f"B.op {ctx}: '{op}' on list field -> 'intersects_related'"); op = "intersects_related"
    if ftypes.get(fname) != "list" and op == "intersects_related":
        log.append(f"B.op {ctx}: 'intersects_related' on scalar field -> 'in_related'"); op = "in_related"
    inner = _bind_selector(sub.get("where"), otable, oview, schema, f"{ctx}->{otable}", log, depth + 1)
    return op, {"table": otable, "field": ofield, "where": inner}


# ---------------------------------------------- stage A/B over whole contract
def _bind_contract(c: dict[str, Any], pre: dict[str, Any], log: list[str]) -> None:
    schema = _fields_tier(pre)
    tier0 = _tier0_tables(pre)
    for clause in ("required", "forbidden", "invariants"):
        kept = []
        for i, p in enumerate(c.get(clause, []) or []):
            ctx = f"{clause}[{i}]"
            kind, op = p.get("kind"), str(p.get("op", ""))
            if kind == "scope":
                if isinstance(p.get("value"), list):
                    vals = []
                    for t in p["value"]:
                        t = str(t).replace("/", ".").strip(".")
                        if t in schema or t in tier0:
                            vals.append(t)
                        else:
                            cands = [x for x in tier0 if x.split(".")[-1] == t.split(".")[-1]]
                            if len(cands) == 1:
                                log.append(f"A.table {ctx}: qualified scope table '{t}' -> '{cands[0]}'"); vals.append(cands[0])
                            else:
                                log.append(f"A.table {ctx}: dropped unknown scope table '{t}'")
                    p["value"] = sorted(set(vals))
                kept.append(p); continue
            if kind != "record":
                kept.append(p); continue
            table = str(p.get("table") or "").replace("/", ".").strip(".")
            if table not in schema:
                cands = [t for t in schema if t.split(".")[1] == table.split(".")[-1]]
                if len(cands) == 1:
                    log.append(f"A.table {ctx}: qualified '{table}' -> '{cands[0]}'"); table = cands[0]
                elif op in RECORD_SET_DELTA_OPS and any(x.split(".")[-1] == table.split(".")[-1] for x in tier0):
                    t0 = [x for x in tier0 if x.split(".")[-1] == table.split(".")[-1]]
                    if len(t0) == 1:
                        log.append(f"E.lift {ctx}: '{table}' has no field projection; lifted {op} to tier-0 delta on {t0[0]}")
                        kept.append({"kind": "delta", "path": "/records/" + t0[0].replace(".", "/"), "op": op,
                                     "value": p.get("value"), "description": p.get("description", ""),
                                     "critical": p.get("critical", True)})
                        continue
                    log.append(f"A.table {ctx}: dropped predicate on unknown table '{table}'"); continue
                else:
                    log.append(f"A.table {ctx}: dropped predicate on unknown table '{table}'"); continue
            p["table"] = table
            p["path"] = f"/fields/{table.replace('.', '/')}"
            view = schema[table]
            if op not in RECORD_OPS:
                log.append(f"A.op {ctx}: dropped predicate (operator '{op}' is not a record operator)"); continue
            if op in FIELD_OPS:
                if not p.get("field") or p["field"] not in (view.get("field_types") or {}):
                    log.append(f"A.field {ctx}: dropped predicate (unknown field '{p.get('field')}' on {table})"); continue
            p["where"] = _bind_selector(p.get("where"), table, view, schema, ctx, log)
            # self-referential selector: selecting on the field the predicate asserts transitions
            if op in RECORD_DELTA_FIELD_OPS and isinstance(p.get("where"), dict) and p.get("field") in p["where"]:
                del p["where"][p["field"]]
                p["where"] = p["where"] or None
                log.append(f"C.self {ctx}: dropped selector on '{p['field']}', the field the predicate asserts on")
            kept.append(p)
        c[clause] = kept


# ------------------------------------------ stage C/D: simplify + reconcile
def _table_of(p: dict[str, Any]) -> str | None:
    if p.get("kind") == "record":
        return p.get("table")
    path = str(p.get("path") or "")
    if path.startswith("/records/") or path.startswith("/counts/"):
        parts = [x for x in path.split("/") if x]
        if len(parts) >= 3:
            return f"{parts[1]}.{parts[2]}"
    return None


def _reconcile(c: dict[str, Any], pre: dict[str, Any], post: dict[str, Any],
               delta: dict[str, Any], log: list[str]) -> None:
    """Remove predicates the known-good transition contradicts; derive scope; tier-lift."""
    schema = _fields_tier(pre)
    changed = set(delta)
    # vacuous predicates. v0.6.1 rule B: a REQUIRED lower bound of 0 is an *invalid
    # required effect* -- it cannot fail, so it requires nothing. It is removed and named
    # as such. The intended magnitude is NOT inferred (0 is never read as 1): no grounded
    # deterministic rule can supply a threshold without inventing it, so if nothing else
    # requires the effect the contract fails closed (F.empty) with this as the reason.
    # Found by the frozen model-axis probe: qwen3.5 located the effect on 692c77d_1 and
    # 229360a_1 and wrote `_ge 0`.
    for clause in ("required", "forbidden", "invariants"):
        kept = []
        for i, p in enumerate(c.get(clause, []) or []):
            op = str(p.get("op", ""))
            if op.endswith("_count_ge") and p.get("value") == 0:
                if clause == "required":
                    log.append(f"B.vacuous_required {clause}[{i}]: '{op} 0' is an invalid required "
                               "effect (a lower bound of 0 cannot fail); removed, not read as 1")
                else:
                    log.append(f"C.vacuous {clause}[{i}]: dropped '{op} 0' (cannot fail)")
                continue
            kept.append(p)
        c[clause] = kept

    # required: effect predicates on tables with no such known-good effect -> tier lift or drop
    kept = []
    for i, p in enumerate(c.get("required", []) or []):
        ctx = f"required[{i}]"
        op = str(p.get("op", ""))
        table = _table_of(p)
        d = delta.get(table or "", {})
        if op.startswith(("added_", "removed_", "updated_")) and table:
            key = "n_" + op.split("_")[0]
            if p.get("kind") == "record" and d.get(key, 0) > 0:
                # tier-1 may not see rows that belong to other users: lift to tier 0 if tier-1 shows none
                try:
                    pre_v, post_v = resolve_table(pre, table), resolve_table(post, table)
                    pre_ids, post_ids = set(pre_v.get("rows") or {}), set(post_v.get("rows") or {})
                    seen = {"n_added": len(post_ids - pre_ids), "n_removed": len(pre_ids - post_ids),
                            "n_updated": len([r for r in pre_ids & post_ids
                                              if (pre_v["rows"][r] != post_v["rows"][r])])}[key]
                except ContractEvalError:
                    seen = 0
                if seen == 0:
                    log.append(f"E.lift {ctx}: {op} on {table} is visible only at tier 0 "
                               f"(other users' rows); lifted to /records/{table.replace('.', '/')}")
                    kept.append({"kind": "delta", "path": "/records/" + table.replace(".", "/"), "op": op,
                                 "value": p.get("value"), "description": p.get("description", ""),
                                 "critical": p.get("critical", True)})
                    continue
            if d.get(key, 0) == 0:
                log.append(f"D.effect {ctx}: dropped {op} on {table} (known-good run shows no such effect)"); continue
        kept.append(p)
    c["required"] = kept

    # scope: exactly one, derived from what the known-good run changed (never narrower)
    scopes = [p for p in c.get("invariants", []) if p.get("kind") == "scope"]
    others = [p for p in c.get("invariants", []) if p.get("kind") != "scope"]
    listed: set[str] = set()
    for p in scopes:
        listed |= set(p.get("value") or [])
    if scopes or changed:
        merged = sorted(listed | changed)
        if not scopes:
            log.append(f"E.scope invariants: derived scope from known-good changed tables {sorted(changed)}")
        elif changed - listed:
            log.append(f"E.scope invariants: widened scope by {sorted(changed - listed)}")
        if len(scopes) > 1:
            log.append(f"E.scope invariants: merged {len(scopes)} scope predicates into one")
        others.append({"kind": "scope", "op": "changed_tables_subset", "value": merged, "path": "/records",
                       "description": "no table outside the known-good change set may change", "critical": True,
                       "table": None, "where": None, "field": None})
    c["invariants"] = others


def _evaluate_and_prune(c: dict[str, Any], pre: dict[str, Any], post: dict[str, Any],
                        log: list[str]) -> tuple[bool, list[str]]:
    """Evaluate on the known-good transition; drop each predicate that rejects it (once)."""
    norm, _ = normalize_raw(c, c.get("task_id", "t"))
    normalize_paths(norm, pre)
    try:
        contract = contract_from_dict(norm)
    except Exception as exc:  # noqa: BLE001
        return False, [f"{type(exc).__name__}: {exc}"]
    res = evaluate_contract(contract, pre, post)
    if res.passed:
        c.update({k: norm[k] for k in ("required", "forbidden", "invariants", "alternatives")})
        return True, []
    # map outcomes back by (group, index)
    bad: dict[str, set[int]] = {}
    idx: dict[str, int] = {}
    for o in res.outcomes:
        g = o.group
        i = idx.get(g, 0); idx[g] = i + 1
        if not o.satisfied and not g.startswith("alternatives["):
            bad.setdefault(g, set()).add(i)
            why = o.error or ("true in the known-good state" if g == "forbidden" else "false in the known-good state")
            p = norm[g][i]
            # weaken an unmatched field_transition to field_changed before dropping
            if g == "required" and p.get("op") == "field_transition" and not o.error:
                p2 = dict(p, op="field_changed", value=None)
                r2 = evaluate_contract(contract_from_dict(dict(norm, required=[p2], forbidden=[], invariants=[])), pre, post)
                if r2.passed:
                    norm[g][i] = p2
                    bad[g].discard(i)
                    log.append(f"D.weaken {g}[{i}]: field_transition on '{p.get('field')}' -> field_changed "
                               "(the known-good run changed the field but not from the stated value)")
                    continue
            log.append(f"D.reject {g}[{i}]: dropped {p.get('op')} on {_table_of(p) or p.get('path')} ({why[:80]})")
    for g, ids in bad.items():
        norm[g] = [p for i, p in enumerate(norm[g]) if i not in ids]
    c.update({k: norm[k] for k in ("required", "forbidden", "invariants", "alternatives")})
    return False, []


# --------------------------------------------------------------------- entry
def link_contract(contract_dict: dict[str, Any], pre_state: dict[str, Any], post_state: dict[str, Any],
                  delta: dict[str, Any], task_id: str | None = None) -> LinkResult:
    """Bind, link, simplify and reconcile one compiled contract. Deterministic."""
    log: list[str] = []
    c = copy.deepcopy(contract_dict)
    if task_id:
        c["task_id"] = task_id
    n_before = _n_preds(c)
    try:
        _bind_paths(c, log, pre_state)
        _bind_contract(c, pre_state, log)
        _reconcile(c, pre_state, post_state, delta, log)
        # iterate evaluate-and-prune to a fixed point (bounded: each round removes >= 1)
        accepted = False
        for _ in range(8):
            accepted, errs = _evaluate_and_prune(c, pre_state, post_state, log)
            if errs:
                return LinkResult("error", None, log, n_before, _n_preds(c), errs, None, errs[0])
            if accepted:
                break
        if not c.get("required") and not c.get("alternatives"):
            vac = [l for l in log if l.startswith("B.vacuous_required")]
            reason = ("no required effect survived linking"
                      + ("; the only required effect(s) had a vacuous lower bound of 0" if vac else ""))
            log.append(f"F.empty: {reason}; fail closed")
            return LinkResult("empty", None, log, n_before, 0, [], accepted, reason)
        norm, _ = normalize_raw(c, c.get("task_id", "t"))
        normalize_paths(norm, pre_state)
        rep = lint_contract(contract_from_dict(norm), pre_state)
        if not rep.ok:
            return LinkResult("error", norm, log, n_before, _n_preds(norm), rep.errors, accepted,
                              "; ".join(rep.errors[:3]))
        return LinkResult("ok", norm, log, n_before, _n_preds(norm), [], accepted, None)
    except Exception as exc:  # noqa: BLE001 - deterministic containment; never emit a half-linked contract
        return LinkResult("error", None, log, n_before, _n_preds(c), [f"{type(exc).__name__}: {exc}"],
                          None, f"{type(exc).__name__}: {exc}")


__all__ = ["LinkResult", "link_contract"]
