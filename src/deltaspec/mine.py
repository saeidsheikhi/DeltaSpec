"""Deterministic effect-fact mining from a reference execution.

Input: the pre-state and post-state in the tiers-0+1 representation (see
scripts/appworld_worker.py), the per-table delta, and nothing else. Output: an ordered
list of candidate *facts*, each true of the reference transition by construction, each
carrying (a) a DSL v0.5 predicate form the evaluator can check on any transition, (b) a
plain-language rendering for the intent labeller, (c) a kind, (d) the table.

Rules that keep the facts from being tautological or incidental:
  * identity, timestamp and hash fields never become required values;
  * counts are lower bounds (`_ge 1`), never exact;
  * a relation fact is emitted only when the related set is a *proper* subset of the
    table's projected rows (otherwise it constrains nothing);
  * selectors that pick updated rows are mined only from fields whose value is shared by
    every updated row and by no other row of the table (a discriminating constant).
Everything is a pure function of its inputs; iteration order is sorted.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any

from effectgate.contracts.fieldview import select_rows

IDENTITY_FIELDS = {"id", "record_hash", "user_id"}
TIMESTAMP_TYPES = {"datetime"}
MAX_STR_VALUE = 160
MAX_FACTS_PER_TABLE = 12
MAX_RELATION_DEPTH = 2


@dataclass
class Fact:
    fid: str
    kind: str                       # table_added | table_removed | table_updated | added_field |
                                    # added_relation | updated_selector_field | updated_to | prohibition
    table: str
    text: str                       # plain-language rendering shown to the labeller
    predicates: list[dict[str, Any]]  # DSL v0.5 predicates asserting the fact (AND)
    clause: str = "required"        # required | forbidden
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"fid": self.fid, "kind": self.kind, "table": self.table, "text": self.text,
                "clause": self.clause, "predicates": self.predicates, "detail": self.detail}


def _pred(kind: str, op: str, table: str | None = None, path: str | None = None, where=None,
          fld: str | None = None, value=None, desc: str = "") -> dict[str, Any]:
    return {"kind": kind, "op": op, "table": table, "path": path or "", "where": where, "field": fld,
            "value": value, "description": desc, "critical": True}


def _tables(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for app, ts in (state.get("fields") or {}).items():
        if isinstance(ts, dict):
            for t, v in ts.items():
                out[f"{app}.{t}"] = v
    return out


def _is_value_field(fname: str, ftype: str) -> bool:
    return fname not in IDENTITY_FIELDS and ftype not in TIMESTAMP_TYPES and not fname.endswith("_at")


def _scalar_ok(v: Any) -> bool:
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return 0 < len(v) <= MAX_STR_VALUE
    return False


def _fmt(v: Any) -> str:
    return repr(v) if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else str(v)


# ------------------------------------------------------------------- relations
def _relation_candidates(table: str, col: str, target: str, tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Sub-selections {table, field, where} whose value set is a proper subset of `target`
    ids reachable from owner-scoped rows via declared FKs, depth <= 2."""
    out = []
    tv = tables.get(target)
    if not tv:
        return out
    # direct: target rows themselves are owner-scoped -> "one of my <target> rows"
    if tv.get("scope") == "owner":
        out.append({"table": target, "field": tv.get("primary_key", "id"), "where": None,
                    "text": f"one of the supervisor's own {target.split('.')[1]} rows"})
    # one hop: a table J with a FK column c_j -> target, J owner/owner_child scoped
    for jname, jv in sorted(tables.items()):
        if jname == table or jv.get("scope") not in ("owner", "owner_child"):
            continue
        for c_j, tgt in sorted((jv.get("foreign_keys") or {}).items()):
            if tgt == target:
                out.append({"table": jname, "field": c_j, "where": None,
                            "text": f"referenced by the supervisor's {jname.split('.')[1]} rows ({c_j})"})
    # list-of-id fields on owner rows (e.g. MusicPlayer.queue_song_ids): an implicit relation
    # the evaluator accepts (FK column <-> list of the target's ids); see fieldview.relation_is_declared
    for jname, jv in sorted(tables.items()):
        if jname == table or jv.get("scope") != "owner":
            continue
        for fname, ftype in sorted((jv.get("field_types") or {}).items()):
            if ftype == "list" and fname.endswith("_ids"):
                out.append({"table": jname, "field": fname, "where": None,
                            "text": f"listed in the supervisor's {jname.split('.')[1]}.{fname}"})
    return out


def _values_of(state: dict[str, Any], sub: dict[str, Any]) -> set:
    try:
        rows = select_rows(_tables(state)[sub["table"]], sub.get("where"), sub["table"], state)
    except Exception:
        return set()
    vals = set()
    for r in rows.values():
        v = r.get(sub["field"])
        if isinstance(v, list):
            vals.update(x for x in v if isinstance(x, (int, str)) and not isinstance(x, bool))
        elif v is not None:
            vals.add(v)
    return vals


# ------------------------------------------------------------------------ mine
def mine_facts(pre: dict[str, Any], post: dict[str, Any], delta: dict[str, Any]) -> list[Fact]:
    facts: list[Fact] = []
    n = 0

    def new(kind, table, text, preds, clause="required", **detail) -> Fact:
        nonlocal n
        n += 1
        f = Fact(f"F{n}", kind, table, text, preds, clause, dict(detail))
        facts.append(f)
        return f

    pre_t, post_t = _tables(pre), _tables(post)
    changed = sorted(delta)
    for table in changed:
        d = delta[table]
        app, tname = table.split(".")
        path = f"/records/{app}/{tname}"
        pv, qv = pre_t.get(table), post_t.get(table)
        n_add, n_rem, n_upd = d.get("n_added", 0), d.get("n_removed", 0), d.get("n_updated", 0)

        if n_add:
            new("table_added", table, f"{n_add} new row(s) were created in {table}",
                [_pred("delta", "added_count_ge", path=path, value=1, desc=f"rows created in {table}")], n=n_add)
            # Exact cardinality. Legitimate only when the task itself fixes the number
            # ("all", "every", "each", "both", an explicit count): then the count follows
            # from the pre-state and any correct version must match it. The labeller
            # decides; unlabelled or incidental, it is dropped. This is what separates
            # "at least one artist followed" from "all artists followed" — the partial
            # completion a lower bound cannot see (dev finding, b7a9ee9_1).
            new("added_count_exact", table,
                f"exactly {n_add} row(s) were created in {table} — the instruction determines this number "
                f"(e.g. 'all', 'every', 'each', an explicit count) rather than leaving it open",
                [_pred("delta", "added_count_eq", path=path, value=n_add, desc=f"exactly {n_add} rows created in {table}")], n=n_add)
        if n_rem:
            new("table_removed", table, f"{n_rem} row(s) were deleted from {table}",
                [_pred("delta", "removed_count_ge", path=path, value=1, desc=f"rows deleted from {table}")], n=n_rem)
            new("removed_count_exact", table,
                f"exactly {n_rem} row(s) were deleted from {table} — the instruction determines this number",
                [_pred("delta", "removed_count_eq", path=path, value=n_rem, desc=f"exactly {n_rem} rows deleted from {table}")], n=n_rem)
        if n_upd:
            new("table_updated", table, f"{n_upd} existing row(s) in {table} were modified",
                [_pred("delta", "updated_count_ge", path=path, value=1, desc=f"rows modified in {table}")], n=n_upd)
            if not n_add and not n_rem:
                new("updated_count_exact", table,
                    f"exactly {n_upd} row(s) in {table} were modified — the instruction determines this number",
                    [_pred("delta", "updated_count_eq", path=path, value=n_upd, desc=f"exactly {n_upd} rows modified in {table}")], n=n_upd)

        # --- tier-1 detail on ADDED rows
        if n_add and qv and pv is not None:
            added = {rid: r for rid, r in (qv.get("rows") or {}).items() if rid not in (pv.get("rows") or {})}
            ftypes = qv.get("field_types") or {}
            fks = qv.get("foreign_keys") or {}
            per_table = 0
            if added:
                for fname in sorted(ftypes):
                    if per_table >= MAX_FACTS_PER_TABLE:
                        break
                    if not _is_value_field(fname, ftypes[fname]) or fname in fks:
                        continue
                    vals = {r.get(fname) if not isinstance(r.get(fname), list) else None for r in added.values()}
                    if len(vals) == 1:
                        v = next(iter(vals))
                        if v is None or not _scalar_ok(v):
                            continue
                        # non-trivial: some pre-existing row of the table has a different value, or the table was empty
                        others = [r.get(fname) for rid, r in (pv.get("rows") or {}).items()]
                        if others and all(o == v for o in others) and len(others) > 3:
                            continue        # the whole table already has this value; not informative
                        new("added_field", table,
                            f"every new {table} row has {fname} = {_fmt(v)}",
                            [_pred("record", "added_count_ge", table=table, where={fname: {"eq": v}}, value=1,
                                   desc=f"new {table} rows with {fname}={_fmt(v)}"),
                             _pred("record", "added_count_eq", table=table, where={fname: {"ne": v}}, value=0,
                                   desc=f"no new {table} row with {fname}!={_fmt(v)}")],
                            field=fname, value=v)
                        per_table += 1
                # relations of added rows through FK columns
                for col in sorted(fks):
                    if col in IDENTITY_FIELDS or per_table >= MAX_FACTS_PER_TABLE:
                        continue
                    target = fks[col]
                    if str(target).endswith(".User"):
                        continue
                    colvals = {r.get(col) for r in added.values() if r.get(col) is not None}
                    if not colvals:
                        continue
                    for sub in _relation_candidates(table, col, target, post_t):
                        rel = _values_of(post, sub)
                        if not rel or not colvals <= rel:
                            continue
                        # proper subset of the target's projected ids -> informative
                        tgt_ids = set()
                        tv = post_t.get(target) or {}
                        for rid, r in (tv.get("rows") or {}).items():
                            tgt_ids.add(r.get(tv.get("primary_key", "id"), rid))
                        if tgt_ids and rel >= tgt_ids:
                            continue
                        subsel = {"table": sub["table"], "field": sub["field"], "where": sub.get("where")}
                        new("added_relation", table,
                            f"every new {table} row's {col} is {sub['text']}",
                            [_pred("record", "added_count_ge", table=table, where={col: {"in_related": subsel}}, value=1,
                                   desc=f"new {table} rows related via {col}"),
                             _pred("record", "added_count_eq", table=table, where={col: {"not_in_related": subsel}}, value=0,
                                   desc=f"no new {table} row outside the relation on {col}")],
                            column=col, relation=subsel)
                        per_table += 1
                        break   # one relation fact per column: the first (most direct) candidate

        # --- tier-1 detail on UPDATED rows
        if n_upd and pv and qv:
            pre_rows, post_rows = pv.get("rows") or {}, qv.get("rows") or {}
            upd = {rid for rid in pre_rows if rid in post_rows and pre_rows[rid] != post_rows[rid]}
            ftypes = pv.get("field_types") or {}
            if upd:
                changed_fields = sorted({f for rid in upd for f in pre_rows[rid]
                                         if pre_rows[rid].get(f) != post_rows[rid].get(f) and _is_value_field(f, ftypes.get(f, ""))})
                # a discriminating constant selector for the updated rows. When every projected
                # row was updated (typically a one-row owner table such as MusicPlayer) no
                # selector is needed and any constant would be spurious.
                selector = None
                all_updated = set(upd) == set(pre_rows)
                for fname in (sorted(ftypes) if not all_updated else []):
                    if not _is_value_field(fname, ftypes[fname]) or fname in changed_fields or ftypes[fname] in ("list", "dict"):
                        continue
                    vals = {pre_rows[rid].get(fname) for rid in upd}
                    if len(vals) != 1:
                        continue
                    v = next(iter(vals))
                    if v is None or not _scalar_ok(v):
                        continue
                    if any(pre_rows[rid].get(fname) == v for rid in pre_rows if rid not in upd):
                        continue
                    selector = (fname, v)
                    break
                for g in changed_fields[:4]:
                    if selector or all_updated:
                        where = {selector[0]: {"eq": selector[1]}} if selector else None
                        rows_text = (f"the {table} row(s) with {selector[0]} = {_fmt(selector[1])}" if selector
                                     else f"the supervisor's {table} row(s)")
                        preds = [_pred("record", "field_changed", table=table, where=where, fld=g,
                                       desc=f"{g} changed on {rows_text}")]
                        text = f"{rows_text} had their {g} changed"
                        scalar_field = ftypes.get(g) not in ("list", "dict")
                        if scalar_field:
                            to_vals = {post_rows[rid].get(g) for rid in upd}
                            from_vals = {pre_rows[rid].get(g) for rid in upd}
                            if len(to_vals) == 1 and len(from_vals) == 1 and _scalar_ok(next(iter(to_vals))) and _scalar_ok(next(iter(from_vals))):
                                fv, tv_ = next(iter(from_vals)), next(iter(to_vals))
                                preds = [_pred("record", "field_transition", table=table, where=where, fld=g,
                                               value={"from": fv, "to": tv_}, desc=f"{g}: {_fmt(fv)} -> {_fmt(tv_)}")]
                                text += f" from {_fmt(fv)} to {_fmt(tv_)}"
                        new("updated_selector_field", table, text, preds,
                            selected_by=(selector[0] if selector else None), field=g)
                    else:
                        new("updated_field", table,
                            f"among the modified {table} rows, the field {g} changed",
                            [_pred("delta", "updated_count_ge", path=path, value=1, desc=f"{table} rows modified ({g})")],
                            field=g)

    # --- prohibitions: for changed tables and their FK targets, "nothing deleted"/"nothing added" where true
    prohibited_seen = set()
    for table in changed:
        d = delta[table]
        app, tname = table.split(".")
        cands = [table]
        tv = post_t.get(table) or {}
        cands += [t for t in sorted(set((tv.get("foreign_keys") or {}).values())) if t in post_t and not t.endswith(".User")]
        for t in cands:
            if t in prohibited_seen:
                continue
            prohibited_seen.add(t)
            dd = delta.get(t, {})
            a2, t2 = t.split(".")
            p2 = f"/records/{a2}/{t2}"
            if dd.get("n_removed", 0) == 0:
                new("prohibition", t, f"no row was deleted from {t}",
                    [_pred("delta", "removed_count_ge", path=p2, value=1, desc=f"a row was deleted from {t}")],
                    clause="forbidden", what="removed")
            if dd.get("n_added", 0) == 0 and t != table:
                new("prohibition", t, f"no row was added to {t}",
                    [_pred("delta", "added_count_ge", path=p2, value=1, desc=f"a row was added to {t}")],
                    clause="forbidden", what="added")
    return facts


def scope_predicate(delta: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "scope", "op": "changed_tables_subset", "value": sorted(delta), "path": "/records",
            "table": None, "where": None, "field": None,
            "description": "no table outside the reference change set may change", "critical": True}
