"""Synthetic state mutants of the reference transition, for offline oracle validation.

Each mutant is a *post-state* that differs from the known-good post-state in one
realistic way. A useful oracle must reject the ones that break the intended effect and
should tolerate the ones that do not. All mutants are deterministic functions of
(pre, post, delta); none needs the environment.

    missing_effect(T)      the reference's added rows in T are absent (and removed rows
                           restored): the task was not done on T
    wrong_value(T, f)      the reference's added rows in T have field f altered
    extra_row(T')          one extra row appears in a table the reference did not change
                           (collateral write)
    collateral_delete(T')  one pre-existing row disappears from a table the reference
                           did not change (destructive collateral)
    missing_update(T)      the reference's updated rows in T are reverted to pre values
Mutants are built on both tiers so the evaluator sees a consistent world.
"""
from __future__ import annotations

import copy
from typing import Any


def _tables(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for app, ts in (state.get("fields") or {}).items():
        if isinstance(ts, dict):
            for t, v in ts.items():
                out[f"{app}.{t}"] = v
    return out


def _rec(state: dict[str, Any], table: str) -> dict[str, str]:
    app, t = table.split(".")
    return state["records"].setdefault(app, {}).setdefault(t, {})


def _fields(state: dict[str, Any], table: str) -> dict[str, Any] | None:
    app, t = table.split(".")
    return ((state.get("fields") or {}).get(app) or {}).get(t)


def _bump_count(state: dict[str, Any], table: str, k: int) -> None:
    app, t = table.split(".")
    c = state["counts"].setdefault(app, {})
    c[t] = max(0, int(c.get(t, 0)) + k)


def missing_effect(pre: dict, post: dict, table: str) -> dict:
    m = copy.deepcopy(post)
    pre_r, post_r = _rec(pre, table), _rec(m, table)
    added = [rid for rid in post_r if rid not in pre_r]
    removed = [rid for rid in pre_r if rid not in post_r]
    for rid in added:
        del post_r[rid]
    for rid in removed:
        post_r[rid] = pre_r[rid]
    _bump_count(m, table, len(removed) - len(added))
    fv, pv = _fields(m, table), _fields(pre, table)
    if fv is not None and pv is not None:
        for rid in added:
            fv["rows"].pop(rid, None)
        for rid in removed:
            if rid in pv["rows"]:
                fv["rows"][rid] = copy.deepcopy(pv["rows"][rid])
        fv["n_projected"] = len(fv["rows"]); fv["n_total"] = fv["n_projected"]
    return m


def missing_update(pre: dict, post: dict, table: str) -> dict:
    m = copy.deepcopy(post)
    pre_r, post_r = _rec(pre, table), _rec(m, table)
    for rid in list(post_r):
        if rid in pre_r and pre_r[rid] != post_r[rid]:
            post_r[rid] = pre_r[rid]
    fv, pv = _fields(m, table), _fields(pre, table)
    if fv is not None and pv is not None:
        for rid, row in pv["rows"].items():
            if rid in fv["rows"] and fv["rows"][rid] != row:
                fv["rows"][rid] = copy.deepcopy(row)
    return m


def wrong_value(pre: dict, post: dict, table: str, fname: str) -> dict | None:
    m = copy.deepcopy(post)
    fv = _fields(m, table)
    if fv is None or fname not in (fv.get("field_types") or {}):
        return None
    pre_r = _rec(pre, table)
    added = [rid for rid in fv["rows"] if rid not in pre_r]
    if not added:
        return None
    ftype = fv["field_types"][fname]
    post_r = _rec(m, table)
    for rid in added:
        v = fv["rows"][rid].get(fname)
        if ftype == "bool":
            nv = (not v) if isinstance(v, bool) else True
        elif ftype in ("int", "float"):
            nv = (v if isinstance(v, (int, float)) else 0) + 1
        elif ftype == "str":
            nv = (v or "") + "_X"
        elif ftype == "list":
            nv = (v or []) + ["_X"]
        else:
            return None
        fv["rows"][rid][fname] = nv
        post_r[rid] = str(post_r.get(rid, "h")) + "'"
    return m


def extra_row(pre: dict, post: dict, table: str) -> dict | None:
    """A new row in a table the reference did not change: copy the first existing row."""
    m = copy.deepcopy(post)
    post_r = _rec(m, table)
    if not post_r:
        return None
    src = sorted(post_r)[0]
    new_id = f"mut_{table.replace('.', '_')}_{src}"
    post_r[new_id] = str(post_r[src]) + "*"
    _bump_count(m, table, 1)
    fv = _fields(m, table)
    if fv is not None and src in fv["rows"]:
        row = copy.deepcopy(fv["rows"][src]); row["id"] = new_id
        fv["rows"][new_id] = row; fv["n_projected"] += 1; fv["n_total"] += 1
    return m


def collateral_delete(pre: dict, post: dict, table: str) -> dict | None:
    m = copy.deepcopy(post)
    post_r = _rec(m, table)
    pre_r = _rec(pre, table)
    victims = [rid for rid in sorted(post_r) if rid in pre_r]
    if not victims:
        return None
    rid = victims[0]
    del post_r[rid]
    _bump_count(m, table, -1)
    fv = _fields(m, table)
    if fv is not None:
        fv["rows"].pop(rid, None); fv["n_projected"] = len(fv["rows"]); fv["n_total"] = fv["n_projected"]
    return m


def build_mutants(pre: dict, post: dict, delta: dict, max_per_kind: int = 3) -> list[dict[str, Any]]:
    """Deterministic list of {name, kind, table, state, expect} where expect is 'reject'
    for regressions and 'tolerate' for benign variants (none here: all are regressions)."""
    out = []
    changed = sorted(delta)
    for t in changed[:max_per_kind]:
        d = delta[t]
        if d.get("n_added") or d.get("n_removed"):
            out.append({"name": f"missing_effect:{t}", "kind": "missing_effect", "table": t,
                        "state": missing_effect(pre, post, t), "expect": "reject"})
        if d.get("n_updated") and not d.get("n_added"):
            out.append({"name": f"missing_update:{t}", "kind": "missing_update", "table": t,
                        "state": missing_update(pre, post, t), "expect": "reject"})
        fv = _fields(post, t)
        if d.get("n_added") and fv is not None:
            # alter the first non-identity scalar field on the added rows
            for fname, ftype in sorted((fv.get("field_types") or {}).items()):
                if fname in ("id", "user_id", "record_hash") or fname.endswith("_at") or ftype in ("datetime", "dict"):
                    continue
                mv = wrong_value(pre, post, t, fname)
                if mv is not None:
                    out.append({"name": f"wrong_value:{t}.{fname}", "kind": "wrong_value", "table": t,
                                "field": fname, "state": mv, "expect": "reject"})
                    break
    # collateral: a table in scope apps that the reference did not change
    all_tables = sorted(t for t in _tables(post) if t not in delta)
    for t in all_tables[:max_per_kind]:
        mv = extra_row(pre, post, t)
        if mv is not None:
            out.append({"name": f"extra_row:{t}", "kind": "extra_row", "table": t, "state": mv, "expect": "reject"})
            break
    for t in all_tables[:max_per_kind * 3]:
        mv = collateral_delete(pre, post, t)
        if mv is not None:
            out.append({"name": f"collateral_delete:{t}", "kind": "collateral_delete", "table": t,
                        "state": mv, "expect": "reject"})
            break
    return out
